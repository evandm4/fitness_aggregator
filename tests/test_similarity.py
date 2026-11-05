from datetime import datetime, timedelta

import pytest

from fitness_aggregator.catalog import ASSISTED, TIMED
from fitness_aggregator.importers import parse_strong_csv
from fitness_aggregator.similarity import (
    LIKELY, POSSIBLE, TopSet, describe_sets, find_candidates, suggest_offset, tokens, top_sets,
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


def test_suggest_offset_with_overlap_uses_closest_sessions():
    kept = [ts(0, 100, 10), ts(10, 100, 10), ts(40, 200, 10)]
    other = [ts(11, 80, 10), ts(30, 80, 10)]
    s = suggest_offset("A", kept, "B", other)
    assert "You used both between 2026-06-12 and 2026-07-01" in s.relation
    assert [(k.weight, a.weight) for k, a in zip(s.kept_sets, s.adjusted_sets)] == [(100, 80), (200, 80)]


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
