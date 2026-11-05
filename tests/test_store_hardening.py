"""Edge cases for the store: bad database files, the workouts cache, and tricky correction sequences."""

import sqlite3

import pytest
from typer.testing import CliRunner

from fitness_aggregator import corrections as corr
from fitness_aggregator import store as store_mod
from fitness_aggregator.cli import app
from fitness_aggregator.importers import parse_strong_csv
from fitness_aggregator.settings import Settings
from fitness_aggregator.store import RemovalsNotConfirmed, Store, StoreError
from helpers import HEADER, workout_rows, write_export

# Three workouts close enough to chain into one session, then one the next day.
A = workout_rows("2026-06-02 10:00:00", "30m", name="A")
B = workout_rows("2026-06-02 10:40:00", "30m", name="B")
C = workout_rows("2026-06-02 11:20:00", "30m", name="C")
D = workout_rows("2026-06-03 10:00:00", "30m", name="D")


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "lifts.db") as s:
        yield s


def do_import(store, path, settings=None, **kw):
    plan = store.plan_import(parse_strong_csv(path), path)
    return store.apply_import(plan, settings or Settings(), **kw)


def merge_for(store, start):
    return next(c for c in store.corrections(type_=corr.WORKOUT_MERGE) if c.workout == start)


# --- bad database files -----------------------------------------------------------

def test_corrupt_database_gives_store_error(tmp_path):
    path = tmp_path / "lifts.db"
    path.write_text("this is not sqlite " * 50)
    with pytest.raises(StoreError, match="is not a fitness_aggregator database, or it is damaged"):
        Store(path)


def test_database_folder_that_cannot_be_created_gives_store_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(StoreError, match="can't create the database"):
        Store(blocker / "lifts.db")


def test_newer_schema_error_is_a_store_error(tmp_path):
    path = tmp_path / "lifts.db"
    with Store(path) as s:
        s.conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
        s.conn.commit()
    with pytest.raises(StoreError, match="newer version"):
        Store(path)


def open_read_only(monkeypatch):
    """Open databases read-only from now on (chmod doesn't stop root, so use SQLite's read-only mode)."""
    real_connect = sqlite3.connect
    monkeypatch.setattr(store_mod.sqlite3, "connect", lambda p: real_connect(f"file:{p}?mode=ro", uri=True))


def test_read_only_database_can_be_read_but_writes_give_store_error(tmp_path, monkeypatch):
    path = tmp_path / "lifts.db"
    with Store(path) as s:
        do_import(s, write_export(tmp_path / "e.csv", A, B))
    open_read_only(monkeypatch)
    with Store(path) as s:
        assert len(s.load_workouts()) == 2 and len(s.sessions()) == 1
        with pytest.raises(StoreError, match="read-only"):
            s.dismiss_flags(["x"])
        with pytest.raises(StoreError, match="read-only"):
            s.set_correction_status(1, corr.REVERTED, Settings())
        assert s.get_correction(1).status == corr.ACTIVE


def test_cli_reports_read_only_database_without_traceback(tmp_path, monkeypatch):
    path = tmp_path / "lifts.db"
    with Store(path) as s:
        do_import(s, write_export(tmp_path / "e.csv", A, B))
    open_read_only(monkeypatch)
    result = CliRunner().invoke(app, ["--db", str(path), "corrections", "revert", "1"])
    assert result.exit_code == 1 and "read-only" in result.output
    assert not isinstance(result.exception, sqlite3.Error)


# --- workouts cache ----------------------------------------------------------------

def test_cache_sees_imports_and_writes_from_other_connections(store, tmp_path):
    assert store.load_workouts() == []
    do_import(store, write_export(tmp_path / "e.csv", A))
    assert [w.name for w in store.load_workouts()] == ["A"]
    first, second = store.load_workouts(), store.load_workouts()
    assert first is not second and first[0] is second[0]  # fresh list, shared (read-only) workouts

    other = sqlite3.connect(store.path)
    other.execute("UPDATE workouts SET name = 'Renamed'")
    other.commit()
    other.close()
    assert [w.name for w in store.load_workouts()] == ["Renamed"]


def test_cached_workouts_are_not_changed_by_fixes_or_merges(store, tmp_path):
    do_import(store, write_export(tmp_path / "e.csv", A, D))
    w = store.load_workouts()[0]
    store.add_value_fix(w, "Bench Press (Barbell)", 1, {1: {"weight": 145}})
    assert store.sessions()[0].exercises[0].sets[0].weight == 145
    assert store.load_workouts()[0].exercises[0].sets[0].weight == 135


# --- correction sequences -------------------------------------------------------------

def test_reverting_middle_of_a_chain_then_reimporting(store, tmp_path):
    export = write_export(tmp_path / "e.csv", A, B, C)
    do_import(store, export)
    assert [len(s.members) for s in store.sessions()] == [3]
    b, c = merge_for(store, "2026-06-02T10:40:00"), merge_for(store, "2026-06-02T11:20:00")
    assert c.after == {"into": "2026-06-02T10:00:00"}

    store.set_correction_status(b.id, corr.REVERTED, Settings())
    # C now follows B, so it merges into B rather than A; the same correction is updated.
    assert merge_for(store, "2026-06-02T11:20:00").after == {"into": "2026-06-02T10:40:00"}
    assert [[w.name for w in s.members] for s in store.sessions()] == [["A"], ["B", "C"]]

    # A re-import with a new workout keeps your revert and the knock-on change.
    do_import(store, write_export(tmp_path / "e.csv", A, B, C, D))
    assert store.get_correction(b.id).status == corr.REVERTED
    assert [[w.name for w in s.members] for s in store.sessions()] == [["A"], ["B", "C"], ["D"]]

    store.set_correction_status(b.id, corr.ACTIVE, Settings())
    assert merge_for(store, "2026-06-02T11:20:00").after == {"into": "2026-06-02T10:00:00"}
    assert [[w.name for w in s.members] for s in store.sessions()] == [["A", "B", "C"], ["D"]]


def test_settings_changed_back_and_forth(store, tmp_path):
    do_import(store, write_export(tmp_path / "e.csv", A, B, C))
    b = merge_for(store, "2026-06-02T10:40:00")
    store.set_correction_status(b.id, corr.REVERTED, Settings())
    for gap, sessions in ((5, [1, 1, 1]), (60, [1, 2]), (5, [1, 1, 1]), (60, [1, 2])):
        store.refresh_corrections(Settings(merge_gap_minutes=gap))
        assert [len(s.members) for s in store.sessions()] == sessions, gap
        # Your revert survives every recomputation; it never becomes obsolete or active.
        assert store.get_correction(b.id).status == corr.REVERTED
    assert len(store.corrections()) == 2  # no duplicates created along the way


def test_value_fix_and_exercise_merge_on_the_same_set(store, tmp_path):
    curl2 = workout_rows("2026-06-02 10:00:00", exercises=(("Curl 2", ((8, 80),)),))
    curl = workout_rows("2026-06-05 10:00:00", exercises=(("Curl", ((50, 8),)),))
    do_import(store, write_export(tmp_path / "e.csv", curl2, curl))
    fix = store.add_value_fix(store.load_workouts()[0], "Curl 2", 1, {1: {"weight": 80, "reps": 8}})
    merge = store.add_exercise_merge("Curl 2", "Curl", offset=-10)

    entry = store.sessions()[0].exercises[0]
    s = entry.sets[0]
    assert (entry.name, entry.merged_from) == ("Curl", "Curl 2")
    # Fix first (80 × 8), then the offset (70); corrected_from keeps the raw values.
    assert (s.weight, s.reps, s.pre_merge_weight) == (70, 8, 80)
    assert s.corrected_from == {"weight": 8, "reps": 80}
    assert s.correction_ids == (fix.id, merge.id)

    # Undoing the merge leaves the fix in place.
    store.undo_exercise_merge("Curl 2", Settings())
    s = store.sessions()[0].exercises[0].sets[0]
    assert (s.weight, s.reps, s.corrected_from, s.correction_ids) == (80, 8, {"weight": 8, "reps": 80}, (fix.id,))


def test_header_only_export(store, tmp_path):
    empty = write_export(tmp_path / "empty.csv")
    report = do_import(store, empty)
    assert not report.applied and store.workout_count() == 0

    do_import(store, write_export(tmp_path / "e.csv", A, B))
    store.add_value_fix(store.load_workouts()[0], "Bench Press (Barbell)", 1, {1: {"weight": 145}})
    with pytest.raises(RemovalsNotConfirmed):
        do_import(store, empty)
    report = do_import(store, empty, allow_removals=True)
    assert report.applied and store.workout_count() == 0 and store.sessions() == []
    statuses = {c.type: c.status for c in store.corrections()}
    assert statuses == {corr.WORKOUT_MERGE: corr.OBSOLETE, corr.VALUE_FIX: corr.STALE}


def test_workouts_with_the_same_start_time_are_stored_once(store, tmp_path):
    path = tmp_path / "e.csv"
    path.write_text(HEADER + workout_rows("2026-06-02 10:00:00", name="Push")
                    + workout_rows("2026-06-02 10:00:00", name="Pull", exercises=(("Row", ((100, 5),)),)))
    result = parse_strong_csv(path)
    assert len(result.warnings) == 1
    do_import(store, path)
    [w] = store.load_workouts()
    assert w.name == "Push" and [e.name for e in w.exercises] == ["Bench Press (Barbell)", "Row"]
