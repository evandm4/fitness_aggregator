"""Charts for the desktop app: metrics DataFrames in, ``matplotlib.figure.Figure`` out.

Kept behind small functions so they can later be switched to the shared
``fitness_aggregator.plots`` package without touching the widgets. Figures
are built with the object API (no pyplot), so nothing here needs Qt.
"""

from __future__ import annotations

import matplotlib.dates as mdates
import pandas as pd
from matplotlib.figure import Figure

from ..catalog import TIMED, UNLOADED

SURFACE = "#ffffff"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
SERIES_1 = "#2a78d6"   # e1RM / main series
SERIES_2 = "#eb6834"   # top set


def _figure(width: float = 7.5, height: float = 3.4) -> tuple[Figure, object]:
    fig = Figure(figsize=(width, height), dpi=100, facecolor=SURFACE, layout="constrained")
    ax = fig.add_subplot(111)
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9, length=0, pad=6)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    return fig, ax


def _dates(ax) -> None:
    locator = mdates.AutoDateLocator(minticks=3, maxticks=8)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))


def _empty(fig: Figure, ax, text: str) -> Figure:
    ax.set_axis_off()
    ax.text(0.5, 0.5, text, ha="center", va="center", color=INK_2, fontsize=11, transform=ax.transAxes)
    return fig


def progress_figure(history: pd.DataFrame, events: pd.DataFrame, exercise: str = "", kind: str = "loaded") -> Figure:
    """e1RM and top set per session (lb), with PRs marked.

    ``history`` is ``metrics.exercise_history`` for one exercise, ``events``
    is ``metrics.pr_events`` for the same exercise.
    """
    fig, ax = _figure()
    if history is None or history.empty:
        return _empty(fig, ax, "No sessions to chart")
    x = pd.to_datetime(history["session"])
    pr_sessions = set(pd.to_datetime(events["session"])) if events is not None and not events.empty else set()
    is_pr = x.isin(pr_sessions)

    if kind in (TIMED, UNLOADED):
        col, label, unit = ("max_seconds", "Best hold", "s") if kind == TIMED else ("max_reps", "Most reps", "reps")
        y = history[col]
        ax.plot(x, y, color=SERIES_1, linewidth=2, marker="o", markersize=4, label=label)
        ax.scatter(x[is_pr], y[is_pr], s=90, marker="*", color=INK, zorder=5, label="PR", linewidths=0)
        ax.set_ylabel(unit, color=INK_2, fontsize=9)
    else:
        e1 = history["e1rm"]
        top = history["top_load"]
        ax.plot(x, e1, color=SERIES_1, linewidth=2, marker="o", markersize=4, label="e1RM")
        ax.plot(x, top, color=SERIES_2, linewidth=2, marker="o", markersize=4, label="Top set load")
        pr_y = e1.where(e1.notna(), top)
        ax.scatter(x[is_pr], pr_y[is_pr], s=110, marker="*", color=INK, zorder=5, label="PR session",
                   edgecolors=SURFACE, linewidths=1)
        ax.set_ylabel("lb", color=INK_2, fontsize=9)
        last = history.iloc[-1]
        if pd.notna(last["e1rm"]):
            ax.annotate(f"{last['e1rm']:,.0f}", (x.iloc[-1], last["e1rm"]), xytext=(6, 0),
                        textcoords="offset points", va="center", color=INK, fontsize=9)
    if exercise:
        ax.set_title(exercise, loc="left", color=INK, fontsize=11, fontweight="bold")
    legend = ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=8.5, frameon=False,
                       labelcolor=INK_2, ncols=3, borderaxespad=0.2)
    legend.set_in_layout(True)
    _dates(ax)
    return fig


def weekly_volume_figure(weekly: pd.DataFrame) -> Figure:
    """Volume per week (lb) from ``metrics.period_stats(..., by="week")``."""
    fig, ax = _figure(height=2.8)
    if weekly is None or weekly.empty:
        return _empty(fig, ax, "No sessions yet")
    x = weekly["period"].map(lambda p: p.start_time)
    ax.bar(x, weekly["volume"] / 1000, width=5.5, color=SERIES_1, linewidth=0)
    ax.set_ylabel("thousand lb", color=INK_2, fontsize=9)
    ax.set_title("Weekly volume", loc="left", color=INK, fontsize=11, fontweight="bold")
    _dates(ax)
    return fig
