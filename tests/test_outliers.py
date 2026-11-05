import pytest

from fitness_aggregator.catalog import Catalog
from fitness_aggregator.importers import parse_strong_csv, parse_strong_text
from fitness_aggregator.outliers import detect, group
from helpers import REAL_EXPORT, export_text, workout_rows


def history(name, weights, start_day=1):
    return [workout_rows(f"2026-06-{start_day + i:02d} 10:00:00", exercises=((name, ((w, 10),)),))
            for i, w in enumerate(weights)]


def flags_for(*rows):
    ws = parse_strong_text(export_text(*rows)).workouts
    return detect(ws, Catalog.build(ws))


def test_high_reps_with_plausible_swap():
    rows = history("Curl", [90, 100, 100]) + [
        workout_rows("2026-06-10 10:00:00", exercises=(("Curl", ((8, 80), (8, 60))),))]
    flags = flags_for(*rows)
    assert [f.rule for f in flags] == ["high_reps", "high_reps"]
    assert all(f.swap_plausible for f in flags)
    [g] = group(flags)
    assert g.swap_plausible and len(g.flags) == 2 and g.occurrence == 1


def test_high_reps_swap_not_plausible_when_far_from_usual():
    rows = history("Curl", [20, 20, 20]) + [
        workout_rows("2026-06-10 10:00:00", exercises=(("Curl", ((8, 200),)),))]
    [f] = flags_for(*rows)
    assert f.rule == "high_reps" and not f.swap_plausible


def test_weight_spike_needs_history_and_only_flags_high_side():
    rows = history("Squat", [100, 110, 120]) + [
        workout_rows("2026-06-10 10:00:00", exercises=(("Squat", ((1100, 5), (20, 10))),))]
    flags = flags_for(*rows)
    assert [(f.rule, f.set.weight) for f in flags] == [("weight_spike", 1100)]
    assert not flags_for(*(history("Squat", [100, 110]) + [
        workout_rows("2026-06-10 10:00:00", exercises=(("Squat", ((1100, 5),)),))]))


def test_zero_reps_flagged_except_failure_sets():
    rows = ('2026-06-02 10:00:00,"W",1h,"Squat",1,225,0.0,0,0.0,"","",\n'
            '2026-06-02 10:00:00,"W",1h,"Squat",F,295,0.0,0,0.0,"","",\n')
    flags = flags_for(rows)
    assert [(f.rule, f.set.weight) for f in flags] == [("zero_reps", 225)]


def test_warmups_and_timed_exercises_ignored():
    rows = ('2026-06-02 10:00:00,"W",1h,"Curl",W,8,80.0,0,0.0,"","",\n'
            '2026-06-02 10:00:00,"W",1h,"Plank",1,0,0.0,0,60.0,"","",\n')
    assert flags_for(rows) == []


def test_flag_keys_are_stable():
    rows = [workout_rows("2026-06-10 10:00:00", exercises=(("Curl", ((8, 80),)),))]
    assert flags_for(*rows)[0].key == "high_reps|2026-06-10T10:00:00|Curl|1|1"


@pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not present")
def test_real_export_flags_only_the_swapped_preacher_curl():
    ws = parse_strong_csv(REAL_EXPORT).workouts
    groups = group(detect(ws, Catalog.build(ws)))
    assert len(groups) == 1
    g = groups[0]
    assert g.entry.name == "Preacher Curl (Machine)" and g.swap_plausible
    assert [(f.set.weight, f.set.reps) for f in g.flags] == [(8, 80), (8, 60), (6, 50)]
