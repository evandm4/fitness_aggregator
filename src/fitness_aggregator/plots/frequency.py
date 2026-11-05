"""Sessions per exercise, as ranked horizontal bars (from ``metrics.exercise_summary``)."""

from __future__ import annotations

from typing import Iterable

import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.patches import Patch

from . import style

NEGLECTED_DAYS = 30


def _short(name: str, limit: int = 34) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


def draw_frequency(ax: Axes, summary: pd.DataFrame, *, top: int = 20, today=None,
                   neglected_days: int = NEGLECTED_DAYS, legend: bool = True, label_limit: int = 34) -> dict:
    if summary is None or summary.empty:
        style.no_data(ax, "No exercises yet")
        return {"shown": 0, "total": 0, "neglected": 0}
    d = summary.sort_values(["sessions", "last"], ascending=[False, False]).head(top).iloc[::-1]
    today = pd.Timestamp(today) if today is not None else pd.Timestamp.now()
    last = pd.to_datetime(d["last"])
    neglected = (today - last).dt.days > neglected_days
    colors = [style.FAINT if n else style.color(0) for n in neglected]
    y = range(len(d))
    ax.barh(list(y), d["sessions"], height=0.72, color=colors, zorder=3)
    ax.set_yticks(list(y), [_short(n, label_limit) for n in d["exercise"]])
    ax.tick_params(axis="y", length=0, labelsize=8.5, labelcolor=style.INK)
    ax.grid(False)
    ax.grid(axis="x", color=style.GRID, lw=0.7)
    ax.spines["left"].set_visible(False)
    ax.set_xlabel("Sessions")
    ax.set_ylim(-0.6, len(d) - 0.4)
    xmax = d["sessions"].max()
    for yi, v in zip(y, d["sessions"]):
        ax.text(v + xmax * 0.012, yi, f"{v:g}", va="center", ha="left", fontsize=8, color=style.INK_2)
    ax.set_xlim(0, xmax * 1.08)
    if legend and neglected.any():
        style.legend_below(ax, [Patch(color=style.color(0)), Patch(color=style.FAINT)],
                           [f"Done in the last {neglected_days} days", f"Not done in {neglected_days}+ days"])
    return {"shown": len(d), "total": len(summary), "neglected": int(neglected.sum())}


@style.use_style
def plot_frequency(summary: pd.DataFrame, *, top: int = 20, today=None, neglected_days: int = NEGLECTED_DAYS,
                   title: str | None = None, footnotes: Iterable[str] = (), width: float = 8.5) -> Figure:
    """Top ``top`` exercises by number of sessions. Exercises you haven't done in
    ``neglected_days`` are greyed out, to spot neglected lifts."""
    n = 0 if summary is None else min(top, len(summary))
    fig, ax = style.new_figure(width, max(2.8, 1.5 + 0.26 * n))
    info = draw_frequency(ax, summary, top=top, today=today, neglected_days=neglected_days)
    sub = None
    if info["total"]:
        sub = f"Top {info['shown']} of {info['total']} exercises by sessions"
        if info["neglected"]:
            sub += f" · {info['neglected']} not done in the last {neglected_days} days"
    style.set_titles(fig, title or "Exercise frequency", sub, ax=ax)
    style.add_footnote(fig, footnotes)
    return fig
