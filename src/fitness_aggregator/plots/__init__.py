"""Charts. Every ``plot_*`` function returns a ``matplotlib.figure.Figure``.

They never call ``plt.show()`` and never touch pyplot: the caller (the CLI
now, a GUI later) decides whether to save, show, or embed the figure. Inputs
are the DataFrames ``metrics`` produces. The ``draw_*`` functions draw one
chart onto an existing Axes, for composing (see ``dashboard``).
"""

from .calendar import draw_calendar, plot_calendar
from .dashboard import plot_dashboard
from .duration import draw_duration, plot_duration
from .frequency import draw_frequency, plot_frequency
from .merge_preview import plot_merge_preview
from .progress import METRICS, default_metric, draw_progress, plot_progress
from .rep_prs import draw_rep_prs, plot_rep_prs, rep_pr_curves
from .sets_per_muscle import plot_sets_per_muscle
from .volume import draw_volume, plot_volume, volume_table

__all__ = [
    "METRICS", "default_metric",
    "plot_calendar", "plot_dashboard", "plot_duration", "plot_frequency", "plot_merge_preview",
    "plot_progress", "plot_rep_prs", "plot_sets_per_muscle", "plot_volume",
    "draw_calendar", "draw_duration", "draw_frequency", "draw_progress", "draw_rep_prs", "draw_volume",
    "rep_pr_curves", "volume_table",
]
