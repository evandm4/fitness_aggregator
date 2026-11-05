from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fitness_aggregator.importers import StrongParseError, parse_strong_csv, parse_strong_text
from fitness_aggregator.importers.strong_csv import parse_duration
from fitness_aggregator.models import SetType

FIXTURES = Path(__file__).parent / "fixtures"
HEADER = "Date,Workout Name,Duration,Exercise Name,Set Order,Weight,Reps,Distance,Seconds,Notes,Workout Notes,RPE\n"


def row(date="2026-06-02 14:00:00", name="W", dur="1h", ex="Curl", order="1",
        weight="20.0", reps="10.0", seconds="0.0", notes="", wnotes="", rpe=""):
    return f'{date},"{name}",{dur},"{ex}",{order},{weight},{reps},0,{seconds},"{notes}","{wnotes}",{rpe}\n'


# --- durations -------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("47m", timedelta(minutes=47)),
    ("1h", timedelta(hours=1)),
    ("1h 26m", timedelta(hours=1, minutes=26)),
    ("22h 43m", timedelta(hours=22, minutes=43)),
    ("1h 2m 3s", timedelta(hours=1, minutes=2, seconds=3)),
    ("45s", timedelta(seconds=45)),
    ("", None),
    ("  ", None),
])
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["abc", "1x", "1h banana", "h"])
def test_parse_duration_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_duration(text)


# --- fixture file ----------------------------------------------------------

@pytest.fixture
def small():
    return parse_strong_csv(FIXTURES / "strong_small.csv")


def test_workouts_keyed_by_timestamp_not_day(small):
    starts = [w.started_at for w in small.workouts]
    assert starts == [
        datetime(2026, 6, 2, 14, 0, 32),
        datetime(2026, 6, 2, 14, 6, 32),
        datetime(2026, 6, 4, 13, 53, 53),
    ]
    assert [w.duration for w in small.workouts] == [
        timedelta(minutes=3), timedelta(minutes=47), timedelta(hours=1, minutes=26)
    ]


def test_set_types_and_numbers(small):
    bench = small.workouts[1].exercises[0]
    assert bench.name == "Bench Press (Barbell)"
    assert [(s.set_type, s.number, s.position) for s in bench.sets] == [
        (SetType.WARMUP, None, 1),
        (SetType.NORMAL, 1, 2),
        (SetType.NORMAL, 2, 3),
        (SetType.DROP, None, 4),
        (SetType.FAILURE, None, 5),
    ]


def test_rest_timers_attach_to_preceding_set(small):
    bench = small.workouts[1].exercises[0]
    assert [s.rest_seconds for s in bench.sets] == [90.0, 120.0, None, None, None]
    assert small.stats.rest_timers_attached == 5
    assert small.stats.rest_timers_orphaned == 0
    assert small.stats.rows == 13
    assert small.stats.sets == 8


def test_values_notes_rpe(small):
    w = small.workouts[1]
    first_working = w.exercises[0].sets[1]
    assert (first_working.weight, first_working.reps) == (135.0, 8.0)
    assert first_working.notes == "Arms tired, limiting"
    assert first_working.rpe == 8.5
    assert w.exercises[0].sets[2].rpe is None
    plank = w.exercises[1].sets[0]
    assert (plank.weight, plank.reps, plank.seconds) == (0.0, 0.0, 60.0)
    assert small.workouts[2].notes == "Felt strong"
    assert small.workouts[0].notes == ""


def test_stats_counts(small):
    assert small.stats.workouts == 3
    assert small.stats.exercise_names == 4
    assert small.stats.set_types[SetType.NORMAL] == 5
    assert small.stats.set_types[SetType.WARMUP] == 1


def test_byte_order_mark_is_tolerated(tmp_path):
    p = tmp_path / "bom.csv"
    p.write_text("﻿" + HEADER + row(), encoding="utf-8")
    assert parse_strong_csv(p).stats.sets == 1
    assert parse_strong_text("﻿" + HEADER + row()).stats.sets == 1


def test_stream_input():
    with open(FIXTURES / "strong_small.csv", newline="") as fh:
        assert parse_strong_csv(fh).stats.workouts == 3


# --- grouping and edge cases ----------------------------------------------

def test_exercise_reappearing_later_is_a_separate_entry():
    text = HEADER + row(ex="Curl") + row(ex="Row") + row(ex="Curl", order="2")
    w = parse_strong_text(text).workouts[0]
    assert [e.name for e in w.exercises] == ["Curl", "Row", "Curl"]
    assert w.exercises[2].sets[0].position == 1


def test_orphan_rest_timers_are_counted_and_warned():
    text = (HEADER
            + row(ex="Curl", order="Rest Timer", seconds="60")   # nothing before it
            + row(ex="Curl")
            + row(ex="Curl", order="Rest Timer", seconds="60")
            + row(ex="Curl", order="Rest Timer", seconds="90")   # second in a row
            + row(ex="Row", order="Rest Timer", seconds="60"))   # different exercise
    r = parse_strong_text(text)
    assert r.stats.rest_timers_attached == 1
    assert r.stats.rest_timers_orphaned == 3
    assert len(r.warnings) == 3
    assert r.workouts[0].exercises[0].sets[0].rest_seconds == 60.0


def test_rest_timer_does_not_cross_workouts():
    text = (HEADER + row(date="2026-06-02 10:00:00")
            + row(date="2026-06-02 12:00:00", order="Rest Timer", seconds="60"))
    assert parse_strong_text(text).stats.rest_timers_orphaned == 1


def test_rows_out_of_order_are_sorted_by_start():
    text = HEADER + row(date="2026-06-05 10:00:00") + row(date="2026-06-01 10:00:00")
    starts = [w.started_at.day for w in parse_strong_text(text).workouts]
    assert starts == [1, 5]


def test_empty_numeric_fields_default_to_zero():
    w = parse_strong_text(HEADER + row(weight="", reps="")).workouts[0]
    s = w.exercises[0].sets[0]
    assert (s.weight, s.reps) == (0.0, 0.0)


def test_empty_duration_is_unknown():
    w = parse_strong_text(HEADER + row(dur="")).workouts[0]
    assert w.duration is None and w.ended_at is None and not w.duration_suspect()


def test_duration_suspect_threshold():
    w = parse_strong_text(HEADER + row(dur="14h 7m")).workouts[0]
    assert w.duration_suspect()
    assert not w.duration_suspect(threshold=timedelta(hours=15))


# --- errors ----------------------------------------------------------------

def test_missing_columns_rejected():
    with pytest.raises(StrongParseError, match="missing column"):
        parse_strong_text("Date,Exercise Name\n2026-06-02 14:00:00,Curl\n")


def test_unknown_set_order_rejected_with_line_number():
    with pytest.raises(StrongParseError, match=r"line 3: unknown Set Order 'X'"):
        parse_strong_text(HEADER + row() + row(order="X"))


def test_bad_number_rejected():
    with pytest.raises(StrongParseError, match="Weight is not a number"):
        parse_strong_text(HEADER + row(weight="heavy"))


def test_bad_date_rejected():
    with pytest.raises(StrongParseError, match="bad Date"):
        parse_strong_text(HEADER + row(date="June 2"))


def test_bad_duration_rejected():
    with pytest.raises(StrongParseError, match="unrecognised duration"):
        parse_strong_text(HEADER + row(dur="forever"))
