"""Bodyweight lookups. Used only for assisted lifts' effective load.

Every lookup goes through ``Bodyweight.at(date)``. With no dated entries it
returns the placeholder from settings. When entries exist (from an Apple
Health import, ``fitness import-health``), it returns the most recent entry on
or before the date, or the first entry for earlier dates.

This module also plans Health imports: each import holds the full history and
replaces the stored entries, so ``plan_changes`` works out which days are
new, changed, or gone, for the import summary.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta


@dataclass
class Bodyweight:
    placeholder_lb: float
    entries: list[tuple[datetime, float]] = field(default_factory=list)  # sorted by date

    def __post_init__(self) -> None:
        self.entries = sorted(self.entries)
        self._dates = [d for d, _ in self.entries]

    @property
    def uses_placeholder(self) -> bool:
        return not self.entries

    def at(self, when: datetime) -> float:
        if not self.entries:
            return self.placeholder_lb
        i = bisect.bisect_right(self._dates, when) - 1
        return self.entries[max(i, 0)][1]

    def change_over(self, days: int) -> float | None:
        """Latest entry minus the bodyweight ``days`` before it (None without enough history)."""
        if len(self.entries) < 2:
            return None
        latest_date, latest = self.entries[-1]
        then = latest_date - timedelta(days=days)
        if then < self.entries[0][0]:
            return None
        return latest - self.at(then)


# --- planning a Health import -------------------------------------------------

@dataclass(frozen=True)
class DayValue:
    day: date
    lb: float
    source: str = ""


@dataclass
class BodyweightPlan:
    incoming: list[DayValue]
    source_path: str
    sha256: str
    new: list[DayValue] = field(default_factory=list)
    changed: list[tuple[DayValue, DayValue]] = field(default_factory=list)  # (stored, incoming)
    removed: list[DayValue] = field(default_factory=list)
    unchanged: int = 0

    @property
    def is_first_import(self) -> bool:
        return not (self.changed or self.removed or self.unchanged)

    @property
    def has_changes(self) -> bool:
        return bool(self.new or self.changed or self.removed)


def plan_changes(stored: list[DayValue], incoming: list[DayValue], source_path: str = "",
                 sha256: str = "") -> BodyweightPlan:
    old = {d.day: d for d in stored}
    new = {d.day: d for d in incoming}
    plan = BodyweightPlan(sorted(new.values(), key=lambda d: d.day), source_path, sha256)
    for day, value in sorted(new.items()):
        before = old.get(day)
        if before is None:
            plan.new.append(value)
        elif abs(before.lb - value.lb) >= 0.005:
            plan.changed.append((before, value))
        else:
            plan.unchanged += 1
    plan.removed = [v for day, v in sorted(old.items()) if day not in new]
    return plan
