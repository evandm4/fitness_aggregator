from datetime import datetime

import pytest

from fitness_aggregator import corrections as corr
from fitness_aggregator.importers import parse_strong_csv
from fitness_aggregator.settings import Settings
from fitness_aggregator.store import OlderExportError, RemovalsNotConfirmed, Store
from helpers import REAL_EXPORT, workout_rows, write_export

A = workout_rows("2026-06-02 14:00:00", "3m", name="Midday")
B = workout_rows("2026-06-02 14:06:00", "47m", name="Midday")
C = workout_rows("2026-06-04 13:00:00", "1h", name="Pull 1")
D = workout_rows("2026-06-06 13:00:00", "1h", name="Legs")


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "lifts.db") as s:
        yield s


def do_import(store, path, settings=Settings(), **kw):
    plan = store.plan_import(parse_strong_csv(path), path)
    return store.apply_import(plan, settings, **kw)


def test_first_import_stores_everything_and_creates_auto_corrections(store, tmp_path):
    report = do_import(store, write_export(tmp_path / "e.csv", A, B, C))
    assert report.applied and report.plan.is_first_import
    assert store.workout_count() == 3
    assert [c.type for c in store.corrections()] == [corr.WORKOUT_MERGE]
    assert len(store.sessions()) == 2
    assert len(store.sessions(raw=True)) == 3
    assert len(store.imports()) == 1


def test_round_trip_matches_parsed_data(store, tmp_path):
    path = write_export(tmp_path / "e.csv", A, B, C)
    do_import(store, path)
    assert store.load_workouts() == parse_strong_csv(path).workouts


def test_reimporting_same_file_changes_nothing(store, tmp_path):
    path = write_export(tmp_path / "e.csv", A, B, C)
    do_import(store, path)
    report = do_import(store, path)
    assert not report.applied and not report.plan.has_changes
    assert report.plan.unchanged == 3
    assert len(store.imports()) == 1


def test_new_and_edited_workouts_detected(store, tmp_path):
    do_import(store, write_export(tmp_path / "1.csv", A, B, C))
    edited_c = workout_rows("2026-06-04 13:00:00", "1h", name="Pull 1",
                            exercises=(("Bench Press (Barbell)", ((135, 8), (140, 6))),))
    plan = store.plan_import(parse_strong_csv(write_export(tmp_path / "2.csv", A, B, edited_c, D)),
                             tmp_path / "2.csv")
    assert [w.name for w in plan.new] == ["Legs"]
    assert [e.description for e in plan.edited] == ["set values changed"]
    assert plan.unchanged == 2 and plan.removed == []


def test_edit_description_lists_structural_changes(store, tmp_path):
    do_import(store, write_export(tmp_path / "1.csv", C))
    more = workout_rows("2026-06-04 13:00:00", "1h 5m", name="Pull 1",
                        exercises=(("Bench Press (Barbell)", ((135, 8), (135, 6))),
                                   ("Row", ((100, 10),))))
    plan = store.plan_import(parse_strong_csv(write_export(tmp_path / "2.csv", more)), tmp_path / "2.csv")
    assert plan.edited[0].description == "duration 1h → 1h 5m; added Row; sets 2 → 3"


def test_removed_workouts_need_confirmation(store, tmp_path):
    do_import(store, write_export(tmp_path / "1.csv", A, B, C, D))
    path = write_export(tmp_path / "2.csv", A, B, D)
    with pytest.raises(RemovalsNotConfirmed):
        do_import(store, path)
    assert store.workout_count() == 4  # nothing written
    report = do_import(store, path, allow_removals=True)
    assert [w.name for w in report.plan.removed] == ["Pull 1"]
    assert store.workout_count() == 3


def test_older_export_refused_unless_forced(store, tmp_path):
    do_import(store, write_export(tmp_path / "new.csv", A, B, C, D))
    old = write_export(tmp_path / "old.csv", A, B, C)
    with pytest.raises(OlderExportError):
        do_import(store, old, allow_removals=True)
    assert store.workout_count() == 4
    do_import(store, old, allow_removals=True, force=True)
    assert store.workout_count() == 3


def test_corrections_keep_ids_across_reimports(store, tmp_path):
    do_import(store, write_export(tmp_path / "1.csv", A, B, C))
    before = store.corrections()
    do_import(store, write_export(tmp_path / "2.csv", A, B, C, D))
    assert [(c.id, c.status) for c in store.corrections()] == [(c.id, c.status) for c in before]


def test_revert_persists_across_reimport(store, tmp_path):
    do_import(store, write_export(tmp_path / "1.csv", A, B, C))
    merge = store.corrections()[0]
    store.set_correction_status(merge.id, corr.REVERTED, Settings())
    assert len(store.sessions()) == 3
    do_import(store, write_export(tmp_path / "2.csv", A, B, C, D))
    assert store.get_correction(merge.id).status == corr.REVERTED
    assert len(store.sessions()) == 4


def test_restore_reapplies(store, tmp_path):
    do_import(store, write_export(tmp_path / "1.csv", A, B, C))
    merge = store.corrections()[0]
    store.set_correction_status(merge.id, corr.REVERTED, Settings())
    updated, _ = store.set_correction_status(merge.id, corr.ACTIVE, Settings())
    assert updated.status == corr.ACTIVE and len(store.sessions()) == 2


def test_correction_becomes_obsolete_when_workout_deleted_and_returns(store, tmp_path):
    do_import(store, write_export(tmp_path / "1.csv", A, B, C))
    merge_id = store.corrections()[0].id
    report = do_import(store, write_export(tmp_path / "2.csv", A, C), allow_removals=True)
    assert [c.id for c in report.corrections.obsolete] == [merge_id]
    assert store.get_correction(merge_id).status == corr.OBSOLETE
    with pytest.raises(ValueError, match="obsolete"):
        store.set_correction_status(merge_id, corr.REVERTED, Settings())
    report = do_import(store, write_export(tmp_path / "3.csv", A, B, C))
    assert [c.id for c, _ in report.corrections.reactivate] == [merge_id]
    assert store.get_correction(merge_id).status == corr.ACTIVE


def test_settings_change_recomputes_auto_corrections(store, tmp_path):
    do_import(store, write_export(tmp_path / "1.csv", A, B, C))
    result = store.refresh_auto_corrections(Settings(merge_gap_minutes=2))
    assert len(result.obsolete) == 1 and len(store.sessions()) == 3


def test_revert_reports_knock_on_changes(store, tmp_path):
    three = [workout_rows("2026-06-02 10:00:00", "30m"),
             workout_rows("2026-06-02 10:40:00", "30m"),
             workout_rows("2026-06-02 11:20:00", "30m")]
    do_import(store, write_export(tmp_path / "1.csv", *three))
    first = store.corrections()[0]
    _, knock_on = store.set_correction_status(first.id, corr.REVERTED, Settings())
    assert [p.after["into"] for _, p in knock_on.update] == ["2026-06-02T10:40:00"]


def test_unknown_correction_id(store):
    with pytest.raises(KeyError):
        store.set_correction_status(99, corr.REVERTED, Settings())


def test_newer_schema_refused(tmp_path):
    path = tmp_path / "lifts.db"
    with Store(path) as s:
        s.conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
        s.conn.commit()
    with pytest.raises(RuntimeError, match="newer version"):
        Store(path)


@pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not present")
def test_real_export_end_to_end(store):
    report = do_import(store, REAL_EXPORT)
    assert store.workout_count() == 104
    sessions = store.sessions()
    assert len(sessions) == 103
    merged = [s for s in sessions if s.is_merged]
    assert [s.started_at for s in merged] == [datetime(2026, 6, 2, 14, 0, 32)]
    active = store.corrections(statuses=[corr.ACTIVE])
    assert sorted(c.type for c in active) == [corr.DURATION_SUSPECT, corr.DURATION_SUSPECT, corr.WORKOUT_MERGE]
    assert len(report.corrections.insert) == 3
