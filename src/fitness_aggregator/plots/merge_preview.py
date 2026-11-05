"""Preview combining two exercises: both top-set timelines plus the combined, adjusted line."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import pandas as pd
from matplotlib.figure import Figure

from ..merges import Merge, adjustment_label
from ..similarity import TopSet
from . import style


def _frame(sets: Sequence[TopSet], timed: bool) -> pd.DataFrame:
    return pd.DataFrame({
        "when": pd.to_datetime([t.when for t in sets]),
        "value": [t.seconds if timed else t.weight for t in sets],
        "reps": [t.reps for t in sets],
    })


@style.use_style
def plot_merge_preview(kept_sets: Sequence[TopSet], other_sets: Sequence[TopSet], *, offset: float | None = None,
                       factor: float | None = None, kept_name: str = "Kept", other_name: str = "Other",
                       title: str | None = None, footnotes: Iterable[str] = (), width: float = 9,
                       height: float = 4.8) -> Figure:
    """Top set per session for both exercises, the other one as recorded (faint)
    and adjusted by ``offset``/``factor`` (as ``merges.Merge.adjust`` does), and
    the combined timeline you'd see after merging."""
    fig, ax = style.new_figure(width, height)
    timed = all(t.weight == 0 for t in [*kept_sets, *other_sets]) and any(t.seconds for t in [*kept_sets, *other_sets])
    k = _frame(kept_sets, timed)
    o = _frame(other_sets, timed)
    label = adjustment_label(offset, factor)
    if k.empty and o.empty:
        style.no_data(ax, "No working sets to compare")
        style.set_titles(fig, title or f"Combine {other_name} into {kept_name}?")
        return fig
    merge = Merge(other_name, kept_name, offset, factor)
    o["adjusted"] = o["value"] if timed else o["value"].map(merge.adjust)
    combined = pd.concat([k[["when", "value"]], o[["when", "adjusted"]].rename(columns={"adjusted": "value"})])
    combined = combined.sort_values("when")

    kc, oc = style.color(0), style.color(1)
    ax.plot(combined["when"], combined["value"], color=style.INK_2, lw=1.4, alpha=0.6, zorder=2,
            label=f"Combined as {kept_name}")
    ax.scatter(k["when"], k["value"], s=34, color=kc, edgecolors=style.SURFACE, linewidths=1.2, zorder=4,
               label=kept_name)
    adjusted = label and not timed and not o.empty and (o["adjusted"] != o["value"]).any()
    if adjusted:
        ax.scatter(o["when"], o["value"], s=26, facecolors=style.SURFACE, edgecolors=oc, linewidths=1.2,
                   alpha=0.7, zorder=3, label=f"{other_name} as recorded")
        for _, r in o.iterrows():
            ax.annotate("", (r["when"], r["adjusted"]), (r["when"], r["value"]),
                        arrowprops=dict(arrowstyle="-", color=oc, alpha=0.35, lw=1), zorder=2)
    ax.scatter(o["when"], o["adjusted"], s=34, color=oc, edgecolors=style.SURFACE, linewidths=1.2, zorder=4,
               label=f"{other_name} {label}" if adjusted else other_name)
    if not o.empty and not k.empty:
        switch = o["when"].min() if o["when"].min() > k["when"].min() else k["when"].min()
        ax.axvline(switch, color=style.BASELINE, lw=1, zorder=1)
        ax.annotate(f"switch {switch:%b %d}", (switch, 1), xycoords=("data", "axes fraction"), xytext=(4, -4),
                    textcoords="offset points", va="top", fontsize=8, color=style.MUTED)
    style.date_axis(ax)
    ax.set_ylabel("Top set (s)" if timed else "Top set weight (lb)")
    ax.margins(x=0.04, y=0.12)
    style.legend_below(ax, ncols=4)
    how = f"with {label}" if label else "as recorded"
    sub = f"{len(k)} + {len(o)} sessions combined {how}"
    style.set_titles(fig, title or f"Combine {other_name} into {kept_name}", sub, ax=ax)
    notes = list(footnotes) + ["Preview only: nothing is combined until you confirm in fitness merge review."]
    style.add_footnote(fig, notes)
    return fig
