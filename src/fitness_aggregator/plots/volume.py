"""Training volume per week or month, optionally stacked by workout name."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure

from . import style

FREQ = {"week": "W-SUN", "month": "M"}
MAX_GROUPS = 6  # past this, the smallest groups fold into "Other"


def volume_table(sets_df: pd.DataFrame, by: str = "week", group: str | None = "workout") -> pd.DataFrame:
    """Period × group volume (lb), every period between the first and last set included.

    ``group`` is ``"workout"`` (session name) or ``None``. Columns are ordered
    largest group first, with an ``Other`` column for anything past MAX_GROUPS.
    """
    if by not in FREQ:
        raise ValueError("by must be 'week' or 'month'")
    if sets_df is None or sets_df.empty:
        return pd.DataFrame()
    d = sets_df.assign(period=pd.to_datetime(sets_df["session"]).dt.to_period(FREQ[by]))
    key = "session_name" if group == "workout" else None
    if key:
        t = d.pivot_table(index="period", columns=key, values="volume", aggfunc="sum", fill_value=0.0)
        order = t.sum().sort_values(ascending=False).index.tolist()
        t = t[order]
        if len(order) > MAX_GROUPS:
            keep = order[:MAX_GROUPS - 1]
            t = t[keep].assign(Other=t[order[MAX_GROUPS - 1:]].sum(axis=1))
    else:
        t = d.groupby("period")["volume"].sum().to_frame("Volume")
    periods = pd.period_range(t.index.min(), t.index.max(), freq=t.index.freq)
    return t.reindex(periods, fill_value=0.0)


def _from_period_stats(stats: pd.DataFrame) -> pd.DataFrame:
    t = stats.set_index("period")[["volume"]].rename(columns={"volume": "Volume"})
    return t


def draw_volume(ax: Axes, table: pd.DataFrame, by: str = "week", *, legend: bool = True,
                colors: dict | None = None) -> None:
    if table is None or table.empty or not np.nansum(table.to_numpy()):
        style.no_data(ax, "No volume to show")
        return
    starts = table.index.to_timestamp(how="start")
    days = 7 if by == "week" else 30.4
    width = pd.Timedelta(days=days * 0.78)
    x = starts + pd.Timedelta(days=days * 0.11)  # centre-ish inside the period, aligned to its start
    bottom = np.zeros(len(table))
    columns = list(table.columns)
    for i, col in enumerate(columns):
        vals = table[col].to_numpy(dtype=float)
        c = (colors or {}).get(col) or (style.OTHER if col == "Other" else style.color(i))
        ax.bar(x, vals, width=width, bottom=bottom, align="edge", color=c,
               edgecolor=style.SURFACE, linewidth=0.8 if len(columns) > 1 else 0,
               label=col if len(columns) > 1 else None, zorder=3)
        bottom += vals
    style.date_axis(ax)
    style.thousands_axis(ax)
    ax.set_ylabel("Volume (lb)")
    ax.set_xlim(starts.min() - pd.Timedelta(days=days * 0.3), starts.max() + pd.Timedelta(days=days * 1.2))
    ax.margins(y=0.08)
    ax.set_ylim(bottom=0)
    if legend and len(columns) > 1:
        style.legend_below(ax, ncols=min(len(columns), 6))


@style.use_style
def plot_volume(data: pd.DataFrame, *, by: str = "week", group: str | None = "workout",
                title: str | None = None, footnotes: Iterable[str] = (), width: float = 9,
                height: float = 4.6) -> Figure:
    """Weekly or monthly volume bars.

    ``data`` is either ``metrics.sets_frame`` output (needed for stacking by
    workout name) or ``metrics.period_stats`` output (plain totals).
    """
    if data is not None and "period" in data.columns and "session_name" not in data.columns:
        table = _from_period_stats(data)
        freq = getattr(table.index, "freqstr", "") or ""
        by = "month" if freq.startswith("M") else "week"
    else:
        table = volume_table(data, by, group)
    fig, ax = style.new_figure(width, height)
    draw_volume(ax, table, by)
    per = "Weekly" if by == "week" else "Monthly"
    sub = None
    if table is not None and not table.empty:
        totals = table.sum(axis=1)
        active = totals[totals > 0]
        sub = (f"{per} average {style.thousands(active.mean() if len(active) else 0)} lb across "
               f"{len(active)} active {by}s · total {style.thousands(totals.sum())} lb")
        if len(table.columns) > 1:
            sub += " · stacked by workout"
    style.set_titles(fig, title or f"{per} training volume", sub, ax=ax)
    notes = list(footnotes) + ["Volume = load × reps over working sets (warm-ups excluded unless requested)."]
    style.add_footnote(fig, notes)
    return fig
