import sqlite3
import zipfile
from datetime import date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fitness_aggregator import metrics
from fitness_aggregator.bodyweight import Bodyweight, DayValue, plan_changes
from fitness_aggregator.cli import app
from fitness_aggregator.importers.apple_health import HealthParseError, parse_apple_health
from fitness_aggregator.importers import parse_strong_text
from fitness_aggregator.store import SCHEMA_VERSION, Store
from helpers import workout_rows, write_export

FIXTURE = Path(__file__).parent / "fixtures" / "apple_health_export.xml"


def make_zip(tmp_path: Path, xml: Path = FIXTURE) -> Path:
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("apple_health_export/export_cda.xml", "<ClinicalDocument/>")
        zf.write(xml, "apple_health_export/export.xml")
    return path


def xml_with(tmp_path: Path, *records: tuple[str, str, str], name="export.xml") -> Path:
    body = "".join(
        f'<Record type="HKQuantityTypeIdentifierBodyMass" sourceName="Health" unit="{u}" '
        f'startDate="{d} 07:00:00 -0700" value="{v}"/>'
        for d, v, u in records
    )
    path = tmp_path / name
    path.write_text(f'<?xml version="1.0"?><HealthData locale="en_US">{body}</HealthData>')
    return path


# --- parsing ---------------------------------------------------------------------

def test_parse_xml_units_daily_means_and_counts():
    r = parse_apple_health(FIXTURE)
    s = r.stats
    assert s.records == 7 and s.invalid == 2 and s.other_records == 4
    assert dict(s.units) == {"lb": 3, "kg": 2}
    assert (s.first, s.last) == (date(2026, 6, 1), date(2026, 9, 10))
    days = r.by_day
    assert list(days) == [date(2026, 6, 1), date(2026, 7, 15), date(2026, 9, 10)]
    assert days[date(2026, 6, 1)].lb == 182.0 and days[date(2026, 6, 1)].readings == 2
    assert days[date(2026, 6, 1)].source == "Health, Smart Scale"
    assert days[date(2026, 7, 15)].lb == 176.4  # 80 kg
    assert days[date(2026, 9, 10)].lb == 173.5  # mean of 78.5 kg and 174 lb
    assert len(r.warnings) == 2
    assert any("isn't a number" in w for w in r.warnings)
    assert any("implausible" in w for w in r.warnings)


def test_parse_zip_matches_xml(tmp_path):
    assert parse_apple_health(make_zip(tmp_path)).days == parse_apple_health(FIXTURE).days


def test_day_uses_local_date_of_reading(tmp_path):
    # 23:30 at -0700 is the next day in UTC, but it's still the 1st where it was taken.
    path = tmp_path / "e.xml"
    path.write_text('<HealthData><Record type="HKQuantityTypeIdentifierBodyMass" unit="lb" '
                    'startDate="2026-06-01 23:30:00 -0700" value="180"/></HealthData>')
    assert parse_apple_health(path).days[0].day == date(2026, 6, 1)


def test_parse_errors(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<HealthData><Record")
    with pytest.raises(HealthParseError):
        parse_apple_health(bad)
    other = tmp_path / "other.xml"
    other.write_text("<Something/>")
    with pytest.raises(HealthParseError):
        parse_apple_health(other)
    empty_zip = tmp_path / "e.zip"
    with zipfile.ZipFile(empty_zip, "w") as zf:
        zf.writestr("readme.txt", "hi")
    with pytest.raises(HealthParseError):
        parse_apple_health(empty_zip)


# --- bodyweight lookups and plans -----------------------------------------------------

def test_plan_changes():
    stored = [DayValue(date(2026, 6, 1), 180.0), DayValue(date(2026, 6, 2), 181.0), DayValue(date(2026, 6, 3), 182.0)]
    incoming = [DayValue(date(2026, 6, 2), 181.0), DayValue(date(2026, 6, 3), 179.5), DayValue(date(2026, 6, 4), 179.0)]
    plan = plan_changes(stored, incoming)
    assert [d.day.day for d in plan.new] == [4]
    assert [(a.lb, b.lb) for a, b in plan.changed] == [(182.0, 179.5)]
    assert [d.day.day for d in plan.removed] == [1]
    assert plan.unchanged == 1 and plan.has_changes and not plan.is_first_import
    assert plan_changes([], incoming).is_first_import


def test_change_over():
    bw = Bodyweight(180, [(datetime(2026, 6, 1), 185.0), (datetime(2026, 7, 1), 182.0), (datetime(2026, 7, 31), 180.0)])
    assert bw.change_over(30) == -2.0
    assert bw.change_over(60) == -5.0
    assert bw.change_over(365) is None
    assert Bodyweight(180).change_over(30) is None


# --- store ----------------------------------------------------------------------

def test_store_replace_and_diff(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    assert store.bodyweight_entries() == []
    first = xml_with(tmp_path, ("2026-06-01", 180, "lb"), ("2026-06-02", 181, "lb"), name="a.xml")
    plan = store.plan_bodyweight_import(parse_apple_health(first).days, first)
    assert plan.is_first_import and len(plan.new) == 2
    assert store.apply_bodyweight_import(plan)
    assert store.bodyweight_entries() == [(datetime(2026, 6, 1), 180.0), (datetime(2026, 6, 2), 181.0)]

    second = xml_with(tmp_path, ("2026-06-02", 182, "lb"), ("2026-06-03", 80, "kg"), name="b.xml")
    plan = store.plan_bodyweight_import(parse_apple_health(second).days, second)
    assert (len(plan.new), len(plan.changed), len(plan.removed), plan.unchanged) == (1, 1, 1, 0)
    store.apply_bodyweight_import(plan)
    entries = store.bodyweight_entries()
    assert [d for d, _ in entries] == [datetime(2026, 6, 2), datetime(2026, 6, 3)]
    assert entries[0][1] == 182.0 and entries[1][1] == 176.4
    assert store.last_health_import()["days"] == 2

    again = store.plan_bodyweight_import(parse_apple_health(second).days, second)
    assert not again.has_changes and not store.apply_bodyweight_import(again)


def test_migration_v3_to_v4(tmp_path):
    path = tmp_path / "old.db"
    Store(path).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE bodyweight")
    conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    store = Store(path)
    assert SCHEMA_VERSION == 4
    assert store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
    assert store.bodyweight_entries() == []
    store.conn.execute("INSERT INTO bodyweight (date, lb, source) VALUES ('2026-06-01', 180, 'x')")
    assert store.bodyweight_entries() == [(datetime(2026, 6, 1), 180.0)]


def test_migration_from_v1(tmp_path):
    path = tmp_path / "v1.db"
    Store(path).close()
    conn = sqlite3.connect(path)
    for table in ("bodyweight", "separate_exercises", "dismissed_flags"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    store = Store(path)
    assert store.bodyweight_entries() == [] and store.separate_pairs() == set()


def test_assisted_load_uses_entries(tmp_path):
    workouts = parse_strong_text(
        "Date,Workout Name,Duration,Exercise Name,Set Order,Weight,Reps,Distance,Seconds,Notes,Workout Notes,RPE\n"
        + workout_rows("2026-07-01 10:00:00", exercises=(("Pull Up (Assisted)", ((50, 8),)),))
    ).workouts
    from fitness_aggregator.catalog import Catalog
    from fitness_aggregator.sessions import build_sessions

    cat = Catalog.build(workouts, tmp_path / "none.toml")
    placeholder = metrics.sets_frame(build_sessions(workouts), cat, Bodyweight(180))
    real = metrics.sets_frame(build_sessions(workouts), cat, Bodyweight(180, [(datetime(2026, 6, 1), 200.0)]))
    assert placeholder["load"].iloc[0] == 130 and real["load"].iloc[0] == 150


# --- CLI ------------------------------------------------------------------------

@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "lifts.db"
    runner = CliRunner()

    def _run(*args, input=None):
        return runner.invoke(app, ["--db", str(db), "--config", str(tmp_path / "s.toml"), *map(str, args)],
                             input=input)
    return _run


def test_cli_bodyweight_placeholder_then_import(run, tmp_path):
    r = run("bodyweight")
    assert r.exit_code == 0 and "placeholder 180 lb" in r.output

    r = run("import-health", make_zip(tmp_path))
    assert r.exit_code == 0, r.output
    assert "Read 7 body-mass records (2 in kg, 3 in lb) on 3 days, 2026-06-01 → 2026-09-10" in r.output
    assert "4 other Health records ignored" in r.output
    assert "2 records skipped" in r.output
    assert "First Health import: 3 days" in r.output
    assert "Imported." in r.output

    r = run("import-health", make_zip(tmp_path))
    assert "No changes" in r.output

    r = run("bodyweight")
    assert r.exit_code == 0, r.output
    assert "3 days of bodyweight" in r.output
    assert "Latest 173.5 lb" in r.output and "highest 182.0 lb (2026-06-01)" in r.output
    assert "Health, Smart Scale" in r.output
    assert "placeholder (180 lb) is not in use" in r.output


def test_cli_import_health_removals_need_confirmation(run, tmp_path):
    run("import-health", xml_with(tmp_path, ("2026-06-01", 180, "lb"), ("2026-06-02", 181, "lb"), name="a.xml"))
    b = xml_with(tmp_path, ("2026-06-02", 182, "lb"), name="b.xml")
    r = run("import-health", b, input="n\n")
    assert r.exit_code == 1 and "0 new, 1 changed, 1 removed" in r.output and "nothing changed" in r.output
    r = run("import-health", b, "--yes")
    assert r.exit_code == 0 and "181.0 lb → 182.0 lb" in r.output


def test_cli_import_health_bad_file(run, tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("not xml")
    r = run("import-health", bad)
    assert r.exit_code == 1 and "Error" in r.output


def test_cli_assisted_note_and_load_switch_to_health(run, tmp_path):
    export = write_export(tmp_path / "e.csv", workout_rows(
        "2026-07-01 10:00:00", name="Pull 1", exercises=(("Pull Up (Assisted)", ((50, 8),)),)))
    assert run("import", export).exit_code == 0
    r = run("history", "pull up")
    assert "(= 130 lb)" in r.output and "placeholder bodyweight 180 lb" in r.output

    run("import-health", xml_with(tmp_path, ("2026-06-01", 200, "lb")))
    r = run("history", "pull up")
    assert r.exit_code == 0, r.output
    assert "(= 150 lb)" in r.output
    assert "Assisted-lift loads use your logged bodyweight (Apple Health)" in r.output
    assert "placeholder" not in r.output
