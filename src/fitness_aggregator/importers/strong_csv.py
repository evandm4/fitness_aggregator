"""Parser for Strong app CSV exports.

Export format (one row per set, plus a row per rest timer)::

    Date,Workout Name,Duration,Exercise Name,Set Order,Weight,Reps,Distance,Seconds,Notes,Workout Notes,RPE

Handling of format quirks:

* A workout is identified by its ``Date`` timestamp (not the calendar day).
* ``Set Order`` is a set number (``1``, ``2``, ...), ``W`` (warm-up),
  ``D`` (drop set), ``F`` (failure), or ``Rest Timer``. A rest timer row is
  not a set: its ``Seconds`` value is attached to the preceding set as
  ``rest_seconds``.
* ``Duration`` is text such as ``"1h 26m"``, ``"47m"`` or ``"1h"``.
* Consecutive rows with the same exercise name form one exercise entry; if an
  exercise reappears later in the workout it becomes a separate entry.

The parser is faithful to the file: it does not drop warm-ups, fix data-entry
errors, or merge anything. It reports counts of what it saw in ``ParseStats``.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from os import PathLike
from typing import IO, Iterable

from ..models import ExerciseEntry, SetType, Workout, WorkoutSet

EXPECTED_COLUMNS = (
    "Date",
    "Workout Name",
    "Duration",
    "Exercise Name",
    "Set Order",
    "Weight",
    "Reps",
    "Distance",
    "Seconds",
    "Notes",
    "Workout Notes",
    "RPE",
)

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
REST_TIMER = "Rest Timer"

_SET_TYPE_CODES = {
    "W": SetType.WARMUP,
    "D": SetType.DROP,
    "F": SetType.FAILURE,
}

_DURATION_TOKEN = re.compile(r"(\d+)\s*([hms])")


class StrongParseError(ValueError):
    """Raised when the file does not match the Strong export format."""

    def __init__(self, message: str, line: int | None = None):
        self.line = line
        super().__init__(f"line {line}: {message}" if line is not None else message)


@dataclass
class ParseStats:
    rows: int = 0
    workouts: int = 0
    sets: int = 0
    set_types: Counter = field(default_factory=Counter)
    rest_timers_attached: int = 0
    rest_timers_orphaned: int = 0
    exercise_names: int = 0


@dataclass
class ParseResult:
    workouts: list[Workout]
    stats: ParseStats
    warnings: list[str] = field(default_factory=list)


def parse_duration(text: str) -> timedelta | None:
    """Parse a Strong duration string such as ``"1h 26m"``; empty means unknown."""
    text = text.strip()
    if not text:
        return None
    tokens = _DURATION_TOKEN.findall(text)
    # Every non-space character must belong to a recognised token.
    if not tokens or _DURATION_TOKEN.sub("", text).strip():
        raise ValueError(f"unrecognised duration {text!r}")
    seconds_per_unit = {"h": 3600, "m": 60, "s": 1}
    return timedelta(seconds=sum(int(n) * seconds_per_unit[u] for n, u in tokens))


def _number(value: str | None, column: str, line: int) -> float:
    value = (value or "").strip()
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        raise StrongParseError(f"{column} is not a number: {value!r}", line) from None


def _optional_number(value: str | None, column: str, line: int) -> float | None:
    if not (value or "").strip():
        return None
    return _number(value, column, line)


def parse_strong_csv(source: str | PathLike | IO[str]) -> ParseResult:
    """Parse a Strong CSV export from a path or an open text stream."""
    if isinstance(source, (str, PathLike)):
        # utf-8-sig tolerates a byte-order mark if the file was re-saved by another tool.
        with open(source, encoding="utf-8-sig", newline="") as fh:
            return _parse(fh)
    return _parse(source)


def parse_strong_text(text: str) -> ParseResult:
    """Parse Strong CSV content held in a string (handy for tests)."""
    return _parse(io.StringIO(text.lstrip("﻿")))


def _parse(fh: Iterable[str]) -> ParseResult:
    reader = csv.DictReader(fh)
    header = tuple(name.strip() for name in (reader.fieldnames or ()))
    missing = [c for c in EXPECTED_COLUMNS if c not in header]
    if missing:
        raise StrongParseError(
            "not a Strong export: missing column(s) " + ", ".join(missing), line=1
        )

    stats = ParseStats()
    warnings: list[str] = []
    workouts: dict[datetime, Workout] = {}
    last_set: WorkoutSet | None = None
    last_set_key: tuple[datetime, str] | None = None

    for row in reader:
        line = reader.line_num
        stats.rows += 1
        row = {k.strip(): (v if v is not None else "") for k, v in row.items() if k}

        try:
            started_at = datetime.strptime(row["Date"].strip(), DATE_FORMAT)
        except ValueError:
            raise StrongParseError(f"bad Date {row['Date']!r}", line) from None
        exercise_name = row["Exercise Name"].strip()
        if not exercise_name:
            raise StrongParseError("empty Exercise Name", line)

        workout = workouts.get(started_at)
        if workout is None:
            try:
                duration = parse_duration(row["Duration"])
            except ValueError as exc:
                raise StrongParseError(str(exc), line) from None
            workout = Workout(
                started_at=started_at,
                name=row["Workout Name"].strip(),
                duration=duration,
            )
            workouts[started_at] = workout
        if not workout.notes and row["Workout Notes"].strip():
            workout.notes = row["Workout Notes"].strip()

        order = row["Set Order"].strip()

        if order == REST_TIMER:
            seconds = _number(row["Seconds"], "Seconds", line)
            if last_set is not None and last_set_key == (started_at, exercise_name):
                last_set.rest_seconds = seconds
                stats.rest_timers_attached += 1
            else:
                stats.rest_timers_orphaned += 1
                warnings.append(
                    f"line {line}: rest timer with no preceding set of "
                    f"{exercise_name!r}; ignored"
                )
            # A rest timer belongs to one set; a second one in a row is orphaned.
            last_set = None
            continue

        if order.isdigit():
            set_type, number = SetType.NORMAL, int(order)
        elif order in _SET_TYPE_CODES:
            set_type, number = _SET_TYPE_CODES[order], None
        else:
            raise StrongParseError(f"unknown Set Order {order!r}", line)

        if not workout.exercises or workout.exercises[-1].name != exercise_name:
            workout.exercises.append(ExerciseEntry(name=exercise_name))
        entry = workout.exercises[-1]

        workout_set = WorkoutSet(
            position=len(entry.sets) + 1,
            set_type=set_type,
            number=number,
            weight=_number(row["Weight"], "Weight", line),
            reps=_number(row["Reps"], "Reps", line),
            distance=_number(row["Distance"], "Distance", line),
            seconds=_number(row["Seconds"], "Seconds", line),
            rpe=_optional_number(row["RPE"], "RPE", line),
            notes=row["Notes"].strip(),
        )
        entry.sets.append(workout_set)
        stats.sets += 1
        stats.set_types[set_type] += 1
        last_set, last_set_key = workout_set, (started_at, exercise_name)

    ordered = sorted(workouts.values(), key=lambda w: w.started_at)
    stats.workouts = len(ordered)
    stats.exercise_names = len({e.name for w in ordered for e in w.exercises})
    return ParseResult(workouts=ordered, stats=stats, warnings=warnings)
