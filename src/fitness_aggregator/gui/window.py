"""Main window and tabs. Widgets only: data comes from ``Controller``."""

from __future__ import annotations

import json
from typing import Callable

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QSplitter, QTableView, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from .. import corrections as corr
from ..importers import StrongParseError
from ..store import ImportRefused
from . import charts
from .controller import TIER_TEXT, Controller, describe_plan
from .models import RIGHT, STATUS_COLORS, Column, RowTableModel, date_col, num_col

MUTED = "#52514e"


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


class ChartView(QWidget):
    """Holds one matplotlib canvas; ``show_figure`` swaps in a new figure."""

    def __init__(self):
        super().__init__()
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self.canvas: FigureCanvasQTAgg | None = None
        self.setMinimumHeight(220)

    @property
    def figure(self):
        return self.canvas.figure if self.canvas else None

    def show_figure(self, fig) -> None:
        if self.canvas is not None:
            self._layout.removeWidget(self.canvas)
            self.canvas.setParent(None)
            self.canvas.deleteLater()
        self.canvas = FigureCanvasQTAgg(fig)
        self._layout.addWidget(self.canvas)
        self.canvas.draw_idle()


# --- tabs ------------------------------------------------------------------------------------

class Tab(QWidget):
    def __init__(self, win: "MainWindow"):
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
        self.prs_view = make_table(self.prs_model)
        lv.addWidget(self.prs_view)
        body.addWidget(left)
        self.chart = ChartView()
        body.addWidget(self.chart)
        body.setSizes([660, 520])
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
        self.chart.show_figure(charts.weekly_volume_figure(self.ctl.snap.weekly))


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
        rv.addWidget(self.title)
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
        rsplit.setSizes([330, 300])
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
        note = [f"{kind} · {len(hist)} sessions · {len(events)} PRs"]
        note += [f"includes {m.source}" + (f" ({m.label})" if m.label else "") for m in sources]
        self.note.setText("  ·  ".join(note))
        self.history_model.set_rows(self.ctl.history_rows(name))
        self.history_view.sortByColumn(0, Qt.DescendingOrder)
        fit_columns(self.history_view)
        self.chart.show_figure(charts.progress_figure(hist, events, "", kind))


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
        self.bests_view = make_table(self.bests_model)
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
        self.recent_view = make_table(self.recent_model)
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
        self.view.setWordWrap(True)
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
        fit_columns(self.view, max_width=180)
        self.view.resizeRowsToContents()
        self._selected()

    def _selected(self, *_):
        row = selected_row(self.view)
        c = row["correction"] if row else None
        self.revert_button.setEnabled(bool(c) and c.status in (corr.ACTIVE, corr.STALE))
        self.restore_button.setEnabled(bool(c) and c.status == corr.REVERTED)
        if c is None:
            self.detail.setText("Select a correction to revert or restore it.")
            return
        target = ", ".join(f"{k}: {v}" for k, v in c.target.items())
        parts = [f"#{c.id} · {target}"]
        if c.before or c.after:
            parts.append(f"before {json.dumps(c.before, ensure_ascii=False)} → after "
                         f"{json.dumps(c.after, ensure_ascii=False)}")
        parts.append(f"created {c.created_at:%Y-%m-%d %H:%M}, updated {c.updated_at:%Y-%m-%d %H:%M}")
        self.detail.setText("  ·  ".join(parts))

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
        for name, s in detail.suggestions.items():
            texts.append(s.relation)
            if s.kept_sets and s.adjusted_sets and detail.offset_allowed:
                texts.append(f"<span style='color:{MUTED}'>Compared top sets: {detail.kept} "
                             f"{describe_sets(s.kept_sets)} vs {name} {describe_sets(s.adjusted_sets)}.</span>")
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


# --- the window -------------------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, controller: Controller, dialogs: Dialogs | None = None):
        super().__init__()
        self.controller = controller
        self.dialogs = dialogs or Dialogs(self)
        self.setWindowTitle(f"Fitness Aggregator — {controller.store.path}")
        self.resize(1200, 780)

        self.tabs = QTabWidget()
        self.overview = OverviewTab(self)
        self.workouts = WorkoutsTab(self)
        self.exercises = ExercisesTab(self)
        self.prs = PRsTab(self)
        self.corrections = CorrectionsTab(self)
        self.review = ReviewTab(self)
        for tab, name in ((self.overview, "Overview"), (self.workouts, "Workouts"), (self.exercises, "Exercises"),
                          (self.prs, "PRs"), (self.corrections, "Corrections"), (self.review, "Review")):
            self.tabs.addTab(tab, name)
        self.setCentralWidget(self.tabs)
        self._menus()
        self.refresh()

    def _menus(self):
        menu = self.menuBar().addMenu("&File")
        for text, slot, key in (("&Import Strong export…", lambda: self.import_export(), QKeySequence.Open),
                                ("&Refresh", lambda: self.refresh(), QKeySequence.Refresh),
                                ("&Quit", self.close, QKeySequence.Quit)):
            action = QAction(text, self)
            action.setShortcut(key)
            action.triggered.connect(slot)
            menu.addAction(action)
            if text == "&Refresh":
                menu.addSeparator()

    def refresh(self) -> None:
        self.controller.reload()
        for tab in (self.overview, self.workouts, self.exercises, self.prs, self.corrections, self.review):
            tab.refresh()
        snap = self.controller.snap
        self.tabs.setTabText(5, f"Review ({len(snap.flags) + len(snap.candidates)})"
                             if snap.flags or snap.candidates else "Review")
        self.statusBar().showMessage(
            f"{len(snap.sessions)} sessions · {len(snap.summary)} exercises · "
            f"{sum(c.status == corr.ACTIVE for c in snap.corrections)} active corrections")

    def show_review(self, part: str) -> None:
        self.tabs.setCurrentWidget(self.review)
        (self.review.flags_view if part == "flags" else self.review.groups_view).setFocus()

    def run_action(self, title: str, action: Callable[[], str]) -> str | None:
        """Run a Store action, report the result, and reload everything."""
        try:
            message = action()
        except (ValueError, KeyError) as exc:
            self.dialogs.error(title, str(exc.args[0] if exc.args else exc))
            return None
        self.refresh()
        if message:
            self.dialogs.info(title, message)
        return message

    def import_export(self, path: str | None = None) -> str | None:
        path = path or self.dialogs.open_file()
        if not path:
            return None
        title = "Import Strong export"
        try:
            plan = self.controller.plan_import(path)
        except (StrongParseError, OSError) as exc:
            self.dialogs.error(title, f"{path}: {exc}")
            return None
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
        except ImportRefused as exc:
            self.dialogs.error(title, str(exc))
            return None
        self.refresh()
        self.dialogs.info(title, message)
        return message

    def closeEvent(self, event):
        self.controller.close()
        super().closeEvent(event)
