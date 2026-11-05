"""``fitness plot …``: charts from the command line.

By default every command saves a PNG to ``./plots/<name>_<YYYY-MM-DD>.png``
and prints the path, so it works over SSH and headless. ``--out`` picks the
file (.png, .svg or .pdf); ``--show`` opens a window when there's a display.

Helpers from ``cli`` are imported inside functions: ``cli`` imports this
module at the end of its own import, so a top-level import would be circular.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import typer
from rich.console import Console
from rich.markup import escape

if TYPE_CHECKING:  # pragma: no cover
    from matplotlib.figure import Figure

plot_app = typer.Typer(
    help="Draw charts: progress, volume, calendar, duration, frequency, rep PRs, sets per muscle, dashboard. "
         "Saves a PNG to ./plots/ by default.",
    no_args_is_help=True,
)

PLOT_DIR = Path("plots")
FORMATS = (".png", ".svg", ".pdf")
OUT_HELP = "Save to this file instead (.png, .svg or .pdf)."
SHOW_HELP = "Open the chart in a window instead of saving it (needs a display)."
SINCE_HELP = "Only data on or after this date (YYYY-MM-DD)."
NON_INTERACTIVE = {"agg", "cairo", "pdf", "pgf", "ps", "svg", "template"}


# --- output ---------------------------------------------------------------------

def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "chart"


def default_path(name: str) -> Path:
    return PLOT_DIR / f"{name}_{date.today():%Y-%m-%d}.png"


def save_figure(fig: Figure, path: Path) -> Path:
    """Save with the Agg canvas (vector formats pick their own canvas)."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    FigureCanvasAgg(fig)
    fig.savefig(path, dpi=150 if path.suffix.lower() == ".png" else None)
    return path


def _has_display() -> bool:
    if sys.platform.startswith("linux") or sys.platform.startswith("freebsd"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def _show(console: Console, fig: Figure) -> None:
    from .cli import _fail

    if not _has_display():
        _fail(console, "--show needs a display and none was found (over SSH or headless?). "
                       "Leave out --show to save a PNG, or choose a file with --out.")
    try:
        import matplotlib.pyplot as plt

        if plt.get_backend().lower() in NON_INTERACTIVE:
            raise RuntimeError("no interactive matplotlib backend (install Tk or Qt for Python)")
        # Hand our pyplot-free Figure to a pyplot window.
        holder = plt.figure()
        manager = holder.canvas.manager
        manager.canvas.figure = fig
        fig.set_canvas(manager.canvas)
        plt.show()
    except typer.Exit:
        raise
    except Exception as exc:  # any GUI toolkit failure becomes a friendly message
        _fail(console, f"couldn't open a window: {escape(str(exc))}. Leave out --show to save a PNG instead.")


def _output(console: Console, fig: Figure, name: str, out: Path | None, show: bool) -> None:
    from .cli import _fail

    if out is not None and out.suffix.lower() not in FORMATS:
        _fail(console, f"--out must end in {', '.join(FORMATS)}; got {escape(str(out))}.")
    if out is not None or not show:
        path = out or default_path(name)
        try:
            path = save_figure(fig, path)
        except OSError as exc:
            _fail(console, f"can't write {escape(str(path))}: {escape(exc.strerror or str(exc))}")
        console.print(f"Saved {escape(str(path))}")
    if show:
        _show(console, fig)


# --- footnotes ------------------------------------------------------------------

def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def footnotes(a, df: pd.DataFrame, *, sessions: bool = False, loads: bool = True) -> list[str]:
    """Notes for data that isn't exactly as recorded: set fixes, exercise and
    workout merges, placeholder bodyweight. ``a`` is ``cli.Analysis``."""
    from .catalog import ASSISTED

    notes = []
    if df.empty:
        return notes
    fixed = int(df["corrected"].sum())
    if fixed:
        notes.append(f"Includes {_plural(fixed, 'corrected set')}; see fitness corrections.")
    merged = (df[df["merged_from"] != ""][["merged_from", "merge_label", "exercise"]]
              .drop_duplicates("merged_from").sort_values("merged_from"))
    if len(merged) and len(merged) <= 2:
        parts = [f"{r.merged_from} → {r.exercise}" + (f", {r.merge_label} offset" if r.merge_label and
                 not r.merge_label.startswith("×") else (f", weights {r.merge_label}" if r.merge_label else ""))
                 for r in merged.itertuples()]
        notes.append(f"Includes {_plural(len(merged), 'exercise merge')} ({'; '.join(parts)}); "
                     "see fitness corrections.")
    elif len(merged):
        notes.append(f"Includes {len(merged)} exercise merges; see fitness merge list.")
    if sessions:
        when = set(df["session"])
        n = sum(1 for s in a.sessions if s.is_merged and pd.Timestamp(s.started_at) in when)
        if n:
            notes.append(f"{_plural(n, 'session')} combined from workouts logged in parts; see fitness corrections.")
    if loads and a.bodyweight.uses_placeholder and (df["kind"] == ASSISTED).any():
        notes.append(f"Assisted-lift loads use placeholder bodyweight {a.bodyweight.placeholder_lb:g} lb "
                     "(fitness config set bodyweight_lb N).")
    return notes


def _since(console, df: pd.DataFrame, column: str, since: str | None) -> pd.DataFrame:
    """Rows on or after ``since``; a bad date is a usage error (from ``cli._parse_date``)."""
    from .cli import _filter_dates

    return _filter_dates(df, column, since, None) if since else df


def _console(ctx: typer.Context) -> Console:
    from .cli import _state

    return _state(ctx).console


def _setup(ctx: typer.Context, raw: bool, include_warmups: bool = False):
    from .cli import _analysis, _state

    state = _state(ctx)
    return state, state.console, _analysis(state, raw=raw, include_warmups=include_warmups)


# --- commands -------------------------------------------------------------------

@plot_app.command()
def progress(
    ctx: typer.Context,
    exercises: list[str] = typer.Argument(..., help="One or more exercises (a unique part of the name works)."),
    metric: str | None = typer.Option(None, "--metric", help="e1rm, top, volume, reps, seconds, or assistance "
                                      "(default: e1rm; seconds for holds, reps for bodyweight, "
                                      "assistance for assisted)."),
    trend: bool = typer.Option(False, "--trend", help="Add a 30-day rolling average."),
    split: bool = typer.Option(False, "--split", help="One panel per exercise instead of overlaying them."),
    since: str | None = typer.Option(None, "--since", help=SINCE_HELP),
    formula: str = typer.Option("epley", "--formula", help="e1RM formula: epley or brzycki."),
    raw: bool = typer.Option(False, "--raw", help="Ignore corrections (no merging, no set fixes)."),
    out: Path | None = typer.Option(None, "--out", help=OUT_HELP),
    show: bool = typer.Option(False, "--show", help=SHOW_HELP),
):
    """Progress over time for one or more exercises, PRs starred."""
    from . import metrics, plots
    from .cli import _check_formula, _fail, _parse_date, _resolve_exercise, esc

    state, console, a = _setup(ctx, raw)
    _check_formula(console, formula)
    if metric is not None and metric not in plots.METRICS:
        _fail(console, f"unknown metric {esc(repr(metric))}; use {', '.join(plots.METRICS)}.")
    if since:
        _parse_date(since)
    merged = {} if raw else state.store.active_merges()
    names = list(dict.fromkeys(_resolve_exercise(console, q, a.df["exercise"], merged) for q in exercises))
    if len(names) > 8:
        _fail(console, "plot at most 8 exercises at once.")
    df = a.df[a.df["exercise"].isin(names)]
    kinds = df.groupby("exercise")["kind"].first()
    metric = metric or plots.default_metric(kinds.get(names[0], "loaded"))
    events = metrics.pr_events(df, formula)
    histories = {n: _since(console, metrics.exercise_history(df, n, formula), "session", since) for n in names}
    shown = _since(console, df, "session", since)
    fig = plots.plot_progress(histories, events, metric=metric, trend=trend, split=split,
                              footnotes=footnotes(a, shown, loads=metric not in ("reps", "seconds", "assistance")))
    name = f"progress_{slug(names[0])}" + (f"-and-{len(names) - 1}-more" if len(names) > 1 else "")
    _output(console, fig, name, out, show)


@plot_app.command()
def volume(
    ctx: typer.Context,
    by: str = typer.Option("week", "--by", help="week or month."),
    group: str = typer.Option("workout", "--group", help="workout (stack by workout name) or none."),
    since: str | None = typer.Option(None, "--since", help=SINCE_HELP),
    include_warmups: bool = typer.Option(False, "--include-warmups", help="Count warm-up sets too."),
    raw: bool = typer.Option(False, "--raw", help="Ignore corrections."),
    out: Path | None = typer.Option(None, "--out", help=OUT_HELP),
    show: bool = typer.Option(False, "--show", help=SHOW_HELP),
):
    """Weekly or monthly training volume, optionally stacked by workout."""
    from . import plots
    from .cli import _fail

    if by not in ("week", "month"):
        _fail(_console(ctx), "--by must be week or month.")
    if group not in ("workout", "none"):
        _fail(_console(ctx), "--group must be workout or none.")
    state, console, a = _setup(ctx, raw, include_warmups)
    df = _since(console, a.df, "session", since)
    notes = footnotes(a, df, sessions=True)
    if include_warmups:
        notes.append("Warm-up sets included.")
    fig = plots.plot_volume(df, by=by, group=None if group == "none" else group, footnotes=notes)
    _output(console, fig, f"volume-{by}", out, show)


@plot_app.command()
def calendar(
    ctx: typer.Context,
    since: str | None = typer.Option(None, "--since", help=SINCE_HELP),
    weeks: int | None = typer.Option(None, "--weeks", min=1, help="Show at most this many recent weeks (default 53)."),
    raw: bool = typer.Option(False, "--raw", help="Ignore corrections."),
    out: Path | None = typer.Option(None, "--out", help=OUT_HELP),
    show: bool = typer.Option(False, "--show", help=SHOW_HELP),
):
    """GitHub-style heatmap of training days, coloured by volume."""
    from . import metrics, plots

    state, console, a = _setup(ctx, raw)
    df = _since(console, a.df, "session", since)
    sessions = _since(console, metrics.sessions_frame(a.sessions), "session", since)
    fig = plots.plot_calendar(df, sessions, weeks=weeks, footnotes=footnotes(a, df, sessions=True))
    _output(console, fig, "calendar", out, show)


@plot_app.command()
def duration(
    ctx: typer.Context,
    since: str | None = typer.Option(None, "--since", help=SINCE_HELP),
    raw: bool = typer.Option(False, "--raw", help="Ignore corrections (durations are then all trusted)."),
    out: Path | None = typer.Option(None, "--out", help=OUT_HELP),
    show: bool = typer.Option(False, "--show", help=SHOW_HELP),
):
    """Session duration over time (untrusted durations left out)."""
    from . import metrics, plots

    state, console, a = _setup(ctx, raw)
    sessions = _since(console, metrics.sessions_frame(a.sessions), "session", since)
    df = _since(console, a.df, "session", since)
    notes = [n for n in footnotes(a, df, sessions=True, loads=False) if "corrected set" not in n
             and "exercise merge" not in n]
    fig = plots.plot_duration(sessions, footnotes=notes)
    _output(console, fig, "duration", out, show)


@plot_app.command()
def frequency(
    ctx: typer.Context,
    top: int = typer.Option(20, "--top", help="How many exercises to show."),
    since: str | None = typer.Option(None, "--since", help=SINCE_HELP),
    raw: bool = typer.Option(False, "--raw", help="Ignore corrections."),
    out: Path | None = typer.Option(None, "--out", help=OUT_HELP),
    show: bool = typer.Option(False, "--show", help=SHOW_HELP),
):
    """Sessions per exercise, most-done first; lifts not done lately are greyed."""
    from . import metrics, plots
    from .cli import _fail

    if top < 1:
        _fail(_console(ctx), "--top must be at least 1.")
    state, console, a = _setup(ctx, raw)
    df = _since(console, a.df, "session", since)
    notes = [n for n in footnotes(a, df, sessions=True, loads=False) if "corrected set" not in n]
    fig = plots.plot_frequency(metrics.exercise_summary(df), top=top, footnotes=notes)
    _output(console, fig, "frequency", out, show)


@plot_app.command("rep-prs")
def rep_prs(
    ctx: typer.Context,
    exercise: str = typer.Argument(..., help="Exercise name (or a unique part of it)."),
    raw: bool = typer.Option(False, "--raw", help="Ignore corrections."),
    out: Path | None = typer.Option(None, "--out", help=OUT_HELP),
    show: bool = typer.Option(False, "--show", help=SHOW_HELP),
):
    """Best load at each rep count 1–15, now vs. 30 days before your latest session."""
    from . import plots
    from .cli import _resolve_exercise

    state, console, a = _setup(ctx, raw)
    merged = {} if raw else state.store.active_merges()
    name = _resolve_exercise(console, exercise, a.df["exercise"], merged)
    current, previous, cutoff = plots.rep_pr_curves(a.df, name)
    df = a.df[a.df["exercise"] == name]
    fig = plots.plot_rep_prs(current, previous, exercise=name, cutoff=cutoff, footnotes=footnotes(a, df))
    _output(console, fig, f"rep-prs_{slug(name)}", out, show)


@plot_app.command()
def dashboard(
    ctx: typer.Context,
    formula: str = typer.Option("epley", "--formula", help="e1RM formula: epley or brzycki."),
    raw: bool = typer.Option(False, "--raw", help="Ignore corrections."),
    out: Path | None = typer.Option(None, "--out", help=OUT_HELP),
    show: bool = typer.Option(False, "--show", help=SHOW_HELP),
):
    """One-glance summary: headline numbers, calendar, volume, duration, top lifts, frequency."""
    from . import metrics, plots
    from .cli import _check_formula

    state, console, a = _setup(ctx, raw)
    _check_formula(console, formula)
    sessions = metrics.sessions_frame(a.sessions)
    fig = plots.plot_dashboard(a.df, sessions, metrics.pr_events(a.df, formula), formula=formula,
                               footnotes=footnotes(a, a.df, sessions=True))
    _output(console, fig, "dashboard", out, show)


def parse_target(text: str) -> tuple[float, float] | None:
    """'10-20' → (10.0, 20.0); 'none' or 'off' → None. Raises ValueError otherwise."""
    t = text.strip().lower().replace("–", "-").replace(" ", "")
    if t in ("none", "off", "no"):
        return None
    lo, sep, hi = t.partition("-")
    if not sep:
        raise ValueError(text)
    low, high = float(lo), float(hi)
    if not (0 <= low < high) or high == float("inf"):
        raise ValueError(text)
    return low, high


def _muscle_names(console: Console, queries: list[str], present) -> list[str]:
    """Catalog muscle names for ``--muscle`` values ('front delts', 'Front_Delts' and 'front-delts' all work)."""
    from .catalog import MUSCLES
    from .cli import _fail, esc

    known = list(MUSCLES) + sorted(set(present) - set(MUSCLES))
    out = []
    for q in queries:
        key = q.strip().lower().replace(" ", "_").replace("-", "_")
        matches = [m for m in known if m == key] or [m for m in known if key and key in m]
        if len(matches) != 1:
            what = "matches several muscles" if matches else "isn't a muscle group"
            _fail(console, f"{esc(repr(q))} {what}; use one of: {esc(', '.join(m.replace('_', ' ') for m in known))}.")
        out.append(matches[0])
    return list(dict.fromkeys(out))


@plot_app.command("sets-per-muscle")
def sets_per_muscle(
    ctx: typer.Context,
    by: str = typer.Option("week", "--by", help="week or month (month shows the average sets per week)."),
    since: str | None = typer.Option(None, "--since", help=SINCE_HELP),
    muscle: list[str] = typer.Option([], "--muscle", "-m",
                                     help="Only this muscle group, e.g. chest or 'front delts'. Repeatable."),
    heatmap: bool = typer.Option(False, "--heatmap", help="A muscle × week grid instead of lines."),
    target: str = typer.Option("10-20", "--target", help="Sets per week to shade as the target, or 'none'."),
    raw: bool = typer.Option(False, "--raw", help="Ignore corrections."),
    out: Path | None = typer.Option(None, "--out", help=OUT_HELP),
    show: bool = typer.Option(False, "--show", help=SHOW_HELP),
):
    """Hard sets per muscle group per week, against a 10–20 sets/week target (see fitness muscles)."""
    from . import metrics, plots
    from .cli import _fail, esc

    console = _console(ctx)
    if by not in ("week", "month"):
        _fail(console, "--by must be week or month.")
    try:
        band = parse_target(target)
    except ValueError:
        _fail(console, f"--target expects a range like 10-20 (or none); got {esc(repr(target))}.")
    state, console, a = _setup(ctx, raw)
    catalog = state.store.catalog()
    df = _since(console, a.df, "session", since)
    wms = metrics.weekly_muscle_sets(df, catalog, by)
    names = _muscle_names(console, muscle, wms["muscle"].unique()) if muscle else None

    notes = []
    today = pd.Timestamp.now()
    if not wms.empty:
        latest = wms["period"].max()
        if latest.start_time <= today <= latest.end_time:
            wms = wms[wms["period"] != latest]  # a partial period would look like a drop-off
            notes.append(f"This {by} is still in progress and left out.")
    if by == "month" and not wms.empty:
        weeks = wms["period"].map(lambda p: p.days_in_month / 7)
        wms = wms.assign(sets=wms["sets"] / weeks.astype(float))
    hard = df[df["set_type"].isin(metrics.HARD_SET_TYPES)] if not df.empty else df
    guessed = sorted(n for n in set(hard["exercise"]) if catalog.is_guessed(n)) if not hard.empty else []
    if guessed:
        notes.append(f"Muscles for {_plural(len(guessed), 'exercise')} are keyword guesses; "
                     "see fitness exercises --unmapped.")
    notes.append("Hard sets (normal, failure, drop): 1 per primary muscle, 0.5 per secondary.")
    notes += footnotes(a, df, loads=False)

    pretty = wms.assign(muscle=wms["muscle"].str.replace("_", " "))
    title = "Sets per muscle per week" if by == "week" else "Average weekly sets per muscle, by month"
    fig = plots.plot_sets_per_muscle(
        pretty, kind="heatmap" if heatmap else "lines", target=band,
        muscles=[n.replace("_", " ") for n in names] if names else None, title=title, footnotes=notes,
    )
    _output(console, fig, f"sets-per-muscle-{by}", out, show)


# --- used by `fitness merge review` ------------------------------------------------

def save_merge_previews(console: Console, kept, others) -> list[Path]:
    """Save a chart per exercise being combined into ``kept`` (``cli.Member``s); print the paths.

    ``others`` is ``[(Member, offset, factor), ...]`` as for ``cli._print_preview``.
    """
    from . import plots

    paths = []
    for m, offset, factor in others:
        fig = plots.plot_merge_preview(kept.sets, m.sets, offset=offset, factor=factor,
                                       kept_name=kept.name, other_name=m.name)
        path = save_figure(fig, default_path(f"merge-preview_{slug(m.name)}"))
        console.print(f"Chart preview saved to {escape(str(path))}")
        paths.append(path)
    return paths
