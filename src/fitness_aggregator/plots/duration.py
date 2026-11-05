"""Session duration over time (from ``metrics.sessions_frame``)."""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from . import style

TREND_DAYS = 30


def draw_duration(ax: Axes, sessions_df: pd.DataFrame, *, trend: bool = True, legend: bool = True) -> dict:
    """Returns {'shown': n, 'excluded': n sessions with untrusted durations}."""
    if sessions_df is None or sessions_df.empty:
        style.no_data(ax, "No sessions yet")
        return {"shown": 0, "excluded": 0}
    d = sessions_df.assign(session=pd.to_datetime(sessions_df["session"])).sort_values("session")
    excluded = int(d["duration_min"].isna().sum())
    d = d.dropna(subset=["duration_min"])
    if d.empty:
        style.no_data(ax, "No sessions with a trusted duration")
        return {"shown": 0, "excluded": excluded}
    ax.scatter(d["session"], d["duration_min"], s=26, color=style.color(0), alpha=0.75,
               edgecolors=style.SURFACE, linewidths=1.0, zorder=3)
    if trend and len(d) >= 3:
        t = d.set_index("session")["duration_min"].rolling(f"{TREND_DAYS}D", min_periods=1).median()
        ax.plot(t.index, t.values, color=style.INK_2, lw=2, zorder=4)
    median = d["duration_min"].median()
    style.date_axis(ax)
    ax.set_ylabel("Duration (min)")
    ax.margins(x=0.03, y=0.1)
    ax.set_ylim(bottom=0)
    if legend:
        handles = [Line2D([], [], ls="", marker="o", ms=6, color=style.color(0), mec=style.SURFACE),
                   Line2D([], [], lw=2, color=style.INK_2)] if trend and len(d) >= 3 else []
        if handles:
            style.legend_below(ax, handles, ["Session", f"{TREND_DAYS}-day median"])
    return {"shown": len(d), "excluded": excluded, "median": median}


@style.use_style
def plot_duration(sessions_df: pd.DataFrame, *, trend: bool = True, title: str | None = None,
                  footnotes: Iterable[str] = (), width: float = 9, height: float = 4.4) -> Figure:
    """Session duration over time. Sessions whose duration isn't trusted
    (NaN ``duration_min``, e.g. a timer left running) are left out, with a note."""
    fig, ax = style.new_figure(width, height)
    info = draw_duration(ax, sessions_df, trend=trend)
    sub = f"Median {info['median']:.0f} min over {info['shown']} sessions" if info.get("median") is not None else None
    style.set_titles(fig, title or "Session duration", sub, ax=ax)
    notes = list(footnotes)
    if info["excluded"]:
        n = info["excluded"]
        notes.insert(0, f"{n} session{'s' if n != 1 else ''} with an untrusted duration (timer left running) "
                        "left out; see fitness corrections.")
    style.add_footnote(fig, notes)
    return fig
