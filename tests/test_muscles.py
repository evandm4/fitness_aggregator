import tomllib
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from fitness_aggregator import metrics
from fitness_aggregator.bodyweight import Bodyweight
from fitness_aggregator.catalog import MUSCLES, TIMED, Catalog, guess_muscles, load_metadata
from fitness_aggregator.cli import app
from fitness_aggregator.importers import parse_strong_csv, parse_strong_text
from fitness_aggregator.sessions import build_sessions
from helpers import REAL_EXPORT, export_text, workout_rows, write_export

CONFIG = Path(__file__).resolve().parent.parent / "config" / "exercises.toml"


def write_toml(tmp_path, text):
    path = tmp_path / "exercises.toml"
    path.write_text(text)
    return path


# --- config/exercises.toml --------------------------------------------------------

@pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not present")
def test_every_real_exercise_is_mapped_with_valid_muscles():
    workouts = parse_strong_csv(REAL_EXPORT).workouts
    cat = Catalog.build(workouts, CONFIG)
    assert cat.warnings == []
    assert len(cat.names) == 103 and cat.unmapped() == []
    for name in cat.names:
        primary, secondary = cat.muscles(name)
        assert set(primary) | set(secondary) <= set(MUSCLES)
        assert name == "Dessert" or primary, name
    assert cat.muscles("Dessert") == ([], []) and cat.kind("Dessert") == TIMED
    assert cat.muscles("Bench Press (Barbell)") == (["chest"], ["triceps", "front_delts"])


def test_config_parses_and_uses_only_known_keys():
    data = tomllib.loads(CONFIG.read_text())
    for name, entry in data["exercise"].items():
        assert set(entry) <= {"primary", "secondary", "kind"}, name


# --- catalog ----------------------------------------------------------------------

def test_load_metadata_validates_muscles(tmp_path):
    path = write_toml(tmp_path, """
[exercise."Curl"]
primary = ["biceps", "Biceps", "pecs"]
secondary = ["forearms", "biceps"]

[exercise."Hold"]
kind = "timed"

[exercise."Old Style"]
muscles = ["front delts"]
""")
    meta = load_metadata(path)
    assert meta.muscles["Curl"] == (["biceps"], ["forearms"])
    assert meta.muscles["Old Style"] == (["front_delts"], [])
    assert "Hold" not in meta.muscles and meta.kinds == {"Hold": "timed"}
    assert meta.warnings == ["Curl: unknown muscle 'pecs' ignored"]


def test_invalid_toml_is_a_warning(tmp_path):
    meta = load_metadata(write_toml(tmp_path, "[exercise.\n"))
    assert meta.muscles == {} and "not valid TOML" in meta.warnings[0]


def test_explicit_mapping_beats_guess_and_unmapped(tmp_path):
    path = write_toml(tmp_path, '[exercise."Weird Curl"]\nprimary = ["forearms"]\n')
    workouts = parse_strong_text(export_text(workout_rows("2026-06-02 10:00:00", exercises=(
        ("Weird Curl", ((20, 10),)), ("Cable Thing", ((20, 10),)), ("Hammer Curl (Dumbbell)", ((20, 10),)),
    )))).workouts
    cat = Catalog.build(workouts, path)
    assert cat.muscles("Weird Curl") == (["forearms"], []) and not cat.is_guessed("Weird Curl")
    assert cat.muscles("Hammer Curl (Dumbbell)") == (["biceps", "forearms"], [])
    assert cat.is_guessed("Hammer Curl (Dumbbell)")
    assert cat.muscles("Hammer Curl (Dumbbell)", guess=False) == ([], [])
    assert cat.muscles("Cable Thing") == ([], []) and not cat.is_guessed("Cable Thing")
    assert cat.unmapped() == ["Cable Thing", "Hammer Curl (Dumbbell)"]


@pytest.mark.parametrize("name, primary", [
    ("Bicep Curl (Cable)", ["biceps"]),
    ("Lying Leg Curl (Machine)", ["hamstrings"]),
    ("Tricep Pushdown (Cable - Rope)", ["triceps"]),
    ("Overhead Triceps Extension", ["triceps"]),
    ("Leg Extension (Machine)", ["quads"]),
    ("Back Extension", ["lower_back", "glutes"]),
    ("Front Squat (Barbell)", ["quads", "glutes"]),
    ("Incline Bench Press (Dumbbell)", ["chest"]),
    ("Lateral Raise (Cable)", ["side_delts"]),
    ("Reverse Fly (Machine)", ["rear_delts"]),
    ("Chest Fly (Cable)", ["chest"]),
    ("Chin Up", ["lats", "biceps"]),
    ("Seated Row (Cable)", ["upper_back", "lats"]),
    ("Hip Abductor (Machine)", ["abductors"]),
    ("Hip Adductor (Machine)", ["adductors"]),
    ("Standing Calf Raise", ["calves"]),
    ("Russian Twist", ["obliques"]),
    ("Hanging Leg Raise", ["abs"]),
])
def test_guesses(name, primary):
    assert guess_muscles(name)[0] == primary


def test_no_guess():
    assert guess_muscles("Dessert") is None
    assert guess_muscles("Farmer Carry") is None


# --- weekly_muscle_sets ---------------------------------------------------------------

def frame(tmp_path, *workouts, toml=None):
    rows = "".join(workouts)
    ws = parse_strong_text(export_text(rows)).workouts
    cat = Catalog.build(ws, write_toml(tmp_path, toml) if toml else tmp_path / "none.toml")
    return metrics.sets_frame(build_sessions(ws), cat, Bodyweight(180)), cat


TOML = """
[exercise."Bench"]
primary = ["chest"]
secondary = ["triceps", "front_delts"]
[exercise."Curl"]
primary = ["biceps"]
"""


def test_weekly_muscle_sets_contract_and_credits(tmp_path):
    warmup = '2026-06-01 10:00:00,"Push",1h,"Bench",W,45,10.0,0,0.0,"","",\n'
    drop = '2026-06-01 10:00:00,"Push",1h,"Bench",D,95,10.0,0,0.0,"","",\n'
    df, cat = frame(
        tmp_path,
        warmup,
        workout_rows("2026-06-01 10:00:00", exercises=(("Bench", ((135, 8), (135, 8))),)),  # Monday, week 1
        drop,
        workout_rows("2026-06-21 10:00:00", exercises=(("Curl", ((30, 10),)),)),  # Sunday, still week 3
        toml=TOML,
    )
    out = metrics.weekly_muscle_sets(df, cat)
    assert list(out.columns) == ["period", "muscle", "sets"]
    assert out["period"].dtype == pd.PeriodDtype("W-SUN")
    assert out["sets"].dtype == float
    table = out.set_index(["period", "muscle"])["sets"]
    w1, w2, w3 = pd.period_range("2026-06-01", periods=3, freq="W-SUN")
    assert table[(w1, "chest")] == 3.0  # 2 normal + 1 drop, warm-up excluded
    assert table[(w1, "triceps")] == 1.5 and table[(w1, "front_delts")] == 1.5
    assert table[(w1, "biceps")] == 0.0
    assert table[(w2, "chest")] == 0.0  # empty week in between is present
    assert table[(w3, "biceps")] == 1.0
    assert set(out["muscle"]) == {"chest", "triceps", "front_delts", "biceps"}
    assert len(out) == 3 * 4


def test_weekly_muscle_sets_by_month_and_guesses(tmp_path):
    df, cat = frame(
        tmp_path,
        workout_rows("2026-06-30 10:00:00", exercises=(("Bicep Curl (Cable)", ((30, 10), (30, 10))),)),
        workout_rows("2026-08-02 10:00:00", exercises=(("Mystery", ((30, 10),)),)),
    )
    out = metrics.weekly_muscle_sets(df, cat, by="month")
    assert out["period"].dtype == pd.PeriodDtype("M")
    table = out.set_index(["period", "muscle"])["sets"]
    june, july, aug = pd.period_range("2026-06", periods=3, freq="M")
    assert table[(june, "biceps")] == 2.0 and table[(june, "forearms")] == 1.0  # guessed
    assert table[(july, "biceps")] == 0.0 and table[(aug, "biceps")] == 0.0
    assert "Mystery" not in set(out["muscle"])


def test_weekly_muscle_sets_empty(tmp_path):
    df, cat = frame(tmp_path, workout_rows("2026-06-01 10:00:00", exercises=(("Mystery", ((30, 10),)),)))
    for d in (df, df.iloc[:0]):
        out = metrics.weekly_muscle_sets(d, cat)
        assert out.empty and list(out.columns) == ["period", "muscle", "sets"]
    with pytest.raises(ValueError):
        metrics.weekly_muscle_sets(df, cat, by="day")


# --- CLI -------------------------------------------------------------------------

@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "exercises.toml").write_text(TOML + '[exercise."Bogus"]\nprimary = ["wings"]\n')
    db = tmp_path / "lifts.db"
    runner = CliRunner()

    def _run(*args, input=None):
        return runner.invoke(app, ["--db", str(db), "--config", str(tmp_path / "s.toml"), *map(str, args)],
                             input=input)

    export = write_export(
        tmp_path / "e.csv",
        workout_rows("2026-06-01 10:00:00", exercises=(("Bench", tuple((135, 8) for _ in range(12))),)),
        workout_rows("2026-06-08 10:00:00", exercises=(
            ("Bench", tuple((135, 8) for _ in range(22))), ("Hammer Curl (Dumbbell)", ((30, 10),)),
            ("Mystery", ((30, 10),)),
        )),
    )
    assert _run("import", export).exit_code == 0
    return _run


def test_cli_muscles(run):
    r = run("muscles")
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].split() == ["Muscle", "06-01", "06-08", "Avg"]
    chest = next(line for line in lines if line.startswith("chest"))
    assert chest.split() == ["chest", "12", "22", "17.0"]
    front = next(line for line in lines if line.startswith("front delts"))
    assert front.split() == ["front", "delts", "6", "11", "8.5"]
    assert "Guessed muscles for 1 exercise" in r.output and "Hammer Curl (Dumbbell) → biceps, forearms" in r.output
    assert "Not counted (no muscles known): Mystery" in r.output
    assert "unknown muscle 'wings' ignored" in r.output


def test_cli_muscles_month_last_and_since(run):
    r = run("muscles", "--by", "month")
    assert r.exit_code == 0 and "Jun 26" in r.output
    r = run("muscles", "--last", "1")
    assert "06-01" not in r.output and "Showing the last 1 of 2 weeks" in r.output
    r = run("muscles", "--since", "2026-06-05")
    assert "06-01" not in r.output and "06-08" in r.output
    assert run("muscles", "--by", "day").exit_code == 1


def test_cli_exercises_muscles_column_and_unmapped(run):
    r = run("exercises")
    assert r.exit_code == 0, r.output
    assert "Muscles" in r.output
    bench = next(line for line in r.output.splitlines() if line.startswith("Bench"))
    assert bench.rstrip().endswith("chest")
    assert "biceps, forearms?" in r.output
    assert "1 exercise with guessed muscles" in r.output

    r = run("exercises", "--unmapped")
    assert r.exit_code == 0, r.output
    assert "Hammer Curl (Dumbbell)" in r.output and "Mystery" in r.output and "no guess" in r.output
    assert "Bench" not in r.output.split("without an entry")[0]
    assert '[exercise."Hammer Curl (Dumbbell)"]' in r.output
    assert "2 exercises without an entry" in r.output
