from datetime import datetime

import pytest

from fitness_aggregator import corrections as corr
from fitness_aggregator.corrections import Correction
from fitness_aggregator.fixes import apply_value_fixes, covered_positions, plan_value_fix, raw_matches
from fitness_aggregator.importers import parse_strong_text
from helpers import export_text, workout_rows

NOW = datetime(2026, 11, 5)
PREACHER = workout_rows("2026-06-04 13:53:53", exercises=(
    ("Preacher Curl (Machine)", ((8, 80), (8, 60), (6, 50))),
))


def workout():
    return parse_strong_text(export_text(PREACHER)).workouts[0]


def stored(p, status=corr.ACTIVE, id_=7):
    return Correction(id=id_, type=p.type, source=p.source, target=p.target, before=p.before,
                      after=p.after, reason=p.reason, status=status, created_at=NOW, updated_at=NOW)


def swap_all(w):
    return {s.position: {"weight": s.reps, "reps": s.weight} for s in w.exercises[0].sets}


def test_swap_fix_plan_and_reason():
    w = workout()
    p = plan_value_fix(w, "Preacher Curl (Machine)", 1, swap_all(w))
    assert p.type == corr.VALUE_FIX and p.source == corr.USER
    assert p.target == {"workout": "2026-06-04T13:53:53", "exercise": "Preacher Curl (Machine)", "occurrence": 1}
    assert p.before["sets"]["1"] == {"weight": 8.0, "reps": 80.0}
    assert p.after["sets"]["1"] == {"weight": 80.0, "reps": 8.0}
    assert p.reason.startswith("Swapped weight and reps on 3 Preacher Curl (Machine) sets, 2026-06-04 13:53")


def test_edit_reason_and_unchanged_fields_dropped():
    w = workout()
    p = plan_value_fix(w, "Preacher Curl (Machine)", 1, {2: {"weight": 8, "reps": 6}})
    assert p.after == {"sets": {"2": {"reps": 6.0}}}
    assert p.reason == "Edited Preacher Curl (Machine), 2026-06-04 13:53: set 2: 8 lb × 60 → 8 lb × 6"


def test_extends_active_fix_but_not_reverted_one():
    w = workout()
    first = stored(plan_value_fix(w, "Preacher Curl (Machine)", 1, {1: {"reps": 8}}))
    p = plan_value_fix(w, "Preacher Curl (Machine)", 1, {2: {"reps": 6}}, first)
    assert set(p.after["sets"]) == {"1", "2"}
    reverted = stored(plan_value_fix(w, "Preacher Curl (Machine)", 1, {1: {"reps": 8}}), corr.REVERTED)
    p = plan_value_fix(w, "Preacher Curl (Machine)", 1, {2: {"reps": 6}}, reverted)
    assert set(p.after["sets"]) == {"2"}


@pytest.mark.parametrize("changes, message", [
    ({1: {"weight": 8, "reps": 80}}, "nothing to change"),
    ({9: {"reps": 1}}, "has no set 9"),
    ({1: {"rpe": 9}}, "can't change 'rpe'"),
])
def test_invalid_fixes(changes, message):
    with pytest.raises(ValueError, match=message):
        plan_value_fix(workout(), "Preacher Curl (Machine)", 1, changes)


def test_unknown_entry():
    with pytest.raises(ValueError, match="is not in"):
        plan_value_fix(workout(), "Preacher Curl (Machine)", 2, {1: {"reps": 1}})


def test_apply_value_fixes_copies_and_marks_sets():
    w = workout()
    c = stored(plan_value_fix(w, "Preacher Curl (Machine)", 1, swap_all(w)))
    [fixed] = apply_value_fixes([w], [c])
    s = fixed.exercises[0].sets[0]
    assert (s.weight, s.reps) == (80.0, 8.0)
    assert s.corrected_from == {"weight": 8.0, "reps": 80.0} and s.correction_ids == (7,)
    assert w.exercises[0].sets[0].weight == 8.0  # raw untouched
    assert fixed is not w


def test_inactive_fixes_not_applied_and_untouched_workouts_shared():
    w = workout()
    c = stored(plan_value_fix(w, "Preacher Curl (Machine)", 1, swap_all(w)), corr.REVERTED)
    [same] = apply_value_fixes([w], [c])
    assert same is w


def test_raw_matches_and_covered_positions():
    w = workout()
    c = stored(plan_value_fix(w, "Preacher Curl (Machine)", 1, {1: {"reps": 8}}))
    key = "2026-06-04T13:53:53"
    assert raw_matches({key: w}, c)
    assert not raw_matches({}, c)
    w.exercises[0].sets[0].reps = 8.0  # fixed in Strong, so the correction is stale
    assert not raw_matches({key: w}, c)
    assert covered_positions([c]) == {(key, "Preacher Curl (Machine)", 1, 1)}
