"""
Container Service - Business logic for container lifecycle
"""
import logging
from datetime import datetime, timedelta

from flask import request

from CTFd.models import db

from ..models.audit import ContainerAuditLog
from ..models.challenge import ContainerChallenge
from ..models.instance import ContainerInstance
from ..models.queue import ContainerQueueRequest
from .docker_service import AUTH_PROXY_SCRIPT, DockerService
from .flag_service import FlagService
from .port_manager import NoAvailablePort, PortManager
from .log_archive_service import LogArchiveService

logger = logging.getLogger(__name__)


class ContainerCreationRateLimitExceeded(Exception):
    """Raised when an account recreates an instance too quickly."""


class ContainerRunningLimitExceeded(Exception):
    """Raised when an account reaches its concurrent container limit."""


class ContainerQueued(Exception):
    def __init__(self, queue_uuid):
        self.queue_uuid = queue_uuid
        super().__init__("Container request queued: {}".format(queue_uuid))


class ContainerQueueActionBlocked(Exception):
    """Raised when lifecycle actions are attempted for a queued request."""


class ContainerService:
    """
    Service to manage container lifecycle
    """

    def __init__(
        self,
        docker_service: DockerService,
        flag_service: FlagService,
        port_manager: PortManager,
        direct_port_manager: PortManager = None,
    ):
        self.docker = docker_service
        self.flag_service = flag_service
        self.port_manager = port_manager
        self.direct_port_manager = direct_port_manager or port_manager
        self._cleanup_running = False  # Prevent overlapping cleanup jobs
        self.log_archive = LogArchiveService()

    def create_instance(
        self, challenge_id: int, account_id: int, user_id: int, from_queue=False
    ) -> ContainerInstance:
        """
        Create new container instance

        Args:
            challenge_id: Challenge ID
            account_id: Team ID (team mode) or User ID (user mode)
            user_id: Actual user ID creating container

        Returns:
            ContainerInstance object

        Raises:
            Exception if error occurs
        """
        # 1. Validate challenge exists
        challenge = ContainerChallenge.query.get(challenge_id)
        if not challenge:
            raise Exception("Challenge not found")

        # 2. Check if already has running instance
        existing = ContainerInstance.query.filter_by(
            challenge_id=challenge_id, account_id=account_id, status="running"
        ).first()

        if existing and not existing.is_expired():
            logger.info(
                f"Account {account_id} already has running instance for challenge {challenge_id}"
            )
            return existing

        # 3. Stop any existing expired instances
        if existing and existing.is_expired():
            logger.info(f"Stopping expired instance {existing.uuid}")
            self.stop_instance(existing, user_id, reason="expired")

        # 4. Enforce the account-wide concurrent container limit. In team mode,
        # account_id is the team id, so this limit is shared by teammates.
        self._enforce_running_container_limit(account_id)

        # 6. Enforce per-challenge account creation cooldown. In team mode,
        # account_id is the team id, so the cooldown is shared by teammates.
        # A queued request already passed this check when it was submitted.
        # Rechecking it would see the failed placeholder instance created by
        # the original request and could discard the queued request.
        if not from_queue:
            self._enforce_creation_rate_limit(challenge_id, account_id)

        # 7. Create instance record (status=pending)
        # Set expiration based on global timeout setting
        expires_at = datetime.utcnow() + timedelta(
            minutes=challenge.get_timeout_minutes()
        )

        # Generate flag
        flag_plaintext = self.flag_service.generate_flag(challenge)
        flag_encrypted = self.flag_service.encrypt_flag(flag_plaintext)
        flag_hash = self.flag_service.hash_flag(flag_plaintext)

        instance = ContainerInstance(
            challenge_id=challenge_id,
            account_id=account_id,
            flag_encrypted=flag_encrypted,
            flag_hash=flag_hash,
            status="pending",
            expires_at=expires_at,
        )

        db.session.add(instance)
        db.session.flush()  # Get instance ID

        # 8. Create flag record (only for random flag mode - anti-cheat tracking)
        if challenge.flag_mode == "random":
            self.flag_service.create_flag_record(
                instance, challenge, account_id, flag_plaintext
            )

        # 9. Audit log
        self._create_audit_log(
            "instance_created",
            instance_id=instance.id,
            challenge_id=challenge_id,
            account_id=account_id,
            user_id=user_id,
            details={"expires_at": expires_at.isoformat()},
        )

        db.session.commit()

        # 10. Provision container (async)
        try:
            self._provision_container(instance, challenge, flag_plaintext)
        except NoAvailablePort:
            if not from_queue and self._queue_enabled() and self._queue_has_capacity():
                queued = ContainerQueueRequest(challenge_id=challenge_id, account_id=account_id, user_id=user_id)
                db.session.add(queued)
                db.session.flush()
                instance.status = "error"
                instance.extra_data = {"queued": True, "queue_uuid": queued.uuid}
                db.session.commit()
                raise ContainerQueued(queued.uuid)
            instance.status = "error"
            instance.extra_data = {"error": "All container ports are currently in use"}
            db.session.commit()
            raise NoAvailablePort("Sorry, all container ports are currently in use.")
        except Exception as e:
            logger.error(f"Failed to provision container: {e}")
            instance.status = "error"
            instance.extra_data = {"error": str(e)}
            db.session.commit()
            raise

        return instance

    def _queue_enabled(self):
        from ..models.config import ContainerConfig
        return ContainerConfig.get("container_queue_enabled", "0") == "1"

    def _queue_has_capacity(self):
        from ..models.config import ContainerConfig
        try:
            maximum = int(ContainerConfig.get("container_queue_max_size", "100"))
        except ValueError:
            maximum = 100
        return maximum > 0 and ContainerQueueRequest.query.filter_by(status="queued").count() < maximum

    def _has_active_queue_request(self, challenge_id, account_id):
        return ContainerQueueRequest.query.filter(
            ContainerQueueRequest.challenge_id == challenge_id,
            ContainerQueueRequest.account_id == account_id,
            ContainerQueueRequest.status.in_(["queued", "processing"]),
        ).first() is not None

    def process_queue(self):
        if not self._queue_enabled():
            ContainerQueueRequest.query.filter(
                ContainerQueueRequest.status.in_(["queued", "processing"])
            ).delete(synchronize_session=False)
            db.session.commit()
            return
        queued = ContainerQueueRequest.query.filter_by(status="queued").order_by(ContainerQueueRequest.created_at).first()
        if not queued:
            return
        queued.status = "processing"
        db.session.commit()
        try:
            instance = self.create_instance(
                queued.challenge_id, queued.account_id, queued.user_id, from_queue=True
            )
            self._notify_queue_ready(queued, instance)
            db.session.delete(queued)
            db.session.commit()
        except NoAvailablePort:
            queued.status = "queued"
            db.session.commit()
        except Exception:
            db.session.delete(queued)
            db.session.commit()

    def _notify_queue_ready(self, queued, instance):
        """Create and publish a notification for the queued account only."""
        try:
            from flask import current_app
            from CTFd.models import Notifications
            from CTFd.schemas.notifications import NotificationSchema
            from CTFd.utils.config import is_teams_mode

            challenge = ContainerChallenge.query.get(queued.challenge_id)
            title = "Container instance is ready"
            content = (
                "Your queued container request for **{}** is now available. "
                "Connect using the details shown on the challenge page."
            ).format(challenge.name if challenge else "the challenge")

            notification = Notifications(title=title, content=content)
            if is_teams_mode():
                notification.team_id = queued.account_id
            else:
                notification.user_id = queued.user_id

            db.session.add(notification)
            db.session.commit()

            serialized = NotificationSchema().dump(notification)
            data = serialized.data if hasattr(serialized, "data") else serialized
            data["type"] = "toast"
            data["sound"] = True
            current_app.events_manager.publish(data=data, type="notification")
        except Exception:
            logger.exception(
                "Failed to notify account %s that queued container %s is ready",
                queued.account_id,
                queued.uuid,
            )

    def _enforce_running_container_limit(self, account_id: int):
        """Prevent an account from exceeding its concurrent container quota."""
        from ..models.config import ContainerConfig

        try:
            max_running = int(
                ContainerConfig.get("max_running_containers_per_account", 0)
            )
        except (TypeError, ValueError):
            max_running = 0

        # Zero (and invalid/negative values) means unlimited.
        if max_running <= 0:
            return

        active_count = ContainerInstance.query.filter(
            ContainerInstance.account_id == account_id,
            ContainerInstance.status.in_(["pending", "provisioning", "running"]),
        ).count()

        if active_count >= max_running:
            raise ContainerRunningLimitExceeded(
                "Maximum concurrent containers ({}) reached for this account.".format(
                    max_running
                )
            )

    def _enforce_creation_rate_limit(self, challenge_id: int, account_id: int):
        """Prevent rapid recreate loops for the same account/challenge."""
        from ..models.config import ContainerConfig

        try:
            cooldown_seconds = int(ContainerConfig.get("instance_rate_limit_seconds", 0))
        except (TypeError, ValueError):
            cooldown_seconds = 0

        if cooldown_seconds <= 0:
            return

        latest_instance = (
            ContainerInstance.query.filter_by(
                challenge_id=challenge_id,
                account_id=account_id,
            )
            .order_by(ContainerInstance.created_at.desc())
            .first()
        )

        if not latest_instance:
            return

        reference_time = latest_instance.stopped_at or latest_instance.created_at
        if not reference_time:
            return

        elapsed_seconds = int((datetime.utcnow() - reference_time).total_seconds())
        remaining_seconds = cooldown_seconds - elapsed_seconds
        if remaining_seconds <= 0:
            return

        raise ContainerCreationRateLimitExceeded(
            "Please wait {} before creating another instance for this challenge.".format(
                self._format_duration(remaining_seconds)
            )
        )

    @staticmethod
    def _format_duration(seconds: int) -> str:
        seconds = max(0, int(seconds))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)

        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if seconds or not parts:
            parts.append(f"{seconds}s")
        return " ".join(parts)

    def _provision_container(
        self, instance: ContainerInstance, challenge: ContainerChallenge, flag: str
    ):
        """
        Provision Docker container

        Args:
            instance: ContainerInstance object
            challenge: ContainerChallenge object
            flag: Plain text flag
        """
        # Update status
        instance.status = "provisioning"
        db.session.commit()

        result = None

        try:
            # 1. Allocate public port and create a private instance network.
            # The challenge container is not published to the host; the
            # node-level auth proxy daemon owns the public port range.
            port_manager = (
                self.port_manager if challenge.auth_required else self.direct_port_manager
            )
            public_port = port_manager.allocate_port(retry_seconds=3)
            network_name = f"ctfd_{instance.uuid.replace('-', '')[:12]}"

            # 2. Get connection host
            from ..models.config import ContainerConfig

            connection_host = ContainerConfig.get("connection_host", "localhost")
            if challenge.auth_required:
                proxy_image = ContainerConfig.get("auth_proxy_image", "python:3.11-alpine")
                ctfd_base_url = ContainerConfig.get("ctfd_base_url", "").rstrip("/")
                if not ctfd_base_url:
                    ctfd_base_url = request.url_root.rstrip("/")
                validate_url = f"{ctfd_base_url}/api/v1/containers/proxy/validate"
                proxy_secret = ContainerConfig.get("proxy_secret")
                if not proxy_secret:
                    raise Exception("Container proxy secret is not configured")
                import hashlib

                proxy_secret_hash = hashlib.sha256(proxy_secret.encode()).hexdigest()
                proxy_script_hash = hashlib.sha256(AUTH_PROXY_SCRIPT.encode()).hexdigest()

                self.docker.ensure_auth_proxy_daemon(
                    image=proxy_image,
                    environment={
                        "VALIDATE_URL": validate_url,
                        "PROXY_SECRET": proxy_secret,
                        "PORT_RANGE_START": ContainerConfig.get("port_range_start", "30000"),
                        "PORT_RANGE_END": ContainerConfig.get("port_range_end", "31000"),
                    },
                    labels={
                        "ctfd.role": "auth-proxy-daemon",
                        "ctfd.auth_proxy_image": proxy_image,
                        "ctfd.validate_url": validate_url,
                        "ctfd.proxy_secret_hash": proxy_secret_hash,
                        "ctfd.proxy_script_hash": proxy_script_hash,
                        "ctfd.port_range_start": ContainerConfig.get("port_range_start", "30000"),
                        "ctfd.port_range_end": ContainerConfig.get("port_range_end", "31000"),
                    },
                )

            network = self.docker.create_network(
                name=network_name,
                labels={
                    "ctfd.instance_uuid": instance.uuid,
                    "ctfd.challenge_id": str(challenge.id),
                    "ctfd.account_id": str(instance.account_id),
                },
            )

            # 3. Create Docker container
            # Generate container name: challengename_accountid
            import re

            # Sanitize challenge name (only alphanumeric and hyphens)
            safe_name = re.sub(
                r"[^a-zA-Z0-9-]", "", challenge.name.replace(" ", "-").lower()
            )
            instance_suffix = instance.uuid.replace("-", "")[:12]
            container_name = f"{safe_name}_{instance.account_id}_{instance_suffix}"

            # Replace {FLAG} placeholder in command if present
            command = challenge.command if challenge.command else None
            if command and "{FLAG}" in command:
                command = command.replace("{FLAG}", flag)

            flag_delivery_mode = (challenge.flag_delivery_mode or "environment").lower()
            environment = {"FLAG": flag}
            flag_file = None
            if flag_delivery_mode == "file":
                flag_file = challenge.get_flag_file_config()
                flag_file["content"] = flag + "\n"
                environment = {}
            elif flag_delivery_mode != "environment":
                raise ValueError(
                    "Unsupported flag delivery mode: {}".format(flag_delivery_mode)
                )

            result = self.docker.create_container(
                image=challenge.image,
                internal_port=challenge.internal_port,
                host_port=public_port if not challenge.auth_required else None,
                command=command,
                environment=environment,
                memory_limit=challenge.get_memory_limit(),
                cpu_limit=challenge.get_cpu_limit(),
                pids_limit=challenge.pids_limit,
                capabilities=challenge.get_capabilities(),
                no_new_privileges=challenge.get_no_new_privileges(),
                flag_file=flag_file,
                name=container_name,
                network=network.name,
                labels={
                    "ctfd.instance_uuid": instance.uuid,
                    "ctfd.challenge_id": str(challenge.id),
                    "ctfd.account_id": str(instance.account_id),
                    "ctfd.expires_at": str(instance.expires_at.timestamp()),
                    "ctfd.role": "challenge",
                },
            )

            target_host = self.docker.get_container_ip(
                result["container_id"], network.name
            )
            if not target_host:
                raise Exception("Failed to determine challenge container IP")

            # 4. Update instance
            instance.container_id = result["container_id"]
            instance.connection_host = connection_host
            instance.connection_port = public_port
            instance.connection_info = {
                "type": challenge.container_connection_type,
                "info": challenge.container_connection_info,
                "auth_proxy": challenge.auth_required,
                "network_name": network.name,
                "target_host": target_host,
                "target_port": challenge.internal_port,
            }
            instance.status = "running"
            instance.started_at = datetime.utcnow()

            db.session.commit()

            # 5. Schedule expiration in Redis (for accurate killing)
            try:
                from .. import redis_expiration_service

                if redis_expiration_service:
                    expires_in_seconds = int(
                        (instance.expires_at - datetime.utcnow()).total_seconds()
                    )
                    redis_expiration_service.schedule_expiration(
                        instance.uuid, expires_in_seconds
                    )
            except Exception as e:
                logger.warning(f"Failed to schedule Redis expiration: {e}")

            logger.info(
                f"Provisioned container {result['container_id'][:12]} for instance {instance.uuid}"
            )

            # Audit log
            self._create_audit_log(
                "instance_started",
                instance_id=instance.id,
                challenge_id=challenge.id,
                account_id=instance.account_id,
                details={
                    "container_id": result["container_id"],
                    "network_name": network.name,
                    "port": public_port,
                },
            )

        except Exception as e:
            logger.error(f"Error provisioning container: {e}")
            if result and result.get("container_id"):
                self.docker.stop_container(result["container_id"])
            if "network_name" in locals():
                self.docker.remove_network(network_name)
            instance.status = "error"
            instance.extra_data = {"error": str(e)}
            db.session.commit()
            raise

    def renew_instance(
        self, instance: ContainerInstance, user_id: int
    ) -> ContainerInstance:
        """
        Renew (extend) container expiration

        Args:
            instance: ContainerInstance object
            user_id: User requesting renewal

        Returns:
            Updated instance
        """
        if self._has_active_queue_request(instance.challenge_id, instance.account_id):
            raise ContainerQueueActionBlocked(
                "Container request is queued; it cannot be extended."
            )

        challenge = ContainerChallenge.query.get(instance.challenge_id)

        # Check renewal limit
        max_renewals = challenge.get_max_renewals()
        if instance.renewal_count >= max_renewals:
            raise Exception(f"Maximum renewals ({max_renewals}) reached")

        # Extend expiration by 5 minutes (fixed)
        extend_minutes = 5
        instance.extend_expiration(extend_minutes)
        instance.last_accessed_at = datetime.utcnow()

        db.session.commit()

        # Extend Redis TTL
        try:
            from .. import redis_expiration_service

            if redis_expiration_service:
                redis_expiration_service.extend_expiration(
                    instance.uuid, extend_minutes * 60  # 5 minutes = 300 seconds
                )
        except Exception as e:
            logger.warning(f"Failed to extend Redis expiration: {e}")

        # Audit log
        self._create_audit_log(
            "instance_renewed",
            instance_id=instance.id,
            challenge_id=instance.challenge_id,
            account_id=instance.account_id,
            user_id=user_id,
            details={
                "new_expires_at": instance.expires_at.isoformat(),
                "renewal_count": instance.renewal_count,
            },
        )

        logger.info(
            f"Renewed instance {instance.uuid} (renewal {instance.renewal_count})"
        )

        return instance

    def stop_instance(
        self, instance: ContainerInstance, user_id: int, reason="manual"
    ) -> bool:
        """
        Stop container instance

        Args:
            instance: ContainerInstance object
            user_id: User stopping the container
            reason: Reason for stopping ('manual', 'expired', 'solved')

        Returns:
            True if successful
        """
        if instance.status == "running" and self._has_active_queue_request(
            instance.challenge_id, instance.account_id
        ):
            raise ContainerQueueActionBlocked(
                "Container request is queued; it cannot be stopped."
            )

        if instance.status not in ("running", "provisioning", "stopping", "error"):
            return False

        instance.status = "stopping"
        db.session.commit()

        # Cancel Redis expiration
        try:
            from .. import redis_expiration_service

            if redis_expiration_service:
                redis_expiration_service.cancel_expiration(instance.uuid)
        except Exception as e:
            logger.warning(f"Failed to cancel Redis expiration: {e}")

        try:
            connection_info = instance.connection_info or {}
            network_name = connection_info.get("network_name")
            cleanup_failures = []

            # Stop Docker container
            if instance.container_id:
                from ..models.config import ContainerConfig

                collection_enabled = ContainerConfig.get(
                    "container_log_collection_enabled", "1"
                ) == "1"
                if collection_enabled:
                    # tail=None is intentional: archive the complete Docker
                    # history accumulated during the instance lifetime.
                    logs = self.docker.get_container_logs(
                        instance.container_id, tail=None, timestamps=True
                    )
                    self.log_archive.archive_instance(instance, logs)
                if not self.docker.stop_container(instance.container_id):
                    cleanup_failures.append(
                        f"container {instance.container_id[:12]}"
                    )

            if network_name:
                if not self.docker.remove_network(network_name):
                    cleanup_failures.append(f"network {network_name}")

            if cleanup_failures:
                raise Exception(
                    "Failed to stop/remove " + ", ".join(cleanup_failures)
                )

            # Release port back to pool
            if instance.connection_port:
                self.port_manager.release_port(instance.connection_port)
                logger.info(f"Released port {instance.connection_port}")

            # Update instance based on reason
            if reason == "solved":
                instance.status = "solved"
                instance.solved_at = datetime.utcnow()
            else:
                instance.status = "stopped"

            instance.stopped_at = datetime.utcnow()

            # Handle flag based on reason (only for random flag mode)
            if reason != "solved":
                # Get challenge to check flag mode
                challenge = ContainerChallenge.query.get(instance.challenge_id)
                if challenge and challenge.flag_mode == "random":
                    from ..models.flag import ContainerFlag

                    flag = ContainerFlag.query.filter_by(
                        instance_id=instance.id
                    ).first()
                    if flag:
                        # Delete flag instead of invalidating to prevent duplicate hash issues
                        # when user recreates container
                        db.session.delete(flag)
                        logger.info(
                            f"Deleted temporary flag for instance {instance.uuid}"
                        )

            db.session.commit()

            # Audit log
            self._create_audit_log(
                f"instance_stopped_{reason}",
                instance_id=instance.id,
                challenge_id=instance.challenge_id,
                account_id=instance.account_id,
                user_id=user_id,
                details={"reason": reason},
            )

            self.process_queue()

            logger.info(f"Stopped instance {instance.uuid} (reason: {reason})")

            return True

        except Exception as e:
            logger.error(f"Error stopping instance: {e}")
            instance.status = "error"
            instance.extra_data = {"error": str(e)}
            db.session.commit()
            return False

    def cleanup_expired_instances(self):
        """
        Background job: Cleanup expired instances

        Optimized for high volume (100+ containers):
        - Prevent overlapping runs
        - Batch processing (max 50 per run)
        - Timeout per container
        - Continue on error
        """
        # Prevent overlapping cleanup jobs
        if self._cleanup_running:
            logger.warning("Cleanup job already running, skipping this run")
            return

        self._cleanup_running = True

        try:
            import signal
            from contextlib import contextmanager

            @contextmanager
            def timeout(seconds):
                """Timeout context manager"""

                def timeout_handler(signum, frame):
                    raise TimeoutError(f"Operation timed out after {seconds}s")

                # Set alarm (Unix only)
                old_handler = signal.signal(signal.SIGALRM, timeout_handler)
                signal.alarm(seconds)
                try:
                    yield
                finally:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old_handler)

            # Get expired instances (limit to 50 per run to prevent overload)
            expired = (
                ContainerInstance.query.filter(
                    ContainerInstance.status == "running",
                    ContainerInstance.expires_at < datetime.utcnow(),
                )
                .limit(50)
                .all()
            )

            if not expired:
                return

            logger.warning(
                f"⚠️ [APSCHEDULER CLEANUP] Found {len(expired)} expired instances (Redis backup cleanup)"
            )

            cleaned = 0
            failed = 0

            for instance in expired:
                try:
                    # Timeout after 10 seconds per container
                    with timeout(10):
                        logger.warning(
                            f"🟡 [APSCHEDULER KILL] Cleaning up expired instance {instance.uuid}"
                        )
                        if self.stop_instance(
                            instance, user_id=None, reason="expired"
                        ):
                            cleaned += 1
                        else:
                            failed += 1
                except TimeoutError:
                    logger.error(f"Timeout cleaning up instance {instance.uuid}")
                    # Mark as error so it gets cleaned up later
                    instance.status = "error"
                    db.session.commit()
                    failed += 1
                except Exception as e:
                    logger.error(f"Error cleaning up instance {instance.uuid}: {e}")
                    failed += 1

            logger.info(f"Cleanup completed: {cleaned} cleaned, {failed} failed")

        finally:
            self._cleanup_running = False

    def cleanup_old_instances(self):
        """
        Background job: Delete old stopped/error instances
        """
        instances = ContainerInstance.query.filter(
            ContainerInstance.status.in_(["stopped", "error"])
        ).all()

        for instance in instances:
            if instance.should_cleanup():
                if self._is_within_creation_rate_limit_window(instance):
                    logger.info(
                        f"Keeping old instance {instance.uuid} for creation rate limit"
                    )
                    continue
                if instance.status == "error" and instance.container_id:
                    if not self.stop_instance(
                        instance, user_id=None, reason="cleanup_retry"
                    ):
                        logger.warning(
                            f"Keeping errored instance {instance.uuid}; Docker cleanup failed"
                        )
                        continue
                logger.info(f"Deleting old instance {instance.uuid}")
                try:
                    # Delete associated flags if invalidated
                    from ..models.flag import ContainerFlag

                    ContainerFlag.query.filter_by(
                        instance_id=instance.id, flag_status="invalidated"
                    ).delete()

                    db.session.delete(instance)
                    db.session.commit()
                except Exception as e:
                    logger.error(f"Error deleting instance: {e}")
                    db.session.rollback()

    def _is_within_creation_rate_limit_window(self, instance: ContainerInstance):
        from ..models.config import ContainerConfig

        try:
            cooldown_seconds = int(ContainerConfig.get("instance_rate_limit_seconds", 0))
        except (TypeError, ValueError):
            cooldown_seconds = 0

        if cooldown_seconds <= 0:
            return False

        reference_time = instance.stopped_at or instance.created_at
        if not reference_time:
            return False

        elapsed_seconds = int((datetime.utcnow() - reference_time).total_seconds())
        return elapsed_seconds < cooldown_seconds

    def _create_audit_log(self, event_type, **kwargs):
        """Create audit log entry"""
        log = ContainerAuditLog(
            event_type=event_type,
            ip_address=request.remote_addr if request else None,
            user_agent=request.headers.get("User-Agent") if request else None,
            **kwargs,
        )
        db.session.add(log)
        # Audit events are created after the lifecycle transaction in several
        # paths (start, renew, and stop), so persist the audit row here rather
        # than relying on a later unrelated commit.
        db.session.commit()
