"""Charts for the desktop app: a thin adapter over ``fitness_aggregator.plots``.

The GUI draws the same figures as ``fitness plot …``, with the same
footnotes (``cli_plots.footnotes``), so a chart saved from the window matches
the CLI's. Inputs come from the controller's ``Snapshot``. Nothing here needs
Qt: figures are built with the object API (no pyplot).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from matplotlib.figure import Figure

from .. import metrics, plots
from ..catalog import LOADED
from ..cli_plots import FORMATS, footnotes, save_figure, slug
from ..plots import style


@dataclass(frozen=True)
class ChartSpec:
    key: str
    label: str
    needs_exercise: bool = False
    min_scale: float = 0.5  # the canvas never shrinks below this fraction of the figure's natural size
    scale: bool = False  # draw at natural size and scale the picture (layouts that only work at that size)


CHARTS = [
    ChartSpec("progress", "Progress (selected exercise)", needs_exercise=True),
    ChartSpec("volume-week", "Weekly volume"),
    ChartSpec("volume-month", "Monthly volume"),
    ChartSpec("calendar", "Training calendar", min_scale=0.6),
    ChartSpec("duration", "Session duration"),
    ChartSpec("frequency", "Exercise frequency"),
    ChartSpec("rep-prs", "Rep PRs (selected exercise)", needs_exercise=True),
    ChartSpec("muscles", "Sets per muscle per week", min_scale=0.7),
    ChartSpec("dashboard", "Dashboard", min_scale=0.6, scale=True),
]
SPECS = {c.key: c for c in CHARTS}


# --- the charts the tabs embed ------------------------------------------------------

def progress_figure(history: pd.DataFrame, events: pd.DataFrame, exercise: str = "", kind: str = LOADED,
                    *, metric: str | None = None, notes=(), width: float = 9, height: float = 4.6) -> Figure:
    """``plots.plot_progress`` for one exercise; the metric defaults to the one that suits its kind."""
    metric = metric or plots.default_metric(kind)
    return plots.plot_progress({exercise or "Exercise": history if history is not None else pd.DataFrame()},
                               events if events is not None and not events.empty else None,
                               metric=metric, footnotes=notes, width=width, height=height)


def weekly_volume_figure(weekly: pd.DataFrame, *, height: float = 4.2) -> Figure:
    """``plots.plot_volume`` over ``metrics.period_stats(..., "week")`` totals."""
    if weekly is None or weekly.empty:
        fig, ax = style.new_figure(9, height)
        style.no_data(ax, "No sessions yet")
        style.set_titles(fig, "Weekly training volume")
        return fig
    return plots.plot_volume(weekly, height=height)


def muscles_figure(muscle_sets: pd.DataFrame, kind: str = "heatmap", notes=()) -> Figure:
    return plots.plot_sets_per_muscle(muscle_sets, kind=kind, footnotes=notes)


@style.use_style
def bodyweight_figure(days: list) -> Figure:
    """Daily bodyweight (``bodyweight.DayValue``s) with a 7-day average. Uses the shared style."""
    fig, ax = style.new_figure(9, 3.6)
    if not days:
        style.no_data(ax, "No bodyweight entries yet")
        style.set_titles(fig, "Bodyweight")
        return fig
    s = pd.Series([d.lb for d in days], index=pd.to_datetime([d.day for d in days])).sort_index()
    ax.plot(s.index, s.values, color=style.FAINT, lw=1.2, marker="o", markersize=2.5, zorder=3)
    if len(s) >= 3:
        avg = s.rolling("7D", min_periods=1).mean()
        ax.plot(avg.index, avg.values, color=style.color(0), lw=2.2, zorder=4, label="7-day average")
        style.legend_below(ax)
    style.date_axis(ax)
    ax.set_ylabel("lb")
    ax.margins(x=0.03, y=0.15)
    style.set_titles(fig, "Bodyweight", f"{len(s)} days · {s.index.min():%b %d, %Y} – {s.index.max():%b %d, %Y}",
                     ax=ax)
    return fig


# --- the Charts tab ----------------------------------------------------------------------

def build(key: str, snap, *, exercise: str | None = None, metric: str | None = None, today=None,
          formula: str = "epley") -> Figure:
    """Build chart ``key`` (see ``CHARTS``) from a controller ``Snapshot``, as ``fitness plot`` would."""
    spec = SPECS[key]
    df = snap.df
    if df.empty:
        fig, ax = style.new_figure(9, 4.2)
        style.no_data(ax, "No workouts yet: import a Strong export first")
        style.set_titles(fig, spec.label)
        return fig
    if spec.needs_exercise and not exercise:
        fig, ax = style.new_figure(9, 4.2)
        style.no_data(ax, "Pick an exercise")
        style.set_titles(fig, spec.label)
        return fig
    if key == "progress":
        ex = df[df["exercise"] == exercise]
        kind = ex["kind"].iloc[0] if not ex.empty else LOADED
        metric = metric or plots.default_metric(kind)
        events = snap.events[snap.events["exercise"] == exercise] if not snap.events.empty else None
        hist = metrics.exercise_history(ex, exercise, formula)
        return plots.plot_progress({exercise: hist}, events, metric=metric,
                                   footnotes=footnotes(snap, ex, loads=metric not in ("reps", "seconds", "assistance")))
    if key in ("volume-week", "volume-month"):
        return plots.plot_volume(df, by=key.split("-")[1], footnotes=footnotes(snap, df, sessions=True))
    if key == "calendar":
        return plots.plot_calendar(df, snap.sessions_df, footnotes=footnotes(snap, df, sessions=True))
    if key == "duration":
        notes = [n for n in footnotes(snap, df, sessions=True, loads=False)
                 if "corrected set" not in n and "exercise merge" not in n]
        return plots.plot_duration(snap.sessions_df, footnotes=notes)
    if key == "frequency":
        notes = [n for n in footnotes(snap, df, sessions=True, loads=False) if "corrected set" not in n]
        return plots.plot_frequency(metrics.exercise_summary(df), today=today, footnotes=notes)
    if key == "rep-prs":
        current, previous, cutoff = plots.rep_pr_curves(df, exercise)
        return plots.plot_rep_prs(current, previous, exercise=exercise, cutoff=cutoff,
                                  footnotes=footnotes(snap, df[df["exercise"] == exercise]))
    if key == "muscles":
        return muscles_figure(snap.muscle_sets, kind="lines")
    if key == "dashboard":
        return plots.plot_dashboard(df, snap.sessions_df, snap.events, formula=formula, today=today,
                                    footnotes=footnotes(snap, df, sessions=True))
    raise KeyError(key)


def default_filename(key: str, exercise: str | None = None, today=None) -> str:
    """Like the CLI's ``plots/<name>_<date>.png``, without the folder."""
    name = {"volume-week": "volume-week", "volume-month": "volume-month"}.get(key, key)
    if SPECS[key].needs_exercise and exercise:
        name = f"{key}_{slug(exercise)}"
    day = pd.Timestamp(today or pd.Timestamp.now())
    return f"{name}_{day:%Y-%m-%d}.png"


def save(fig: Figure, path: str | Path) -> Path:
    """Save as PNG, SVG or PDF (by extension; none means PNG). Raises ValueError for other extensions."""
    path = Path(path)
    if not path.suffix:
        path = path.with_suffix(".png")
    if path.suffix.lower() not in FORMATS:
        raise ValueError(f"save as {', '.join(FORMATS)}; got {path.name}")
    return save_figure(fig, path)


# --- keeping figures tidy when the widget resizes them ------------------------------------

def keep_footnotes_fitted(fig: Figure) -> None:
    """``style.add_footnote`` reserves a fixed fraction of the figure's natural height. When an
    embedded canvas resizes the figure, keep the same room in inches instead, so notes never
    overlap the tick labels. Call after the figure has a canvas."""
    engine = fig.get_layout_engine()
    params = engine.get() if engine is not None and hasattr(engine, "get") else {}
    rect = params.get("rect")
    if not rect or not rect[1] or fig.canvas is None:
        return
    reserved_in = rect[1] * fig.get_figheight()
    notes = [t for t in fig.texts if t.get_position()[0] == 0.012 and t.get_va() == "bottom"]

    def fit(_event=None):
        h = fig.get_figheight()
        frac = min(reserved_in / h, 0.4)
        engine.set(rect=(0, frac, 1, 1 - frac))
        for t in notes:
            t.set_y(0.06 / h)

    fig.canvas.mpl_connect("resize_event", fit)
    fit()
