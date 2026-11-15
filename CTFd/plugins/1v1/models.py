"""Persistent models for the public 1v1 tournament bracket."""

import datetime

from CTFd.models import db


class OneVOneBracket(db.Model):
    __tablename__ = "onevone_brackets"

    id = db.Column(db.Integer, primary_key=True)
    winner_count = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)

    entries = db.relationship(
        "OneVOneBracketEntry",
        backref="bracket",
        cascade="all, delete-orphan",
        order_by="OneVOneBracketEntry.seed",
    )
    stages = db.relationship(
        "OneVOneBracketStage",
        backref="bracket",
        cascade="all, delete-orphan",
        order_by="OneVOneBracketStage.sequence",
    )
    matches = db.relationship(
        "OneVOneBracketMatch",
        backref="bracket",
        cascade="all, delete-orphan",
    )


class OneVOneBracketEntry(db.Model):
    __tablename__ = "onevone_bracket_entries"
    __table_args__ = (db.UniqueConstraint("bracket_id", "seed"),)

    id = db.Column(db.Integer, primary_key=True)
    bracket_id = db.Column(
        db.Integer, db.ForeignKey("onevone_brackets.id", ondelete="CASCADE"), nullable=False
    )
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    seed = db.Column(db.Integer, nullable=False)

    team = db.relationship("Teams", foreign_keys="OneVOneBracketEntry.team_id")


class OneVOneBracketStage(db.Model):
    __tablename__ = "onevone_bracket_stages"
    __table_args__ = (db.UniqueConstraint("bracket_id", "sequence"),)

    id = db.Column(db.Integer, primary_key=True)
    bracket_id = db.Column(
        db.Integer, db.ForeignKey("onevone_brackets.id", ondelete="CASCADE"), nullable=False
    )
    sequence = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(80), nullable=False)
    kind = db.Column(db.String(32), nullable=False)
    best_of = db.Column(db.Integer, nullable=False, default=1)

    matches = db.relationship(
        "OneVOneBracketMatch",
        backref="stage",
        cascade="all, delete-orphan",
        order_by="OneVOneBracketMatch.sequence",
    )


class OneVOneBracketMatch(db.Model):
    __tablename__ = "onevone_bracket_matches"
    __table_args__ = (db.UniqueConstraint("bracket_id", "sequence"),)

    id = db.Column(db.Integer, primary_key=True)
    bracket_id = db.Column(
        db.Integer, db.ForeignKey("onevone_brackets.id", ondelete="CASCADE"), nullable=False
    )
    stage_id = db.Column(
        db.Integer, db.ForeignKey("onevone_bracket_stages.id", ondelete="CASCADE"), nullable=False
    )
    sequence = db.Column(db.Integer, nullable=False)
    label = db.Column(db.String(120), nullable=False)
    kind = db.Column(db.String(32), nullable=False)
    placement = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(32), nullable=False, default="pending")
    best_of = db.Column(db.Integer, nullable=True)
    team_one_score = db.Column(db.Integer, nullable=False, default=0)
    team_two_score = db.Column(db.Integer, nullable=False, default=0)
    game_ready = db.Column(db.Boolean, nullable=False, default=False)

    team_one_entry_id = db.Column(
        db.Integer, db.ForeignKey("onevone_bracket_entries.id", ondelete="SET NULL"), nullable=True
    )
    team_two_entry_id = db.Column(
        db.Integer, db.ForeignKey("onevone_bracket_entries.id", ondelete="SET NULL"), nullable=True
    )
    team_one_source_match_id = db.Column(
        db.Integer, db.ForeignKey("onevone_bracket_matches.id", ondelete="SET NULL"), nullable=True
    )
    team_two_source_match_id = db.Column(
        db.Integer, db.ForeignKey("onevone_bracket_matches.id", ondelete="SET NULL"), nullable=True
    )
    team_one_source_result = db.Column(db.String(16), nullable=True)
    team_two_source_result = db.Column(db.String(16), nullable=True)
    winner_entry_id = db.Column(
        db.Integer, db.ForeignKey("onevone_bracket_entries.id", ondelete="SET NULL"), nullable=True
    )
    loser_entry_id = db.Column(
        db.Integer, db.ForeignKey("onevone_bracket_entries.id", ondelete="SET NULL"), nullable=True
    )

    team_one_entry = db.relationship(
        "OneVOneBracketEntry", foreign_keys=[team_one_entry_id]
    )
    team_two_entry = db.relationship(
        "OneVOneBracketEntry", foreign_keys=[team_two_entry_id]
    )
    winner_entry = db.relationship(
        "OneVOneBracketEntry", foreign_keys=[winner_entry_id]
    )
    loser_entry = db.relationship(
        "OneVOneBracketEntry", foreign_keys=[loser_entry_id]
    )

    @property
    def effective_best_of(self):
        return self.best_of or self.stage.best_of


class OneVOneBracketGame(db.Model):
    """One wheel-selected challenge and its point within a bracket match."""

    __tablename__ = "onevone_bracket_games"
    __table_args__ = (db.UniqueConstraint("match_id", "game_number"),)

    id = db.Column(db.Integer, primary_key=True)
    match_id = db.Column(
        db.Integer,
        db.ForeignKey("onevone_bracket_matches.id", ondelete="CASCADE"),
        nullable=False,
    )
    game_number = db.Column(db.Integer, nullable=False)
    category = db.Column(db.String(80), nullable=False)
    challenge_id = db.Column(
        db.Integer, db.ForeignKey("challenges.id", ondelete="SET NULL"), nullable=True
    )
    status = db.Column(db.String(32), nullable=False, default="active")
    is_tiebreaker = db.Column(db.Boolean, nullable=False, default=False)
    started_at = db.Column(db.DateTime, nullable=True)
    deadline = db.Column(db.DateTime, nullable=True)
    paused = db.Column(db.Boolean, nullable=False, default=False)
    paused_remaining_seconds = db.Column(db.Integer, nullable=True)
    winner_entry_id = db.Column(
        db.Integer,
        db.ForeignKey("onevone_bracket_entries.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)
    finished_at = db.Column(db.DateTime, nullable=True)

    match = db.relationship(
        "OneVOneBracketMatch",
        backref=db.backref("games", cascade="all, delete-orphan", order_by="OneVOneBracketGame.game_number"),
    )
    challenge = db.relationship("Challenges", foreign_keys=[challenge_id])
    winner_entry = db.relationship("OneVOneBracketEntry", foreign_keys=[winner_entry_id])


class OneVOneDraft(db.Model):
    """The two-turn category ban phase for one game in a match."""

    __tablename__ = "onevone_game_drafts"
    __table_args__ = (db.UniqueConstraint("match_id", "game_number"),)

    id = db.Column(db.Integer, primary_key=True)
    match_id = db.Column(
        db.Integer,
        db.ForeignKey("onevone_bracket_matches.id", ondelete="CASCADE"),
        nullable=False,
    )
    game_number = db.Column(db.Integer, nullable=False)
    game_id = db.Column(
        db.Integer,
        db.ForeignKey("onevone_bracket_games.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    categories = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="active")
    current_turn = db.Column(db.Integer, nullable=False, default=1)
    started_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)
    deadline = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    match = db.relationship(
        "OneVOneBracketMatch",
        backref=db.backref(
            "drafts",
            cascade="all, delete-orphan",
            order_by="OneVOneDraft.game_number",
        ),
    )
    game = db.relationship("OneVOneBracketGame", foreign_keys=[game_id])


class OneVOneDraftBan(db.Model):
    """One team's manual or automatic category ban."""

    __tablename__ = "onevone_game_draft_bans"
    __table_args__ = (
        db.UniqueConstraint("draft_id", "turn"),
        db.UniqueConstraint("draft_id", "category"),
        db.UniqueConstraint("draft_id", "entry_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    draft_id = db.Column(
        db.Integer,
        db.ForeignKey("onevone_game_drafts.id", ondelete="CASCADE"),
        nullable=False,
    )
    entry_id = db.Column(
        db.Integer,
        db.ForeignKey("onevone_bracket_entries.id", ondelete="CASCADE"),
        nullable=False,
    )
    turn = db.Column(db.Integer, nullable=False)
    category = db.Column(db.String(80), nullable=False)
    automatic = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)

    draft = db.relationship(
        "OneVOneDraft",
        backref=db.backref(
            "bans",
            cascade="all, delete-orphan",
            order_by="OneVOneDraftBan.turn",
        ),
    )
    entry = db.relationship("OneVOneBracketEntry", foreign_keys=[entry_id])
