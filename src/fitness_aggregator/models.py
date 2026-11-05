"""Core data model: workouts, the exercises performed in them, and their sets.

These are plain dataclasses that mirror the imported data faithfully. Nothing
here applies corrections or interpretation (assisted lifts, merges, offsets);
that happens in later layers so the raw data can always be recovered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class SetType(Enum):
    NORMAL = "normal"
    WARMUP = "warmup"
    DROP = "drop"
    FAILURE = "failure"


@dataclass
class WorkoutSet:
    """One performed set.

    ``position`` is the 1-based order of the set within its exercise entry,
    counting every set type. It is stable across re-imports of the same data
    and is used as part of the key that corrections refer to.

    ``number`` is the set number as exported (1, 2, 3, ...) and is only present
    for normal sets; warm-up, drop, and failure sets have none.
    """

    position: int
    set_type: SetType
    number: int | None
    weight: float
    reps: float
    distance: float = 0.0
    seconds: float = 0.0
    rpe: float | None = None
    notes: str = ""
    rest_seconds: float | None = None
    # Set only on views with corrections applied: the original values of any
    # changed fields, and the ids of the corrections that changed them.
    corrected_from: dict | None = field(default=None, compare=False)
    correction_ids: tuple[int, ...] = field(default=(), compare=False)
    # The weight before an exercise-merge offset or factor was applied.
    pre_merge_weight: float | None = field(default=None, compare=False)

    @property
    def volume(self) -> float:
        """Raw weight x reps, before any corrections or offsets."""
        return self.weight * self.reps


@dataclass
class ExerciseEntry:
    """A run of sets of one exercise within a workout, in performed order."""

    name: str
    sets: list[WorkoutSet] = field(default_factory=list)
    # Set only on views where this entry was combined into another exercise:
    # the name it was recorded under, how weights were adjusted, and the correction id.
    merged_from: str | None = field(default=None, compare=False)
    merge_label: str = field(default="", compare=False)
    merge_correction_id: int | None = field(default=None, compare=False)


@dataclass
class Workout:
    """One workout as exported, identified by its start timestamp."""

    started_at: datetime
    name: str
    duration: timedelta | None
    notes: str = ""
    exercises: list[ExerciseEntry] = field(default_factory=list)

    @property
    def ended_at(self) -> datetime | None:
        if self.duration is None:
            return None
        return self.started_at + self.duration

    def duration_suspect(self, threshold: timedelta = timedelta(hours=4)) -> bool:
        """True when the recorded duration is implausibly long (a timer left running)."""
        return self.duration is not None and self.duration > threshold

    def iter_sets(self):
        """Yield ``(exercise_entry, workout_set)`` pairs in performed order."""
        for entry in self.exercises:
            for s in entry.sets:
                yield entry, s

    @property
    def set_count(self) -> int:
        return sum(len(e.sets) for e in self.exercises)
