"""Command-line interface. The only module that talks to the terminal."""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text
from typer.core import TyperGroup

from . import __version__, merges, metrics, outliers, similarity
from . import corrections as corr
from . import settings as settings_mod
from .bodyweight import Bodyweight
from .catalog import ASSISTED, LOADED, TIMED, UNLOADED
from .corrections import Correction, ReconcileResult, format_duration
from .fixes import describe_set
from .importers import StrongParseError, parse_strong_csv
from .models import ExerciseEntry, SetType, WorkoutSet
from .sessions import Session, workout_key
from .settings import Settings, SettingsError
from .store import ImportPlan, Store, StoreError, default_db_path


class _FriendlyErrors(TyperGroup):
    """Turn database and settings errors raised anywhere in a command into one ``Error:`` line.

    ``State.store`` and ``State.settings`` already catch errors when opening; this catches the
    rest (a write to a read-only database, a page of a damaged one that only fails when read).
    """

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except (StoreError, SettingsError, sqlite3.DatabaseError) as exc:
            state = ctx.obj if isinstance(ctx.obj, State) else None
            console = state.console if state else Console(highlight=False)
            message = str(exc)
            if isinstance(exc, sqlite3.DatabaseError) and state:
                message = f"database {state.db_path}: {message}"
            _fail(console, esc(message))


app = typer.Typer(
    cls=_FriendlyErrors,
    help="Track weightlifting statistics from Strong app exports.",
    no_args_is_help=True,
    add_completion=False,
)
workouts_app = typer.Typer(help="List workouts, or undo an automatic workout merge.")
corrections_app = typer.Typer(help="Show, revert, or restore corrections applied to your data.")
config_app = typer.Typer(help="Show or change settings.", no_args_is_help=True)
merge_app = typer.Typer(help="Combine similar exercises (e.g. the same machine at two gyms), or keep them separate.",
                        no_args_is_help=True)
app.add_typer(merge_app, name="merge")
app.add_typer(workouts_app, name="workouts")
app.add_typer(corrections_app, name="corrections")
app.add_typer(config_app, name="config")

TYPE_LABELS = {
    corr.WORKOUT_MERGE: ("workout merged", "workouts merged"),
    corr.DURATION_SUSPECT: ("duration excluded", "durations excluded"),
    corr.VALUE_FIX: ("set fix", "set fixes"),
    corr.EXERCISE_MERGE: ("exercise combined", "exercises combined"),
}
PR_LABELS = {"e1rm": "e1RM", "weight": "Heaviest", "rep": "Rep PR", "seconds": "Longest hold", "reps": "Most reps"}
SET_LABELS = {SetType.WARMUP: "W", SetType.DROP: "D", SetType.FAILURE: "F"}


# --- shared state ------------------------------------------------------------

@dataclass
class State:
    db_path: Path
    config_path: Path | None
    console: Console
    _store: Store | None = None
    _settings: Settings | None = None

    @property
    def store(self) -> Store:
        if self._store is None:
            try:
                self._store = Store(self.db_path)
            except StoreError as exc:
                _fail(self.console, esc(exc))
        return self._store

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            try:
                self._settings = settings_mod.load(self.config_path)
            except SettingsError as exc:
                _fail(self.console, esc(exc))
        return self._settings


def _state(ctx: typer.Context) -> State:
    return ctx.find_root().obj


def _fail(console: Console, message: str, code: int = 1):
    """Print one error line and exit. ``message`` is Rich markup: wrap user data in ``esc()``."""
    message = message.rstrip()
    if not message.endswith((".", "!", "?", "…")):
        message += "."  # errors from lower layers come without a full stop
    console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(code)


def _require_workouts(state: State) -> None:
    """Fail early, with the same hint as the read-only commands, when nothing is imported yet."""
    if not state.store.workout_count():
        _fail(state.console, "no workouts yet. Run [bold]fitness import FILE[/bold] first.")


def esc(value) -> str:
    """User data (exercise and workout names, notes, reasons, paths) for a Rich markup string.

    Strong lets you type anything, so a name like ``Curl [/x]`` would otherwise be parsed as
    markup: it crashes Rich, and ``[green]`` silently disappears.
    """
    return escape(str(value))


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"fitness_aggregator {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    db: Path | None = typer.Option(
        None, "--db", help="Database file (default: $FITNESS_AGGREGATOR_DB, else ~/.fitness_aggregator/lifts.db)."
    ),
    config: Path | None = typer.Option(
        None, "--config", help="Settings file (default: $FITNESS_AGGREGATOR_CONFIG, else "
        "./config/settings.toml if present, else ~/.fitness_aggregator/settings.toml)."
    ),
    version: bool = typer.Option(False, "--version", callback=_print_version, is_eager=True,
                                 help="Show the version and exit."),
):
    ctx.obj = State(db_path=db or default_db_path(), config_path=config, console=Console(highlight=False))
    ctx.call_on_close(lambda: ctx.obj._store and ctx.obj._store.close())


# --- formatting helpers ----------------------------------------------------

def _num(x: float) -> str:
    return f"{x:g}"


def _when(dt: datetime) -> str:
    return f"{dt:%a %Y-%m-%d %H:%M}"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _correction_counts(corrections: list[Correction]) -> str:
    counts = Counter(c.type for c in corrections if c.is_active)
    parts = []
    for type_, n in sorted(counts.items()):
        one, many = TYPE_LABELS.get(type_, (type_, type_))
        parts.append(f"{n} {one if n == 1 else many}")
    return ", ".join(parts) or "none"


def _print_reconcile(console: Console, result: ReconcileResult, store: Store) -> None:
    ids = {c.key: c.id for c in store.corrections()}
    for p in result.insert:
        console.print(f"  [green]+[/green] #{ids.get(p.key, '?')} {esc(p.reason)}")
    for current, p in result.reactivate:
        console.print(f"  [green]↺[/green] #{current.id} applies again: {esc(p.reason)}")
    for current, p in result.update:
        note = " (still reverted)" if current.status == corr.REVERTED else ""
        console.print(f"  [yellow]~[/yellow] #{current.id} updated{note}: {esc(p.reason)}")
    for current in result.obsolete:
        console.print(f"  [dim]-[/dim] #{current.id} no longer applies: {esc(current.reason)}")
    for current in result.stale:
        console.print(
            f"  [red]![/red] #{current.id} is stale (the imported data changed, so it's no longer applied): "
            f"{esc(current.reason)}"
        )
    for current in result.unstale:
        console.print(f"  [green]↺[/green] #{current.id} matches the data again and is applied: "
                      f"{esc(current.reason)}")


def _print_parse_summary(console: Console, plan: ImportPlan) -> None:
    s = plan.result.stats
    types = s.set_types
    console.print(
        f"Parsed {_plural(s.rows, 'row')}: {_plural(s.workouts, 'workout')}, {_plural(s.sets, 'set')} "
        f"({types[SetType.NORMAL]:,} normal, {types[SetType.WARMUP]:,} warm-up, "
        f"{types[SetType.DROP]:,} drop, {types[SetType.FAILURE]:,} failure), "
        f"{_plural(s.exercise_names, 'exercise name')}."
    )
    rest = f"{_plural(s.rest_timers_attached, 'rest timer')} attached to sets"
    if s.rest_timers_orphaned:
        rest += f", {s.rest_timers_orphaned:,} ignored (no preceding set)"
    console.print(rest + ".")
    for w in plan.result.warnings:
        console.print(f"  [yellow]warning:[/yellow] {esc(w)}")


def _set_row(s: WorkoutSet, show_rpe: bool) -> list[str]:
    label = _set_label(s)
    if s.seconds and not s.weight and not s.reps:
        weight, amount = "", f"{_num(s.seconds)} s"
    elif s.weight:
        weight, amount = f"{_num(s.weight)} lb", f"× {_num(s.reps)}"
    else:
        weight, amount = "—", f"× {_num(s.reps)}"
    notes = s.notes
    if s.corrected_from:
        amount += " [yellow]*[/yellow]"
        was = f"was {describe_set(s, s.corrected_from)} (correction #{', #'.join(map(str, s.correction_ids))})"
        notes = f"{notes}; {was}" if notes else was
    row = [label, weight, amount, format_duration_seconds(s.rest_seconds)]
    if show_rpe:
        row.append("" if s.rpe is None else f"RPE {_num(s.rpe)}")
    row.append(esc(notes))
    return row


def _set_label(s: WorkoutSet) -> str:
    return str(s.number) if s.set_type is SetType.NORMAL else SET_LABELS[s.set_type]


def format_duration_seconds(seconds: float | None) -> str:
    if seconds is None:
        return ""
    minutes, secs = divmod(int(seconds), 60)
    if minutes and secs:
        return f"{minutes}m {secs}s"
    return f"{minutes}m" if minutes else f"{secs}s"


def _session_duration(session: Session) -> str:
    if session.duration is not None:
        return format_duration(session.duration)
    recorded = [m.duration for m in session.members if m.duration is not None]
    if recorded:
        return f"[dim]({format_duration(sum(recorded, start=timedelta()))})[/dim]"
    return "[dim]—[/dim]"


# --- import -----------------------------------------------------------------

@app.command("import")
def import_cmd(
    ctx: typer.Context,
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Strong CSV export."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask before dropping workouts missing from the export."),
    force: bool = typer.Option(False, "--force", help="Import even if this export is older than the stored data."),
):
    """Import a Strong export. Each export holds your full history and replaces the stored data."""
    state = _state(ctx)
    console = state.console
    try:
        result = parse_strong_csv(file)
    except StrongParseError as exc:
        _fail(console, f"{esc(file)}: {esc(exc)}")

    store = state.store
    plan = store.plan_import(result, file)
    _print_parse_summary(console, plan)

    if plan.is_older and not force:
        _fail(
            console,
            f"this export's latest workout ({plan.file_latest:%Y-%m-%d %H:%M}) is older than what's "
            f"already stored ({plan.stored_latest:%Y-%m-%d %H:%M}). Importing it would look like recent "
            "workouts were deleted. Use --force if you really mean to.",
        )

    if not plan.has_changes:
        console.print(
            f"No changes: this export matches the stored data ({_plural(plan.unchanged, 'workout')})."
        )
        return

    if plan.is_first_import:
        console.print(f"First import: {_plural(len(plan.new), 'workout')}.")
    else:
        console.print(
            f"Changes since last import: {len(plan.new)} new, {len(plan.edited)} edited, "
            f"{len(plan.removed)} removed, {plan.unchanged:,} unchanged."
        )
        for w in plan.new:
            console.print(f"  [green]new[/green]     {_when(w.started_at)}  {esc(w.name)}")
        for e in plan.edited:
            console.print(f"  [yellow]edited[/yellow]  {_when(e.new.started_at)}  {esc(e.new.name)}: "
                          f"{esc(e.description)}")
        for w in plan.removed:
            console.print(f"  [red]removed[/red] {_when(w.started_at)}  {esc(w.name)} (deleted in Strong?)")

    if plan.removed and not yes:
        if not typer.confirm(
            f"Drop {_plural(len(plan.removed), 'workout')} that are no longer in the export?", default=False
        ):
            console.print("Import cancelled; nothing changed.")
            raise typer.Exit(1)

    report = store.apply_import(plan, state.settings, allow_removals=True, force=force)
    console.print(f"[bold]Imported.[/bold] {_plural(store.workout_count(), 'workout')} stored.")
    if report.corrections.changed:
        console.print("Corrections changed by this import:")
        _print_reconcile(console, report.corrections, store)
    console.print(
        f"Corrections applied: {_correction_counts(store.corrections())}. "
        "See [bold]fitness corrections[/bold]."
    )
    _print_flag_count(console, store)


def _print_flag_count(console: Console, store: Store) -> None:
    flags = store.open_flags()
    if flags:
        console.print(
            f"[yellow]{_plural(len(flags), 'set')} flagged as possible data-entry errors.[/yellow] "
            "Review with [bold]fitness check[/bold]."
        )
    candidates = store.merge_candidates()
    if candidates:
        console.print(
            f"{_plural(len(candidates), 'group')} of similar exercises to review "
            "(combine or keep separate): [bold]fitness merge review[/bold]."
        )


# --- workouts ---------------------------------------------------------------

def _parse_date(value: str, option: str = "--since") -> datetime:
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        raise typer.BadParameter(f"expected a date like 2026-06-02, got {value!r}", param_hint=option) from None


@workouts_app.callback(invoke_without_command=True)
def workouts_list(
    ctx: typer.Context,
    last: int = typer.Option(20, "--last", "-n", min=0, help="Show the most recent N sessions (0 for all)."),
    all_: bool = typer.Option(False, "--all", help="Show every session."),
    since: str | None = typer.Option(None, "--since", help="Only sessions on or after this date."),
    raw: bool = typer.Option(False, "--raw", help="Ignore corrections (no merging, recorded durations)."),
):
    """List sessions, most recent last."""
    if ctx.invoked_subcommand:
        return
    state = _state(ctx)
    sessions = state.store.sessions(raw=raw)
    if not sessions:
        state.console.print("No workouts yet. Run [bold]fitness import FILE[/bold] first.")
        return
    if since:
        cutoff = _parse_date(since)
        sessions = [s for s in sessions if s.started_at >= cutoff]
    total = len(sessions)
    if not all_ and last > 0:
        sessions = sessions[-last:]

    table = Table(box=None, header_style="bold", pad_edge=False)
    for col, justify in [("Date", "left"), ("Name", "left"), ("Duration", "right"),
                         ("Exercises", "right"), ("Sets", "right"), ("", "left")]:
        table.add_column(col, justify=justify)
    for s in sessions:
        flags = []
        if s.is_merged:
            flags.append(f"merged ×{len(s.members)}")
        if any(c.type == corr.DURATION_SUSPECT for c in s.applied):
            flags.append("duration excluded")
        table.add_row(
            _when(s.started_at), esc(s.name), _session_duration(s), str(len(s.exercises)),
            str(s.set_count), "[dim]" + ", ".join(flags) + "[/dim]" if flags else "",
        )
    state.console.print(table)
    shown = f"{len(sessions)} of {total}" if len(sessions) != total else str(total)
    note = " (raw: no corrections applied)" if raw else ""
    state.console.print(f"[dim]{shown} sessions{note}. Durations in (parentheses) are excluded from stats.[/dim]")


def _match_sessions(sessions: list[Session], when: str) -> list[Session]:
    if when == "last":
        return sessions[-1:]
    text = when.replace(" ", "T")
    matches = [s for s in sessions if any(workout_key(m.started_at).startswith(text) for m in s.members)]
    return matches


@workouts_app.command("unmerge")
def workouts_unmerge(
    ctx: typer.Context,
    when: str = typer.Argument(..., help="Start time of the merged-in workout, e.g. 2026-06-02T14:06."),
):
    """Undo an automatic merge so the workout counts as its own session."""
    state = _state(ctx)
    text = when.replace(" ", "T")
    candidates = [
        c for c in state.store.corrections(type_=corr.WORKOUT_MERGE, statuses=[corr.ACTIVE])
        if c.workout.startswith(text)
    ]
    if not candidates:
        _fail(state.console, f"no active workout merge for a workout starting {esc(repr(when))}. "
              "See [bold]fitness corrections --type workout_merge[/bold].")
    if len(candidates) > 1:
        _fail(state.console, f"{esc(repr(when))} matches several merges "
              f"({esc(', '.join(c.workout for c in candidates))}); be more specific.")
    _revert_or_restore(state, candidates[0].id, corr.REVERTED)


# --- show -------------------------------------------------------------------

@app.command()
def show(
    ctx: typer.Context,
    when: str = typer.Argument("last", help="'last', a date (2026-06-02), or a start time (2026-06-02T14:06)."),
    raw: bool = typer.Option(False, "--raw", help="Ignore corrections."),
):
    """Show a session set by set."""
    state = _state(ctx)
    console = state.console
    sessions = state.store.sessions(raw=raw)
    if not sessions:
        console.print("No workouts yet. Run [bold]fitness import FILE[/bold] first.")
        return
    matches = _match_sessions(sessions, when)
    if not matches:
        _fail(console, f"no session matches {esc(repr(when))}. See [bold]fitness workouts[/bold].")

    merge_corrections = {} if raw else {
        c.id: c for c in state.store.corrections(type_=corr.EXERCISE_MERGE, statuses=[corr.ACTIVE])}
    for i, session in enumerate(matches):
        if i:
            console.print()
        _print_session(console, session, merge_corrections)


def _print_session(console: Console, session: Session, merge_corrections: dict[int, Correction] | None = None) -> None:
    header = Text()
    header.append(session.name, style="bold")
    header.append(f"  {_when(session.started_at)}  ·  ")
    header.append_text(Text.from_markup(_session_duration(session)))
    header.append(f"  ·  {_plural(len(session.exercises), 'exercise')}, {_plural(session.set_count, 'set')}")
    if session.is_merged:
        header.append(f"  ·  {len(session.members)} workouts merged", style="yellow")
    console.print(header)
    if session.notes:
        console.print(f"[italic]{esc(session.notes)}[/italic]")

    for member in session.members:
        if session.is_merged:
            console.print(f"[dim]── {member.started_at:%H:%M} workout ({format_duration(member.duration)}) ──[/dim]")
        for entry in member.exercises:
            show_rpe = any(s.rpe is not None for s in entry.sets)
            table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
            table.add_column(justify="right", style="dim")  # set label
            table.add_column(justify="right")               # weight
            table.add_column(justify="left")                # reps / seconds
            table.add_column(style="dim")                   # rest
            if show_rpe:
                table.add_column(justify="right")
            table.add_column(style="italic")
            for s in entry.sets:
                table.add_row(*_set_row(s, show_rpe))
            heading = f"\n[bold]{esc(entry.name)}[/bold]"
            if entry.merged_from:
                adjusted = f", {entry.merge_label}" if entry.merge_label else ""
                heading += f"  [dim](recorded as {esc(entry.merged_from + adjusted)})[/dim]"
            console.print(heading)
            console.print(table)

    applied = list(session.applied)
    merge_ids = {e.merge_correction_id for e in session.exercises if e.merge_correction_id}
    applied += [merge_corrections[i] for i in sorted(merge_ids) if merge_corrections and i in merge_corrections]
    if applied:
        console.print("\n[yellow]Corrections applied:[/yellow]")
        for c in applied:
            console.print(f"  #{c.id} {esc(c.reason)}")


# --- corrections ------------------------------------------------------------

@corrections_app.callback(invoke_without_command=True)
def corrections_list(
    ctx: typer.Context,
    type_: str | None = typer.Option(None, "--type", help="Only this type, e.g. workout_merge."),
    auto: bool = typer.Option(False, "--auto", help="Only automatic corrections."),
    user: bool = typer.Option(False, "--user", help="Only corrections you made."),
    status: str | None = typer.Option(None, "--status", help="active, reverted, obsolete, or stale."),
    exercise: str | None = typer.Option(None, "--exercise", help="Only corrections affecting this exercise."),
    since: str | None = typer.Option(None, "--since", help="Only corrections created on or after this date."),
    all_: bool = typer.Option(False, "--all", help="Include obsolete corrections."),
):
    """List every correction applied on top of your raw data."""
    if ctx.invoked_subcommand:
        return
    state = _state(ctx)
    console = state.console
    if auto and user:
        _fail(console, "use --auto or --user, not both.")
    if status and status not in corr.STATUSES:
        _fail(console, f"unknown status {esc(repr(status))}; use one of {', '.join(corr.STATUSES)}.")
    statuses = [status] if status else (None if all_ else [corr.ACTIVE, corr.REVERTED, corr.STALE])
    items = state.store.corrections(
        type_=type_,
        source=corr.AUTO if auto else corr.USER if user else None,
        statuses=statuses,
        since=_parse_date(since) if since else None,
    )
    if exercise:
        items = [c for c in items if c.target.get("exercise", "").lower() == exercise.lower()]
    if not items:
        console.print("No corrections match.")
        return

    table = Table(box=None, header_style="bold", pad_edge=False)
    for col in ("ID", "Type", "Source", "Status", "Reason"):
        table.add_column(col, justify="right" if col == "ID" else "left", overflow="fold")
    status_style = {corr.ACTIVE: "green", corr.REVERTED: "yellow", corr.STALE: "red", corr.OBSOLETE: "dim"}
    for c in items:
        table.add_row(
            str(c.id), esc(c.type), esc(c.source), f"[{status_style[c.status]}]{c.status}[/]", esc(c.reason)
        )
    console.print(table)
    console.print(f"[dim]{_plural(len(items), 'correction')}. Undo one with: fitness corrections revert ID[/dim]")


def _revert_or_restore(state: State, correction_id: int, status: str) -> None:
    console = state.console
    store = state.store
    current = store.get_correction(correction_id)
    if current is None:
        _fail(console, f"no correction #{correction_id}.")
    if current.status == status:
        console.print(f"Correction #{correction_id} is already {status}.")
        return
    try:
        updated, knock_on = store.set_correction_status(correction_id, status, state.settings)
    except ValueError as exc:
        _fail(console, esc(exc))
    verb = "Reverted" if status == corr.REVERTED else "Restored"
    console.print(f"{verb} #{updated.id}: {esc(updated.reason)}")
    if knock_on.changed:
        console.print("Knock-on changes:")
        _print_reconcile(console, knock_on, store)


@corrections_app.command("revert")
def corrections_revert(ctx: typer.Context, correction_id: int = typer.Argument(..., metavar="ID")):
    """Undo a correction. Automatic corrections you revert stay reverted on later imports."""
    _revert_or_restore(_state(ctx), correction_id, corr.REVERTED)


@corrections_app.command("restore")
def corrections_restore(ctx: typer.Context, correction_id: int = typer.Argument(..., metavar="ID")):
    """Re-apply a correction you reverted."""
    _revert_or_restore(_state(ctx), correction_id, corr.ACTIVE)


# --- analysis helpers -------------------------------------------------------

FORMULA_HELP = "e1RM formula: epley or brzycki."
RAW_HELP = "Ignore corrections (no merging, no set fixes)."
WARMUP_HELP = "Count warm-up sets in volume and set totals (never in PRs)."


@dataclass
class Analysis:
    sessions: list[Session]
    df: pd.DataFrame
    bodyweight: Bodyweight


def _analysis(state: State, *, raw: bool = False, include_warmups: bool = False) -> Analysis:
    store = state.store
    sessions = store.sessions(raw=raw)
    if not sessions:
        state.console.print("No workouts yet. Run [bold]fitness import FILE[/bold] first.")
        raise typer.Exit(0)
    bodyweight = Bodyweight(state.settings.bodyweight_lb, store.bodyweight_entries())
    df = metrics.sets_frame(sessions, store.catalog(), bodyweight, include_warmups=include_warmups)
    return Analysis(sessions, df, bodyweight)


def _check_formula(console: Console, formula: str) -> None:
    if formula not in metrics.FORMULAS:
        _fail(console, f"unknown formula {esc(repr(formula))}; use {' or '.join(metrics.FORMULAS)}.")


def _resolve_exercise(console: Console, query: str, names, merged: dict | None = None,
                      where: str = "", hint: str = "See [bold]fitness exercises[/bold].") -> str:
    """Match a name or a unique part of one. A name combined into another resolves to the kept name.

    ``where`` (e.g. " in that workout") and ``hint`` (Rich markup) shape the no-match error.
    """
    merged = merged or {}
    names = sorted(set(names) | set(merged))
    q = query.lower()

    def resolve(name: str) -> str:
        if name in merged:
            console.print(f"[dim]{esc(name)} is combined into {esc(merged[name].into)}; showing that.[/dim]")
            return merged[name].into
        return name

    exact = [n for n in names if n.lower() == q]
    if exact:
        return resolve(exact[0])
    partial = [n for n in names if q in n.lower()]
    targets = sorted({merged[n].into if n in merged else n for n in partial})
    if len(targets) == 1:
        sources = [n for n in partial if n in merged]
        return resolve(sources[0]) if sources and targets[0] not in partial else targets[0]
    if not partial:
        _fail(console, f"no exercise{where} matches {esc(repr(query))}. {hint}")
    shown = ", ".join(targets[:8]) + (", …" if len(targets) > 8 else "")
    _fail(console, f"{esc(repr(query))} matches {len(targets)} exercises ({esc(shown)}); be more specific.")


def _bodyweight_note(console: Console, df: pd.DataFrame, bodyweight: Bodyweight) -> None:
    if not (df["kind"] == ASSISTED).any():
        return
    if not bodyweight.uses_placeholder:
        console.print("[dim]Assisted-lift loads use your logged bodyweight (Apple Health). "
                      "See fitness bodyweight[/dim]")
    else:
        console.print(
            f"[dim]Assisted-lift loads use placeholder bodyweight {_num(bodyweight.placeholder_lb)} lb "
            "(load = bodyweight − assistance). Change with: fitness config set bodyweight_lb N[/dim]"
        )


def _date(ts) -> str:
    return "" if ts is None or pd.isna(ts) else f"{pd.Timestamp(ts):%Y-%m-%d}"


def _lb(x, decimals: int | None = None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:,.{decimals}f} lb" if decimals is not None else f"{x:,g} lb"


def _parse_window(console: Console, text: str) -> pd.Timedelta:
    text = text.strip().lower()
    units = {"d": 1, "w": 7, "m": 30, "y": 365}
    if len(text) < 2 or text[-1] not in units or not text[:-1].isdigit():
        _fail(console, f"expected a window like 30d, 4w, or 3m; got {esc(repr(text))}.")
    return pd.Timedelta(days=int(text[:-1]) * units[text[-1]])


def _filter_dates(df: pd.DataFrame, column: str, since: str | None, until: str | None) -> pd.DataFrame:
    if since:
        df = df[df[column] >= pd.Timestamp(_parse_date(since))]
    if until:
        df = df[df[column] < pd.Timestamp(_parse_date(until, "--until")) + pd.Timedelta(days=1)]
    return df


def _pr_value(pr_type: str, value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    if pr_type == "e1rm":
        return _lb(value, 0)
    if pr_type == "seconds":
        return f"{value:g} s"
    if pr_type == "reps":
        return f"{value:g} reps"
    return _lb(value)


# --- history ----------------------------------------------------------------

@app.command()
def history(
    ctx: typer.Context,
    exercise: str = typer.Argument(..., help="Exercise name (or a unique part of it)."),
    since: str | None = typer.Option(None, "--since", help="Only sessions on or after this date."),
    formula: str = typer.Option("epley", "--formula", help=FORMULA_HELP),
    raw: bool = typer.Option(False, "--raw", help=RAW_HELP),
    include_warmups: bool = typer.Option(False, "--include-warmups", help=WARMUP_HELP),
):
    """Session-by-session history of one exercise."""
    state = _state(ctx)
    console = state.console
    _check_formula(console, formula)
    a = _analysis(state, raw=raw, include_warmups=include_warmups)
    merged = {} if raw else state.store.active_merges()
    name = _resolve_exercise(console, exercise, a.df["exercise"], merged)
    hist = metrics.exercise_history(a.df, name, formula)
    events = metrics.pr_events(a.df[a.df["exercise"] == name], formula)
    prs_by_session = events.groupby("session")["pr_type"].apply(list).to_dict() if not events.empty else {}
    hist = _filter_dates(hist, "session", since, None)
    kind = hist["kind"].iloc[0] if not hist.empty else a.df.loc[a.df["exercise"] == name, "kind"].iloc[0]

    console.print(f"[bold]{esc(name)}[/bold]  [dim]({kind})[/dim]")
    if hist.empty:
        console.print("No sessions in that range.")
        return
    table = Table(box=None, header_style="bold", pad_edge=False)
    if kind == TIMED:
        cols = ["Date", "Best hold", "Total", "Sets", "PRs"]
    elif kind == UNLOADED:
        cols = ["Date", "Most reps", "Total reps", "Sets", "PRs"]
    else:
        cols = ["Date", "Top set", "e1RM", "Sets", "Reps", "Volume", "PRs"]
    for c in cols:
        table.add_column(c, justify="left" if c in ("Date", "Top set", "PRs") else "right")

    for r in hist.itertuples():
        date = _date(r.session) + (" [yellow]*[/yellow]" if r.corrected else "") + (
            " [cyan]†[/cyan]" if r.merged_from else "")
        types = prs_by_session.get(r.session, [])
        counts = Counter(types)
        prs = ", ".join(
            f"{PR_LABELS[t]} ×{n}" if n > 1 else PR_LABELS[t] for t, n in counts.items()
        )
        prs = f"[green]{prs}[/green]" if prs else ""
        if kind == TIMED:
            table.add_row(date, f"{r.max_seconds:g} s", f"{r.total_seconds:g} s", str(r.sets), prs)
        elif kind == UNLOADED:
            table.add_row(date, f"{r.max_reps:g}", f"{r.reps:g}", str(r.sets), prs)
        else:
            if math.isnan(r.top_load):
                top = "—"
            elif kind == ASSISTED:
                top = f"{_num(r.top_weight)} lb assist × {_num(r.top_reps)} [dim](= {_num(r.top_load)} lb)[/dim]"
            else:
                top = f"{_num(r.top_load)} lb × {_num(r.top_reps)}"
                if not math.isnan(r.top_pre_merge_weight):
                    top += f" [dim]({_num(r.top_pre_merge_weight)} {_merge_math(r.merge_label)})[/dim]"
            e1 = "—" if math.isnan(r.e1rm) else f"{r.e1rm:,.0f}"
            table.add_row(date, top, e1, str(r.sets), f"{r.reps:g}", f"{r.volume:,.0f}", prs)
    console.print(table)

    footer = [f"{_plural(len(hist), 'session')}"]
    if kind not in (TIMED, UNLOADED) and hist["e1rm"].notna().any():
        best = hist.loc[hist["e1rm"].idxmax()]
        footer.append(f"best e1RM {best['e1rm']:,.0f} lb ({_num(best['e1rm_load'])} × {_num(best['e1rm_reps'])}, "
                      f"{_date(best['session'])})")
    console.print("[dim]" + "; ".join(footer)
                  + f". e1RM: {formula.title()}, sets of 1–{metrics.E1RM_MAX_REPS} reps.[/dim]")
    _bodyweight_note(console, a.df[a.df["exercise"] == name], a.bodyweight)
    sources = {name} | {m.source for m in merged.values() if m.into == name}
    if hist["corrected"].any():
        fixes_for = [c for c in state.store.corrections(type_=corr.VALUE_FIX, statuses=[corr.ACTIVE])
                     if c.target.get("exercise") in sources]
        console.print("[yellow]*[/yellow] [dim]includes corrected sets:[/dim]")
        for c in fixes_for:
            console.print(f"  [dim]#{c.id} {esc(c.reason)}[/dim]")
    for m in sorted((m for m in merged.values() if m.into == name), key=lambda m: m.source):
        how = f", weights adjusted {m.label}" if m.label else ", weights as recorded"
        console.print(f"[cyan]†[/cyan] [dim]recorded as {esc(m.source)}{how} (correction #{m.correction_id})[/dim]")


def _quote(name: str) -> str:
    """A name as a shell argument in a hint: double quotes unless it holds a double quote."""
    return f"'{name}'" if '"' in name else f'"{name}"'


def _merge_math(label: str) -> str:
    """'-50 lb' → '− 50', '+40 lb' → '+ 40', '×0.75' → '× 0.75'."""
    if label.startswith("×"):
        return f"× {label[1:]}"
    number = label.removesuffix(" lb")
    return f"− {number[1:]}" if number.startswith("-") else f"+ {number.lstrip('+')}"


# --- prs --------------------------------------------------------------------

@app.command()
def prs(
    ctx: typer.Context,
    exercise: str | None = typer.Argument(None, help="Show rep maxes and PR timeline for one exercise."),
    recent: str | None = typer.Option(None, "--recent", help="PRs set within a window, e.g. 30d, 4w, 3m."),
    sort: str = typer.Option("name", "--sort", help="name, e1rm, or recent."),
    formula: str = typer.Option("epley", "--formula", help=FORMULA_HELP),
    raw: bool = typer.Option(False, "--raw", help=RAW_HELP),
):
    """Personal records: current bests, recent PRs, or one exercise in detail."""
    state = _state(ctx)
    console = state.console
    _check_formula(console, formula)
    a = _analysis(state, raw=raw)

    if exercise:
        name = _resolve_exercise(console, exercise, a.df["exercise"], {} if raw else state.store.active_merges())
        _prs_for_exercise(console, a, name, formula)
        return

    if recent:
        window = _parse_window(console, recent)
        events = metrics.pr_events(a.df, formula)
        latest = a.df["session"].max()
        cutoff = pd.Timestamp(datetime.now()) - window
        events = events[events["session"] >= cutoff]
        if events.empty:
            console.print(f"No PRs in the last {esc(recent)} (most recent session: {_date(latest)}).")
            return
        table = Table(box=None, header_style="bold", pad_edge=False)
        for c in ("Date", "Exercise", "PR", "Set", "New", "Previous"):
            table.add_column(c, justify="right" if c in ("New", "Previous") else "left")
        for r in events.itertuples():
            table.add_row(_date(r.session), esc(r.exercise), PR_LABELS[r.pr_type], esc(r.detail),
                          _pr_value(r.pr_type, r.value), _pr_value(r.pr_type, r.previous))
        console.print(table)
        console.print(f"[dim]{_plural(len(events), 'PR')} across {events['exercise'].nunique()} exercises "
                      f"since {cutoff:%Y-%m-%d}.[/dim]")
        _bodyweight_note(console, a.df[a.df["exercise"].isin(events["exercise"])], a.bodyweight)
        return

    bests = metrics.current_bests(a.df, formula)
    if sort not in ("name", "e1rm", "recent"):
        _fail(console, "sort by name, e1rm, or recent.")
    bests = bests.sort_values(
        {"name": "exercise", "e1rm": "e1rm", "recent": "last"}[sort],
        ascending=(sort == "name"), na_position="last",
    )
    table = Table(box=None, header_style="bold", pad_edge=False)
    for c in ("Exercise", "Best e1RM", "Set", "Date", "Heaviest", "Date ", "Sessions"):
        table.add_column(c, justify="right" if c in ("Best e1RM", "Sessions") else "left")
    for r in bests.itertuples():
        if r.kind == TIMED:
            table.add_row(esc(r.exercise), "—", f"{r.max_seconds:g} s hold", "", "", "", str(r.sessions))
        elif r.kind == UNLOADED:
            table.add_row(esc(r.exercise), "—", f"{r.max_reps:g} reps", "", "", "", str(r.sessions))
        else:
            e1 = "—" if math.isnan(r.e1rm) else f"{r.e1rm:,.0f}"
            table.add_row(esc(r.exercise), e1, esc(r.e1rm_set), _date(r.e1rm_date), esc(r.heaviest_set),
                          _date(r.heaviest_date), str(r.sessions))
    console.print(table)
    console.print(f"[dim]{_plural(len(bests), 'exercise')}. Loads in lb. e1RM: {formula.title()}, sets of "
                  f"1–{metrics.E1RM_MAX_REPS} reps. Details: fitness prs EXERCISE. "
                  "Recent: fitness prs --recent 30d[/dim]")
    _bodyweight_note(console, a.df, a.bodyweight)


def _prs_for_exercise(console: Console, a: Analysis, name: str, formula: str) -> None:
    d = a.df[a.df["exercise"] == name]
    kind = d["kind"].iloc[0]
    console.print(f"[bold]{esc(name)}[/bold]  [dim]({kind})[/dim]")
    if kind not in (TIMED, UNLOADED):
        maxes = metrics.rep_maxes(d, name)
        if not maxes.empty:
            table = Table(title="Best load for at least N reps", title_justify="left", box=None,
                          header_style="bold", pad_edge=False)
            for c in ("Reps", "Load", "Set", "Date"):
                table.add_column(c, justify="right" if c in ("Reps", "Load") else "left")
            for r in maxes.itertuples():
                done = (f"{_num(r.weight)} lb assist × {_num(r.actual_reps)}" if kind == ASSISTED
                        else f"{_num(r.load)} × {_num(r.actual_reps)}")
                table.add_row(str(r.reps), _lb(r.load), done, _date(r.session))
            console.print(table)
    events = metrics.pr_events(d, formula)
    if events.empty:
        console.print("No PRs since the first session yet.")
    else:
        table = Table(title="PR timeline", title_justify="left", box=None, header_style="bold", pad_edge=False)
        for c in ("Date", "PR", "Set", "New", "Previous"):
            table.add_column(c, justify="right" if c in ("New", "Previous") else "left")
        for r in events.itertuples():
            table.add_row(_date(r.session), PR_LABELS[r.pr_type], esc(r.detail),
                          _pr_value(r.pr_type, r.value), _pr_value(r.pr_type, r.previous))
        console.print(table)
        console.print(f"[dim]{_plural(len(events), 'PR')}; the first session is the baseline.[/dim]")
    _bodyweight_note(console, d, a.bodyweight)


# --- stats ------------------------------------------------------------------

@app.command()
def stats(
    ctx: typer.Context,
    by: str = typer.Option("week", "--by", help="week or month."),
    last: int = typer.Option(12, "--last", "-n", min=0, help="Show the most recent N periods (0 for all)."),
    all_: bool = typer.Option(False, "--all", help="Show every period."),
    since: str | None = typer.Option(None, "--since", help="Only sessions on or after this date."),
    until: str | None = typer.Option(None, "--until", help="Only sessions on or before this date."),
    raw: bool = typer.Option(False, "--raw", help=RAW_HELP),
    include_warmups: bool = typer.Option(False, "--include-warmups", help=WARMUP_HELP),
):
    """Training summary: frequency, consistency, volume, and PRs over time."""
    state = _state(ctx)
    console = state.console
    if by not in ("week", "month"):
        _fail(console, "--by must be week or month.")
    a = _analysis(state, raw=raw, include_warmups=include_warmups)
    events = metrics.pr_events(a.df)  # PRs need the full history, then get filtered
    sdf = _filter_dates(metrics.sessions_frame(a.sessions), "session", since, until)
    df = _filter_dates(a.df, "session", since, until)
    events = _filter_dates(events, "session", since, until)
    if sdf.empty:
        console.print("No sessions in that range.")
        return

    c = metrics.consistency(sdf, datetime.now())
    console.print(
        f"[bold]{_plural(c['sessions'], 'session')}[/bold] from {_when(c['first'])} to {_when(c['last'])}  ·  "
        f"{c['per_week']:.1f} per week  ·  {c['avg_gap_days']:.1f} days between sessions on average"
    )
    gap = f"Longest gap: {_plural(c['longest_gap_days'], 'day')}"
    if c["longest_gap_from"] is not None and c["longest_gap_days"]:
        gap += f" ({_date(c['longest_gap_from'])} → {_date(c['longest_gap_to'])})"
    console.print(
        f"{gap}  ·  Last session {_plural(c['days_since_last'], 'day')} ago  ·  "
        f"Weekly streak: {c['current_week_streak']} (longest {c['longest_week_streak']})"
    )
    console.print(
        f"Totals: {df['volume'].sum():,.0f} lb volume, {_plural(len(df), 'set')}, "
        f"{df['reps'].sum():,.0f} reps, {_plural(len(events), 'PR')}\n"
    )

    periods = metrics.period_stats(sdf, df, events, by)
    total = len(periods)
    if not all_ and last > 0:
        periods = periods.tail(last)
    table = Table(box=None, header_style="bold", pad_edge=False)
    first_col = "Week of" if by == "week" else "Month"
    for col in (first_col, "Sessions", "Time", "Avg", "Sets", "Reps", "Volume (lb)", "PRs"):
        table.add_column(col, justify="left" if col in ("Week of", "Month") else "right")
    for r in periods.itertuples():
        label = f"{r.period.start_time:%Y-%m-%d}" if by == "week" else f"{r.period.start_time:%b %Y}"
        time = "—" if pd.isna(r.duration_min) else format_duration(timedelta(minutes=r.duration_min))
        avg = "—" if pd.isna(r.avg_duration_min) else format_duration(timedelta(minutes=round(r.avg_duration_min)))
        dim = "[dim]" if r.sessions == 0 else ""
        table.add_row(f"{dim}{label}", str(r.sessions), time, avg, str(r.sets), f"{r.reps:,.0f}",
                      f"{r.volume:,.0f}", str(r.prs) if r.prs else "")
    console.print(table)
    if len(periods) != total:
        console.print(f"[dim]Showing the last {len(periods)} of {total} {by}s (--all for every {by}).[/dim]")

    names = metrics.name_counts(sdf)
    table = Table(title="\nBy workout name", title_justify="left", box=None, header_style="bold", pad_edge=False)
    for col in ("Workout", "Sessions", "Per week", "Last"):
        table.add_column(col, justify="left" if col in ("Workout", "Last") else "right")
    for r in names.itertuples():
        table.add_row(esc(r.name), str(r.sessions), f"{r.per_week:.1f}", _date(r.last))
    console.print(table)

    notes = ["Warm-ups " + ("included in" if include_warmups else "excluded from") + " sets and volume",
             "time excludes suspect durations"]
    console.print(f"\n[dim]{'; '.join(notes)}.[/dim]")
    _bodyweight_note(console, df, a.bodyweight)


# --- exercises --------------------------------------------------------------

@app.command()
def exercises(
    ctx: typer.Context,
    search: str | None = typer.Option(None, "--search", "-s", help="Only names containing this text."),
    sort: str = typer.Option("count", "--sort", help="count, recent, or name."),
    raw: bool = typer.Option(False, "--raw", help=RAW_HELP),
    unmapped: bool = typer.Option(False, "--unmapped",
                                  help="List exercises with no muscle groups in config/exercises.toml."),
):
    """Every exercise you've logged, with frequency, when you last did it, and muscles worked."""
    state = _state(ctx)
    console = state.console
    if sort not in ("count", "recent", "name"):
        _fail(console, "sort by count, recent, or name.")
    a = _analysis(state, raw=raw)
    catalog = state.store.catalog()
    if unmapped:
        from .cli_metadata import print_unmapped

        print_unmapped(console, a.df, catalog, search)
        return
    summary = metrics.exercise_summary(a.df).merge(
        metrics.current_bests(a.df)[["exercise", "e1rm", "max_seconds", "max_reps"]], on="exercise"
    )
    if search:
        summary = summary[summary["exercise"].str.contains(search, case=False, regex=False)]
    summary = summary.sort_values(
        {"count": ["sessions", "exercise"], "recent": ["last", "exercise"], "name": ["exercise"]}[sort],
        ascending={"count": [False, True], "recent": [False, True], "name": [True]}[sort],
    )
    if summary.empty:
        console.print("No exercises match.")
        return
    now = pd.Timestamp(datetime.now()).normalize()
    table = Table(box=None, header_style="bold", pad_edge=False)
    from .cli_metadata import muscles_label

    left = ("Exercise", "Kind", "First", "Last", "Best", "Muscles")
    for col in ("Exercise", "Kind", "Sessions", "Sets", "First", "Last", "Days ago", "Avg rest", "Best", "Muscles"):
        table.add_column(col, justify="left" if col in left else "right")
    guessed = 0
    for r in summary.itertuples():
        if r.kind == TIMED:
            best = f"{r.max_seconds:g} s"
        elif r.kind == UNLOADED:
            best = f"{r.max_reps:g} reps"
        else:
            best = "—" if math.isnan(r.e1rm) else f"e1RM {r.e1rm:,.0f}"
        rest = "" if pd.isna(r.avg_rest) else format_duration_seconds(round(r.avg_rest))
        days = (now - pd.Timestamp(r.last).normalize()).days
        guessed += catalog.is_guessed(r.exercise)
        table.add_row(esc(r.exercise), "" if r.kind == "loaded" else r.kind, str(r.sessions), str(r.sets),
                      _date(r.first), _date(r.last), str(days), rest, best, muscles_label(catalog, r.exercise))
    console.print(table)
    console.print(f"[dim]{_plural(len(summary), 'exercise')}. Sets exclude warm-ups. Muscles are primary only.[/dim]")
    if guessed:
        console.print(f"[dim]{_plural(guessed, 'exercise')} with guessed muscles (marked ?): "
                      "fitness exercises --unmapped[/dim]")
    merged = {} if raw else state.store.active_merges()
    shown = set(summary["exercise"])
    for m in sorted(merged.values(), key=lambda m: m.into):
        if m.into in shown:
            how = f" ({m.label})" if m.label else ""
            console.print(f"[dim]{esc(m.into)} includes {esc(m.source)}{how}. See fitness merge list.[/dim]")
    candidates = [] if raw else state.store.merge_candidates()
    if candidates:
        console.print(f"[dim]{_plural(len(candidates), 'group')} of similar exercises to review: "
                      "fitness merge review[/dim]")


# --- check & fix ------------------------------------------------------------

def _valid_amount(value: float) -> bool:
    return math.isfinite(value) and value >= 0


def _resolve_set_labels(console: Console, entry: ExerciseEntry, labels: list[str]) -> list[int]:
    """Turn set labels as shown by `fitness show` (1, 2, W, D, D2, F) into positions."""
    if not labels:
        return [s.position for s in entry.sets]
    positions = []
    for label in labels:
        label = label.strip().upper()
        if label.isdigit():
            match = [s for s in entry.sets if s.set_type is SetType.NORMAL and s.number == int(label)]
        elif label[:1] in ("W", "D", "F") and (len(label) == 1 or label[1:].isdigit()):
            type_ = {"W": SetType.WARMUP, "D": SetType.DROP, "F": SetType.FAILURE}[label[0]]
            of_type = [s for s in entry.sets if s.set_type is type_]
            if len(label) == 1 and len(of_type) > 1:
                _fail(console, f"{esc(entry.name)} has {len(of_type)} {esc(label)} sets; "
                      f"use {esc(label)}1, {esc(label)}2, …")
            index = int(label[1:] or 1)
            match = of_type[index - 1:index]
        else:
            match = []
        if not match:
            _fail(console, f"{esc(entry.name)} has no set {esc(repr(label))} in that workout (sets: "
                  f"{', '.join(_set_label(s) for s in entry.sets)}).")
        positions.append(match[0].position)
    return positions


@app.command()
def fix(
    ctx: typer.Context,
    when: str = typer.Argument(..., help="Workout start time, e.g. 2026-06-04T13:53 (a date works if unique)."),
    exercise: str = typer.Argument(..., help="Exercise name in that workout (or a unique part of it)."),
    sets: list[str] = typer.Option([], "--set", help="Set label as shown by `fitness show` (1, 2, W, D, F). "
                                   "Repeatable; default all sets."),
    occurrence: int = typer.Option(1, "--occurrence", min=1,
                                   help="Which entry, if the exercise appears more than once."),
    swap: bool = typer.Option(False, "--swap-weight-reps", help="Swap weight and reps."),
    weight: float | None = typer.Option(None, "--weight", help="Set the weight (lb)."),
    reps: float | None = typer.Option(None, "--reps", help="Set the reps."),
    seconds: float | None = typer.Option(None, "--seconds", help="Set the hold time."),
):
    """Correct set values. The original data is kept; undo with `fitness corrections revert ID`."""
    state = _state(ctx)
    console = state.console
    store = state.store
    if swap and any(v is not None for v in (weight, reps, seconds)):
        _fail(console, "use --swap-weight-reps or --weight/--reps/--seconds, not both.")
    if not swap and all(v is None for v in (weight, reps, seconds)):
        _fail(console, "say what to change: --swap-weight-reps, --weight, --reps, or --seconds.")
    for option, v in (("--weight", weight), ("--reps", reps), ("--seconds", seconds)):
        if v is not None and not _valid_amount(v):
            _fail(console, f"{option} must be a number of 0 or more, got {_num(v)}.")
    _require_workouts(state)
    workouts = store.find_workouts(when)
    if not workouts:
        _fail(console, f"no workout starts at {esc(repr(when))}. See [bold]fitness workouts[/bold].")
    if len(workouts) > 1:
        _fail(console, f"{esc(repr(when))} matches {len(workouts)} workouts "
              f"({', '.join(f'{w.started_at:%H:%M}' for w in workouts)}); add the time, "
              f"e.g. {workout_key(workouts[0].started_at)[:16]}.")
    workout = workouts[0]
    in_workout = list(dict.fromkeys(e.name for e in workout.exercises))
    name = _resolve_exercise(console, exercise, in_workout, where=" in that workout",
                             hint=f"It has: {esc(', '.join(in_workout))}.")
    entries = [e for e in workout.exercises if e.name == name]
    if not 1 <= occurrence <= len(entries):
        _fail(console, f"{esc(name)} appears {len(entries)} time(s) in that workout.")
    entry = entries[occurrence - 1]
    positions = _resolve_set_labels(console, entry, sets)

    by_pos = {s.position: s for s in entry.sets}
    changes = {}
    for pos in positions:
        s = by_pos[pos]
        if swap:
            changes[pos] = {"weight": s.reps, "reps": s.weight}
        else:
            given = (("weight", weight), ("reps", reps), ("seconds", seconds))
            changes[pos] = {f: v for f, v in given if v is not None}
    existing_ids = {c.id for c in store.corrections(type_=corr.VALUE_FIX)}
    try:
        c = store.add_value_fix(workout, name, occurrence, changes)
    except ValueError as exc:
        _fail(console, esc(exc))
    verb = "Updated" if c.id in existing_ids else "Recorded"
    console.print(f"{verb} correction #{c.id}: {esc(c.reason)}")
    console.print(f"[dim]Undo with: fitness corrections revert {c.id}[/dim]")


@app.command()
def check(
    ctx: typer.Context,
    list_only: bool = typer.Option(False, "--list", help="List flagged sets without asking about them."),
):
    """Review sets that look like data-entry errors: swap, edit, or dismiss each."""
    state = _state(ctx)
    console = state.console
    store = state.store
    if not store.workout_count():
        console.print("No workouts yet. Run [bold]fitness import FILE[/bold] first.")
        return
    groups = outliers.group(store.open_flags())
    dismissed = len(store.dismissed_flag_keys())
    if not groups:
        note = f" ({_plural(dismissed, 'flag')} dismissed earlier.)" if dismissed else ""
        console.print(f"No likely data-entry errors found.{note}")
        return
    total = sum(len(g.flags) for g in groups)
    verb = "looks" if total == 1 else "look"
    console.print(f"{_plural(total, 'set')} in {_plural(len(groups), 'exercise entry', 'exercise entries')} "
                  f"{verb} like data-entry errors.\n")

    for i, g in enumerate(groups, start=1):
        _print_flag_group(console, g, i, len(groups))
        if list_only:
            console.print()
            continue
        options = (["s"] if g.swap_plausible else []) + ["e", "i", "k", "q"]
        menu = ("  [s] swap weight and reps" if g.swap_plausible else "") + \
               "  [e] edit  [i] not an error (dismiss)  [k] skip  [q] quit"
        console.print(escape(menu))
        choice = typer.prompt(">", default="k", show_default=False).strip().lower()
        while choice not in options:
            choice = typer.prompt(f"Choose one of {', '.join(options)}", default="k",
                                  show_default=False).strip().lower()
        if choice == "q":
            break
        if choice == "k":
            console.print("[dim]Skipped.[/dim]\n")
            continue
        if choice == "i":
            store.dismiss_flags(f.key for f in g.flags)
            console.print("[dim]Dismissed; it won't be flagged again.[/dim]\n")
            continue
        changes = {}
        for f in g.flags:
            s = f.set
            if choice == "s":
                changes[s.position] = {"weight": s.reps, "reps": s.weight}
            else:
                new_weight = typer.prompt(f"  Set {_set_label(s)} weight (lb)", default=_num(s.weight))
                new_reps = typer.prompt(f"  Set {_set_label(s)} reps", default=_num(s.reps))
                try:
                    values = {"weight": float(new_weight), "reps": float(new_reps)}
                except ValueError:
                    values = {}
                if not values or not all(map(_valid_amount, values.values())):
                    _fail(console, "weight and reps must be numbers of 0 or more.")
                changes[s.position] = values
        try:
            c = store.add_value_fix(g.workout, g.entry.name, g.occurrence, changes)
        except ValueError as exc:
            console.print(f"[yellow]{esc(exc)}[/yellow]\n")
            continue
        console.print(f"Recorded correction #{c.id}: {esc(c.reason)}\n")


def _print_flag_group(console: Console, g: outliers.FlagGroup, i: int, n: int) -> None:
    console.print(f"[bold]({i}/{n}) {esc(g.entry.name)}[/bold] — {_when(g.workout.started_at)} "
                  f"({esc(g.workout.name)})")
    flagged = {f.set.position: f for f in g.flags}
    for s in g.entry.sets:
        line = esc(f"  {_set_label(s):>2}  {describe_set(s)}")
        if s.position in flagged:
            console.print(f"[yellow]{line}  ⚠ {esc(flagged[s.position].message)}[/yellow]")
        else:
            console.print(f"[dim]{line}[/dim]")
    if g.swap_plausible:
        swapped = ", ".join(f"{f.set.reps:g} lb × {f.set.weight:g}" for f in g.flags)
        console.print(f"  Looks like weight and reps were swapped → {swapped}")


# --- merge (combining similar exercises) --------------------------------------

TIER_TEXT = {
    similarity.LIKELY: "Likely the same exercise (names match apart from numbering or plurals)",
    similarity.POSSIBLE: "Possibly the same exercise (similar names)",
}


@dataclass
class Member:
    name: str
    sets: list[similarity.TopSet]

    @property
    def sessions(self) -> int:
        return len(self.sets)


def _members(store: Store, group: similarity.CandidateGroup) -> list[Member]:
    return [Member(n, store.top_sets(n)) for n in group.names]


def _default_kept(members: list[Member]) -> int:
    """Index of the name to keep by default: most sessions, then the one used first."""
    def rank(i):
        m = members[i]
        first = m.sets[0].when if m.sets else datetime.max
        return (-m.sessions, first)
    return min(range(len(members)), key=rank)


def _offset_allowed(kind: str) -> bool:
    return kind in (LOADED, ASSISTED)


def _print_candidate(console: Console, store: Store, catalog, group, members, i: int, n: int) -> None:
    console.print(f"[bold]({i}/{n}) {TIER_TEXT[group.tier]}[/bold]")
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
    for _ in range(5):
        table.add_column()
    for j, m in enumerate(members, start=1):
        if m.sets:
            span = f"{m.sets[0].when:%Y-%m-%d} → {m.sets[-1].when:%Y-%m-%d}"
            last = f"last top set {similarity.describe_sets(m.sets[-1:])}"
        else:
            span, last = "", "no working sets"
        table.add_row(escape(f"  [{j}]"), esc(m.name), _plural(m.sessions, "session"), span,
                      f"[dim]{esc(last)}[/dim]")
    console.print(table)
    kept = members[_default_kept(members)]
    kind = catalog.kind(kept.name)
    for m in members:
        if m is kept:
            continue
        s = similarity.suggest_offset(kept.name, kept.sets, m.name, m.sets, kind)
        console.print(f"  {esc(s.relation)}")
        if s.kept_sets and s.adjusted_sets and _offset_allowed(kind):
            console.print(f"  [dim]Compared top sets: {esc(kept.name)} {similarity.describe_sets(s.kept_sets)} vs "
                          f"{esc(m.name)} {similarity.describe_sets(s.adjusted_sets)}.[/dim]")
            console.print(f"  {esc(s.summary)}")


def _print_preview(console: Console, kept: Member, others: list[tuple[Member, float | None, float | None]]) -> None:
    """Text preview of the combined timeline around the switch, with adjustments applied."""
    rows = [(t.when, kept.name, t, t.weight) for t in kept.sets]
    for m, offset, factor in others:
        merge = merges.Merge(m.name, kept.name, offset, factor)
        rows += [(t.when, m.name, t, merge.adjust(t.weight)) for t in m.sets]
    rows.sort(key=lambda r: r[0])
    if not rows:
        return
    switch = min((m.sets[0].when for m, _, _ in others if m.sets), default=rows[0][0])
    nearest = sorted(rows, key=lambda r: abs((r[0] - switch).total_seconds()))[:12]
    nearest.sort(key=lambda r: r[0])
    table = Table(title=f"Combined as {esc(kept.name)} (12 sessions nearest the switch)", title_justify="left",
                  box=None, header_style="bold", pad_edge=False)
    for col in ("Date", "Recorded as", "Top set", "Counts as"):
        table.add_column(col)
    for when, name, t, adjusted in nearest:
        counts = f"{_num(adjusted)} lb × {_num(t.reps)}" if name != kept.name else ""
        table.add_row(f"{when:%Y-%m-%d}", esc(name), f"{_num(t.weight)} lb × {_num(t.reps)}", counts)
    console.print(table)


def _parse_adjustment(text: str) -> tuple[float | None, float | None]:
    """'-50' → offset −50 lb; 'x0.75' → factor 0.75; '0' or blank → none."""
    t = text.strip().lower().replace("lb", "").replace(" ", "").replace("−", "-")
    if t in ("", "0", "none"):
        return None, None
    if t[0] in "x*×":
        return None, float(t[1:])
    return float(t), None


def _pick(console: Console, prompt: str, members: list[Member], default: list[int], many: bool) -> list[int]:
    shown = ",".join(str(i + 1) for i in default)
    while True:
        text = typer.prompt(prompt, default=shown).strip()
        try:
            picked = [int(x) - 1 for x in text.replace(" ", "").split(",") if x]
        except ValueError:
            picked = []
        if picked and all(0 <= i < len(members) for i in picked) and (many or len(picked) == 1) \
                and len(set(picked)) == len(picked):
            return picked
        console.print(f"Enter {'numbers like 1,2' if many else 'one number'} from the list.")


@merge_app.command("review")
def merge_review(
    ctx: typer.Context,
    list_only: bool = typer.Option(False, "--list", help="List the candidates without asking about them."),
):
    """Go through similar exercises and decide: combine (optionally with an offset), or keep separate."""
    state = _state(ctx)
    console = state.console
    store = state.store
    if not store.workout_count():
        console.print("No workouts yet. Run [bold]fitness import FILE[/bold] first.")
        return
    groups = store.merge_candidates()
    if not groups:
        decided = len(store.active_merges()) + len(store.separate_pairs())
        note = f" ({_plural(decided, 'decision')} made earlier; see fitness merge list.)" if decided else ""
        console.print(f"No similar exercises to review.{note}")
        return
    console.print(f"{_plural(len(groups), 'group')} of similar exercises.\n")
    catalog = store.catalog()

    for i, group in enumerate(groups, start=1):
        members = _members(store, group)
        _print_candidate(console, store, catalog, group, members, i, len(groups))
        if list_only:
            console.print()
            continue
        kind = catalog.kind(members[_default_kept(members)].name)
        options = ["c"] + (["o"] if _offset_allowed(kind) else []) + ["k", "p", "s", "q"]
        menu = "  [c] combine as-is" + ("  [o] combine with weight offset" if "o" in options else "") + \
               "  [k] keep separate  [p] preview combined  [s] skip  [q] quit"
        while True:
            console.print(escape(menu))
            choice = typer.prompt(">", default="s", show_default=False).strip().lower()
            if choice not in options:
                console.print(f"Choose one of {', '.join(options)}.")
                continue
            if choice == "p":
                kept_i = _default_kept(members)
                others = []
                for j, m in enumerate(members):
                    if j != kept_i:
                        s = similarity.suggest_offset(members[kept_i].name, members[kept_i].sets, m.name, m.sets, kind)
                        others.append((m, s.offset, None))
                _print_preview(console, members[kept_i], others)
                from .cli_plots import save_merge_previews
                save_merge_previews(console, members[kept_i], others)
                continue
            break
        if choice == "q":
            break
        if choice == "s":
            console.print("[dim]Skipped; it will be offered again next time.[/dim]\n")
            continue
        if choice == "k":
            store.keep_separate(group.names)
            console.print("[dim]Kept separate; won't ask again (undo with fitness merge forget).[/dim]\n")
            continue

        everyone = list(range(len(members)))
        chosen = everyone if len(members) == 2 else _pick(console, "Combine which? (e.g. 1,2)", members, everyone, True)
        if len(chosen) < 2:
            console.print("[yellow]Pick at least two to combine.[/yellow]\n")
            continue
        default_kept = _default_kept(members)
        kept_i = _pick(console, "Keep which name?", members,
                       [default_kept if default_kept in chosen else chosen[0]], False)[0]
        if kept_i not in chosen:
            chosen.append(kept_i)
        kept = members[kept_i]
        kind = catalog.kind(kept.name)
        for j in chosen:
            if j == kept_i:
                continue
            m = members[j]
            offset = factor = None
            if choice == "o":
                s = similarity.suggest_offset(kept.name, kept.sets, m.name, m.sets, kind)
                default = "" if s.offset is None else f"{s.offset:+g}"
                hint = "lb like -50, factor like x0.75" if kind == LOADED else "lb like -10"
                while True:
                    text = typer.prompt(f"  Adjustment for {m.name} ({hint}, 0 for none)", default=default or "0")
                    try:
                        offset, factor = _parse_adjustment(text)
                        if factor is not None and kind != LOADED:
                            raise ValueError
                        break
                    except ValueError:
                        console.print("  Enter a number of lb (e.g. -50)"
                                      + (" or a factor (e.g. x0.75)." if kind == LOADED else "."))
            try:
                c = store.add_exercise_merge(m.name, kept.name, offset=offset, factor=factor)
            except ValueError as exc:
                console.print(f"[yellow]{esc(exc)}[/yellow]")
                continue
            console.print(f"Recorded correction #{c.id}: {esc(c.reason)}")
        left_out = [members[j].name for j in everyone if j not in chosen]
        for name in left_out:
            store.keep_separate([name, kept.name])
            console.print(f"[dim]Kept {esc(name)} separate from {esc(kept.name)}.[/dim]")
        console.print()


def _all_names(state: State) -> list[str]:
    return sorted(state.store.exercise_names(state.store.load_workouts()))


@merge_app.command("list")
def merge_list(ctx: typer.Context):
    """Show combined exercises and pairs you chose to keep separate."""
    state = _state(ctx)
    console = state.console
    store = state.store
    active = store.active_merges()
    separate = sorted(tuple(sorted(p)) for p in store.separate_pairs())
    if not active and not separate:
        console.print("No decisions yet. Review similar exercises with [bold]fitness merge review[/bold].")
        return
    if active:
        table = Table(title="Combined", title_justify="left", box=None, header_style="bold", pad_edge=False)
        for col in ("Recorded as", "Counts as", "Adjustment", "Correction"):
            table.add_column(col)
        for m in sorted(active.values(), key=lambda m: (m.into, m.source)):
            table.add_row(esc(m.source), esc(m.into), m.label or "none", f"#{m.correction_id}")
        console.print(table)
    if separate:
        table = Table(title="\nKept separate", title_justify="left", box=None, header_style="bold", pad_edge=False)
        table.add_column("Exercise")
        table.add_column("")
        table.add_column("Exercise")
        for a, b in separate:
            table.add_row(esc(a), "≠", esc(b))
        console.print(table)
    console.print("[dim]Undo a combination with: fitness merge undo NAME. "
                  "Forget a keep-separate decision with: fitness merge forget A B[/dim]")


@merge_app.command("add")
def merge_add(
    ctx: typer.Context,
    source: str = typer.Argument(..., help="Exercise to combine away (its sets get counted under --into)."),
    into: str = typer.Option(..., "--into", help="Exercise name to keep."),
    offset: float | None = typer.Option(None, "--offset", help="lb to add to every set of SOURCE (e.g. -50)."),
    factor: float | None = typer.Option(None, "--factor", help="Multiply every SOURCE weight by this (e.g. 0.75)."),
):
    """Combine one exercise into another, optionally adjusting its weights."""
    state = _state(ctx)
    console = state.console
    _require_workouts(state)
    names = _all_names(state)
    source = _resolve_exercise(console, source, names)
    into = _resolve_exercise(console, into, names)
    try:
        c = state.store.add_exercise_merge(source, into, offset=offset, factor=factor)
    except ValueError as exc:
        _fail(console, esc(exc))
    console.print(f"Recorded correction #{c.id}: {esc(c.reason)}")
    console.print(f"[dim]Undo with: fitness merge undo {esc(_quote(source))}[/dim]")


@merge_app.command("separate")
def merge_separate(
    ctx: typer.Context,
    names: list[str] = typer.Argument(..., help="Two or more exercises that are different movements."),
):
    """Record that exercises are different, so they aren't offered for combining again."""
    state = _state(ctx)
    console = state.console
    _require_workouts(state)
    all_names = _all_names(state)
    resolved = sorted({_resolve_exercise(console, n, all_names) for n in names})
    if len(resolved) < 2:
        _fail(console, "name at least two different exercises.")
    pairs = state.store.keep_separate(resolved)
    console.print(f"Keeping {esc(', '.join(resolved))} separate ({_plural(pairs, 'pair')}).")


@merge_app.command("forget")
def merge_forget(ctx: typer.Context, a: str, b: str):
    """Forget a keep-separate decision, so the pair is offered for review again."""
    state = _state(ctx)
    console = state.console
    _require_workouts(state)
    names = _all_names(state)
    a, b = _resolve_exercise(console, a, names), _resolve_exercise(console, b, names)
    if state.store.forget_separate(a, b):
        console.print(f"Forgot the decision to keep {esc(a)} and {esc(b)} separate.")
    else:
        _fail(console, f"{esc(a)} and {esc(b)} weren't marked as separate.")


@merge_app.command("undo")
def merge_undo(ctx: typer.Context, source: str = typer.Argument(..., help="The exercise that was combined away.")):
    """Undo a combination; the exercise goes back to its own history."""
    state = _state(ctx)
    console = state.console
    _require_workouts(state)
    source = _resolve_exercise(console, source, _all_names(state))
    try:
        c = state.store.undo_exercise_merge(source, state.settings)
    except (KeyError, ValueError) as exc:
        _fail(console, esc(exc.args[0]))
    console.print(f"Reverted #{c.id}: {esc(c.reason)}")
    console.print(f"[dim]Re-apply with: fitness corrections restore {c.id}[/dim]")


# --- config -----------------------------------------------------------------

@config_app.command("show")
def config_show(ctx: typer.Context):
    """Show the settings file in use and its values."""
    state = _state(ctx)
    path = settings_mod.resolve_path(state.config_path)
    exists = "" if path.exists() else " (not created yet; using defaults)"
    state.console.print(f"Settings file: {esc(path)}{exists}")
    for key in settings_mod.KEYS:
        state.console.print(f"  {key} = {_num(getattr(state.settings, key))}")


@config_app.command("set")
def config_set(ctx: typer.Context, key: str, value: str):
    """Change a setting, e.g. `fitness config set merge_gap_minutes 45`."""
    state = _state(ctx)
    console = state.console
    try:
        old, new = settings_mod.set_value(key, value, state.config_path)
    except (KeyError, ValueError) as exc:
        _fail(console, esc(exc.args[0]))
    key = settings_mod.normalize_key(key)
    console.print(f"{key}: {_num(getattr(old, key))} → {_num(getattr(new, key))}")
    state._settings = new
    if key in settings_mod.AFFECTS_AUTO_CORRECTIONS and state.db_path.exists():
        result = state.store.refresh_corrections(new)
        if result.changed:
            console.print("Automatic corrections updated:")
            _print_reconcile(console, result, state.store)
        else:
            console.print("No automatic corrections changed.")


# --- gui --------------------------------------------------------------------

@app.command()
def gui(ctx: typer.Context):
    """Open the desktop app (an optional install; see the README)."""
    from .gui.app import missing_dependencies, run
    state = _state(ctx)
    if missing := missing_dependencies():
        _fail(state.console, escape(missing))
    raise typer.Exit(run(state.db_path, state.config_path))


# Commands defined in other modules add themselves to `app` on import.
from . import cli_metadata  # noqa: E402,F401
from .cli_plots import plot_app  # noqa: E402

app.add_typer(plot_app, name="plot")
from . import cli_export  # noqa: E402,F401

if __name__ == "__main__":
    app()
