"""User data that looks like Rich markup (``[/x]``, ``[green]``, ``[bold]``) is printed literally, never parsed."""

import pytest
from typer.testing import CliRunner

from fitness_aggregator.cli import app
from helpers import HEADER

EX = "Curl [/x]"                  # an unmatched closing tag: Rich raises MarkupError on it
EX2 = "Curl [/x] 2"               # similar name, so `merge review` offers it
BENCH = "Bench [green] Press"     # a tag Rich would silently swallow
WORKOUT = "Push [bold]day"
SET_NOTE = "felt [green]easy"
WORKOUT_NOTE = "gym [bold]busy[/bold]"


def _row(start, name, ex, order, weight, reps, note="", wnote="", duration="1h"):
    return f'{start},"{name}",{duration},"{ex}",{order},{weight},{reps},0,0.0,"{note}","{wnote}",\n'


def _export(tmp_path):
    rows = []
    days = ["2026-06-02", "2026-06-04", "2026-06-06", "2026-06-08", "2026-06-10", "2026-06-12"]
    for i, day in enumerate(days):
        start = f"{day} 13:00:00"
        rows.append(_row(start, WORKOUT, BENCH, 1, 135 + 5 * i, 8, SET_NOTE, WORKOUT_NOTE))
        rows.append(_row(start, WORKOUT, BENCH, 2, 135 + 5 * i, 6, "", WORKOUT_NOTE))
        rows.append(_row(start, WORKOUT, EX if i < 4 else EX2, 1, 30 + 2 * i, 10, "", WORKOUT_NOTE))
    # A swapped set (8 lb × 140 reps) for `fitness check`.
    start = "2026-06-14 13:00:00"
    rows.append(_row(start, WORKOUT, BENCH, 1, 8, 140, SET_NOTE, WORKOUT_NOTE))
    rows.append(_row(start, WORKOUT, EX2, 1, 40, 10, "", WORKOUT_NOTE))
    # Two parts of one session, so a workout merge correction (whose reason names the workout) exists.
    rows.append(_row("2026-06-16 13:00:00", WORKOUT, EX2, 1, 40, 10, "", "", "3m"))
    rows.append(_row("2026-06-16 13:05:00", WORKOUT, EX2, 1, 42, 10, "", "", "40m"))
    path = tmp_path / "markup.csv"
    path.write_text(HEADER + "".join(rows))
    return path


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "250")
    monkeypatch.chdir(tmp_path)
    db, config = tmp_path / "lifts.db", tmp_path / "settings.toml"
    runner = CliRunner()

    def _run(*args, input=None):
        r = runner.invoke(app, ["--db", str(db), "--config", str(config), *map(str, args)], input=input)
        assert r.exception is None or isinstance(r.exception, SystemExit), (args, r.output, r.exception)
        assert r.exit_code == 0, (args, r.output)
        return r.output

    return _run


def test_markup_like_names_and_notes_are_printed_literally(run, tmp_path):
    out = run("import", _export(tmp_path))
    assert "Imported." in out

    out = run("workouts")
    assert WORKOUT in out
    out = run("show", "2026-06-02")
    assert WORKOUT in out and BENCH in out and SET_NOTE in out and WORKOUT_NOTE in out
    out = run("show", "last")
    assert WORKOUT in out and EX2 in out
    assert BENCH in run("history", "bench")
    assert EX in run("history", EX)
    out = run("prs")
    assert EX in out and BENCH in out
    assert BENCH in run("prs", "bench")
    assert BENCH in run("prs", "--recent", "3650d")
    assert WORKOUT in run("stats")
    out = run("exercises")
    assert EX in out and BENCH in out
    out = run("exercises", "--unmapped")
    assert EX in out or BENCH in out
    out = run("muscles")
    assert "Muscle" in out

    out = run("check", "--list")
    assert BENCH in out and WORKOUT in out
    out = run("fix", "2026-06-14", "bench", "--set", "1", "--swap-weight-reps")
    assert BENCH in out
    out = run("corrections")
    assert BENCH in out and "Merged 2026-06-16 13:05" in out
    out = run("corrections", "--exercise", BENCH)
    assert BENCH in out

    out = run("merge", "review", "--list")
    assert EX in out and EX2 in out
    out = run("merge", "add", EX2, "--into", EX)
    assert EX2 in out and EX in out
    out = run("merge", "list")
    assert EX2 in out and EX in out
    assert EX in run("history", EX)  # now with a "recorded as" footnote
    out = run("show", "last")
    assert f"recorded as {EX2}" in out
    out = run("merge", "undo", EX2)
    assert EX2 in out
    out = run("merge", "separate", EX, EX2)
    assert EX in out and EX2 in out
    out = run("merge", "forget", EX, EX2)
    assert EX in out and EX2 in out

    assert BENCH in run("export", "--what", "exercises")
    assert WORKOUT in run("export", "--what", "sessions", "--format", "json")
    for args in (["progress", EX], ["rep-prs", "bench"], ["volume"], ["calendar"], ["duration"],
                 ["frequency"], ["dashboard"], ["sets-per-muscle"]):
        out = run("plot", *args, "--out", tmp_path / f"{args[0]}.png")
        assert "Saved" in out


def test_markup_in_interactive_prompts(run, tmp_path):
    run("import", _export(tmp_path))
    out = run("check", input="k\n")
    assert BENCH in out and "[s] swap" in out
    out = run("merge", "review", input="k\n")
    assert EX in out and EX2 in out and "[c] combine" in out


def test_markup_in_unknown_name_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "250")
    runner = CliRunner()
    base = ["--db", str(tmp_path / "lifts.db"), "--config", str(tmp_path / "s.toml")]
    runner.invoke(app, [*base, "import", str(_export(tmp_path))])
    for args in (["history", "nope [/x]"], ["show", "[/x]"], ["fix", "[/x]", "bench", "--reps", "3"],
                 ["workouts", "unmerge", "[/x]"]):
        r = runner.invoke(app, [*base, *args])
        assert r.exit_code == 1 and (r.exception is None or isinstance(r.exception, SystemExit)), (args, r.output)
        assert "[/x]" in r.output, (args, r.output)
