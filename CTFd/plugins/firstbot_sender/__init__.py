import requests
from flask import Blueprint, render_template, request

from CTFd.api.v1.challenges import ChallengeAttempt
from CTFd.models import Challenges, Solves
from CTFd.utils import config, get_config, set_config
from CTFd.utils.decorators import (
    admins_only,
    during_ctf_time_only,
    require_verified_emails,
)
from CTFd.utils.decorators.visibility import check_challenge_visibility
from CTFd.utils.user import get_current_team, get_current_user


class FirstbotException(Exception):
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message


class FirstbotChallAttempt(ChallengeAttempt):
    @check_challenge_visibility
    @during_ctf_time_only
    @require_verified_emails
    def post(self):
        res = super().post()
        res_data = res.get("data")
        if res_data and res_data.get("status") == "correct":

            if request.content_type != "application/json":
                request_data = request.form
            else:
                request_data = request.get_json()

            challenge_id = request_data.get("challenge_id")
            challenge = Challenges.query.filter_by(id=challenge_id).first_or_404()

            firstsolves = (
                Solves.query.filter_by(challenge_id=challenge_id)
                .order_by(Solves.id)
                .first()
            )
            firstsolves_id = None
            if firstsolves is not None:
                if config.is_teams_mode():
                    firstsolves_id = firstsolves.team_id
                else:
                    firstsolves_id = firstsolves.user_id

            solver = ""
            solver_id = None
            if config.is_teams_mode():
                team = get_current_team()
                solver = team.name
                solver_id = team.id
            else:
                user = get_current_user()
                solver = user.name
                solver_id = user.id

            if config.is_scoreboard_frozen():
                solver = ""

            if firstsolves_id and firstsolves_id == solver_id:
                self.send_firstblood_data(challenge.name, solver)
        return res

    def send_firstblood_data(self, chall, solver=""):
        url = get_config("firstbot_endpoint")
        secret = get_config("firstbot_secret")

        if url is None:
            return
        data = {
            "status": "ok",
            "data": {"chall": chall, "solver": solver, "secret": secret},
        }
        try:
            requests.post(url, json=data, timeout=1)
        except Exception as e:
            print(e)
            return


def load(app):
    firstbot = Blueprint("firstbot_sender", __name__, template_folder="templates")

    @firstbot.route("/admin/firstbot-sender", methods=["GET", "POST"])
    @admins_only
    def view_firstbot():
        if request.method == "POST":
            data = request.form or request.json

            endpoint = data.get("firstbot_endpoint")
            secret = data.get("firstbot_secret")
            if endpoint == "None" and secret == "None":
                set_config("firstbot_endpoint", None)
                set_config("firstbot_secret", None)
            elif endpoint and secret:
                set_config("firstbot_endpoint", endpoint)
                set_config("firstbot_secret", secret)
        context = {
            "firstbot_endpoint": get_config("firstbot_endpoint"),
            "firstbot_secret": get_config("firstbot_secret"),
        }
        return render_template("firstbot.html", **context)

    app.register_blueprint(firstbot)
    app.view_functions["api.challenges_challenge_attempt"] = FirstbotChallAttempt().post
