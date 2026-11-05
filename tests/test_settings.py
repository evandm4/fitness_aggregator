import pytest

from fitness_aggregator import settings as s


def test_defaults_when_file_missing(tmp_path):
    loaded = s.load(tmp_path / "nope.toml")
    assert loaded == s.Settings()
    assert loaded.bodyweight_lb == 180


def test_repo_settings_file_loads():
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent / "config" / "settings.toml"
    assert s.load(repo).bodyweight_lb == 180


def test_set_value_preserves_comments(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text("# my comment\nbodyweight_lb = 180\n\n# gap\nmerge_gap_minutes = 60\n")
    old, new = s.set_value("merge-gap-minutes", "45", path)
    assert (old.merge_gap_minutes, new.merge_gap_minutes) == (60, 45)
    assert path.read_text() == "# my comment\nbodyweight_lb = 180\n\n# gap\nmerge_gap_minutes = 45\n"


def test_set_value_appends_missing_key_and_creates_file(tmp_path):
    path = tmp_path / "sub" / "settings.toml"
    s.set_value("bodyweight_lb", "175.5", path)
    assert path.read_text() == "bodyweight_lb = 175.5\n"
    assert s.load(path).bodyweight_lb == 175.5


@pytest.mark.parametrize("key, value, error", [
    ("height", "70", KeyError),
    ("bodyweight_lb", "heavy", ValueError),
    ("merge_gap_minutes", "0", ValueError),
])
def test_set_value_rejects_bad_input(tmp_path, key, value, error):
    with pytest.raises(error):
        s.set_value(key, value, tmp_path / "settings.toml")


def test_unknown_keys_in_file_are_ignored(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text("future_option = 1\nbodyweight_lb = 170\n")
    assert s.load(path).bodyweight_lb == 170


def test_resolve_path_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(s.ENV_VAR, raising=False)
    assert s.resolve_path() == s.DEFAULT_DIR / "settings.toml"
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.toml").write_text("")
    assert s.resolve_path() == tmp_path / "config" / "settings.toml"
    monkeypatch.setenv(s.ENV_VAR, str(tmp_path / "env.toml"))
    assert s.resolve_path() == tmp_path / "env.toml"
    assert s.resolve_path(tmp_path / "x.toml") == tmp_path / "x.toml"
