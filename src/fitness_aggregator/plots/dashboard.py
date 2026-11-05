"""One-glance summary: headline numbers, calendar, volume, duration, top lifts, frequency."""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd
from matplotlib.figure import Figure

from .. import metrics
from . import style
from .calendar import daily_volume, draw_calendar
from .duration import draw_duration
from .frequency import draw_frequency
from .progress import draw_progress
from .volume import draw_volume, volume_table

TOP_LIFTS = 3


def _top_lifts(sets_df: pd.DataFrame, n: int) -> list[str]:
    loaded = sets_df[sets_df["kind"] == "loaded"]
    if loaded.empty:
        return []
    return loaded.groupby("exercise")["session"].nunique().sort_values(ascending=False).head(n).index.tolist()


def _tile(ax, value: str, label: str) -> None:
    ax.set_axis_off()
    ax.text(0, 0.62, value, transform=ax.transAxes, fontsize=21, fontweight="bold", color=style.INK, va="center")
    ax.text(0, 0.12, label, transform=ax.transAxes, fontsize=9, color=style.INK_2, va="center")


@style.use_style
def plot_dashboard(sets_df: pd.DataFrame, sessions_df: pd.DataFrame, events: pd.DataFrame | None = None, *,
                   formula: str = "epley", today=None, top_lifts: int = TOP_LIFTS, title: str | None = None,
                   footnotes: Iterable[str] = ()) -> Figure:
    """``sets_df`` from ``metrics.sets_frame``, ``sessions_df`` from
    ``metrics.sessions_frame``, ``events`` from ``metrics.pr_events`` (computed if omitted)."""
    fig = Figure(figsize=(16, 12.5), layout="constrained")
    fig.get_layout_engine().set(w_pad=0.15, h_pad=0.12, hspace=0.05, wspace=0.05)
    if sets_df is None or sets_df.empty or sessions_df is None or sessions_df.empty:
        ax = fig.subplots()
        style.no_data(ax, "No workouts yet")
        style.set_titles(fig, title or "Training dashboard")
        return fig
    if events is None:
        events = metrics.pr_events(sets_df, formula)
    today = pd.Timestamp(today) if today is not None else pd.Timestamp.now()

    gs = fig.add_gridspec(4, 6, height_ratios=[0.5, 1.55, 2.0, 2.0])
    # Headline numbers.
    c = metrics.consistency(sessions_df, today.to_pydatetime())
    recent_prs = 0 if events.empty else int((pd.to_datetime(events["session"]) >= today - pd.Timedelta(days=30)).sum())
    week_vol = volume_table(sets_df, "week", None)
    tiles = [
        (f"{c['sessions']}", f"sessions since {c['first']:%b %d, %Y}"),
        (f"{c['per_week']:.1f}", "sessions per week"),
        (f"{style.thousands(sets_df['volume'].sum())} lb", "total volume"),
        (f"{style.thousands(week_vol['Volume'].iloc[-4:].mean())} lb", "avg weekly volume, last 4 weeks"),
        (f"{recent_prs}", "PRs in the last 30 days (incl. rep PRs)"),
        (f"{c['current_week_streak']} wk", f"current weekly streak (best {c['longest_week_streak']})"),
    ]
    tile_gs = gs[0, :].subgridspec(1, len(tiles))
    for i, (value, label) in enumerate(tiles):
        _tile(fig.add_subplot(tile_gs[0, i]), value, label)

    ax_cal = fig.add_subplot(gs[1, :4])
    draw_calendar(ax_cal, daily_volume(sets_df, sessions_df), scale_legend=False)
    style.panel_title(ax_cal, "Training days", "darker = more volume")

    ax_freq = fig.add_subplot(gs[1:3, 4:])
    summary = metrics.exercise_summary(sets_df)
    draw_frequency(ax_freq, summary, top=18, today=today, legend=True, label_limit=28)
    style.panel_title(ax_freq, "Most-done exercises", "sessions")

    ax_vol = fig.add_subplot(gs[2, :2])
    draw_volume(ax_vol, week_vol, "week", legend=False)
    style.panel_title(ax_vol, "Weekly volume", "lb")

    ax_dur = fig.add_subplot(gs[2, 2:4])
    info = draw_duration(ax_dur, sessions_df, legend=False)
    style.panel_title(ax_dur, "Session duration", "min · line: 30-day median")

    lifts = _top_lifts(sets_df, top_lifts)
    span = 6 // max(len(lifts), 1)
    for i, name in enumerate(lifts):
        ax = fig.add_subplot(gs[3, i * span:(i + 1) * span])
        hist = metrics.exercise_history(sets_df, name, formula)
        draw_progress(ax, {name: hist}, events, metric="e1rm", trend=False, colors={name: style.color(i)},
                      legend=False)
        style.panel_title(ax, name, "e1RM, lb · ★ PR")
        ax.set_ylabel("")

    start, end = sessions_df["session"].min(), sessions_df["session"].max()
    style.set_titles(fig, title or f"Training dashboard · {start:%b %d, %Y} – {end:%b %d, %Y}")
    notes = list(footnotes)
    if info.get("excluded"):
        n = info["excluded"]
        notes.insert(0, f"Duration: {n} session{'s' if n != 1 else ''} with an untrusted duration left out.")
    notes.append(f"e1RM ({formula.title()}) from working sets of 1–12 reps. Volume = load × reps.")
    style.add_footnote(fig, notes)
    return fig
