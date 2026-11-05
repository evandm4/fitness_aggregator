"""Value fixes: corrections you make to individual sets (data-entry errors).

A value fix targets one exercise entry in one raw workout::

    target = {"workout": "2026-06-04T13:53:53", "exercise": "Preacher Curl (Machine)", "occurrence": 1}
    before = {"sets": {"1": {"weight": 8.0, "reps": 80.0}, ...}}   # raw values
    after  = {"sets": {"1": {"weight": 80.0, "reps": 8.0}, ...}}

``occurrence`` counts entries of that exercise within the workout (1-based),
so the target survives other exercises being added or removed in Strong.
Positions are set positions within the entry (as listed by ``fitness show``).

All fixes to the same entry live in one correction; fixing it again updates
that correction. If the raw values no longer match ``before`` (you fixed it in
Strong, say), the correction is stale and is not applied.
"""

from __future__ import annotations

import copy

from . import corrections as corr
from .corrections import Correction, PlannedCorrection
from .models import ExerciseEntry, Workout, WorkoutSet
from .sessions import workout_key

VALUE_FIELDS = ("weight", "reps", "seconds")


def entry_target(workout: Workout, exercise: str, occurrence: int) -> dict:
    return {"workout": workout_key(workout.started_at), "exercise": exercise, "occurrence": occurrence}


def find_entry(workout: Workout, exercise: str, occurrence: int) -> ExerciseEntry | None:
    matches = [e for e in workout.exercises if e.name == exercise]
    if 1 <= occurrence <= len(matches):
        return matches[occurrence - 1]
    return None


def _set_at(entry: ExerciseEntry, position: int) -> WorkoutSet | None:
    return next((s for s in entry.sets if s.position == position), None)


def describe_set(s: WorkoutSet, overrides: dict | None = None) -> str:
    values = {f: getattr(s, f) for f in VALUE_FIELDS}
    values.update(overrides or {})
    if values["seconds"] and not values["weight"] and not values["reps"]:
        return f"{values['seconds']:g} s"
    return f"{values['weight']:g} lb × {values['reps']:g}"


def _is_swap(before: dict, after: dict) -> bool:
    return (
        set(before) == {"weight", "reps"} == set(after)
        and before["weight"] == after["reps"]
        and before["reps"] == after["weight"]
    )


def describe_fix(entry: ExerciseEntry, workout: Workout, before: dict, after: dict) -> str:
    when = f"{workout.started_at:%Y-%m-%d %H:%M}"
    positions = sorted(before["sets"], key=int)
    changes = []
    for pos in positions:
        s = _set_at(entry, int(pos))
        changes.append(
            f"set {pos}: {describe_set(s, before['sets'][pos])} → {describe_set(s, after['sets'][pos])}"
        )
    if all(_is_swap(before["sets"][p], after["sets"][p]) for p in positions):
        n = len(positions)
        return f"Swapped weight and reps on {n} {entry.name} set{'s' if n != 1 else ''}, {when} ({'; '.join(changes)})"
    return f"Edited {entry.name}, {when}: " + "; ".join(changes)


def plan_value_fix(
    workout: Workout,
    exercise: str,
    occurrence: int,
    changes: dict[int, dict[str, float]],
    existing: Correction | None = None,
) -> PlannedCorrection:
    """Build (or extend) the value-fix correction for one exercise entry.

    ``changes`` maps set position → {field: new value}. ``existing`` is the
    current correction for the same entry; its changes are kept if it's active.
    """
    entry = find_entry(workout, exercise, occurrence)
    if entry is None:
        raise ValueError(f"{exercise!r} (occurrence {occurrence}) is not in the {workout_key(workout.started_at)} workout")

    # Build on the existing fix only while it's applied; a reverted or stale fix starts over.
    after: dict[str, dict] = {}
    if existing is not None and existing.status == corr.ACTIVE:
        after = copy.deepcopy(existing.after["sets"])
    for position, fields in changes.items():
        s = _set_at(entry, position)
        if s is None:
            raise ValueError(f"{exercise} has no set {position} in that workout (it has {len(entry.sets)})")
        for f, value in fields.items():
            if f not in VALUE_FIELDS:
                raise ValueError(f"can't change {f!r}; only {', '.join(VALUE_FIELDS)}")
            after.setdefault(str(position), {})[f] = float(value)

    before_sets, after_sets = {}, {}
    for pos, fields in after.items():
        s = _set_at(entry, int(pos))
        raw = {f: getattr(s, f) for f in fields}
        changed = {f: v for f, v in fields.items() if v != raw[f]}
        if changed:
            before_sets[pos] = {f: raw[f] for f in changed}
            after_sets[pos] = changed
    if not after_sets:
        raise ValueError("those values are the same as the imported data; nothing to change")

    before, after = {"sets": before_sets}, {"sets": after_sets}
    return PlannedCorrection(
        type=corr.VALUE_FIX,
        source=corr.USER,
        target=entry_target(workout, exercise, occurrence),
        before=before,
        after=after,
        reason=describe_fix(entry, workout, before, after),
    )


def raw_matches(workouts_by_key: dict[str, Workout], c: Correction) -> bool:
    """True if the raw data still has the values this fix was made against."""
    workout = workouts_by_key.get(c.target["workout"])
    if workout is None:
        return False
    entry = find_entry(workout, c.target["exercise"], c.target["occurrence"])
    if entry is None:
        return False
    for pos, fields in c.before["sets"].items():
        s = _set_at(entry, int(pos))
        if s is None or any(getattr(s, f) != v for f, v in fields.items()):
            return False
    return True


def apply_value_fixes(workouts: list[Workout], corrections) -> list[Workout]:
    """Return workouts with active value fixes applied. Untouched workouts are shared, not copied."""
    fixes = [c for c in corrections if c.is_active and c.type == corr.VALUE_FIX]
    if not fixes:
        return list(workouts)
    by_workout: dict[str, list[Correction]] = {}
    for c in fixes:
        by_workout.setdefault(c.target["workout"], []).append(c)

    out = []
    for w in workouts:
        todo = by_workout.get(workout_key(w.started_at))
        if not todo:
            out.append(w)
            continue
        w = copy.deepcopy(w)
        for c in todo:
            entry = find_entry(w, c.target["exercise"], c.target["occurrence"])
            if entry is None:
                continue
            for pos, fields in c.after["sets"].items():
                s = _set_at(entry, int(pos))
                if s is None:
                    continue
                original = dict(s.corrected_from or {})
                for f, value in fields.items():
                    original.setdefault(f, getattr(s, f))
                    setattr(s, f, value)
                s.corrected_from = original
                s.correction_ids = (*s.correction_ids, c.id)
        out.append(w)
    return out


def covered_positions(corrections) -> set[tuple[str, str, int, int]]:
    """(workout, exercise, occurrence, position) of every set changed by an active fix."""
    out = set()
    for c in corrections:
        if c.is_active and c.type == corr.VALUE_FIX:
            t = c.target
            for pos in c.after["sets"]:
                out.add((t["workout"], t["exercise"], t["occurrence"], int(pos)))
    return out


def occurrence_of(workout: Workout, entry: ExerciseEntry) -> int:
    """1-based occurrence number of ``entry`` among same-named entries in ``workout``."""
    n = 0
    for e in workout.exercises:
        if e.name == entry.name:
            n += 1
        if e is entry:
            return n
    raise ValueError("entry is not in workout")


__all__ = [
    "apply_value_fixes",
    "covered_positions",
    "describe_set",
    "entry_target",
    "find_entry",
    "occurrence_of",
    "plan_value_fix",
    "raw_matches",
]
