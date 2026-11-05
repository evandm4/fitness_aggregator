"""Qt table models over plain rows (lists of dicts from the controller)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

LEFT = Qt.AlignLeft | Qt.AlignVCenter
RIGHT = Qt.AlignRight | Qt.AlignVCenter

STATUS_COLORS = {"active": "#1a7f37", "reverted": "#a15c00", "stale": "#c62828", "obsolete": "#8a8984"}


def _missing(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or (v is pd.NaT)


def default_text(v) -> str:
    if _missing(v):
        return "—"
    if isinstance(v, pd.Timestamp) or hasattr(v, "strftime"):
        return f"{v:%Y-%m-%d}"
    if isinstance(v, float):
        return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:g}"
    return str(v)


@dataclass
class Column:
    header: str
    key: str
    text: Callable[[dict], str] | None = None   # display text; default formats row[key]
    sort: Callable[[dict], Any] | None = None   # sort key; default row[key]
    align: Qt.AlignmentFlag = LEFT
    color: Callable[[dict], str | None] | None = None
    tooltip: Callable[[dict], str] | None = None

    def display(self, row: dict) -> str:
        return self.text(row) if self.text else default_text(row.get(self.key))

    def sort_key(self, row: dict):
        v = self.sort(row) if self.sort else row.get(self.key)
        # Missing values sort first ascending; keep types comparable.
        if _missing(v):
            return (0, 0)
        if isinstance(v, str):
            return (1, v.lower())
        if hasattr(v, "timestamp"):
            return (1, v.timestamp())
        return (1, v)


def date_col(header: str, key: str, fmt: str = "%Y-%m-%d") -> Column:
    return Column(header, key, text=lambda r: "" if _missing(r.get(key)) else f"{r[key]:{fmt}}")


def num_col(header: str, key: str, fmt: str = "{:,.0f}", sort: Callable | None = None) -> Column:
    return Column(header, key, align=RIGHT, sort=sort,
                  text=lambda r: "—" if _missing(r.get(key)) else fmt.format(r[key]))


class RowTableModel(QAbstractTableModel):
    """A read-only table of dict rows with per-column formatting and sorting."""

    def __init__(self, columns: list[Column], rows: list[dict] | None = None, parent=None):
        super().__init__(parent)
        self.columns = columns
        self._all: list[dict] = []
        self.rows: list[dict] = []
        self._filter: Callable[[dict], bool] | None = None
        self._sort: tuple[int, Qt.SortOrder] | None = None
        self.set_rows(rows or [])

    # --- data ------------------------------------------------------------------

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self._all = list(rows)
        self._apply()
        self.endResetModel()

    def set_filter(self, predicate: Callable[[dict], bool] | None) -> None:
        self.beginResetModel()
        self._filter = predicate
        self._apply()
        self.endResetModel()

    def _apply(self) -> None:
        rows = [r for r in self._all if self._filter is None or self._filter(r)]
        if self._sort is not None:
            col, order = self._sort
            rows.sort(key=self.columns[col].sort_key, reverse=order == Qt.DescendingOrder)
        self.rows = rows

    def row(self, index: int) -> dict:
        return self.rows[index]

    # --- Qt API ----------------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return self.columns[section].header
            if role == Qt.TextAlignmentRole:
                return int(self.columns[section].align)
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = self.rows[index.row()], self.columns[index.column()]
        if role == Qt.DisplayRole:
            return col.display(row)
        if role == Qt.TextAlignmentRole:
            return int(col.align)
        if role == Qt.ForegroundRole and col.color:
            c = col.color(row)
            return QColor(c) if c else None
        if role == Qt.ToolTipRole:
            return col.tooltip(row) if col.tooltip else col.display(row)
        if role == Qt.UserRole:
            return row
        return None

    def sort(self, column: int, order=Qt.AscendingOrder) -> None:
        self.layoutAboutToBeChanged.emit()
        self._sort = (column, order)
        self._apply()
        self.layoutChanged.emit()
