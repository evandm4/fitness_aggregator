from datetime import datetime

from fitness_aggregator import corrections as corr
from fitness_aggregator.corrections import Correction, PlannedCorrection, format_duration, reconcile

NOW = datetime(2026, 11, 5)


def planned(workout="2026-06-02T14:06:00", reason="r"):
    return PlannedCorrection(type=corr.WORKOUT_MERGE, target={"workout": workout},
                             before={}, after={"into": "x"}, reason=reason)


def stored(p, status=corr.ACTIVE, id_=1, source=corr.AUTO):
    return Correction(id=id_, type=p.type, source=source, target=p.target, before=p.before,
                      after=p.after, reason=p.reason, status=status, created_at=NOW, updated_at=NOW)


def test_new_correction_is_inserted():
    r = reconcile([], [planned()])
    assert len(r.insert) == 1 and r.changed


def test_identical_correction_is_unchanged():
    p = planned()
    r = reconcile([stored(p)], [p])
    assert r.unchanged == 1 and not r.changed


def test_changed_details_are_updated():
    r = reconcile([stored(planned(reason="old"))], [planned(reason="new")])
    assert [(c.id, p.reason) for c, p in r.update] == [(1, "new")]


def test_active_correction_no_longer_planned_becomes_obsolete():
    r = reconcile([stored(planned())], [])
    assert [c.id for c in r.obsolete] == [1]


def test_reverted_correction_stays_reverted_when_still_planned_or_not():
    p = planned()
    assert not reconcile([stored(p, corr.REVERTED)], [p]).changed
    assert not reconcile([stored(p, corr.REVERTED)], []).changed


def test_obsolete_correction_is_reactivated():
    p = planned()
    r = reconcile([stored(p, corr.OBSOLETE)], [p])
    assert [c.id for c, _ in r.reactivate] == [1]


def test_user_corrections_are_ignored_by_auto_reconcile():
    p = planned()
    r = reconcile([stored(p, source=corr.USER)], [])
    assert not r.changed


def test_format_duration():
    from datetime import timedelta as td
    assert format_duration(td(hours=1, minutes=26)) == "1h 26m"
    assert format_duration(td(minutes=47)) == "47m"
    assert format_duration(td(hours=2)) == "2h"
    assert format_duration(td(seconds=45)) == "45s"
    assert format_duration(td()) == "0m"
    assert format_duration(None) == "unknown"
