"""Main window and tabs. Widgets only: data comes from ``Controller``."""

from __future__ import annotations

import html
import io
import time
from collections.abc import Callable

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from PySide6.QtCore import QByteArray, QItemSelectionModel, QObject, QRunnable, QSettings, Qt, QThreadPool, Signal
from PySide6.QtGui import QAction, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTableView,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import corrections as corr
from ..importers import StrongParseError
from ..plots import METRICS
from ..plots.progress import METRIC_TITLES
from ..store import ImportRefused
from . import charts
from .controller import HEALTH_ERRORS, HIGH_SETS, LOW_SETS, TIER_TEXT, Controller, describe_plan
from .models import RIGHT, STATUS_COLORS, Column, RowTableModel, date_col, num_col

MUTED = "#52514e"
CHART_SURFACE = "#fcfcfb"  # plots.style.SURFACE, so the canvas edge blends in
LOW_BG, HIGH_BG = "#fbe7c2", "#ead9f6"  # muscle cells under 10 / over 20 sets a week
SETTINGS_ORG, SETTINGS_APP = "fitness_aggregator", "Fitness Aggregator"


# --- dialogs (injectable so tests never block) ---------------------------------------

class Dialogs:
    """Every modal interaction the window needs. Tests pass a scripted subclass."""

    def __init__(self, parent: QWidget | None = None):
        self.parent = parent

    def info(self, title: str, text: str) -> None:
        QMessageBox.information(self.parent, title, text)

    def error(self, title: str, text: str) -> None:
        QMessageBox.warning(self.parent, title, text)

    def confirm(self, title: str, text: str, ok: str = "Continue") -> bool:
        box = QMessageBox(QMessageBox.Question, title, text, parent=self.parent)
        yes = box.addButton(ok, QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        return box.clickedButton() is yes

    def open_file(self) -> str | None:
        path, _ = QFileDialog.getOpenFileName(self.parent, "Import Strong export", "",
                                              "Strong exports (*.csv *.txt);;All files (*)")
        return path or None

    def open_health_file(self) -> str | None:
        path, _ = QFileDialog.getOpenFileName(self.parent, "Import Apple Health export", "",
                                              "Apple Health exports (*.zip *.xml);;All files (*)")
        return path or None

    def save_file(self, title: str, name: str) -> str | None:
        path, _ = QFileDialog.getSaveFileName(self.parent, title, name,
                                              "PNG image (*.png);;SVG image (*.svg);;PDF document (*.pdf)")
        return path or None

    def about(self, title: str, text: str) -> None:
        QMessageBox.about(self.parent, title, text)

    def edit_set(self, title: str, weight: float, reps: float) -> tuple[float, float] | None:
        dlg = QDialog(self.parent)
        dlg.setWindowTitle(title)
        form = QFormLayout(dlg)
        w, r = QDoubleSpinBox(), QDoubleSpinBox()
        for box, value, suffix in ((w, weight, " lb"), (r, reps, "")):
            box.setRange(0, 2000)
            box.setDecimals(1)
            box.setValue(value)
            box.setSuffix(suffix)
        form.addRow("Weight", w)
        form.addRow("Reps", r)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        return (w.value(), r.value()) if dlg.exec() == QDialog.Accepted else None


# --- widget helpers --------------------------------------------------------------------

def make_table(model: RowTableModel, *, sortable: bool = True, stretch: int | None = -1) -> QTableView:
    view = QTableView()
    view.setModel(model)
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setSelectionMode(QAbstractItemView.SingleSelection)
    view.setEditTriggers(QAbstractItemView.NoEditTriggers)
    view.setAlternatingRowColors(True)
    view.setWordWrap(False)
    view.setShowGrid(False)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(24)
    view.horizontalHeader().setHighlightSections(False)
    view.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    view.setSortingEnabled(sortable)
    if stretch is not None:
        view.horizontalHeader().setStretchLastSection(stretch == -1)
        if stretch >= 0:
            view.horizontalHeader().setSectionResizeMode(stretch, QHeaderView.Stretch)
    return view


def fit_columns(view: QTableView, max_width: int = 360) -> None:
    view.resizeColumnsToContents()
    header = view.horizontalHeader()
    last = header.count() - 1
    for i in range(header.count()):
        if i == last and header.stretchLastSection():
            continue
        if header.sectionResizeMode(i) != QHeaderView.Stretch:
            header.resizeSection(i, min(header.sectionSize(i) + 14, max_width))


def selected_row(view: QTableView) -> dict | None:
    rows = view.selectionModel().selectedRows() if view.selectionModel() else []
    return view.model().row(rows[0].row()) if rows else None


def heading(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet("font-size: 13pt; font-weight: 600; margin-top: 4px;")
    return label


def muted(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {MUTED};")
    label.setWordWrap(True)
    return label


class ScaledImage(QWidget):
    """Paints a pixmap scaled to the widget (the owner keeps the aspect ratio)."""

    def __init__(self, pixmap: QPixmap):
        super().__init__()
        self.pixmap = pixmap

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawPixmap(self.rect(), self.pixmap)


class ChartView(QScrollArea):
    """Holds one chart; ``show_figure`` swaps in a new figure.

    Normally the matplotlib canvas fills the view and the figure resizes with
    it (constrained layout re-flows it). It never shrinks below ``min_scale``
    of the figure's natural size: past that the view scrolls instead of
    squashing the chart. With ``scale=True`` (big composite figures like the
    dashboard, whose layout only works at its natural size) the figure is
    drawn once at natural size and shown scaled to the view's width.
    """

    def __init__(self, min_height: int = 220):
        super().__init__()
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(f"QScrollArea, QScrollArea > QWidget > QWidget {{ background: {CHART_SURFACE}; }}")
        self.canvas: FigureCanvasQTAgg | None = None
        self.image: ScaledImage | None = None
        self._figure = None
        self._natural = (0, 0)
        self._min_scale = 0.5
        self.setMinimumHeight(min_height)

    @property
    def figure(self):
        return self._figure

    def show_figure(self, fig, min_scale: float = 0.5, scale: bool = False) -> None:
        self._figure, self._min_scale = fig, min_scale
        w, h = fig.get_size_inches() * fig.dpi
        self._natural = (w, h)
        if scale:
            ratio = self.devicePixelRatioF()
            FigureCanvasAgg(fig)
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=fig.dpi * max(ratio, 1.0))
            pixmap = QPixmap()
            pixmap.loadFromData(buf.getvalue(), "PNG")
            self.canvas, self.image = None, ScaledImage(pixmap)
            self.setWidgetResizable(False)
            self.setWidget(self.image)  # deletes the previous chart widget
            self._fit_image()
            return
        canvas = FigureCanvasQTAgg(fig)
        canvas.setMinimumSize(int(w * min_scale), int(h * min_scale))
        charts.keep_footnotes_fitted(fig)
        self.setWidgetResizable(True)
        self.setWidget(canvas)  # deletes the previous chart widget
        self.canvas, self.image = canvas, None
        canvas.draw_idle()

    def _fit_image(self) -> None:
        if self.image is None:
            return
        nw, nh = self._natural
        width = max(self.viewport().width(), int(nw * self._min_scale))
        self.image.setFixedSize(width, int(width * nh / nw))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_image()


# --- tabs ------------------------------------------------------------------------------------

class Tab(QWidget):
    def __init__(self, win: MainWindow):
        super().__init__()
        self.win = win

    @property
    def ctl(self) -> Controller:
        return self.win.controller

    def refresh(self) -> None:
        raise NotImplementedError


class OverviewTab(Tab):
    def __init__(self, win):
        super().__init__(win)
        layout = QVBoxLayout(self)
        self.cards: dict[str, QLabel] = {}
        grid = QGridLayout()
        grid.setSpacing(10)
        for i, name in enumerate(("Sessions", "Per week", "Weekly streak", "Days since last", "Total volume")):
            frame = QFrame()
            frame.setObjectName("card")
            frame.setStyleSheet("#card { background: palette(base); border: 1px solid #dcdbd6; border-radius: 6px; }")
            v = QVBoxLayout(frame)
            title = QLabel(name)
            title.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
            value = QLabel("—")
            value.setStyleSheet("font-size: 18pt; font-weight: 600;")
            v.addWidget(title)
            v.addWidget(value)
            self.cards[name] = value
            grid.addWidget(frame, 0, i)
        layout.addLayout(grid)
        self.range_label = muted()
        layout.addWidget(self.range_label)

        review = QHBoxLayout()
        self.flags_label = QLabel()
        self.flags_button = QPushButton("Review data-entry flags")
        self.flags_button.clicked.connect(lambda: self.win.show_review("flags"))
        self.groups_label = QLabel()
        self.groups_button = QPushButton("Review similar exercises")
        self.groups_button.clicked.connect(lambda: self.win.show_review("groups"))
        for w in (self.flags_label, self.flags_button):
            review.addWidget(w)
        review.addSpacing(24)
        for w in (self.groups_label, self.groups_button):
            review.addWidget(w)
        review.addStretch(1)
        layout.addLayout(review)

        body = QSplitter(Qt.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(heading("Recent PRs (last 30 days)"))
        self.prs_model = RowTableModel([
            date_col("Date", "date"), Column("Exercise", "exercise"), Column("PR", "pr"),
            Column("Set", "detail"), Column("New", "new", align=RIGHT, sort=lambda r: r["value"]),
        ])
        self.prs_view = make_table(self.prs_model, stretch=1)
        lv.addWidget(self.prs_view)
        body.addWidget(left)
        self.chart = ChartView()
        body.addWidget(self.chart)
        body.setSizes([560, 640])
        layout.addWidget(body, 1)

    def refresh(self):
        for name, value in self.ctl.headline().items():
            self.cards[name].setText(value)
        self.range_label.setText(self.ctl.date_range_text())
        flags, groups = len(self.ctl.snap.flags), len(self.ctl.snap.candidates)
        self.flags_label.setText(f"<b>{flags}</b> set{'s' if flags != 1 else ''} flagged as possible data-entry errors")
        self.groups_label.setText(f"<b>{groups}</b> group{'s' if groups != 1 else ''} of similar exercises")
        self.flags_button.setEnabled(bool(flags))
        self.groups_button.setEnabled(bool(groups))
        self.prs_model.set_rows(self.ctl.recent_pr_rows())
        fit_columns(self.prs_view)
        self.chart.show_figure(charts.weekly_volume_figure(self.ctl.snap.weekly), min_scale=0.45)


class WorkoutsTab(Tab):
    def __init__(self, win):
        super().__init__(win)
        layout = QHBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        self.model = RowTableModel([
            date_col("Date", "date", "%a %Y-%m-%d %H:%M"), Column("Name", "name"),
            Column("Duration", "duration", align=RIGHT, sort=lambda r: r["duration_s"]),
            num_col("Exercises", "exercises"), num_col("Sets", "sets"),
            Column("", "flags", color=lambda r: "#a15c00"),
        ])
        self.view = make_table(self.model)
        self.view.selectionModel().selectionChanged.connect(self._selected)
        split.addWidget(self.view)
        self.detail = QTextBrowser()
        self.detail.setOpenLinks(False)
        split.addWidget(self.detail)
        split.setSizes([620, 520])
        layout.addWidget(split)

    def refresh(self):
        self.model.set_rows(self.ctl.session_rows())
        self.view.sortByColumn(0, Qt.DescendingOrder)
        fit_columns(self.view)
        if self.model.rowCount():
            self.select(0)

    def select(self, row: int) -> None:
        self.view.selectRow(row)
        self.view.scrollTo(self.model.index(row, 0))

    def _selected(self, *_):
        row = selected_row(self.view)
        self.detail.setHtml(self.ctl.session_html(row["index"]) if row else "")


class ExercisesTab(Tab):
    def __init__(self, win):
        super().__init__(win)
        layout = QHBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search exercises…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        lv.addWidget(self.search)
        self.model = RowTableModel([
            Column("Exercise", "exercise"), num_col("Sessions", "sessions"), date_col("Last", "last"),
            Column("Best", "best", align=RIGHT, sort=lambda r: r["best_v"]),
        ])
        self.view = make_table(self.model, stretch=0)
        self.view.selectionModel().selectionChanged.connect(self._selected)
        lv.addWidget(self.view)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.title = heading("")
        self.note = muted()
        title_row = QHBoxLayout()
        title_row.addWidget(self.title, 1)
        title_row.addWidget(QLabel("Chart"))
        self.metric_combo = QComboBox()
        self.metric_combo.addItem("Best for this exercise", None)
        for key in METRICS:
            self.metric_combo.addItem(METRIC_TITLES[key], key)
        self.metric_combo.setToolTip("What the progress chart shows")
        self.metric_combo.currentIndexChanged.connect(lambda _: self._draw_chart())
        title_row.addWidget(self.metric_combo)
        rv.addLayout(title_row)
        rv.addWidget(self.note)
        rsplit = QSplitter(Qt.Vertical)
        self.chart = ChartView()
        rsplit.addWidget(self.chart)
        self.history_model = RowTableModel([
            date_col("Date", "date"), Column("Top set", "top", sort=lambda r: r["top_v"]),
            num_col("e1RM", "e1rm"), num_col("Sets", "sets"), num_col("Reps", "reps"),
            num_col("Volume", "volume"), Column("PRs", "prs", color=lambda r: "#1a7f37"),
            Column("Notes", "notes", color=lambda r: MUTED),
        ])
        self.history_view = make_table(self.history_model)
        rsplit.addWidget(self.history_view)
        rsplit.setStretchFactor(0, 3)
        rsplit.setStretchFactor(1, 2)
        rsplit.setSizes([400, 260])
        rv.addWidget(rsplit, 1)
        split.addWidget(right)
        split.setSizes([470, 790])
        layout.addWidget(split)
        self.current: str | None = None

    def refresh(self):
        current = self.current
        self.model.set_rows(self.ctl.exercise_rows())
        self.view.sortByColumn(1, Qt.DescendingOrder)
        self._filter(self.search.text())
        fit_columns(self.view)
        rows = [i for i, r in enumerate(self.model.rows) if r["exercise"] == current]
        if rows or self.model.rowCount():
            self.view.selectRow(rows[0] if rows else 0)

    def _filter(self, text: str):
        t = text.strip().lower()
        self.model.set_filter((lambda r: t in r["exercise"].lower()) if t else None)

    def select_exercise(self, name: str) -> None:
        self.search.clear()
        rows = [i for i, r in enumerate(self.model.rows) if r["exercise"] == name]
        if not rows:
            raise KeyError(name)
        self.view.selectRow(rows[0])

    def _selected(self, *_):
        row = selected_row(self.view)
        if row is None:
            return
        name = self.current = row["exercise"]
        kind = self.ctl.kind(name)
        hist, events = self.ctl.history(name)
        self.title.setText(name)
        sources = [m for m in self.ctl.snap.active_merges.values() if m.into == name]
        note = [f"{kind.capitalize()} · {len(hist)} sessions · {len(events)} PRs"]
        note += [f"includes {m.source}" + (f" ({m.label})" if m.label else "") for m in sources]
        self.note.setText("  ·  ".join(note))
        self.history_model.set_rows(self.ctl.history_rows(name))
        self.history_view.sortByColumn(0, Qt.DescendingOrder)
        fit_columns(self.history_view)
        self._draw_chart()
        self.win.exercise_selected(name)

    @property
    def metric(self) -> str | None:
        return self.metric_combo.currentData()

    def _draw_chart(self) -> None:
        if self.current is None:
            return
        hist, events = self.ctl.history(self.current)
        self.chart.show_figure(charts.progress_figure(hist, events, self.current, self.ctl.kind(self.current),
                                                      metric=self.metric))


class PRsTab(Tab):
    def __init__(self, win):
        super().__init__(win)
        layout = QVBoxLayout(self)
        split = QSplitter(Qt.Vertical)
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.addWidget(heading("Current bests"))
        self.bests_model = RowTableModel([
            Column("Exercise", "exercise"), Column("Best e1RM", "e1rm", align=RIGHT, sort=lambda r: r["e1rm_v"]),
            Column("Set", "e1rm_set"), Column("Date", "e1rm_date"), Column("Heaviest", "heaviest"),
            Column("Date ", "heaviest_date"), num_col("Sessions", "sessions"), date_col("Last done", "last"),
        ])
        self.bests_view = make_table(self.bests_model, stretch=0)
        tv.addWidget(self.bests_view)
        split.addWidget(top)
        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.addWidget(heading("Recent PRs (last 30 days)"))
        self.recent_model = RowTableModel([
            date_col("Date", "date"), Column("Exercise", "exercise"), Column("PR", "pr"), Column("Set", "detail"),
            Column("New", "new", align=RIGHT, sort=lambda r: r["value"]), Column("Previous", "previous", align=RIGHT),
        ])
        self.recent_view = make_table(self.recent_model, stretch=1)
        bv.addWidget(self.recent_view)
        split.addWidget(bottom)
        layout.addWidget(split)
        self.footer = muted("Loads in lb. e1RM: Epley, from sets of 1–12 reps. The first session of each "
                            "exercise is the baseline, not a PR.")
        layout.addWidget(self.footer)

    def refresh(self):
        self.bests_model.set_rows(self.ctl.bests_rows())
        self.bests_view.sortByColumn(0, Qt.AscendingOrder)
        fit_columns(self.bests_view)
        self.recent_model.set_rows(self.ctl.recent_pr_rows())
        fit_columns(self.recent_view)


class CorrectionsTab(Tab):
    def __init__(self, win):
        super().__init__(win)
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(muted("Every change applied on top of your raw data. Reverting one undoes it; "
                            "automatic corrections you revert stay reverted on later imports."), 1)
        self.show_obsolete = QCheckBox("Show obsolete")
        self.show_obsolete.toggled.connect(lambda _: self.refresh())
        bar.addWidget(self.show_obsolete)
        layout.addLayout(bar)
        self.model = RowTableModel([
            num_col("ID", "id"), Column("Type", "type"), Column("Source", "source"),
            Column("Status", "status", color=lambda r: STATUS_COLORS.get(r["status"])),
            Column("Reason", "reason"),
        ])
        self.view = make_table(self.model)
        self.view.selectionModel().selectionChanged.connect(self._selected)
        layout.addWidget(self.view, 1)
        self.detail = muted()
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.detail)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.revert_button = QPushButton("Revert")
        self.revert_button.clicked.connect(lambda: self.revert())
        self.restore_button = QPushButton("Restore")
        self.restore_button.clicked.connect(lambda: self.restore())
        buttons.addWidget(self.revert_button)
        buttons.addWidget(self.restore_button)
        layout.addLayout(buttons)

    def refresh(self):
        self.model.set_rows(self.ctl.correction_rows(self.show_obsolete.isChecked()))
        self.view.sortByColumn(0, Qt.AscendingOrder)
        fit_columns(self.view, max_width=180)
        self._selected()

    def _selected(self, *_):
        row = selected_row(self.view)
        c = row["correction"] if row else None
        self.revert_button.setEnabled(bool(c) and c.status in (corr.ACTIVE, corr.STALE))
        self.restore_button.setEnabled(bool(c) and c.status == corr.REVERTED)
        if c is None:
            self.detail.setText("Select a correction to revert or restore it.")
            return
        target = ", ".join(f"{k} {v}" for k, v in sorted(c.target.items()))
        self.detail.setText(
            f"<b>#{c.id}</b> {html.escape(c.reason)}<br>Applies to {html.escape(target)} · {c.source}, "
            f"{c.status} · created {c.created_at:%Y-%m-%d %H:%M}, updated {c.updated_at:%Y-%m-%d %H:%M}")

    def _target(self, correction_id):
        if correction_id is None:
            row = selected_row(self.view)
            return row["id"] if row else None
        return correction_id

    def revert(self, correction_id: int | None = None) -> str | None:
        cid = self._target(correction_id)
        if cid is None:
            return None
        return self.win.run_action("Revert correction",
                                   lambda: self.ctl.set_correction_status(cid, corr.REVERTED))

    def restore(self, correction_id: int | None = None) -> str | None:
        cid = self._target(correction_id)
        if cid is None:
            return None
        return self.win.run_action("Restore correction",
                                   lambda: self.ctl.set_correction_status(cid, corr.ACTIVE))


class ReviewTab(Tab):
    def __init__(self, win):
        super().__init__(win)
        layout = QVBoxLayout(self)
        split = QSplitter(Qt.Vertical)

        # (a) data-entry flags
        flags_box = QGroupBox("Possible data-entry errors")
        fv = QVBoxLayout(flags_box)
        self.flags_model = RowTableModel([
            date_col("Workout", "date", "%Y-%m-%d %H:%M"), Column("Exercise", "exercise"), Column("Set", "set"),
            Column("Recorded", "recorded"), Column("Swap to", "suggested"), Column("Why", "message"),
        ])
        self.flags_view = make_table(self.flags_model)
        self.flags_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.flags_view.selectionModel().selectionChanged.connect(self._flags_selected)
        fv.addWidget(self.flags_view)
        fb = QHBoxLayout()
        self.flags_note = muted()
        fb.addWidget(self.flags_note, 1)
        self.swap_button = QPushButton("Swap weight and reps")
        self.swap_button.clicked.connect(lambda: self.swap())
        self.edit_button = QPushButton("Edit…")
        self.edit_button.clicked.connect(lambda: self.edit())
        self.dismiss_button = QPushButton("Not an error (dismiss)")
        self.dismiss_button.clicked.connect(lambda: self.dismiss())
        for b in (self.swap_button, self.edit_button, self.dismiss_button):
            fb.addWidget(b)
        fv.addLayout(fb)
        split.addWidget(flags_box)

        # (b) similar exercises
        groups_box = QGroupBox("Similar exercises")
        gh = QHBoxLayout(groups_box)
        self.groups_model = RowTableModel([Column("Exercises", "names"), Column("Match", "tier")])
        self.groups_view = make_table(self.groups_model, sortable=False, stretch=0)
        self.groups_view.selectionModel().selectionChanged.connect(self._group_selected)
        gh.addWidget(self.groups_view, 5)
        panel = QWidget()
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(6, 0, 0, 0)
        self.tier_label = QLabel()
        self.tier_label.setStyleSheet("font-weight: 600;")
        pv.addWidget(self.tier_label)
        self.members_model = RowTableModel([
            Column("Exercise", "name"), num_col("Sessions", "sessions"), Column("Used", "span"),
            Column("Last top set", "last"),
        ])
        self.members_view = make_table(self.members_model, sortable=False)
        self.members_view.setMaximumHeight(110)
        pv.addWidget(self.members_view)
        keep_row = QHBoxLayout()
        keep_row.addWidget(QLabel("Keep the name"))
        self.kept_combo = QComboBox()
        self.kept_combo.currentTextChanged.connect(self._kept_changed)
        keep_row.addWidget(self.kept_combo, 1)
        pv.addLayout(keep_row)
        self.suggestion_label = QLabel()
        self.suggestion_label.setWordWrap(True)
        pv.addWidget(self.suggestion_label)
        self.adjust_form = QFormLayout()
        pv.addLayout(self.adjust_form)
        self.adjust_edits: dict[str, QLineEdit] = {}
        pv.addStretch(1)
        gb = QHBoxLayout()
        gb.addStretch(1)
        self.combine_button = QPushButton("Combine as-is")
        self.combine_button.clicked.connect(lambda: self.combine(with_offset=False))
        self.offset_button = QPushButton("Combine with offset")
        self.offset_button.clicked.connect(lambda: self.combine(with_offset=True))
        self.separate_button = QPushButton("Keep separate")
        self.separate_button.clicked.connect(lambda: self.keep_separate())
        for b in (self.combine_button, self.offset_button, self.separate_button):
            gb.addWidget(b)
        pv.addLayout(gb)
        gh.addWidget(panel, 6)
        split.addWidget(groups_box)
        split.setSizes([210, 470])
        layout.addWidget(split)
        self.flags_box, self.groups_box = flags_box, groups_box
        self.detail = None

    def refresh(self):
        self.flags_model.set_rows(self.ctl.flag_rows())
        fit_columns(self.flags_view)
        self._flags_selected()
        self.groups_model.set_rows(self.ctl.candidate_rows())
        fit_columns(self.groups_view)
        if self.groups_model.rowCount():
            self.groups_view.selectRow(0)
        else:
            self._show_detail(None)

    # --- flags ------------------------------------------------------------

    def _flag_keys(self, keys) -> list[str]:
        if keys is not None:
            return list(keys)
        rows = self.flags_view.selectionModel().selectedRows()
        return [self.flags_model.row(i.row())["key"] for i in rows]

    def _flags_selected(self, *_):
        rows = [self.flags_model.row(i.row()) for i in self.flags_view.selectionModel().selectedRows()]
        n = self.flags_model.rowCount()
        self.swap_button.setEnabled(bool(rows) and all(r["swap"] for r in rows))
        self.edit_button.setEnabled(len(rows) == 1)
        self.dismiss_button.setEnabled(bool(rows))
        if not n:
            self.flags_note.setText("No likely data-entry errors.")
        else:
            self.flags_note.setText(f"{n} flagged set{'s' if n != 1 else ''}. Select one or more "
                                    "(Ctrl/Shift-click) and choose an action.")

    def select_flags(self, predicate: Callable[[dict], bool]) -> None:
        sm = self.flags_view.selectionModel()
        sm.clearSelection()
        flags = QItemSelectionModel.Select | QItemSelectionModel.Rows
        for i, r in enumerate(self.flags_model.rows):
            if predicate(r):
                sm.select(self.flags_model.index(i, 0), flags)

    def dismiss(self, keys: list[str] | None = None) -> str | None:
        keys = self._flag_keys(keys)
        if not keys:
            return None
        return self.win.run_action("Dismiss flags", lambda: self.ctl.dismiss_flags(keys))

    def swap(self, keys: list[str] | None = None) -> str | None:
        keys = self._flag_keys(keys)
        if not keys:
            return None
        return self.win.run_action("Swap weight and reps", lambda: self.ctl.swap_flags(keys))

    def edit(self, key: str | None = None, weight: float | None = None, reps: float | None = None) -> str | None:
        keys = self._flag_keys([key] if key else None)
        if len(keys) != 1:
            return None
        flag = self.ctl.flag(keys[0])
        if weight is None or reps is None:
            answer = self.win.dialogs.edit_set(f"{flag.entry.name}, set {flag.set.position}",
                                               flag.set.weight, flag.set.reps)
            if answer is None:
                return None
            weight, reps = answer
        return self.win.run_action("Edit set", lambda: self.ctl.edit_flag(keys[0], weight, reps))

    # --- similar exercises --------------------------------------------------

    def _group_index(self, index):
        if index is not None:
            return index
        row = selected_row(self.groups_view)
        return row["index"] if row else None

    def select_group(self, contains: str) -> int:
        for i, r in enumerate(self.groups_model.rows):
            if contains in r["names"]:
                self.groups_view.selectRow(i)
                return r["index"]
        raise KeyError(contains)

    def _group_selected(self, *_):
        index = self._group_index(None)
        self._show_detail(None if index is None else self.ctl.candidate_detail(index))

    def _kept_changed(self, name: str):
        if self.detail is not None and name and name != self.detail.kept:
            self._show_detail(self.ctl.candidate_detail(self._group_index(None), name))

    def _show_detail(self, detail):
        self.detail = detail
        for b in (self.combine_button, self.offset_button, self.separate_button):
            b.setEnabled(detail is not None)
        while self.adjust_form.rowCount():
            self.adjust_form.removeRow(0)
        self.adjust_edits = {}
        if detail is None:
            self.tier_label.setText("No similar exercises to review.")
            self.members_model.set_rows([])
            self.kept_combo.blockSignals(True)
            self.kept_combo.clear()
            self.kept_combo.blockSignals(False)
            self.suggestion_label.setText("")
            return
        from ..similarity import describe_sets
        self.tier_label.setText(TIER_TEXT[detail.group.tier])
        self.members_model.set_rows([
            {"name": m.name, "sessions": m.sessions, "span": m.span,
             "last": describe_sets(m.sets[-1:]) if m.sets else ""} for m in detail.members])
        fit_columns(self.members_view)
        self.kept_combo.blockSignals(True)
        self.kept_combo.clear()
        self.kept_combo.addItems(detail.group.names)
        self.kept_combo.setCurrentText(detail.kept)
        self.kept_combo.blockSignals(False)
        texts = []
        for s in detail.suggestions.values():
            texts.append(s.relation)
            if s.kept_sets and s.adjusted_sets and detail.offset_allowed:
                compared = "<br>".join(f"&nbsp;&nbsp;{html.escape(line)}" for line in s.compared)
                texts.append(f"<span style='color:{MUTED}'>Compared top sets:<br>{compared}</span>")
                texts.append(s.summary)
        self.suggestion_label.setText("<br>".join(texts))
        self.offset_button.setEnabled(detail.offset_allowed)
        if detail.offset_allowed:
            for name in detail.suggestions:
                edit = QLineEdit(detail.suggested_text(name))
                edit.setPlaceholderText("lb like -50, or a factor like x0.75")
                edit.setMaximumWidth(220)
                self.adjust_edits[name] = edit
                self.adjust_form.addRow(f"Adjustment for {name}:", edit)

    def combine(self, with_offset: bool, index: int | None = None, kept: str | None = None,
                adjustments: dict[str, str] | None = None) -> str | None:
        index = self._group_index(index)
        if index is None:
            return None
        kept = kept or self.kept_combo.currentText() or None
        if kept is None:
            kept = self.ctl.candidate_detail(index).kept
        if with_offset and adjustments is None:
            adjustments = {name: e.text() for name, e in self.adjust_edits.items()}
        if not with_offset:
            adjustments = {}
        return self.win.run_action("Combine exercises", lambda: self.ctl.combine(index, kept, adjustments))

    def keep_separate(self, index: int | None = None) -> str | None:
        index = self._group_index(index)
        if index is None:
            return None
        return self.win.run_action("Keep separate", lambda: self.ctl.keep_separate(index))


class ChartsTab(Tab):
    """Every ``fitness plot`` chart, with a picker and Save as…"""

    def __init__(self, win):
        super().__init__(win)
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Chart"))
        self.chart_combo = QComboBox()
        for spec in charts.CHARTS:
            self.chart_combo.addItem(spec.label, spec.key)
        self.chart_combo.currentIndexChanged.connect(lambda _: self._changed())
        bar.addWidget(self.chart_combo)
        bar.addSpacing(16)
        self.exercise_label = QLabel("Exercise")
        bar.addWidget(self.exercise_label)
        self.exercise_combo = QComboBox()
        self.exercise_combo.setEditable(True)
        self.exercise_combo.setInsertPolicy(QComboBox.NoInsert)
        self.exercise_combo.setMinimumWidth(300)
        completer = self.exercise_combo.completer()
        completer.setFilterMode(Qt.MatchContains)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self.exercise_combo.currentIndexChanged.connect(lambda _: self._changed(exercise=True))
        bar.addWidget(self.exercise_combo, 1)
        bar.addStretch(1)
        self.save_button = QPushButton("Save as…")
        self.save_button.setToolTip("Save this chart as PNG, SVG or PDF (the same file `fitness plot` makes)")
        self.save_button.clicked.connect(lambda: self.save())
        bar.addWidget(self.save_button)
        layout.addLayout(bar)
        self.chart = ChartView()
        layout.addWidget(self.chart, 1)
        self.dirty = True

    @property
    def key(self) -> str:
        return self.chart_combo.currentData()

    @property
    def spec(self) -> charts.ChartSpec:
        return charts.SPECS[self.key]

    @property
    def exercise(self) -> str | None:
        text = self.exercise_combo.currentText().strip()
        return text if text and self.exercise_combo.findText(text) >= 0 else None

    def refresh(self):
        current = self.exercise_combo.currentText() or self.win.exercises.current
        self.exercise_combo.blockSignals(True)
        self.exercise_combo.clear()
        self.exercise_combo.addItems(self.ctl.exercise_names())
        if current and self.exercise_combo.findText(current) >= 0:
            self.exercise_combo.setCurrentText(current)
        self.exercise_combo.blockSignals(False)
        self._enable()
        self.dirty = True
        if self.win.tabs.currentWidget() is self:
            self.render()

    def shown(self):
        if self.dirty:
            self.render()

    def follow_exercise(self, name: str) -> None:
        """The Exercises tab's selection becomes this tab's exercise."""
        if self.exercise_combo.findText(name) < 0 or name == self.exercise_combo.currentText():
            return
        self.exercise_combo.blockSignals(True)
        self.exercise_combo.setCurrentText(name)
        self.exercise_combo.blockSignals(False)
        if self.spec.needs_exercise:
            self.dirty = True

    def set_chart(self, key: str, exercise: str | None = None) -> None:
        """Pick a chart (and exercise) and draw it."""
        self.chart_combo.blockSignals(True)
        self.chart_combo.setCurrentIndex(self.chart_combo.findData(key))
        self.chart_combo.blockSignals(False)
        if exercise is not None:
            self.follow_exercise(exercise)
        self._enable()
        self.render()

    def _enable(self):
        needs = self.spec.needs_exercise
        self.exercise_label.setEnabled(needs)
        self.exercise_combo.setEnabled(needs)

    def _changed(self, exercise: bool = False):
        self._enable()
        if exercise and not self.spec.needs_exercise:
            return
        self.render()

    def _figure(self):
        return charts.build(self.key, self.ctl.snap, exercise=self.exercise, today=self.ctl.today,
                            formula=self.ctl.formula)

    def render(self) -> None:
        self.chart.show_figure(self._figure(), self.spec.min_scale, scale=self.spec.scale)
        self.dirty = False

    def save(self, path: str | None = None):
        """Save the current chart at its natural size, as ``fitness plot --out`` would. Returns the path."""
        path = path or self.win.dialogs.save_file(
            "Save chart", charts.default_filename(self.key, self.exercise, self.ctl.today))
        if not path:
            return None
        try:
            saved = charts.save(self._figure(), path)
        except (ValueError, OSError) as exc:
            self.win.dialogs.error("Save chart", str(exc))
            return None
        self.win.statusBar().showMessage(f"Saved {saved}", 10000)
        return saved


class MusclesTab(Tab):
    """Weekly hard sets per muscle (``fitness muscles``) and the sets-per-muscle chart."""

    def __init__(self, win):
        super().__init__(win)
        layout = QVBoxLayout(self)
        layout.addWidget(muted(
            f"Hard sets a week (normal, failure and drop sets; 1 per set for primary muscles, 0.5 for "
            f"secondary). <span style='background:{LOW_BG}'>&nbsp;Under {LOW_SETS}&nbsp;</span> "
            f"<span style='background:{HIGH_BG}'>&nbsp;Over {HIGH_SETS}&nbsp;</span> "
            f"(a common hypertrophy range). * this week is still in progress."))
        split = QSplitter(Qt.Vertical)
        self.model = RowTableModel([Column("Muscle", "label")])
        self.view = make_table(self.model, sortable=False, stretch=None)
        split.addWidget(self.view)
        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.note = muted()
        row.addWidget(self.note, 1)
        row.addWidget(QLabel("Chart"))
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("Heatmap", "heatmap")
        self.kind_combo.addItem("Lines", "lines")
        self.kind_combo.currentIndexChanged.connect(lambda _: self.render())
        row.addWidget(self.kind_combo)
        bv.addLayout(row)
        self.chart = ChartView()
        bv.addWidget(self.chart, 1)
        split.addWidget(bottom)
        split.setSizes([330, 380])
        layout.addWidget(split, 1)
        self.periods: list = []
        self.dirty = True

    def refresh(self):
        periods, rows = self.ctl.muscle_table()
        self.periods = periods
        columns = [Column("Muscle", "label")]
        for p in periods:
            partial = any(r[("status", p)] == "partial" for r in rows)
            columns.append(Column(
                f"{p.start_time:%b %d}" + ("*" if partial else ""), p, align=RIGHT,
                text=lambda r, p=p: f"{r[p]:g}",
                background=lambda r, p=p: {"low": LOW_BG, "high": HIGH_BG}.get(r[("status", p)]),
                color=lambda r, p=p: MUTED if r[("status", p)] in ("zero", "partial") else None,
                tooltip=lambda r, p=p: f"{r['label']}, week of {p.start_time:%Y-%m-%d}: {r[p]:g} hard sets"))
        columns.append(num_col("Avg", "avg", "{:.1f}"))
        self.model = RowTableModel(columns, rows)
        self.view.setModel(self.model)
        self.view.resizeColumnsToContents()
        notes = self.ctl.muscle_notes()
        self.note.setText(" ".join(notes) if rows else
                          "No hard sets for exercises with known muscles yet.")
        self.dirty = True
        if self.win.tabs.currentWidget() is self:
            self.render()

    def shown(self):
        if self.dirty:
            self.render()

    def render(self) -> None:
        self.chart.show_figure(charts.muscles_figure(self.ctl.snap.muscle_sets, self.kind_combo.currentData()),
                               min_scale=0.7)
        self.dirty = False


class BodyweightTab(Tab):
    def __init__(self, win):
        super().__init__(win)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        top.addWidget(self.summary, 1)
        self.import_button = QPushButton("Import Apple Health export…")
        self.import_button.clicked.connect(lambda: self.win.import_health())
        top.addWidget(self.import_button, 0, Qt.AlignTop)
        layout.addLayout(top)
        split = QSplitter(Qt.Vertical)
        self.chart = ChartView(min_height=180)
        split.addWidget(self.chart)
        self.model = RowTableModel([
            date_col("Date", "date", "%a %Y-%m-%d"), num_col("Weight", "lb", "{:,.1f} lb"),
            num_col("Change", "change", "{:+.1f}"), Column("Source", "source"),
        ])
        self.view = make_table(self.model)
        split.addWidget(self.view)
        split.setSizes([320, 320])
        layout.addWidget(split, 1)
        self.dirty = True

    def refresh(self):
        self.summary.setText(self.ctl.bodyweight_summary())
        self.model.set_rows(self.ctl.bodyweight_rows())
        self.view.sortByColumn(0, Qt.DescendingOrder)
        fit_columns(self.view)
        self.dirty = True
        if self.win.tabs.currentWidget() is self:
            self.render()

    def shown(self):
        if self.dirty:
            self.render()

    def render(self) -> None:
        self.chart.show_figure(charts.bodyweight_figure(self.ctl.snap.bodyweight_days), min_scale=0.45)
        self.dirty = False


class WelcomePage(QWidget):
    """Shown instead of the tabs while the database has no workouts."""

    def __init__(self, win: MainWindow):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        box = QFrame()
        box.setObjectName("welcome")
        box.setMaximumWidth(640)
        box.setStyleSheet("#welcome { background: palette(base); border: 1px solid #dcdbd6; border-radius: 8px; }")
        v = QVBoxLayout(box)
        v.setContentsMargins(28, 24, 28, 24)
        title = QLabel("Welcome to Fitness Aggregator")
        title.setStyleSheet("font-size: 17pt; font-weight: 600;")
        v.addWidget(title)
        steps = QLabel(
            "<p>There are no workouts in this database yet. To get started:</p><ol>"
            "<li>In the <b>Strong</b> app, open <i>Settings → Export Strong Data</i> and save or send "
            "yourself the CSV file.</li>"
            "<li>Click <b>Import Strong export…</b> below (or press Ctrl+I) and pick that file. You'll see "
            "what it contains before anything is stored.</li>"
            "<li>Optional: import bodyweight from an <b>Apple Health</b> export (<i>Health → your profile → "
            "Export All Health Data</i>), so assisted lifts use your real weight.</li></ol>"
            "<p>Import a newer export whenever you like: only new and edited workouts change, and every "
            "correction stays reversible.</p>")
        steps.setWordWrap(True)
        steps.setTextFormat(Qt.RichText)
        v.addWidget(steps)
        self.db_label = muted()
        v.addWidget(self.db_label)
        buttons = QHBoxLayout()
        self.import_button = QPushButton("Import Strong export…")
        self.import_button.setDefault(True)
        self.import_button.setStyleSheet("font-weight: 600; padding: 6px 14px;")
        self.import_button.clicked.connect(lambda: win.import_export())
        self.health_button = QPushButton("Import Apple Health export…")
        self.health_button.clicked.connect(lambda: win.import_health())
        buttons.addWidget(self.import_button)
        buttons.addWidget(self.health_button)
        buttons.addStretch(1)
        v.addLayout(buttons)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(box, 3)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(2)

    def refresh(self, controller: Controller) -> None:
        days = len(controller.snap.bodyweight_days)
        text = f"Database: {controller.store.path}"
        if days:
            text += f" · {days:,} days of bodyweight stored"
        self.db_label.setText(text)


# --- background work ------------------------------------------------------------------------------

class _TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(object)


class _Task(QRunnable):
    """Runs ``fn`` on a pool thread and reports back through queued signals."""

    def __init__(self, fn: Callable[[], object]):
        super().__init__()
        self.fn = fn
        self.signals = _TaskSignals()

    def run(self):
        try:
            value = self.fn()
        except BaseException as exc:  # reported on the UI thread
            self.signals.failed.emit(exc)
        else:
            self.signals.done.emit(value)


# --- the window -------------------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """The main window.

    ``background`` (default True) loads data on a worker thread after imports and
    actions, with a busy indicator; tests pass False to run everything in order.
    ``settings`` is a QSettings to remember the window size and last tab in;
    None (the default, and in tests) remembers nothing.
    """

    refreshed = Signal()

    def __init__(self, controller: Controller, dialogs: Dialogs | None = None, *, background: bool = True,
                 settings: QSettings | None = None):
        super().__init__()
        self.controller = controller
        self.dialogs = dialogs or Dialogs(self)
        self.background = background
        self.settings = settings
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self._tasks: set[_Task] = set()
        self._generation = 0
        self.setWindowTitle(f"Fitness Aggregator — {controller.store.path}")
        self.resize(1280, 800)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.overview = OverviewTab(self)
        self.workouts = WorkoutsTab(self)
        self.exercises = ExercisesTab(self)
        self.prs = PRsTab(self)
        self.charts = ChartsTab(self)
        self.muscles = MusclesTab(self)
        self.bodyweight = BodyweightTab(self)
        self.corrections = CorrectionsTab(self)
        self.review = ReviewTab(self)
        self.all_tabs = (self.overview, self.workouts, self.exercises, self.prs, self.charts, self.muscles,
                         self.bodyweight, self.corrections, self.review)
        for tab, name in zip(self.all_tabs, ("Overview", "Workouts", "Exercises", "PRs", "Charts", "Muscles",
                                             "Bodyweight", "Corrections", "Review"), strict=True):
            tab.setObjectName(name.lower())
            self.tabs.addTab(tab, name)
        self.tabs.currentChanged.connect(self._tab_changed)

        self.welcome = WelcomePage(self)
        self.loading = QLabel("Loading your training data…")
        self.loading.setAlignment(Qt.AlignCenter)
        self.loading.setStyleSheet(f"color: {MUTED}; font-size: 12pt;")
        self.stack = QStackedWidget()
        for page in (self.loading, self.welcome, self.tabs):
            self.stack.addWidget(page)
        self.setCentralWidget(self.stack)

        self.busy = QProgressBar()
        self.busy.setRange(0, 0)  # indeterminate
        self.busy.setMaximumWidth(140)
        self.busy.setMaximumHeight(14)
        self.busy.setTextVisible(False)
        self.busy.hide()
        self.statusBar().addPermanentWidget(self.busy)

        self.actions: dict[str, QAction] = {}
        self._menus()
        self._restore_settings()
        self.refresh()

    # --- menus and shortcuts ------------------------------------------------------------

    def _action(self, menu, key: str, text: str, slot, shortcuts=()) -> QAction:
        action = QAction(text, self)
        if shortcuts:
            action.setShortcuts([QKeySequence(s) for s in shortcuts])
        action.triggered.connect(slot)
        menu.addAction(action)
        self.actions[key] = action
        return action

    def _menus(self):
        file = self.menuBar().addMenu("&File")
        self._action(file, "import", "&Import Strong export…", lambda: self.import_export(),
                     ("Ctrl+I", QKeySequence(QKeySequence.Open).toString()))
        self._action(file, "import_health", "Import Apple &Health export…", lambda: self.import_health(),
                     ("Ctrl+Shift+I",))
        file.addSeparator()
        self._action(file, "save_chart", "&Save chart as…", lambda: self.save_chart(), ("Ctrl+S",))
        self._action(file, "refresh", "&Refresh", lambda: self.refresh(), ("F5",))
        file.addSeparator()
        self._action(file, "quit", "&Quit", self.close, (QKeySequence(QKeySequence.Quit).toString() or "Ctrl+Q",))
        edit = self.menuBar().addMenu("&Edit")
        self._action(edit, "find", "&Find exercise…", lambda: self.find_exercise(), ("Ctrl+F",))
        help_menu = self.menuBar().addMenu("&Help")
        self._action(help_menu, "about", "&About Fitness Aggregator", lambda: self.show_about())

    def find_exercise(self) -> None:
        if self.stack.currentWidget() is not self.tabs:
            return
        self.tabs.setCurrentWidget(self.exercises)
        self.exercises.search.setFocus(Qt.ShortcutFocusReason)
        self.exercises.search.selectAll()

    def save_chart(self):
        """Save the Charts tab's chart (switching to it first)."""
        if self.stack.currentWidget() is not self.tabs:
            return None
        self.tabs.setCurrentWidget(self.charts)
        return self.charts.save()

    def show_about(self) -> None:
        self.dialogs.about("About Fitness Aggregator", self.controller.about_text())

    def exercise_selected(self, name: str) -> None:
        self.charts.follow_exercise(name)

    def _tab_changed(self, index: int) -> None:
        tab = self.tabs.widget(index)
        if hasattr(tab, "shown") and self.controller.snapshot is not None:
            tab.shown()

    # --- settings -------------------------------------------------------------------------------

    def _restore_settings(self) -> None:
        if self.settings is None:
            return
        geometry = self.settings.value("window/geometry")
        if isinstance(geometry, QByteArray) and not geometry.isEmpty():
            self.restoreGeometry(geometry)
        name = self.settings.value("window/tab")
        for tab in self.all_tabs:
            if tab.objectName() == name:
                self.tabs.setCurrentWidget(tab)

    def _save_settings(self) -> None:
        if self.settings is None:
            return
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/tab", self.tabs.currentWidget().objectName())
        self.settings.sync()

    # --- background tasks ---------------------------------------------------------------------

    @property
    def is_busy(self) -> bool:
        return bool(self._tasks)

    def run_task(self, label: str, fn: Callable[[], object], done: Callable[[object], object],
                 failed: Callable[[BaseException], object]):
        """Run ``fn`` off the UI thread, then ``done(value)`` or ``failed(exc)`` on it.

        In synchronous mode (``background=False``) everything runs now and the
        callback's return value is returned; otherwise this returns None at once.
        """
        if not self.background:
            try:
                value = fn()
            except Exception as exc:
                return failed(exc)
            return done(value)
        task = _Task(fn)
        self._tasks.add(task)
        self.busy.show()
        self.statusBar().showMessage(label)

        def finish(callback, arg):
            self._tasks.discard(task)
            if not self._tasks:
                self.busy.hide()
            callback(arg)

        task.signals.done.connect(lambda value: finish(done, value))
        task.signals.failed.connect(lambda exc: finish(failed, exc))
        task.setAutoDelete(False)  # self._tasks owns it, so the signals object outlives run()
        self.pool.start(task)
        return None

    def wait_until_idle(self, timeout: float = 30.0) -> bool:
        """Process events until no background task is running (for tests). True if idle."""
        deadline = time.monotonic() + timeout
        while self._tasks and time.monotonic() < deadline:
            self.pool.waitForDone(20)
            QApplication.processEvents()
        return not self._tasks

    # --- refreshing -----------------------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload everything from the database: on a worker thread in background mode."""
        self._generation += 1
        generation = self._generation
        if not self.background or not self.controller.can_load_elsewhere:
            self.controller.reload()
            self._populate()
            return

        def loaded(snapshot):
            if generation == self._generation:  # a newer refresh supersedes this one
                self.controller.set_snapshot(snapshot)
                self._populate()

        def failed(exc):
            if generation == self._generation:
                self.statusBar().showMessage("Couldn't load the data.")
                self.dialogs.error("Refresh", f"Couldn't load the data: {exc}")

        self.run_task("Loading…", self.controller.load_snapshot, loaded, failed)

    def _populate(self) -> None:
        snap = self.controller.snap
        if snap.is_empty:
            self.welcome.refresh(self.controller)
            self.stack.setCurrentWidget(self.welcome)
            self.statusBar().showMessage("No workouts yet. Import a Strong export to begin.")
            self.refreshed.emit()
            return
        self.stack.setCurrentWidget(self.tabs)
        for tab in self.all_tabs:
            tab.refresh()
        review = self.tabs.indexOf(self.review)
        self.tabs.setTabText(review, f"Review ({len(snap.flags) + len(snap.candidates)})"
                             if snap.flags or snap.candidates else "Review")
        self.statusBar().showMessage(
            f"{len(snap.sessions)} sessions · {len(snap.summary)} exercises · "
            f"{sum(c.status == corr.ACTIVE for c in snap.corrections)} active corrections")
        self.refreshed.emit()

    def show_review(self, part: str) -> None:
        self.tabs.setCurrentWidget(self.review)
        (self.review.flags_view if part == "flags" else self.review.groups_view).setFocus()

    def run_action(self, title: str, action: Callable[[], str]) -> str | None:
        """Run a Store action, report the result, and reload everything.

        Refused while a reload is still running: the rows on screen may be out of date."""
        if self.is_busy:
            self.statusBar().showMessage("Still loading, try again in a moment.", 5000)
            return None
        try:
            message = action()
        except (ValueError, KeyError) as exc:
            self.dialogs.error(title, str(exc.args[0] if exc.args else exc))
            return None
        self.refresh()
        if message:
            self.dialogs.info(title, message)
        return message

    # --- importing -------------------------------------------------------------------------------------

    def import_export(self, path: str | None = None) -> str | None:
        """File → Import Strong export. The file is parsed off the UI thread; the
        questions and the import itself run on it. Returns the final message in
        synchronous mode."""
        path = path or self.dialogs.open_file()
        if not path:
            return None
        title = "Import Strong export"

        def failed(exc):
            if not isinstance(exc, (StrongParseError, OSError, ValueError)):
                return self._unexpected(title, exc)
            self.statusBar().showMessage("Import failed.", 8000)
            self.dialogs.error(title, f"{path}: {exc}")
            return None

        return self.run_task(f"Reading {path}…", lambda: self.controller.parse_export(path),
                             lambda parsed: self._continue_import(title, self.controller.plan_parsed(parsed, path)),
                             failed)

    def _continue_import(self, title: str, plan) -> str | None:
        summary = describe_plan(plan)
        force = False
        if plan.is_older:
            if not self.dialogs.confirm(
                    title, f"{summary}\n\nThis export's latest workout ({plan.file_latest:%Y-%m-%d %H:%M}) is "
                    f"older than what's stored ({plan.stored_latest:%Y-%m-%d %H:%M}). Importing it would look "
                    "like recent workouts were deleted. Import anyway?", ok="Import older export"):
                return None
            force = True
        if not plan.has_changes:
            self.dialogs.info(title, summary)
            return summary
        if plan.removed:
            question = (f"{summary}\n\nDrop {len(plan.removed)} workout(s) that are no longer in the export?")
            if not self.dialogs.confirm(title, question, ok="Import and drop them"):
                return None
        elif not force and not self.dialogs.confirm(title, summary, ok="Import"):
            return None
        try:
            _, message = self.controller.apply_import(plan, allow_removals=True, force=force)
        except (ImportRefused, ValueError) as exc:
            self.dialogs.error(title, str(exc))
            return None
        self.refresh()
        self.dialogs.info(title, message)
        return message

    def import_health(self, path: str | None = None) -> str | None:
        """File → Import Apple Health export: the same plan / confirm / apply flow as
        ``fitness import-health``. Asks before dropping days missing from the export."""
        path = path or self.dialogs.open_health_file()
        if not path:
            return None
        title = "Import Apple Health export"

        def failed(exc):
            if not isinstance(exc, HEALTH_ERRORS + (ValueError,)):
                return self._unexpected(title, exc)
            self.statusBar().showMessage("Import failed.", 8000)
            self.dialogs.error(title, f"{path}: {exc}")
            return None

        return self.run_task(f"Reading {path}…", lambda: self.controller.parse_health(path),
                             lambda result: self._continue_health(title, result, path), failed)

    def _continue_health(self, title: str, result, path: str) -> str | None:
        plan = self.controller.plan_health(result, path)
        summary = self.controller.describe_health_plan(result, plan)
        if not plan.has_changes:
            self.dialogs.info(title, summary)
            return summary
        if plan.removed:
            question = (f"{summary}\n\nDrop {len(plan.removed)} day(s) of bodyweight that are no longer "
                        "in the export?")
            if not self.dialogs.confirm(title, question, ok="Import and drop them"):
                return None
        elif not self.dialogs.confirm(title, summary, ok="Import"):
            return None
        try:
            message = self.controller.apply_health_import(plan)
        except ValueError as exc:
            self.dialogs.error(title, str(exc))
            return None
        self.refresh()
        self.dialogs.info(title, message)
        return message

    def _unexpected(self, title: str, exc: BaseException):
        """A bug, not a bad file: raise it in synchronous mode (tests); say so in the app."""
        if not self.background:
            raise exc
        self.dialogs.error(title, f"Something went wrong: {type(exc).__name__}: {exc}")
        return None

    def closeEvent(self, event):
        self._save_settings()
        self.pool.waitForDone(10000)
        self._tasks.clear()
        self.controller.close()
        super().closeEvent(event)
