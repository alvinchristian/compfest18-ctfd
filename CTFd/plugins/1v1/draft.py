"""Server-authoritative per-game category draft and automatic wheel."""

import datetime
import json
import secrets

from sqlalchemy.exc import IntegrityError

from CTFd.cache import clear_challenges
from CTFd.models import Challenges, db
from CTFd.utils import get_config, set_config

from .models import OneVOneBracketGame, OneVOneDraft, OneVOneDraftBan


WHEEL_CATEGORIES_CONFIG_KEY = "onevone_wheel_categories"
GRAND_FINAL_CATEGORIES_CONFIG_KEY = "onevone_grand_final_categories"
TIEBREAKER_CATEGORIES_CONFIG_KEY = "onevone_tiebreaker_categories"
SHOWN_CHALLENGES_CONFIG_KEY = "onevone_shown_challenge_ids"


def configured_categories(match=None):
    """Return configured categories for qualifiers or the grand final."""
    grand_final = bool(match and match.kind == "grand_final")
    config_key = GRAND_FINAL_CATEGORIES_CONFIG_KEY if grand_final else WHEEL_CATEGORIES_CONFIG_KEY
    mode = "onevone_grand_final" if grand_final else "onevone"
    stored = get_config(config_key, default="[]")
    try:
        configured = json.loads(stored) if isinstance(stored, str) else stored
    except (TypeError, ValueError):
        configured = []
    if not isinstance(configured, list):
        return []

    available = {
        category
        for category, in Challenges.query.with_entities(Challenges.category)
        .filter(
            Challenges.competition_mode == mode,
            Challenges.category.isnot(None),
            Challenges.category != "",
        )
        .distinct()
        .all()
    }
    return list(
        dict.fromkeys(
            category
            for category in configured
            if isinstance(category, str) and category in available
        )
    )


def _shown_challenge_ids():
    """Combine legacy wheel history with challenges persisted in game rows."""
    stored = get_config(SHOWN_CHALLENGES_CONFIG_KEY, default="[]")
    try:
        stored_ids = json.loads(stored) if isinstance(stored, str) else stored
    except (TypeError, ValueError):
        stored_ids = []
    shown = {
        challenge_id
        for challenge_id in stored_ids
        if isinstance(challenge_id, int)
    } if isinstance(stored_ids, list) else set()
    shown.update(
        challenge_id
        for challenge_id, in OneVOneBracketGame.query.with_entities(
            OneVOneBracketGame.challenge_id
        ).filter(OneVOneBracketGame.challenge_id.isnot(None)).all()
    )
    return shown


def _remember_challenge(challenge_id):
    """Keep wheel history even when an administrator regenerates the bracket."""
    shown = _shown_challenge_ids()
    shown.add(challenge_id)
    set_config(SHOWN_CHALLENGES_CONFIG_KEY, json.dumps(sorted(shown)))


def _candidate_challenges(categories, lock=False, competition_mode="onevone"):
    if not categories:
        return {}
    query = Challenges.query.filter(
        Challenges.competition_mode == competition_mode,
        Challenges.category.in_(categories),
        Challenges.state == "hidden",
    )
    shown = _shown_challenge_ids()
    if shown:
        query = query.filter(Challenges.id.notin_(shown))
    if lock:
        query = query.with_for_update()

    result = {category: [] for category in categories}
    for challenge in query.order_by(Challenges.id.asc()).all():
        result[challenge.category].append(challenge)
    return {category: values for category, values in result.items() if values}


def _tiebreaker_categories():
    stored = get_config(TIEBREAKER_CATEGORIES_CONFIG_KEY, default="[]")
    try:
        configured = json.loads(stored) if isinstance(stored, str) else stored
    except (TypeError, ValueError):
        configured = []
    available = {
        category for category, in Challenges.query.with_entities(Challenges.category)
        .filter(Challenges.competition_mode == "onevone_tiebreaker").distinct().all()
    }
    return list(dict.fromkeys(c for c in configured if isinstance(c, str) and c in available))


def _candidate_tiebreakers(categories, lock=False):
    query = Challenges.query.filter(
        Challenges.competition_mode == "onevone_tiebreaker",
        Challenges.category.in_(categories), Challenges.state == "hidden",
    )
    shown = _shown_challenge_ids()
    if shown:
        query = query.filter(Challenges.id.notin_(shown))
    if lock:
        query = query.with_for_update()
    result = {category: [] for category in categories}
    for challenge in query.order_by(Challenges.id.asc()).all():
        result[challenge.category].append(challenge)
    return {category: values for category, values in result.items() if values}


def _ordered_entries(match):
    entries = [match.team_one_entry, match.team_two_entry]
    return sorted(entries, key=lambda entry: (entry.seed, entry.id))


def latest_draft(match):
    return match.drafts[-1] if match and match.drafts else None


def start_draft(match):
    """Start a fresh two-ban phase for the match's next game."""
    current = latest_draft(match)
    if current and current.status == "active":
        return current, "The next game's draft is already active."
    if any(game.status == "active" for game in match.games):
        return None, "Wait for the current game to finish before drafting again."

    used_categories = {game.category for game in match.games}
    configured = [
        category
        for category in configured_categories(match)
        if category not in used_categories
    ]
    competition_mode = "onevone_grand_final" if match.kind == "grand_final" else "onevone"
    candidates = _candidate_challenges(configured, competition_mode=competition_mode)
    categories = [category for category in configured if category in candidates]
    if len(categories) < 3:
        return (
            None,
            "At least three unused 1v1 categories with unshown hidden challenges are required for the next game.",
        )

    now = datetime.datetime.utcnow()
    draft = OneVOneDraft(
        match_id=match.id,
        game_number=len(match.games) + 1,
        categories=categories,
        status="active",
        current_turn=1,
        started_at=now,
        deadline=None,
    )
    match.game_ready = False
    db.session.add(draft)
    return draft, None


def _current_entry(draft):
    entries = _ordered_entries(draft.match)
    index = draft.current_turn - 1
    return entries[index] if 0 <= index < len(entries) else None


def _remaining_categories(draft):
    banned = {ban.category for ban in draft.bans}
    return [category for category in draft.categories if category not in banned]


def _spin_automatically(draft, completed_at):
    """Select and reveal one challenge as part of completing the draft."""
    mode = "onevone_grand_final" if draft.match.kind == "grand_final" else "onevone"
    candidates = _candidate_challenges(
        _remaining_categories(draft), lock=True, competition_mode=mode
    )
    if not candidates:
        return "No eligible challenge remains outside the banned categories."

    category = secrets.choice(list(candidates))
    challenge = secrets.choice(candidates[category])
    game = OneVOneBracketGame(
        match_id=draft.match_id,
        game_number=draft.game_number,
        category=category,
        challenge_id=challenge.id,
        status="active",
    )
    db.session.add(game)
    db.session.flush()

    draft.game = game
    draft.status = "completed"
    draft.completed_at = completed_at
    draft.deadline = None
    draft.match.game_ready = False
    challenge.state = "visible"
    # Record the reveal immediately. A challenge is consumed by being shown,
    # regardless of whether either team eventually solves it.
    _remember_challenge(challenge.id)
    return None


def start_tiebreaker(match):
    """Create an untimed tiebreaker that waits for an administrator spin."""
    categories = [c for c in _tiebreaker_categories() if c not in {g.category for g in match.games if g.is_tiebreaker}]
    candidates = _candidate_tiebreakers(categories, lock=True)
    if not candidates:
        return None, "No hidden tiebreaker challenge is configured."
    game = OneVOneBracketGame(
        match_id=match.id, game_number=len(match.games) + 1,
        category="", challenge_id=None, status="active",
        is_tiebreaker=True,
    )
    db.session.add(game)
    db.session.flush()
    return game, None


def spin_tiebreaker(match):
    """Select and reveal a tiebreaker challenge after the admin spins."""
    if match is None:
        return None, "There is no active match."
    game = (
        OneVOneBracketGame.query.filter_by(
            match_id=match.id, status="active", is_tiebreaker=True
        )
        .order_by(OneVOneBracketGame.id.desc())
        .with_for_update()
        .first()
    )
    if game is None:
        return None, "There is no pending tiebreaker wheel."
    if game.challenge_id:
        return game, "The tiebreaker wheel has already been spun."
    used_categories = {
        item.category
        for item in game.match.games
        if item.is_tiebreaker and item.id != game.id and item.category
    }
    categories = [category for category in _tiebreaker_categories() if category not in used_categories]
    candidates = _candidate_tiebreakers(categories, lock=True)
    if not candidates:
        return None, "No hidden tiebreaker challenge is configured."
    category = secrets.choice(list(candidates))
    challenge = secrets.choice(candidates[category])
    game.category = category
    game.challenge_id = challenge.id
    challenge.state = "visible"
    _remember_challenge(challenge.id)
    clear_challenges()
    db.session.commit()
    return game, None


def _tiebreaker_wheel_categories(game):
    """Return the categories that were available when a tiebreaker spun."""
    used_categories = {
        item.category
        for item in game.match.games
        if item.is_tiebreaker and item.id != game.id and item.category
    }
    configured = [category for category in _tiebreaker_categories() if category not in used_categories]
    candidates = _candidate_tiebreakers(configured)
    categories = [category for category in configured if category in candidates]
    # The selected challenge is now in the shown-history set, so add its
    # category back to the visual wheel explicitly.
    if game.category and game.category not in categories:
        categories.append(game.category)
    return categories or [game.category]


def _record_ban(draft, entry, category, automatic, selected_at):
    ban = OneVOneDraftBan(
        draft=draft,
        entry_id=entry.id,
        turn=draft.current_turn,
        category=category,
        automatic=automatic,
        created_at=selected_at,
    )
    db.session.add(ban)
    draft.current_turn += 1
    if draft.current_turn > 2:
        error = _spin_automatically(draft, selected_at)
        if error:
            return None, error
    return ban, None


def _advance_expired_locked(draft, now):
    """Drafts no longer expire; bans are always submitted by an administrator."""
    return None


def process_expired_draft(match):
    """Return the current draft; draft turns remain active until admin action."""
    return latest_draft(match)


def submit_ban(match, team_id, category, admin=False):
    """Record the active team's choice atomically."""
    if match is None:
        return None, "There is no active match."
    draft = latest_draft(match)
    if draft is None or draft.status != "active":
        return None, "This match has no active draft."

    draft = OneVOneDraft.query.filter_by(id=draft.id).with_for_update().first()
    now = datetime.datetime.utcnow()
    timeout_error = _advance_expired_locked(draft, now)
    if timeout_error:
        db.session.commit()
        return None, timeout_error
    if draft.status != "active":
        spun_challenge_id = draft.game.challenge_id if draft.game_id else None
        db.session.commit()
        if spun_challenge_id:
            _remember_challenge(spun_challenge_id)
            clear_challenges()
        return None, "The draft is already complete."

    entry = _current_entry(draft)
    if entry is None or (not admin and entry.team_id != team_id):
        db.session.commit()
        return None, "It is not your team's turn to ban."
    if category not in _remaining_categories(draft):
        db.session.commit()
        return None, "Choose an available category."

    try:
        ban, error = _record_ban(
            draft,
            entry,
            category,
            automatic=False,
            selected_at=now,
        )
        if error:
            db.session.rollback()
            return None, error
        if draft.status == "active":
            draft.deadline = None
        spun_challenge_id = draft.game.challenge_id if draft.game_id else None
        db.session.commit()
        if spun_challenge_id:
            _remember_challenge(spun_challenge_id)
            clear_challenges()
        return ban, None
    except IntegrityError:
        db.session.rollback()
        return None, "That category was already banned."


def draft_active(match):
    draft = process_expired_draft(match)
    return bool(draft and draft.status == "active")


def _isoformat(value):
    return value.isoformat(timespec="milliseconds") + "Z" if value else None


def serialize_draft(match, team_id=None, is_admin_user=False):
    """Build public state for polling clients."""
    now = datetime.datetime.utcnow()
    if match is None:
        return {"status": "idle", "server_time": _isoformat(now)}

    draft = process_expired_draft(match)
    # Import lazily to avoid the scoring -> draft import cycle.
    from .scoring import match_duration_minutes, refresh_active_game
    refresh_active_game(match)
    if draft is None:
        return {"status": "idle", "server_time": _isoformat(now)}

    entries = _ordered_entries(match)
    bans_by_category = {ban.category: ban for ban in draft.bans}
    current_entry = _current_entry(draft) if draft.status == "active" else None
    categories = []
    for category in draft.categories:
        ban = bans_by_category.get(category)
        categories.append(
            {
                "name": category,
                "banned": ban is not None,
                "team": ban.entry.team.name if ban else None,
                "seed": ban.entry.seed if ban else None,
                "automatic": ban.automatic if ban else False,
                "turn": ban.turn if ban else None,
            }
        )

    game = next((item for item in match.games if item.status == "active"), None)
    if game is None:
        game = draft.game
    default_timer_seconds = match_duration_minutes() * 60
    wheel_categories = (
        _tiebreaker_wheel_categories(game)
        if game and game.is_tiebreaker
        else (_remaining_categories(draft) if game and draft.status == "completed" else [game.category] if game else [])
    )
    return {
        "id": draft.id,
        "status": draft.status,
        "game_number": draft.game_number,
        "server_time": _isoformat(datetime.datetime.utcnow()),
        # Draft turns have no timeout; retain the field for API compatibility.
        "deadline": None,
        "match": {
            "id": match.id,
            "sequence": match.sequence,
            "label": match.label,
            "teams": [
                {"id": entry.team_id, "name": entry.team.name, "seed": entry.seed}
                for entry in entries
            ],
        },
        "current_team": (
            {
                "id": current_entry.team_id,
                "name": current_entry.team.name,
                "seed": current_entry.seed,
            }
            if current_entry
            else None
        ),
        "is_admin": bool(is_admin_user),
        "can_ban": bool(
            is_admin_user or (current_entry and team_id and current_entry.team_id == team_id)
        ),
        "categories": categories,
        "game": (
            {
                "id": game.id, "status": game.status, "is_tiebreaker": bool(game.is_tiebreaker),
                "started_at": _isoformat(game.started_at), "deadline": _isoformat(game.deadline),
                "timer_seconds": (
                    max(0, int((game.deadline - datetime.datetime.utcnow()).total_seconds()))
                    if game.deadline
                    else (default_timer_seconds if game.started_at is None else None)
                ),
                "paused": bool(game.paused),
                "paused_remaining_seconds": game.paused_remaining_seconds,
            } if game else None
        ),
        "wheel": (
            {
                "categories": wheel_categories,
                "selected_category": game.category or None,
                "challenge": {"id": game.challenge_id, "name": game.challenge.name} if game.challenge else None,
                "is_tiebreaker": bool(game.is_tiebreaker),
            }
            if game
            else None
        ),
        "message": (
            "The automatic wheel could not select a challenge."
            if draft.status == "error"
            else None
        ),
    }
