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


def create_window(db: Path | None = None, config: Path | None = None, **controller_kwargs):
    """Build the controller and main window (a QApplication must already exist)."""
    from .controller import Controller
    from .window import MainWindow
    return MainWindow(Controller.open(db, config, **controller_kwargs))


def run(db: Path | None = None, config: Path | None = None) -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Fitness Aggregator")
    window = create_window(db, config)
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
