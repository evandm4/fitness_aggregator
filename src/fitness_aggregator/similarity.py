"""Find exercises that may be the same movement, and suggest weight offsets.

Nothing here merges anything: it produces candidates for you to review with
``fitness merge review``.

Two tiers of candidates:

* **likely** — names that match after normalizing: case, punctuation, word
  order, plurals (``Tricep``/``Triceps``) and machine numbers
  (``Hip Abductor 2 (Machine)`` = ``Hip Abductor (Machine)``).
* **possible** — near-identical names (``difflib`` ratio ≥ 0.88) among names
  with no likely match, unless they differ by a word pair that usually means
  a different movement (incline/decline, abductor/adductor, barbell/dumbbell…).

Offset suggestions compare top sets from the sessions on either side of the
switch between the two exercises (or the closest sessions in time if you used
both during the same period), so real progress over time isn't mistaken for
a machine difference.
"""

from __future__ import annotations

import difflib
import itertools
import math
import re
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from .catalog import ASSISTED, LOADED
from .models import SetType, Workout

LIKELY, POSSIBLE = "likely", "possible"
FUZZY_THRESHOLD = 0.88
SESSIONS_COMPARED = 3

# Word pairs that usually mean a different movement, not a different machine.
CONTRASTS = [
    {"incline", "decline"}, {"abductor", "adductor"}, {"barbell", "dumbbell"},
    {"front", "rear"}, {"front", "back"}, {"upper", "lower"}, {"seated", "standing"},
    {"flexion", "extension"}, {"curl", "extension"}, {"push", "pull"},
]


def tokens(name: str) -> tuple[str, ...]:
    out = []
    for t in re.split(r"[^a-z0-9]+", name.lower()):
        if not t or t.isdigit():
            continue
        if len(t) > 4 and t.endswith("s"):
            t = t[:-1]  # triceps → tricep, raises → raise
        out.append(t)
    return tuple(sorted(out))


def _contrast(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """True if one name has one word of a contrast pair and the other has its opposite."""
    only_a, only_b = set(a) - set(b), set(b) - set(a)
    return any(pair & only_a and pair & only_b for pair in CONTRASTS)


def _display_order(name: str) -> str:
    """Sort so a base name comes before its numbered variants: Row (Machine) before Row (Machine 2)."""
    return re.sub(r"[()\-]", " ", name.lower())


@dataclass
class CandidateGroup:
    names: list[str]
    tier: str

    @property
    def pairs(self) -> list[frozenset[str]]:
        return [frozenset(p) for p in itertools.combinations(self.names, 2)]


def find_candidates(names: Iterable[str], separate: set[frozenset[str]] = frozenset()) -> list[CandidateGroup]:
    """Candidate groups among ``names`` (current exercise names, merges already resolved).

    Groups whose every pair you've marked as separate are left out.
    """
    names = sorted(set(names))
    by_key: dict[tuple, list[str]] = defaultdict(list)
    for n in names:
        by_key[tokens(n)].append(n)

    groups = [CandidateGroup(sorted(v, key=_display_order), LIKELY) for v in by_key.values() if len(v) > 1]
    singles = [v[0] for v in by_key.values() if len(v) == 1]
    for a, b in itertools.combinations(sorted(singles), 2):
        ta, tb = tokens(a), tokens(b)
        ratio = difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
        if ratio >= FUZZY_THRESHOLD and not _contrast(ta, tb):
            groups.append(CandidateGroup(sorted([a, b], key=_display_order), POSSIBLE))

    undecided = [g for g in groups if any(p not in separate for p in g.pairs)]
    return sorted(undecided, key=lambda g: (g.tier != LIKELY, g.names))


# --- offset suggestions -----------------------------------------------------------

@dataclass(frozen=True)
class TopSet:
    when: datetime
    weight: float
    reps: float
    seconds: float = 0.0


def top_sets(workouts: Iterable[Workout], exercise: str) -> list[TopSet]:
    """The top working set of ``exercise`` in each workout, oldest first."""
    out = []
    for w in workouts:
        sets = [s for e in w.exercises if e.name == exercise for s in e.sets
                if s.set_type in (SetType.NORMAL, SetType.FAILURE) and (s.reps > 0 or s.seconds > 0)]
        if sets:
            best = max(sets, key=lambda s: (s.weight, s.reps, s.seconds))
            out.append(TopSet(w.started_at, best.weight, best.reps, best.seconds))
    return sorted(out, key=lambda t: t.when)


def describe_sets(sets: list[TopSet]) -> str:
    def span(values):
        lo, hi = min(values), max(values)
        return f"{lo:g}" if lo == hi else f"{lo:g}–{hi:g}"
    if all(s.weight == 0 and s.reps == 0 for s in sets):
        return f"{span([s.seconds for s in sets])} s"
    return f"{span([s.weight for s in sets])} lb × {span([s.reps for s in sets])}"


def round_to(x: float, step: float = 5) -> float:
    """Round to the nearest ``step``, halves away from zero (12.5 → 15, -12.5 → -15).

    Python's ``round`` rounds halves to even, which made 12.5 → 10 but 17.5 → 20.
    """
    q = abs(x) / step
    return math.copysign(step * math.floor(q + 0.5), x) + 0.0


@dataclass
class Suggestion:
    """How to adjust ``adjusted`` so its weights line up with ``kept``."""

    kept: str
    adjusted: str
    relation: str               # how the two were used over time, in words
    kept_sets: list[TopSet] = field(default_factory=list)
    adjusted_sets: list[TopSet] = field(default_factory=list)
    offset: float | None = None   # lb to add to every adjusted set
    factor: float | None = None   # or multiply by this

    @property
    def summary(self) -> str:
        if self.offset is None:
            return "No offset suggestion (not enough comparable sets)."
        if self.offset == 0:
            return f"{self.adjusted} sets were about the same as {self.kept} at similar reps: no offset needed."
        direction = "heavier" if self.offset < 0 else "lighter"
        factor = f" (or ×{self.factor:g})" if self.factor else ""
        return (f"{self.adjusted} top sets were about {abs(self.offset):g} lb {direction} at similar reps. "
                f"Suggested: {self.offset:+g} lb on {self.adjusted}{factor}.")


def _select(kept_name: str, kept: list[TopSet], adjusted_name: str, adjusted: list[TopSet]):
    """Pick the sessions to compare and describe how the two exercises were used over time."""
    n = SESSIONS_COMPARED
    k_start, k_end = kept[0].when, kept[-1].when
    a_start, a_end = adjusted[0].when, adjusted[-1].when
    if k_end < a_start:
        return (kept[-n:], adjusted[:n],
                f"You switched from {kept_name} to {adjusted_name} on {a_start:%Y-%m-%d} (no overlap).")
    if a_end < k_start:
        return (kept[:n], adjusted[-n:],
                f"You switched from {adjusted_name} to {kept_name} on {k_start:%Y-%m-%d} (no overlap).")
    # Used during the same period: pair each adjusted session with the nearest kept one.
    def nearest(a: TopSet) -> TopSet:
        return min(kept, key=lambda k: abs((k.when - a.when).total_seconds()))

    pairs = sorted(((abs((nearest(a).when - a.when).total_seconds()), nearest(a), a) for a in adjusted),
                   key=lambda p: p[0])[:n]
    lo, hi = max(k_start, a_start), min(k_end, a_end)
    period = f"on {lo:%Y-%m-%d}" if lo.date() == hi.date() else f"between {lo:%Y-%m-%d} and {hi:%Y-%m-%d}"
    return ([p[1] for p in pairs], [p[2] for p in pairs],
            f"You used both {period}; comparing the closest sessions.")


def suggest_offset(kept_name: str, kept: list[TopSet], adjusted_name: str, adjusted: list[TopSet],
                   kind: str = LOADED) -> Suggestion:
    """Suggest an offset (and factor) that puts ``adjusted`` on ``kept``'s scale."""
    if not kept or not adjusted:
        return Suggestion(kept_name, adjusted_name, "One of them has no working sets to compare.")
    k_sel, a_sel, relation = _select(kept_name, kept, adjusted_name, adjusted)
    s = Suggestion(kept_name, adjusted_name, relation, k_sel, a_sel)

    if kind == LOADED:
        a_weight = statistics.mean(t.weight for t in a_sel)
        # No weight on either side (bodyweight sets): nothing to scale. Without this check an
        # unweighted kept side suggested an offset that zeroes every adjusted set, and factor 0.
        if a_weight <= 0 or statistics.mean(t.weight for t in k_sel) <= 0:
            return s
        a_reps = statistics.mean(t.reps for t in a_sel)
        k_e1rm = statistics.mean(t.weight * (1 + t.reps / 30) for t in k_sel)
        k_at_a_reps = k_e1rm / (1 + a_reps / 30)  # kept's weight at the adjusted side's rep count
        s.offset = round_to(k_at_a_reps - a_weight)
        s.factor = round(k_at_a_reps / a_weight, 2)
    elif kind == ASSISTED:
        # Weight is assistance; compare it directly (a rep conversion makes no sense here).
        s.offset = round_to(statistics.mean(t.weight for t in k_sel) - statistics.mean(t.weight for t in a_sel))
    return s
