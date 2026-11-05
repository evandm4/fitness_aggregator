"""Bad input, broken files and edge cases end in one clear line, never a traceback."""

import pytest
from typer.testing import CliRunner

from fitness_aggregator import __version__
from fitness_aggregator.cli import app
from fitness_aggregator.store import Store, StoreError
from helpers import workout_rows, write_export

A = workout_rows("2026-06-02 14:00:00", "1h", name="Push")
B = workout_rows("2026-06-04 13:00:00", "1h", name="Push")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "lifts.db", tmp_path / "settings.toml"


@pytest.fixture
def run(paths):
    db, config = paths
    runner = CliRunner()

    def _run(*args, input=None):
        return runner.invoke(app, ["--db", str(db), "--config", str(config), *map(str, args)], input=input)

    return _run


def _clean(r, code=1):
    """Exit code as expected, no Python exception, and (for errors) one 'Error:' line."""
    assert r.exception is None or isinstance(r.exception, SystemExit), r.exception
    assert r.exit_code == code, r.output
    assert "Traceback" not in r.output


def test_version(run):
    r = run("--version")
    _clean(r, 0)
    assert r.output.strip() == f"fitness_aggregator {__version__}"


@pytest.mark.parametrize("args", [["workouts"], ["stats"], ["import", "e.csv"], ["corrections"], ["merge", "list"]])
def test_corrupt_database_is_one_line(run, paths, tmp_path, args):
    write_export(tmp_path / "e.csv", A)
    paths[0].write_bytes(b"this is not sqlite at all " * 40)
    r = run(*args)
    _clean(r)
    assert r.output.startswith("Error: ")
    assert "is not a fitness_aggregator database, or it is damaged" in r.output
    assert r.output.count("\n") <= 2


@pytest.mark.parametrize("text, message", [
    ("merge_gap_minutes = [\n", "is not valid TOML"),
    ('merge_gap_minutes = "soon"\n', "merge_gap_minutes must be a number"),
])
def test_bad_settings_file_is_one_line(run, paths, tmp_path, text, message):
    run("import", write_export(tmp_path / "e.csv", A))
    paths[1].write_text(text)
    for args in (["stats"], ["config", "show"], ["config", "set", "bodyweight_lb", "170"]):
        r = run(*args)
        _clean(r)
        assert "Error: settings file" in r.output and message in r.output


def test_store_error_during_a_write_is_one_line(run, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise StoreError("can't write to the database x.db: it is read-only (check the file's permissions)")

    monkeypatch.setattr(Store, "apply_import", boom)
    r = run("import", write_export(tmp_path / "e.csv", A))
    _clean(r)
    assert "Error: can't write to the database x.db: it is read-only" in r.output


def test_missing_files_and_directories(run, tmp_path):
    for args in (["import", "nope.csv"], ["import-health", "nope.zip"], ["import", tmp_path]):
        r = run(*args)
        _clean(r, 2)
        assert "does not exist" in r.output or "is a directory" in r.output


def test_empty_database(run):
    for args in (["workouts"], ["show"], ["history", "x"], ["prs"], ["stats"], ["exercises"], ["check"],
                 ["muscles"], ["merge", "review"], ["plot", "volume"], ["plot", "sets-per-muscle"]):
        r = run(*args)
        _clean(r, 0)
        assert "No workouts yet" in r.output, args
    for args in (["fix", "2026-06-02", "bench", "--reps", "3"], ["merge", "add", "a", "--into", "b"],
                 ["merge", "undo", "a"], ["merge", "separate", "a", "b"], ["merge", "forget", "a", "b"]):
        r = run(*args)
        _clean(r)
        assert "Error: no workouts yet. Run fitness import FILE first." in r.output, args


@pytest.mark.parametrize("args, option", [
    (["workouts", "--since", "banana"], "--since"),
    (["history", "bench", "--since", "banana"], "--since"),
    (["stats", "--until", "banana"], "--until"),
    (["corrections", "--since", "banana"], "--since"),
    (["muscles", "--since", "banana"], "--since"),
    (["export", "--until", "banana"], "--until"),
    (["plot", "volume", "--since", "banana"], "--since"),
    (["plot", "progress", "bench", "--since", "banana"], "--since"),
    (["plot", "sets-per-muscle", "--since", "banana"], "--since"),
])
def test_bad_dates_are_usage_errors_naming_the_option(run, tmp_path, args, option):
    run("import", write_export(tmp_path / "e.csv", A, B))
    r = run(*args)
    _clean(r, 2)
    assert f"Invalid value for {option}" in r.output and "banana" in r.output


def test_last_zero_shows_everything_and_negative_is_rejected(run, tmp_path):
    run("import", write_export(tmp_path / "e.csv", A, B))
    r = run("workouts", "--last", "0")
    _clean(r, 0)
    assert "2026-06-02" in r.output and "2026-06-04" in r.output
    for args in (["workouts", "--last", "-1"], ["stats", "-n", "-2"], ["muscles", "--last", "-1"],
                 ["bodyweight", "--last", "-1"], ["plot", "calendar", "--weeks", "0"]):
        r = run(*args)
        _clean(r, 2)
        assert "not in the range" in r.output, args


def test_by_year_is_rejected_everywhere(run, tmp_path):
    run("import", write_export(tmp_path / "e.csv", A, B))
    for args in (["stats"], ["muscles"], ["plot", "volume"], ["plot", "sets-per-muscle"]):
        r = run(*args, "--by", "year")
        _clean(r)
        assert "Error: --by must be week or month." in r.output, args


def test_unknown_names(run, tmp_path):
    run("import", write_export(tmp_path / "e.csv", A, B))
    for args in (["history", "zzz"], ["prs", "zzz"], ["plot", "progress", "zzz"], ["plot", "rep-prs", "zzz"]):
        r = run(*args)
        _clean(r)
        assert "Error: no exercise matches 'zzz'. See fitness exercises." in r.output
    r = run("fix", "2026-06-02", "zzz", "--reps", "3")
    _clean(r)
    assert "no exercise in that workout matches 'zzz'. It has: Bench Press (Barbell)." in r.output
    r = run("show", "1999-01-01")
    _clean(r)
    assert "no session matches '1999-01-01'" in r.output


def test_fix_rejects_negative_and_nan_values(run, tmp_path):
    run("import", write_export(tmp_path / "e.csv", A))
    for value in ("-3", "nan", "inf"):
        r = run("fix", "2026-06-02", "bench", "--reps", value)
        _clean(r)
        assert "--reps must be a number of 0 or more" in r.output
    r = run("fix", "2026-06-02", "bench", "--occurrence", "0", "--reps", "3")
    _clean(r, 2)


def test_lower_layer_errors_get_a_full_stop(run, tmp_path):
    run("import", write_export(tmp_path / "e.csv", A))
    r = run("merge", "add", "bench", "--into", "bench")
    _clean(r)
    assert r.output.strip().endswith(".")


def test_out_to_missing_directory_is_created(run, tmp_path):
    run("import", write_export(tmp_path / "e.csv", A, B))
    r = run("plot", "volume", "--out", tmp_path / "new" / "dir" / "v.png")
    _clean(r, 0)
    assert (tmp_path / "new" / "dir" / "v.png").exists()
    r = run("export", "--out", tmp_path / "other" / "dir" / "sets.csv")
    _clean(r, 0)
    assert (tmp_path / "other" / "dir" / "sets.csv").exists()


def test_out_that_cannot_be_written_is_one_line(run, tmp_path):
    run("import", write_export(tmp_path / "e.csv", A, B))
    blocker = tmp_path / "file"
    blocker.write_text("")
    r = run("plot", "volume", "--out", blocker / "v.png")
    _clean(r)
    assert r.output.startswith("Error: can't write ")
    r = run("export", "--out", blocker / "sets.csv")
    _clean(r)
    assert "Error: can't write " in r.output
