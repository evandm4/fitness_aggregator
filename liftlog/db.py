import csv
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DEFAULT_DB = Path.home() / ".liftlog.db"
CSV_FIELDS = ["date", "exercise", "weight", "reps", "rpe"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sets (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    exercise TEXT NOT NULL,
    weight REAL NOT NULL CHECK (weight >= 0),
    reps INTEGER NOT NULL CHECK (reps > 0),
    rpe REAL CHECK (rpe IS NULL OR (rpe >= 1 AND rpe <= 10))
);
CREATE INDEX IF NOT EXISTS idx_sets_ex_day ON sets (exercise, day);
"""


@dataclass(frozen=True)
class LiftSet:
    day: str
    exercise: str
    weight: float
    reps: int
    rpe: float | None = None
    id: int | None = None


def norm_exercise(name: str) -> str:
    return " ".join(name.strip().lower().split())


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def add_set(conn, s: LiftSet) -> int:
    date.fromisoformat(s.day)  # validate
    cur = conn.execute(
        "INSERT INTO sets (day, exercise, weight, reps, rpe) VALUES (?,?,?,?,?)",
        (s.day, norm_exercise(s.exercise), s.weight, s.reps, s.rpe),
    )
    conn.commit()
    return cur.lastrowid


def delete_set(conn, set_id: int) -> bool:
    cur = conn.execute("DELETE FROM sets WHERE id = ?", (set_id,))
    conn.commit()
    return cur.rowcount > 0


def get_sets(conn, exercise: str | None = None, since: str | None = None) -> list[LiftSet]:
    q, args = "SELECT * FROM sets WHERE 1=1", []
    if exercise:
        q += " AND exercise = ?"
        args.append(norm_exercise(exercise))
    if since:
        q += " AND day >= ?"
        args.append(since)
    q += " ORDER BY day, id"
    return [LiftSet(r["day"], r["exercise"], r["weight"], r["reps"], r["rpe"], r["id"])
            for r in conn.execute(q, args)]


def export_csv(conn, path) -> int:
    rows = get_sets(conn)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_FIELDS)
        for s in rows:
            w.writerow([s.day, s.exercise, s.weight, s.reps, "" if s.rpe is None else s.rpe])
    return len(rows)


def import_csv(conn, path) -> int:
    """Import rows with columns: date, exercise, weight, reps[, rpe]. Empty file = 0 rows."""
    n = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            rpe = (row.get("rpe") or "").strip()
            add_set(conn, LiftSet(row["date"].strip(), row["exercise"], float(row["weight"]),
                                  int(row["reps"]), float(rpe) if rpe else None))
            n += 1
    return n
