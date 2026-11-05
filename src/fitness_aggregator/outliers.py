"""Detect likely data-entry errors for review with ``fitness check``.

Flags never change data. Each one is either fixed (a value-fix correction) or
dismissed by you; both stop it being raised again.

Rules (tuned against real data so ordinary progress isn't flagged):

* ``high_reps``     reps over 30 on a weighted set. Usually weight and reps
                    swapped; swap is suggested when the swapped weight is in
                    line with your usual weights for that exercise.
* ``weight_spike``  a working set more than 2.5x your median top set for that
                    exercise in other sessions (needs 3+ other sessions).
                    Only the high side is checked: low weights are usually
                    early progress, deloads, or back-off sets.
* ``zero_reps``     weight but no reps on a normal or drop set. Failure sets
                    are exempt, since 0 reps there means a missed attempt.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from .catalog import ASSISTED, LOADED, Catalog
from .fixes import occurrence_of
from .models import ExerciseEntry, SetType, Workout, WorkoutSet
from .sessions import workout_key

HIGH_REPS = 30
SPIKE_FACTOR = 2.5
MIN_OTHER_SESSIONS = 3
SWAP_RANGE = (0.4, 2.5)

WORKING = (SetType.NORMAL, SetType.FAILURE)


@dataclass
class Flag:
    rule: str
    workout: Workout
    entry: ExerciseEntry
    occurrence: int
    set: WorkoutSet
    message: str
    swap_plausible: bool = False

    @property
    def key(self) -> str:
        return "|".join(
            [self.rule, workout_key(self.workout.started_at), self.entry.name,
             str(self.occurrence), str(self.set.position)]
        )

    @property
    def location(self) -> tuple[str, str, int, int]:
        return (workout_key(self.workout.started_at), self.entry.name, self.occurrence, self.set.position)


@dataclass
class FlagGroup:
    """Flags on one exercise entry, reviewed together."""

    workout: Workout
    entry: ExerciseEntry
    occurrence: int
    flags: list[Flag] = field(default_factory=list)

    @property
    def swap_plausible(self) -> bool:
        return all(f.swap_plausible for f in self.flags)


def _top_loads(workouts: list[Workout]) -> dict[str, dict[str, float]]:
    """exercise → {workout key → heaviest working-set weight}."""
    tops: dict[str, dict[str, float]] = defaultdict(dict)
    for w in workouts:
        key = workout_key(w.started_at)
        for e in w.exercises:
            weights = [s.weight for s in e.sets if s.set_type in WORKING and s.weight > 0]
            if weights:
                tops[e.name][key] = max(tops[e.name].get(key, 0), max(weights))
    return tops


def detect(workouts: list[Workout], catalog: Catalog) -> list[Flag]:
    tops = _top_loads(workouts)
    flags: list[Flag] = []
    for w in workouts:
        wkey = workout_key(w.started_at)
        for e in w.exercises:
            if catalog.kind(e.name) not in (LOADED, ASSISTED):
                continue
            others = [v for k, v in tops.get(e.name, {}).items() if k != wkey]
            median = statistics.median(others) if others else None
            occurrence = occurrence_of(w, e)
            for s in e.sets:
                if s.set_type is SetType.WARMUP:
                    continue
                if s.weight > 0 and s.reps > HIGH_REPS:
                    plausible = s.weight <= HIGH_REPS and (
                        median is None or SWAP_RANGE[0] * median <= s.reps <= SWAP_RANGE[1] * median
                    )
                    usual = f"; your usual top set is about {median:g} lb" if median else ""
                    flags.append(Flag(
                        "high_reps", w, e, occurrence, s,
                        f"{s.weight:g} lb × {s.reps:g}: over {HIGH_REPS} reps on a weighted set{usual}",
                        swap_plausible=plausible,
                    ))
                elif (s.set_type in WORKING and median is not None and len(others) >= MIN_OTHER_SESSIONS
                      and s.weight > SPIKE_FACTOR * median):
                    flags.append(Flag(
                        "weight_spike", w, e, occurrence, s,
                        f"{s.weight:g} lb × {s.reps:g}: more than {SPIKE_FACTOR:g}× your usual top set ({median:g} lb)",
                    ))
                elif s.set_type in (SetType.NORMAL, SetType.DROP) and s.weight > 0 and s.reps == 0 and not s.seconds:
                    flags.append(Flag(
                        "zero_reps", w, e, occurrence, s, f"{s.weight:g} lb with no reps recorded",
                    ))
    return flags


def group(flags: list[Flag]) -> list[FlagGroup]:
    groups: dict[tuple, FlagGroup] = {}
    for f in flags:
        k = (f.workout.started_at, f.entry.name, f.occurrence)
        if k not in groups:
            groups[k] = FlagGroup(f.workout, f.entry, f.occurrence)
        groups[k].flags.append(f)
    return list(groups.values())
