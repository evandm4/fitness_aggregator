from collections import defaultdict
from datetime import date

from .db import LiftSet


def e1rm(weight: float, reps: int) -> float:
    """Estimated one-rep max (Epley). A single is its own 1RM."""
    return weight if reps == 1 else weight * (1 + reps / 30)


def volume(sets: list[LiftSet]) -> float:
    return sum(s.weight * s.reps for s in sets)


def personal_records(sets: list[LiftSet]) -> dict[str, dict]:
    """Per exercise: heaviest weight, best e1RM, best single-set volume."""
    prs: dict[str, dict] = {}
    for s in sets:
        p = prs.setdefault(s.exercise, {"max_weight": s, "best_e1rm": s, "best_volume": s})
        if s.weight > p["max_weight"].weight:
            p["max_weight"] = s
        if e1rm(s.weight, s.reps) > e1rm(p["best_e1rm"].weight, p["best_e1rm"].reps):
            p["best_e1rm"] = s
        if s.weight * s.reps > p["best_volume"].weight * p["best_volume"].reps:
            p["best_volume"] = s
    return prs


def progression(sets: list[LiftSet]) -> list[tuple[str, float]]:
    """Best e1RM per day, chronological."""
    best: dict[str, float] = defaultdict(float)
    for s in sets:
        best[s.day] = max(best[s.day], e1rm(s.weight, s.reps))
    return sorted(best.items())


def weekly_volume(sets: list[LiftSet]) -> list[tuple[str, float]]:
    """Total volume per ISO week (label like 2026-W44)."""
    out: dict[str, float] = defaultdict(float)
    for s in sets:
        y, w, _ = date.fromisoformat(s.day).isocalendar()
        out[f"{y}-W{w:02d}"] += s.weight * s.reps
    return sorted(out.items())
