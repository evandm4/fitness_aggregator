import pytest

from fitness_aggregator import corrections as corr
from fitness_aggregator import metrics
from fitness_aggregator.bodyweight import Bodyweight
from fitness_aggregator.importers import parse_strong_csv
from fitness_aggregator.settings import Settings
from fitness_aggregator.store import Store
from helpers import workout_rows, write_export

OLD = [workout_rows(f"2026-08-{d:02d} 10:00:00", exercises=(("Hip Abductor (Machine)", ((w, 14),)),))
       for d, w in ((4, 145), (8, 150), (12, 160))]
NEW = [workout_rows(f"2026-08-{d:02d} 10:00:00", exercises=(("Hip Abductor 2 (Machine)", ((205, r),)),))
       for d, r in ((22, 12), (25, 14), (28, 14))]
OTHER = [workout_rows("2026-08-30 10:00:00", exercises=(("Crunch (Machine)", ((100, 10),)),
                                                         ("Crunch (Machine 2)", ((40, 10),))))]
A, B = "Hip Abductor (Machine)", "Hip Abductor 2 (Machine)"


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "lifts.db") as s:
        s.apply_import(s.plan_import(parse_strong_csv(p := write_export(tmp_path / "e.csv", *OLD, *NEW, *OTHER)), p),
                       Settings())
        yield s


def frame(store):
    return metrics.sets_frame(store.sessions(), store.catalog(), Bodyweight(180))


def test_candidates_then_merge_hides_group(store):
    assert [g.names for g in store.merge_candidates()] == [
        ["Crunch (Machine)", "Crunch (Machine 2)"], [A, B]]
    c = store.add_exercise_merge(B, A, offset=-50)
    assert c.type == corr.EXERCISE_MERGE and c.source == corr.USER
    assert [g.names for g in store.merge_candidates()] == [["Crunch (Machine)", "Crunch (Machine 2)"]]


def test_merge_applies_to_metrics(store):
    store.add_exercise_merge(B, A, offset=-50)
    df = frame(store)
    assert B not in set(df["exercise"])
    hist = metrics.exercise_history(df, A)
    assert list(hist["top_load"]) == [145, 150, 160, 155, 155, 155]
    assert list(hist["top_pre_merge_weight"].fillna(0)) == [0, 0, 0, 205, 205, 205]
    assert list(hist["merged_from"]) == ["", "", "", B, B, B]
    assert not hist["corrected"].any()  # merges aren't "set fixes"


def test_merging_again_updates_same_correction(store):
    first = store.add_exercise_merge(B, A, offset=-50)
    second = store.add_exercise_merge(B, A, factor=0.75)
    assert first.id == second.id and second.after == {"into": A, "offset": None, "factor": 0.75}
    assert len(store.corrections(type_=corr.EXERCISE_MERGE)) == 1


def test_undo_and_restore(store):
    c = store.add_exercise_merge(B, A)
    store.undo_exercise_merge(B, Settings())
    assert B in set(frame(store)["exercise"])
    with pytest.raises(KeyError):
        store.undo_exercise_merge(B, Settings())
    store.set_correction_status(c.id, corr.ACTIVE, Settings())
    assert B not in set(frame(store)["exercise"])


def test_restore_refused_if_it_would_chain(store):
    c = store.add_exercise_merge(B, A)
    store.undo_exercise_merge(B, Settings())
    store.add_exercise_merge(A, "Crunch (Machine)")  # nonsense, but allowed
    with pytest.raises(ValueError, match="already combined into"):
        store.set_correction_status(c.id, corr.ACTIVE, Settings())


def test_merge_goes_stale_when_exercise_disappears(store, tmp_path):
    c = store.add_exercise_merge(B, A, offset=-50)
    p = write_export(tmp_path / "2.csv", *OLD, *OTHER,
                     workout_rows("2026-09-01 10:00:00", exercises=((A, ((150, 14),)),)))
    report = store.apply_import(store.plan_import(parse_strong_csv(p), p), Settings(), allow_removals=True)
    assert [x.id for x in report.corrections.stale] == [c.id]


def test_keep_separate_and_forget(store):
    assert store.keep_separate(["Crunch (Machine)", "Crunch (Machine 2)"]) == 1
    assert [g.names for g in store.merge_candidates()] == [[A, B]]
    assert store.forget_separate("Crunch (Machine 2)", "Crunch (Machine)")
    assert not store.forget_separate("Crunch (Machine 2)", "Crunch (Machine)")
    assert len(store.merge_candidates()) == 2


def test_merging_clears_a_previous_keep_separate(store):
    store.keep_separate([A, B])
    store.add_exercise_merge(B, A)
    assert store.separate_pairs() == set()


def test_top_sets_and_merged_sources(store):
    assert [t.weight for t in store.top_sets(B)] == [205, 205, 205]
    store.add_exercise_merge(B, A)
    assert store.merged_sources(A) == [B]
    assert [t.weight for t in store.top_sets(B)] == [205, 205, 205]  # as recorded, not merged
