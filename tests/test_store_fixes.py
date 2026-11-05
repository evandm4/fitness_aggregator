
import pytest

from fitness_aggregator import corrections as corr
from fitness_aggregator.importers import parse_strong_csv
from fitness_aggregator.settings import Settings
from fitness_aggregator.store import SCHEMA_VERSION, Store
from helpers import workout_rows, write_export

HISTORY = [workout_rows(f"2026-06-0{d} 10:00:00", exercises=(("Preacher Curl (Machine)", ((100, 10),)),))
           for d in (1, 2, 3)]
BAD = workout_rows("2026-06-04 13:53:53", exercises=(("Preacher Curl (Machine)", ((8, 80), (8, 60))),))
FIXED_IN_STRONG = workout_rows("2026-06-04 13:53:53", exercises=(("Preacher Curl (Machine)", ((80, 8), (8, 60))),))


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "lifts.db") as s:
        yield s


def do_import(store, path, **kw):
    return store.apply_import(store.plan_import(parse_strong_csv(path), path), Settings(), **kw)


def preacher_sets(store):
    s = next(s for s in store.sessions() if s.started_at.day == 4)
    return [(x.weight, x.reps) for x in s.exercises[0].sets]


def swap(store, positions=(1, 2)):
    w = store.find_workouts("2026-06-04")[0]
    sets = {s.position: s for s in w.exercises[0].sets}
    return store.add_value_fix(w, "Preacher Curl (Machine)", 1,
                               {p: {"weight": sets[p].reps, "reps": sets[p].weight} for p in positions})


def test_fix_applies_to_sessions_but_not_raw(store, tmp_path):
    do_import(store, write_export(tmp_path / "e.csv", *HISTORY, BAD))
    assert len(store.open_flags()) == 2
    c = swap(store)
    assert c.source == corr.USER and c.status == corr.ACTIVE
    assert preacher_sets(store) == [(80, 8), (60, 8)]
    raw = next(s for s in store.sessions(raw=True) if s.started_at.day == 4)
    assert [(x.weight, x.reps) for x in raw.exercises[0].sets] == [(8, 80), (8, 60)]
    assert store.open_flags() == []
    s = next(s for s in store.sessions() if s.started_at.day == 4)
    assert [a.id for a in s.applied] == [c.id]


def test_fixing_same_entry_again_extends_one_correction(store, tmp_path):
    do_import(store, write_export(tmp_path / "e.csv", *HISTORY, BAD))
    first = swap(store, positions=(1,))
    assert len(store.open_flags()) == 1
    second = swap(store, positions=(2,))
    assert first.id == second.id and len(store.corrections(type_=corr.VALUE_FIX)) == 1
    assert set(second.after["sets"]) == {"1", "2"}


def test_revert_fix_restores_raw_values_and_flags(store, tmp_path):
    do_import(store, write_export(tmp_path / "e.csv", *HISTORY, BAD))
    c = swap(store)
    store.set_correction_status(c.id, corr.REVERTED, Settings())
    assert preacher_sets(store) == [(8, 80), (8, 60)]
    assert len(store.open_flags()) == 2


def test_fix_goes_stale_when_changed_in_strong_and_back(store, tmp_path):
    do_import(store, write_export(tmp_path / "1.csv", *HISTORY, BAD))
    c = swap(store)
    report = do_import(store, write_export(tmp_path / "2.csv", *HISTORY, FIXED_IN_STRONG))
    assert [x.id for x in report.corrections.stale] == [c.id]
    assert store.get_correction(c.id).status == corr.STALE
    assert preacher_sets(store) == [(80, 8), (8, 60)]  # stale fix not applied; Strong's data used
    with pytest.raises(ValueError, match="no longer matches"):
        store.set_correction_status(c.id, corr.ACTIVE, Settings())
    report = do_import(store, write_export(tmp_path / "3.csv", *HISTORY, BAD))
    assert [x.id for x in report.corrections.unstale] == [c.id]
    assert preacher_sets(store) == [(80, 8), (60, 8)]


def test_dismissed_flags_stay_dismissed_across_imports(store, tmp_path):
    do_import(store, write_export(tmp_path / "1.csv", *HISTORY, BAD))
    store.dismiss_flags(f.key for f in store.open_flags())
    assert store.open_flags() == []
    do_import(store, write_export(tmp_path / "2.csv", *HISTORY, BAD,
                                  workout_rows("2026-06-09 10:00:00")))
    assert store.open_flags() == []


def test_find_workouts(store, tmp_path):
    do_import(store, write_export(tmp_path / "e.csv", *HISTORY, BAD))
    assert len(store.find_workouts("2026-06")) == 4
    assert len(store.find_workouts("2026-06-04 13:53")) == 1


def test_schema_1_database_is_upgraded(tmp_path):
    path = tmp_path / "old.db"
    with Store(path) as s:
        s.conn.execute("DROP TABLE dismissed_flags")
        s.conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        s.conn.commit()
    with Store(path) as s:
        assert s.dismissed_flag_keys() == set()
        assert s.separate_pairs() == set()
        assert s.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(SCHEMA_VERSION)


def test_undismiss_flags(store, tmp_path):
    do_import(store, write_export(tmp_path / "e.csv", *HISTORY, BAD))
    keys = [f.key for f in store.open_flags()]
    store.dismiss_flags(keys)
    assert [f.key for f in store.dismissed_flags()] == keys and store.open_flags() == []
    assert store.undismiss_flags(keys[:1]) == 1
    assert [f.key for f in store.open_flags()] == keys[:1]
    assert store.undismiss_flags() == 1
    assert len(store.open_flags()) == 2 and store.dismissed_flags() == []
