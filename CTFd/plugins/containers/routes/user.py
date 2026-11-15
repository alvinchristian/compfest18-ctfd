"""
User-facing Routes - Container operations for players
"""
from calendar import timegm
from hmac import compare_digest

from flask import Blueprint, jsonify, request

from CTFd.models import db
from CTFd.plugins import bypass_csrf_protection
from CTFd.utils import get_config
from CTFd.utils.decorators import (
    authed_only,
    during_ctf_time_only,
    ratelimit,
    require_verified_emails,
)
from CTFd.utils.user import get_current_user
from CTFd.exceptions import UserNotFoundException, UserTokenExpiredException
from CTFd.utils.security.auth import lookup_user_token

from ..models.challenge import ContainerChallenge
from ..models.config import ContainerConfig
from ..models.instance import ContainerInstance
from ..models.queue import ContainerQueueRequest
from ..services.container_service import (
    ContainerQueueActionBlocked,
    ContainerCreationRateLimitExceeded,
    ContainerQueued,
    ContainerRunningLimitExceeded,
)
from ..services.port_manager import NoAvailablePort

user_bp = Blueprint("containers_user", __name__, url_prefix="/api/v1/containers")


def _utc_timestamp_ms(dt):
    """Convert a naive UTC datetime to milliseconds since epoch."""
    return int(timegm(dt.timetuple()) * 1000)


# Global services (will be injected by plugin init)
container_service = None
flag_service = None
anticheat_service = None


def set_services(c_service, f_service, a_service):
    """Inject services"""
    global container_service, flag_service, anticheat_service
    container_service = c_service
    flag_service = f_service
    anticheat_service = a_service


def get_account_id():
    """
    Get account ID based on CTF mode
    Returns: (account_id, is_team_mode)
    """
    user = get_current_user()
    if not user:
        raise Exception("User not authenticated")

    mode = get_config("user_mode")
    is_team_mode = mode == "teams"

    if is_team_mode:
        if not user.team_id:
            raise Exception("You must be on a team to access this feature")
        return user.team_id, True
    else:
        return user.id, False


@user_bp.route("/proxy/validate", methods=["POST"])
@bypass_csrf_protection
def validate_proxy_token():
    """
    Validate an access token for the remote auth proxy daemon.

    This endpoint is authenticated with the plugin's proxy_secret, not a user
    session, because the caller is infrastructure running on the challenge VM.
    """
    try:
        data = request.get_json() or {}
        expected_secret = ContainerConfig.get("proxy_secret") or ""
        provided_secret = data.get("secret") or ""
        if not compare_digest(provided_secret, expected_secret):
            return jsonify({"allowed": False}), 403

        public_port = data.get("port")
        token_value = data.get("token")
        if not public_port:
            return jsonify({"allowed": False}), 400

        instance = ContainerInstance.query.filter_by(
            connection_port=int(public_port), status="running"
        ).first()
        if not instance or instance.is_expired():
            return jsonify({"allowed": False})

        connection_info = instance.connection_info or {}
        response = {
            "allowed": False,
            "type": connection_info.get("type", "tcp"),
        }

        # Route lookup for the trusted proxy daemon before it knows how to ask
        # the client for credentials.
        if not token_value:
            return jsonify(response)

        try:
            token_user = lookup_user_token(token_value.strip())
        except (UserNotFoundException, UserTokenExpiredException):
            return jsonify(response)

        if get_config("user_mode") == "teams":
            allowed = token_user.team_id == instance.account_id
        else:
            allowed = token_user.id == instance.account_id

        if allowed:
            instance.last_accessed_at = db.func.now()
            db.session.commit()

        response["allowed"] = allowed
        if allowed:
            response.update(
                {
                    "target_host": connection_info.get("target_host"),
                    "target_port": connection_info.get("target_port"),
                }
            )
        return jsonify(response)

    except Exception:
        return jsonify({"allowed": False}), 500


@user_bp.route("/request", methods=["POST"])
@authed_only
@during_ctf_time_only
@require_verified_emails
@ratelimit(method="POST", limit=10, interval=60)
def request_container():
    """
    Request a new container or get existing one

    Body:
        {
            "challenge_id": 123
        }

    Response:
        {
            "status": "created" | "existing",
            "instance_uuid": "...",
            "connection": {
                "host": "...",
                "port": 12345,
                "type": "ssh",
                "info": "..."
            },
            "expires_at": "2024-01-01T00:00:00Z"
        }
    """
    try:
        data = request.get_json()
        challenge_id = data.get("challenge_id")

        if not challenge_id:
            return jsonify({"error": "challenge_id is required"}), 400

        user = get_current_user()
        account_id, is_team_mode = get_account_id()

        # Check if challenge exists
        challenge = ContainerChallenge.query.get(challenge_id)
        if not challenge:
            return jsonify({"error": "Challenge not found"}), 404

        # Check if already has running instance
        existing = (
            ContainerInstance.query.filter_by(
                challenge_id=challenge_id, account_id=account_id
            )
            .filter(ContainerInstance.status.in_(["running", "provisioning"]))
            .first()
        )

        if existing and not existing.is_expired():
            # Return existing instance
            return jsonify(
                {
                    "status": "existing",
                    "instance_uuid": existing.uuid,
                    "connection": {
                        "host": existing.connection_host,
                        "port": existing.connection_port,
                        "type": existing.connection_info.get("type")
                        if existing.connection_info
                        else "ssh",
                        "auth_proxy": existing.connection_info.get("auth_proxy", True)
                        if existing.connection_info
                        else True,
                        "info": existing.connection_info.get("info")
                        if existing.connection_info
                        else "",
                    },
                    "expires_at": _utc_timestamp_ms(existing.expires_at),
                    "renewal_count": existing.renewal_count,
                    "max_renewals": challenge.get_max_renewals(),
                }
            )

        # Create new instance
        instance = container_service.create_instance(
            challenge_id=challenge_id, account_id=account_id, user_id=user.id
        )

        return jsonify(
            {
                "status": "created",
                "instance_uuid": instance.uuid,
                "connection": {
                    "host": instance.connection_host,
                    "port": instance.connection_port,
                    "type": instance.connection_info.get("type")
                    if instance.connection_info
                    else "ssh",
                    "auth_proxy": instance.connection_info.get("auth_proxy", True)
                    if instance.connection_info
                    else True,
                    "info": instance.connection_info.get("info")
                    if instance.connection_info
                    else "",
                },
                "expires_at": _utc_timestamp_ms(instance.expires_at),
                "renewal_count": instance.renewal_count,
                "max_renewals": challenge.get_max_renewals(),
            }
        )

    except ContainerQueued as e:
        return jsonify({"status": "queued", "queue_uuid": e.queue_uuid, "message": str(e)}), 202
    except (ContainerCreationRateLimitExceeded, ContainerRunningLimitExceeded) as e:
        return jsonify({"error": str(e)}), 429
    except NoAvailablePort as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@user_bp.route("/info/<int:challenge_id>", methods=["GET"])
@authed_only
@during_ctf_time_only
@require_verified_emails
def get_container_info(challenge_id):
    """
    Get info about running container for a challenge

    Response:
        {
            "status": "running" | "not_found",
            "connection": {...},
            "expires_at": "..."
        }
    """
    try:
        account_id, _ = get_account_id()

        instance = (
            ContainerInstance.query.filter_by(
                challenge_id=challenge_id, account_id=account_id
            )
            .filter(ContainerInstance.status.in_(["running", "provisioning"]))
            .first()
        )

        if not instance or instance.is_expired():
            return jsonify({"status": "not_found"})

        # Update last accessed
        instance.last_accessed_at = db.func.now()
        db.session.commit()

        return jsonify(
            {
                "status": instance.status,
                "instance_uuid": instance.uuid,
                "connection": {
                    "host": instance.connection_host,
                    "port": instance.connection_port,
                    "type": instance.connection_info.get("type")
                    if instance.connection_info
                    else "ssh",
                    "auth_proxy": instance.connection_info.get("auth_proxy", True)
                    if instance.connection_info
                    else True,
                    "info": instance.connection_info.get("info")
                    if instance.connection_info
                    else "",
                },
                "expires_at": _utc_timestamp_ms(instance.expires_at),
                "renewal_count": instance.renewal_count,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@user_bp.route("/queue/cancel", methods=["POST"])
@authed_only
@during_ctf_time_only
@require_verified_emails
@ratelimit(method="POST", limit=10, interval=60)
def cancel_container_queue():
    """Cancel the current user's queued container request."""
    try:
        data = request.get_json(silent=True) or {}
        queue_uuid = data.get("queue_uuid")
        if not queue_uuid:
            return jsonify({"error": "queue_uuid is required"}), 400

        account_id, _ = get_account_id()
        queued = ContainerQueueRequest.query.filter_by(
            uuid=queue_uuid, account_id=account_id, status="queued"
        ).first()
        if not queued:
            return jsonify({"error": "Queued request not found"}), 404

        db.session.delete(queued)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@user_bp.route("/renew", methods=["POST"])
@authed_only
@during_ctf_time_only
@require_verified_emails
@ratelimit(method="POST", limit=10, interval=60)
def renew_container():
    """
    Renew (extend) container expiration

    Body:
        {
            "challenge_id": 123
        }

    Response:
        {
            "success": true,
            "expires_at": "...",
            "renewal_count": 2
        }
    """
    try:
        data = request.get_json()
        challenge_id = data.get("challenge_id")

        if not challenge_id:
            return jsonify({"error": "challenge_id is required"}), 400

        user = get_current_user()
        account_id, _ = get_account_id()

        if ContainerQueueRequest.query.filter(
            ContainerQueueRequest.challenge_id == challenge_id,
            ContainerQueueRequest.account_id == account_id,
            ContainerQueueRequest.status.in_(["queued", "processing"]),
        ).first():
            return jsonify({"error": "Container request is queued; it cannot be extended."}), 409

        instance = ContainerInstance.query.filter_by(
            challenge_id=challenge_id, account_id=account_id, status="running"
        ).first()

        if not instance:
            return jsonify({"error": "No running container found"}), 404

        instance = container_service.renew_instance(instance, user.id)

        return jsonify(
            {
                "success": True,
                "expires_at": _utc_timestamp_ms(instance.expires_at),
                "renewal_count": instance.renewal_count,
            }
        )

    except ContainerQueueActionBlocked as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@user_bp.route("/stop", methods=["POST"])
@authed_only
@during_ctf_time_only
@require_verified_emails
@ratelimit(method="POST", limit=10, interval=60)
def stop_container():
    """
    Stop running container

    Body:
        {
            "challenge_id": 123
        }

    Response:
        {
            "success": true
        }
    """
    try:
        data = request.get_json()
        challenge_id = data.get("challenge_id")

        if not challenge_id:
            return jsonify({"error": "challenge_id is required"}), 400

        user = get_current_user()
        account_id, _ = get_account_id()

        if ContainerQueueRequest.query.filter(
            ContainerQueueRequest.challenge_id == challenge_id,
            ContainerQueueRequest.account_id == account_id,
            ContainerQueueRequest.status.in_(["queued", "processing"]),
        ).first():
            return jsonify({"error": "Container request is queued; it cannot be stopped."}), 409

        instance = ContainerInstance.query.filter_by(
            challenge_id=challenge_id, account_id=account_id, status="running"
        ).first()

        if not instance:
            return jsonify({"error": "No running container found"}), 404

        success = container_service.stop_instance(instance, user.id, reason="manual")

        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"error": "Failed to stop container"}), 500

    except ContainerQueueActionBlocked as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500
