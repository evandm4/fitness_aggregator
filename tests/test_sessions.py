from datetime import datetime, timedelta

import pytest

from fitness_aggregator import corrections as corr
from fitness_aggregator.corrections import Correction
from fitness_aggregator.importers import parse_strong_csv, parse_strong_text
from fitness_aggregator.sessions import build_sessions, plan_auto_corrections, typical_duration
from fitness_aggregator.settings import Settings
from helpers import REAL_EXPORT, export_text, workout_rows

NOW = datetime(2026, 11, 5)


def workouts(*rows):
    return parse_strong_text(export_text(*rows)).workouts


def as_active(planned, reverted=()):
    """Turn planned corrections into stored-looking corrections."""
    return [
        Correction(id=i, type=p.type, source=p.source, target=p.target, before=p.before,
                   after=p.after, reason=p.reason,
                   status=corr.REVERTED if p.key in reverted else corr.ACTIVE,
                   created_at=NOW, updated_at=NOW)
        for i, p in enumerate(planned, start=1)
    ]


def sessions_for(ws, settings=None, reverted=frozenset()):
    settings = settings or Settings()
    planned = plan_auto_corrections(ws, settings, reverted)
    return planned, build_sessions(ws, as_active(planned, reverted))


def merges(planned):
    return [(p.target["workout"], p.after["into"]) for p in planned if p.type == corr.WORKOUT_MERGE]


def test_merges_workout_starting_within_gap_of_previous_end():
    ws = workouts(workout_rows("2026-06-02 14:00:00", "3m"), workout_rows("2026-06-02 14:06:00", "47m"))
    planned, sessions = sessions_for(ws)
    assert merges(planned) == [("2026-06-02T14:06:00", "2026-06-02T14:00:00")]
    assert "started 3 min after it ended (threshold 60 min)" in planned[0].reason
    assert len(sessions) == 1
    s = sessions[0]
    assert s.is_merged and s.duration == timedelta(minutes=53)
    assert len(s.exercises) == 2 and s.set_count == 4


def test_does_not_merge_beyond_gap():
    ws = workouts(workout_rows("2026-09-15 16:55:00", "54m"), workout_rows("2026-09-15 19:02:00", "52m"))
    planned, sessions = sessions_for(ws)
    assert merges(planned) == []
    assert len(sessions) == 2


def test_gap_is_configurable():
    ws = workouts(workout_rows("2026-09-15 16:55:00", "54m"), workout_rows("2026-09-15 19:02:00", "52m"))
    planned, sessions = sessions_for(ws, Settings(merge_gap_minutes=90))
    assert len(merges(planned)) == 1 and len(sessions) == 1


def test_boundary_exactly_at_gap_merges():
    ws = workouts(workout_rows("2026-06-02 10:00:00", "1h"), workout_rows("2026-06-02 12:00:00", "1h"))
    assert len(merges(plan_auto_corrections(ws, Settings()))) == 1


def test_overlapping_workout_merges():
    ws = workouts(workout_rows("2026-06-02 10:00:00", "1h"), workout_rows("2026-06-02 10:30:00", "1h"))
    planned, sessions = sessions_for(ws)
    assert "started 30 min before it ended" in planned[0].reason
    assert sessions[0].duration == timedelta(hours=1, minutes=30)


def test_chain_merges_into_first_workout():
    ws = workouts(workout_rows("2026-06-02 10:00:00", "30m"),
                  workout_rows("2026-06-02 10:40:00", "30m"),
                  workout_rows("2026-06-02 11:20:00", "30m"))
    planned, sessions = sessions_for(ws)
    assert [m[1] for m in merges(planned)] == ["2026-06-02T10:00:00"] * 2
    assert len(sessions) == 1 and len(sessions[0].members) == 3


def test_reverted_merge_breaks_chain_and_later_workout_follows_new_session():
    ws = workouts(workout_rows("2026-06-02 10:00:00", "30m"),
                  workout_rows("2026-06-02 10:40:00", "30m"),
                  workout_rows("2026-06-02 11:20:00", "30m"))
    first = plan_auto_corrections(ws, Settings())
    reverted = {first[0].key}  # undo merging the 10:40 workout
    planned, sessions = sessions_for(ws, reverted=reverted)
    assert merges(planned) == [("2026-06-02T10:40:00", "2026-06-02T10:00:00"),
                               ("2026-06-02T11:20:00", "2026-06-02T10:40:00")]
    assert [len(s.members) for s in sessions] == [1, 2]


def test_suspect_duration_flagged_and_excluded():
    ws = workouts(workout_rows("2026-08-28 20:36:24", "14h 7m"))
    planned, sessions = sessions_for(ws)
    assert [p.type for p in planned] == [corr.DURATION_SUSPECT]
    assert planned[0].before == {"duration": "14h 7m"}
    assert sessions[0].duration is None and not sessions[0].duration_trusted


def test_runaway_timer_does_not_merge_with_next_day():
    # The real 2026-08-28 case: the 14h timer "ends" 20 s before the next workout.
    ws = workouts(workout_rows("2026-08-27 18:00:00", "1h 10m"),
                  workout_rows("2026-08-28 20:36:24", "14h 7m"),
                  workout_rows("2026-08-29 10:43:44", "1h 19m"))
    planned, sessions = sessions_for(ws)
    assert merges(planned) == []
    assert len(sessions) == 3


def test_reverting_suspect_flag_trusts_the_recorded_duration():
    ws = workouts(workout_rows("2026-08-28 20:36:24", "14h 7m"),
                  workout_rows("2026-08-29 10:43:44", "1h 19m"))
    suspect = plan_auto_corrections(ws, Settings())[0]
    planned, sessions = sessions_for(ws, reverted={suspect.key})
    assert len(merges(planned)) == 1  # you said the 14h was real, so the next workout is "3 s later"
    assert len(sessions) == 1


def test_estimated_end_is_explained_in_reason():
    ws = workouts(workout_rows("2026-06-01 10:00:00", "1h"),
                  workout_rows("2026-06-02 10:00:00", "9h"),
                  workout_rows("2026-06-02 11:30:00", "1h"))
    planned = plan_auto_corrections(ws, Settings())
    merge = next(p for p in planned if p.type == corr.WORKOUT_MERGE)
    assert "end estimated from your typical duration, 1h" in merge.reason


def test_missing_duration_is_untrusted_but_not_flagged():
    ws = workouts(workout_rows("2026-06-02 10:00:00", ""))
    planned, sessions = sessions_for(ws)
    assert planned == []
    assert sessions[0].duration is None


def test_raw_view_has_no_merges():
    ws = workouts(workout_rows("2026-06-02 14:00:00", "3m"), workout_rows("2026-06-02 14:06:00", "47m"))
    assert len(build_sessions(ws)) == 2


def test_session_name_is_longest_member():
    ws = workouts(workout_rows("2026-06-02 14:00:00", "3m", name="Warmup"),
                  workout_rows("2026-06-02 14:06:00", "47m", name="Legs"))
    assert sessions_for(ws)[1][0].name == "Legs"


@pytest.mark.parametrize("durations, expected", [
    ([], timedelta(hours=1)),
    ([timedelta(minutes=30), timedelta(minutes=90), timedelta(minutes=60)], timedelta(minutes=60)),
])
def test_typical_duration(durations, expected):
    assert typical_duration(durations) == expected


@pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not present")
def test_real_export_sessions():
    ws = parse_strong_csv(REAL_EXPORT).workouts
    planned, sessions = sessions_for(ws)
    assert merges(planned) == [("2026-06-02T14:06:32", "2026-06-02T14:00:32")]
    assert sorted(p.target["workout"] for p in planned if p.type == corr.DURATION_SUSPECT) == [
        "2026-08-28T20:36:24", "2026-10-22T21:39:39"]
    assert len(sessions) == 103
    assert sessions[0].duration == timedelta(minutes=53)
