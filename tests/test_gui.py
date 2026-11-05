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

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from fitness_aggregator import __version__
from fitness_aggregator import corrections as corr
from fitness_aggregator.cli import app as cli_app
from fitness_aggregator.gui import app as gui_app
from fitness_aggregator.gui import charts
from fitness_aggregator.gui.controller import Controller, parse_adjustment
from fitness_aggregator.gui.window import HIGH_BG, LOW_BG, Dialogs, MainWindow
from fitness_aggregator.importers import parse_strong_csv
from fitness_aggregator.settings import Settings
from fitness_aggregator.store import Store
from helpers import REAL_EXPORT, workout_rows, write_export

TODAY = datetime(2026, 11, 5)
HEALTH_EXPORT = Path(__file__).parent / "fixtures" / "apple_health_export.xml"
needs_export = pytest.mark.skipif(not REAL_EXPORT.exists(), reason="real export not present")


class ScriptedDialogs(Dialogs):
    """Records messages and answers questions without showing anything."""

    def __init__(self, confirm=True, edit=None, open_path=None, health_path=None, save_path=None):
        super().__init__(None)
        self.infos, self.errors, self.questions, self.abouts, self.save_names = [], [], [], [], []
        self.answer, self.edit, self.open_path = confirm, edit, open_path
        self.health_path, self.save_path = health_path, save_path

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

    def open_health_file(self):
        return self.health_path

    def save_file(self, title, name):
        self.save_names.append(name)
        return self.save_path

    def about(self, title, text):
        self.abouts.append(text)


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

    def make(dialogs=None, **kwargs):
        kwargs.setdefault("background", False)
        w = MainWindow(Controller(Store(db), Settings(), today=TODAY), dialogs or ScriptedDialogs(), **kwargs)
        if kwargs.get("settings") is None:
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
    assert [w.tabs.tabText(i) for i in range(w.tabs.count())] == [
        "Overview", "Workouts", "Exercises", "PRs", "Charts", "Muscles", "Bodyweight", "Corrections", "Review (11)"]
    assert w.stack.currentWidget() is w.tabs
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
    fig = w.exercises.chart.figure
    assert fig.get_suptitle() == "Estimated 1RM: Leg Extension (Machine)"  # the shared plot_progress
    ax = fig.axes[0]
    assert len(ax.lines) == 1  # e1RM
    stars = [c for c in ax.collections if len(c.get_offsets()) and c.get_sizes()[0] == 120]
    assert stars  # PR markers
    w.exercises.metric_combo.setCurrentIndex(w.exercises.metric_combo.findData("top"))
    assert w.exercises.chart.figure.get_suptitle() == "Top set: Leg Extension (Machine)"


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
    w = MainWindow(Controller(Store(tmp_path / "empty.db"), Settings(), today=TODAY), dialogs, background=False)
    assert w.workouts.model.rowCount() == 0
    assert w.import_export(str(bad)) is None
    assert dialogs.errors
    w.close()


# --- charts ------------------------------------------------------------------------------------

@needs_export
def test_every_chart_draws_in_the_charts_tab(make_window):
    w = make_window()
    w.exercises.select_exercise("Leg Extension (Machine)")
    w.tabs.setCurrentWidget(w.charts)
    assert w.charts.exercise == "Leg Extension (Machine)"  # follows the Exercises tab
    titles = {}
    for spec in charts.CHARTS:
        w.charts.set_chart(spec.key)
        fig = w.charts.chart.figure
        assert fig is not None and fig.axes, spec.key
        titles[spec.key] = fig.get_suptitle()
        assert w.charts.exercise_combo.isEnabled() == spec.needs_exercise
    assert titles["progress"] == "Estimated 1RM: Leg Extension (Machine)"
    assert titles["rep-prs"] == "Rep maxes: Leg Extension (Machine)"
    assert titles["volume-month"] == "Monthly training volume"
    assert titles["dashboard"].startswith("Training dashboard")
    assert w.charts.chart.image is not None  # the dashboard is scaled, not squashed
    w.charts.set_chart("progress", "Leg Press")
    assert w.charts.chart.figure.get_suptitle() == "Estimated 1RM: Leg Press"


@needs_export
def test_charts_are_drawn_only_when_their_tab_is_shown(make_window):
    w = make_window()
    assert w.charts.chart.figure is None and w.muscles.chart.figure is None
    w.tabs.setCurrentWidget(w.muscles)
    assert w.muscles.chart.figure.get_suptitle() == "Sets per muscle per week"
    w.refresh()
    assert w.muscles.dirty is False and w.charts.dirty is True  # only the visible tab redraws


@needs_export
@pytest.mark.parametrize("suffix", [".png", ".svg", ".pdf"])
def test_save_chart_as(make_window, tmp_path, suffix):
    out = tmp_path / f"chart{suffix}"
    dialogs = ScriptedDialogs(save_path=str(out))
    w = make_window(dialogs)
    w.exercises.select_exercise("Leg Press")
    w.tabs.setCurrentWidget(w.charts)
    w.charts.set_chart("progress")
    assert w.charts.save() == out
    assert out.stat().st_size > 1000
    assert dialogs.save_names == ["progress_leg-press_2026-11-05.png"]
    assert str(out) in w.statusBar().currentMessage()


@needs_export
def test_save_chart_rejects_other_formats(make_window, tmp_path):
    dialogs = ScriptedDialogs(save_path=str(tmp_path / "chart.jpg"))
    w = make_window(dialogs)
    assert w.save_chart() is None
    assert w.tabs.currentWidget() is w.charts
    assert ".png" in dialogs.errors[-1] and not (tmp_path / "chart.jpg").exists()
    assert w.charts.save(str(tmp_path / "noext")) == tmp_path / "noext.png"


# --- muscles ---------------------------------------------------------------------------------

@needs_export
def test_muscles_table_colours_low_and_high_weeks(make_window):
    w = make_window()
    model = w.muscles.model
    assert model.rowCount() > 10
    assert model.headerData(0, Qt.Horizontal) == "Muscle"
    assert model.headerData(model.columnCount() - 1, Qt.Horizontal) == "Avg"
    assert len(w.muscles.periods) == 16
    seen = set()
    for r in range(model.rowCount()):
        row = model.row(r)
        for c, p in enumerate(w.muscles.periods, start=1):
            colour = model.data(model.index(r, c), Qt.BackgroundRole)
            status = row[("status", p)]
            if status == "low":
                assert 0 < row[p] < 10 and colour.name() == LOW_BG
            elif status == "high":
                assert row[p] > 20 and colour.name() == HIGH_BG
            else:
                assert colour is None
            seen.add(status)
    assert {"low", "high", "ok", "zero"} <= seen
    chest = next(model.row(r) for r in range(model.rowCount()) if model.row(r)["muscle"] == "chest")
    assert chest["avg"] == pytest.approx(sum(chest[p] for p in w.muscles.periods) / 16)


def test_muscle_table_leaves_the_current_week_uncoloured(qapp, tmp_path):
    bench = (("Bench Press (Barbell)", ((135, 8),) * 12),)
    export = write_export(tmp_path / "e.csv", workout_rows("2026-10-27 10:00:00"),
                          workout_rows("2026-11-02 10:00:00", exercises=bench),
                          workout_rows("2026-11-04 10:00:00", exercises=bench))
    with Store(tmp_path / "m.db") as store:
        store.apply_import(store.plan_import(parse_strong_csv(export), export), Settings())
    ctl = Controller(Store(tmp_path / "m.db"), Settings(), today=TODAY)
    periods, table = ctl.muscle_table()
    chest = next(r for r in table if r["muscle"] == "chest")
    assert chest[periods[-1]] == 24 and chest[("status", periods[-1])] == "partial"
    assert chest[("status", periods[0])] == "low"
    assert chest["avg"] == 2  # only complete weeks are averaged
    ctl.close()


# --- bodyweight ------------------------------------------------------------------------------

def _health_xml(path: Path, *readings: tuple[str, float]) -> Path:
    records = "".join(
        f'<Record type="HKQuantityTypeIdentifierBodyMass" sourceName="Scale" unit="lb" '
        f'startDate="{day} 07:00:00 -0700" endDate="{day} 07:00:00 -0700" value="{lb}"/>' for day, lb in readings)
    path.write_text(f'<?xml version="1.0"?><HealthData locale="en_US">{records}</HealthData>')
    return path


@needs_export
def test_import_apple_health_then_drop_days(make_window, tmp_path):
    dialogs = ScriptedDialogs(health_path=str(HEALTH_EXPORT))
    w = make_window(dialogs)
    assert "placeholder" in w.bodyweight.summary.text() and w.bodyweight.model.rowCount() == 0

    def pull_up():
        return next(r for r in w.controller.bests_rows() if r["exercise"] == "Pull Up (Assisted)")["e1rm_v"]

    before = pull_up()
    message = w.import_health()
    assert message.startswith("Imported. 3 days of bodyweight stored")
    assert "First Health import: 3 days" in dialogs.questions[-1]
    assert w.bodyweight.model.rowCount() == 3
    assert w.bodyweight.model.row(0)["lb"] == 173.5  # newest first
    assert "3 days" in w.bodyweight.summary.text()
    assert pull_up() != before  # assisted loads now use the entries, as in the CLI
    assert "No changes" in w.import_health()

    smaller = _health_xml(tmp_path / "export.xml", ("2026-09-10", 174))
    dialogs.answer = False
    assert w.import_health(str(smaller)) is None
    assert "Drop 2 day(s) of bodyweight" in dialogs.questions[-1]
    assert w.bodyweight.model.rowCount() == 3
    dialogs.answer = True
    assert w.import_health(str(smaller)).startswith("Imported. 1 days")
    assert w.bodyweight.model.rowCount() == 1


def test_bad_health_export_shows_an_error(qapp, tmp_path):
    dialogs = ScriptedDialogs()
    w = MainWindow(Controller(Store(tmp_path / "e.db"), Settings(), today=TODAY), dialogs, background=False)
    (tmp_path / "bad.xml").write_text("<nope")
    assert w.import_health(str(tmp_path / "bad.xml")) is None
    assert w.import_health(str(_health_xml(tmp_path / "empty.xml"))) is None
    assert len(dialogs.errors) == 2 and "no usable bodyweight" in dialogs.errors[-1]
    w.close()


# --- first run, shortcuts, settings, about ------------------------------------------------------

def test_empty_database_shows_the_welcome_screen(qapp, tmp_path):
    export = write_export(tmp_path / "e.csv", workout_rows("2026-10-27 10:00:00"))
    dialogs = ScriptedDialogs(open_path=str(export))
    w = MainWindow(Controller(Store(tmp_path / "e.db"), Settings(), today=TODAY), dialogs, background=False)
    assert w.stack.currentWidget() is w.welcome
    assert "e.db" in w.welcome.db_label.text()
    w.find_exercise()  # nothing to search yet
    assert w.stack.currentWidget() is w.welcome
    w.welcome.import_button.click()
    assert dialogs.infos[-1].startswith("Imported. 1 workouts")
    assert w.stack.currentWidget() is w.tabs
    assert w.workouts.model.rowCount() == 1
    w.close()


@needs_export
def test_keyboard_shortcuts(make_window):
    w = make_window()
    keys = {name: [k.toString() for k in a.shortcuts()] for name, a in w.actions.items()}
    assert "Ctrl+I" in keys["import"] and keys["refresh"] == ["F5"] and keys["find"] == ["Ctrl+F"]
    w.actions["find"].trigger()
    assert w.tabs.currentWidget() is w.exercises
    w.actions["refresh"].trigger()
    assert w.exercises.model.rowCount() == 103


@needs_export
def test_about_box(make_window, db):
    dialogs = ScriptedDialogs()
    w = make_window(dialogs)
    w.actions["about"].trigger()
    assert __version__ in dialogs.abouts[-1] and str(db) in dialogs.abouts[-1]
    assert "103 sessions" in dialogs.abouts[-1]


@needs_export
def test_window_remembers_size_and_tab(make_window, tmp_path):
    ini = str(tmp_path / "gui.ini")
    w = make_window(settings=QSettings(ini, QSettings.IniFormat))
    w.resize(720, 520)  # fits the offscreen screen, which restoreGeometry clamps to
    w.tabs.setCurrentWidget(w.muscles)
    w.close()
    again = make_window(settings=QSettings(ini, QSettings.IniFormat))
    assert again.tabs.currentWidget() is again.muscles
    assert (again.width(), again.height()) == (720, 520)
    fresh = make_window()
    assert fresh.tabs.currentWidget() is fresh.overview  # no settings: nothing remembered


# --- background loading ------------------------------------------------------------------------

@needs_export
def test_background_refresh_keeps_the_window_responsive(make_window):
    dialogs = ScriptedDialogs()
    w = make_window(dialogs, background=True)
    done = []
    w.refreshed.connect(lambda: done.append(True))
    assert w.stack.currentWidget() is w.loading and w.is_busy  # returned before the data loaded
    assert w.wait_until_idle(60)
    assert done and w.stack.currentWidget() is w.tabs
    assert w.workouts.model.rowCount() == 103 and not w.busy.isVisibleTo(w)

    # An action returns at once; the reload lands later.
    third = next(r["key"] for r in w.review.flags_model.rows if r["set"] == "3")
    assert "Dismissed 1" in w.review.dismiss([third])
    assert w.is_busy and w.review.flags_model.rowCount() == 3
    assert w.review.dismiss([third]) is None  # no actions on rows that are out of date
    assert "Still loading" in w.statusBar().currentMessage()
    assert w.wait_until_idle(60)
    assert w.review.flags_model.rowCount() == 2

    # Two quick refreshes: only the newest result is used.
    w.refresh()
    w.refresh()
    assert w.wait_until_idle(60)
    assert w.workouts.model.rowCount() == 103 and not dialogs.errors


@needs_export
def test_background_import_parses_off_the_ui_thread(make_window):
    dialogs = ScriptedDialogs()
    w = make_window(dialogs, background=True)
    assert w.wait_until_idle(60)
    assert w.import_export(str(REAL_EXPORT)) is None  # continues once parsing finishes
    assert w.wait_until_idle(60)
    assert "No changes" in dialogs.infos[-1]


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
    assert charts.bodyweight_figure([]).axes
    assert charts.muscles_figure(pd.DataFrame(columns=["period", "muscle", "sets"])).axes


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
