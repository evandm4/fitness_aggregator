"""SQLite persistence.

Raw workouts and sets are stored exactly as imported. Every Strong export holds
the full history, so an import *replaces* the stored raw data with the new
file's contents, then recomputes automatic corrections. Corrections are kept
in their own table and refer to workouts by start time, so they survive the
replacement.

Importing happens in two steps so callers (CLI now, GUI later) can show what
will change and ask before anything is written:

    plan = store.plan_import(parse_result, path)
    ... show plan.new / plan.edited / plan.removed, confirm removals ...
    report = store.apply_import(plan, settings, allow_removals=True)
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from . import corrections as corr
from . import fixes, merges, outliers, similarity
from .catalog import Catalog
from .corrections import Correction, PlannedCorrection, ReconcileResult, reconcile
from .importers.strong_csv import ParseResult
from .models import ExerciseEntry, SetType, Workout, WorkoutSet
from .sessions import Session, build_sessions, plan_auto_corrections, reverted_keys, workout_key
from .settings import DEFAULT_DIR, Settings

DB_ENV_VAR = "FITNESS_AGGREGATOR_DB"
SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workouts (
    started_at       TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    duration_seconds INTEGER,
    notes            TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sets (
    workout_started_at TEXT NOT NULL REFERENCES workouts(started_at) ON DELETE CASCADE,
    entry_index        INTEGER NOT NULL,
    exercise           TEXT NOT NULL,
    position           INTEGER NOT NULL,
    set_type           TEXT NOT NULL,
    number             INTEGER,
    weight             REAL NOT NULL,
    reps               REAL NOT NULL,
    distance           REAL NOT NULL,
    seconds            REAL NOT NULL,
    rpe                REAL,
    notes              TEXT NOT NULL DEFAULT '',
    rest_seconds       REAL,
    PRIMARY KEY (workout_started_at, entry_index, position)
);

CREATE TABLE IF NOT EXISTS corrections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT NOT NULL,
    source     TEXT NOT NULL,
    target     TEXT NOT NULL,
    before     TEXT NOT NULL,
    after      TEXT NOT NULL,
    reason     TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (type, target)
);

-- Outlier flags you dismissed in `fitness check` (added in schema 2).
CREATE TABLE IF NOT EXISTS dismissed_flags (
    flag_key     TEXT PRIMARY KEY,
    dismissed_at TEXT NOT NULL
);

-- Exercise pairs you chose to keep separate in `fitness merge review` (added in schema 3).
-- Stored with a < b so each pair has one row.
CREATE TABLE IF NOT EXISTS separate_exercises (
    a          TEXT NOT NULL,
    b          TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (a, b)
);

CREATE TABLE IF NOT EXISTS imports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at    TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    source_sha256  TEXT NOT NULL,
    latest_workout TEXT,
    workouts       INTEGER NOT NULL,
    sets           INTEGER NOT NULL,
    summary        TEXT NOT NULL
);
"""


class ImportRefused(Exception):
    """An import was refused because it needs an explicit go-ahead."""


class OlderExportError(ImportRefused):
    pass


class RemovalsNotConfirmed(ImportRefused):
    pass


def default_db_path() -> Path:
    if os.environ.get(DB_ENV_VAR):
        return Path(os.environ[DB_ENV_VAR])
    return DEFAULT_DIR / "lifts.db"


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def file_sha256(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --- import planning --------------------------------------------------------

@dataclass
class WorkoutEdit:
    old: Workout
    new: Workout
    description: str


@dataclass
class ImportPlan:
    result: ParseResult
    source_path: str
    sha256: str
    new: list[Workout] = field(default_factory=list)
    edited: list[WorkoutEdit] = field(default_factory=list)
    removed: list[Workout] = field(default_factory=list)
    unchanged: int = 0
    stored_latest: datetime | None = None
    file_latest: datetime | None = None

    @property
    def is_first_import(self) -> bool:
        return self.stored_latest is None

    @property
    def is_older(self) -> bool:
        return (
            self.stored_latest is not None
            and self.file_latest is not None
            and self.file_latest < self.stored_latest
        )

    @property
    def has_changes(self) -> bool:
        return bool(self.new or self.edited or self.removed)


@dataclass
class ImportReport:
    plan: ImportPlan
    corrections: ReconcileResult
    applied: bool


def describe_edit(old: Workout, new: Workout) -> str:
    parts = []
    if old.name != new.name:
        parts.append(f"renamed {old.name!r} → {new.name!r}")
    if old.duration != new.duration:
        parts.append(
            f"duration {corr.format_duration(old.duration)} → {corr.format_duration(new.duration)}"
        )
    if old.notes != new.notes:
        parts.append("workout notes changed")
    old_names = [e.name for e in old.exercises]
    new_names = [e.name for e in new.exercises]
    added = [n for n in new_names if n not in old_names]
    dropped = [n for n in old_names if n not in new_names]
    if added:
        parts.append("added " + ", ".join(added))
    if dropped:
        parts.append("removed " + ", ".join(dropped))
    if old.set_count != new.set_count:
        parts.append(f"sets {old.set_count} → {new.set_count}")
    if not parts and old.exercises != new.exercises:
        parts.append("set values changed")
    return "; ".join(parts) or "changed"


# --- the store ---------------------------------------------------------------

class Store:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(SCHEMA)
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
                )
            elif int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database {self.path} was created by a newer version (schema {row['value']})"
                )
            elif int(row["value"]) < SCHEMA_VERSION:
                # Migrations so far only add tables, which SCHEMA creates above.
                self.conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),)
                )

    # --- raw data ---------------------------------------------------------

    def load_workouts(self) -> list[Workout]:
        workouts: dict[str, Workout] = {}
        for r in self.conn.execute("SELECT * FROM workouts ORDER BY started_at"):
            workouts[r["started_at"]] = Workout(
                started_at=datetime.fromisoformat(r["started_at"]),
                name=r["name"],
                duration=None if r["duration_seconds"] is None else timedelta(seconds=r["duration_seconds"]),
                notes=r["notes"],
            )
        rows = self.conn.execute(
            "SELECT * FROM sets ORDER BY workout_started_at, entry_index, position"
        )
        last: tuple[str, int] | None = None
        for r in rows:
            w = workouts[r["workout_started_at"]]
            if last != (r["workout_started_at"], r["entry_index"]):
                w.exercises.append(ExerciseEntry(name=r["exercise"]))
                last = (r["workout_started_at"], r["entry_index"])
            w.exercises[-1].sets.append(
                WorkoutSet(
                    position=r["position"],
                    set_type=SetType(r["set_type"]),
                    number=r["number"],
                    weight=r["weight"],
                    reps=r["reps"],
                    distance=r["distance"],
                    seconds=r["seconds"],
                    rpe=r["rpe"],
                    notes=r["notes"],
                    rest_seconds=r["rest_seconds"],
                )
            )
        return list(workouts.values())

    def latest_workout(self) -> datetime | None:
        row = self.conn.execute("SELECT MAX(started_at) AS m FROM workouts").fetchone()
        return datetime.fromisoformat(row["m"]) if row["m"] else None

    def workout_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0]

    def _replace_workouts(self, workouts: Iterable[Workout]) -> None:
        self.conn.execute("DELETE FROM sets")
        self.conn.execute("DELETE FROM workouts")
        for w in workouts:
            key = _iso(w.started_at)
            self.conn.execute(
                "INSERT INTO workouts (started_at, name, duration_seconds, notes) VALUES (?, ?, ?, ?)",
                (
                    key,
                    w.name,
                    None if w.duration is None else int(w.duration.total_seconds()),
                    w.notes,
                ),
            )
            self.conn.executemany(
                "INSERT INTO sets (workout_started_at, entry_index, exercise, position, set_type, number,"
                " weight, reps, distance, seconds, rpe, notes, rest_seconds)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        key, i, e.name, s.position, s.set_type.value, s.number, s.weight, s.reps,
                        s.distance, s.seconds, s.rpe, s.notes, s.rest_seconds,
                    )
                    for i, e in enumerate(w.exercises)
                    for s in e.sets
                ],
            )

    # --- importing --------------------------------------------------------

    def plan_import(self, result: ParseResult, source_path: str | os.PathLike) -> ImportPlan:
        stored = {w.started_at: w for w in self.load_workouts()}
        incoming = {w.started_at: w for w in result.workouts}
        plan = ImportPlan(
            result=result,
            source_path=str(source_path),
            sha256=file_sha256(source_path),
            stored_latest=max(stored) if stored else None,
            file_latest=max(incoming) if incoming else None,
        )
        for started_at, new in incoming.items():
            old = stored.get(started_at)
            if old is None:
                plan.new.append(new)
            elif old != new:
                plan.edited.append(WorkoutEdit(old, new, describe_edit(old, new)))
            else:
                plan.unchanged += 1
        plan.removed = [w for s, w in sorted(stored.items()) if s not in incoming]
        return plan

    def apply_import(
        self,
        plan: ImportPlan,
        settings: Settings,
        *,
        allow_removals: bool = False,
        force: bool = False,
    ) -> ImportReport:
        if plan.is_older and not force:
            raise OlderExportError(
                f"this export's latest workout ({plan.file_latest:%Y-%m-%d %H:%M}) is older than "
                f"what's stored ({plan.stored_latest:%Y-%m-%d %H:%M})"
            )
        if plan.removed and not allow_removals:
            raise RemovalsNotConfirmed(
                f"{len(plan.removed)} stored workout(s) are not in this export"
            )
        if not plan.has_changes:
            return ImportReport(plan=plan, corrections=ReconcileResult(), applied=False)

        with self.conn:
            self._replace_workouts(plan.result.workouts)
            reconciled = self._refresh_corrections(settings)
            self.conn.execute(
                "INSERT INTO imports (imported_at, source_path, source_sha256, latest_workout,"
                " workouts, sets, summary) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _iso(_now()),
                    plan.source_path,
                    plan.sha256,
                    _iso(plan.file_latest) if plan.file_latest else None,
                    plan.result.stats.workouts,
                    plan.result.stats.sets,
                    json.dumps(
                        {
                            "new": len(plan.new),
                            "edited": len(plan.edited),
                            "removed": len(plan.removed),
                            "unchanged": plan.unchanged,
                        }
                    ),
                ),
            )
        return ImportReport(plan=plan, corrections=reconciled, applied=True)

    def imports(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM imports ORDER BY id"))

    # --- corrections ------------------------------------------------------

    @staticmethod
    def _row_to_correction(r: sqlite3.Row) -> Correction:
        return Correction(
            id=r["id"],
            type=r["type"],
            source=r["source"],
            target=json.loads(r["target"]),
            before=json.loads(r["before"]),
            after=json.loads(r["after"]),
            reason=r["reason"],
            status=r["status"],
            created_at=datetime.fromisoformat(r["created_at"]),
            updated_at=datetime.fromisoformat(r["updated_at"]),
        )

    def corrections(
        self,
        *,
        type_: str | None = None,
        source: str | None = None,
        statuses: Iterable[str] | None = None,
        since: datetime | None = None,
    ) -> list[Correction]:
        sql, args = "SELECT * FROM corrections WHERE 1=1", []
        if type_:
            sql += " AND type = ?"
            args.append(type_)
        if source:
            sql += " AND source = ?"
            args.append(source)
        if statuses:
            statuses = list(statuses)
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args.extend(statuses)
        if since:
            sql += " AND created_at >= ?"
            args.append(_iso(since))
        sql += " ORDER BY id"
        return [self._row_to_correction(r) for r in self.conn.execute(sql, args)]

    def get_correction(self, correction_id: int) -> Correction | None:
        r = self.conn.execute("SELECT * FROM corrections WHERE id = ?", (correction_id,)).fetchone()
        return self._row_to_correction(r) if r else None

    def _refresh_corrections(self, settings: Settings) -> ReconcileResult:
        existing = self.corrections()
        workouts = self.load_workouts()
        planned = plan_auto_corrections(workouts, settings, reverted_keys(existing))
        result = reconcile(existing, planned)
        now = _iso(_now())

        # Your own corrections: stale when the raw data no longer has what they were made against.
        for c in existing:
            if c.source != corr.USER or c.status not in (corr.ACTIVE, corr.STALE):
                continue
            matches = self._user_correction_matches(c, workouts)
            if c.status == corr.ACTIVE and not matches:
                result.stale.append(c)
                self.conn.execute(
                    "UPDATE corrections SET status = ?, updated_at = ? WHERE id = ?", (corr.STALE, now, c.id)
                )
            elif c.status == corr.STALE and matches:
                result.unstale.append(c)
                self.conn.execute(
                    "UPDATE corrections SET status = ?, updated_at = ? WHERE id = ?", (corr.ACTIVE, now, c.id)
                )
        for p in result.insert:
            self._insert_correction(p, now)
        for current, p in result.update:
            self.conn.execute(
                "UPDATE corrections SET before = ?, after = ?, reason = ?, updated_at = ? WHERE id = ?",
                (json.dumps(p.before), json.dumps(p.after), p.reason, now, current.id),
            )
        for current, p in result.reactivate:
            self.conn.execute(
                "UPDATE corrections SET before = ?, after = ?, reason = ?, status = ?, updated_at = ?"
                " WHERE id = ?",
                (json.dumps(p.before), json.dumps(p.after), p.reason, corr.ACTIVE, now, current.id),
            )
        for current in result.obsolete:
            self.conn.execute(
                "UPDATE corrections SET status = ?, updated_at = ? WHERE id = ?",
                (corr.OBSOLETE, now, current.id),
            )
        return result

    def refresh_corrections(self, settings: Settings) -> ReconcileResult:
        """Recompute automatic corrections and re-check yours (after a settings change, for example)."""
        with self.conn:
            return self._refresh_corrections(settings)

    def set_correction_status(
        self, correction_id: int, status: str, settings: Settings
    ) -> tuple[Correction, ReconcileResult]:
        """Revert or restore a correction, then recompute knock-on automatic corrections.

        Returns the updated correction and what else changed as a result (for
        example, reverting one merge can change whether the next workout merges).
        """
        if status not in (corr.ACTIVE, corr.REVERTED):
            raise ValueError("status must be 'active' or 'reverted'")
        current = self.get_correction(correction_id)
        if current is None:
            raise KeyError(f"no correction with id {correction_id}")
        if current.status == corr.OBSOLETE:
            raise ValueError(
                f"correction {correction_id} is obsolete (its condition no longer applies)"
            )
        if status == corr.ACTIVE and current.source == corr.USER:
            workouts = self.load_workouts()
            if not self._user_correction_matches(current, workouts):
                raise ValueError(
                    f"correction {correction_id} no longer matches the imported data "
                    "(it was probably changed in Strong); make a new one instead"
                )
            if current.type == corr.EXERCISE_MERGE:
                others = merges.active_merges(self.corrections(statuses=[corr.ACTIVE]))
                others.pop(current.target["exercise"], None)
                merges.check_merge(current.target["exercise"], current.after["into"], others,
                                   self.exercise_names(workouts))
        with self.conn:
            self.conn.execute(
                "UPDATE corrections SET status = ?, updated_at = ? WHERE id = ?",
                (status, _iso(_now()), correction_id),
            )
            knock_on = self._refresh_corrections(settings)
        return self.get_correction(correction_id), knock_on

    # --- views ------------------------------------------------------------

    def sessions(self, *, raw: bool = False) -> list[Session]:
        """Workouts as sessions with active corrections applied (or none, if ``raw``)."""
        workouts = self.load_workouts()
        if raw:
            return build_sessions(workouts)
        active = self.corrections(statuses=[corr.ACTIVE])
        fixed = fixes.apply_value_fixes(workouts, active)
        return build_sessions(merges.apply_exercise_merges(fixed, active), active)

    @staticmethod
    def exercise_names(workouts: Iterable[Workout]) -> set[str]:
        return {e.name for w in workouts for e in w.exercises}

    def _user_correction_matches(self, c: Correction, workouts: list[Workout]) -> bool:
        if c.type == corr.VALUE_FIX:
            return fixes.raw_matches({workout_key(w.started_at): w for w in workouts}, c)
        if c.type == corr.EXERCISE_MERGE:
            return merges.merges_valid_against(c, self.exercise_names(workouts))
        return True

    def catalog(self, overrides_path: Path | None = None) -> Catalog:
        return Catalog.build(self.load_workouts(), overrides_path)

    def find_workouts(self, when: str) -> list[Workout]:
        """Raw workouts whose start time begins with ``when`` (e.g. ``2026-06-04`` or ``2026-06-04T13:53``)."""
        text = when.strip().replace(" ", "T")
        return [w for w in self.load_workouts() if workout_key(w.started_at).startswith(text)]

    # --- value fixes --------------------------------------------------------

    def add_value_fix(
        self,
        workout: Workout,
        exercise: str,
        occurrence: int,
        changes: dict[int, dict[str, float]],
    ) -> Correction:
        """Record a fix to set values. Fixing the same exercise entry again extends its correction."""
        target = fixes.entry_target(workout, exercise, occurrence)
        existing = next(
            (c for c in self.corrections(type_=corr.VALUE_FIX) if c.target == target), None
        )
        planned = fixes.plan_value_fix(workout, exercise, occurrence, changes, existing)
        now = _iso(_now())
        with self.conn:
            if existing is None:
                cur = self._insert_correction(planned, now)
                correction_id = cur.lastrowid
            else:
                self.conn.execute(
                    "UPDATE corrections SET before = ?, after = ?, reason = ?, status = ?, updated_at = ?"
                    " WHERE id = ?",
                    (json.dumps(planned.before), json.dumps(planned.after), planned.reason, corr.ACTIVE,
                     now, existing.id),
                )
                correction_id = existing.id
        return self.get_correction(correction_id)

    def _insert_correction(self, p: PlannedCorrection, now: str, status: str = corr.ACTIVE):
        return self.conn.execute(
            "INSERT INTO corrections (type, source, target, before, after, reason, status,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p.type, p.source, json.dumps(p.target, sort_keys=True), json.dumps(p.before),
                json.dumps(p.after), p.reason, status, now, now,
            ),
        )

    # --- outlier flags --------------------------------------------------------

    def open_flags(self, catalog: Catalog | None = None) -> list[outliers.Flag]:
        """Likely data-entry errors not yet fixed or dismissed."""
        workouts = self.load_workouts()
        catalog = catalog or Catalog.build(workouts)
        dismissed = self.dismissed_flag_keys()
        covered = fixes.covered_positions(self.corrections(statuses=[corr.ACTIVE]))
        return [
            f for f in outliers.detect(workouts, catalog)
            if f.key not in dismissed and f.location not in covered
        ]

    def dismissed_flag_keys(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT flag_key FROM dismissed_flags")}

    def dismiss_flags(self, keys: Iterable[str]) -> None:
        now = _iso(_now())
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO dismissed_flags (flag_key, dismissed_at) VALUES (?, ?)",
                [(k, now) for k in keys],
            )

    def corrections_for_workouts(self, keys: Iterable[str]) -> list[Correction]:
        keys = set(keys)
        return [c for c in self.corrections() if c.workout in keys]

    # --- combining exercises -------------------------------------------------

    def active_merges(self) -> dict[str, merges.Merge]:
        return merges.active_merges(self.corrections(statuses=[corr.ACTIVE]))

    def add_exercise_merge(
        self, source: str, into: str, *, offset: float | None = None, factor: float | None = None
    ) -> Correction:
        """Combine ``source`` into ``into``. Combining the same exercise again updates its correction."""
        current = self.active_merges()
        current.pop(source, None)
        merges.check_merge(source, into, current, self.exercise_names(self.load_workouts()), offset, factor)
        planned = merges.plan_merge(source, into, offset, factor)
        existing = next((c for c in self.corrections(type_=corr.EXERCISE_MERGE) if c.target == planned.target), None)
        now = _iso(_now())
        with self.conn:
            if existing is None:
                correction_id = self._insert_correction(planned, now).lastrowid
            else:
                self.conn.execute(
                    "UPDATE corrections SET after = ?, reason = ?, status = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(planned.after), planned.reason, corr.ACTIVE, now, existing.id),
                )
                correction_id = existing.id
            self.conn.execute(
                "DELETE FROM separate_exercises WHERE a = ? AND b = ?", tuple(sorted((source, into)))
            )
        return self.get_correction(correction_id)

    def undo_exercise_merge(self, source: str, settings: Settings) -> Correction:
        c = next((c for c in self.corrections(type_=corr.EXERCISE_MERGE, statuses=[corr.ACTIVE])
                  if c.target["exercise"] == source), None)
        if c is None:
            raise KeyError(f"{source} isn't combined into anything")
        updated, _ = self.set_correction_status(c.id, corr.REVERTED, settings)
        return updated

    def separate_pairs(self) -> set[frozenset[str]]:
        return {frozenset((a, b)) for a, b in self.conn.execute("SELECT a, b FROM separate_exercises")}

    def keep_separate(self, names: Iterable[str]) -> int:
        """Record that these exercises are different; returns how many pairs were recorded."""
        names = sorted(set(names))
        pairs = [(a, b) for i, a in enumerate(names) for b in names[i + 1:]]
        now = _iso(_now())
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO separate_exercises (a, b, decided_at) VALUES (?, ?, ?)",
                [(a, b, now) for a, b in pairs],
            )
        return len(pairs)

    def forget_separate(self, a: str, b: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM separate_exercises WHERE a = ? AND b = ?", tuple(sorted((a, b))))
        return cur.rowcount > 0

    def merge_candidates(self) -> list[similarity.CandidateGroup]:
        """Similar exercises not yet decided on, using current names (merges resolved)."""
        current = self.active_merges()
        names = {merges.canonical(n, current) for n in self.exercise_names(self.load_workouts())}
        return similarity.find_candidates(names, self.separate_pairs())

    def top_sets(self, exercise: str) -> list[similarity.TopSet]:
        """Top set per workout for an exercise as recorded (set fixes applied, not merges)."""
        workouts = fixes.apply_value_fixes(self.load_workouts(), self.corrections(statuses=[corr.ACTIVE]))
        return similarity.top_sets(workouts, exercise)

    def merged_sources(self, into: str) -> list[str]:
        return sorted(m.source for m in self.active_merges().values() if m.into == into)


__all__ = [
    "ImportPlan",
    "ImportRefused",
    "ImportReport",
    "OlderExportError",
    "RemovalsNotConfirmed",
    "Store",
    "WorkoutEdit",
    "default_db_path",
    "workout_key",
]
