"""Bodyweight lookups. Used only for assisted lifts' effective load.

Every lookup goes through ``Bodyweight.at(date)``. Today that always returns
the placeholder from settings. When dated entries exist (a future Apple Health
import), it returns the most recent entry on or before the date, or the first
entry for earlier dates.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Bodyweight:
    placeholder_lb: float
    entries: list[tuple[datetime, float]] = field(default_factory=list)  # sorted by date

    @property
    def uses_placeholder(self) -> bool:
        return not self.entries

    def at(self, when: datetime) -> float:
        if not self.entries:
            return self.placeholder_lb
        dates = [d for d, _ in self.entries]
        i = bisect.bisect_right(dates, when) - 1
        return self.entries[max(i, 0)][1]
