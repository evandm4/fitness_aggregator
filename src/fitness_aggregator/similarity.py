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


def _base_tokens(name: str) -> tuple[str, ...]:
    """Tokens of the name before any "(equipment)" part."""
    return tokens(name.split("(", 1)[0])


def _same_base_one_unlabelled(a: str, b: str) -> bool:
    """Same movement where one name lists no equipment: "Leg Press" vs "Leg Press 2 (Machine)"."""
    return ("(" in a) != ("(" in b) and bool(_base_tokens(a)) and _base_tokens(a) == _base_tokens(b)


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
        if (ratio >= FUZZY_THRESHOLD and not _contrast(ta, tb)) or _same_base_one_unlabelled(a, b):
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


@dataclass(frozen=True)
class Comparison:
    """One session of the exercise being adjusted, and the kept-exercise session(s) it's compared with.

    ``kept`` holds one session (the same day, or the nearest one) or two (the
    sessions just before and after, whose e1RM is interpolated to this date).
    """

    adjusted: TopSet
    kept: tuple[TopSet, ...]

    def kept_e1rm(self) -> float:
        """The kept exercise's estimated e1RM on the adjusted session's date."""
        if len(self.kept) == 1:
            return _e1rm(self.kept[0])
        before, after = self.kept
        span = (after.when - before.when).total_seconds()
        frac = (self.adjusted.when - before.when).total_seconds() / span if span else 0.0
        return _e1rm(before) + (_e1rm(after) - _e1rm(before)) * frac

    def kept_weight(self) -> float:
        """The kept exercise's weight on that date (interpolated the same way), for assisted lifts."""
        if len(self.kept) == 1:
            return self.kept[0].weight
        before, after = self.kept
        span = (after.when - before.when).total_seconds()
        frac = (self.adjusted.when - before.when).total_seconds() / span if span else 0.0
        return before.weight + (after.weight - before.weight) * frac


def _e1rm(t: TopSet) -> float:
    return t.weight * (1 + t.reps / 30)


def _dated(t: TopSet) -> str:
    return f"{t.when:%Y-%m-%d} {describe_sets([t])}"


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
    comparisons: list[Comparison] = field(default_factory=list)  # set when the two overlapped in time

    @property
    def compared(self) -> list[str]:
        """The exact sessions the suggestion is based on, with dates, one line each."""
        if not self.kept_sets or not self.adjusted_sets:
            return []
        if not self.comparisons:  # a clean switch: the sessions either side of it
            return [f"{self.kept}: {', '.join(_dated(t) for t in self.kept_sets)}",
                    f"{self.adjusted}: {', '.join(_dated(t) for t in self.adjusted_sets)}"]
        lines = []
        for c in self.comparisons:
            if len(c.kept) == 2:
                estimate = c.kept_e1rm() / (1 + c.adjusted.reps / 30)
                ref = (f"{self.kept} {_dated(c.kept[0])} and {_dated(c.kept[1])} "
                       f"(≈ {estimate:.0f} lb × {c.adjusted.reps:g} on {c.adjusted.when:%Y-%m-%d})")
            else:
                ref = f"{self.kept} {_dated(c.kept[0])}"
            lines.append(f"{self.adjusted} {_dated(c.adjusted)} vs {ref}")
        return lines

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


def _times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def _dates(sets: list[TopSet]) -> str:
    if len(sets) <= 3:
        return ", ".join(f"{t.when:%Y-%m-%d}" for t in sets)
    return f"{sets[0].when:%Y-%m-%d} … {sets[-1].when:%Y-%m-%d}"


def _compare_overlapping(kept: list[TopSet], adjusted: list[TopSet]) -> list[Comparison]:
    """Pair each adjusted session with kept sessions: the same day, else the ones either side, else the nearest."""
    out = []
    for a in adjusted:
        same_day = [k for k in kept if k.when.date() == a.when.date()]
        before = [k for k in kept if k.when < a.when and k.when.date() != a.when.date()]
        after = [k for k in kept if k.when > a.when and k.when.date() != a.when.date()]
        if same_day:
            refs = (min(same_day, key=lambda k: abs((k.when - a.when).total_seconds())),)
        elif before and after:
            refs = (before[-1], after[0])
        else:
            refs = (min(kept, key=lambda k: abs((k.when - a.when).total_seconds())),)
        out.append(Comparison(a, refs))
    # Keep the adjusted sessions with the closest kept reference.
    def gap(c: Comparison) -> float:
        return min(abs((k.when - c.adjusted.when).total_seconds()) for k in c.kept)

    chosen = sorted(out, key=gap)[:SESSIONS_COMPARED]
    return sorted(chosen, key=lambda c: c.adjusted.when)


def _select(kept_name: str, kept: list[TopSet], adjusted_name: str, adjusted: list[TopSet]):
    """Pick the sessions to compare and describe how the two exercises were used over time."""
    n = SESSIONS_COMPARED
    k_start, k_end = kept[0].when, kept[-1].when
    a_start, a_end = adjusted[0].when, adjusted[-1].when
    if k_end < a_start:
        return (kept[-n:], adjusted[:n], [],
                f"You switched from {kept_name} to {adjusted_name} on {a_start:%Y-%m-%d} (no overlap).")
    if a_end < k_start:
        return (kept[:n], adjusted[-n:], [],
                f"You switched from {adjusted_name} to {kept_name} on {k_start:%Y-%m-%d} (no overlap).")

    comparisons = _compare_overlapping(kept, adjusted)
    if k_start < a_start and a_end < k_end:
        relation = (f"You used {adjusted_name} {_times(len(adjusted))} ({_dates(adjusted)}), during the period "
                    f"you were using {kept_name} ({k_start:%Y-%m-%d} → {k_end:%Y-%m-%d}).")
    elif a_start < k_start and k_end < a_end:
        relation = (f"You used {kept_name} {_times(len(kept))} ({_dates(kept)}), during the period "
                    f"you were using {adjusted_name} ({a_start:%Y-%m-%d} → {a_end:%Y-%m-%d}).")
    else:
        lo, hi = max(k_start, a_start), min(k_end, a_end)
        period = f"on {lo:%Y-%m-%d}" if lo.date() == hi.date() else f"between {lo:%Y-%m-%d} and {hi:%Y-%m-%d}"
        relation = f"You used both {period}."
    if any(len(c.kept) == 2 for c in comparisons):
        relation += (f" Each {adjusted_name} session is compared with the {kept_name} sessions just before "
                     "and after it, allowing for progress in between.")
    else:
        relation += f" Comparing with the closest {kept_name} sessions."
    kept_used = sorted({k for c in comparisons for k in c.kept}, key=lambda t: t.when)
    return kept_used, [c.adjusted for c in comparisons], comparisons, relation


def suggest_offset(kept_name: str, kept: list[TopSet], adjusted_name: str, adjusted: list[TopSet],
                   kind: str = LOADED) -> Suggestion:
    """Suggest an offset (and factor) that puts ``adjusted`` on ``kept``'s scale."""
    if not kept or not adjusted:
        return Suggestion(kept_name, adjusted_name, "One of them has no working sets to compare.")
    k_sel, a_sel, comparisons, relation = _select(kept_name, kept, adjusted_name, adjusted)
    s = Suggestion(kept_name, adjusted_name, relation, k_sel, a_sel, comparisons=comparisons)

    if kind == LOADED:
        a_weight = statistics.mean(t.weight for t in a_sel)
        # No weight on either side (bodyweight sets): nothing to scale. Without this check an
        # unweighted kept side suggested an offset that zeroes every adjusted set, and factor 0.
        if a_weight <= 0 or statistics.mean(t.weight for t in k_sel) <= 0:
            return s
        if comparisons:
            # Kept exercise's weight at each adjusted session's rep count, estimated for that date.
            k_at = [c.kept_e1rm() / (1 + c.adjusted.reps / 30) for c in comparisons]
            s.offset = round_to(statistics.mean(k - c.adjusted.weight for k, c in zip(k_at, comparisons, strict=True)))
            s.factor = round(statistics.mean(k_at) / a_weight, 2)
        else:
            a_reps = statistics.mean(t.reps for t in a_sel)
            k_e1rm = statistics.mean(_e1rm(t) for t in k_sel)
            k_at_a_reps = k_e1rm / (1 + a_reps / 30)  # kept's weight at the adjusted side's rep count
            s.offset = round_to(k_at_a_reps - a_weight)
            s.factor = round(k_at_a_reps / a_weight, 2)
    elif kind == ASSISTED:
        # Weight is assistance; compare it directly (a rep conversion makes no sense here).
        if comparisons:
            s.offset = round_to(statistics.mean(c.kept_weight() - c.adjusted.weight for c in comparisons))
        else:
            s.offset = round_to(statistics.mean(t.weight for t in k_sel) - statistics.mean(t.weight for t in a_sel))
    return s
