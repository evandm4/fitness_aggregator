"""Exercise metadata: what kind of measurement each exercise uses.

Kinds:

* ``loaded``    weight x reps (the default)
* ``assisted``  weight is assistance, so less is better; effective load is
                bodyweight - assistance
* ``timed``     a hold measured in seconds (Plank)
* ``unloaded``  reps with no external weight (Supported Leg Raise)

Kinds are inferred from the exercise name and the data, and can be overridden
in ``config/exercises.toml``::

    [exercise."Dead Hang"]
    kind = "timed"
"""

from __future__ import annotations

import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .models import SetType, Workout

LOADED, ASSISTED, TIMED, UNLOADED = "loaded", "assisted", "timed", "unloaded"
KINDS = (LOADED, ASSISTED, TIMED, UNLOADED)


def default_path() -> Path:
    return Path.cwd() / "config" / "exercises.toml"


@dataclass
class Catalog:
    kinds: dict[str, str] = field(default_factory=dict)

    def kind(self, exercise: str) -> str:
        return self.kinds.get(exercise, LOADED)

    @classmethod
    def build(cls, workouts: Iterable[Workout], overrides_path: Path | None = None) -> "Catalog":
        all_sets = defaultdict(list)
        for w in workouts:
            for entry in w.exercises:
                all_sets[entry.name].extend(entry.sets)

        kinds = {}
        for name, sets in all_sets.items():
            # Judge by working sets; fall back to warm-ups if that's all there is.
            working = [s for s in sets if s.set_type is not SetType.WARMUP]
            kinds[name] = infer_kind(name, working or sets)
        kinds.update(load_overrides(overrides_path or default_path()))
        return cls(kinds)


def infer_kind(name: str, sets) -> str:
    if "assisted" in name.lower():
        return ASSISTED
    sets = list(sets)
    if sets and all(s.seconds > 0 and s.reps == 0 and s.weight == 0 for s in sets):
        return TIMED
    if sets and all(s.weight == 0 for s in sets):
        return UNLOADED
    return LOADED


def load_overrides(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    out = {}
    for name, meta in data.get("exercise", {}).items():
        kind = meta.get("kind")
        if kind in KINDS:
            out[name] = kind
    return out
