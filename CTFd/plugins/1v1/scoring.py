"""Series scoring and participant access rules for the 1v1 bracket."""

import datetime

from flask import current_app
from sqlalchemy import or_

from CTFd.models import Challenges, Solves, db
from CTFd.utils import get_config, set_config
from CTFd.utils.user import get_ip

from .draft import draft_active, start_draft, start_tiebreaker


MATCH_DURATION_CONFIG_KEY = "onevone_match_duration_minutes"
DEFAULT_MATCH_DURATION_MINUTES = 20


def match_duration_minutes():
    value = get_config(MATCH_DURATION_CONFIG_KEY, default=DEFAULT_MATCH_DURATION_MINUTES)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MATCH_DURATION_MINUTES
    return value if 1 <= value <= 180 else DEFAULT_MATCH_DURATION_MINUTES


def set_match_duration_minutes(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "Match duration must be a whole number of minutes."
    if not 1 <= value <= 180:
        return "Match duration must be between 1 and 180 minutes."
    set_config(MATCH_DURATION_CONFIG_KEY, value)
    return None
from .models import OneVOneBracket, OneVOneBracketGame, OneVOneBracketMatch


def plugin_enabled():
    value = get_config("onevone_enabled", default=True)
    # Configs are stored as strings in some database backends, where a saved
    # boolean can round-trip as integer 0/1 rather than Python False/True.
    return value not in (False, 0, "false", "False", "0")


def set_plugin_enabled(enabled):
    """Pause active games safely and restore them when the plugin resumes."""
    enabled = bool(enabled)
    for game in OneVOneBracketGame.query.filter_by(status="active").all():
        if game.challenge:
            game.challenge.state = "visible" if enabled else "hidden"
    db.session.commit()
    set_config("onevone_enabled", enabled)


def best_of_wins(best_of):
    return best_of // 2 + 1


def valid_best_of(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 1 and value % 2 else None


def active_match():
    return (
        OneVOneBracketMatch.query.filter_by(status="active")
        .order_by(OneVOneBracketMatch.id.desc())
        .first()
    )


def _entry_for_team(match, team_id):
    if match.team_one_entry and match.team_one_entry.team_id == team_id:
        return match.team_one_entry
    if match.team_two_entry and match.team_two_entry.team_id == team_id:
        return match.team_two_entry
    return None


def _propagate_result(match):
    """Fill downstream participant slots once a match result is known."""
    targets = OneVOneBracketMatch.query.filter(
        or_(
            OneVOneBracketMatch.team_one_source_match_id == match.id,
            OneVOneBracketMatch.team_two_source_match_id == match.id,
        )
    ).all()
    for target in targets:
        for slot in ("one", "two"):
            if getattr(target, f"team_{slot}_source_match_id") != match.id:
                continue
            result = getattr(target, f"team_{slot}_source_result")
            entry_id = (
                match.winner_entry_id
                if result == "winner"
                else match.loser_entry_id
            )
            setattr(target, f"team_{slot}_entry_id", entry_id)


def _finish_match(match):
    """Resolve a completed series and make its result available downstream."""
    if match.team_one_score > match.team_two_score:
        match.winner_entry_id = match.team_one_entry_id
        match.loser_entry_id = match.team_two_entry_id
    else:
        match.winner_entry_id = match.team_two_entry_id
        match.loser_entry_id = match.team_one_entry_id
    match.status = "completed"
    match.game_ready = False
    _propagate_result(match)


def match_is_ready(match):
    return bool(match.team_one_entry_id and match.team_two_entry_id)


def next_eligible_match():
    """Activate the first unfinished match whose stage is now eligible."""
    if not plugin_enabled():
        return None, "The 1v1 plugin is disabled."
    if active_match():
        return None, "Finish the active match before starting another one."

    bracket = OneVOneBracket.query.order_by(OneVOneBracket.id.desc()).first()
    if bracket is None:
        return None, "Generate a bracket before starting a match."

    for stage in bracket.stages:
        matches = list(stage.matches)
        if all(match.status == "completed" for match in matches):
            continue
        if any(
            any(previous.status != "completed" for previous in earlier.matches)
            for earlier in bracket.stages
            if earlier.sequence < stage.sequence
        ):
            return None, "Complete every match in the previous stage first."
        for match in matches:
            if match.status == "pending" and match_is_ready(match):
                match.status = "active"
                match.game_ready = False
                _, error = start_draft(match)
                if error:
                    db.session.rollback()
                    return None, error
                db.session.commit()
                return match, None
        return None, "This stage has no ready match yet."

    return None, "All bracket matches are complete."


def prepare_next_game(match):
    if not plugin_enabled():
        return "The 1v1 plugin is disabled."
    if match is None or match.status != "active":
        return "There is no active match."
    # Finalize an expired timed game before deciding whether another normal
    # game may be started. This also creates the tiebreaker for a tied BO1.
    refresh_active_game(match)
    if draft_active(match):
        return "The next game's category draft is already active."
    if OneVOneBracketGame.query.filter_by(match_id=match.id, status="active").first():
        return "Wait for the current game to be solved or scored first."
    if max(match.team_one_score, match.team_two_score) >= best_of_wins(match.effective_best_of):
        return "This match is already decided."
    normal_games = OneVOneBracketGame.query.filter_by(
        match_id=match.id, is_tiebreaker=False
    ).count()
    if normal_games >= match.effective_best_of and match.team_one_score == match.team_two_score:
        _, error = _start_tiebreaker_if_series_tied(match)
        if error:
            db.session.rollback()
            return error
        db.session.commit()
        return "The series ended tied. The tiebreaker wheel is ready."
    _, error = start_draft(match)
    if error:
        db.session.rollback()
        return error
    db.session.commit()
    return None


def start_active_game(match):
    """Let an administrator start the clock after the wheel reveal."""
    if not plugin_enabled():
        return "The 1v1 plugin is disabled."
    if match is None or match.status != "active":
        return "There is no active match."
    game = next((item for item in match.games if item.status == "active"), None)
    if game is None:
        return "Reveal the next challenge before starting the game."
    if game.is_tiebreaker:
        return "Tiebreaker games start immediately and have no timer."
    if game.started_at:
        return "This game has already started."
    now = datetime.datetime.utcnow()
    game.started_at = now
    game.deadline = now + datetime.timedelta(minutes=match_duration_minutes())
    game.paused = False
    game.paused_remaining_seconds = None
    db.session.commit()
    return None


def _locked_active_game(match):
    if match is None or match.status != "active":
        return None
    game = next((item for item in match.games if item.status == "active"), None)
    if game is None:
        return None
    return OneVOneBracketGame.query.filter_by(id=game.id).with_for_update().first()


def pause_active_game(match):
    game = _locked_active_game(match)
    if game is None:
        return "There is no active game."
    if game.is_tiebreaker:
        return "Tiebreaker games cannot be paused."
    if not game.started_at:
        return "Start the game before pausing it."
    if game.paused:
        return "This game is already paused."
    remaining = max(0, int((game.deadline - datetime.datetime.utcnow()).total_seconds())) if game.deadline else 0
    game.paused = True
    game.paused_remaining_seconds = remaining
    game.deadline = None
    db.session.commit()
    return None


def resume_active_game(match):
    game = _locked_active_game(match)
    if game is None:
        return "There is no active game."
    if game.is_tiebreaker:
        return "Tiebreaker games do not use a timer."
    if not game.paused:
        return "This game is not paused."
    now = datetime.datetime.utcnow()
    game.paused = False
    game.deadline = now + datetime.timedelta(seconds=max(0, game.paused_remaining_seconds or 0))
    game.paused_remaining_seconds = None
    db.session.commit()
    return None


def add_game_time(match, minutes):
    game = _locked_active_game(match)
    if game is None:
        return "There is no active game."
    if game.is_tiebreaker:
        return "Tiebreaker games have no time limit."
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return "Enter a valid number of minutes."
    if minutes == 0 or minutes < -120 or minutes > 120:
        return "Time adjustment must be between -120 and 120 minutes, excluding 0."
    seconds = minutes * 60
    if game.paused:
        game.paused_remaining_seconds = max(0, (game.paused_remaining_seconds or 0) + seconds)
    elif game.deadline:
        game.deadline += datetime.timedelta(seconds=seconds)
    else:
        return "Start the game before adding time."
    db.session.commit()
    return None


def end_active_game(match):
    """Manually end the timed game and advance the series if necessary."""
    game = _locked_active_game(match)
    if game is None:
        return "There is no active game."
    if game.is_tiebreaker:
        return "Resolve the tiebreaker by a solve or manual score adjustment."
    if not game.started_at:
        return "Start the game before ending it."
    game.status = "timed_out"
    game.finished_at = datetime.datetime.utcnow()
    game.deadline = None
    game.paused = False
    game.paused_remaining_seconds = None
    if game.challenge:
        game.challenge.state = "hidden"
    tie_game, error = _start_tiebreaker_if_series_tied(match)
    if error:
        db.session.commit()
        return error
    db.session.commit()
    return None


def _start_tiebreaker_if_series_tied(match):
    """Start a tiebreaker only after all normal BO games end tied."""
    normal_games = OneVOneBracketGame.query.filter_by(
        match_id=match.id, is_tiebreaker=False
    ).count()
    if normal_games < match.effective_best_of:
        return None, None
    if match.team_one_score != match.team_two_score:
        return None, None
    return start_tiebreaker(match)


def _publish_timer_expired(game, match):
    try:
        current_app.events_manager.publish(
            data={
                "id": game.id,
                "match_id": match.id,
                "challenge": game.challenge.name if game.challenge else None,
            },
            type="onevone_timer_expired",
        )
    except Exception:
        current_app.logger.exception("Unable to publish 1v1 timer-expired event")


def refresh_active_game(match):
    """Expire a normal game and start a tiebreaker only for a tied series."""
    if match is None:
        return None
    game = next((item for item in match.games if item.status == "active"), None)
    if game is None or game.is_tiebreaker or game.paused or not game.deadline:
        return game
    now = datetime.datetime.utcnow()
    if now < game.deadline:
        return game
    locked = OneVOneBracketGame.query.filter_by(id=game.id).with_for_update().first()
    if locked is None or locked.status != "active" or not locked.deadline or now < locked.deadline:
        return locked
    locked.status = "timed_out"
    locked.finished_at = locked.deadline
    locked.deadline = None
    locked.paused = False
    locked.paused_remaining_seconds = None
    if locked.challenge:
        locked.challenge.state = "hidden"
    tie_game, error = _start_tiebreaker_if_series_tied(match)
    if error:
        # Keep the match playable/admin-visible even if tiebreaker setup is incomplete.
        db.session.commit()
        _publish_timer_expired(locked, match)
        return locked
    db.session.commit()
    _publish_timer_expired(locked, match)
    return tie_game


def can_team_access_challenge(challenge_id, team_id):
    """Only the two series teams may access a spun 1v1 challenge."""
    game = OneVOneBracketGame.query.filter_by(challenge_id=challenge_id).first()
    if game is None:
        return True
    return bool(team_id and _entry_for_team(game.match, team_id))


def is_active_game_challenge(challenge_id):
    return (
        OneVOneBracketGame.query.filter_by(
            challenge_id=challenge_id, status="active"
        ).first()
        is not None
    )


def is_paused_game_challenge(challenge_id):
    game = OneVOneBracketGame.query.filter_by(
        challenge_id=challenge_id, status="active"
    ).first()
    return bool(game and game.paused)


def is_onevone_game_challenge(challenge_id):
    return OneVOneBracketGame.query.filter_by(challenge_id=challenge_id).first() is not None


def record_first_solve(challenge_id, team_id, user=None, request=None):
    """Atomically award an active game to its first correct solver.

    Returns ``None`` for ordinary CTFd challenges. For 1v1 games it returns
    whether this solve won the point and whether that point ended the series.
    """
    if not plugin_enabled():
        return None
    game = (
        OneVOneBracketGame.query.filter_by(challenge_id=challenge_id)
        .with_for_update()
        .first()
    )
    if game is None:
        return None
    if game.status != "active":
        return {"awarded": False, "match_complete": game.match.status == "completed"}

    match = OneVOneBracketMatch.query.filter_by(id=game.match_id).with_for_update().first()
    if not game.is_tiebreaker:
        if game.paused:
            return {"awarded": False, "match_complete": False}
        if game.started_at is None:
            return {"awarded": False, "match_complete": False, "not_started": True}
        if game.deadline and datetime.datetime.utcnow() >= game.deadline:
            db.session.rollback()
            refresh_active_game(match)
            return {"awarded": False, "match_complete": False}
    entry = _entry_for_team(match, team_id)
    if entry is None or match.status != "active":
        return {"awarded": False, "match_complete": match.status == "completed"}

    game.winner_entry_id = entry.id
    game.status = "awarded"
    game.finished_at = datetime.datetime.utcnow()
    if entry.id == match.team_one_entry_id:
        match.team_one_score += 1
    else:
        match.team_two_score += 1

    challenge = Challenges.query.filter_by(id=challenge_id).first()
    if challenge:
        challenge.state = "hidden"

    # Keep a normal CTFd solve record for audit/history, but the scoreboard
    # excludes non-Jeopardy challenge sets. This deliberately bypasses the
    # challenge class solve hook so dynamic Jeopardy decay is unaffected by a
    # 1v1 result.
    if user is not None and not Solves.query.filter_by(
        challenge_id=challenge_id, user_id=user.id
    ).first():
        data = (request.form or request.get_json(silent=True)) if request else {}
        db.session.add(Solves(
            user_id=user.id,
            team_id=team_id,
            challenge_id=challenge_id,
            ip=get_ip(req=request) if request else None,
            provided=(data or {}).get("submission", "").strip(),
        ))

    match_complete = max(match.team_one_score, match.team_two_score) >= best_of_wins(
        match.effective_best_of
    )
    if match_complete:
        _finish_match(match)
    db.session.commit()
    try:
        current_app.events_manager.publish(
            data={
                "id": game.id,
                "team": entry.team.name,
                "challenge": challenge.name if challenge else None,
            },
            type="onevone_first_blood",
        )
    except Exception:
        # The solve has already committed; a notification transport failure
        # must not turn a successful 1v1 solve into an API error.
        current_app.logger.exception("Unable to publish 1v1 first-blood event")
    return {"awarded": True, "match_complete": match_complete}


def adjust_active_score(team_slot, delta):
    """Apply an admin correction to the current series score."""
    if not plugin_enabled():
        return None, "The 1v1 plugin is disabled."
    match = active_match()
    if match is None:
        return None, "There is no active match."
    if team_slot not in ("one", "two"):
        return None, "Choose a team whose score should be adjusted."
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        return None, "Invalid score adjustment."
    if delta not in (-1, 1):
        return None, "Score adjustments must add or remove one point."

    field = f"team_{team_slot}_score"
    setattr(match, field, max(0, getattr(match, field) + delta))
    match_complete = max(match.team_one_score, match.team_two_score) >= best_of_wins(
        match.effective_best_of
    )
    if match_complete:
        for game in match.games:
            if game.status == "active":
                game.status = "cancelled"
                game.finished_at = datetime.datetime.utcnow()
                if game.challenge:
                    game.challenge.state = "hidden"
        _finish_match(match)
    db.session.commit()
    return match, None
