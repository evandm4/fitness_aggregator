"""``fitness export``: write corrected (or raw) data as CSV or JSON for spreadsheets and other tools.

Registered on the main app from the end of ``cli.py``; importing ``cli`` here
(not the other way round) avoids a circular import.
"""

from __future__ import annotations

import csv
import io
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import typer
from rich.console import Console
from rich.markup import escape

from . import metrics
from .cli import FORMULA_HELP, RAW_HELP, WARMUP_HELP, _analysis, _check_formula, _parse_date, _state, app

WHATS = ("sets", "sessions", "exercises", "prs", "corrections")
FORMATS = ("csv", "json")

SESSION_COLUMNS = [
    "session", "name", "duration_min", "duration_trusted", "workouts", "merged",
    "exercises", "exercise_names", "sets", "reps", "volume",
]
CORRECTION_COLUMNS = [
    "id", "type", "source", "status", "workout", "target", "before", "after", "reason", "created_at", "updated_at",
]


# --- value conversion -----------------------------------------------------------

def _plain(value, *, for_json: bool):
    """A JSON/CSV-friendly version of a pandas/numpy/datetime value. Missing values become None."""
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float):
        # Round away float noise (62.99999999999999); weights and reps never need more than 4 places.
        return None if math.isnan(value) else round(value, 4)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat(sep="T" if for_json else " ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (timedelta, pd.Timedelta)):
        return value.total_seconds()
    if isinstance(value, (dict, list, tuple)) and not for_json:
        return json.dumps(value, sort_keys=True)
    return value


def _records(df: pd.DataFrame, columns: list[str]) -> list[dict]:
    if df.empty:
        return []
    return [dict(zip(columns, row, strict=True)) for row in df[columns].itertuples(index=False, name=None)]


def render(records: list[dict], columns: list[str], fmt: str) -> str:
    if fmt == "json":
        rows = [{c: _plain(r.get(c), for_json=True) for c in columns} for r in records]
        return json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for r in records:
        writer.writerow(["" if (v := _plain(r.get(c), for_json=False)) is None else v for c in columns])
    return buf.getvalue()


# --- datasets ---------------------------------------------------------------------

def _in_window(series: pd.Series, since: datetime | None, until: datetime | None) -> pd.Series:
    """Rows on or after ``since`` and on or before the end of ``until``'s day."""
    keep = pd.Series(True, index=series.index)
    if since is not None:
        keep &= series >= pd.Timestamp(since)
    if until is not None:
        keep &= series < pd.Timestamp(until.date()) + pd.Timedelta(days=1)
    return keep


def _sessions_records(sessions, df: pd.DataFrame) -> list[dict]:
    by_session = {k: g for k, g in df.groupby("session")} if not df.empty else {}
    out = []
    for s in sessions:
        g = by_session.get(pd.Timestamp(s.started_at))
        names = list(dict.fromkeys(g["exercise"])) if g is not None else []
        out.append({
            "session": s.started_at,
            "name": s.name,
            "duration_min": None if s.duration is None else round(s.duration.total_seconds() / 60, 1),
            "duration_trusted": s.duration_trusted,
            "workouts": len(s.members),
            "merged": s.is_merged,
            "exercises": len(names),
            "exercise_names": "; ".join(names),
            "sets": 0 if g is None else len(g),
            "reps": 0.0 if g is None else float(g["reps"].sum()),
            "volume": 0.0 if g is None else float(g["volume"].sum()),
        })
    return out


def _corrections_records(store, since, until) -> list[dict]:
    out = []
    for c in store.corrections():
        if since is not None and c.created_at < since:
            continue
        if until is not None and c.created_at.date() > until.date():
            continue
        out.append({
            "id": c.id, "type": c.type, "source": c.source, "status": c.status, "workout": c.workout,
            "target": c.target, "before": c.before, "after": c.after, "reason": c.reason,
            "created_at": c.created_at, "updated_at": c.updated_at,
        })
    return out


# --- the command ---------------------------------------------------------------------

@app.command("export")
def export_cmd(
    ctx: typer.Context,
    what: str = typer.Option("sets", "--what", "-w", help="sets, sessions, exercises, prs, or corrections."),
    fmt: str = typer.Option("csv", "--format", "-f", help="csv or json."),
    out: Path | None = typer.Option(None, "--out", "-o", help="File to write (default: standard output)."),
    since: str | None = typer.Option(None, "--since", help="Only from this date (e.g. 2026-09-01)."),
    until: str | None = typer.Option(None, "--until", help="Only up to and including this date."),
    raw: bool = typer.Option(False, "--raw", help=RAW_HELP + " Not used for corrections."),
    include_warmups: bool = typer.Option(False, "--include-warmups", help=WARMUP_HELP),
    formula: str = typer.Option("epley", "--formula", help=FORMULA_HELP),
):
    """Export your data (corrections applied, unless --raw) as CSV or JSON.

    \b
    sets         one row per set (warm-ups only with --include-warmups)
    sessions     one row per session: start, name, duration, exercises, sets, volume, merged
    exercises    one row per exercise: how often, first/last, and bests (--since/--until limit the period)
    prs          every PR, in order (computed on your full history, then filtered by date)
    corrections  the corrections log (--since/--until filter by when each was made)
    """
    state = _state(ctx)
    err = Console(stderr=True, highlight=False)
    what, fmt = what.strip().lower(), fmt.strip().lower()
    if what not in WHATS:
        raise typer.BadParameter(f"choose one of {', '.join(WHATS)}", param_hint="--what")
    if fmt not in FORMATS:
        raise typer.BadParameter(f"choose csv or json, got {fmt!r}", param_hint="--format")
    _check_formula(err, formula)
    start = _parse_date(since) if since else None
    end = _parse_date(until, "--until") if until else None
    store = state.store

    if what == "corrections":
        columns, records = CORRECTION_COLUMNS, _corrections_records(store, start, end)
    else:
        if store.workout_count():
            a = _analysis(state, raw=raw, include_warmups=include_warmups)
            sessions, df = a.sessions, a.df
        else:
            sessions, df = [], pd.DataFrame(columns=metrics.SET_COLUMNS)
        if what == "sets":
            columns = list(metrics.SET_COLUMNS)
            records = _records(df[_in_window(df["session"], start, end)], columns)
        elif what == "sessions":
            columns = SESSION_COLUMNS
            sessions = [s for s in sessions
                        if (start is None or s.started_at >= start)
                        and (end is None or s.started_at.date() <= end.date())]
            records = _sessions_records(sessions, df)
        elif what == "exercises":
            window = df[_in_window(df["session"], start, end)]
            summary = metrics.exercise_summary(window)
            if not window.empty:
                summary = summary.merge(metrics.current_bests(window, formula), on=["exercise", "kind"],
                                        how="left", suffixes=("", "_bests"))
                summary = summary.drop(columns=[c for c in summary.columns if c.endswith("_bests")])
            columns = list(summary.columns)
            records = _records(summary, columns)
        else:  # prs
            events = metrics.pr_events(df, formula) if not df.empty else pd.DataFrame(
                columns=["session", "exercise", "kind", "pr_type", "value", "previous", "detail"])
            columns = list(events.columns)
            records = _records(events[_in_window(events["session"], start, end)], columns)

    text = render(records, columns, fmt)
    if out is None:
        typer.echo(text, nl=False)
        return
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    except OSError as exc:
        err.print(f"[red]Error:[/red] can't write {escape(str(out))}: {escape(exc.strerror or str(exc))}")
        raise typer.Exit(1) from None
    label = what if len(records) != 1 else what.rstrip("s")
    err.print(f"Wrote {len(records):,} {label} to {escape(str(out))} ({fmt}).")
