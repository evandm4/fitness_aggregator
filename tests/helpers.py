"""Helpers for building small Strong CSV exports in tests."""

from __future__ import annotations

from pathlib import Path

HEADER = "Date,Workout Name,Duration,Exercise Name,Set Order,Weight,Reps,Distance,Seconds,Notes,Workout Notes,RPE\n"
REAL_EXPORT = Path(__file__).resolve().parent.parent / "weights_11_3_26.txt"


def workout_rows(start: str, duration: str = "1h", name: str = "Push 1",
                 exercises=(("Bench Press (Barbell)", ((135, 8), (135, 6))),)) -> str:
    """CSV rows for one workout. ``exercises`` is ((name, ((weight, reps), ...)), ...)."""
    out = []
    for ex, sets in exercises:
        for i, (weight, reps) in enumerate(sets, start=1):
            out.append(f'{start},"{name}",{duration},"{ex}",{i},{weight},{reps},0,0.0,"","",\n')
            out.append(f'{start},"{name}",{duration},"{ex}",Rest Timer,0,0.0,0,90.0,,,\n')
    return "".join(out)


def export_text(*workouts: str) -> str:
    return HEADER + "".join(workouts)


def write_export(path: Path, *workouts: str) -> Path:
    path.write_text(export_text(*workouts))
    return path
