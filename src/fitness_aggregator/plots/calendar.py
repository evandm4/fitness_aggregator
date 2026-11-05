"""GitHub-style heatmap of training days, coloured by that day's volume."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.collections import PatchCollection
from matplotlib.figure import Figure
from matplotlib.patches import FancyBboxPatch

from . import style

LEVELS = [style.BLUES[1], style.BLUES[2], style.BLUES[4], style.BLUES[6]]  # light → dark
MAX_WEEKS = 53
WEEKDAYS = ["Mon", "", "Wed", "", "Fri", "", "Sun"]


def daily_volume(sets_df: pd.DataFrame, sessions_df: pd.DataFrame | None = None) -> pd.Series:
    """Volume per training day. Days trained with zero volume (holds, bodyweight work) are 0, not missing."""
    parts = []
    if sets_df is not None and not sets_df.empty:
        parts.append(sets_df.groupby(pd.to_datetime(sets_df["session"]).dt.normalize())["volume"].sum())
    if sessions_df is not None and not sessions_df.empty:
        days = pd.to_datetime(sessions_df["session"]).dt.normalize().unique()
        parts.append(pd.Series(0.0, index=pd.DatetimeIndex(days)))
    if not parts:
        return pd.Series(dtype=float)
    s = pd.concat(parts)
    return s.groupby(level=0).sum().sort_index()


def _levels(values: np.ndarray) -> tuple[np.ndarray, list[float]]:
    """Bin positive volumes into 4 levels by quartile. Returns level per value (0-3) and the bin edges."""
    pos = values[values > 0]
    if pos.size == 0:
        return np.zeros(len(values), dtype=int), []
    edges = list(np.quantile(pos, [0.25, 0.5, 0.75]))
    return np.searchsorted(edges, values, side="right"), edges


def draw_calendar(ax: Axes, daily: pd.Series, *, end: pd.Timestamp | None = None, weeks: int | None = None,
                  scale_legend: bool = True, month_labels: bool = True) -> dict:
    """Draw the grid on ``ax``. Returns {'days': n training days, 'weeks': n columns}."""
    if daily is None or daily.empty:
        style.no_data(ax, "No training days yet")
        return {"days": 0, "weeks": 0}
    end = pd.Timestamp(end or daily.index.max()).normalize()
    first = daily.index.min().normalize()
    start = first - pd.Timedelta(days=first.weekday())  # Monday of first week
    n_weeks = int((end - start).days // 7) + 1
    cap = weeks or MAX_WEEKS
    if n_weeks > cap:
        start = start + pd.Timedelta(weeks=n_weeks - cap)
        n_weeks = cap
    daily = daily[(daily.index >= start) & (daily.index <= end)]
    levels, edges = _levels(daily.to_numpy(dtype=float))
    level_of = dict(zip(daily.index, levels, strict=True))

    cells, colors = [], []
    pad = 0.12
    for day in pd.date_range(start, end, freq="D"):
        col = (day - start).days // 7
        row = day.weekday()
        trained = day in level_of
        c = LEVELS[level_of[day]] if trained else style.EMPTY
        if trained and daily[day] <= 0:
            c = style.BLUES[0]
        cells.append(FancyBboxPatch((col + pad, row + pad), 1 - 2 * pad, 1 - 2 * pad,
                                    boxstyle="round,pad=0,rounding_size=0.14"))
        colors.append(c)
    ax.add_collection(PatchCollection(cells, facecolors=colors, edgecolors="none", zorder=2))
    ax.set_xlim(-0.1, n_weeks + 0.1)
    ax.set_ylim(7.1, -0.1)
    ax.set_aspect("equal")
    ax.grid(False)
    for side in ("left", "bottom", "top", "right"):
        ax.spines[side].set_visible(False)
    ax.set_yticks(np.arange(7) + 0.5, WEEKDAYS)
    ax.tick_params(axis="y", length=0, labelsize=8, labelcolor=style.MUTED, pad=3)
    ax.tick_params(axis="x", length=0, labelsize=8.5, labelcolor=style.INK_2, pad=2)
    ax.xaxis.set_ticks_position("top")
    if month_labels:
        ticks, labels = [], []
        for m in pd.date_range(start, end, freq="MS"):
            col = (m - start).days // 7
            if m.weekday() != 0:
                col += 1  # label the first full week of the month
            if col < n_weeks and (not ticks or col - ticks[-1] >= 3):
                ticks.append(col)
                labels.append(f"{m:%b}" if m.month != 1 or not labels else f"{m:%b %Y}")
        ax.set_xticks(ticks, labels, ha="left")
    else:
        ax.set_xticks([])
    if scale_legend and edges:
        _scale_legend(ax, edges, n_weeks)
    return {"days": int(len(daily)), "weeks": n_weeks}


def _scale_legend(ax: Axes, edges: list[float], n_weeks: int) -> None:
    """'Less ■■■■ More' key under the grid, with the volume ranges."""
    y = 7.75
    x0 = max(n_weeks - 4.5 - 6.5, 0)
    ax.text(x0 - 0.3, y + 0.42, "Less", ha="right", va="center", fontsize=8, color=style.MUTED)
    cells = []
    for i in range(len(LEVELS)):
        cells.append(FancyBboxPatch((x0 + i + 0.12, y + 0.12), 0.76, 0.76,
                                    boxstyle="round,pad=0,rounding_size=0.14"))
    ax.add_collection(PatchCollection(cells, facecolors=LEVELS, edgecolors="none", clip_on=False))
    ax.text(x0 + len(LEVELS) + 0.3, y + 0.42, "More", ha="left", va="center", fontsize=8, color=style.MUTED)
    rng = f"quartiles of day volume: {style.thousands(edges[0])} · {style.thousands(edges[1])} · " \
          f"{style.thousands(edges[2])} lb"
    ax.text(0, y + 0.42, rng, ha="left", va="center", fontsize=8, color=style.MUTED)
    ax.set_ylim(y + 1.1, -0.1)


@style.use_style
def plot_calendar(sets_df: pd.DataFrame, sessions_df: pd.DataFrame | None = None, *, end=None,
                  weeks: int | None = None, title: str | None = None, footnotes: Iterable[str] = (),
                  width: float | None = None) -> Figure:
    """Training-day heatmap. ``sets_df`` is ``metrics.sets_frame``; ``sessions_df``
    (``metrics.sessions_frame``) adds days with no loaded volume."""
    daily = daily_volume(sets_df, sessions_df)
    n_weeks = 20
    if not daily.empty:
        span = (pd.Timestamp(end or daily.index.max()) - daily.index.min()).days // 7 + 2
        n_weeks = min(max(span, 12), weeks or MAX_WEEKS)
    w = width or min(max(1.3 + n_weeks * 0.265, 6.8), 16)
    fig, ax = style.new_figure(w, 3.4)
    info = draw_calendar(ax, daily, end=end, weeks=weeks)
    sub = None
    if info["days"]:
        sub = f"{info['days']} training days over {info['weeks']} weeks · darker = more volume"
    style.set_titles(fig, title or "Training calendar", sub, ax=ax)
    style.add_footnote(fig, footnotes)
    return fig
