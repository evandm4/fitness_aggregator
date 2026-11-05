"""One shared look for every chart: palette, type, gridlines, dates, footnotes.

Charts are built on ``matplotlib.figure.Figure`` directly (never pyplot), so
they can be saved headless or embedded in a GUI. Wrap drawing code in
``with styled():`` so the rc settings below apply to every artist created.

Palette: a validated categorical order (adjacent pairs stay distinguishable
under common colour-vision deficiencies), a single-hue blue ramp for
magnitudes, and quiet neutral inks for text, axes and grid.
"""

from __future__ import annotations

import contextlib
import functools
from typing import Iterable, Sequence

import matplotlib
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.transforms import ScaledTranslation

# --- palette -----------------------------------------------------------------

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"      # secondary text
MUTED = "#898781"      # axis labels, ticks, footnotes
GRID = "#e1e0d9"       # hairline gridlines
BASELINE = "#c3c2b7"   # axis spines
EMPTY = "#efeee9"      # "nothing here" cells (calendar)
FAINT = "#c9c8c0"      # previous / reference series

# Categorical series, in this fixed order. Never cycle past 8: fold into "Other".
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
OTHER = "#b4b2a9"

# Sequential (magnitude) ramp, light → dark blue.
BLUES = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]

GOOD = "#0ca30c"       # status "good": PRs only
TARGET_BAND = "#1baf7a"  # target-zone shading (used at low alpha)

FONT = ["Inter", "Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans", "sans-serif"]

RC = {
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.family": "sans-serif",
    "font.sans-serif": FONT,
    "font.size": 10,
    "text.color": INK,
    "axes.labelcolor": INK_2,
    "axes.labelsize": 9.5,
    "axes.titlesize": 11,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.titlepad": 8,
    "axes.edgecolor": BASELINE,
    "axes.linewidth": 0.8,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "axes.grid.axis": "y",
    "axes.axisbelow": True,
    "axes.prop_cycle": matplotlib.cycler(color=SERIES),
    "grid.color": GRID,
    "grid.linewidth": 0.7,
    "grid.linestyle": "-",
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "xtick.labelcolor": INK_2,
    "ytick.labelcolor": INK_2,
    "xtick.labelsize": 8.5,
    "ytick.labelsize": 8.5,
    "xtick.major.size": 3,
    "ytick.major.size": 0,
    "xtick.major.width": 0.8,
    "lines.linewidth": 2,
    "lines.markersize": 6,
    "lines.solid_capstyle": "round",
    "legend.frameon": False,
    "legend.fontsize": 8.5,
    "legend.labelcolor": INK_2,
    "legend.handlelength": 1.6,
    "legend.borderaxespad": 0.2,
    "patch.linewidth": 0,
    "savefig.dpi": 150,
    "figure.dpi": 100,
}


@contextlib.contextmanager
def styled():
    with matplotlib.rc_context(RC):
        yield


def use_style(func):
    """Decorator: run a chart function inside ``styled()``."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with styled():
            return func(*args, **kwargs)
    return wrapper


def color(i: int) -> str:
    """Categorical slot ``i`` (0-based). Past the eighth slot everything is 'Other' grey."""
    return SERIES[i] if 0 <= i < len(SERIES) else OTHER


# --- figure & axes helpers -----------------------------------------------------

def new_figure(width: float = 9, height: float = 4.8, **subplot_kw) -> tuple[Figure, object]:
    """A constrained-layout Figure (no pyplot) and its axes."""
    fig = Figure(figsize=(width, height), layout="constrained")
    fig.get_layout_engine().set(w_pad=0.12, h_pad=0.1, hspace=0.06, wspace=0.04)
    axes = fig.subplots(**subplot_kw) if subplot_kw else fig.subplots()
    return fig, axes


def set_titles(fig: Figure, title: str, subtitle: str | None = None, ax: Axes | None = None) -> None:
    """Bold left-aligned figure title, with an optional muted subtitle on the (first) axes."""
    fig.suptitle(title, x=0.012, ha="left", fontsize=14, fontweight="bold", color=INK)
    if subtitle and ax is not None:
        ax.set_title(subtitle, loc="left", fontsize=9.5, fontweight="normal", color=INK_2, pad=10)


def panel_title(ax: Axes, title: str, subtitle: str | None = None) -> None:
    """Title for one panel of a multi-panel figure."""
    text = title if not subtitle else f"{title}  "
    ax.set_title(text, loc="left", fontsize=10.5, fontweight="bold", color=INK)
    if subtitle:
        ax.set_title(subtitle, loc="right", fontsize=8.5, fontweight="normal", color=MUTED)


def date_axis(ax: Axes, axis: str = "x", maxticks: int = 8) -> None:
    """Concise date ticks: 'Jul', 'Aug', … with the year shown once."""
    locator = mdates.AutoDateLocator(minticks=3, maxticks=maxticks)
    formatter = mdates.ConciseDateFormatter(locator, show_offset=False)
    formatter.zero_formats = ["", "%Y", "%b", "%b %d", "%H:%M", "%H:%M"]
    formatter.formats = ["%Y", "%b", "%b %d", "%H:%M", "%H:%M", "%S.%f"]
    target = ax.xaxis if axis == "x" else ax.yaxis
    target.set_major_locator(locator)
    target.set_major_formatter(formatter)


def thousands(x: float, _pos=None) -> str:
    if abs(x) >= 1_000_000:
        return f"{x / 1_000_000:.1f}M".replace(".0M", "M")
    if abs(x) >= 1000:
        return f"{x / 1000:.0f}k" if abs(x) >= 10_000 else f"{x / 1000:.1f}k".replace(".0k", "k")
    return f"{x:g}"


def thousands_axis(ax: Axes, axis: str = "y") -> None:
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(mticker.FuncFormatter(thousands))


def no_data(ax: Axes, message: str = "No data to show") -> None:
    """Blank axes with a centred message."""
    ax.set_xticks([])
    ax.set_yticks([])
    for side in ("left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.grid(False)
    ax.text(0.5, 0.5, message, transform=ax.transAxes, ha="center", va="center", color=MUTED, fontsize=10)


def clean_legend(ax: Axes, **kwargs):
    kw = {"loc": "upper left", "ncols": 1, "fontsize": 8.5, "handletextpad": 0.5, "columnspacing": 1.2}
    kw.update(kwargs)
    handles, labels = ax.get_legend_handles_labels()
    if not handles:
        return None
    return ax.legend(**kw)


# --- footnotes -----------------------------------------------------------------

FOOTNOTE_LINE_IN = 0.19  # inches per footnote line


def add_footnote(fig: Figure, notes: str | Iterable[str] | None) -> None:
    """Small muted notes under the figure, e.g. merges, corrections, placeholder bodyweight.

    Reserves room at the bottom of the constrained layout so the notes never
    overlap tick labels. Duplicate notes are dropped; call once per figure.
    """
    if notes is None:
        return
    lines = [notes] if isinstance(notes, str) else list(dict.fromkeys(n for n in notes if n))
    if not lines:
        return
    height = fig.get_figheight()
    frac = min((FOOTNOTE_LINE_IN * len(lines) + 0.12) / height, 0.4)
    engine = fig.get_layout_engine()
    if engine is not None and hasattr(engine, "set"):
        try:
            engine.set(rect=(0, frac, 1, 1 - frac))
        except TypeError:  # very old matplotlib: no rect; fall back to subplots_adjust-free text
            pass
    fig.text(0.012, 0.06 / height, "\n".join(lines), ha="left", va="bottom",
             fontsize=8, color=MUTED, linespacing=1.45)


def legend_below(ax: Axes, handles: Sequence | None = None, labels: Sequence | None = None,
                 ncols: int | None = None):
    """A frameless legend in a row under the x-axis, so it never covers data."""
    if handles is None:
        handles, labels = ax.get_legend_handles_labels()
    if not handles:
        return None
    # Anchor a fixed distance under the axes (past tick labels, and the x label if any).
    drop = 0.5 if ax.get_xlabel() else 0.27
    transform = ax.transAxes + ScaledTranslation(0, -drop, ax.figure.dpi_scale_trans)
    return ax.legend(handles, labels, loc="upper left", bbox_to_anchor=(0, 0), bbox_transform=transform,
                     ncols=ncols or min(len(handles), 5), fontsize=8.5, handletextpad=0.45,
                     columnspacing=1.6, borderaxespad=0)
