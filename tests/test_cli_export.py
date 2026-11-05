import csv
import io
import json

import pytest
from typer.testing import CliRunner

from fitness_aggregator import metrics
from fitness_aggregator.cli import app
from fitness_aggregator.cli_export import CORRECTION_COLUMNS, SESSION_COLUMNS
from helpers import REAL_EXPORT, workout_rows, write_export

# A and B merge into one session; C is two days later and heavier (a PR).
A = workout_rows("2026-06-02 14:00:00", "3m", name="Midday")
B = workout_rows("2026-06-02 14:06:00", "47m", name="Midday",
                 exercises=(("Lat Pulldown (Cable)", ((100, 10), (100, 8))),))
C = workout_rows("2026-06-04 13:00:00", "1h", name="Push 1",
                 exercises=(("Bench Press (Barbell)", ((155, 8), (155, 6))),))


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    db, config = tmp_path / "lifts.db", tmp_path / "settings.toml"
    runner = CliRunner()

    def _run(*args, input=None):
        return runner.invoke(app, ["--db", str(db), "--config", str(config), *map(str, args)], input=input)

    return _run


@pytest.fixture
def imported(run, tmp_path):
    r = run("import", write_export(tmp_path / "e.csv", A, B, C))
    assert r.exit_code == 0, r.output
    return run


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_empty_database_exports_header_only(run):
    r = run("export")
    assert r.exit_code == 0, r.output
    assert r.stdout == ",".join(metrics.SET_COLUMNS) + "\n"
    r = run("export", "--what", "sessions", "--format", "json")
    assert r.exit_code == 0 and json.loads(r.stdout) == []


def test_sets_csv_is_the_corrected_sets_frame(imported):
    r = imported("export")
    assert r.exit_code == 0, r.output
    data = rows(r.stdout)
    assert list(data[0]) == metrics.SET_COLUMNS
    assert len(data) == 6
    # B was merged into A, so its sets belong to A's session.
    lat = [d for d in data if d["exercise"] == "Lat Pulldown (Cable)"]
    assert {d["session"] for d in lat} == {"2026-06-02 14:00:00"}
    assert {d["workout"] for d in lat} == {"2026-06-02 14:06:00"}
    assert lat[0]["weight"] == "100.0" and lat[0]["rpe"] == ""


def test_raw_ignores_corrections(imported):
    data = rows(imported("export", "--raw").stdout)
    lat = [d for d in data if d["exercise"] == "Lat Pulldown (Cable)"]
    assert {d["session"] for d in lat} == {"2026-06-02 14:06:00"}


def test_value_fix_shows_in_export_but_not_raw(imported):
    r = imported("fix", "2026-06-04T13:00", "bench press (barbell)", "--set", "1", "--weight", "165")
    assert r.exit_code == 0, r.output
    fixed = [d for d in rows(imported("export").stdout) if d["session"].startswith("2026-06-04")]
    raw = [d for d in rows(imported("export", "--raw").stdout) if d["session"].startswith("2026-06-04")]
    assert (fixed[0]["weight"], fixed[0]["corrected"]) == ("165.0", "True")
    assert (raw[0]["weight"], raw[0]["corrected"]) == ("155.0", "False")


def test_sessions_json(imported):
    r = imported("export", "--what", "sessions", "--format", "json")
    assert r.exit_code == 0, r.output
    sessions = json.loads(r.stdout)
    assert [list(s) for s in sessions] == [SESSION_COLUMNS] * 2
    first, second = sessions
    assert first["session"] == "2026-06-02T14:00:00"
    assert (first["merged"], first["workouts"], first["exercises"], first["sets"]) == (True, 2, 2, 4)
    assert first["exercise_names"] == "Bench Press (Barbell); Lat Pulldown (Cable)"
    assert first["volume"] == 135 * 8 + 135 * 6 + 100 * 10 + 100 * 8
    assert first["duration_min"] == 53.0 and first["duration_trusted"] is True
    assert (second["merged"], second["name"]) == (False, "Push 1")


def test_since_and_until_filter_by_session_date(imported):
    only_c = rows(imported("export", "--since", "2026-06-03").stdout)
    assert {d["session"] for d in only_c} == {"2026-06-04 13:00:00"}
    only_a = rows(imported("export", "--what", "sessions", "--until", "2026-06-02").stdout)
    assert [d["session"] for d in only_a] == ["2026-06-02 14:00:00"]


def test_exercises_include_bests(imported):
    data = {d["exercise"]: d for d in json.loads(imported("export", "-w", "exercises", "-f", "json").stdout)}
    bench = data["Bench Press (Barbell)"]
    assert (bench["sessions"], bench["sets"], bench["heaviest"]) == (2, 4, 155.0)
    assert bench["e1rm"] == pytest.approx(155 * (1 + 8 / 30), abs=1e-3)
    assert bench["first"] == "2026-06-02T14:00:00"


def test_prs_are_computed_on_full_history_then_filtered(imported):
    every = json.loads(imported("export", "-w", "prs", "-f", "json").stdout)
    assert {e["exercise"] for e in every} == {"Bench Press (Barbell)"}
    assert all(e["session"] == "2026-06-04T13:00:00" for e in every)
    # The PR on 06-04 is still a PR (vs. 06-02) when exporting only from 06-03.
    assert json.loads(imported("export", "-w", "prs", "-f", "json", "--since", "2026-06-03").stdout) == every
    assert json.loads(imported("export", "-w", "prs", "-f", "json", "--until", "2026-06-03").stdout) == []


def test_corrections_csv_and_json(imported):
    data = rows(imported("export", "-w", "corrections").stdout)
    assert list(data[0]) == CORRECTION_COLUMNS
    [merge] = data
    assert merge["type"] == "workout_merge" and json.loads(merge["after"]) == {"into": "2026-06-02T14:00:00"}
    [c] = json.loads(imported("export", "-w", "corrections", "-f", "json").stdout)
    assert c["target"] == {"workout": "2026-06-02T14:06:00"} and c["status"] == "active"


def test_out_writes_file_and_reports_on_stderr(imported, tmp_path):
    out = tmp_path / "sub" / "sets.json"
    r = imported("export", "-f", "json", "--out", out)
    assert r.exit_code == 0, r.output
    assert r.stdout == ""
    assert "Wrote 6 sets" in r.output
    assert len(json.loads(out.read_text())) == 6


def test_out_to_unwritable_place_fails_cleanly(imported, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    r = imported("export", "--out", blocker / "x.csv")
    assert r.exit_code == 1 and "can't write" in r.output


@pytest.mark.parametrize("args", [("--what", "bogus"), ("--format", "xml"), ("--since", "June"),
                                  ("--formula", "nope")])
def test_bad_options_rejected(imported, args):
    r = imported("export", *args)
    assert r.exit_code != 0


@pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not present")
def test_real_export_round_trips(run):
    assert run("import", REAL_EXPORT).exit_code == 0
    for what in ("sets", "sessions", "exercises", "prs", "corrections"):
        r = run("export", "-w", what, "-f", "json")
        assert r.exit_code == 0, (what, r.output)
        assert json.loads(r.stdout)
    assert len(json.loads(run("export", "-w", "sessions", "-f", "json").stdout)) == 103
