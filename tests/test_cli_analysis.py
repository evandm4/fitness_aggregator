from datetime import datetime, timedelta

import pytest
from typer.testing import CliRunner

from fitness_aggregator.cli import app
from helpers import REAL_EXPORT, workout_rows, write_export


def day(n_days_ago: int, hour: int = 10) -> str:
    return (datetime.now() - timedelta(days=n_days_ago)).replace(hour=hour, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")


def bench(when, sets, name="Push 1", duration="1h"):
    return workout_rows(when, duration, name, exercises=(("Bench Press (Barbell)", sets),))


EXPORT = [
    bench(day(40), ((135, 5), (125, 8))),
    bench(day(30), ((140, 5), (125, 10))),
    workout_rows(day(25), exercises=(("Pull Up (Assisted)", ((60, 8), (45, 6))),), name="Pull 1"),
    workout_rows(day(20), exercises=(("Pull Up (Assisted)", ((50, 8),)),), name="Pull 1"),
    bench(day(3), ((150, 3), (130, 8))),
    workout_rows(day(2), exercises=(("Preacher Curl (Machine)", ((8, 80), (8, 60))),), name="Pull 1"),
]


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)  # no config/ here: defaults (180 lb) apply
    db = tmp_path / "lifts.db"
    runner = CliRunner()

    def _run(*args, input=None):
        return runner.invoke(app, ["--db", str(db), "--config", str(tmp_path / "s.toml"), *map(str, args)],
                             input=input)

    r = _run("import", write_export(tmp_path / "e.csv", *EXPORT))
    assert r.exit_code == 0, r.output
    return _run


def test_import_reports_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    r = CliRunner().invoke(app, ["--db", str(tmp_path / "x.db"), "import", str(write_export(tmp_path / "e.csv", *EXPORT))])
    assert "2 sets flagged as possible data-entry errors. Review with fitness check." in r.output


def test_history(run):
    r = run("history", "bench")
    assert r.exit_code == 0, r.output
    assert "Bench Press (Barbell)" in r.output
    assert "150 lb × 3" in r.output and "e1RM, Heaviest" in r.output
    assert "3 sessions; best e1RM 167 lb (125 × 10" in r.output


def test_history_assisted_shows_assistance_and_placeholder_note(run):
    r = run("history", "pull up")
    assert "45 lb assist × 6 (= 135 lb)" in r.output
    assert "placeholder bodyweight 180 lb" in r.output


def test_history_unknown_or_ambiguous_exercise(run):
    assert "no exercise matches" in run("history", "squat").output
    r = run("history", "e")
    assert r.exit_code == 1 and "matches 3 exercises" in r.output


def test_prs_table_detail_and_recent(run):
    r = run("prs")
    assert r.exit_code == 0 and "Bench Press (Barbell)" in r.output and "150 × 3" in r.output
    r = run("prs", "bench")
    assert "Best load for at least N reps" in r.output and "PR timeline" in r.output
    r = run("prs", "--recent", "7d")
    assert "Bench Press (Barbell)" in r.output and "Pull Up" not in r.output
    assert run("prs", "--recent", "soon").exit_code == 1


def test_formula_option(run):
    assert run("history", "bench", "--formula", "brzycki").exit_code == 0
    assert run("history", "bench", "--formula", "magic").exit_code == 1


def test_stats(run):
    r = run("stats")
    assert r.exit_code == 0, r.output
    assert "6 sessions" in r.output and "Week of" in r.output and "By workout name" in r.output
    r = run("stats", "--by", "month", "--all")
    assert "Month" in r.output
    assert run("stats", "--by", "year").exit_code == 1
    assert "No sessions in that range." in run("stats", "--since", "2099-01-01").output


def test_exercises(run):
    r = run("exercises")
    assert "Bench Press (Barbell)" in r.output and "assisted" in r.output and "3 exercises." in r.output
    r = run("exercises", "--search", "curl")
    assert "1 exercise." in r.output


def test_check_list_and_swap(run):
    r = run("check", "--list")
    assert "2 sets in 1 exercise entry look like data-entry errors." in r.output
    assert "Looks like weight and reps were swapped → 80 lb × 8, 60 lb × 8" in r.output
    r = run("check", input="s\n")
    assert "[s] swap weight and reps" in r.output
    assert "Recorded correction #" in r.output and "Swapped weight and reps on 2" in r.output
    assert "No likely data-entry errors found." in run("check").output
    r = run("show", "last")
    assert "was 8 lb × 80" in r.output


def test_check_edit_and_dismiss(run):
    r = run("check", input="e\n70\n8\n80\n6\n")
    assert "Edited Preacher Curl (Machine)" in r.output
    run("corrections", "revert", "1")
    r = run("check", input="i\n")
    assert "Dismissed" in r.output
    assert "1 flag dismissed earlier" not in run("check").output
    assert "flags dismissed earlier" in run("check").output


def test_check_skip_and_bad_choice(run):
    r = run("check", input="x\nk\n")
    assert "Choose one of" in r.output and "Skipped." in r.output


def test_fix_command(run):
    when = day(2)[:16].replace(" ", "T")
    r = run("fix", when, "preacher", "--set", "1", "--swap-weight-reps")
    assert r.exit_code == 0, r.output
    assert r.output.startswith("Recorded correction #1: Swapped weight and reps on 1")
    r = run("fix", when, "preacher", "--set", "2", "--weight", "60", "--reps", "8")
    assert r.output.startswith("Updated correction #1")
    assert "1 correction" in run("corrections", "--user").output


@pytest.mark.parametrize("args, message", [
    (["--swap-weight-reps", "--weight", "5"], "not both"),
    ([], "say what to change"),
    (["--set", "D", "--reps", "1"], "has no set 'D'"),
    (["--set", "1", "--weight", "8", "--reps", "80"], "nothing to change"),
])
def test_fix_errors(run, args, message):
    r = run("fix", day(2)[:10], "preacher", *args)
    assert r.exit_code == 1 and message in r.output


def test_fix_unknown_workout(run):
    assert "no workout starts at" in run("fix", "1999-01-01", "bench", "--reps", "1").output


@pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not present")
def test_real_export_check_and_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    runner = CliRunner()
    db = str(tmp_path / "real.db")
    invoke = lambda *a, **k: runner.invoke(app, ["--db", db, *a], **k)
    assert "3 sets flagged" in invoke("import", str(REAL_EXPORT)).output
    r = invoke("check", input="s\n")
    assert "Swapped weight and reps on 3 Preacher Curl (Machine) sets, 2026-06-04 13:53" in r.output
    r = invoke("history", "Preacher Curl (Machine)")
    assert "2026-06-04 *" in r.output and "80 lb × 8" in r.output
