from datetime import datetime, timedelta

import pytest

from fitness_aggregator.catalog import ASSISTED, TIMED
from fitness_aggregator.importers import parse_strong_csv
from fitness_aggregator.similarity import (
    LIKELY,
    POSSIBLE,
    TopSet,
    describe_sets,
    find_candidates,
    suggest_offset,
    tokens,
    top_sets,
)
from helpers import REAL_EXPORT


def ts(day, weight, reps):
    return TopSet(datetime(2026, 6, 1) + timedelta(days=day), weight, reps)


@pytest.mark.parametrize("a, b", [
    ("Hip Abductor 2 (Machine)", "Hip Abductor (Machine)"),
    ("Crunch (Machine 2)", "Crunch (Machine)"),
    ("Tricep Pushdown (Cable)", "Triceps Pushdown (Cable)"),
    ("Curl (Cable) ", "cable curl"),
])
def test_tokens_match(a, b):
    assert tokens(a) == tokens(b)


def test_tokens_differ_for_different_movements():
    assert tokens("Hip Abductor (Machine)") != tokens("Hip Adductor (Machine)")


def test_find_candidates_tiers_and_contrasts():
    names = ["Hip Abductor (Machine)", "Hip Abductor 2 (Machine)", "Hip Adductor (Machine)",
             "Incline Bench Press (Barbell)", "Decline Bench Press (Barbell)",
             "Real Tricep Extension (Machine)", "Triceps Extension (Machine)", "Squat (Barbell)"]
    groups = find_candidates(names)
    assert [(g.tier, g.names) for g in groups] == [
        (LIKELY, ["Hip Abductor (Machine)", "Hip Abductor 2 (Machine)"]),
        (POSSIBLE, ["Real Tricep Extension (Machine)", "Triceps Extension (Machine)"]),
    ]


def test_separate_pairs_hide_groups():
    names = ["Crunch (Machine)", "Crunch (Machine 2)"]
    assert find_candidates(names, {frozenset(names)}) == []


def test_three_name_group_stays_until_every_pair_decided():
    names = ["Row (Machine)", "Row (Machine 2)", "Row (Machine 3)"]
    [g] = find_candidates(names, {frozenset(names[:2])})
    assert len(g.names) == 3 and len(g.pairs) == 3


def test_suggest_offset_after_a_switch():
    kept = [ts(0, 140, 14), ts(5, 145, 14), ts(10, 150, 14), ts(15, 160, 14)]
    other = [ts(20, 205, 12), ts(25, 205, 14), ts(30, 205, 14), ts(35, 210, 14)]
    s = suggest_offset("A", kept, "B", other)
    assert s.relation == "You switched from A to B on 2026-06-21 (no overlap)."
    assert [t.weight for t in s.kept_sets] == [145, 150, 160]   # last 3 before the switch
    assert [t.weight for t in s.adjusted_sets] == [205, 205, 205]  # first 3 after
    assert s.offset == -50 and s.factor == 0.75
    assert "about 50 lb heavier" in s.summary and "Suggested: -50 lb on B (or ×0.75)" in s.summary


def test_suggest_offset_when_switching_back_is_positive():
    s = suggest_offset("A", [ts(30, 100, 10), ts(35, 100, 10)], "B", [ts(0, 60, 10), ts(5, 60, 10)])
    assert s.relation.startswith("You switched from B to A")
    assert s.offset == 40 and "lighter" in s.summary


def test_overlap_interpolates_between_the_kept_sessions_either_side():
    kept = [ts(0, 100, 10), ts(10, 100, 10), ts(40, 200, 10)]
    other = [ts(11, 80, 10), ts(30, 80, 10)]
    s = suggest_offset("A", kept, "B", other)
    assert s.relation.startswith("You used B twice (2026-06-12, 2026-07-01), during the period you were using A "
                                 "(2026-06-01 → 2026-07-11).")
    assert "just before and after it" in s.relation
    # Each B session sits between A's 2026-06-11 (100×10) and 2026-07-11 (200×10) sessions.
    assert [[k.weight for k in c.kept] for c in s.comparisons] == [[100, 200], [100, 200]]
    # A's interpolated weight at 10 reps: 103.3 lb on 06-12 and 166.7 lb on 07-01 → mean diff vs 80 lb = 55.
    assert s.offset == 55
    assert s.compared[0] == ("B 2026-06-12 80 lb × 10 vs A 2026-06-11 100 lb × 10 and 2026-07-11 200 lb × 10 "
                             "(≈ 103 lb × 10 on 2026-06-12)")


def test_overlap_prefers_a_same_day_session_then_the_nearest():
    kept = [ts(0, 100, 10), ts(5, 110, 10)]
    other = [ts(5, 90, 10), ts(9, 95, 10)]
    s = suggest_offset("A", kept, "B", other)
    assert [[k.weight for k in c.kept] for c in s.comparisons] == [[110], [110]]
    assert "Comparing with the closest A sessions." in s.relation


def test_real_leg_extension_one_off_machine_is_compared_with_both_neighbours():
    if not REAL_EXPORT.exists():
        pytest.skip("real export not present")
    ws = parse_strong_csv(REAL_EXPORT).workouts
    a, b = "Leg Extension (Machine)", "Leg Extension (Machine 2)"
    s = suggest_offset(a, top_sets(ws, a), b, top_sets(ws, b))
    assert s.relation.startswith(f"You used {b} once (2026-08-22), during the period you were using {a}")
    assert s.compared == [f"{b} 2026-08-22 240 lb × 10 vs {a} 2026-08-12 170 lb × 12 and 2026-08-28 240 lb × 8 "
                          "(≈ 209 lb × 10 on 2026-08-22)"]
    assert (s.offset, s.factor) == (-30, 0.87)


def test_switch_comparison_lines_are_dated():
    kept = [ts(0, 140, 14), ts(5, 145, 14)]
    other = [ts(20, 205, 12)]
    assert suggest_offset("A", kept, "B", other).compared == [
        "A: 2026-06-01 140 lb × 14, 2026-06-06 145 lb × 14", "B: 2026-06-21 205 lb × 12"]


def test_assisted_and_timed_suggestions():
    s = suggest_offset("A", [ts(0, 60, 8)], "B", [ts(5, 40, 8)], ASSISTED)
    assert s.offset == 20 and s.factor is None
    s = suggest_offset("A", [ts(0, 0, 0)], "B", [ts(5, 0, 0)], TIMED)
    assert s.offset is None and "No offset suggestion" in s.summary
    assert suggest_offset("A", [], "B", [ts(0, 1, 1)]).offset is None


def test_describe_sets():
    assert describe_sets([ts(0, 145, 14), ts(1, 160, 14)]) == "145–160 lb × 14"
    assert describe_sets([ts(0, 205, 12)]) == "205 lb × 12"


@pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not present")
def test_real_export_candidates_and_hip_abductor_suggestion():
    ws = parse_strong_csv(REAL_EXPORT).workouts
    groups = find_candidates({e.name for w in ws for e in w.exercises})
    likely = [g.names for g in groups if g.tier == LIKELY]
    assert likely == [
        ["Crunch (Machine)", "Crunch (Machine 2)"],
        ["Hip Abductor (Machine)", "Hip Abductor 2 (Machine)"],
        ["Hip Adductor (Machine)", "Hip Adductor 2 (Machine)"],
        ["Lat Pulldown (Machine)", "Lat Pulldown (Machine 2)"],
        ["Leg Extension (Machine)", "Leg Extension (Machine 2)"],
    ]
    assert not any("Incline" in " ".join(g.names) and "Decline" in " ".join(g.names) for g in groups)
    a, b = "Hip Abductor (Machine)", "Hip Abductor 2 (Machine)"
    s = suggest_offset(a, top_sets(ws, a), b, top_sets(ws, b))
    assert (s.offset, s.factor) == (-50, 0.75)


# --- edge cases (hardening) ------------------------------------------------------

def _t(day, weight, reps, seconds=0.0):
    return TopSet(datetime(2026, 6, day), weight, reps, seconds)


def test_suggest_offset_kept_side_without_weight_gives_no_suggestion():
    # Bodyweight sets kept, weighted sets adjusted: an offset would zero every weight (and factor 0 is invalid).
    s = suggest_offset("Dip", [_t(1, 0, 12), _t(3, 0, 12)], "Dip (Weighted)", [_t(10, 25, 8)])
    assert s.offset is None and s.factor is None
    assert s.summary.startswith("No offset suggestion")


def test_suggest_offset_adjusted_side_without_weight_gives_no_suggestion():
    s = suggest_offset("Dip (Weighted)", [_t(1, 25, 8)], "Dip", [_t(10, 0, 12)])
    assert s.offset is None and s.factor is None


def test_suggest_offset_with_a_single_session_each():
    s = suggest_offset("A", [_t(1, 100, 5)], "B", [_t(1, 80, 5)])
    assert (s.offset, s.factor) == (20, 1.25)
    assert "on 2026-06-01" in s.relation


def test_three_name_group_with_one_separate_pair_still_offers_the_rest():
    names = ["Row (Machine)", "Row (Machine 2)", "Row (Machine 3)"]
    [g] = find_candidates(names, {frozenset(names[:2])})
    undecided = [p for p in g.pairs if p != frozenset(names[:2])]
    assert len(undecided) == 2
    # Once every pair is decided, the group disappears.
    assert find_candidates(names, set(g.pairs)) == []


@pytest.mark.parametrize("x, expected", [(12.5, 15), (7.5, 10), (-12.5, -15), (-51, -50), (2.4, 0), (-2.4, 0), (0, 0)])
def test_round_to_rounds_halves_away_from_zero(x, expected):
    from fitness_aggregator.similarity import round_to
    assert round_to(x) == expected


def test_same_base_name_without_equipment_is_a_possible_match():
    groups = find_candidates(["Leg Press", "Leg Press 2 (Machine)", "Squat (Barbell)", "Squat"])
    assert [(g.tier, g.names) for g in groups] == [
        (POSSIBLE, ["Leg Press", "Leg Press 2 (Machine)"]),
        (POSSIBLE, ["Squat", "Squat (Barbell)"]),
    ]


def test_both_labelled_with_different_equipment_is_not_a_match():
    assert find_candidates(["Bicep Curl (Cable)", "Bicep Curl (Machine)"]) == []
