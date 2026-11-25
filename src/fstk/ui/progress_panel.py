"""Progress panel: overall bar, current step, and a per-step checklist with its own bars and timings."""
from __future__ import annotations

from typing import ClassVar

from PySide6.QtCore import QSize, Qt, QTimer, Signal
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

from fstk.ui.step_help import STATUS_HELP, step_tooltip
from fstk.workflow.progress import ProgressTracker, Snapshot, StepProgress, format_duration, is_space_block

STATUS_TEXT = {"pending": "waiting", "running": "running", "passed": "done", "failed": "FAILED",
               "blocked": "blocked", "awaiting-review": "needs your review", "stale": "out of date",
               "cancelled": "cancelled", "interrupted": "pending"}
STATUS_COLOUR = {"passed": "#2e7d32", "failed": "#c62828", "blocked": "#ef6c00", "awaiting-review": "#1565c0",
                 "running": "#6a1b9a", "stale": "#ef6c00", "cancelled": "#616161", "interrupted": "#ef6c00"}
SCALE = 1000


#: steps with nothing to restart: never run, or running now
NOT_STARTED = frozenset({"pending", "waiting", "running", "queued"})


class ProgressPanel(QWidget):
    space_refresh = Signal()            # the Refresh icon on a step blocked by disk space was clicked
    columns_resized = Signal(dict)      # the person dragged a column edge: {column name: width}, to remember
    pack_save = Signal()                # the Save icon beside dvd-probe: save this DVD's opening pack
    restart_from = Signal(str)          # "Restart from here" on a step: its id

    COLUMNS: ClassVar[list[str]] = ["Step", "Status", "Progress", "Time", "", "What it is doing"]
    #: starting widths; every column can be dragged wider or narrower, and the widths are remembered
    WIDTHS: ClassVar[dict[str, int]] = {"Step": 130, "Status": 120, "Progress": 260, "Time": 130, "": 56,
                                        "What it is doing": 360}
    HEADER_TIPS: ClassVar[dict[str, str]] = {
        "Step": "The step. Hover over a step's name to see what it does. Drag a column edge to resize it.",
        "Status": "Where the step is: waiting, running, done, needs your review, blocked or failed.",
        "Progress": "How far the step has got. “working…” means the tool gives no honest percentage.",
        "Time": "How long the step has taken, and roughly how long is left when earlier runs give a guide.",
        "": ("Restart from here (on steps that have run and could start now), and re-check free disk space (on "
             "steps blocked by disk space)."),
        "What it is doing": "What the step is doing right now, or what it reported when it finished.",
    }

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
        # every column is the person's to size: drag an edge (the last one fills what is left over)
        head.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        head.setStretchLastSection(True)
        head.setMinimumSectionSize(24)
        head.setHighlightSections(False)
        for col, name in enumerate(self.COLUMNS):
            self.table.setColumnWidth(col, self.WIDTHS[name])
            self.table.horizontalHeaderItem(col).setToolTip(self.HEADER_TIPS[name])
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(800)          # remember the widths once the dragging stops
        self._resize_timer.timeout.connect(lambda: self.columns_resized.emit(self.column_widths()))
        self._restoring = False
        head.sectionResized.connect(self._on_resized)
        self.refresh_buttons: dict[str, QToolButton] = {}
        self.bars: dict[str, QProgressBar] = {}
        self.save_buttons: dict[str, QToolButton] = {}
        self.restart_buttons: dict[str, QToolButton] = {}
        self.allow_restart = True                    # off while a job runs in this window
        self.pack_target: str | None = None          # where the opening pack would be written (set by the window)
        self.pack_exists = False
        lay = QVBoxLayout(self)
        lay.addWidget(self.headline)
        lay.addWidget(self.overall)
        lay.addWidget(self.detail)
        lay.addWidget(self.table, 1)
        # shown only over the empty part of the table: every cell and header has its own tip
        self.table.setToolTip("Hover over a step for what it does. Drag a column's edge to resize it.")

    # ------------------------------------------------------------------ columns
    def column_widths(self) -> dict[str, int]:
        last = len(self.COLUMNS) - 1                 # the last column fills the rest: not remembered
        return {name: self.table.columnWidth(col) for col, name in enumerate(self.COLUMNS) if col != last}

    def set_column_widths(self, widths: dict | None) -> None:
        """Widths remembered from an earlier session (unknown names and silly values are ignored)."""
        self._restoring = True
        try:
            for col, name in enumerate(self.COLUMNS[:-1]):
                w = (widths or {}).get(name)
                if isinstance(w, int) and 24 <= w <= 2000:
                    self.table.setColumnWidth(col, w)
        finally:
            self._restoring = False

    def _on_resized(self, col: int, _old: int, _new: int) -> None:
        if not self._restoring and col != len(self.COLUMNS) - 1:
            self._resize_timer.start()

    # ------------------------------------------------------------------ data
    def set_tracker(self, tracker: ProgressTracker | None) -> None:
        self.tracker = tracker
        self.bars.clear()
        self.refresh_buttons.clear()
        self.save_buttons.clear()
        self.restart_buttons.clear()
        self.table.setRowCount(0)
        if tracker is None:
            self.headline.setText("No run yet")
            self.overall.setValue(0)
            self.detail.setText("")
            return
        self.table.setRowCount(len(tracker.steps))
        import fstk.stages  # noqa: F401 - registers every stage, for the titles
        from fstk.stages.base import REGISTRY

        for row, step in enumerate(tracker.steps.values()):
            stage = REGISTRY.get(step.stage)
            tip = step_tooltip(step.stage, stage.title if stage else step.id)
            name = QTableWidgetItem(step.id)
            name.setToolTip(tip)
            self.table.setItem(row, 0, name)
            if step.stage == "dvd-probe":
                self._add_save_button(row, step.id)
            bar = QProgressBar()
            bar.setRange(0, SCALE)
            bar.setTextVisible(True)
            bar.setToolTip(tip)
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
            hl.setSpacing(0)
            hl.addWidget(btn)
            again = QToolButton()
            again.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSkipBackward))
            again.setAutoRaise(True)
            again.setAccessibleName(f"Restart from {step.id}")
            again.setToolTip(f"<qt><b>Restart from here</b><br><br>Runs <b>{step.id}</b> again, then the steps "
                             "after it. Its previous result is replaced. A later step is kept only if this step "
                             "gives exactly the same result back; otherwise it runs again too.<br><br>"
                             "You are asked to confirm first.</qt>")
            again.clicked.connect(lambda _=False, sid=step.id: self.restart_from.emit(sid))
            again.setVisible(False)
            hl.addWidget(again)
            hl.addStretch(1)
            self.table.setCellWidget(row, 4, holder)
            self.refresh_buttons[step.id] = btn
            self.restart_buttons[step.id] = again
        self.refresh()

    # ------------------------------------------------------------------ DVD opening pack
    def can_restart(self, step_id: str, status: str) -> bool:
        """The "Restart from here" icon: only for a step that has run (or tried to) and could start now — every
        step it depends on has passed; one held up by unfinished steps before it would only wait again."""
        if not self.allow_restart or self.tracker is None or status in NOT_STARTED:
            return False
        after = next((s.get("after", []) for s in self.tracker.plan["steps"] if s["id"] == step_id), [])
        return all(self.tracker.steps[d].status == "passed" for d in after if d in self.tracker.steps)

    def set_allow_restart(self, allow: bool) -> None:
        """Hide the "Restart from here" icons while a job runs in this window (the state is being written)."""
        self.allow_restart = allow
        self.refresh()

    def set_pack_target(self, target: str | None, exists: bool = False) -> None:
        """Where the Save icon writes this episode's opening pack, and whether one is already there."""
        self.pack_target, self.pack_exists = target, exists
        for btn in self.save_buttons.values():
            btn.setToolTip(self.save_tip())

    def save_tip(self) -> str:
        where = self.pack_target or "the app's packaged packs (src/fstk/data/dvd_openings/<series>/<code>.npz)"
        return ("Save this DVD's opening pack.\n\n"
                "Makes the episode's start findable on the LaserDisc for people who don't have this DVD: the first "
                "seconds of the DVD's picture after its opening black, kept as tiny 48×36 grey thumbnails used only "
                "for matching (not watchable video, no sound). Without the DVD, the trim step looks for these "
                "pictures on the LD and starts the episode exactly there.\n\n"
                "Uses the DVD thumbnails DVD alignment already made when they are from this same file; otherwise "
                "reads the DVD's first two minutes (about a minute). A pack whose opening is not distinctive is not "
                "saved.\n\n"
                f"Saves to:\n{where}" + ("\n(replaces the pack already there; you are asked first)"
                                          if self.pack_exists else ""))

    def _add_save_button(self, row: int, step_id: str) -> None:
        btn = QToolButton()
        btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        btn.setAutoRaise(True)
        btn.setIconSize(QSize(16, 16))
        btn.setToolTip(self.save_tip())
        btn.setAccessibleName("Save DVD opening pack")
        btn.clicked.connect(self.pack_save.emit)
        btn.setVisible(False)
        # a see-through holder over the step's name: the name stays drawn by the table, the icon sits at its right
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 2, 0)
        hl.addStretch(1)
        hl.addWidget(btn)
        self.table.setCellWidget(row, 0, holder)
        self.save_buttons[step_id] = btn

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
        if self.tracker.note:
            parts.append(self.tracker.note)
        self.detail.setText("   ·   ".join(parts))
        for row, step in enumerate(snap.steps):
            self._row(row, step, snap)
        return snap

    def _row(self, row: int, step: StepProgress, snap: Snapshot) -> None:
        text = STATUS_TEXT.get(step.status, step.status)
        if step.status == "running" and snap.paused:
            text = "paused"
        status = QTableWidgetItem(text)
        status.setToolTip("Paused: the tools are frozen and resume exactly where they stopped."
                           if text == "paused" else STATUS_HELP.get(step.status, ""))
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
        when = QTableWidgetItem(label)
        when.setToolTip("Time taken so far" + (" and a rough guess at the time left, from earlier runs"
                                               if "left" in label else ""))
        self.table.setItem(row, 3, when)
        self.refresh_buttons[step.id].setVisible(is_space_block(step.status, step.detail))
        self.restart_buttons[step.id].setVisible(self.can_restart(step.id, step.status))
        if step.id in self.save_buttons:               # the DVD has been probed: its opening can be packed
            self.save_buttons[step.id].setVisible(step.status == "passed" and self.pack_target is not None)
        msg = step.message if step.status == "running" else step.detail
        item = QTableWidgetItem(msg)
        item.setToolTip(msg or self.table.item(row, 0).toolTip())
        self.table.setItem(row, 5, item)
        if step.status in ("failed", "blocked", "stale"):
            self.table.item(row, 0).setForeground(QColor(STATUS_COLOUR["failed"]))
        else:
            self.table.item(row, 0).setData(Qt.ItemDataRole.ForegroundRole, None)
