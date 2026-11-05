"""Bodyweight from Apple Health's "Export All Health Data" file.

The Health app exports ``export.zip`` containing ``apple_health_export/export.xml``
(often hundreds of MB). Body-mass readings look like::

    <Record type="HKQuantityTypeIdentifierBodyMass" sourceName="Health" unit="lb"
            startDate="2026-06-01 07:12:00 -0700" value="181.4" .../>

The file is streamed with ``iterparse`` and each element is discarded once
read, so memory stays flat. Readings are converted to lb and averaged per
calendar day (the local date the reading was taken).
"""

from __future__ import annotations

import os
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import IO
from xml.etree.ElementTree import ParseError, iterparse

BODY_MASS = "HKQuantityTypeIdentifierBodyMass"
LB_PER_UNIT = {"lb": 1.0, "kg": 2.20462262185, "g": 0.00220462262185, "st": 14.0}
PLAUSIBLE_LB = (20.0, 1000.0)  # anything outside is a typo, not a bodyweight
DATE_FORMAT = "%Y-%m-%d %H:%M:%S %z"


class HealthParseError(Exception):
    pass


@dataclass(frozen=True)
class DailyWeight:
    day: date
    lb: float  # mean of the day's readings, rounded to 0.01
    readings: int
    source: str  # source names, comma separated


@dataclass
class HealthStats:
    records: int = 0  # body-mass records read
    other_records: int = 0  # records of other types (ignored)
    invalid: int = 0  # body-mass records that couldn't be used
    units: Counter = field(default_factory=Counter)
    first: date | None = None
    last: date | None = None


@dataclass
class HealthResult:
    days: list[DailyWeight]  # sorted by day
    stats: HealthStats
    warnings: list[str] = field(default_factory=list)  # one per kind of invalid record, with an example

    @property
    def by_day(self) -> dict[date, DailyWeight]:
        return {d.day: d for d in self.days}


def parse_apple_health(path: str | os.PathLike) -> HealthResult:
    """Parse ``export.zip`` or a bare ``export.xml``."""
    path = os.fspath(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            member = _find_export_xml(zf)
            with zf.open(member) as fh:
                return parse_health_xml(fh)
    with open(path, "rb") as fh:
        return parse_health_xml(fh)


def _find_export_xml(zf: zipfile.ZipFile) -> str:
    names = [n for n in zf.namelist() if n.rsplit("/", 1)[-1] == "export.xml"]
    if not names:
        raise HealthParseError("no export.xml in this zip (expected apple_health_export/export.xml)")
    return min(names, key=lambda n: (n.count("/"), n))


def parse_health_xml(fh: IO[bytes]) -> HealthResult:
    stats = HealthStats()
    problems: dict[str, list] = {}  # reason -> [count, example]
    readings: dict[date, list[float]] = defaultdict(list)
    sources: dict[date, set[str]] = defaultdict(set)

    def invalid(reason: str, example: str) -> None:
        stats.invalid += 1
        problems.setdefault(reason, [0, example])[0] += 1

    depth = 0
    root = None
    try:
        for event, elem in iterparse(fh, events=("start", "end")):
            if event == "start":
                if root is None:
                    root = elem
                depth += 1
                continue
            depth -= 1
            if elem.tag == "Record":
                if elem.get("type") != BODY_MASS:
                    stats.other_records += 1
                else:
                    stats.records += 1
                    _read_record(elem, stats, readings, sources, invalid)
            # Free each top-level element (and anything nested in it) once read.
            if depth == 1:
                elem.clear()
                root.clear()
    except ParseError as exc:
        raise HealthParseError(f"not a readable Health export ({exc})") from None
    if root is None or root.tag != "HealthData":
        raise HealthParseError("not an Apple Health export (no <HealthData> root element)")

    days = [
        DailyWeight(d, round(sum(v) / len(v), 2), len(v), ", ".join(sorted(sources[d])))
        for d, v in sorted(readings.items())
    ]
    if days:
        stats.first, stats.last = days[0].day, days[-1].day
    warnings = [f"{n} record(s) skipped: {reason} (e.g. {ex})" for reason, (n, ex) in problems.items()]
    return HealthResult(days, stats, warnings)


def _read_record(elem, stats: HealthStats, readings, sources, invalid) -> None:
    unit = (elem.get("unit") or "").strip()
    value_text = elem.get("value")
    start = elem.get("startDate") or ""
    example = f"{value_text} {unit} at {start}".strip()
    if unit not in LB_PER_UNIT:
        invalid(f"unknown unit {unit!r}", example)
        return
    try:
        value = float(value_text)
    except (TypeError, ValueError):
        invalid("value isn't a number", example)
        return
    try:
        when = datetime.strptime(start.strip(), DATE_FORMAT)
    except ValueError:
        try:
            when = datetime.fromisoformat(start.strip())
        except ValueError:
            invalid("unreadable date", example)
            return
    lb = value * LB_PER_UNIT[unit]
    if not PLAUSIBLE_LB[0] <= lb <= PLAUSIBLE_LB[1]:
        invalid(f"implausible bodyweight (outside {PLAUSIBLE_LB[0]:g}-{PLAUSIBLE_LB[1]:g} lb)", example)
        return
    stats.units[unit] += 1
    day = when.date()  # the local date in the reading's own UTC offset
    readings[day].append(lb)
    if elem.get("sourceName"):
        sources[day].add(elem.get("sourceName"))
