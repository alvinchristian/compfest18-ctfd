"""Admin-only category wheel for 1v1 challenge selection."""

import json

from flask import Blueprint, Response, abort, current_app, jsonify, redirect, render_template, request, stream_with_context, url_for
from sqlalchemy import inspect, text

from CTFd.cache import clear_challenges
from CTFd.models import Challenges, Teams, db
from CTFd.utils import get_config, set_config
from CTFd.utils.decorators import admins_only
from CTFd.utils.user import get_current_team, is_admin

from .bracket_generator import generate_bracket
from .draft import (
    process_expired_draft,
    serialize_draft,
    spin_tiebreaker,
    submit_ban,
)
from .models import OneVOneBracket, OneVOneBracketGame, OneVOneBracketMatch, OneVOneBracketStage
from .scoring import (
    active_match,
    adjust_active_score,
    next_eligible_match,
    prepare_next_game,
    refresh_active_game,
    set_plugin_enabled,
    match_duration_minutes,
    set_match_duration_minutes,
    valid_best_of,
)

CONFIG_KEY = "onevone_wheel_categories"
TIEBREAKER_CONFIG_KEY = "onevone_tiebreaker_categories"
GRAND_FINAL_CONFIG_KEY = "onevone_grand_final_categories"
ENABLED_CONFIG_KEY = "onevone_enabled"


def _is_enabled():
    # Preserve the already-deployed behavior until an admin explicitly turns
    # the plugin off.
    value = get_config(ENABLED_CONFIG_KEY, default=True)
    return value not in (False, 0, "false", "False", "0")


def _publish_live_update():
    try:
        match = active_match()
        current_app.events_manager.publish(
            data={"match_id": match.id if match else None},
            type="onevone_draft_update",
        )
    except Exception:
        current_app.logger.exception("Unable to publish 1v1 live update")


def _challenge_categories(mode="onevone"):
    """Return non-empty categories from one configured challenge set."""
    categories = (
        Challenges.query.with_entities(Challenges.category)
        .filter(
            Challenges.competition_mode == mode,
            Challenges.category.isnot(None),
            Challenges.category != "",
        )
        .distinct()
        .order_by(Challenges.category.asc())
        .all()
    )
    return [category for category, in categories]


def _tiebreaker_categories():
    return _challenge_categories("onevone_tiebreaker")


def _configured_categories(available_categories):
    return _configured_categories_for_key(available_categories, CONFIG_KEY)


def _configured_categories_for_key(available_categories, key):
    """Read the stored wheel contents, ignoring categories no longer in CTFd."""
    stored = get_config(key, default="[]")
    try:
        categories = json.loads(stored) if isinstance(stored, str) else stored
    except (TypeError, ValueError):
        categories = []

    if not isinstance(categories, list):
        return []

    available = set(available_categories)
    return [category for category in categories if category in available]


def _current_bracket():
    return OneVOneBracket.query.order_by(OneVOneBracket.id.desc()).first()


def _slot_label(match, slot, matches_by_id=None):
    """Render a match participant or its future result source."""
    entry = getattr(match, f"team_{slot}_entry")
    if entry:
        return entry.team.name

    source_match_id = getattr(match, f"team_{slot}_source_match_id")
    if source_match_id:
        source = "Winner" if getattr(match, f"team_{slot}_source_result") == "winner" else "Loser"
        source_match = (matches_by_id or {}).get(source_match_id)
        source_number = source_match.sequence if source_match else source_match_id
        return f"{source} of Match {source_number}"
    return "To be determined"


def _bracket_stages(bracket):
    if bracket is None:
        return []

    matches_by_id = {match.id: match for match in bracket.matches}
    return [
        {
            "stage": stage,
            "matches": [
                {
                    "match": match,
                    "team_one": _slot_label(match, "one", matches_by_id),
                    "team_two": _slot_label(match, "two", matches_by_id),
                }
                for match in stage.matches
            ],
        }
        for stage in bracket.stages
    ]


def _ensure_scoring_schema(app):
    """Add scoring fields for brackets created before scoring was introduced."""
    required_columns = {
        "onevone_bracket_stages": {
            "best_of": "INTEGER NOT NULL DEFAULT 1",
        },
        "onevone_bracket_matches": {
            "best_of": "INTEGER",
            "team_one_score": "INTEGER NOT NULL DEFAULT 0",
            "team_two_score": "INTEGER NOT NULL DEFAULT 0",
            "game_ready": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "onevone_bracket_games": {
            "is_tiebreaker": "BOOLEAN NOT NULL DEFAULT 0",
            "started_at": "DATETIME",
            "deadline": "DATETIME",
            "paused": "BOOLEAN NOT NULL DEFAULT 0",
            "paused_remaining_seconds": "INTEGER",
        },
        "challenges": {
            "team_visibility": "TEXT",
        },
    }
    inspector = inspect(app.db.engine)
    with app.db.engine.begin() as connection:
        for table, columns in required_columns.items():
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))


def load(app):
    # This plugin owns its tables. Importing the models above registers them
    # with SQLAlchemy before CTFd creates any plugin tables.
    app.db.create_all()
    _ensure_scoring_schema(app)
    onevone = Blueprint("onevone", __name__)

    @onevone.route("/admin/1v1", methods=["GET", "POST"])
    @admins_only
    def configuration():
        available_categories = _challenge_categories()
        grand_final_available = _challenge_categories("onevone_grand_final")
        saved = False
        duration_error = None

        if request.method == "POST":
            if request.form.get("action") == "toggle":
                set_plugin_enabled(request.form.get("enabled") == "true")
                clear_challenges()
                configured_categories = _configured_categories(available_categories)
            elif request.form.get("action") == "tiebreaker_categories":
                tie_available = _tiebreaker_categories()
                tie_selected = list(dict.fromkeys(
                    category for category in request.form.getlist("tiebreaker_categories")
                    if category in set(tie_available)
                ))
                set_config(TIEBREAKER_CONFIG_KEY, json.dumps(tie_selected))
                configured_categories = _configured_categories(available_categories)
            elif request.form.get("action") == "match_duration":
                duration_error = set_match_duration_minutes(request.form.get("minutes"))
                configured_categories = _configured_categories(available_categories)
            elif request.form.get("action") == "grand_final_categories":
                selected = request.form.getlist("grand_final_categories")
                available = set(grand_final_available)
                grand_final_configured = list(dict.fromkeys(
                    category for category in selected if category in available
                ))
                set_config(GRAND_FINAL_CONFIG_KEY, json.dumps(grand_final_configured))
                configured_categories = _configured_categories(available_categories)
            else:
                selected = request.form.getlist("categories")
                available = set(available_categories)
                # Preserve order from the admin's selection and reject stale/tampered values.
                configured_categories = list(
                    dict.fromkeys(category for category in selected if category in available)
                )
                set_config(CONFIG_KEY, json.dumps(configured_categories))
                # Normal wheel settings are independent of the tiebreaker set.
            saved = True
        else:
            configured_categories = _configured_categories(available_categories)

        tiebreaker_available = _tiebreaker_categories()
        tiebreaker_configured = _configured_categories_for_key(
            tiebreaker_available, TIEBREAKER_CONFIG_KEY
        )
        grand_final_configured = _configured_categories_for_key(
            grand_final_available, GRAND_FINAL_CONFIG_KEY
        )
        admin_match = active_match()
        admin_draft = process_expired_draft(admin_match)
        refresh_active_game(admin_match)
        admin_game = (
            next((game for game in admin_match.games if game.status == "active"), None)
            if admin_match else None
        )

        return render_template(
            "plugins/1v1/templates/configuration.html",
            available_categories=available_categories,
            configured_categories=configured_categories,
            saved=saved,
            enabled=_is_enabled(),
            tiebreaker_available=tiebreaker_available,
            tiebreaker_configured=tiebreaker_configured,
            grand_final_available=grand_final_available,
            grand_final_configured=grand_final_configured,
            admin_match=admin_match,
            admin_draft=admin_draft,
            admin_game=admin_game,
            match_duration_minutes=match_duration_minutes(),
            duration_error=locals().get("duration_error"),
        )

    @onevone.route("/admin/1v1/bracket", methods=["GET", "POST"])
    @admins_only
    def bracket_configuration():
        teams = (
            Teams.query.filter_by(hidden=False, banned=False)
            .order_by(Teams.name.asc(), Teams.id.asc())
            .all()
        )
        error = None
        generated = False
        requested_team_ids = None
        requested_winner_count = None

        if request.method == "POST":
            requested_ids = request.form.getlist("team_ids")
            team_ids = []
            for raw_id in requested_ids:
                try:
                    team_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if team_id not in team_ids:
                    team_ids.append(team_id)
            requested_team_ids = team_ids

            valid_team_ids = {team.id for team in teams}
            if len(team_ids) < 2:
                error = "Select at least two teams to generate a bracket."
            elif any(team_id not in valid_team_ids for team_id in team_ids):
                error = "One or more selected teams are no longer eligible."
            else:
                try:
                    winner_count = int(request.form.get("winner_count", ""))
                except (TypeError, ValueError):
                    winner_count = 0
                requested_winner_count = winner_count

                if not 1 <= winner_count <= len(team_ids):
                    error = "The number of winners must be between 1 and the number of teams."
                else:
                    generate_bracket(team_ids, winner_count)
                    generated = True

        bracket = _current_bracket()
        selected_team_ids = (
            requested_team_ids
            if requested_team_ids is not None and error
            else [entry.team_id for entry in bracket.entries]
            if bracket
            else []
        )
        current_match = active_match()
        current_draft = process_expired_draft(current_match)
        refresh_active_game(current_match)
        current_game = (
            next(
                (game for game in current_match.games if game.status == "active"),
                None,
            )
            if current_match
            else None
        )
        return render_template(
            "plugins/1v1/templates/bracket_configuration.html",
            teams=teams,
            bracket=bracket,
            selected_team_ids=selected_team_ids,
            generated=generated,
            error=error,
            winner_count=requested_winner_count
            if requested_winner_count is not None and error
            else bracket.winner_count
            if bracket
            else 1,
            stage_views=_bracket_stages(bracket),
            active_match=current_match,
            active_draft=current_draft,
            active_game=current_game,
            match_duration_minutes=match_duration_minutes(),
            notice=request.args.get("notice"),
        )

    @onevone.route("/admin/1v1/bracket/next-match", methods=["POST"])
    @admins_only
    def next_match():
        match, error = next_eligible_match()
        if not error:
            _publish_live_update()
        notice = error or f"Match {match.sequence} is active and the category draft has started."
        return redirect(url_for("onevone.bracket_configuration", notice=notice))

    @onevone.route("/admin/1v1/bracket/next-game", methods=["POST"])
    @admins_only
    def next_game():
        error = prepare_next_game(active_match())
        if not error:
            _publish_live_update()
        notice = error or "The next game's category draft has started."
        return redirect(url_for("onevone.bracket_configuration", notice=notice))

    @onevone.route("/admin/1v1/bracket/start-game", methods=["POST"])
    @admins_only
    def start_game():
        from .scoring import start_active_game
        error = start_active_game(active_match())
        if not error:
            _publish_live_update()
        notice = error or "The 20-minute game timer has started."
        return redirect(url_for("onevone.bracket_configuration", notice=notice))

    @onevone.route("/admin/1v1/bracket/game-control", methods=["POST"])
    @admins_only
    def game_control():
        from .scoring import add_game_time, end_active_game, pause_active_game, resume_active_game
        action = request.form.get("game_action")
        if action == "pause":
            error = pause_active_game(active_match())
        elif action == "resume":
            error = resume_active_game(active_match())
        elif action == "add_time":
            error = add_game_time(active_match(), request.form.get("minutes"))
        elif action == "end":
            error = end_active_game(active_match())
        else:
            error = "Choose a valid game action."
        clear_challenges()
        if not error:
            _publish_live_update()
        notice = error or "Game control updated."
        return redirect(url_for("onevone.bracket_configuration", notice=notice))

    @onevone.route("/admin/1v1/bracket/score", methods=["POST"])
    @admins_only
    def score_adjustment():
        _, error = adjust_active_score(
            request.form.get("team_slot"), request.form.get("delta")
        )
        clear_challenges()
        if not error:
            _publish_live_update()
        notice = error or "Match score updated."
        return redirect(url_for("onevone.bracket_configuration", notice=notice))

    @onevone.route("/admin/1v1/bracket/stages/<int:stage_id>/best-of", methods=["POST"])
    @admins_only
    def stage_best_of(stage_id):
        stage = OneVOneBracketStage.query.filter_by(id=stage_id).first_or_404()
        best_of = valid_best_of(request.form.get("best_of"))
        if best_of is None:
            notice = "Best-of must be a positive odd number, such as 1, 3, or 5."
        else:
            stage.best_of = best_of
            db.session.commit()
            notice = f"{stage.title} is now best-of-{best_of}."
        return redirect(url_for("onevone.bracket_configuration", notice=notice))

    @onevone.route("/admin/1v1/bracket/matches/<int:match_id>/best-of", methods=["POST"])
    @admins_only
    def match_best_of(match_id):
        match = OneVOneBracketMatch.query.filter_by(id=match_id).first_or_404()
        value = request.form.get("best_of", "").strip()
        if not value:
            match.best_of = None
            db.session.commit()
            notice = f"Match {match.sequence} now uses its stage default."
        else:
            best_of = valid_best_of(value)
            if best_of is None:
                notice = "Best-of must be a positive odd number, such as 1, 3, or 5."
            else:
                match.best_of = best_of
                db.session.commit()
                notice = f"Match {match.sequence} is now best-of-{best_of}."
        return redirect(url_for("onevone.bracket_configuration", notice=notice))

    @onevone.route("/1v1/bracket")
    def public_bracket():
        if not _is_enabled():
            abort(404)
        bracket = _current_bracket()
        return render_template(
            "plugins/1v1/templates/public_bracket.html",
            bracket=bracket,
            stages=_bracket_stages(bracket),
        )

    @onevone.route("/1v1/draft")
    def draft_pick():
        if not _is_enabled():
            abort(404)
        return render_template("plugins/1v1/templates/draft_pick.html")

    @onevone.route("/api/v1/1v1/draft")
    def draft_state():
        if not _is_enabled():
            return jsonify(success=True, data={"status": "disabled"})
        team = get_current_team()
        return jsonify(
            success=True,
            data=serialize_draft(active_match(), team.id if team else None, is_admin_user=is_admin()),
        )

    @onevone.route("/api/v1/1v1/events")
    def live_events():
        """Public SSE stream limited to safe 1v1 state-change notifications."""
        @stream_with_context
        def generate():
            for event in current_app.events_manager.subscribe():
                if event.type in (None, "ping", "onevone_draft_update"):
                    yield str(event)

        response = Response(generate(), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @onevone.route("/api/v1/1v1/draft/ban", methods=["POST"])
    @admins_only
    def draft_ban():
        if not _is_enabled():
            return jsonify(success=False, message="The 1v1 plugin is disabled."), 403

        data = request.get_json(silent=True) or request.form
        category = str(data.get("category", "")).strip()
        _, error = submit_ban(active_match(), None, category, admin=True)
        if error:
            return jsonify(success=False, message=error), 409
        _publish_live_update()
        return jsonify(
            success=True,
            data=serialize_draft(active_match(), is_admin_user=True),
        )

    @onevone.route("/api/v1/1v1/tiebreaker/spin", methods=["POST"])
    @admins_only
    def tiebreaker_spin():
        if not _is_enabled():
            return jsonify(success=False, message="The 1v1 plugin is disabled."), 403
        _, error = spin_tiebreaker(active_match())
        if error:
            return jsonify(success=False, message=error), 409
        _publish_live_update()
        return jsonify(
            success=True,
            data=serialize_draft(active_match(), is_admin_user=True),
        )

    @onevone.route("/api/v1/1v1/admin/first-blood")
    @admins_only
    def first_blood_state():
        game = (
            OneVOneBracketGame.query.filter_by(status="awarded")
            .order_by(OneVOneBracketGame.id.desc())
            .first()
        )
        return jsonify(
            success=True,
            data={
                "id": game.id if game else None,
                "team": game.winner_entry.team.name if game and game.winner_entry else None,
                "challenge": game.challenge.name if game and game.challenge else None,
            },
        )

    @onevone.route("/1v1/wheel")
    @admins_only
    def wheel():
        return redirect(url_for("onevone.draft_pick"))

    @onevone.route("/admin/1v1/wheel")
    @admins_only
    def legacy_wheel():
        return redirect(url_for("onevone.draft_pick"))

    @onevone.route("/admin/1v1/spin", methods=["POST"])
    @admins_only
    def spin():
        return (
            jsonify(
                success=False,
                message="The wheel is spun automatically on the Draft Pick page.",
            ),
            410,
        )

    app.register_blueprint(onevone)
