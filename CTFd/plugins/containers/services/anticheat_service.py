"""
Anti-Cheat Service - Flag validation and cheat detection
"""
import logging
from datetime import datetime

import requests
from flask import request

from CTFd.models import db
from CTFd.utils import get_config

from ..models.audit import ContainerAuditLog
from ..models.flag import ContainerFlag, ContainerFlagAttempt
from .flag_service import FlagService

logger = logging.getLogger(__name__)


class AntiCheatService:
    """
    Service to validate flags and detect cheating
    """

    DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1456970132848115877/yYjLwO4EInn8c8Y7ggGv01jWPpnA3hkFC8st9taIm-XyQA4YwsXklMbhCIEg5FmAX6nT"

    def __init__(self, flag_service: FlagService):
        self.flag_service = flag_service

    def send_discord_alert(
        self,
        cheater_account_id: int,
        cheater_user_id: int,
        owner_account_id: int,
        challenge_id: int,
        challenge_name: str = None,
        ip_address: str = None,
        additional_info: dict = None,
    ):
        """
        Send a Discord webhook alert for detected cheating

        Args:
            cheater_account_id: Account ID of the cheater
            cheater_user_id: User ID of the cheater
            owner_account_id: Account ID of the flag owner
            challenge_id: Challenge ID
            challenge_name: Challenge name (optional)
            ip_address: IP address of the cheater
            additional_info: Additional details to include
        """
        # Get webhook URL from config or class attribute
        webhook_url = get_config("discord_webhook_url") or self.DISCORD_WEBHOOK_URL

        if not webhook_url:
            logger.warning("Discord webhook URL not configured, skipping alert")
            return False

        try:
            # Get user/team names for better readability
            from CTFd.models import Teams, Users

            mode = get_config("user_mode")
            is_team_mode = mode == "teams"

            cheater_user = Users.query.get(cheater_user_id)
            cheater_user_name = (
                cheater_user.name if cheater_user else f"User #{cheater_user_id}"
            )

            if is_team_mode:
                cheater_team = Teams.query.get(cheater_account_id)
                owner_team = Teams.query.get(owner_account_id)
                cheater_name = (
                    cheater_team.name if cheater_team else f"Team #{cheater_account_id}"
                )
                owner_name = (
                    owner_team.name if owner_team else f"Team #{owner_account_id}"
                )
                account_type = "Team"
            else:
                cheater = Users.query.get(cheater_account_id)
                owner = Users.query.get(owner_account_id)
                cheater_name = (
                    cheater.name if cheater else f"User #{cheater_account_id}"
                )
                owner_name = owner.name if owner else f"User #{owner_account_id}"
                account_type = "User"

            # Get challenge name if not provided
            if not challenge_name:
                from ..models.challenge import ContainerChallenge

                challenge = ContainerChallenge.query.get(challenge_id)
                challenge_name = (
                    challenge.name if challenge else f"Challenge #{challenge_id}"
                )

            # Build Discord embed
            embed = {
                "title": "⚠️ FLAG SHARING ALERT",
                "color": 15158332,
                "timestamp": datetime.utcnow().isoformat(),
                "fields": [
                    {
                        "name": "Challenge",
                        "value": f"`{challenge_name}`",
                        "inline": False,
                    },
                    {
                        "name": f"Cheater {account_type}",
                        "value": f"`{cheater_name}`",
                        "inline": True,
                    },
                    {
                        "name": "Submitted By",
                        "value": f"`{cheater_user_name}`",
                        "inline": True,
                    },
                    {
                        "name": f"Flag Owner {account_type}",
                        "value": f"`{owner_name}`",
                        "inline": True,
                    },
                    {
                        "name": "🌐 IP Address",
                        "value": f"`{ip_address or 'Unknown'}`",
                        "inline": True,
                    },
                    {
                        "name": "Detected At",
                        "value": f"`{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}`",
                        "inline": True,
                    },
                ],
            }

            # Add additional info if provided
            if additional_info:
                embed["fields"].append(
                    {
                        "name": "📝 Additional Info",
                        "value": f"```json\n{additional_info}\n```",
                        "inline": False,
                    }
                )

            payload = {
                "username": "Bobol Anti-Cheat",
                "avatar_url": "",
                "embeds": [embed],
            }

            response = requests.post(webhook_url, json=payload, timeout=10)

            if response.status_code in [200, 204]:
                logger.info(
                    f"Discord alert sent for flag sharing: {cheater_account_id} -> {owner_account_id}"
                )
                return True
            else:
                logger.error(
                    f"Discord webhook failed with status {response.status_code}: {response.text}"
                )
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send Discord webhook: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending Discord alert: {e}")
            return False

    def validate_flag(
        self, challenge_id: int, account_id: int, user_id: int, submitted_flag: str
    ) -> tuple:
        """
        Validate submitted flag

        Args:
            challenge_id: Challenge ID
            account_id: Team/User ID
            user_id: Actual user submitting
            submitted_flag: Submitted flag text

        Returns:
            (is_correct: bool, message: str, is_cheating: bool)
        """
        # Get challenge to check flag mode
        from ..models.challenge import ContainerChallenge

        challenge = ContainerChallenge.query.get(challenge_id)
        if not challenge:
            return (False, "Challenge not found", False)

        # Hash submitted flag
        flag_hash = FlagService.hash_flag(submitted_flag)
        ip_address = request.remote_addr if request else None
        user_agent = request.headers.get("User-Agent") if request else None

        # Create attempt log (always)
        attempt = ContainerFlagAttempt(
            challenge_id=challenge_id,
            account_id=account_id,
            user_id=user_id,
            submitted_flag_hash=flag_hash,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        # STATIC FLAG: Compare directly with challenge flag
        if challenge.flag_mode == "static":
            static_flag = f"{challenge.flag_prefix}{challenge.flag_suffix}"

            if submitted_flag == static_flag:
                # Correct static flag
                attempt.is_correct = True
                attempt.is_cheating = False

                # Audit log
                audit_log = ContainerAuditLog(
                    event_type="flag_submitted_correct",
                    challenge_id=challenge_id,
                    account_id=account_id,
                    user_id=user_id,
                    details={"flag_mode": "static", "ip_address": ip_address},
                    severity="info",
                    ip_address=ip_address,
                    user_agent=user_agent,
                )

                db.session.add(attempt)
                db.session.add(audit_log)
                db.session.commit()

                logger.info(
                    f"Account {account_id} correctly submitted static flag for challenge {challenge_id}"
                )
                return (True, "Correct", False)
            else:
                # Wrong static flag
                attempt.is_correct = False
                attempt.is_cheating = False
                db.session.add(attempt)
                db.session.commit()

                logger.info(
                    f"Account {account_id} submitted incorrect static flag for challenge {challenge_id}"
                )
                return (False, "Incorrect", False)

        # RANDOM FLAG: Check in database
        flag_record = ContainerFlag.query.filter_by(flag_hash=flag_hash).first()

        # Flag does not exist → wrong
        if not flag_record:
            attempt.is_correct = False
            attempt.is_cheating = False
            db.session.add(attempt)
            db.session.commit()

            logger.info(
                f"Account {account_id} submitted non-existent flag for challenge {challenge_id}"
            )
            return (False, "Incorrect", False)

        # 5. Flag invalidated → expired
        if flag_record.flag_status == "invalidated":
            attempt.is_correct = False
            attempt.is_cheating = False
            db.session.add(attempt)
            db.session.commit()

            logger.info(
                f"Account {account_id} submitted invalidated flag for challenge {challenge_id}"
            )
            return (False, "This flag has expired", False)

        # 6. Flag belongs to different account → CHEATING
        if flag_record.account_id != account_id:
            attempt.is_correct = True
            attempt.is_cheating = True
            attempt.flag_owner_account_id = flag_record.account_id

            # Audit log with critical severity
            audit_log = ContainerAuditLog(
                event_type="flag_reuse_detected",
                challenge_id=challenge_id,
                account_id=account_id,
                user_id=user_id,
                details={
                    "submitted_flag_hash": flag_hash,
                    "actual_owner_account_id": flag_record.account_id,
                    "flag_status": flag_record.flag_status,
                    "ip_address": ip_address,
                    "action_taken": "logged_only",
                },
                severity="warning",
                ip_address=ip_address,
                user_agent=user_agent,
            )

            db.session.add(attempt)
            db.session.add(audit_log)
            db.session.commit()

            logger.warning(
                f"FLAG SHARING DETECTED: Account {account_id} submitted flag belonging to {flag_record.account_id} "
                f"for challenge {challenge_id} - LOGGED FOR REVIEW"
            )

            self.send_discord_alert(
                cheater_account_id=account_id,
                cheater_user_id=user_id,
                owner_account_id=flag_record.account_id,
                challenge_id=challenge_id,
                challenge_name=challenge.name,
                ip_address=ip_address,
            )

            # Don't reveal to user that flag reuse was detected
            return (True, "Correct!", True)

        # 7. Flag is correct and belongs to this account
        if flag_record.account_id == account_id:
            # 7.1. Already submitted (duplicate)
            if flag_record.flag_status == "submitted_correct":
                attempt.is_correct = True
                attempt.is_cheating = False
                db.session.add(attempt)
                db.session.commit()

                logger.info(
                    f"Account {account_id} re-submitted already solved challenge {challenge_id}"
                )
                return (True, "Already solved", False)

            # 7.2. First correct submission
            flag_record.mark_as_submitted(user_id, ip_address)

            # Note: Do NOT update instance status here
            # The ContainerService.stop_instance() will handle it when called after validation
            # This allows __init__.py to find instance with status='running' to stop it

            attempt.is_correct = True
            attempt.is_cheating = False

            # Audit log
            audit_log = ContainerAuditLog(
                event_type="flag_submitted_correct",
                instance_id=flag_record.instance_id,
                challenge_id=challenge_id,
                account_id=account_id,
                user_id=user_id,
                details={"ip_address": ip_address},
                severity="info",
                ip_address=ip_address,
                user_agent=user_agent,
            )

            db.session.add(attempt)
            db.session.add(audit_log)
            db.session.commit()

            logger.info(
                f"Account {account_id} correctly solved challenge {challenge_id}"
            )

            return (True, "Correct!", False)

        # 8. Fallback
        return (False, "Unexpected error", False)

    def get_cheat_attempts(self, limit=100):
        """
        Get recent cheat attempts

        Returns:
            List of ContainerFlagAttempt records where is_cheating=True
        """
        return (
            ContainerFlagAttempt.query.filter_by(is_cheating=True)
            .order_by(ContainerFlagAttempt.timestamp.desc())
            .limit(limit)
            .all()
        )

    def get_account_attempts(self, account_id, challenge_id=None):
        """
        Get flag attempts for an account

        Args:
            account_id: Account ID
            challenge_id: Optional challenge filter

        Returns:
            List of attempts
        """
        query = ContainerFlagAttempt.query.filter_by(account_id=account_id)

        if challenge_id:
            query = query.filter_by(challenge_id=challenge_id)

        return query.order_by(ContainerFlagAttempt.timestamp.desc()).all()
