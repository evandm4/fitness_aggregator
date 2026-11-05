import pytest

from liftlog import db, metrics
from liftlog.cli import main


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def test_e1rm():
    assert metrics.e1rm(100, 1) == 100
    assert metrics.e1rm(100, 5) == pytest.approx(116.667, rel=1e-3)


def test_prs_and_volume(conn):
    for day, w, r in [("2026-11-02", 100, 5), ("2026-11-04", 110, 3), ("2026-11-10", 90, 10)]:
        db.add_set(conn, db.LiftSet(day, "Squat", w, r))
    sets = db.get_sets(conn, "squat")
    pr = metrics.personal_records(sets)["squat"]
    assert pr["max_weight"].weight == 110
    assert pr["best_volume"].weight == 90
    assert metrics.weekly_volume(sets) == [("2026-W45", 500 + 330), ("2026-W46", 900)]


def test_csv_roundtrip_and_empty(conn, tmp_path):
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    assert db.import_csv(conn, empty) == 0
    db.add_set(conn, db.LiftSet("2026-11-03", "Bench Press", 80, 8, 8.5))
    out = tmp_path / "o.csv"
    assert db.export_csv(conn, out) == 1
    conn2 = db.connect(tmp_path / "t2.db")
    assert db.import_csv(conn2, out) == 1
    assert db.get_sets(conn2)[0].rpe == 8.5


def test_cli(tmp_path, capsys):
    d = str(tmp_path / "c.db")
    assert main(["--db", d, "add", "Deadlift", "180", "3", "--date", "2026-11-03"]) == 0
    assert main(["--db", d, "prs"]) == 0
    assert "deadlift" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["--db", d, "add", "squat", "100", "0"])
