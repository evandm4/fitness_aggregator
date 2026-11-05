"""Turn raw workouts into training sessions.

Two automatic rules, both recorded in the corrections log:

* **Suspect durations.** A duration longer than ``suspect_duration_hours`` is
  almost certainly a timer left running. It is excluded from duration stats.
* **Merging nearby workouts.** A workout that starts within
  ``merge_gap_minutes`` of the previous session ending is merged into it.
  When a workout's own duration can't be trusted, its end is estimated as
  start + your median trusted duration, so a runaway timer can't cause a merge
  with the next day's workout.

``plan_auto_corrections`` decides which corrections should exist.
``build_sessions`` applies a list of active corrections to raw workouts. Keeping
them separate means the stored log, not the rules, is what the views apply.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .corrections import (
    DURATION_SUSPECT,
    REVERTED,
    WORKOUT_MERGE,
    Correction,
    CorrectionKey,
    PlannedCorrection,
    format_duration,
    make_key,
)
from .models import ExerciseEntry, Workout
from .settings import Settings

FALLBACK_TYPICAL_DURATION = timedelta(hours=1)


def workout_key(started_at: datetime) -> str:
    return started_at.isoformat(timespec="seconds")


@dataclass
class Session:
    """One training session: one workout, or several merged together."""

    members: list[Workout]
    duration: timedelta | None
    duration_trusted: bool
    applied: list[Correction] = field(default_factory=list)

    @property
    def started_at(self) -> datetime:
        return self.members[0].started_at

    @property
    def key(self) -> str:
        return workout_key(self.started_at)

    @property
    def name(self) -> str:
        # The name of the longest member (by trusted duration, then by sets).
        def weight(w: Workout):
            return (w.duration or timedelta(), w.set_count)

        return max(self.members, key=weight).name

    @property
    def notes(self) -> str:
        return "; ".join(w.notes for w in self.members if w.notes)

    @property
    def exercises(self) -> list[ExerciseEntry]:
        return [e for w in self.members for e in w.exercises]

    @property
    def is_merged(self) -> bool:
        return len(self.members) > 1

    @property
    def set_count(self) -> int:
        return sum(w.set_count for w in self.members)


def typical_duration(durations: Iterable[timedelta]) -> timedelta:
    durations = list(durations)
    if not durations:
        return FALLBACK_TYPICAL_DURATION
    seconds = statistics.median(d.total_seconds() for d in durations)
    return timedelta(seconds=round(seconds))


def _minutes(td: timedelta) -> str:
    minutes = round(abs(td.total_seconds()) / 60)
    return f"{minutes} min"


def plan_auto_corrections(
    workouts: list[Workout],
    settings: Settings,
    reverted: set[CorrectionKey] = frozenset(),
) -> list[PlannedCorrection]:
    """Work out which automatic corrections should exist for these workouts.

    ``reverted`` holds keys of corrections you have undone. They are still
    planned (so the log keeps them), but treated as not applied when deciding
    later merges.
    """
    workouts = sorted(workouts, key=lambda w: w.started_at)
    planned: list[PlannedCorrection] = []

    # 1. Suspect durations.
    untrusted: set[datetime] = set()
    threshold = settings.suspect_duration
    for w in workouts:
        if w.duration is None:
            untrusted.add(w.started_at)
        elif w.duration_suspect(threshold):
            p = PlannedCorrection(
                type=DURATION_SUSPECT,
                target={"workout": workout_key(w.started_at)},
                before={"duration": format_duration(w.duration)},
                after={"duration": None},
                reason=(
                    f"Duration {format_duration(w.duration)} excluded from duration stats: "
                    f"over {settings.suspect_duration_hours:g}h, probably a timer left running"
                ),
            )
            planned.append(p)
            if p.key not in reverted:
                untrusted.add(w.started_at)

    typical = typical_duration(
        w.duration for w in workouts if w.started_at not in untrusted and w.duration is not None
    )

    def end_of(w: Workout) -> tuple[datetime, bool]:
        if w.started_at in untrusted:
            return w.started_at + typical, True
        return w.started_at + w.duration, False

    # 2. Merge workouts that start soon after the current session ends.
    head: Workout | None = None
    session_end: datetime | None = None
    end_estimated = False
    for w in workouts:
        if head is not None:
            gap = w.started_at - session_end
            if gap <= settings.merge_gap:
                if gap >= timedelta():
                    timing = f"started {_minutes(gap)} after it ended"
                else:
                    timing = f"started {_minutes(gap)} before it ended"
                if end_estimated:
                    timing += f" (end estimated from your typical duration, {format_duration(typical)})"
                p = PlannedCorrection(
                    type=WORKOUT_MERGE,
                    target={"workout": workout_key(w.started_at)},
                    before={},
                    after={"into": workout_key(head.started_at)},
                    reason=(
                        f"Merged {w.started_at:%Y-%m-%d %H:%M} workout into "
                        f"{head.started_at:%Y-%m-%d %H:%M} workout: {timing} "
                        f"(threshold {settings.merge_gap_minutes:g} min)"
                    ),
                )
                planned.append(p)
                if p.key not in reverted:
                    end, estimated = end_of(w)
                    if end > session_end:
                        session_end, end_estimated = end, estimated
                    continue
        head = w
        session_end, end_estimated = end_of(w)

    return planned


def build_sessions(workouts: list[Workout], corrections: list[Correction] = ()) -> list[Session]:
    """Group workouts into sessions using active merge and duration corrections.

    Value fixes must already be applied to ``workouts`` (see ``fixes.apply_value_fixes``);
    they are only attached to ``Session.applied`` here. With no corrections, this
    is the raw view.
    """
    active = [c for c in corrections if c.is_active]
    merges = {c.workout: c for c in active if c.type == WORKOUT_MERGE}
    suspect = {c.workout: c for c in active if c.type == DURATION_SUSPECT}
    other: dict[str, list[Correction]] = {}
    for c in active:
        if c.type not in (WORKOUT_MERGE, DURATION_SUSPECT) and c.workout:
            other.setdefault(c.workout, []).append(c)

    groups: list[tuple[list[Workout], list[Correction]]] = []
    for w in sorted(workouts, key=lambda w: w.started_at):
        key = workout_key(w.started_at)
        applied = ([suspect[key]] if key in suspect else []) + other.get(key, [])
        if key in merges and groups:
            groups[-1][0].append(w)
            groups[-1][1].extend([merges[key], *applied])
        else:
            groups.append(([w], applied))

    sessions = []
    for members, applied in groups:
        trusted = all(
            m.duration is not None and workout_key(m.started_at) not in suspect for m in members
        )
        if not trusted:
            duration = None
        elif len(members) == 1:
            duration = members[0].duration
        else:
            last_end = max(m.ended_at for m in members)
            duration = last_end - members[0].started_at
        sessions.append(Session(members=members, duration=duration, duration_trusted=trusted, applied=applied))
    return sessions


def reverted_keys(corrections: Iterable[Correction]) -> set[CorrectionKey]:
    return {make_key(c.type, c.target) for c in corrections if c.status == REVERTED}
