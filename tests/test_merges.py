from datetime import datetime

import pytest

from fitness_aggregator import corrections as corr
from fitness_aggregator.corrections import Correction
from fitness_aggregator.importers import parse_strong_text
from fitness_aggregator.merges import Merge, active_merges, apply_exercise_merges, check_merge, plan_merge
from helpers import export_text, workout_rows

NOW = datetime(2026, 11, 5)


def stored(p, id_=5, status=corr.ACTIVE):
    return Correction(id=id_, type=p.type, source=p.source, target=p.target, before=p.before, after=p.after,
                      reason=p.reason, status=status, created_at=NOW, updated_at=NOW)


@pytest.mark.parametrize("offset, factor, reason", [
    (None, None, "B combined into A as-is"),
    (0, None, "B combined into A as-is"),
    (-50, None, "B combined into A with -50 lb offset"),
    (None, 0.75, "B combined into A with ×0.75 factor"),
])
def test_plan_merge_reason(offset, factor, reason):
    assert plan_merge("B", "A", offset, factor).reason == reason


def test_adjust():
    assert Merge("B", "A", -50, None).adjust(205) == 155
    assert Merge("B", "A", -50, None).adjust(30) == 0       # never negative
    assert Merge("B", "A", None, 0.75).adjust(200) == 150
    assert Merge("B", "A", -50, None).adjust(0) == 0        # unloaded sets untouched


@pytest.mark.parametrize("source, into, existing, message", [
    ("B", "B", {}, "into itself"),
    ("B", "Z", {}, "no exercise named 'Z'"),
    ("C", "B", {"B": Merge("B", "A", None, None)}, "B is already combined into A"),
    ("A", "C", {"B": Merge("B", "A", None, None)}, "B is combined into A; undo that first"),
])
def test_check_merge_errors(source, into, existing, message):
    with pytest.raises(ValueError, match=message):
        check_merge(source, into, existing, {"A", "B", "C"})


def test_check_merge_offset_and_factor_rules():
    with pytest.raises(ValueError, match="not both"):
        check_merge("B", "A", {}, {"A", "B"}, offset=1, factor=2)
    with pytest.raises(ValueError, match="greater than 0"):
        check_merge("B", "A", {}, {"A", "B"}, factor=0)


def test_apply_renames_and_adjusts_keeping_originals():
    ws = parse_strong_text(export_text(
        workout_rows("2026-08-22 10:00:00", exercises=(("Hip Abductor 2 (Machine)", ((205, 12), (0, 10))),)),
        workout_rows("2026-08-12 10:00:00", exercises=(("Hip Abductor (Machine)", ((160, 14),)),)),
    )).workouts
    c = stored(plan_merge("Hip Abductor 2 (Machine)", "Hip Abductor (Machine)", -50))
    out = apply_exercise_merges(ws, [c])
    assert out[0] is ws[0]  # untouched workout is shared
    entry = out[1].exercises[0]
    assert entry.name == "Hip Abductor (Machine)" and entry.merged_from == "Hip Abductor 2 (Machine)"
    assert entry.merge_label == "-50 lb" and entry.merge_correction_id == 5
    first, zero = entry.sets
    assert (first.weight, first.pre_merge_weight) == (155, 205)
    assert (first.corrected_from, first.correction_ids) == ({"weight": 205}, (5,))
    assert (zero.weight, zero.correction_ids) == (0, ())
    assert ws[1].exercises[0].name == "Hip Abductor 2 (Machine)"  # raw untouched


def test_inactive_merges_ignored():
    c = stored(plan_merge("B", "A"), status=corr.REVERTED)
    assert active_merges([c]) == {}


def test_adjust_offset_has_no_float_noise():
    # 1.1 + 2.2 is 3.3000000000000003 in floating point; it showed up in exports and broke equality.
    assert Merge("a", "b", 2.2, None).adjust(1.1) == 3.3
    assert Merge("a", "b", -0.1, None).adjust(0.3) == 0.2
