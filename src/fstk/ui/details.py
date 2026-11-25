"""Details tab: every value recorded for the episode (the manifest), grouped and searchable.

Unset fields are hidden unless "Show empty fields" is ticked; each row shows a readable name (the key is in the
tooltip, with the field's meaning) and who set it and when ("fstk · dvd-alignment · 24 Nov 22:41").
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fstk.ui.colours import tone

SECTION_NAMES = {"mode": "Choices", "stack": "Stack", "audio": "Audio", "video": "Video", "quality": "Quality",
                 "qc": "QC", "retime": "Retime", "deliverable": "Delivery", "episode": "Episode",
                 "subs": "Subtitles", "dvd": "DVD"}


def readable(key: str) -> str:
    """``stack.fell_back_to_mean`` → "Fell back to mean"; known short words keep their capitals."""
    part = key.split(".", 1)[-1].replace(".", " ").replace("_", " ")
    words = [w.upper() if w in ("dvd", "ld", "av", "qc", "efm", "dar", "sar", "ppm", "ncc", "md5", "srt", "pad")
             else w for w in part.split()]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def set_by(row) -> str:
    """"fstk · dvd-ld-audio · 24 Nov 22:52" (local time) from the field's provenance."""
    if not row.set:
        return ""
    who = row.origin or ""
    by = row.by or ""
    if by.startswith("stage:"):
        by = by.removeprefix("stage:")
    parts = [p for p in (who, by) if p]
    when = ""
    if getattr(row, "utc", ""):
        try:
            when = datetime.fromisoformat(row.utc).astimezone().strftime("%d %b %H:%M")
        except ValueError:
            when = ""
    return " · ".join(parts + ([when] if when else []))


class DetailsPanel(QWidget):
    COLUMNS = ("Field", "Value", "Unit", "Set by")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search fields and values…")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Type part of a field's name or value to list only those fields.")
        self.search.textChanged.connect(self._fill)
        self.show_empty = QCheckBox("Show empty fields")
        self.show_empty.setToolTip("Also list the fields nothing has set yet for this episode.")
        self.show_empty.toggled.connect(self._fill)
        self.count = QLabel("")
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setToolTip("Every value recorded for this episode, and who or what set it. Hover a field for "
                              "its meaning and key.")
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.show_empty)
        top.addWidget(self.count)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)

    def set_rows(self, rows) -> None:
        self.rows = list(rows)
        self._fill()

    def _visible(self) -> list:
        needle = self.search.text().strip().lower()
        out = []
        for r in self.rows:
            if not r.set and not self.show_empty.isChecked():
                continue
            if needle and needle not in f"{r.key} {readable(r.key)} {r.value}".lower():
                continue
            out.append(r)
        return out

    def _fill(self) -> None:
        from fstk.ui.services import describe_field

        rows = self._visible()
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r.key.split(".", 1)[0], []).append(r)
        self.table.clearSpans()
        self.table.setRowCount(0)
        self.table.setRowCount(sum(len(g) + 1 for g in groups.values()))
        i = 0
        bold = QFont()
        bold.setBold(True)
        for section, items in groups.items():
            head = QTableWidgetItem(SECTION_NAMES.get(section, section.title()))
            head.setFont(bold)
            head.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(i, 0, head)
            for j in range(1, len(self.COLUMNS)):
                self.table.setItem(i, j, QTableWidgetItem(""))
            self.table.setSpan(i, 0, 1, len(self.COLUMNS))
            i += 1
            for r in items:
                label = readable(r.key)
                if r.unit and label.lower().endswith(" " + r.unit.lower()):
                    label = label[:-len(r.unit) - 1]          # "Skip s" with unit s → "Skip"
                cells = (label, r.value, r.unit, set_by(r))
                for j, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, r.key)
                    if j == 0:
                        try:
                            item.setToolTip(f"{r.key}\n{describe_field(r.key)}")
                        except Exception:  # an unknown key still shows
                            item.setToolTip(r.key)
                    elif j == 1:
                        item.setToolTip(r.value)
                    if not r.set:
                        item.setForeground(tone("quiet"))
                    self.table.setItem(i, j, item)
                i += 1
        self.table.resizeColumnToContents(0)
        self.table.setColumnWidth(3, max(self.table.columnWidth(3), 240))
        shown = len(rows)
        total = sum(1 for r in self.rows if r.set)
        self.count.setText(f"{shown} shown · {total} set of {len(self.rows)}")

    def rowCount(self) -> int:              # the number of fields listed (section headings not counted)
        return len(self._visible())
