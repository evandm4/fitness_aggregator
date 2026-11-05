"""User settings, stored in a small TOML file.

Lookup order for the settings file:

1. an explicit path (``--config``)
2. ``$FITNESS_AGGREGATOR_CONFIG``
3. ``config/settings.toml`` in the current directory, if it exists
4. ``~/.fitness_aggregator/settings.toml``

Missing keys fall back to built-in defaults. Writing a value edits only that
key's line, so comments in the file are preserved.
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, fields, replace
from datetime import timedelta
from pathlib import Path

ENV_VAR = "FITNESS_AGGREGATOR_CONFIG"
DEFAULT_DIR = Path.home() / ".fitness_aggregator"


@dataclass(frozen=True)
class Settings:
    bodyweight_lb: float = 180.0
    merge_gap_minutes: float = 60.0
    suspect_duration_hours: float = 4.0

    @property
    def merge_gap(self) -> timedelta:
        return timedelta(minutes=self.merge_gap_minutes)

    @property
    def suspect_duration(self) -> timedelta:
        return timedelta(hours=self.suspect_duration_hours)


KEYS = tuple(f.name for f in fields(Settings))

# Keys whose change alters which automatic corrections apply.
AFFECTS_AUTO_CORRECTIONS = {"merge_gap_minutes", "suspect_duration_hours"}


def resolve_path(explicit: str | os.PathLike | None = None) -> Path:
    if explicit:
        return Path(explicit)
    if os.environ.get(ENV_VAR):
        return Path(os.environ[ENV_VAR])
    local = Path.cwd() / "config" / "settings.toml"
    if local.exists():
        return local
    return DEFAULT_DIR / "settings.toml"


def normalize_key(key: str) -> str:
    key = key.strip().replace("-", "_")
    if key not in KEYS:
        raise KeyError(f"unknown setting {key!r}; valid settings: {', '.join(KEYS)}")
    return key


MAX_VALUE = 1_000_000  # far beyond any sensible value; keeps timedelta from overflowing


class SettingsError(ValueError):
    """The settings file can't be read or written, or holds a bad value. The message is for the user."""


def _coerce(key: str, value) -> float:
    if isinstance(value, bool):  # float(True) == 1.0; a TOML boolean is a mistake here
        raise ValueError(f"{key} must be a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number, got {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"{key} must be a number, got {value!r}")
    if number <= 0:
        raise ValueError(f"{key} must be greater than 0")
    if number >= MAX_VALUE:
        raise ValueError(f"{key} must be less than {MAX_VALUE:,}")
    return number


def _format_number(number: float) -> str:
    """TOML text for a value, without the precision loss of ``:g`` (1234567.5 stays 1234567.5)."""
    return str(int(number)) if number.is_integer() else repr(number)


def load(path: str | os.PathLike | None = None) -> Settings:
    path = resolve_path(path)
    if not path.exists():
        return Settings()
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"settings file {path} is not valid TOML: {exc}") from None
    except OSError as exc:
        raise SettingsError(f"can't read settings file {path}: {exc.strerror or exc}") from None
    values = {}
    for raw_key, value in data.items():
        try:
            key = normalize_key(raw_key)
        except KeyError:
            continue  # ignore unknown keys rather than refusing to start
        try:
            values[key] = _coerce(key, value)
        except ValueError as exc:
            raise SettingsError(f"settings file {path}: {exc}") from None
    return replace(Settings(), **values)


def set_value(key: str, value: str, path: str | os.PathLike | None = None) -> tuple[Settings, Settings]:
    """Write one setting. Returns ``(old_settings, new_settings)``."""
    key = normalize_key(key)
    number = _coerce(key, value)
    path = resolve_path(path)
    old = load(path)

    text = path.read_text() if path.exists() else ""
    formatted = f"{key} = {_format_number(number)}"
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(formatted, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += formatted + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    except OSError as exc:
        raise SettingsError(f"can't write settings file {path}: {exc.strerror or exc}") from None
    return old, load(path)
