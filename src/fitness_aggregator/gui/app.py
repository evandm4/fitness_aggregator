"""Entry point: ``fitness-gui [--db PATH] [--config PATH]`` (or ``fitness gui``)."""

from __future__ import annotations

import argparse
import sys
import importlib.util
from pathlib import Path

INSTALL_HINT = "pip install 'fitness_aggregator[gui]'"


def missing_dependencies() -> str | None:
    """A friendly message if PySide6 or matplotlib isn't installed, else None."""
    missing = [m for m in ("PySide6", "matplotlib") if importlib.util.find_spec(m) is None]
    if missing:
        return f"the desktop app needs {' and '.join(missing)}. Install it with: {INSTALL_HINT}"
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fitness-gui", description="Desktop app for fitness_aggregator.")
    p.add_argument("--db", type=Path, default=None,
                   help="Database file (default: $FITNESS_AGGREGATOR_DB, else ~/.fitness_aggregator/lifts.db).")
    p.add_argument("--config", type=Path, default=None,
                   help="Settings file (same lookup as the CLI when omitted).")
    return p


def create_window(db: Path | None = None, config: Path | None = None, *, settings=None, background: bool = True,
                  **controller_kwargs):
    """Build the controller and main window (a QApplication must already exist).

    ``settings`` is a QSettings for the window size and last tab (None remembers nothing).
    """
    from .controller import Controller
    from .window import MainWindow
    return MainWindow(Controller.open(db, config, **controller_kwargs), settings=settings, background=background)


def run(db: Path | None = None, config: Path | None = None) -> int:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from .window import SETTINGS_APP, SETTINGS_ORG

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(SETTINGS_APP)
    app.setOrganizationName(SETTINGS_ORG)
    window = create_window(db, config, settings=QSettings(SETTINGS_ORG, SETTINGS_APP))
    window.show()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    missing = missing_dependencies()
    if missing:
        print(f"Error: {missing}", file=sys.stderr)
        return 1
    return run(args.db, args.config)


if __name__ == "__main__":
    sys.exit(main())
