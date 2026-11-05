"""CLI commands for exercise metadata and bodyweight: ``muscles``, ``import-health``, ``bodyweight``.

These register themselves on ``cli.app`` when ``cli`` imports this module (at
the end of ``cli.py``), so nothing here runs at import time except decorators.
"""

from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import metrics
from .catalog import MUSCLES, Catalog
from .cli import RAW_HELP, _analysis, _date, _fail, _filter_dates, _num, _plural, _state, app
from .importers.apple_health import HealthParseError, parse_apple_health

LOW_SETS, HIGH_SETS = 10, 20  # weekly hard sets: a common hypertrophy target range


# --- shared helpers (also used by `fitness exercises`) ---------------------------

def muscles_label(catalog: Catalog, exercise: str) -> str:
    """Primary muscles, compact. Guesses end in ``?``; ``—`` when nothing is known."""
    primary, _ = catalog.muscles(exercise)
    if catalog.has_mapping(exercise):
        return escape(", ".join(primary)) if primary else "[dim]none[/dim]"
    if primary:
        return f"[italic]{escape(', '.join(primary))}?[/italic]"
    return "[dim]—[/dim]"


def print_unmapped(console: Console, df: pd.DataFrame, catalog: Catalog, search: Optional[str] = None) -> None:
    names = sorted(set(df["exercise"]))
    if search:
        names = [n for n in names if search.lower() in n.lower()]
    missing = catalog.unmapped(names)
    if not missing:
        console.print(f"Every exercise ({len(names)}) has muscle groups in config/exercises.toml.")
        return
    sessions = df.groupby("exercise")["session"].nunique()
    table = Table(box=None, header_style="bold", pad_edge=False)
    for col in ("Exercise", "Sessions", "Guess: primary", "Secondary"):
        table.add_column(col, justify="right" if col == "Sessions" else "left")
    for name in missing:
        guess = catalog.muscles(name) if catalog.is_guessed(name) else None
        table.add_row(escape(name), str(sessions.get(name, 0)),
                      ", ".join(guess[0]) if guess else "[dim]no guess[/dim]",
                      ", ".join(guess[1]) if guess else "")
    console.print(table)
    console.print(
        f"[dim]{_plural(len(missing), 'exercise')} without an entry. Guesses are used for "
        "fitness muscles until you add an entry, for example:[/dim]"
    )
    name = missing[0]
    p, s = catalog.muscles(name)
    console.print(escape(f'  [exercise."{name}"]\n  primary = {_toml_list(p)}\n  secondary = {_toml_list(s)}'))
    console.print(f"[dim]Muscle names: {', '.join(MUSCLES)}[/dim]")
    _print_catalog_warnings(console, catalog)


def _toml_list(xs: list[str]) -> str:
    return "[" + ", ".join(f'"{x}"' for x in xs) + "]"


def _print_catalog_warnings(console: Console, catalog: Catalog) -> None:
    for w in catalog.warnings:
        console.print(f"[yellow]warning:[/yellow] config/exercises.toml: {escape(w)}")


# --- muscles ---------------------------------------------------------------------

@app.command()
def muscles(
    ctx: typer.Context,
    by: str = typer.Option("week", "--by", help="week or month."),
    last: int = typer.Option(8, "--last", "-n", help="Show the most recent N periods."),
    all_: bool = typer.Option(False, "--all", help="Show every period."),
    since: Optional[str] = typer.Option(None, "--since", help="Only sessions on or after this date."),
    raw: bool = typer.Option(False, "--raw", help=RAW_HELP),
):
    """Hard sets per muscle group per week (or month), against a 10-20 sets/week target."""
    state = _state(ctx)
    console = state.console
    if by not in ("week", "month"):
        _fail(console, "--by must be week or month.")
    a = _analysis(state, raw=raw)
    catalog = state.store.catalog()
    df = _filter_dates(a.df, "session", since, None)
    if df.empty:
        console.print("No sessions in that range.")
        return
    wms = metrics.weekly_muscle_sets(df, catalog, by)
    if wms.empty:
        console.print("No hard sets for exercises with known muscles. See [bold]fitness exercises --unmapped[/bold].")
        return

    grid = wms.pivot(index="muscle", columns="period", values="sets")
    grid = grid.loc[[m for m in MUSCLES if m in grid.index] + [m for m in grid.index if m not in MUSCLES]]
    periods = list(grid.columns)
    total = len(periods)
    if not all_ and last > 0:
        periods = periods[-last:]
    today = pd.Timestamp(datetime.now())
    in_progress = {p for p in periods if p.start_time <= today <= p.end_time}
    complete = [p for p in periods if p not in in_progress]

    def label(p) -> str:
        text = f"{p.start_time:%m-%d}" if by == "week" else f"{p.start_time:%b %y}"
        return text + ("*" if p in in_progress else "")

    def bounds(p) -> tuple[float, float]:
        weeks = 1.0 if by == "week" else p.days_in_month / 7
        return LOW_SETS * weeks, HIGH_SETS * weeks

    table = Table(box=None, header_style="bold", pad_edge=False)
    table.add_column("Muscle")
    for p in periods:
        table.add_column(label(p), justify="right")
    table.add_column("Avg", justify="right", style="bold")
    for muscle, row in grid.iterrows():
        cells = []
        for p in periods:
            v = row[p]
            low, high = bounds(p)
            text = _num(v)
            if v == 0:
                text = f"[dim]{text}[/dim]"
            elif p in in_progress:
                pass  # partial period: no verdict yet
            elif v < low:
                text = f"[yellow]{text}[/yellow]"
            elif v > high:
                text = f"[magenta]{text}[/magenta]"
            cells.append(text)
        avg_over = complete or periods
        avg = row[avg_over].mean()
        table.add_row(escape(muscle.replace("_", " ")), *cells, f"{avg:.1f}")
    console.print(table)

    unit = "a week" if by == "week" else "a week (scaled to each month's length)"
    console.print(
        f"[dim]Hard sets = normal, failure and drop sets; each counts 1 for primary muscles and 0.5 for "
        f"secondary.[/dim] [yellow]Yellow[/yellow][dim]: under {LOW_SETS}[/dim], "
        f"[magenta]magenta[/magenta][dim]: over {HIGH_SETS} sets {unit} (a common hypertrophy range).[/dim]"
    )
    notes = []
    if in_progress:
        notes.append(f"* this {by} is still in progress (not highlighted or averaged)")
    if len(periods) != total:
        notes.append(f"showing the last {len(periods)} of {total} {by}s (--all for every {by})")
    if notes:
        console.print(f"[dim]{'; '.join(notes).capitalize()}.[/dim]")

    hard = df[df["set_type"].isin(metrics.HARD_SET_TYPES)]
    names = sorted(set(hard["exercise"]))
    guessed = [n for n in names if catalog.is_guessed(n)]
    uncounted = [n for n in names if not catalog.has_mapping(n) and not catalog.is_guessed(n)]
    if guessed:
        shown = "; ".join(f"{n} → {', '.join(catalog.muscles(n)[0])}" for n in guessed[:6])
        more = f"; and {len(guessed) - 6} more" if len(guessed) > 6 else ""
        console.print(
            f"[yellow]Guessed muscles[/yellow] for {_plural(len(guessed), 'exercise')} not in "
            f"config/exercises.toml: {escape(shown)}{more}. See fitness exercises --unmapped"
        )
    if uncounted:
        console.print(f"[dim]Not counted (no muscles known): {escape(', '.join(uncounted))}.[/dim]")
    _print_catalog_warnings(console, catalog)


# --- Apple Health bodyweight -----------------------------------------------------

def _lb1(x: float) -> str:
    return f"{x:,.1f} lb"


@app.command("import-health")
def import_health(
    ctx: typer.Context,
    file: Path = typer.Argument(..., exists=True, dir_okay=False,
                                help="Apple Health export.zip (or the export.xml inside it)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask before dropping days missing from the export."),
):
    """Import bodyweight from Apple Health. Each export holds your full history and replaces stored entries."""
    state = _state(ctx)
    console = state.console
    try:
        result = parse_apple_health(file)
    except (HealthParseError, zipfile.BadZipFile, OSError) as exc:
        _fail(console, f"{escape(str(file))}: {escape(str(exc))}")
    s = result.stats
    units = ", ".join(f"{n:,} in {u}" for u, n in sorted(s.units.items()))
    line = f"Read {_plural(s.records, 'body-mass record')}"
    if units:
        line += f" ({units})"
    if result.days:
        line += f" on {_plural(len(result.days), 'day')}, {s.first} → {s.last}"
    console.print(line + f"; {s.other_records:,} other Health records ignored.")
    if s.invalid:
        console.print(f"[yellow]{_plural(s.invalid, 'record')} skipped:[/yellow]")
        for w in result.warnings:
            console.print(f"  {escape(w)}")
    if not result.days:
        _fail(console, "no usable bodyweight readings in this export; nothing imported.")
    multi = sum(1 for d in result.days if d.readings > 1)
    if multi:
        console.print(f"[dim]{_plural(multi, 'day')} with several readings use that day's average.[/dim]")

    store = state.store
    plan = store.plan_bodyweight_import(result.days, file)
    if not plan.has_changes:
        console.print(f"No changes: this export matches the stored bodyweight ({_plural(plan.unchanged, 'day')}).")
        return
    if plan.is_first_import:
        console.print(f"First Health import: {_plural(len(plan.new), 'day')} of bodyweight.")
    else:
        console.print(
            f"Changes since last Health import: {len(plan.new)} new, {len(plan.changed)} changed, "
            f"{len(plan.removed)} removed, {plan.unchanged:,} unchanged."
        )
        limit = 10
        for d in plan.new[-limit:]:
            console.print(f"  [green]new[/green]     {d.day}  {_lb1(d.lb)}")
        if len(plan.new) > limit:
            console.print(f"  [dim]… and {len(plan.new) - limit} earlier new days[/dim]")
        for old, new in plan.changed[:limit]:
            console.print(f"  [yellow]changed[/yellow] {new.day}  {_lb1(old.lb)} → {_lb1(new.lb)}")
        if len(plan.changed) > limit:
            console.print(f"  [dim]… and {len(plan.changed) - limit} more changed days[/dim]")
        for d in plan.removed[:limit]:
            console.print(f"  [red]removed[/red] {d.day}  {_lb1(d.lb)} (deleted in Health?)")
        if len(plan.removed) > limit:
            console.print(f"  [dim]… and {len(plan.removed) - limit} more removed days[/dim]")

    if plan.removed and not yes:
        if not typer.confirm(f"Drop {_plural(len(plan.removed), 'day')} that are no longer in the export?",
                             default=False):
            console.print("Import cancelled; nothing changed.")
            raise typer.Exit(1)

    store.apply_bodyweight_import(plan)
    latest = plan.incoming[-1]
    console.print(
        f"[bold]Imported.[/bold] {_plural(len(plan.incoming), 'day')} of bodyweight stored "
        f"({plan.incoming[0].day} → {latest.day}, latest {_lb1(latest.lb)})."
    )
    console.print("Assisted-lift loads now use these entries instead of the placeholder. See [bold]fitness bodyweight[/bold].")


@app.command()
def bodyweight(
    ctx: typer.Context,
    last: int = typer.Option(14, "--last", "-n", help="Show the most recent N entries (0 for all)."),
):
    """Logged bodyweight (from Apple Health), and whether the placeholder is still in use."""
    state = _state(ctx)
    console = state.console
    placeholder = state.settings.bodyweight_lb
    days = state.store.bodyweight_days()
    if not days:
        console.print(
            f"No bodyweight entries yet. Assisted-lift loads use the [bold]placeholder {_num(placeholder)} lb[/bold] "
            "(change with: fitness config set bodyweight_lb N)."
        )
        console.print("Import real weights from Apple Health with [bold]fitness import-health export.zip[/bold].")
        return

    from .bodyweight import Bodyweight

    bw = Bodyweight(placeholder, state.store.bodyweight_entries())
    lo = min(days, key=lambda d: d.lb)
    hi = max(days, key=lambda d: d.lb)
    latest = days[-1]
    console.print(
        f"[bold]{_plural(len(days), 'day')}[/bold] of bodyweight, {days[0].day} → {latest.day}. "
        f"Latest {_lb1(latest.lb)} ({latest.day})."
    )
    console.print(f"Lowest {_lb1(lo.lb)} ({lo.day})  ·  highest {_lb1(hi.lb)} ({hi.day})")
    trends = []
    for n in (30, 90, 365):
        change = bw.change_over(n)
        if change is not None:
            trends.append(f"{change:+.1f} lb vs {n} days earlier")
    if trends:
        console.print("Latest " + ", ".join(trends) + ".")

    shown = days if last <= 0 else days[-last:]
    table = Table(box=None, header_style="bold", pad_edge=False)
    for col in ("Date", "Weight", "Change", "Source"):
        table.add_column(col, justify="right" if col in ("Weight", "Change") else "left")
    index = {d.day: i for i, d in enumerate(days)}
    for d in shown:
        i = index[d.day]
        change = "" if i == 0 else f"{d.lb - days[i - 1].lb:+.1f}"
        table.add_row(f"{d.day:%a %Y-%m-%d}", _lb1(d.lb), change, escape(d.source))
    console.print(table)
    if len(shown) != len(days):
        console.print(f"[dim]Showing the last {len(shown)} of {len(days)} entries (--last 0 for all).[/dim]")
    console.print(
        f"[dim]The placeholder ({_num(placeholder)} lb) is not in use: assisted-lift loads use the latest entry "
        "on or before each workout (the first entry for earlier workouts).[/dim]"
    )
    info = state.store.last_health_import()
    if info:
        when = _date(pd.Timestamp(info["imported_at"]))
        console.print(f"[dim]Last imported {when} from {escape(info['source_path'])}.[/dim]")
