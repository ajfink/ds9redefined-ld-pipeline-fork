"""Progress panel: the run's headline, overall bar with Pause/Cancel, a run picker, and a per-step checklist.

The checklist shows each step by its short name (the stage id is in the tooltip), one status word from
``STATUS_WORDS``, a bar only while a step is running, how long each step took (also for runs that ended before the
app started), what it is doing or reported, and per-row actions (icons, right-click menu, double-click for the full
message).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from fstk.ui.colours import quiet_css, status_colour
from fstk.ui.step_help import STATUS_HELP, step_name, step_tooltip
from fstk.workflow.progress import (
    STATUS_WORDS,
    ProgressTracker,
    Snapshot,
    StepProgress,
    format_duration,
    is_space_block,
)

STATUS_TEXT = STATUS_WORDS                       # kept for older imports
SCALE = 1000
DONE_MARK = "✓ "

#: steps with nothing to restart: never run, or running now
NOT_STARTED = frozenset({"pending", "waiting", "running", "queued", "not-started"})


class ProgressPanel(QWidget):
    space_refresh = Signal()            # the Refresh icon on a step blocked by disk space was clicked
    columns_resized = Signal(dict)      # the person dragged a column edge: {column name: width}, to remember
    pack_save = Signal()                # the Save icon beside dvd-probe: save this DVD's opening pack
    restart_from = Signal(str)          # "Restart from here" on a step: its id
    open_step_log = Signal(str)         # right-click: the step's tool logs
    open_step_report = Signal(str)      # right-click: the step's report
    pause_clicked = Signal()
    cancel_clicked = Signal()
    run_chosen = Signal(str)            # the run picker: show this job's steps
    reveal_file = Signal(str)           # the Finished file row's "show in folder" link: this path

    COLUMNS: ClassVar[list[str]] = ["Step", "Status", "Progress", "Time", "", "What it is doing"]
    #: starting widths; every column can be dragged wider or narrower, and the widths are remembered
    WIDTHS: ClassVar[dict[str, int]] = {"Step": 150, "Status": 110, "Progress": 200, "Time": 130, "": 56,
                                        "What it is doing": 360}
    HEADER_TIPS: ClassVar[dict[str, str]] = {
        "Step": "The step. Hover over a step's name to see what it does; right-click for its actions. Drag a "
                "column edge to resize it.",
        "Status": "Where the step is: waiting, running, done, needs you, out of date, blocked, failed, cancelled "
                  "or not started.",
        "Progress": "How far the running step has got. “working…” means the tool gives no honest percentage.",
        "Time": "How long the step took (or has taken so far), and roughly how long is left when earlier runs give "
                "a guide.",
        "": ("Restart from here (on steps that have run and could start now), and re-check free disk space (on "
             "steps blocked by disk space)."),
        "What it is doing": "What the step is doing right now, or what it reported when it finished. Double-click "
                            "a row to read all of it.",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tracker: ProgressTracker | None = None
        self.headline = QLabel("No run yet")
        self.headline.setStyleSheet("font-size: 14px; font-weight: 600;")
        self.headline.setWordWrap(True)
        self.overall = QProgressBar()
        self.overall.setRange(0, SCALE)
        self.overall.setTextVisible(False)           # the figures sit beside the bar, never across its edge
        self.overall.setMaximumHeight(14)
        self.overall_label = QLabel("")
        self.pause_button = QPushButton("Pause")
        self.pause_button.setToolTip("Freeze the running tools where they are and start nothing new; Resume "
                                     "continues exactly from here")
        self.pause_button.clicked.connect(self.pause_clicked.emit)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setToolTip("Stop the run. Finished steps are kept; the step in progress starts over "
                                      "next time.")
        self.cancel_button.clicked.connect(self.cancel_clicked.emit)
        self.set_running(False)
        self.runs = QComboBox()
        self.runs.setToolTip("Which run of this episode the table shows: the latest one, or an earlier one.")
        self.runs.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.runs.activated.connect(lambda i: self.run_chosen.emit(self.runs.itemData(i) or ""))
        self.runs_label = QLabel("Run:")
        self.detail = QLabel("")
        self.detail.setStyleSheet(f"color: {quiet_css()};")
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu_at)
        self.table.cellDoubleClicked.connect(lambda row, _col: self.show_message(row))
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
        #: the episode's final output file (set by the window); the "Finished file" row after a delivery's steps
        self.final_file_source: Callable[[], Path | None] | None = None
        self.final_path: Path | None = None
        self.final_exists = False
        self.final_row: int | None = None
        self.final_link: QLabel | None = None
        self.pack_exists = False
        self.running = False

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(self.headline, 1)
        top.addWidget(self.runs_label)
        top.addWidget(self.runs)
        lay.addLayout(top)
        bar = QHBoxLayout()
        bar.addWidget(self.overall, 1)
        bar.addWidget(self.overall_label)
        bar.addWidget(self.pause_button)
        bar.addWidget(self.cancel_button)
        lay.addLayout(bar)
        lay.addWidget(self.detail)
        lay.addWidget(self.table, 1)
        # shown only over the empty part of the table: every cell and header has its own tip
        self.table.setToolTip("Hover over a step for what it does; right-click for its actions.")
        self.set_runs([], None)

    # ------------------------------------------------------------------ run controls and picker
    def set_running(self, running: bool, paused: bool = False) -> None:
        """Pause/Cancel belong to a run going in this window: shown only then."""
        self.running = running
        self.pause_button.setVisible(running)
        self.cancel_button.setVisible(running)
        self.pause_button.setEnabled(running)
        self.cancel_button.setEnabled(running)
        self.pause_button.setText("Resume" if paused else "Pause")

    def set_runs(self, runs: list[tuple[str, str]], current: str | None) -> None:
        """The episode's runs, newest first: (job id, label)."""
        self.runs.blockSignals(True)
        self.runs.clear()
        for job, label in runs:
            self.runs.addItem(label, job)
            self.runs.setItemData(self.runs.count() - 1, job, Qt.ItemDataRole.ToolTipRole)
        idx = self.runs.findData(current) if current else -1
        if idx >= 0:
            self.runs.setCurrentIndex(idx)
        self.runs.blockSignals(False)
        show = len(runs) > 1
        self.runs.setVisible(show)
        self.runs_label.setVisible(show)

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
    def step_label(self, step: StepProgress) -> str:
        return step_name(step.stage, step.id)

    def set_tracker(self, tracker: ProgressTracker | None) -> None:
        self.tracker = tracker
        self.bars.clear()
        self.refresh_buttons.clear()
        self.save_buttons.clear()
        self.restart_buttons.clear()
        self.table.setRowCount(0)
        self.final_row, self.final_link = None, None
        if tracker is None:
            self.headline.setText("No run yet")
            self.overall.setValue(0)
            self.overall_label.setText("")
            self.detail.setText("")
            return
        self.table.setRowCount(len(tracker.steps))
        import fstk.stages  # noqa: F401 - registers every stage, for the titles
        from fstk.stages.base import REGISTRY

        for row, step in enumerate(tracker.steps.values()):
            stage = REGISTRY.get(step.stage)
            tip = step_tooltip(step.stage, stage.title if stage else step.id)
            name = QTableWidgetItem(self.step_label(step))
            name.setToolTip(tip)
            name.setData(Qt.ItemDataRole.UserRole, step.id)
            self.table.setItem(row, 0, name)
            if step.stage == "dvd-probe":
                self._add_save_button(row, step.id)
            bar = QProgressBar()
            bar.setRange(0, SCALE)
            bar.setTextVisible(True)
            bar.setToolTip(tip)
            # in a plain container, so hiding the bar on rows that are not running sticks (see below)
            bar_holder = QWidget()
            bl = QHBoxLayout(bar_holder)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.addWidget(bar)
            self.table.setCellWidget(row, 2, bar_holder)
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
            label = self.step_label(step)
            again.setAccessibleName(f"Restart from {step.id}")
            again.setToolTip(f"<qt><b>Restart from here</b><br><br>Runs <b>{label}</b> again, then the steps "
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
        if any(step.stage == "remux" for step in tracker.steps.values()):   # a run that makes the final file
            self._add_final_row()
        self.update_final_file()

    # ------------------------------------------------------------------ the "Finished file" row
    FINAL_TIP = ("The finished file this run delivers: the encode with every audio and subtitle track. Click its "
                 "name to play it, or \"show in folder\" to open the folder it is in.")

    def _add_final_row(self) -> None:
        row = self.table.rowCount()
        self.table.setRowCount(row + 1)
        name = QTableWidgetItem("Finished file")
        name.setToolTip(self.FINAL_TIP)
        f = name.font()
        f.setBold(True)
        name.setFont(f)
        self.table.setItem(row, 0, name)
        link = QLabel()
        link.setTextFormat(Qt.TextFormat.RichText)
        link.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        link.setContentsMargins(4, 0, 4, 0)
        link.linkActivated.connect(self._final_link_clicked)
        self.table.setCellWidget(row, 5, link)
        self.final_row, self.final_link = row, link

    def update_final_file(self) -> None:
        """Look the final file up again (the window calls this when a step passes and when a run ends)."""
        self.final_path = None
        if self.final_row is not None and self.final_file_source is not None:
            try:
                self.final_path = self.final_file_source()
            except Exception:                               # a manifest being written: try again next time
                self.final_path = None
        self.final_exists = bool(self.final_path and self.final_path.is_file())
        self.refresh()

    def _final_link_clicked(self, href: str) -> None:
        if self.final_path is None:
            return
        if href == "folder":
            self.reveal_file.emit(str(self.final_path))
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.final_path)))

    def _final_row_refresh(self, snap: Snapshot) -> None:
        if self.final_row is None or self.final_link is None:
            return
        if self.final_exists and snap.state == "finished":
            text, tone, tip = DONE_MARK + "ready", "passed", "Every step is done and the delivery was approved."
        elif self.final_exists:
            text, tone, tip = "made", "running", "The file is made; QC and your approval come next."
        else:
            text, tone, tip = "not made yet", "pending", "The file is made by the Remux step."
        status = QTableWidgetItem(text)
        status.setToolTip(tip)
        if tone != "pending":
            status.setForeground(status_colour(tone))
        self.table.setItem(self.final_row, 1, status)
        if self.final_exists:
            p = self.final_path
            self.final_link.setText(f'<a href="file">{p.name}</a> &nbsp;·&nbsp; <a href="folder">show in folder</a>')
            self.final_link.setToolTip(f"{p}\n\n{self.FINAL_TIP}")
        else:
            self.final_link.setText("")
            self.final_link.setToolTip(self.FINAL_TIP)

    # ------------------------------------------------------------------ row actions
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

    def step_at(self, row: int) -> StepProgress | None:
        if self.tracker is None or not (0 <= row < len(self.tracker.steps)):
            return None
        return list(self.tracker.steps.values())[row]

    def message_of(self, step: StepProgress) -> str:
        return step.message if step.status == "running" else step.detail

    def show_message(self, row: int) -> None:
        """Double-click: everything the step reported (the table shows only the start of a long message)."""
        step = self.step_at(row)
        if step is None:
            return
        msg = self.message_of(step)
        QMessageBox.information(self, self.step_label(step),
                                f"{self.step_label(step)} ({step.id}) — "
                                f"{STATUS_WORDS.get(step.shown_status, step.status)}\n\n"
                                + (msg or "It reported nothing."))

    def row_menu(self, row: int) -> QMenu | None:
        step = self.step_at(row)
        if step is None:
            return None
        menu = QMenu(self)
        msg = self.message_of(step)

        def add(text: str, slot, enabled: bool = True) -> QAction:
            act = menu.addAction(text)
            act.triggered.connect(slot)
            act.setEnabled(enabled)
            return act

        add("Restart from here…", lambda: self.restart_from.emit(step.id), self.can_restart(step.id, step.status))
        menu.addSeparator()
        add("Show the full message…", lambda: self.show_message(row), bool(msg))
        add("Copy the message", lambda: QGuiApplication.clipboard().setText(msg), bool(msg))
        add("Open this step's report", lambda: self.open_step_report.emit(step.id), step.ran or step.done)
        add("Open this step's tool logs", lambda: self.open_step_log.emit(step.id), step.ran or step.done)
        if step.stage == "dvd-probe":
            menu.addSeparator()
            add("Save DVD opening pack…", self.pack_save.emit, step.status == "passed" and bool(self.pack_target))
        return menu

    def _menu_at(self, pos) -> None:
        menu = self.row_menu(self.table.rowAt(pos.y()))
        if menu is not None:
            menu.exec(self.table.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ DVD opening pack
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

    # ------------------------------------------------------------------ refresh
    def refresh(self) -> Snapshot | None:
        if self.tracker is None:
            return None
        snap = self.tracker.snapshot()
        self.headline.setText(snap.headline(self.step_label))
        self.overall.setValue(round(snap.overall * SCALE))
        self.overall_label.setText(f"{snap.overall:.0%} · {snap.done} of {snap.total} steps done")
        parts = []
        if snap.state == "running" or snap.paused:
            parts.append(f"elapsed {format_duration(snap.elapsed)}")
            if snap.state == "running" and snap.overall_eta is not None:
                parts.append(f"about {format_duration(snap.overall_eta)} left (rough)")
            elif snap.state == "running":
                parts.append("time left: not known yet (needs timings from earlier runs)")
        else:
            worked = sum(s.took or 0.0 for s in snap.steps)
            if worked:
                parts.append(f"machine time so far {format_duration(worked)}")
        if snap.paused:
            parts.append("PAUSED: tools are frozen and resume exactly where they stopped")
        if self.tracker.note:
            parts.append(self.tracker.note)
        self.detail.setText("   ·   ".join(parts))
        self.detail.setVisible(bool(parts))
        for row, step in enumerate(snap.steps):
            self._row(row, step, snap)
        self._final_row_refresh(snap)
        return snap

    def _row(self, row: int, step: StepProgress, snap: Snapshot) -> None:
        shown = step.shown_status
        text = STATUS_WORDS.get(shown, shown)
        if step.status == "running" and snap.paused:
            text = "paused"
        if shown == "passed":
            text = DONE_MARK + text
        status = QTableWidgetItem(text)
        status.setToolTip("Paused: the tools are frozen and resume exactly where they stopped."
                          if text == "paused" else STATUS_HELP.get(shown, STATUS_HELP.get(step.status, "")))
        if shown != "pending":
            status.setForeground(status_colour(shown))
        self.table.setItem(row, 1, status)
        bar = self.bars[step.id]
        running = step.status == "running"
        bar.setVisible(running)                                  # a bar only where it says something
        if running and step.fraction is None and not snap.paused:
            bar.setRange(0, 0)                                   # indeterminate: no honest percentage exists
            bar.setFormat("working…")
        else:
            bar.setRange(0, SCALE)
            frac = step.credit() if running else 0.0
            bar.setValue(round(frac * SCALE))
            bar.setFormat(f"{frac:.0%}")
        now = self.tracker.paused_at if self.tracker and self.tracker.paused_at else self.tracker.clock()
        took = step.elapsed(now)
        label = "" if took is None else format_duration(took)
        eta = self.tracker.step_eta(step.id) if self.tracker else None
        if running and eta is not None and not snap.paused:
            label += f"  (≈{format_duration(eta)} left)"
        when = QTableWidgetItem(label)
        when.setToolTip(("Time taken so far" if running else "How long it took")
                        + (" and a rough guess at the time left, from earlier runs" if "left" in label else ""))
        self.table.setItem(row, 3, when)
        self.refresh_buttons[step.id].setVisible(is_space_block(step.status, step.detail))
        self.restart_buttons[step.id].setVisible(self.can_restart(step.id, shown))
        if step.id in self.save_buttons:               # the DVD has been probed: its opening can be packed
            self.save_buttons[step.id].setVisible(step.status == "passed" and self.pack_target is not None)
        msg = self.message_of(step)
        item = QTableWidgetItem(msg)
        item.setToolTip(msg or self.table.item(row, 0).toolTip())
        self.table.setItem(row, 5, item)
        if step.status in ("failed", "blocked", "stale"):
            self.table.item(row, 0).setForeground(status_colour("failed"))
        else:
            self.table.item(row, 0).setData(Qt.ItemDataRole.ForegroundRole, None)
