"""Generate seeded upper and consolation brackets for the 1v1 plugin."""

from dataclasses import dataclass

from CTFd.models import db

from .models import (
    OneVOneBracket,
    OneVOneBracketEntry,
    OneVOneBracketGame,
    OneVOneBracketMatch,
    OneVOneBracketStage,
    OneVOneDraft,
    OneVOneDraftBan,
)


def _next_power_of_two(value):
    return 1 << (value - 1).bit_length()


def _seed_positions(size):
    positions = [1, 2]
    while len(positions) < size:
        expanded_size = len(positions) * 2
        positions = [seed for item in positions for seed in (item, expanded_size + 1 - item)]
    return positions


@dataclass(frozen=True)
class Slot:
    entry_id: int | None = None
    source_key: int | None = None
    source_result: str | None = None


@dataclass
class MatchSpec:
    key: int
    left: Slot
    right: Slot
    kind: str = "qualifier"
    label: str = "Qualifier"
    placement: int | None = None
    depth: int = 1


class BracketGenerator:
    def __init__(self, entries, winner_count):
        self.entries = entries
        self.winner_count = winner_count
        self.field_size = _next_power_of_two(len(entries))
        self.specs = []
        self._next_key = 1

    def _new_match(self, left, right, depth, label="Qualifier"):
        spec = MatchSpec(
            key=self._next_key,
            left=left,
            right=right,
            depth=depth,
            label=label,
        )
        self._next_key += 1
        self.specs.append(spec)
        return Slot(source_key=spec.key, source_result="winner"), Slot(
            source_key=spec.key, source_result="loser"
        ), spec

    @staticmethod
    def _slot_depth(slot, specs):
        return specs[slot.source_key].depth if slot.source_key else 0

    def _elimination_tree(self, sources):
        """Create a seeded elimination tree and return its final and loss cohorts."""
        size = _next_power_of_two(len(sources))
        slots = list(sources) + [None] * (size - len(sources))
        rounds = []
        specs_by_key = {spec.key: spec for spec in self.specs}

        while len(slots) > 1:
            next_slots = []
            round_losers = []
            for index in range(0, len(slots), 2):
                left, right = slots[index], slots[index + 1]
                if left is None:
                    next_slots.append(right)
                elif right is None:
                    next_slots.append(left)
                else:
                    depth = max(
                        self._slot_depth(left, specs_by_key),
                        self._slot_depth(right, specs_by_key),
                    ) + 1
                    winner, loser, spec = self._new_match(left, right, depth)
                    specs_by_key[spec.key] = spec
                    next_slots.append(winner)
                    round_losers.append(loser)
            rounds.append(round_losers)
            slots = next_slots

        final = slots[0]
        final_spec = specs_by_key.get(final.source_key) if final and final.source_key else None
        return final, final_spec, rounds, size

    def _classification(self, sources, rank_start):
        """Build a consolation tree for the ranking range beginning at rank_start."""
        if not sources or rank_start > self.winner_count:
            return
        if len(sources) == 1:
            return

        _, final_spec, loser_rounds, size = self._elimination_tree(sources)
        if final_spec is None:
            return

        final_spec.kind = "placement"
        final_spec.placement = rank_start
        final_spec.label = _placement_label(rank_start)

        # Earlier-round losers form lower ranking groups. Generate their
        # classification paths only when they can still produce a requested rank.
        for round_number, losers in enumerate(loser_rounds[:-1], start=1):
            lower_rank = rank_start + size // (2**round_number)
            self._classification(losers, lower_rank)

    def build(self):
        seeded = {entry.seed: Slot(entry_id=entry.id) for entry in self.entries}
        initial_slots = [seeded.get(seed) for seed in _seed_positions(self.field_size)]
        _, final_spec, loser_rounds, _ = self._elimination_tree(initial_slots)
        if final_spec is None:
            raise ValueError("At least two teams are required to create a bracket")

        final_spec.kind = "grand_final"
        final_spec.label = "Grand Final — 1st / 2nd Place"
        final_spec.placement = 1

        # Upper-bracket losses map naturally to rank ranges: semifinal losses
        # compete for 3rd/4th, quarterfinal losses for 5th–8th, and so on.
        for round_number, losers in enumerate(loser_rounds[:-1], start=1):
            rank_start = self.field_size // (2**round_number) + 1
            self._classification(losers, rank_start)

        return self.specs


def _placement_label(rank):
    suffix = "th"
    if rank % 100 not in (11, 12, 13):
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(rank % 10, "th")
    return f"{rank}{suffix} Place"


def generate_bracket(team_ids, winner_count):
    """Replace the current bracket with one seeded from the supplied team IDs."""
    existing = OneVOneBracket.query.order_by(OneVOneBracket.id.desc()).all()
    for bracket in existing:
        # Delete dependent rows explicitly. In addition to making replacement
        # work consistently across SQLite/MySQL/PostgreSQL, this avoids an
        # ordering issue from matches that reference earlier matches.
        match_ids = [match.id for match in bracket.matches]
        if match_ids:
            draft_ids = [
                draft_id
                for draft_id, in OneVOneDraft.query.with_entities(OneVOneDraft.id)
                .filter(OneVOneDraft.match_id.in_(match_ids))
                .all()
            ]
            if draft_ids:
                OneVOneDraftBan.query.filter(
                    OneVOneDraftBan.draft_id.in_(draft_ids)
                ).delete(synchronize_session=False)
                OneVOneDraft.query.filter(
                    OneVOneDraft.id.in_(draft_ids)
                ).delete(synchronize_session=False)
            OneVOneBracketGame.query.filter(
                OneVOneBracketGame.match_id.in_(match_ids)
            ).delete(synchronize_session=False)
        OneVOneBracketMatch.query.filter_by(bracket_id=bracket.id).delete(
            synchronize_session=False
        )
        OneVOneBracketStage.query.filter_by(bracket_id=bracket.id).delete(
            synchronize_session=False
        )
        OneVOneBracketEntry.query.filter_by(bracket_id=bracket.id).delete(
            synchronize_session=False
        )
        db.session.delete(bracket)
    db.session.flush()

    bracket = OneVOneBracket(winner_count=winner_count)
    db.session.add(bracket)
    db.session.flush()

    entries = []
    for seed, team_id in enumerate(team_ids, start=1):
        entry = OneVOneBracketEntry(bracket_id=bracket.id, team_id=team_id, seed=seed)
        db.session.add(entry)
        entries.append(entry)
    db.session.flush()

    specs = BracketGenerator(entries, winner_count).build()
    qualifier_depths = sorted({spec.depth for spec in specs if spec.kind == "qualifier"})
    stages = {}
    sequence = 1
    for depth in qualifier_depths:
        stages[("qualifier", depth)] = OneVOneBracketStage(
            bracket_id=bracket.id,
            sequence=sequence,
            title=f"Qualifier Stage {sequence}",
            kind="qualifier",
        )
        db.session.add(stages[("qualifier", depth)])
        sequence += 1

    placement_ranks = sorted(
        {spec.placement for spec in specs if spec.kind == "placement"}, reverse=True
    )
    for rank in placement_ranks:
        stages[("placement", rank)] = OneVOneBracketStage(
            bracket_id=bracket.id,
            sequence=sequence,
            title=_placement_label(rank),
            kind="placement",
        )
        db.session.add(stages[("placement", rank)])
        sequence += 1

    stages[("grand_final", 1)] = OneVOneBracketStage(
        bracket_id=bracket.id,
        sequence=sequence,
        title="Grand Final — 1st / 2nd Place",
        kind="grand_final",
        best_of=3,
    )
    db.session.add(stages[("grand_final", 1)])
    db.session.flush()

    matches_by_key = {}
    # Match IDs are generated from the elimination tree, but the public/admin
    # numbering should follow the actual stage order. This keeps placement
    # matches ahead of the grand final (for example: 5th, 3rd, grand final).
    def spec_stage_sequence(spec):
        if spec.kind == "qualifier":
            return stages[("qualifier", spec.depth)].sequence
        if spec.kind == "placement":
            return stages[("placement", spec.placement)].sequence
        return stages[("grand_final", 1)].sequence

    ordered_specs = sorted(specs, key=lambda spec: (spec_stage_sequence(spec), spec.key))
    for sequence, spec in enumerate(ordered_specs, start=1):
        if spec.kind == "qualifier":
            stage = stages[("qualifier", spec.depth)]
        elif spec.kind == "placement":
            stage = stages[("placement", spec.placement)]
        else:
            stage = stages[("grand_final", 1)]
        match = OneVOneBracketMatch(
            bracket_id=bracket.id,
            stage_id=stage.id,
            sequence=sequence,
            label=spec.label,
            kind=spec.kind,
            placement=spec.placement,
        )
        db.session.add(match)
        matches_by_key[spec.key] = match
    db.session.flush()

    for spec in specs:
        match = matches_by_key[spec.key]
        for prefix, slot in (("team_one", spec.left), ("team_two", spec.right)):
            if slot.entry_id:
                setattr(match, f"{prefix}_entry_id", slot.entry_id)
            elif slot.source_key:
                setattr(match, f"{prefix}_source_match_id", matches_by_key[slot.source_key].id)
                setattr(match, f"{prefix}_source_result", slot.source_result)

    db.session.commit()
    return bracket
