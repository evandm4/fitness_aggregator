"""Combining exercises: one exercise's sets counted under another's name.

An exercise merge is a correction you make::

    type   = "exercise_merge"
    target = {"exercise": "Hip Abductor 2 (Machine)"}
    after  = {"into": "Hip Abductor (Machine)", "offset": -50.0, "factor": None}

With an offset, every set with weight gets ``weight + offset``; with a factor,
``weight × factor``. Sets with no weight (holds, bodyweight reps) are renamed
but not adjusted. The original weight is kept on every adjusted set.

Merges never chain: you can't combine into an exercise that is itself combined
into another, or combine away an exercise others are combined into.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from . import corrections as corr
from .corrections import Correction, PlannedCorrection
from .models import Workout


@dataclass(frozen=True)
class Merge:
    source: str
    into: str
    offset: float | None
    factor: float | None
    correction_id: int | None = None

    @property
    def label(self) -> str:
        return adjustment_label(self.offset, self.factor)

    def adjust(self, weight: float) -> float:
        if weight <= 0:
            return weight
        if self.factor is not None:
            return max(round(weight * self.factor, 2), 0.0)
        if self.offset is not None:
            return max(weight + self.offset, 0.0)
        return weight


def adjustment_label(offset: float | None, factor: float | None) -> str:
    if factor is not None:
        return f"×{factor:g}"
    if offset:
        return f"{offset:+g} lb"
    return ""


def active_merges(corrections) -> dict[str, Merge]:
    """source exercise → Merge, for active exercise-merge corrections."""
    out = {}
    for c in corrections:
        if c.is_active and c.type == corr.EXERCISE_MERGE:
            out[c.target["exercise"]] = Merge(
                c.target["exercise"], c.after["into"], c.after.get("offset"), c.after.get("factor"), c.id
            )
    return out


def canonical(name: str, merges: dict[str, Merge]) -> str:
    return merges[name].into if name in merges else name


def check_merge(source: str, into: str, merges: dict[str, Merge], known: set[str],
                offset: float | None = None, factor: float | None = None) -> None:
    """Raise ValueError if this merge isn't allowed."""
    for name in (source, into):
        if name not in known:
            raise ValueError(f"no exercise named {name!r} in your data")
    if source == into:
        raise ValueError("can't combine an exercise into itself")
    if offset is not None and factor is not None:
        raise ValueError("use an offset or a factor, not both")
    if factor is not None and factor <= 0:
        raise ValueError("factor must be greater than 0")
    if into in merges and merges[into].source != source:
        raise ValueError(f"{into} is already combined into {merges[into].into}; combine into that instead")
    others = sorted(m.source for m in merges.values() if m.into == source)
    if others:
        raise ValueError(
            f"{', '.join(others)} {'is' if len(others) == 1 else 'are'} combined into {source}; "
            f"undo that first, or combine into {source} instead"
        )


def plan_merge(source: str, into: str, offset: float | None = None, factor: float | None = None) -> PlannedCorrection:
    if offset == 0:
        offset = None
    if factor == 1:
        factor = None
    label = adjustment_label(offset, factor)
    how = f"with {label} {'factor' if factor is not None else 'offset'}" if label else "as-is"
    return PlannedCorrection(
        type=corr.EXERCISE_MERGE,
        source=corr.USER,
        target={"exercise": source},
        before={},
        after={"into": into, "offset": offset, "factor": factor},
        reason=f"{source} combined into {into} {how}",
    )


def apply_exercise_merges(workouts: list[Workout], corrections) -> list[Workout]:
    """Rename merged exercises to the kept name and adjust their weights. Raw workouts are untouched."""
    merges = active_merges(corrections)
    if not merges:
        return list(workouts)
    out = []
    for w in workouts:
        if not any(e.name in merges for e in w.exercises):
            out.append(w)
            continue
        w = copy.deepcopy(w)
        for entry in w.exercises:
            m = merges.get(entry.name)
            if m is None:
                continue
            entry.merged_from, entry.name = entry.name, m.into
            entry.merge_label, entry.merge_correction_id = m.label, m.correction_id
            for s in entry.sets:
                new = m.adjust(s.weight)
                if new != s.weight:
                    original = dict(s.corrected_from or {})
                    original.setdefault("weight", s.weight)
                    s.corrected_from = original
                    s.pre_merge_weight = s.weight
                    s.weight = new
                    s.correction_ids = (*s.correction_ids, m.correction_id)
        out.append(w)
    return out


def merges_valid_against(c: Correction, known: set[str]) -> bool:
    """A merge is stale if either exercise no longer exists in the imported data."""
    return c.target["exercise"] in known and c.after["into"] in known
