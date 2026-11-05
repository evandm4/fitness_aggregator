"""Exercise metadata: how each exercise is measured and which muscles it trains.

Kinds:

* ``loaded``    weight x reps (the default)
* ``assisted``  weight is assistance, so less is better; effective load is
                bodyweight - assistance
* ``timed``     a hold measured in seconds (Plank)
* ``unloaded``  reps with no external weight (Supported Leg Raise)

Kinds are inferred from the exercise name and the data. Both kinds and muscles
come from ``config/exercises.toml``::

    [exercise."Bench Press (Barbell)"]
    primary = ["chest"]
    secondary = ["triceps", "front_delts"]

    [exercise."Dead Hang"]
    kind = "timed"

Muscle names must come from ``MUSCLES``; others are ignored with a warning
(collected in ``Catalog.warnings``). Exercises with no entry get a
keyword-based guess (``guess_muscles``), which stats use only when there's no
explicit mapping and always label as a guess.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .models import SetType, Workout

LOADED, ASSISTED, TIMED, UNLOADED = "loaded", "assisted", "timed", "unloaded"
KINDS = (LOADED, ASSISTED, TIMED, UNLOADED)

# Fixed vocabulary, in a roughly head-to-toe display order.
MUSCLES = (
    "chest", "front_delts", "side_delts", "rear_delts", "triceps", "biceps", "forearms",
    "lats", "upper_back", "lower_back", "abs", "obliques",
    "quads", "hamstrings", "glutes", "adductors", "abductors", "calves",
)

Muscles = tuple[list[str], list[str]]  # (primary, secondary)


ENV_VAR = "FITNESS_AGGREGATOR_EXERCISES"
# The copy shipped with the project (``<repo>/config/exercises.toml``), found
# relative to this file so it works from any working directory.
PROJECT_FILE = Path(__file__).resolve().parents[2] / "config" / "exercises.toml"


def candidate_paths() -> list[Path]:
    """Where to look for exercise metadata, in order."""
    paths = []
    if os.environ.get(ENV_VAR):
        paths.append(Path(os.environ[ENV_VAR]))
    paths += [
        Path.cwd() / "config" / "exercises.toml",
        Path.home() / ".fitness_aggregator" / "exercises.toml",
        PROJECT_FILE,
    ]
    return paths


def default_path() -> Path:
    """The first exercise metadata file that exists (or the local path, if none do).

    Looks at ``$FITNESS_AGGREGATOR_EXERCISES``, ``./config/exercises.toml``,
    ``~/.fitness_aggregator/exercises.toml``, then the project's own copy, so
    muscle mappings work no matter which folder you run ``fitness`` from.
    """
    paths = candidate_paths()
    return next((p for p in paths if p.exists()), paths[0])


@dataclass
class Catalog:
    kinds: dict[str, str] = field(default_factory=dict)
    # Explicit mappings from exercises.toml. An entry with empty lists is still
    # explicit (e.g. "Dessert": deliberately no muscles).
    muscle_map: dict[str, Muscles] = field(default_factory=dict)
    names: set[str] = field(default_factory=set)  # exercises seen in the data
    warnings: list[str] = field(default_factory=list)

    def kind(self, exercise: str) -> str:
        return self.kinds.get(exercise, LOADED)

    def has_mapping(self, exercise: str) -> bool:
        return exercise in self.muscle_map

    def muscles(self, exercise: str, *, guess: bool = True) -> Muscles:
        """(primary, secondary) muscles: the explicit mapping, else a guess (unless ``guess=False``)."""
        if exercise in self.muscle_map:
            p, s = self.muscle_map[exercise]
            return list(p), list(s)
        if guess:
            g = guess_muscles(exercise)
            if g is not None:
                return g
        return [], []

    def is_guessed(self, exercise: str) -> bool:
        """True when ``muscles()`` for this exercise comes from a guess."""
        return exercise not in self.muscle_map and guess_muscles(exercise) is not None

    def unmapped(self, names: Iterable[str] | None = None) -> list[str]:
        """Exercises (from ``names``, default all seen in the data) with no explicit mapping."""
        return sorted(n for n in (self.names if names is None else names) if n not in self.muscle_map)

    @classmethod
    def build(cls, workouts: Iterable[Workout], overrides_path: Path | None = None) -> Catalog:
        all_sets = defaultdict(list)
        for w in workouts:
            for entry in w.exercises:
                all_sets[entry.name].extend(entry.sets)

        kinds = {}
        for name, sets in all_sets.items():
            # Judge by working sets; fall back to warm-ups if that's all there is.
            working = [s for s in sets if s.set_type is not SetType.WARMUP]
            kinds[name] = infer_kind(name, working or sets)
        meta = load_metadata(overrides_path or default_path())
        kinds.update(meta.kinds)
        return cls(kinds, meta.muscles, set(all_sets), meta.warnings)


def infer_kind(name: str, sets) -> str:
    if "assisted" in name.lower():
        return ASSISTED
    sets = list(sets)
    if sets and all(s.seconds > 0 and s.reps == 0 and s.weight == 0 for s in sets):
        return TIMED
    if sets and all(s.weight == 0 for s in sets):
        return UNLOADED
    return LOADED


# --- exercises.toml -------------------------------------------------------------

@dataclass
class Metadata:
    kinds: dict[str, str] = field(default_factory=dict)
    muscles: dict[str, Muscles] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def load_metadata(path: Path) -> Metadata:
    meta = Metadata()
    if not path.exists():
        return meta
    with open(path, "rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            meta.warnings.append(f"{path}: not valid TOML ({exc}); exercise metadata ignored")
            return meta
    for name, entry in data.get("exercise", {}).items():
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind in KINDS:
            meta.kinds[name] = kind
        elif kind is not None:
            meta.warnings.append(f"{name}: unknown kind {kind!r} ignored")
        # `muscles` is accepted as an alias for `primary` (the format sketched in PLAN.md).
        primary = entry.get("primary", entry.get("muscles"))
        secondary = entry.get("secondary")
        if primary is None and secondary is None:
            continue  # kind-only entry: muscles stay unmapped
        p = _valid_muscles(name, primary, meta.warnings)
        s = [m for m in _valid_muscles(name, secondary, meta.warnings) if m not in p]
        meta.muscles[name] = (p, s)
    return meta


def _valid_muscles(name: str, value, warnings: list[str]) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    out = []
    for m in value:
        key = str(m).strip().lower().replace(" ", "_").replace("-", "_")
        if key in MUSCLES:
            if key not in out:
                out.append(key)
        else:
            warnings.append(f"{name}: unknown muscle {m!r} ignored")
    return out


def load_overrides(path: Path) -> dict[str, str]:
    """Kind overrides only (kept for callers that need nothing else)."""
    return load_metadata(path).kinds


# --- guessing -------------------------------------------------------------------

# (words that must all appear, primary, secondary). First match wins, so more
# specific patterns come first. Words match at the start of a word, so
# "tricep" matches "Triceps" and "curl" matches "Curls".
_GUESSES: list[tuple[tuple[str, ...], list[str], list[str]]] = [
    (("leg", "curl"), ["hamstrings"], []),
    (("hamstring",), ["hamstrings"], []),
    (("wrist",), ["forearms"], []),
    (("reverse", "curl"), ["forearms", "biceps"], []),
    (("hammer",), ["biceps", "forearms"], []),
    (("zottman",), ["biceps", "forearms"], []),
    (("calf",), ["calves"], []),
    (("calves",), ["calves"], []),
    (("leg", "extension"), ["quads"], []),
    (("back", "extension"), ["lower_back", "glutes"], ["hamstrings"]),
    (("hyperextension",), ["lower_back", "glutes"], ["hamstrings"]),
    (("good", "morning"), ["hamstrings", "lower_back"], ["glutes"]),
    (("tricep",), ["triceps"], []),
    (("skullcrusher",), ["triceps"], []),
    (("pushdown",), ["triceps"], []),
    (("kickback",), ["triceps"], []),
    (("dip",), ["chest", "triceps"], ["front_delts"]),
    (("curl",), ["biceps"], ["forearms"]),
    (("bicep",), ["biceps"], []),
    (("romanian",), ["hamstrings", "glutes"], ["lower_back"]),
    (("rdl",), ["hamstrings", "glutes"], ["lower_back"]),
    (("deadlift",), ["glutes", "hamstrings", "lower_back"], ["quads", "upper_back", "forearms"]),
    (("squat",), ["quads", "glutes"], ["adductors"]),
    (("lunge",), ["quads", "glutes"], ["adductors"]),
    (("step", "up"), ["quads", "glutes"], []),
    (("leg", "press"), ["quads"], ["glutes", "adductors"]),
    (("hip", "thrust"), ["glutes"], ["hamstrings"]),
    (("glute",), ["glutes"], []),
    (("bridge",), ["glutes"], ["hamstrings"]),
    (("abductor",), ["abductors"], ["glutes"]),
    (("abduction",), ["abductors"], ["glutes"]),
    (("adductor",), ["adductors"], []),
    (("adduction",), ["adductors"], []),
    (("lateral", "raise"), ["side_delts"], []),
    (("upright", "row"), ["side_delts"], ["upper_back"]),
    (("front", "raise"), ["front_delts"], []),
    (("face", "pull"), ["rear_delts"], ["upper_back"]),
    (("reverse", "fly"), ["rear_delts"], ["upper_back"]),
    (("rear", "delt"), ["rear_delts"], ["upper_back"]),
    (("fly",), ["chest"], ["front_delts"]),
    (("flye",), ["chest"], ["front_delts"]),
    (("pec",), ["chest"], ["front_delts"]),
    (("crossover",), ["chest"], ["front_delts"]),
    (("shoulder", "press"), ["front_delts"], ["triceps", "side_delts"]),
    (("overhead", "press"), ["front_delts"], ["triceps", "side_delts"]),
    (("military",), ["front_delts"], ["triceps", "side_delts"]),
    (("arnold",), ["front_delts"], ["triceps", "side_delts"]),
    (("bench",), ["chest"], ["triceps", "front_delts"]),
    (("decline", "press"), ["chest"], ["triceps"]),
    (("incline", "press"), ["chest"], ["front_delts", "triceps"]),
    (("chest", "press"), ["chest"], ["triceps", "front_delts"]),
    (("push", "up"), ["chest"], ["triceps", "front_delts"]),
    (("pushup",), ["chest"], ["triceps", "front_delts"]),
    (("pulldown",), ["lats"], ["biceps", "upper_back"]),
    (("pull", "down"), ["lats"], ["biceps", "upper_back"]),
    (("pull", "up"), ["lats"], ["biceps", "upper_back"]),
    (("pullup",), ["lats"], ["biceps", "upper_back"]),
    (("chin", "up"), ["lats", "biceps"], ["upper_back"]),
    (("pull", "in"), ["lats"], []),
    (("pullover",), ["lats"], ["chest"]),
    (("row",), ["upper_back", "lats"], ["biceps", "rear_delts"]),
    (("shrug",), ["upper_back"], ["forearms"]),
    (("crunch",), ["abs"], []),
    (("sit", "up"), ["abs"], []),
    (("situp",), ["abs"], []),
    (("leg", "raise"), ["abs"], []),
    (("knee", "raise"), ["abs"], []),
    (("plank",), ["abs"], ["obliques"]),
    (("ab",), ["abs"], []),
    (("twist",), ["obliques"], ["abs"]),
    (("torso",), ["obliques"], ["abs"]),
    (("woodchop",), ["obliques"], ["abs"]),
    (("oblique",), ["obliques"], ["abs"]),
    (("side", "bend"), ["obliques"], []),
]


def guess_muscles(exercise: str) -> Muscles | None:
    """A keyword guess for an exercise with no explicit mapping, or None."""
    words = re.findall(r"[a-z]+", exercise.lower())
    for keys, primary, secondary in _GUESSES:
        if all(any(w.startswith(k) for w in words) for k in keys):
            return list(primary), list(secondary)
    return None
