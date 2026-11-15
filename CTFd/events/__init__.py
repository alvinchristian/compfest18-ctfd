from flask import Blueprint, Response, current_app, stream_with_context

from CTFd.models import db
from CTFd.utils import get_app_config
from CTFd.utils.decorators import authed_only, ratelimit
from CTFd.utils.user import get_current_team, get_current_user, is_admin

events = Blueprint("events", __name__)


@events.route("/events")
@authed_only
@ratelimit(method="GET", limit=150, interval=60)
def subscribe():
    @stream_with_context
    def gen():
        for event in current_app.events_manager.subscribe():
            if event.type == "notification" and isinstance(event.data, dict):
                user = get_current_user()
                if not is_admin() and user:
                    team = get_current_team()
                    target_user = event.data.get("user_id")
                    target_team = event.data.get("team_id")
                    if target_user and target_user != user.id and not (
                        team and target_team == team.id
                    ):
                        continue
                    if target_team and not (team and target_team == team.id):
                        continue
            yield str(event)

    enabled = get_app_config("SERVER_SENT_EVENTS")
    if enabled is False:
        return ("", 204)

    # Close the db session to avoid OperationalError with MySQL connection errors
    db.session.close()

    return Response(gen(), mimetype="text/event-stream")
