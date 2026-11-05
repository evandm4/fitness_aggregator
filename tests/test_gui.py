"""Desktop app tests: run offscreen against a database imported from the real export."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("matplotlib")

from helpers import REAL_EXPORT, workout_rows, write_export
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from fitness_aggregator import corrections as corr
from fitness_aggregator.cli import app as cli_app
from fitness_aggregator.gui import app as gui_app
from fitness_aggregator.gui import charts
from fitness_aggregator.gui.controller import Controller, parse_adjustment
from fitness_aggregator.gui.window import Dialogs, MainWindow
from fitness_aggregator.importers import parse_strong_csv
from fitness_aggregator.settings import Settings
from fitness_aggregator.store import Store

TODAY = datetime(2026, 11, 5)
needs_export = pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not present")


class ScriptedDialogs(Dialogs):
    """Records messages and answers questions without showing anything."""

    def __init__(self, confirm=True, edit=None, open_path=None):
        super().__init__(None)
        self.infos, self.errors, self.questions = [], [], []
        self.answer, self.edit, self.open_path = confirm, edit, open_path

    def info(self, title, text):
        self.infos.append(text)

    def error(self, title, text):
        self.errors.append(text)

    def confirm(self, title, text, ok="Continue"):
        self.questions.append(text)
        return self.answer(text) if callable(self.answer) else self.answer

    def open_file(self):
        return self.open_path

    def edit_set(self, title, weight, reps):
        return self.edit


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def imported_db(tmp_path_factory):
    if not REAL_EXPORT.exists():
        pytest.skip("real export not present")
    path = tmp_path_factory.mktemp("gui") / "base.db"
    with Store(path) as store:
        store.apply_import(store.plan_import(parse_strong_csv(REAL_EXPORT), REAL_EXPORT), Settings())
    return path


@pytest.fixture
def db(imported_db, tmp_path):
    path = tmp_path / "lifts.db"
    shutil.copy(imported_db, path)
    return path


@pytest.fixture
def make_window(qapp, db):
    windows = []

    def make(dialogs=None):
        w = MainWindow(Controller(Store(db), Settings(), today=TODAY), dialogs or ScriptedDialogs())
        w.resize(1280, 800)
        windows.append(w)
        return w

    yield make
    for w in windows:
        w.close()


def _session_row(win, day: str) -> int:
    return next(i for i, r in enumerate(win.workouts.model.rows) if f"{r['date']:%Y-%m-%d}" == day)


# --- populated tabs -------------------------------------------------------------------------

@needs_export
def test_every_tab_is_populated(make_window):
    w = make_window()
    assert w.tabs.count() == 6
    assert w.overview.cards["Sessions"].text() == "103"
    assert w.overview.cards["Total volume"].text().endswith("lb")
    assert "3" in w.overview.flags_label.text() and "8" in w.overview.groups_label.text()
    assert w.overview.prs_model.rowCount() > 0
    assert w.overview.chart.figure is not None
    assert w.workouts.model.rowCount() == 103
    assert w.exercises.model.rowCount() == 103
    assert w.prs.bests_model.rowCount() == 103
    assert w.prs.recent_model.rowCount() > 0
    assert w.corrections.model.rowCount() == 3
    assert w.review.flags_model.rowCount() == 3
    assert w.review.groups_model.rowCount() == 8
    assert w.tabs.tabText(5) == "Review (11)"


@needs_export
def test_overview_buttons_jump_to_review(make_window):
    w = make_window()
    w.overview.groups_button.click()
    assert w.tabs.currentWidget() is w.review


@needs_export
def test_selecting_a_workout_shows_its_sets_and_corrections(make_window):
    w = make_window()
    w.workouts.select(_session_row(w, "2026-06-02"))
    html = w.workouts.detail.toPlainText()
    assert "Hip Abductor (Machine)" in html
    assert "2 workouts merged" in html
    assert "Corrections applied" in html and "#3 Merged 2026-06-02 14:06" in html
    assert w.workouts.model.rows[_session_row(w, "2026-06-02")]["flags"] == "merged ×2"


@needs_export
def test_selecting_an_exercise_fills_history_and_chart(make_window):
    w = make_window()
    w.exercises.select_exercise("Leg Extension (Machine)")
    assert w.exercises.title.text() == "Leg Extension (Machine)"
    assert w.exercises.history_model.rowCount() == 24
    newest = w.exercises.history_model.row(0)
    assert newest["top"] == "300 lb × 8" and "e1RM" in newest["prs"]
    ax = w.exercises.chart.figure.axes[0]
    assert len(ax.lines) == 2  # e1RM and top set
    assert len(ax.collections[0].get_offsets()) > 0  # PR markers


@needs_export
def test_exercise_search_filters_the_list(make_window):
    w = make_window()
    w.exercises.search.setText("abductor")
    names = [r["exercise"] for r in w.exercises.model.rows]
    assert names == ["Hip Abductor (Machine)", "Hip Abductor 2 (Machine)"]  # most sessions first
    w.exercises.search.clear()
    assert w.exercises.model.rowCount() == 103


# --- actions -----------------------------------------------------------------------------------

@needs_export
def test_dismiss_then_swap_preacher_curl_flags(make_window):
    dialogs = ScriptedDialogs()
    w = make_window(dialogs)
    third = next(r["key"] for r in w.review.flags_model.rows if r["set"] == "3")
    assert "Dismissed 1" in w.review.dismiss([third])
    assert w.review.flags_model.rowCount() == 2

    # Select the remaining Preacher Curl flags and use the button's slot.
    w.review.select_flags(lambda r: r["exercise"] == "Preacher Curl (Machine)")
    assert w.review.swap_button.isEnabled()
    message = w.review.swap()
    assert message.startswith("Recorded correction #4")
    assert w.review.flags_model.rowCount() == 0
    assert dialogs.infos[-1] == message

    w.workouts.select(_session_row(w, "2026-06-04"))
    text = w.workouts.detail.toPlainText()
    assert "was 8 lb × 80 (correction #4)" in text
    assert "80 lb" in text
    assert any(r["type"] == "Set fix" for r in w.corrections.model.rows)


@needs_export
def test_edit_flag_uses_the_dialog(make_window):
    dialogs = ScriptedDialogs(edit=(80, 8))
    w = make_window(dialogs)
    first = next(r["key"] for r in w.review.flags_model.rows if r["set"] == "1")
    assert "Recorded correction" in w.review.edit(first)
    assert w.review.flags_model.rowCount() == 2


@needs_export
def test_combine_hip_abductor_with_offset(make_window):
    w = make_window()
    w.review.select_group("Hip Abductor (Machine)")
    assert w.review.detail.kept == "Hip Abductor (Machine)"
    edit = w.review.adjust_edits["Hip Abductor 2 (Machine)"]
    assert edit.text() == "-50"  # prefilled from similarity.suggest_offset
    assert "Suggested: -50 lb" in w.review.suggestion_label.text()

    message = w.review.combine(with_offset=True)
    assert "Recorded correction" in message
    merge = w.controller.store.active_merges()["Hip Abductor 2 (Machine)"]
    assert (merge.into, merge.offset) == ("Hip Abductor (Machine)", -50)
    assert w.review.groups_model.rowCount() == 7
    names = {r["exercise"] for r in w.exercises.model.rows}
    assert "Hip Abductor 2 (Machine)" not in names
    w.exercises.select_exercise("Hip Abductor (Machine)")
    assert w.exercises.history_model.rowCount() == 21
    assert "includes Hip Abductor 2 (Machine) (-50 lb)" in w.exercises.note.text()


@needs_export
def test_combine_rejects_bad_adjustment_and_keep_separate(make_window):
    dialogs = ScriptedDialogs()
    w = make_window(dialogs)
    index = w.review.select_group("Crunch")
    assert w.review.combine(True, adjustments={"Crunch (Machine)": "lots"}) is None
    assert "adjustment" in dialogs.errors[-1]
    assert "Keeping" in w.review.keep_separate(index)
    assert w.review.groups_model.rowCount() == 7


@needs_export
def test_revert_and_restore_a_correction(make_window):
    dialogs = ScriptedDialogs()
    w = make_window(dialogs)
    row = next(i for i, r in enumerate(w.corrections.model.rows) if r["type"] == "Workout merge")
    w.corrections.view.selectRow(row)
    assert w.corrections.revert_button.isEnabled() and not w.corrections.restore_button.isEnabled()
    message = w.corrections.revert()
    assert message.startswith("Reverted #3")
    assert w.workouts.model.rowCount() == 104
    assert w.controller.store.get_correction(3).status == corr.REVERTED
    assert "Restored #3" in w.corrections.restore(3)
    assert w.workouts.model.rowCount() == 103


# --- importing ------------------------------------------------------------------------------------

@needs_export
def test_reimporting_the_same_export_reports_no_changes(make_window):
    dialogs = ScriptedDialogs(open_path=str(REAL_EXPORT))
    w = make_window(dialogs)
    assert "No changes" in w.import_export()


@needs_export
def test_older_export_needs_confirmation(make_window, tmp_path):
    older = write_export(tmp_path / "old.csv", workout_rows("2026-06-02 14:00:32"))
    dialogs = ScriptedDialogs(confirm=False)
    w = make_window(dialogs)
    assert w.import_export(str(older)) is None
    assert "older" in dialogs.questions[-1]
    assert w.workouts.model.rowCount() == 103

    dialogs.answer = True  # confirm the older export, then the removals
    message = w.import_export(str(older))
    assert message.startswith("Imported. 1 workouts stored.")
    assert any("Drop 103 workout(s)" in q for q in dialogs.questions)
    assert w.workouts.model.rowCount() == 1


def test_bad_file_shows_an_error(qapp, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("not,a,strong,export\n")
    dialogs = ScriptedDialogs()
    w = MainWindow(Controller(Store(tmp_path / "empty.db"), Settings(), today=TODAY), dialogs)
    assert w.workouts.model.rowCount() == 0
    assert w.import_export(str(bad)) is None
    assert dialogs.errors
    w.close()


# --- pieces ------------------------------------------------------------------------------------------

def test_parse_adjustment():
    assert parse_adjustment("-50") == (-50, None)
    assert parse_adjustment(" −50 lb") == (-50, None)
    assert parse_adjustment("x0.75") == (None, 0.75)
    assert parse_adjustment("") == (None, None)
    with pytest.raises(ValueError):
        parse_adjustment("abc")


def test_charts_handle_empty_frames():
    assert charts.progress_figure(pd.DataFrame(), pd.DataFrame()).axes
    assert charts.weekly_volume_figure(pd.DataFrame()).axes


def test_cli_gui_command_passes_options(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(gui_app, "run", lambda db, config: seen.update(db=db, config=config) or 0)
    result = CliRunner().invoke(cli_app, ["--db", str(tmp_path / "x.db"), "--config", str(tmp_path / "s.toml"), "gui"])
    assert result.exit_code == 0, result.output
    assert seen == {"db": tmp_path / "x.db", "config": tmp_path / "s.toml"}


def test_cli_gui_explains_missing_install(monkeypatch, tmp_path):
    monkeypatch.setattr(gui_app, "missing_dependencies", lambda: "the desktop app needs PySide6. Install it with: "
                        + gui_app.INSTALL_HINT)
    result = CliRunner().invoke(cli_app, ["--db", str(tmp_path / "x.db"), "gui"])
    assert result.exit_code == 1
    assert "pip install 'fitness_aggregator[gui]'" in " ".join(result.output.split())


def test_gui_script_parses_options(monkeypatch):
    seen = {}
    monkeypatch.setattr(gui_app, "run", lambda db, config: seen.update(db=db, config=config) or 0)
    assert gui_app.main(["--db", "a.db"]) == 0
    assert seen == {"db": Path("a.db"), "config": None}
