"""Hard sets per muscle per week, as lines (small multiples) or a heatmap.

Input contract: a DataFrame with columns ``period`` (pandas Period),
``muscle`` (str) and ``sets`` (float), one row per period and muscle.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.figure import Figure
from matplotlib.patches import Patch

from . import style

DEFAULT_TARGET = (10.0, 20.0)
OVERLAY_MAX = 4  # up to this many muscles share one axes; more become small multiples


def _table(df: pd.DataFrame) -> pd.DataFrame:
    """period × muscle, all periods in range, muscles ordered by total sets (most first)."""
    t = df.pivot_table(index="period", columns="muscle", values="sets", aggfunc="sum", fill_value=0.0)
    idx = t.index
    if isinstance(idx, pd.PeriodIndex) and len(idx):
        t = t.reindex(pd.period_range(idx.min(), idx.max(), freq=idx.freq), fill_value=0.0)
    return t[t.sum().sort_values(ascending=False).index]


def _unit(index) -> str:
    """'month' for monthly periods (values are then average sets per week), else 'week'."""
    return "month" if isinstance(index, pd.PeriodIndex) and index.freqstr.startswith("M") else "week"


def _x(index) -> pd.DatetimeIndex:
    return index.to_timestamp(how="start") if isinstance(index, pd.PeriodIndex) else pd.to_datetime(index)


def _band(ax: Axes, target) -> None:
    if target:
        ax.axhspan(target[0], target[1], color=style.TARGET_BAND, alpha=0.12, lw=0, zorder=1)


def _draw_lines(fig: Figure, t: pd.DataFrame, target, colors) -> list[Axes]:
    muscles = list(t.columns)
    x = _x(t.index)
    if len(muscles) <= OVERLAY_MAX:
        ax = fig.subplots()
        _band(ax, target)
        for i, m in enumerate(muscles):
            ax.plot(x, t[m], color=(colors or {}).get(m, style.color(i)), lw=2, marker="o", ms=4.5,
                    mec=style.SURFACE, label=m, zorder=3)
        style.date_axis(ax)
        ax.set_ylabel("Sets per week")
        ax.set_ylim(bottom=0)
        handles, labels = ax.get_legend_handles_labels()
        if target:
            handles.append(Patch(color=style.TARGET_BAND, alpha=0.25))
            labels.append(f"Target {target[0]:g}–{target[1]:g}")
        style.legend_below(ax, handles, labels)
        return [ax]
    cols = 3 if len(muscles) > 4 else 2
    rows = math.ceil(len(muscles) / cols)
    axes = np.atleast_1d(fig.subplots(rows, cols, sharex=True, sharey=True)).ravel()
    ymax = max(float(t.to_numpy().max()), target[1] if target else 0) * 1.1 or 1
    for ax, m in zip(axes, muscles, strict=False):
        _band(ax, target)
        ax.plot(x, t[m], color=style.color(0), lw=1.8, zorder=3)
        ax.fill_between(x, 0, t[m], color=style.color(0), alpha=0.08, lw=0, zorder=2)
        last = t[m].iloc[-1]
        style.panel_title(ax, m, f"latest {_unit(t.index)} {round(float(last), 1):g}")
        ax.set_ylim(0, ymax)
        style.date_axis(ax, maxticks=4)
    for i, ax in enumerate(axes[len(muscles):], start=len(muscles)):
        ax.set_visible(False)
        if i - cols >= 0:  # the panel above an empty slot shows its own dates
            axes[i - cols].xaxis.set_tick_params(labelbottom=True)
    for ax in axes[::cols]:
        ax.set_ylabel("Sets / week")
    return list(axes[:len(muscles)])


def _draw_heatmap(fig: Figure, t: pd.DataFrame, target) -> Axes:
    ax = fig.subplots()
    data = t.T.to_numpy(dtype=float)
    lo, hi = target if target else (np.nanmax(data) / 3, 2 * np.nanmax(data) / 3)
    # Bins: none, under target (two shades), in target, over target.
    bounds = [0, 0.5, lo / 2, lo, hi, max(hi * 1.5, np.nanmax(data) + 1)]
    cmap = ListedColormap([style.EMPTY, style.BLUES[0], style.BLUES[1], style.BLUES[3], style.BLUES[6]])
    norm = BoundaryNorm(bounds, cmap.N, clip=True)
    x = _x(t.index)
    ax.pcolormesh(np.arange(len(x) + 1), np.arange(data.shape[0] + 1), data, cmap=cmap, norm=norm,
                  edgecolors=style.SURFACE, linewidth=1.5)
    ax.set_yticks(np.arange(data.shape[0]) + 0.5, t.columns)
    ax.invert_yaxis()
    step = max(1, len(x) // 10)
    fmt = "%b %Y" if _unit(t.index) == "month" else "%b %d"
    ax.set_xticks(np.arange(0, len(x), step) + 0.5, [f"{d:{fmt}}" for d in x[::step]])
    ax.tick_params(length=0)
    ax.grid(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_visible(False)
    labels = ["0", f"under {lo / 2:g}", f"{lo / 2:g}–{lo:g}", f"{lo:g}–{hi:g} (target)" if target else f"{lo:g}–{hi:g}",
              f"over {hi:g}"]
    handles = [Patch(color=c) for c in cmap.colors]
    style.legend_below(ax, handles, labels)
    return ax


@style.use_style
def plot_sets_per_muscle(df: pd.DataFrame, *, kind: str = "lines", target: tuple[float, float] | None = DEFAULT_TARGET,
                         muscles: Iterable[str] | None = None, colors: dict | None = None, title: str | None = None,
                         footnotes: Iterable[str] = (), width: float = 9.5) -> Figure:
    """Weekly sets per muscle. ``kind`` is ``lines`` (overlaid up to 4 muscles,
    small multiples beyond) or ``heatmap``. ``target`` shades a band
    (default 10–20 sets/week); pass ``None`` for no band."""
    if kind not in ("lines", "heatmap"):
        raise ValueError("kind must be 'lines' or 'heatmap'")
    t = _table(df) if df is not None and not df.empty else pd.DataFrame()
    if muscles is not None and not t.empty:
        t = t[[m for m in muscles if m in t.columns]]
    n = len(t.columns)
    if t.empty or n == 0:
        fig, ax = style.new_figure(width, 3.5)
        style.no_data(ax, "No sets per muscle to show")
        style.set_titles(fig, title or "Sets per muscle")
        return fig
    if kind == "heatmap":
        height = 1.9 + 0.32 * n
    elif n <= OVERLAY_MAX:
        height = 4.6
    else:
        height = 1.4 + 1.75 * math.ceil(n / 3)
    fig = Figure(figsize=(width, height), layout="constrained")
    fig.get_layout_engine().set(w_pad=0.12, h_pad=0.1, hspace=0.08, wspace=0.04)
    if kind == "heatmap":
        first = _draw_heatmap(fig, t, target)
    else:
        first = _draw_lines(fig, t, target, colors)[0]
    sub = f"{len(t)} {_unit(t.index)}s · {n} muscle{'s' if n != 1 else ''}"
    if target:
        sub += f" · target {target[0]:g}–{target[1]:g} sets/week shaded" if kind == "lines" else ""
    style.set_titles(fig, title or "Sets per muscle per week", sub if (kind == "heatmap" or n <= OVERLAY_MAX) else None,
                     ax=first)
    notes = list(footnotes)
    if kind == "lines" and n > OVERLAY_MAX:
        notes.insert(0, sub + ".")
    style.add_footnote(fig, notes)
    return fig
