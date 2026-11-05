"""The corrections log: every change applied on top of the raw imported data.

Raw workouts and sets are never edited. Instead, each change is a
``Correction`` with a type, a target (what it applies to), before/after
values, and a plain-English reason. Views of the data (sessions, metrics,
plots) are built by applying the *active* corrections to the raw data.

Automatic corrections are recomputed from the data and settings whenever
either changes (``reconcile``). Choices you make are respected across those
recomputations: a correction you reverted stays reverted.

Statuses:

* ``active``    applied to the data
* ``reverted``  undone by you; not applied, and not re-applied automatically
* ``obsolete``  an automatic correction whose condition no longer holds
                (for example the workout was deleted in Strong)
* ``stale``     a correction you made whose target has since changed in the
                source data; reported and not applied until you decide
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

# Types
WORKOUT_MERGE = "workout_merge"
DURATION_SUSPECT = "duration_suspect"

# Sources
AUTO = "auto"
USER = "user"

# Statuses
ACTIVE = "active"
REVERTED = "reverted"
OBSOLETE = "obsolete"
STALE = "stale"
STATUSES = (ACTIVE, REVERTED, OBSOLETE, STALE)

CorrectionKey = tuple[str, str]


def make_key(type_: str, target: dict) -> CorrectionKey:
    return type_, json.dumps(target, sort_keys=True)


@dataclass
class PlannedCorrection:
    """A correction the automatic rules say should exist."""

    type: str
    target: dict
    before: dict
    after: dict
    reason: str
    source: str = AUTO

    @property
    def key(self) -> CorrectionKey:
        return make_key(self.type, self.target)

    def same_details(self, other: "Correction") -> bool:
        return (self.before, self.after, self.reason) == (other.before, other.after, other.reason)


@dataclass
class Correction:
    id: int
    type: str
    source: str
    target: dict
    before: dict
    after: dict
    reason: str
    status: str
    created_at: datetime
    updated_at: datetime

    @property
    def key(self) -> CorrectionKey:
        return make_key(self.type, self.target)

    @property
    def is_active(self) -> bool:
        return self.status == ACTIVE

    @property
    def workout(self) -> str | None:
        """ISO start time of the workout this correction targets, if any."""
        return self.target.get("workout")


@dataclass
class ReconcileResult:
    """What has to change in the stored log to match the planned corrections."""

    insert: list[PlannedCorrection] = field(default_factory=list)
    update: list[tuple[Correction, PlannedCorrection]] = field(default_factory=list)
    reactivate: list[tuple[Correction, PlannedCorrection]] = field(default_factory=list)
    obsolete: list[Correction] = field(default_factory=list)
    unchanged: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.insert or self.update or self.reactivate or self.obsolete)


def reconcile(existing: list[Correction], planned: list[PlannedCorrection]) -> ReconcileResult:
    """Compare stored automatic corrections with freshly planned ones."""
    result = ReconcileResult()
    by_key = {c.key: c for c in existing if c.source == AUTO}
    planned_keys = set()

    for p in planned:
        planned_keys.add(p.key)
        current = by_key.get(p.key)
        if current is None:
            result.insert.append(p)
        elif current.status == OBSOLETE:
            result.reactivate.append((current, p))
        elif current.status == REVERTED:
            # Your decision stands; keep the description current so the log stays accurate.
            if not p.same_details(current):
                result.update.append((current, p))
            else:
                result.unchanged += 1
        elif not p.same_details(current):
            result.update.append((current, p))
        else:
            result.unchanged += 1

    for key, current in by_key.items():
        if key not in planned_keys and current.status == ACTIVE:
            result.obsolete.append(current)
    return result


def format_duration(td) -> str:
    """Format a timedelta the way Strong does: ``1h 26m``, ``47m``, ``45s``."""
    if td is None:
        return "unknown"
    total = int(td.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds and not hours:
        parts.append(f"{seconds}s")
    return sign + (" ".join(parts) or "0m")
