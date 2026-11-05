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


# --- bad files and values (hardening) -------------------------------------------

@pytest.mark.parametrize("value", ["inf", "nan", "-inf", "1e300", "0", "-5", "abc"])
def test_set_value_rejects_bad_numbers_and_leaves_file_alone(tmp_path, value):
    path = tmp_path / "settings.toml"
    path.write_text("merge_gap_minutes = 60\n")
    with pytest.raises(ValueError):
        s.set_value("merge_gap_minutes", value, path)
    assert path.read_text() == "merge_gap_minutes = 60\n"


def test_set_value_keeps_full_precision(tmp_path):
    path = tmp_path / "settings.toml"
    s.set_value("bodyweight_lb", "1234.567", path)
    s.set_value("merge_gap_minutes", "45.0", path)
    assert path.read_text() == "bodyweight_lb = 1234.567\nmerge_gap_minutes = 45\n"
    assert s.load(path).bodyweight_lb == 1234.567


@pytest.mark.parametrize("text, message", [
    ("merge_gap_minutes = [\n", "not valid TOML"),
    ('merge_gap_minutes = "abc"\n', "merge_gap_minutes must be a number"),
    ("merge_gap_minutes = inf\n", "merge_gap_minutes must be a number"),
    ("merge_gap_minutes = true\n", "merge_gap_minutes must be a number"),
    ("suspect_duration_hours = -1\n", "greater than 0"),
])
def test_bad_settings_file_gives_settings_error_naming_the_file(tmp_path, text, message):
    path = tmp_path / "settings.toml"
    path.write_text(text)
    with pytest.raises(s.SettingsError, match=message) as info:
        s.load(path)
    assert str(path) in str(info.value)


def test_unwritable_settings_location_gives_settings_error(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("")  # a file where a directory is needed: fails even as root
    with pytest.raises(s.SettingsError, match="can't write settings file"):
        s.set_value("bodyweight_lb", "170", blocker / "settings.toml")


def test_settings_error_is_a_value_error():
    # The CLI's `config set` reports ValueErrors as a one-line error.
    assert issubclass(s.SettingsError, ValueError)
