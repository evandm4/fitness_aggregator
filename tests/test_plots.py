from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from matplotlib.figure import Figure

from fitness_aggregator import metrics, plots
from fitness_aggregator.bodyweight import Bodyweight
from fitness_aggregator.catalog import Catalog
from fitness_aggregator.importers.strong_csv import parse_strong_text
from fitness_aggregator.plots import style
from fitness_aggregator.sessions import build_sessions
from fitness_aggregator.similarity import TopSet
from helpers import export_text, workout_rows


def _texts(fig: Figure) -> str:
    """Every piece of text in a figure: titles, labels, ticks, legends, annotations."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.text import Text

    FigureCanvasAgg(fig).draw()  # also proves the figure renders
    return "\n".join(t.get_text() for t in fig.findobj(Text))


def _frames(*workouts):
    result = parse_strong_text(export_text(*workouts))
    sessions = build_sessions(result.workouts)
    df = metrics.sets_frame(sessions, Catalog(), Bodyweight(180))
    return sessions, df


BENCH = "Bench Press (Barbell)"
SQUAT = "Squat (Barbell)"


@pytest.fixture(scope="module")
def data():
    start = datetime(2026, 6, 1, 10)
    workouts = []
    for i in range(12):
        when = (start + timedelta(days=4 * i)).strftime("%Y-%m-%d %H:%M:%S")
        name = "Push 1" if i % 2 == 0 else "Legs"
        exercises = ((BENCH, ((135 + 5 * i, 5), (125, 8))),) if i % 2 == 0 else ((SQUAT, ((185 + 10 * i, 5),)),)
        workouts.append(workout_rows(when, "1h 5m" if i != 3 else "9h", name, exercises=exercises))
    workouts.append(workout_rows("2026-07-20 10:00:00", "50m", "Pull 1",
                                 exercises=(("Pull Up (Assisted)", ((60, 8), (40, 6))),)))
    sessions, df = _frames(*workouts)
    return sessions, df


def test_style_helpers():
    assert style.color(0) == style.SERIES[0]
    assert style.color(8) == style.OTHER
    assert style.thousands(1500) == "1.5k" and style.thousands(25000) == "25k" and style.thousands(1_700_000) == "1.7M"
    with style.styled():
        fig, ax = style.new_figure()
        style.add_footnote(fig, ["Note one", "Note one", "Note two"])
    text = _texts(fig)
    assert text.count("Note one") == 1 and "Note two" in text


def test_progress_single_and_multi(data):
    _, df = data
    events = metrics.pr_events(df)
    h = metrics.exercise_history(df, BENCH)
    fig = plots.plot_progress({BENCH: h}, events, trend=True, footnotes=["Includes 1 exercise merge"])
    assert isinstance(fig, Figure)
    ax = fig.axes[0]
    assert ax.get_ylabel() == "Estimated 1RM (lb)"
    text = _texts(fig)
    assert f"Estimated 1RM: {BENCH}" in text and "PR" in text and "30-day average" in text
    assert "Includes 1 exercise merge" in text

    hs = {n: metrics.exercise_history(df, n) for n in (BENCH, SQUAT)}
    fig = plots.plot_progress(hs, events, metric="top")
    text = _texts(fig)
    assert BENCH in text and SQUAT in text and fig.axes[0].get_ylabel() == "Top set load (lb)"
    fig = plots.plot_progress(hs, events, split=True)
    assert len(fig.axes) == 2


def test_progress_assistance_axis_is_inverted(data):
    _, df = data
    fig = plots.plot_progress({"Pull Up (Assisted)": metrics.exercise_history(df, "Pull Up (Assisted)")},
                              metric="assistance")
    ax = fig.axes[0]
    assert ax.get_ylabel() == "Assistance (lb, lower is better)"
    assert ax.yaxis_inverted()


def test_progress_marks_merged_and_corrected_sessions(data):
    _, df = data
    h = metrics.exercise_history(df, BENCH).copy()
    h.loc[h.index[-2:], "merged_from"] = "Bench Press 2 (Barbell)"
    h.loc[h.index[0], "corrected"] = True
    text = _texts(plots.plot_progress({BENCH: h}))
    assert "Recorded under a combined exercise" in text and "Includes corrected sets" in text


def test_progress_bad_metric_and_empty():
    with pytest.raises(ValueError):
        plots.plot_progress({"X": pd.DataFrame()}, metric="nope")
    text = _texts(plots.plot_progress({"X": pd.DataFrame()}))
    assert "No sessions with this metric" in text


def test_volume_stacked_and_plain(data):
    sessions, df = data
    fig = plots.plot_volume(df, by="week", group="workout")
    text = _texts(fig)
    assert "Weekly training volume" in text and "Push 1" in text and "Legs" in text
    assert fig.axes[0].get_ylabel() == "Volume (lb)"
    t = plots.volume_table(df, "week", "workout")
    assert t.sum().sum() == pytest.approx(df["volume"].sum())
    stats = metrics.period_stats(metrics.sessions_frame(sessions), df, metrics.pr_events(df), "month")
    text = _texts(plots.plot_volume(stats))
    assert "Monthly training volume" in text


def test_volume_table_folds_extra_groups():
    rows = []
    for i in range(9):
        rows.append({"session": pd.Timestamp("2026-06-01") + pd.Timedelta(days=i), "session_name": f"W{i}",
                     "volume": 100.0 * (i + 1)})
    t = plots.volume_table(pd.DataFrame(rows), "week", "workout")
    assert list(t.columns)[-1] == "Other" and len(t.columns) == 6


def test_calendar(data):
    sessions, df = data
    fig = plots.plot_calendar(df, metrics.sessions_frame(sessions))
    text = _texts(fig)
    assert "Training calendar" in text and "Mon" in text and "Less" in text and "More" in text
    assert "13 training days" in text


def test_duration_excludes_untrusted(data):
    sessions, _ = data
    sf = metrics.sessions_frame(sessions)
    sf.loc[0, "duration_min"] = np.nan
    fig = plots.plot_duration(sf)
    text = _texts(fig)
    assert fig.axes[0].get_ylabel() == "Duration (min)"
    assert "1 session with an untrusted duration" in text


def test_frequency(data):
    _, df = data
    summary = metrics.exercise_summary(df)
    fig = plots.plot_frequency(summary, top=2, today=pd.Timestamp("2026-07-30"))
    text = _texts(fig)
    assert "Top 2 of 3 exercises" in text and BENCH in text
    assert fig.axes[0].get_xlabel() == "Sessions"


def test_rep_prs(data):
    _, df = data
    current, previous, cutoff = plots.rep_pr_curves(df, BENCH)
    assert not current.empty and cutoff is not None
    fig = plots.plot_rep_prs(current, previous, exercise=BENCH, cutoff=cutoff)
    text = _texts(fig)
    assert f"Rep maxes: {BENCH}" in text and "Now" in text and f"As of {cutoff:%b %d}" in text
    assert [t.get_text() for t in fig.axes[0].get_xticklabels()][:3] == ["1", "2", "3"]


def _spm(muscles, weeks=8):
    per = pd.period_range("2026-06-01", periods=weeks, freq="W-SUN")
    return pd.DataFrame([{"period": p, "muscle": m, "sets": float(5 + 3 * i + j % 3)}
                         for j, p in enumerate(per) for i, m in enumerate(muscles)])


def test_sets_per_muscle_overlay_small_multiples_heatmap():
    fig = plots.plot_sets_per_muscle(_spm(["Chest", "Back"]))
    text = _texts(fig)
    assert len(fig.axes) == 1 and "Target 10–20" in text and "Chest" in text
    many = _spm(["Chest", "Back", "Quads", "Hamstrings", "Biceps", "Triceps", "Shoulders"])
    fig = plots.plot_sets_per_muscle(many)
    assert sum(ax.get_visible() for ax in fig.axes) == 7
    fig = plots.plot_sets_per_muscle(many, kind="heatmap", target=(8, 16))
    text = _texts(fig)
    assert "8–16 (target)" in text and "Shoulders" in text
    fig = plots.plot_sets_per_muscle(_spm(["Chest"]), target=None)
    assert "Target" not in _texts(fig)
    with pytest.raises(ValueError):
        plots.plot_sets_per_muscle(_spm(["Chest"]), kind="pie")


def test_merge_preview_offset_and_factor():
    kept = [TopSet(datetime(2026, 8, d), w, 14) for d, w in ((4, 145), (8, 150), (12, 160))]
    other = [TopSet(datetime(2026, 8, d), 205, r) for d, r in ((22, 12), (25, 14))]
    fig = plots.plot_merge_preview(kept, other, offset=-50, kept_name="Hip Abductor (Machine)",
                                   other_name="Hip Abductor 2 (Machine)")
    text = _texts(fig)
    assert "Combine Hip Abductor 2 (Machine) into Hip Abductor (Machine)" in text
    assert "Hip Abductor 2 (Machine) -50 lb" in text and "as recorded" in text
    combined = [l for l in fig.axes[0].get_lines() if l.get_label().startswith("Combined")][0]
    assert list(combined.get_ydata()) == [145, 150, 160, 155, 155]
    text = _texts(plots.plot_merge_preview(kept, other, factor=0.75))
    assert "×0.75" in text
    timed = [TopSet(datetime(2026, 9, 1), 0, 0, 60)]
    fig = plots.plot_merge_preview(timed, [TopSet(datetime(2026, 9, 2), 0, 0, 90)])
    assert fig.axes[0].get_ylabel() == "Top set (s)"


def test_dashboard(data):
    sessions, df = data
    fig = plots.plot_dashboard(df, metrics.sessions_frame(sessions), today=pd.Timestamp("2026-07-30"),
                               footnotes=["Assisted-lift loads use placeholder bodyweight 180 lb"])
    text = _texts(fig)
    for s in ("Training dashboard", "Training days", "Weekly volume", "Session duration", "Most-done exercises",
              BENCH, "sessions per week", "placeholder bodyweight 180 lb"):
        assert s in text, s
    assert len(fig.axes) >= 10


EMPTY_SETS = pd.DataFrame(columns=metrics.SET_COLUMNS)
EMPTY_SESSIONS = pd.DataFrame(columns=["session", "name", "duration_min", "merged"])


@pytest.mark.parametrize("make", [
    lambda: plots.plot_volume(EMPTY_SETS),
    lambda: plots.plot_calendar(EMPTY_SETS, EMPTY_SESSIONS),
    lambda: plots.plot_duration(EMPTY_SESSIONS),
    lambda: plots.plot_frequency(metrics.exercise_summary(EMPTY_SETS)),
    lambda: plots.plot_rep_prs(metrics.rep_maxes(EMPTY_SETS, "X")),
    lambda: plots.plot_sets_per_muscle(pd.DataFrame(columns=["period", "muscle", "sets"])),
    lambda: plots.plot_merge_preview([], []),
    lambda: plots.plot_dashboard(EMPTY_SETS, EMPTY_SESSIONS),
    lambda: plots.plot_progress({"X": metrics.exercise_history(EMPTY_SETS, "X")}),
])
def test_empty_data_does_not_crash(make):
    fig = make()
    assert isinstance(fig, Figure)
    _texts(fig)


def test_single_session_renders(data):
    _, df = data
    one = df[df["session"] == df["session"].min()]
    _texts(plots.plot_progress({BENCH: metrics.exercise_history(one, BENCH)}, trend=True))
    _texts(plots.plot_volume(one))
    _texts(plots.plot_calendar(one))
    _texts(plots.plot_rep_prs(*plots.rep_pr_curves(one, BENCH)[:2]))
