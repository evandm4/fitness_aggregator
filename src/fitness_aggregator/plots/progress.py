"""Progress over time for one or more exercises (from ``metrics.exercise_history``)."""

from __future__ import annotations

from typing import Iterable, Mapping

import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from . import style

METRICS = {
    # metric: (history column, axis label, PR types that mark a session)
    "e1rm": ("e1rm", "Estimated 1RM (lb)", {"e1rm"}),
    "top": ("top_load", "Top set load (lb)", {"weight"}),
    "volume": ("volume", "Volume (lb)", set()),
    "reps": ("max_reps", "Most reps in a set", {"reps"}),
    "seconds": ("max_seconds", "Longest hold (s)", {"seconds"}),
    "assistance": ("min_assistance", "Assistance (lb, lower is better)", {"weight"}),
}
METRIC_TITLES = {"e1rm": "Estimated 1RM", "top": "Top set", "volume": "Session volume",
                 "reps": "Most reps", "seconds": "Longest hold", "assistance": "Assistance"}
TREND_DAYS = 30


def default_metric(kind: str) -> str:
    """The metric that makes sense for an exercise kind (catalog kinds)."""
    return {"timed": "seconds", "unloaded": "reps", "assisted": "assistance"}.get(kind, "e1rm")


def _series(hist: pd.DataFrame, metric: str) -> pd.DataFrame:
    col = METRICS[metric][0]
    if hist is None or hist.empty or col not in hist:
        return pd.DataFrame(columns=["session", "value", "corrected", "merged_from", "merge_label"])
    d = pd.DataFrame({
        "session": pd.to_datetime(hist["session"]),
        "value": pd.to_numeric(hist[col], errors="coerce"),
        "corrected": hist.get("corrected", False),
        "merged_from": hist.get("merged_from", "").fillna("") if "merged_from" in hist else "",
        "merge_label": hist.get("merge_label", "").fillna("") if "merge_label" in hist else "",
    })
    if metric in ("volume", "reps", "seconds"):
        d.loc[d["value"] <= 0, "value"] = np.nan
    return d.dropna(subset=["value"]).sort_values("session").reset_index(drop=True)


def _pr_sessions(prs: pd.DataFrame | None, exercise: str, metric: str) -> set:
    types = METRICS[metric][2]
    if prs is None or prs.empty or not types:
        return set()
    sel = prs[(prs["exercise"] == exercise) & prs["pr_type"].isin(types)]
    return set(pd.to_datetime(sel["session"]))


def _fmt(v: float, metric: str) -> str:
    if metric == "seconds":
        return f"{v:g} s"
    if metric == "reps":
        return f"{v:g}"
    if metric == "volume":
        return f"{style.thousands(v)} lb"
    return f"{v:,.0f} lb"


def draw_progress(ax: Axes, histories: Mapping[str, pd.DataFrame], prs: pd.DataFrame | None = None, *,
                  metric: str = "e1rm", trend: bool = False, label_ends: bool = True,
                  colors: Mapping[str, str] | None = None, legend: bool = True) -> dict:
    """Draw onto ``ax``. Returns what was drawn: {'merged': bool, 'corrected': bool, 'prs': int}."""
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}; use one of {', '.join(METRICS)}")
    info = {"merged": False, "corrected": False, "prs": 0, "points": 0}
    names = list(histories)
    for i, name in enumerate(names):
        d = _series(histories[name], metric)
        if d.empty:
            continue
        c = (colors or {}).get(name, style.color(i))
        info["points"] += len(d)
        ax.plot(d["session"], d["value"], color=c, lw=2, alpha=0.9 if not trend else 0.55, zorder=3,
                label=name if len(names) > 1 else None)
        merged = d["merged_from"] != ""
        corrected = d["corrected"].astype(bool) & ~merged
        plain = ~merged & ~corrected
        ax.scatter(d.loc[plain, "session"], d.loc[plain, "value"], s=26, color=c, edgecolors=style.SURFACE,
                   linewidths=1.2, zorder=4)
        if merged.any():
            info["merged"] = True
            ax.scatter(d.loc[merged, "session"], d.loc[merged, "value"], s=28, facecolors=style.SURFACE,
                       edgecolors=c, linewidths=1.6, zorder=4)
        if corrected.any():
            info["corrected"] = True
            ax.scatter(d.loc[corrected, "session"], d.loc[corrected, "value"], s=34, marker="D", color=c,
                       edgecolors=style.INK, linewidths=0.9, zorder=4)
        pr_when = _pr_sessions(prs, name, metric)
        pr = d[d["session"].isin(pr_when)]
        if not pr.empty:
            info["prs"] += len(pr)
            ax.scatter(pr["session"], pr["value"], s=120, marker="*", color=c, edgecolors=style.SURFACE,
                       linewidths=1.0, zorder=5)
        if trend and len(d) >= 3:
            t = d.set_index("session")["value"].rolling(f"{TREND_DAYS}D", min_periods=1).mean()
            ax.plot(t.index, t.values, color=c, lw=3.2, alpha=0.95, zorder=3.5, solid_capstyle="round")
        if label_ends:
            last = d.iloc[-1]
            ax.annotate(_fmt(last["value"], metric), (last["session"], last["value"]), xytext=(7, 0),
                        textcoords="offset points", va="center", ha="left", fontsize=8.5, color=style.INK_2,
                        annotation_clip=False)
    if info["points"] == 0:
        style.no_data(ax, "No sessions with this metric")
        return info
    style.date_axis(ax)
    ax.set_ylabel(METRICS[metric][1])
    if metric == "volume":
        style.thousands_axis(ax)
    if metric == "assistance":
        ax.invert_yaxis()
    ax.margins(x=0.04, y=0.12)
    if legend:
        _legend(ax, names, info, trend, colors)
    return info


def _legend(ax: Axes, names, info: dict, trend: bool, colors) -> None:
    handles, labels = ax.get_legend_handles_labels()
    key = style.INK_2 if len(names) > 1 else style.color(0)
    if len(names) == 1 and colors:
        key = colors.get(names[0], key)
    extra = []
    if info["prs"]:
        extra.append((Line2D([], [], ls="", marker="*", ms=11, color=key, mec=style.SURFACE), "PR"))
    if info["merged"]:
        extra.append((Line2D([], [], ls="", marker="o", ms=6, mfc=style.SURFACE, mec=key, mew=1.6),
                      "Recorded under a combined exercise"))
    if info["corrected"]:
        extra.append((Line2D([], [], ls="", marker="D", ms=5.5, color=key, mec=style.INK, mew=0.9),
                      "Includes corrected sets"))
    if trend:
        extra.append((Line2D([], [], lw=3.2, color=key), f"{TREND_DAYS}-day average"))
    for h, l in extra:
        handles.append(h)
        labels.append(l)
    if handles:
        style.legend_below(ax, handles, labels)


@style.use_style
def plot_progress(histories: Mapping[str, pd.DataFrame] | pd.DataFrame, prs: pd.DataFrame | None = None, *,
                  metric: str = "e1rm", trend: bool = False, split: bool = False, name: str | None = None,
                  title: str | None = None, footnotes: Iterable[str] = (), width: float = 9,
                  height: float | None = None) -> Figure:
    """One or more exercises over time.

    ``histories`` maps exercise name → ``metrics.exercise_history`` frame (or a
    single frame with ``name``). ``prs`` is ``metrics.pr_events`` output; PR
    sessions for the chosen metric get a star. Sessions recorded under a
    combined exercise (``merged_from``) are hollow; sessions with corrected
    sets are diamonds. ``split`` draws one panel per exercise instead of
    overlaying them. ``assistance`` uses an inverted y-axis.
    """
    if isinstance(histories, pd.DataFrame):
        histories = {name or "Exercise": histories}
    names = list(histories)
    split = split and len(names) > 1
    notes = list(footnotes)
    if split:
        rows = len(names)
        fig, axes = style.new_figure(width, height or 1.0 + 2.3 * rows, nrows=rows, sharex=True)
        axes = list(np.atleast_1d(axes))
        info = {"merged": False, "corrected": False}
        for i, (ax, n) in enumerate(zip(axes, names)):
            got = draw_progress(ax, {n: histories[n]}, prs, metric=metric, trend=trend,
                                colors={n: style.color(i)}, legend=True)
            style.panel_title(ax, n)
            info["merged"] |= got["merged"]
            info["corrected"] |= got["corrected"]
        first = axes[0]
    else:
        fig, ax = style.new_figure(width, height or 4.8)
        info = draw_progress(ax, histories, prs, metric=metric, trend=trend)
        first = ax
    head = title or (f"{METRIC_TITLES[metric]}: {names[0]}" if len(names) == 1 else METRIC_TITLES[metric])
    span = _span(histories)
    style.set_titles(fig, head, span if not split else None, ax=first if not split else None)
    if metric == "e1rm":
        notes.insert(0, "e1RM estimated from working sets of 1–12 reps.")
    style.add_footnote(fig, notes)
    return fig


def _span(histories: Mapping[str, pd.DataFrame]) -> str:
    stamps = [pd.to_datetime(h["session"]) for h in histories.values() if h is not None and not h.empty]
    if not stamps:
        return ""
    allv = pd.concat(stamps)
    n = len(allv)
    return f"{allv.min():%b %d, %Y} – {allv.max():%b %d, %Y} · {n} session{'s' if n != 1 else ''}"
