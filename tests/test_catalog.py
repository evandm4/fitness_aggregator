from datetime import datetime

from fitness_aggregator.bodyweight import Bodyweight
from fitness_aggregator.catalog import ASSISTED, LOADED, TIMED, UNLOADED, Catalog
from fitness_aggregator.importers import parse_strong_text
from helpers import export_text, workout_rows


def catalog_for(*rows, overrides=None):
    return Catalog.build(parse_strong_text(export_text(*rows)).workouts, overrides)


def test_kind_inference(tmp_path):
    timed = '2026-06-02 10:00:00,"W",1h,"Plank",1,0,0.0,0,60.0,"","",\n'
    cat = catalog_for(
        workout_rows("2026-06-02 10:00:00", exercises=(
            ("Pull Up (Assisted)", ((60, 8),)),
            ("Supported Leg Raise", ((0, 12),)),
            ("Bench Press (Barbell)", ((135, 8),)),
        )),
        timed,
        overrides=tmp_path / "missing.toml",
    )
    assert cat.kind("Pull Up (Assisted)") == ASSISTED
    assert cat.kind("Plank") == TIMED
    assert cat.kind("Supported Leg Raise") == UNLOADED
    assert cat.kind("Bench Press (Barbell)") == LOADED
    assert cat.kind("Never Seen") == LOADED


def test_warmups_ignored_for_inference(tmp_path):
    rows = ('2026-06-02 10:00:00,"W",1h,"Dip",W,20,10.0,0,0.0,"","",\n'
            '2026-06-02 10:00:00,"W",1h,"Dip",1,0,10.0,0,0.0,"","",\n')
    assert catalog_for(rows, overrides=tmp_path / "x.toml").kind("Dip") == UNLOADED


def test_overrides(tmp_path):
    path = tmp_path / "exercises.toml"
    path.write_text('[exercise."Bench Press (Barbell)"]\nkind = "timed"\n[exercise."Other"]\nkind = "bogus"\n')
    cat = catalog_for(workout_rows("2026-06-02 10:00:00"), overrides=path)
    assert cat.kind("Bench Press (Barbell)") == TIMED
    assert cat.kind("Other") == LOADED


def test_bodyweight_placeholder_and_entries():
    bw = Bodyweight(180)
    assert bw.uses_placeholder and bw.at(datetime(2026, 1, 1)) == 180
    bw = Bodyweight(180, [(datetime(2026, 6, 1), 175.0), (datetime(2026, 8, 1), 172.0)])
    assert not bw.uses_placeholder
    assert bw.at(datetime(2026, 5, 1)) == 175.0   # before first entry → first entry
    assert bw.at(datetime(2026, 7, 1)) == 175.0
    assert bw.at(datetime(2026, 8, 1)) == 172.0
    assert bw.at(datetime(2026, 12, 1)) == 172.0
