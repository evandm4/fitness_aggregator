import pytest
from typer.testing import CliRunner

from fitness_aggregator.cli import app
from helpers import REAL_EXPORT, workout_rows, write_export

A = workout_rows("2026-06-02 14:00:00", "3m", name="Midday")
B = workout_rows("2026-06-02 14:06:00", "47m", name="Midday")
C = workout_rows("2026-06-04 13:00:00", "1h", name="Pull 1")
D = workout_rows("2026-06-06 13:00:00", "14h 7m", name="Legs")


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    db, config = tmp_path / "lifts.db", tmp_path / "settings.toml"
    runner = CliRunner()

    def _run(*args, input=None):
        return runner.invoke(app, ["--db", str(db), "--config", str(config), *map(str, args)], input=input)

    return _run


def test_import_reports_parse_counts_and_corrections(run, tmp_path):
    r = run("import", write_export(tmp_path / "e.csv", A, B, C, D))
    assert r.exit_code == 0, r.output
    assert "Parsed 16 rows: 4 workouts, 8 sets" in r.output
    assert "8 rest timers attached to sets." in r.output
    assert "First import: 4 workouts." in r.output
    assert "+ #" in r.output and "Merged 2026-06-02 14:06 workout into 2026-06-02 14:00 workout" in r.output
    assert "Duration 14h 7m excluded" in r.output
    assert "Corrections applied: 1 duration excluded, 1 workout merged." in r.output


def test_reimport_says_no_changes(run, tmp_path):
    path = write_export(tmp_path / "e.csv", A, B, C)
    run("import", path)
    r = run("import", path)
    assert r.exit_code == 0
    assert "No changes: this export matches the stored data (3 workouts)." in r.output


def test_import_lists_changes_and_confirms_removals(run, tmp_path):
    run("import", write_export(tmp_path / "1.csv", A, B, C))
    path = write_export(tmp_path / "2.csv", A, C, D)
    r = run("import", path, input="n\n")
    assert r.exit_code == 1
    assert "1 new, 0 edited, 1 removed, 2 unchanged" in r.output
    assert "removed Tue 2026-06-02 14:06  Midday" in r.output
    assert "Import cancelled; nothing changed." in r.output
    r = run("import", path, input="y\n")
    assert r.exit_code == 0 and "Imported. 3 workouts stored." in r.output
    assert "no longer applies: Merged" in r.output


def test_import_yes_skips_prompt(run, tmp_path):
    run("import", write_export(tmp_path / "1.csv", A, B, C))
    r = run("import", write_export(tmp_path / "2.csv", A, B, C, D, ), "--yes")
    assert r.exit_code == 0
    r = run("import", write_export(tmp_path / "3.csv", A, B, D), "--yes")
    assert r.exit_code == 0 and "Imported." in r.output


def test_import_refuses_older_export(run, tmp_path):
    run("import", write_export(tmp_path / "new.csv", A, B, C, D))
    r = run("import", write_export(tmp_path / "old.csv", A, B), "--yes")
    assert r.exit_code == 1 and "older than what's already stored" in r.output
    r = run("import", tmp_path / "old.csv", "--yes", "--force")
    assert r.exit_code == 0


def test_import_bad_file(run, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("Date,Exercise Name\n2026-06-02,Curl\n")
    r = run("import", bad)
    assert r.exit_code == 1 and "missing column" in r.output


def test_workouts_list_and_raw(run, tmp_path):
    run("import", write_export(tmp_path / "e.csv", A, B, C, D))
    r = run("workouts")
    assert r.exit_code == 0
    assert "merged ×2" in r.output and "duration excluded" in r.output and "(14h 7m)" in r.output
    assert "3 sessions." in r.output
    r = run("workouts", "--raw")
    assert "4 sessions (raw: no corrections applied)" in r.output
    r = run("workouts", "--last", "1")
    assert "1 of 3 sessions" in r.output


def test_workouts_empty_db(run):
    assert "No workouts yet" in run("workouts").output


def test_show_session_and_corrections(run, tmp_path):
    run("import", write_export(tmp_path / "e.csv", A, B, C))
    r = run("show", "2026-06-02T14:06")
    assert r.exit_code == 0
    assert "Midday  Tue 2026-06-02 14:00" in r.output and "2 workouts merged" in r.output
    assert "135 lb" in r.output and "× 8" in r.output
    assert "Corrections applied:" in r.output
    r = run("show")
    assert "Pull 1  Thu 2026-06-04 13:00" in r.output
    assert run("show", "2030-01-01").exit_code == 1


def test_unmerge_and_restore(run, tmp_path):
    run("import", write_export(tmp_path / "e.csv", A, B, C))
    r = run("workouts", "unmerge", "2026-06-02T14:06")
    assert r.exit_code == 0 and r.output.startswith("Reverted #1")
    assert "3 sessions." in run("workouts").output
    assert run("workouts", "unmerge", "2026-06-02T14:06").exit_code == 1
    r = run("corrections", "restore", "1")
    assert r.output.startswith("Restored #1")
    assert "2 sessions." in run("workouts").output


def test_corrections_list_and_filters(run, tmp_path):
    run("import", write_export(tmp_path / "e.csv", A, B, C, D))
    r = run("corrections")
    assert "workout_merge" in r.output and "duration_suspect" in r.output
    assert "2 corrections." in r.output
    r = run("corrections", "--type", "workout_merge")
    assert "duration_suspect" not in r.output
    assert "No corrections match." in run("corrections", "--user").output
    assert run("corrections", "--auto", "--user").exit_code == 1
    assert run("corrections", "revert", "42").exit_code == 1
    run("corrections", "revert", "1")
    assert "already reverted" in run("corrections", "revert", "1").output


def test_config_set_refreshes_corrections(run, tmp_path):
    run("import", write_export(tmp_path / "e.csv", A, B, C))
    r = run("config", "set", "merge_gap_minutes", "2")
    assert r.exit_code == 0
    assert "merge_gap_minutes: 60 → 2" in r.output and "no longer applies" in r.output
    r = run("config", "show")
    assert "merge_gap_minutes = 2" in r.output
    assert run("config", "set", "nonsense", "1").exit_code == 1


@pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not present")
def test_real_export_via_cli(run):
    r = run("import", REAL_EXPORT)
    assert r.exit_code == 0, r.output
    assert "Corrections applied: 2 durations excluded, 1 workout merged." in r.output
    assert "103 sessions." in run("workouts", "--all").output
