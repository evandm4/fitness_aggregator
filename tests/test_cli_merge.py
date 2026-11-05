import pytest
from typer.testing import CliRunner

from fitness_aggregator.cli import app
from helpers import REAL_EXPORT, workout_rows, write_export

OLD = [workout_rows(f"2026-08-{d:02d} 10:00:00", exercises=(("Hip Abductor (Machine)", ((w, 14),)),))
       for d, w in ((4, 145), (8, 150), (12, 160))]
NEW = [workout_rows(f"2026-08-{d:02d} 10:00:00", exercises=(("Hip Abductor 2 (Machine)", ((205, r),)),))
       for d, r in ((22, 12), (25, 14), (28, 14))]
ROWS = [workout_rows("2026-08-30 10:00:00", exercises=(("Row (Machine)", ((100, 10),)),
                                                        ("Row (Machine 2)", ((90, 10),)),
                                                        ("Row (Machine 3)", ((80, 10),)))),
        workout_rows("2026-08-31 10:00:00", exercises=(("Plank", ()),))]
PLANKS = ('2026-09-01 10:00:00,"W",1h,"Plank",1,0,0.0,0,60.0,"","",\n'
          '2026-09-02 10:00:00,"W",1h,"Plank 2",1,0,0.0,0,90.0,"","",\n')
A, B = "Hip Abductor (Machine)", "Hip Abductor 2 (Machine)"


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    def _run(*args, input=None):
        return runner.invoke(app, ["--db", str(tmp_path / "lifts.db"), "--config", str(tmp_path / "s.toml"),
                                   *map(str, args)], input=input)

    r = _run("import", write_export(tmp_path / "e.csv", *OLD, *NEW, *ROWS, PLANKS))
    assert r.exit_code == 0, r.output
    assert "3 groups of similar exercises to review" in r.output
    return _run


def test_review_list(run):
    r = run("merge", "review", "--list")
    assert "3 groups of similar exercises." in r.output
    assert "[1] Hip Abductor (Machine)  3 sessions, 2026-08-04 → 2026-08-12, last top set 160 lb × 14 on 2026-08-12" \
        in r.output
    assert "[1] Plank  1 session on 2026-09-01, top set 60 s" in r.output
    assert "Compared top sets:" in r.output and "Hip Abductor (Machine): 2026-08-04 145 lb × 14" in r.output
    assert f"You switched from {A} to {B} on 2026-08-22 (no overlap)." in r.output
    assert "Suggested: -50 lb on Hip Abductor 2 (Machine) (or ×0.75)." in r.output


def test_review_combine_with_suggested_offset(run):
    # group order: Hip Abductor, Plank, Row
    r = run("merge", "review", input="o\n\n\nq\n")
    assert "[o] combine with weight offset" in r.output
    assert f"Recorded correction #1: {B} combined into {A} with -50 lb offset" in r.output
    r = run("history", A)
    assert "155 lb × 12 (205 − 50)" in r.output and "† recorded as Hip Abductor 2 (Machine)" in r.output
    assert "Combined" in run("merge", "list").output


def test_review_custom_factor_and_kept_name(run):
    r = run("merge", "review", input="o\n2\nx1.2\nq\n")
    assert f"{A} combined into {B} with ×1.2 factor" in r.output


def test_review_bad_adjustment_reprompts(run):
    r = run("merge", "review", input="o\n\nabc\n-40\nq\n")
    assert "Enter a number of lb" in r.output and "-40 lb offset" in r.output


def test_review_keep_separate_skip_preview(run):
    r = run("merge", "review", input="p\nk\ns\nq\n")
    assert "Combined as Hip Abductor (Machine)" in r.output and "155 lb × 12" in r.output
    assert "Kept separate" in r.output and "Skipped" in r.output
    r = run("merge", "review", "--list")
    assert "2 groups" in r.output and "Hip Abductor" not in r.output
    assert "≠" in run("merge", "list").output


def test_timed_exercises_have_no_offset_option(run):
    r = run("merge", "review", input="s\nc\n\nq\n")
    assert "[o] combine" not in r.output.split("(2/3)")[1].split("(3/3)")[0]
    assert "Plank 2 combined into Plank as-is" in r.output


def test_review_three_way_partial_combine(run):
    r = run("merge", "review", input="s\ns\nc\n1,2\n1\nq\n")
    assert "Row (Machine 2) combined into Row (Machine) as-is" in r.output
    assert "Kept Row (Machine 3) separate from Row (Machine)." in r.output
    assert "No similar exercises" not in run("merge", "review", "--list").output  # hip + plank still open
    out = run("merge", "review", "--list").output
    assert "Row" not in out


def test_add_separate_forget_undo(run):
    r = run("merge", "add", "abductor 2", "--into", "hip abductor (machine)", "--offset", "-50")
    assert r.exit_code == 0 and "with -50 lb offset" in r.output
    r = run("merge", "add", "abductor 2", "--into", "hip abductor (machine)", "--offset", "1", "--factor", "2")
    assert r.exit_code == 1 and "not both" in r.output
    assert run("merge", "undo", "abductor 2").output.startswith("Reverted #1")
    assert run("merge", "undo", "abductor 2").exit_code == 1
    r = run("merge", "separate", "Row (Machine)", "Row (Machine 2)", "Row (Machine 3)")
    assert "(3 pairs)" in r.output
    assert run("merge", "separate", "Plank", "Plank").exit_code == 1
    assert "Forgot" in run("merge", "forget", "Row (Machine)", "Row (Machine 3)").output
    assert run("merge", "forget", "Row (Machine)", "Row (Machine 3)").exit_code == 1


def test_show_and_exercises_reflect_merge(run):
    run("merge", "add", B, "--into", A, "--offset", "-50")
    r = run("show", "2026-08-22")
    assert "(recorded as Hip Abductor 2 (Machine), -50 lb)" in r.output
    assert "was 205 lb × 12" in r.output and "combined into Hip Abductor (Machine) with -50 lb offset" in r.output
    r = run("exercises", "-s", "abductor")
    assert "Hip Abductor (Machine) includes Hip Abductor 2 (Machine) (-50 lb)" in r.output
    assert "is combined into" in run("history", "abductor 2").output
    assert "exercise_merge" in run("corrections").output


def test_raw_ignores_merges(run):
    run("merge", "add", B, "--into", A)
    assert B in run("exercises", "--raw").output


@pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not present")
def test_real_export_review(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    db = str(tmp_path / "real.db")
    def invoke(*a, **k):
        return CliRunner().invoke(app, ["--db", db, *a], **k)
    assert "11 groups of similar exercises to review" in invoke("import", str(REAL_EXPORT)).output
    r = invoke("merge", "review", input="s\no\n\n\nq\n")
    assert "Hip Abductor 2 (Machine) combined into Hip Abductor (Machine) with -50 lb offset" in r.output
    assert "21 sessions" in invoke("history", "Hip Abductor (Machine)").output
