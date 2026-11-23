"""Progress panel: overall bar, current step, and a per-step checklist with its own bars and timings."""
from __future__ import annotations

from typing import ClassVar

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from fstk.workflow.progress import ProgressTracker, Snapshot, StepProgress, format_duration, is_space_block

STATUS_TEXT = {"pending": "waiting", "running": "running", "passed": "done", "failed": "FAILED",
               "blocked": "blocked", "awaiting-review": "needs your review", "stale": "out of date",
               "cancelled": "cancelled", "interrupted": "pending"}
STATUS_COLOUR = {"passed": "#2e7d32", "failed": "#c62828", "blocked": "#ef6c00", "awaiting-review": "#1565c0",
                 "running": "#6a1b9a", "stale": "#ef6c00", "cancelled": "#616161", "interrupted": "#ef6c00"}
SCALE = 1000


class ProgressPanel(QWidget):
    space_refresh = Signal()            # the Refresh icon on a step blocked by disk space was clicked

    COLUMNS: ClassVar[list[str]] = ["Step", "Status", "Progress", "Time", "", "What it is doing"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tracker: ProgressTracker | None = None
        self.headline = QLabel("No run yet")
        self.headline.setStyleSheet("font-size: 14px; font-weight: 600;")
        self.overall = QProgressBar()
        self.overall.setRange(0, SCALE)
        self.overall.setTextVisible(True)
        self.detail = QLabel("")
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(4, 34)
        head.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.refresh_buttons: dict[str, QToolButton] = {}
        self.bars: dict[str, QProgressBar] = {}
        lay = QVBoxLayout(self)
        lay.addWidget(self.headline)
        lay.addWidget(self.overall)
        lay.addWidget(self.detail)
        lay.addWidget(self.table, 1)
        self.table.setToolTip("Every step of the plan: its status, progress, time and what it is doing now.")

    # ------------------------------------------------------------------ data
    def set_tracker(self, tracker: ProgressTracker | None) -> None:
        self.tracker = tracker
        self.bars.clear()
        self.refresh_buttons.clear()
        self.table.setRowCount(0)
        if tracker is None:
            self.headline.setText("No run yet")
            self.overall.setValue(0)
            self.detail.setText("")
            return
        self.table.setRowCount(len(tracker.steps))
        for row, step in enumerate(tracker.steps.values()):
            self.table.setItem(row, 0, QTableWidgetItem(step.id))
            bar = QProgressBar()
            bar.setRange(0, SCALE)
            bar.setTextVisible(True)
            self.table.setCellWidget(row, 2, bar)
            self.bars[step.id] = bar
            btn = QToolButton()
            btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
            btn.setAutoRaise(True)
            btn.setToolTip("Re-check free disk space now (applies to every step blocked by disk space)")
            btn.clicked.connect(self.space_refresh.emit)
            btn.setVisible(False)
            # the button sits in a plain container: the table force-shows its direct cell widgets on every layout
            # pass, which would defeat hiding the icon on rows that are not blocked
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.addWidget(btn)
            self.table.setCellWidget(row, 4, holder)
            self.refresh_buttons[step.id] = btn
        self.refresh()

    def refresh(self) -> Snapshot | None:
        if self.tracker is None:
            return None
        snap = self.tracker.snapshot()
        self.headline.setText(snap.headline())
        self.overall.setValue(round(snap.overall * SCALE))
        self.overall.setFormat(f"{snap.overall:.0%}   ({snap.done} of {snap.total} steps)")
        parts = [f"elapsed {format_duration(snap.elapsed)}"]
        if snap.state == "running" and snap.overall_eta is not None:
            parts.append(f"about {format_duration(snap.overall_eta)} left (rough)")
        elif snap.state == "running":
            parts.append("time left: not known yet (needs timings from earlier runs)")
        if snap.paused:
            parts.append("PAUSED: tools are frozen and resume exactly where they stopped")
        self.detail.setText("   ·   ".join(parts))
        for row, step in enumerate(snap.steps):
            self._row(row, step, snap)
        return snap

    def _row(self, row: int, step: StepProgress, snap: Snapshot) -> None:
        text = STATUS_TEXT.get(step.status, step.status)
        if step.status == "running" and snap.paused:
            text = "paused"
        status = QTableWidgetItem(text)
        colour = STATUS_COLOUR.get(step.status)
        if colour:
            status.setForeground(QColor(colour))
        self.table.setItem(row, 1, status)
        bar = self.bars[step.id]
        if step.status == "running" and step.fraction is None and not snap.paused:
            bar.setRange(0, 0)                                   # indeterminate: no honest percentage exists
            bar.setFormat("working…")
        else:
            bar.setRange(0, SCALE)
            frac = step.credit() if step.status != "pending" else 0.0
            bar.setValue(round(frac * SCALE))
            bar.setFormat(f"{frac:.0%}" if step.status in ("running", "passed", "awaiting-review") else "")
        now = self.tracker.paused_at if self.tracker and self.tracker.paused_at else self.tracker.clock()
        took = step.elapsed(now)
        label = "" if took is None else format_duration(took)
        eta = self.tracker.step_eta(step.id) if self.tracker else None
        if step.status == "running" and eta is not None and not snap.paused:
            label += f"  (≈{format_duration(eta)} left)"
        self.table.setItem(row, 3, QTableWidgetItem(label))
        self.refresh_buttons[step.id].setVisible(is_space_block(step.status, step.detail))
        msg = step.message if step.status == "running" else step.detail
        item = QTableWidgetItem(msg)
        item.setToolTip(msg)
        self.table.setItem(row, 5, item)
        if step.status in ("failed", "blocked", "stale"):
            self.table.item(row, 0).setForeground(QColor(STATUS_COLOUR["failed"]))
        else:
            self.table.item(row, 0).setData(Qt.ItemDataRole.ForegroundRole, None)
