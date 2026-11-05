from datetime import date

import pytest
from typer.testing import CliRunner

from fitness_aggregator.cli import app
from helpers import REAL_EXPORT, workout_rows, write_export

TODAY = f"{date.today():%Y-%m-%d}"

EXPORT = [
    workout_rows("2026-08-04 10:00:00", "1h", "Push 1", (("Bench Press (Barbell)", ((135, 5), (125, 8))),)),
    workout_rows("2026-08-10 10:00:00", "1h", "Push 1", (("Bench Press (Barbell)", ((140, 5), (125, 10))),)),
    workout_rows("2026-08-12 10:00:00", "50m", "Pull 1", (("Pull Up (Assisted)", ((60, 8), (45, 6))),)),
    workout_rows("2026-08-04 18:00:00", "1h", "Legs", (("Hip Abductor (Machine)", ((145, 14),)),)),
    workout_rows("2026-08-08 18:00:00", "1h", "Legs", (("Hip Abductor (Machine)", ((150, 14),)),)),
    workout_rows("2026-08-22 18:00:00", "9h", "Legs", (("Hip Abductor 2 (Machine)", ((205, 12),)),)),
    workout_rows("2026-08-25 18:00:00", "1h", "Legs", (("Hip Abductor 2 (Machine)", ((205, 14),)),)),
    workout_rows("2026-09-20 10:00:00", "1h", "Push 1", (("Bench Press (Barbell)", ((150, 3), (130, 8))),)),
]


def _runner(tmp_path, monkeypatch, export):
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "lifts.db"
    runner = CliRunner()

    def _run(*args, input=None):
        return runner.invoke(app, ["--db", str(db), "--config", str(tmp_path / "s.toml"), *map(str, args)],
                             input=input)

    r = _run("import", export)
    assert r.exit_code == 0, r.output
    return _run


@pytest.fixture
def run(tmp_path, monkeypatch):
    return _runner(tmp_path, monkeypatch, write_export(tmp_path / "e.csv", *EXPORT))


def _saved(r, tmp_path, name):
    assert r.exit_code == 0, r.output
    path = tmp_path / "plots" / f"{name}_{TODAY}.png"
    assert "Saved" in r.output and path.exists() and path.stat().st_size > 5000, r.output
    assert path.read_bytes()[:4] == b"\x89PNG"
    return path


def test_default_paths(run, tmp_path):
    _saved(run("plot", "progress", "bench"), tmp_path, "progress_bench-press-barbell")
    _saved(run("plot", "progress", "bench", "pull up", "--metric", "top", "--trend"), tmp_path,
           "progress_bench-press-barbell-and-1-more")
    _saved(run("plot", "volume"), tmp_path, "volume-week")
    _saved(run("plot", "volume", "--by", "month", "--group", "none"), tmp_path, "volume-month")
    _saved(run("plot", "calendar"), tmp_path, "calendar")
    _saved(run("plot", "duration"), tmp_path, "duration")
    _saved(run("plot", "frequency", "--top", "2"), tmp_path, "frequency")
    _saved(run("plot", "rep-prs", "bench"), tmp_path, "rep-prs_bench-press-barbell")
    _saved(run("plot", "dashboard"), tmp_path, "dashboard")


@pytest.mark.parametrize("suffix,magic", [(".png", b"\x89PNG"), (".svg", b"<?xml"), (".pdf", b"%PDF")])
def test_out_formats(run, tmp_path, suffix, magic):
    out = tmp_path / "charts" / f"x{suffix}"
    r = run("plot", "calendar", "--out", out)
    assert r.exit_code == 0, r.output
    assert out.read_bytes()[: len(magic)] == magic


def test_bad_options(run):
    r = run("plot", "volume", "--out", "x.txt")
    assert r.exit_code == 1 and "--out must end in" in r.output
    r = run("plot", "progress", "bench", "--metric", "nope")
    assert r.exit_code == 1 and "unknown metric" in r.output
    assert run("plot", "volume", "--by", "year").exit_code == 1
    assert run("plot", "frequency", "--top", "0").exit_code == 1
    r = run("plot", "progress", "deadlift")
    assert r.exit_code == 1 and "no exercise matches" in r.output


def test_show_without_display_is_friendly(run, monkeypatch, tmp_path):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    r = run("plot", "duration", "--show")
    assert r.exit_code == 1 and "--show needs a display" in r.output
    assert not (tmp_path / "plots").exists()


def test_merge_and_placeholder_footnotes(run, tmp_path, monkeypatch):
    assert run("merge", "add", "hip abductor 2", "--into", "Hip Abductor (Machine)", "--offset", "-50").exit_code == 0
    from fitness_aggregator import cli_plots

    notes = []
    real = cli_plots.footnotes
    monkeypatch.setattr(cli_plots, "footnotes", lambda *a, **k: notes.extend(real(*a, **k)) or notes[:])
    _saved(run("plot", "progress", "hip abductor (machine)", "--metric", "top"), tmp_path,
           "progress_hip-abductor-machine")
    assert any("Hip Abductor 2 (Machine) → Hip Abductor (Machine), -50 lb offset" in n for n in notes)
    notes.clear()
    run("plot", "progress", "pull up", "--metric", "e1rm")
    assert any("placeholder bodyweight 180 lb" in n for n in notes)
    notes.clear()
    run("plot", "progress", "hip abductor (machine)", "--raw")
    assert notes == [] or not any("exercise merge" in n for n in notes)


def test_since_filters(run, tmp_path):
    r = run("plot", "progress", "bench", "--since", "2026-09-01", "--out", tmp_path / "p.png")
    assert r.exit_code == 0 and (tmp_path / "p.png").exists()
    r = run("plot", "volume", "--since", "not-a-date")
    assert r.exit_code != 0


def test_merge_review_preview_saves_chart(run, tmp_path):
    r = run("merge", "review", input="p\ns\nq\n")
    assert r.exit_code == 0, r.output
    path = tmp_path / "plots" / f"merge-preview_hip-abductor-2-machine_{TODAY}.png"
    assert "Chart preview saved to" in r.output and path.exists()


def test_no_workouts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(app, ["--db", str(tmp_path / "x.db"), "plot", "calendar"])
    assert r.exit_code == 0 and "No workouts yet" in r.output


@pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not present")
def test_real_export_every_chart(tmp_path, monkeypatch):
    run = _runner(tmp_path, monkeypatch, REAL_EXPORT)
    for args in (("progress", "leg extension (machine)", "preacher curl", "--trend"),
                 ("progress", "pull up", "--metric", "assistance"),
                 ("volume",), ("calendar",), ("duration",), ("frequency",),
                 ("rep-prs", "leg extension (machine)"), ("dashboard",)):
        r = run("plot", *args)
        assert r.exit_code == 0, (args, r.output)
    files = list((tmp_path / "plots").glob("*.png"))
    assert len(files) == 8
