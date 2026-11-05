"""Command-line interface. The only module that talks to the terminal."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import corrections as corr
from . import settings as settings_mod
from .corrections import Correction, ReconcileResult, format_duration
from .importers import StrongParseError, parse_strong_csv
from .models import SetType, WorkoutSet
from .sessions import Session, workout_key
from .settings import Settings
from .store import ImportPlan, Store, default_db_path

app = typer.Typer(
    help="Track weightlifting statistics from Strong app exports.",
    no_args_is_help=True,
    add_completion=False,
)
workouts_app = typer.Typer(help="List workouts, or undo an automatic workout merge.")
corrections_app = typer.Typer(help="Show, revert, or restore corrections applied to your data.")
config_app = typer.Typer(help="Show or change settings.", no_args_is_help=True)
app.add_typer(workouts_app, name="workouts")
app.add_typer(corrections_app, name="corrections")
app.add_typer(config_app, name="config")

TYPE_LABELS = {
    corr.WORKOUT_MERGE: ("workout merged", "workouts merged"),
    corr.DURATION_SUSPECT: ("duration excluded", "durations excluded"),
}
SET_LABELS = {SetType.WARMUP: "W", SetType.DROP: "D", SetType.FAILURE: "F"}


# --- shared state ------------------------------------------------------------

@dataclass
class State:
    db_path: Path
    config_path: Optional[Path]
    console: Console
    _store: Optional[Store] = None
    _settings: Optional[Settings] = None

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.db_path)
        return self._store

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = settings_mod.load(self.config_path)
        return self._settings


def _state(ctx: typer.Context) -> State:
    return ctx.find_root().obj


def _fail(console: Console, message: str, code: int = 1):
    console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(code)


@app.callback()
def main(
    ctx: typer.Context,
    db: Optional[Path] = typer.Option(
        None, "--db", help="Database file (default: $FITNESS_AGGREGATOR_DB, else ~/.fitness_aggregator/lifts.db)."
    ),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Settings file (default: $FITNESS_AGGREGATOR_CONFIG, else ./config/settings.toml if present, else ~/.fitness_aggregator/settings.toml)."
    ),
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
        console.print(f"  [green]+[/green] #{ids.get(p.key, '?')} {p.reason}")
    for current, p in result.reactivate:
        console.print(f"  [green]↺[/green] #{current.id} applies again: {p.reason}")
    for current, p in result.update:
        note = " (still reverted)" if current.status == corr.REVERTED else ""
        console.print(f"  [yellow]~[/yellow] #{current.id} updated{note}: {p.reason}")
    for current in result.obsolete:
        console.print(f"  [dim]-[/dim] #{current.id} no longer applies: {current.reason}")


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
        console.print(f"  [yellow]warning:[/yellow] {w}")


def _set_row(s: WorkoutSet, show_rpe: bool) -> list[str]:
    label = str(s.number) if s.set_type is SetType.NORMAL else SET_LABELS[s.set_type]
    if s.seconds and not s.weight and not s.reps:
        weight, amount = "", f"{_num(s.seconds)} s"
    elif s.weight:
        weight, amount = f"{_num(s.weight)} lb", f"× {_num(s.reps)}"
    else:
        weight, amount = "—", f"× {_num(s.reps)}"
    row = [label, weight, amount, format_duration_seconds(s.rest_seconds)]
    if show_rpe:
        row.append("" if s.rpe is None else f"RPE {_num(s.rpe)}")
    row.append(s.notes)
    return row


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
        _fail(console, f"{file}: {exc}")

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
            console.print(f"  [green]new[/green]     {_when(w.started_at)}  {w.name}")
        for e in plan.edited:
            console.print(f"  [yellow]edited[/yellow]  {_when(e.new.started_at)}  {e.new.name}: {e.description}")
        for w in plan.removed:
            console.print(f"  [red]removed[/red] {_when(w.started_at)}  {w.name} (deleted in Strong?)")

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


# --- workouts ---------------------------------------------------------------

def _parse_date(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(f"expected a date like 2026-06-02, got {value!r}") from None


@workouts_app.callback(invoke_without_command=True)
def workouts_list(
    ctx: typer.Context,
    last: int = typer.Option(20, "--last", "-n", help="Show the most recent N sessions."),
    all_: bool = typer.Option(False, "--all", help="Show every session."),
    since: Optional[str] = typer.Option(None, "--since", help="Only sessions on or after this date."),
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
            _when(s.started_at), s.name, _session_duration(s), str(len(s.exercises)),
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
        _fail(state.console, f"no active workout merge for a workout starting {when!r}. "
              "See [bold]fitness corrections --type workout_merge[/bold].")
    if len(candidates) > 1:
        _fail(state.console, f"{when!r} matches several merges ({', '.join(c.workout for c in candidates)}); be more specific.")
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
        _fail(console, f"no session matches {when!r}.")

    for i, session in enumerate(matches):
        if i:
            console.print()
        _print_session(console, session)


def _print_session(console: Console, session: Session) -> None:
    header = Text()
    header.append(session.name, style="bold")
    header.append(f"  {_when(session.started_at)}  ·  ")
    header.append_text(Text.from_markup(_session_duration(session)))
    header.append(f"  ·  {_plural(len(session.exercises), 'exercise')}, {_plural(session.set_count, 'set')}")
    if session.is_merged:
        header.append(f"  ·  {len(session.members)} workouts merged", style="yellow")
    console.print(header)
    if session.notes:
        console.print(f"[italic]{session.notes}[/italic]")

    for m_index, member in enumerate(session.members):
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
            console.print(f"\n[bold]{entry.name}[/bold]")
            console.print(table)

    if session.applied:
        console.print("\n[yellow]Corrections applied:[/yellow]")
        for c in session.applied:
            console.print(f"  #{c.id} {c.reason}")


# --- corrections ------------------------------------------------------------

@corrections_app.callback(invoke_without_command=True)
def corrections_list(
    ctx: typer.Context,
    type_: Optional[str] = typer.Option(None, "--type", help="Only this type, e.g. workout_merge."),
    auto: bool = typer.Option(False, "--auto", help="Only automatic corrections."),
    user: bool = typer.Option(False, "--user", help="Only corrections you made."),
    status: Optional[str] = typer.Option(None, "--status", help="active, reverted, obsolete, or stale."),
    exercise: Optional[str] = typer.Option(None, "--exercise", help="Only corrections affecting this exercise."),
    since: Optional[str] = typer.Option(None, "--since", help="Only corrections created on or after this date."),
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
        _fail(console, f"unknown status {status!r}; use one of {', '.join(corr.STATUSES)}.")
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
            str(c.id), c.type, c.source, f"[{status_style[c.status]}]{c.status}[/]", c.reason
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
        _fail(console, str(exc))
    verb = "Reverted" if status == corr.REVERTED else "Restored"
    console.print(f"{verb} #{updated.id}: {updated.reason}")
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


# --- config -----------------------------------------------------------------

@config_app.command("show")
def config_show(ctx: typer.Context):
    """Show the settings file in use and its values."""
    state = _state(ctx)
    path = settings_mod.resolve_path(state.config_path)
    exists = "" if path.exists() else " (not created yet; using defaults)"
    state.console.print(f"Settings file: {path}{exists}")
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
        _fail(console, str(exc.args[0]))
    key = settings_mod.normalize_key(key)
    console.print(f"{key}: {_num(getattr(old, key))} → {_num(getattr(new, key))}")
    state._settings = new
    if key in settings_mod.AFFECTS_AUTO_CORRECTIONS and state.db_path.exists():
        result = state.store.refresh_auto_corrections(new)
        if result.changed:
            console.print("Automatic corrections updated:")
            _print_reconcile(console, result, state.store)
        else:
            console.print("No automatic corrections changed.")


if __name__ == "__main__":
    app()
