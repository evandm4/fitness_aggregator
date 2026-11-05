from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from fitness_aggregator import metrics as m
from fitness_aggregator.bodyweight import Bodyweight
from fitness_aggregator.catalog import Catalog
from fitness_aggregator.importers import parse_strong_csv, parse_strong_text
from fitness_aggregator.sessions import build_sessions
from helpers import REAL_EXPORT, export_text


def frame(text, include_warmups=False, bodyweight=180):
    ws = parse_strong_text(export_text(text)).workouts
    sessions = build_sessions(ws)
    return sessions, m.sets_frame(sessions, Catalog.build(ws), Bodyweight(bodyweight),
                                  include_warmups=include_warmups)


def rows(date, exercise, sets):
    """sets: (order, weight, reps[, seconds])"""
    out = []
    for s in sets:
        order, weight, reps, *rest = s
        seconds = rest[0] if rest else 0
        out.append(f'{date},"W",1h,"{exercise}",{order},{weight},{reps},0,{seconds},"","",\n')
    return "".join(out)


BENCH = (
    rows("2026-06-01 10:00:00", "Bench", [("W", 45, 10), (1, 135, 5), (2, 125, 8), ("D", 95, 10)])
    + rows("2026-06-08 10:00:00", "Bench", [(1, 140, 5), (2, 125, 10), ("F", 140, 3)])
    + rows("2026-06-15 10:00:00", "Bench", [(1, 145, 1), (2, 130, 8)])
)


@pytest.mark.parametrize("load, reps, formula, expected", [
    (100, 1, "epley", 100), (100, 10, "epley", 100 * (1 + 10 / 30)),
    (100, 1, "brzycki", 100), (100, 10, "brzycki", 100 * 36 / 27),
    (100, 13, "epley", np.nan), (100, 0, "epley", np.nan), (0, 5, "epley", np.nan),
])
def test_e1rm(load, reps, formula, expected):
    got = m.e1rm([load], [reps], formula)[0]
    assert (np.isnan(got) and np.isnan(expected)) or got == pytest.approx(expected)


def test_e1rm_rejects_unknown_formula():
    with pytest.raises(ValueError):
        m.e1rm([1], [1], "magic")


def test_sets_frame_excludes_warmups_by_default():
    _, df = frame(BENCH)
    assert "warmup" not in set(df["set_type"]) and len(df) == 8
    _, df = frame(BENCH, include_warmups=True)
    assert len(df) == 9


def test_assisted_load_is_bodyweight_minus_assistance_and_unloaded_has_no_volume():
    text = rows("2026-06-01 10:00:00", "Pull Up (Assisted)", [(1, 60, 8), (2, 0, 3)]) + \
        rows("2026-06-01 10:00:00", "Leg Raise", [(1, 0, 12)])
    _, df = frame(text, bodyweight=175)
    pull = df[df["exercise"] == "Pull Up (Assisted)"]
    assert list(pull["load"]) == [115, 175] and list(pull["bodyweight"]) == [175, 175]
    assert list(pull["volume"]) == [115 * 8, 175 * 3]
    assert df.loc[df["exercise"] == "Leg Raise", "volume"].iloc[0] == 0


def test_exercise_history_top_set_ignores_drop_sets():
    _, df = frame(BENCH)
    h = m.exercise_history(df, "Bench")
    assert list(h["top_load"]) == [135, 140, 145]
    assert list(h["top_reps"]) == [5, 5, 1]
    assert h["e1rm"].iloc[0] == pytest.approx(125 * (1 + 8 / 30))  # 125×8 beats 135×5
    assert list(h["sets"]) == [3, 3, 2]
    assert h["volume"].iloc[0] == 135 * 5 + 125 * 8 + 95 * 10


def test_pr_events_baseline_and_types():
    _, df = frame(BENCH)
    ev = m.pr_events(df)
    assert pd.Timestamp("2026-06-01 10:00") not in set(ev["session"])  # baseline
    week2 = ev[ev["session"] == pd.Timestamp("2026-06-08 10:00")]
    # 140×5 is the heaviest; 140×3 is outdone by 140×5; 125×10 has no earlier 10+ rep set to beat.
    assert list(week2["pr_type"]) == ["e1rm", "weight"]
    assert week2.iloc[0]["detail"] == "125 lb × 10"
    week3 = ev[ev["session"] == pd.Timestamp("2026-06-15 10:00")]
    assert list(week3["pr_type"]) == ["weight", "rep"]  # 145×1 heaviest; 130×8 beats 125 for 8+
    assert (week3.iloc[1]["value"], week3.iloc[1]["previous"]) == (130, 125)


def test_timed_and_unloaded_prs():
    text = (rows("2026-06-01 10:00:00", "Plank", [(1, 0, 0, 60)])
            + rows("2026-06-08 10:00:00", "Plank", [(1, 0, 0, 75)])
            + rows("2026-06-01 10:00:00", "Leg Raise", [(1, 0, 10)])
            + rows("2026-06-08 10:00:00", "Leg Raise", [(1, 0, 12)]))
    _, df = frame(text)
    ev = m.pr_events(df)
    assert sorted(zip(ev["pr_type"], ev["value"], strict=True)) == [("reps", 12), ("seconds", 75)]


def test_rep_maxes():
    _, df = frame(BENCH)
    r = m.rep_maxes(df, "Bench").set_index("reps")
    assert r.loc[1, "load"] == 145 and r.loc[5, "load"] == 140 and r.loc[10, "load"] == 125
    assert 11 not in r.index


def test_current_bests():
    _, df = frame(BENCH)
    b = m.current_bests(df).iloc[0]
    assert b["heaviest_set"] == "145 × 1" and b["sessions"] == 3
    assert b["e1rm_set"] == "125 × 10"


def test_period_stats_fills_empty_weeks():
    text = rows("2026-06-01 10:00:00", "Bench", [(1, 100, 10)]) + rows("2026-06-22 10:00:00", "Bench", [(1, 110, 10)])
    sessions, df = frame(text)
    out = m.period_stats(m.sessions_frame(sessions), df, m.pr_events(df), "week")
    assert list(out["sessions"]) == [1, 0, 0, 1]
    assert list(out["volume"]) == [1000, 0, 0, 1100]
    assert list(out["prs"]) == [0, 0, 0, 2]  # e1RM + heaviest (rep PR not double-counted)


def test_consistency_and_streaks():
    text = "".join(rows(d, "Bench", [(1, 100, 10)]) for d in (
        "2026-06-01 10:00:00", "2026-06-03 10:00:00", "2026-06-09 10:00:00",
        "2026-06-24 10:00:00", "2026-06-30 10:00:00"))
    sessions, _ = frame(text)
    c = m.consistency(m.sessions_frame(sessions), datetime(2026, 7, 2))
    assert c["sessions"] == 5 and c["longest_gap_days"] == 15
    assert c["longest_gap_from"] == pd.Timestamp("2026-06-09 10:00")
    assert c["longest_week_streak"] == 2 and c["current_week_streak"] == 2
    assert c["days_since_last"] == 2
    # A week with no session yet doesn't break the streak; a full empty week does.
    assert m.consistency(m.sessions_frame(sessions), datetime(2026, 7, 13))["current_week_streak"] == 0


def test_name_counts():
    text = rows("2026-06-01 10:00:00", "Bench", [(1, 100, 10)]) + rows("2026-06-15 10:00:00", "Bench", [(1, 100, 10)])
    sessions, _ = frame(text)
    n = m.name_counts(m.sessions_frame(sessions))
    assert list(n["sessions"]) == [2] and n["per_week"].iloc[0] == pytest.approx(1.0)


def test_empty_inputs():
    empty = m.sets_frame([], Catalog(), Bodyweight(180))
    assert list(empty.columns) == m.SET_COLUMNS
    assert m.period_stats(m.sessions_frame([]), empty, m.pr_events(empty)).empty
    assert m.consistency(m.sessions_frame([]), datetime(2026, 1, 1)) == {}


@pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not present")
def test_real_export_metrics_run():
    ws = parse_strong_csv(REAL_EXPORT).workouts
    sessions = build_sessions(ws)
    df = m.sets_frame(sessions, Catalog.build(ws), Bodyweight(180))
    assert len(df) == 1962 - 15  # warm-ups excluded
    squat = m.rep_maxes(df, "Squat (Barbell)").set_index("reps")
    assert squat.loc[1, "load"] == 275 and squat.loc[10, "load"] == 225
