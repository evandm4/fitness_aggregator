"""Best load at each rep count (a strength-endurance curve), now vs. a month ago."""

from __future__ import annotations

from typing import Iterable

import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from .. import metrics
from . import style

COMPARE_DAYS = 30


def rep_pr_curves(sets_df: pd.DataFrame, exercise: str, days: int = COMPARE_DAYS,
                  max_reps: int = metrics.REP_MAX_RANGE) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp | None]:
    """(current rep maxes, rep maxes as of ``days`` before the latest session, that cutoff date)."""
    d = sets_df[sets_df["exercise"] == exercise]
    current = metrics.rep_maxes(d, exercise, max_reps)
    if d.empty:
        return current, current.iloc[0:0], None
    cutoff = pd.Timestamp(d["session"].max()) - pd.Timedelta(days=days)
    previous = metrics.rep_maxes(d[d["session"] <= cutoff], exercise, max_reps)
    return current, previous, cutoff


def draw_rep_prs(ax: Axes, current: pd.DataFrame, previous: pd.DataFrame | None = None, *,
                 cutoff: pd.Timestamp | None = None, max_reps: int = metrics.REP_MAX_RANGE,
                 legend: bool = True) -> dict:
    if current is None or current.empty:
        style.no_data(ax, "No working sets with weight")
        return {"improved": 0}
    c = style.color(0)
    prev = previous if previous is not None and not previous.empty else None
    improved = []
    if prev is not None:
        ax.plot(prev["reps"], prev["load"], color=style.FAINT, lw=2, marker="o", ms=5, mec=style.SURFACE,
                zorder=2, drawstyle="default")
        before = dict(zip(prev["reps"], prev["load"]))
        improved = [r for r, l in zip(current["reps"], current["load"]) if l > before.get(r, float("-inf"))]
    ax.plot(current["reps"], current["load"], color=c, lw=2, zorder=3)
    new = current["reps"].isin(improved)
    ax.scatter(current.loc[~new, "reps"], current.loc[~new, "load"], s=30, color=c, edgecolors=style.SURFACE,
               linewidths=1.2, zorder=4)
    if new.any():
        ax.scatter(current.loc[new, "reps"], current.loc[new, "load"], s=130, marker="*", color=c,
                   edgecolors=style.SURFACE, linewidths=1.0, zorder=5)
    # Label the heaviest single and the highest-rep point only.
    for row in (current.iloc[0], current.iloc[-1]) if len(current) > 1 else (current.iloc[0],):
        ax.annotate(f"{row['load']:g} lb", (row["reps"], row["load"]), xytext=(0, 9), textcoords="offset points",
                    ha="center", fontsize=8.5, color=style.INK_2)
    ax.set_xticks(range(1, max_reps + 1))
    ax.set_xlim(0.4, max_reps + 0.6)
    ax.set_xlabel("Reps (best load lifted for at least this many)")
    ax.set_ylabel("Load (lb)")
    ax.margins(y=0.15)
    ax.tick_params(axis="x", length=0)
    if legend:
        handles = [Line2D([], [], color=c, lw=2, marker="o", ms=5, mec=style.SURFACE)]
        labels = ["Now"]
        if prev is not None:
            handles.append(Line2D([], [], color=style.FAINT, lw=2, marker="o", ms=5, mec=style.SURFACE))
            labels.append(f"As of {cutoff:%b %d}" if cutoff is not None else "Before")
        if improved:
            handles.append(Line2D([], [], ls="", marker="*", ms=11, color=c, mec=style.SURFACE))
            labels.append("Improved since then")
        style.legend_below(ax, handles, labels)
    return {"improved": len(improved) if prev is not None else 0}


@style.use_style
def plot_rep_prs(current: pd.DataFrame, previous: pd.DataFrame | None = None, *, exercise: str = "",
                 cutoff: pd.Timestamp | None = None, title: str | None = None, footnotes: Iterable[str] = (),
                 width: float = 8.5, height: float = 4.6) -> Figure:
    """``current``/``previous`` are ``metrics.rep_maxes`` frames (see ``rep_pr_curves``)."""
    fig, ax = style.new_figure(width, height)
    info = draw_rep_prs(ax, current, previous, cutoff=cutoff)
    sub = None
    if current is not None and not current.empty:
        if previous is None or previous.empty:
            sub = "No sessions before the comparison date"
        elif info["improved"]:
            sub = f"Improved at {info['improved']} rep count{'s' if info['improved'] != 1 else ''} in the last {COMPARE_DAYS} days"
        else:
            sub = f"No rep PRs in the last {COMPARE_DAYS} days"
    style.set_titles(fig, title or (f"Rep maxes: {exercise}" if exercise else "Rep maxes"), sub, ax=ax)
    style.add_footnote(fig, footnotes)
    return fig
