"""Smoke test against the real export checked into the repository root."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fitness_aggregator.importers import parse_strong_csv
from fitness_aggregator.models import SetType

REAL_EXPORT = Path(__file__).resolve().parent.parent / "weights_11_3_26.txt"

pytestmark = pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not present")


@pytest.fixture(scope="module")
def result():
    return parse_strong_csv(REAL_EXPORT)


def test_totals(result):
    s = result.stats
    assert s.rows == 3816
    assert s.workouts == 104
    assert s.sets == 1962
    assert s.exercise_names == 103
    assert s.rest_timers_attached == 1854
    assert s.rest_timers_orphaned == 0
    assert result.warnings == []


def test_set_type_counts(result):
    assert result.stats.set_types == {
        SetType.NORMAL: 1895, SetType.DROP: 50, SetType.WARMUP: 15, SetType.FAILURE: 2,
    }


def test_date_range(result):
    assert result.workouts[0].started_at == datetime(2026, 6, 2, 14, 0, 32)
    assert result.workouts[-1].started_at == datetime(2026, 11, 1, 19, 23, 37)


def test_suspect_durations(result):
    suspect = [(w.started_at.date().isoformat(), w.duration) for w in result.workouts
               if w.duration_suspect()]
    assert suspect == [
        ("2026-08-28", timedelta(hours=14, minutes=7)),
        ("2026-10-22", timedelta(hours=22, minutes=43)),
    ]


def test_raw_values_preserved_including_known_entry_error(result):
    # 2026-06-04 Preacher Curl has weight/reps swapped in the source; the
    # parser keeps it as-is (fixes are applied later as logged corrections).
    w = next(w for w in result.workouts if w.started_at == datetime(2026, 6, 4, 13, 53, 53))
    preacher = next(e for e in w.exercises if e.name == "Preacher Curl (Machine)")
    assert [(s.weight, s.reps) for s in preacher.sets] == [(8.0, 80.0), (8.0, 60.0), (6.0, 50.0)]


def test_rest_timers_land_on_distinct_sets(result):
    with_rest = [s for w in result.workouts for _, s in w.iter_sets() if s.rest_seconds is not None]
    assert len(with_rest) == 1854
    assert {s.rest_seconds for s in with_rest} == {45.0, 60.0, 90.0, 120.0, 270.0}
