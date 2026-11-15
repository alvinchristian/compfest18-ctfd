"""Platform-wide anti-cheat detection and evidence collection."""

import json
import logging
from datetime import timedelta

import requests
from flask import request

from CTFd.models import AnticheatAlerts, Solves, Tracking, db
from CTFd.utils import get_config
from CTFd.utils.user import get_ip

logger = logging.getLogger(__name__)

DEFAULT_SOLVE_WINDOW_MINUTES = 20


def is_enabled():
    """Anti-cheat is enabled unless explicitly disabled in configuration."""
    value = get_config("anticheat_enabled")
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "disabled")
    return bool(value)


def _json_value(value):
    if hasattr(value, "isoformat"):
        return value.isoformat() + "Z"
    return value


def _record(model):
    return {
        column.name: _json_value(getattr(model, column.name))
        for column in model.__table__.columns
    }


def _webhook_url():
    return get_config("anticheat_discord_webhook_url") or ""


def _solve_window_minutes():
    try:
        return max(1, int(get_config("anticheat_solve_window_minutes") or DEFAULT_SOLVE_WINDOW_MINUTES))
    except (TypeError, ValueError):
        return DEFAULT_SOLVE_WINDOW_MINUTES


def _send_webhook(alert):
    webhook_url = _webhook_url()
    if not webhook_url:
        return

    players = alert.evidence.get("players", []) if isinstance(alert.evidence, dict) else []
    player_field = []
    for player in players:
        player_field.append(
            "{} — {}".format(player.get("name", "Unknown"), player.get("team_name", "Unknown team"))
        )

    fields = [
        {"name": "Type", "value": alert.alert_type, "inline": True},
        {"name": "Alert ID", "value": str(alert.id), "inline": True},
        {"name": "IP", "value": alert.ip or "Unknown", "inline": True},
    ]
    if player_field:
        fields.append({"name": "Players / Teams", "value": "\n".join(player_field)[:1024], "inline": False})
    fields.append(
        {
            "name": "Evidence",
            "value": "```json\n{}\n```".format(
                json.dumps(alert.evidence, indent=2, default=str)[:3900]
            ),
            "inline": False,
        }
    )

    payload = {
        "username": "CTFd Anti-Cheat",
        "embeds": [
            {
                "title": "⚠️ Anti-Cheat Alert",
                "color": 15158332,
                "fields": fields,
            }
        ],
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        if response.status_code not in (200, 204):
            raise RuntimeError("Discord returned HTTP {}: {}".format(response.status_code, response.text[:500]))
        alert.notified_at = alert.created_at
    except Exception as exc:  # noqa: BLE001 - notification failure must not break solving
        alert.notification_error = str(exc)
        logger.exception("Failed to send anti-cheat webhook for alert %s", alert.id)
    db.session.commit()


def create_alert(alert_type, fingerprint, evidence, user_id=None, account_id=None, ip=None):
    if AnticheatAlerts.query.filter_by(fingerprint=fingerprint).first():
        return None

    alert = AnticheatAlerts(
        alert_type=alert_type,
        fingerprint=fingerprint,
        user_id=user_id,
        account_id=account_id,
        ip=ip,
        evidence=evidence,
    )
    db.session.add(alert)
    db.session.commit()
    _send_webhook(alert)
    return alert


def check_shared_ip(ip, user_id):
    if not is_enabled():
        return

    if not ip or not user_id:
        return

    from CTFd.models import Teams, Users

    current_user = Users.query.get(user_id)
    if not current_user or not current_user.team_id:
        return

    other_tracking = (
        Tracking.query.filter(Tracking.ip == ip, Tracking.user_id.isnot(None), Tracking.user_id != user_id)
        .order_by(Tracking.date.asc())
        .all()
    )
    if not other_tracking:
        return

    other_user_ids = {track.user_id for track in other_tracking}
    other_users = Users.query.filter(Users.id.in_(other_user_ids)).all()
    cross_team_users = [user for user in other_users if user.team_id and user.team_id != current_user.team_id]
    if not cross_team_users:
        return

    user_ids = sorted({user_id, *(user.id for user in cross_team_users)})
    team_ids = {current_user.team_id, *(user.team_id for user in cross_team_users)}
    teams = {team.id: team for team in Teams.query.filter(Teams.id.in_(team_ids)).all()}
    players = [current_user, *cross_team_users]
    fingerprint = "shared_ip:{}:{}".format(ip, ":".join(map(str, user_ids)))
    create_alert(
        "shared_ip",
        fingerprint,
        {
            "reason": "Multiple users were observed using the same IP address.",
            "ip": ip,
            "current_user_id": user_id,
            "players": [
                {
                    "user_id": player.id,
                    "name": player.name,
                    "team_id": player.team_id,
                    "team_name": teams.get(player.team_id).name if teams.get(player.team_id) else "Unknown team",
                }
                for player in players
            ],
            "matching_tracking_records": [
                _record(track) for track in other_tracking if track.user_id in {user.id for user in cross_team_users}
            ],
        },
        user_id=user_id,
        ip=ip,
    )


def check_rapid_solves(solve):
    if not is_enabled():
        return

    window = _solve_window_minutes()
    previous_solves = (
        Solves.query.filter(
            Solves.user_id == solve.user_id,
            Solves.id != solve.id,
            Solves.date >= solve.date - timedelta(minutes=window),
            Solves.date <= solve.date,
        )
        .order_by(Solves.date.asc())
        .all()
    )
    if not previous_solves:
        return

    solve_ids = sorted([solve.id, *(item.id for item in previous_solves)])
    fingerprint = "rapid_solves:{}".format(":".join(map(str, solve_ids)))
    create_alert(
        "rapid_solves",
        fingerprint,
        {
            "reason": "The user solved multiple challenges inside the configured time window.",
            "window_minutes": window,
            "user_id": solve.user_id,
            "solves": [_record(item) for item in [*previous_solves, solve]],
        },
        user_id=solve.user_id,
        account_id=solve.team_id,
        ip=solve.ip,
    )


def check_container_solve(solve, challenge):
    if not is_enabled():
        return

    if getattr(challenge, "type", None) != "container":
        return

    try:
        from CTFd.plugins.containers.models import ContainerAuditLog, ContainerInstance
    except ImportError:
        logger.warning("Container models unavailable while checking container solve %s", solve.id)
        return

    account_id = solve.team_id or solve.user_id
    instances = (
        ContainerInstance.query.filter_by(challenge_id=solve.challenge_id, account_id=account_id)
        .order_by(ContainerInstance.created_at.asc())
        .all()
    )
    started = [instance for instance in instances if instance.started_at is not None]
    if started:
        return

    audit_logs = (
        ContainerAuditLog.query.filter_by(challenge_id=solve.challenge_id, account_id=account_id)
        .order_by(ContainerAuditLog.timestamp.asc())
        .all()
    )
    fingerprint = "container_without_start:{}".format(solve.id)
    create_alert(
        "container_without_start",
        fingerprint,
        {
            "reason": "A container challenge was solved without a recorded started instance.",
            "solve": _record(solve),
            "challenge_id": solve.challenge_id,
            "account_id": account_id,
            "instances": [_record(instance) for instance in instances],
            "container_audit_logs": [_record(log) for log in audit_logs],
        },
        user_id=solve.user_id,
        account_id=account_id,
        ip=solve.ip,
    )


def inspect_solve(solve, challenge):
    check_rapid_solves(solve)
    check_container_solve(solve, challenge)


def inspect_request_ip(user_id):
    check_shared_ip(get_ip(), user_id)
