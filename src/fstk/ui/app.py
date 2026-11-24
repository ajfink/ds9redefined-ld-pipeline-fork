"""FromStunToKill desktop app (PySide6). First shell over ``fstk.ui.services``.

Layout
  left   – episodes found under the profile's data_root
  centre – tabs: Manifest (every declared field, set or unset, with provenance),
                 Jobs (step states for each job), Reviews (awaiting-review steps)
  bottom – live event log for the running plan

Plans run on a worker ``QThread`` and stream engine events back through a signal; the GUI
thread never blocks on media work. Review decisions require a non-empty note and are bound
to exactly the evidence files listed for the step.

Review windows (``fstk.ui.reviews``): EFM capture choice with A/B audition, and the delivery QC
review. Other workbenches (alignment, cadence, PAD, retime) are not ported yet; v38's Tkinter
workbenches remain the reference (docs/v38-inventory.md).
"""
from __future__ import annotations

import contextlib
import getpass
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from fstk.core.errors import FstkError
from fstk.ui.progress_panel import ProgressPanel
from fstk.ui.services import Services, describe_field
from fstk.workflow.progress import ProgressTracker, describe_job_state

LOG = logging.getLogger("fstk.ui")
STATUS_COLOURS = {"passed": "#2e7d32", "failed": "#c62828", "blocked": "#ef6c00",
                  "awaiting-review": "#1565c0", "running": "#6a1b9a", "pending": "#616161",
                  "cancelled": "#616161"}


def code_stamp() -> tuple[int, int]:
    """(file count, newest change) of the app's Python files, to notice an update made while it runs."""
    root = Path(__file__).resolve().parent.parent
    newest, count = 0, 0
    try:
        for f in root.rglob("*.py"):
            count += 1
            newest = max(newest, f.stat().st_mtime_ns)
    except OSError:
        pass
    return count, newest


def _report_ui_error(exc_type, exc, tb) -> None:
    """An error in a button or window: tell the person instead of nothing happening (it is also logged)."""
    if _report_ui_error.busy or QApplication.instance() is None:
        return
    _report_ui_error.busy = True
    try:
        updated = code_stamp() != _report_ui_error.stamp
        hint = ("\n\nThe app's files were updated while it was open, so old and new parts are mixed. Quit and start "
                "it again." if updated else "\n\nThe details are in the log (Open logs); Save debug bundle… collects "
                "them for a bug report.")
        QMessageBox.critical(None, "Something went wrong", f"{exc_type.__name__}: {exc}{hint}")
    finally:
        _report_ui_error.busy = False


_report_ui_error.busy = False
_report_ui_error.stamp = (0, 0)


class PlanWorker(QObject):
    event = Signal(dict)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, services: Services, plan_path: str, actor: str):
        super().__init__()
        self.services, self.plan_path, self.actor = services, plan_path, actor
        self.engine = None

    @Slot()
    def run(self) -> None:
        try:
            _engine, state = self.services.run_plan(self.plan_path, actor=self.actor, events=self.event.emit,
                                                   on_engine=lambda e: setattr(self, "engine", e))
            self.finished.emit(state)
        except FstkError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # surface anything unexpected instead of dying silently
            self.failed.emit(f"internal error: {exc!r}")


class CatalogRefreshWorker(QObject):
    """Owner decision: re-check the Archive catalog when the snapshot is more than 30 days old."""

    done = Signal(str)
    failed = Signal(str)

    def __init__(self, services: Services):
        super().__init__()
        self.services = services

    @Slot()
    def run(self) -> None:
        try:
            if self.services.catalog_status()["stale"]:
                self.done.emit(self.services.refresh_catalog())
            else:
                self.done.emit("")
        except Exception as exc:  # offline etc.: the existing snapshot stays in use
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self, services: Services, *, actor: str | None = None):
        super().__init__()
        self.services = services
        self.actor = actor or str((services.profile.ui or {}).get("operator") or "").strip() \
            or f"{getpass.getuser()}@{socket.gethostname()}"
        self.thread: QThread | None = None
        self.worker: PlanWorker | None = None
        self.setWindowTitle(f"FromStunToKill — {services.profile.machine}")
        self.resize(1280, 800)
        self.catalog_thread: QThread | None = None
        QTimer.singleShot(1500, self.check_catalog_age)

        self.episodes = QListWidget()
        self.episodes.currentTextChanged.connect(self.show_episode)
        self.title = QLabel("No episode selected")
        self.title.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.override_flag = QPushButton()
        self.override_flag.setFlat(True)
        self.override_flag.setStyleSheet("text-align: left; color: #ef6c00; font-weight: 600;")
        self.override_flag.setCursor(Qt.CursorShape.PointingHandCursor)
        self.override_flag.clicked.connect(self.open_settings)
        self.override_flag.setVisible(False)

        self.values = QTableWidget(0, 5)
        self.values.setHorizontalHeaderLabels(["Field", "Value", "Unit", "Kind", "Set by"])
        self.values.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.values.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for table_header in (self.values.verticalHeader(),):
            table_header.setVisible(False)
            table_header.setDefaultSectionSize(22)

        self.jobs = QTableWidget(0, 4)
        self.jobs.setHorizontalHeaderLabels(["Job", "Step", "Status", "Issues"])
        self.jobs.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.jobs.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        from fstk.ui.visual import DetailsToggle, SpotViewer, TimelineBar

        self.reviews = QListWidget()
        self.reviews.setMaximumHeight(72)
        self.reviews.currentRowChanged.connect(self.show_review)
        self.review_headline = QLabel("")
        self.review_headline.setWordWrap(True)
        self.review_headline.setTextFormat(Qt.TextFormat.RichText)
        self.review_how = QLabel("")
        self.review_how.setWordWrap(True)
        self.review_how.setStyleSheet("color: #9e9e9e;")
        self.review_timeline = TimelineBar()
        self.review_timeline.tick_clicked.connect(lambda key: self.review_spots.show_spot(int(key)))
        self.review_spots = SpotViewer()
        self.review_spots.spot_changed.connect(self.review_timeline.select_tick)
        self.review_detail = QPlainTextEdit()
        self.review_detail.setReadOnly(True)
        self.review_details_toggle = DetailsToggle(self.review_detail, "the numbers")
        self.review_image = QLabel("")
        self.review_image.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.review_scroll = QScrollArea()
        self.review_scroll.setWidget(self.review_image)
        self.review_scroll.setWidgetResizable(False)
        self.review_scroll.setVisible(False)
        self.open_evidence = QPushButton("Open files")
        self.open_evidence.clicked.connect(self.open_selected_evidence)
        self.approve = QPushButton("Approve…")
        self.reject = QPushButton("Reject…")
        self.approve.setStyleSheet("font-weight: 600;")
        self.approve.clicked.connect(lambda: self.decide("APPROVED"))
        self.reject.clicked.connect(lambda: self.decide("REJECTED"))
        review_buttons = QHBoxLayout()
        review_buttons.addWidget(self.review_details_toggle)
        review_buttons.addStretch(1)
        for b in (self.open_evidence, self.reject, self.approve):
            review_buttons.addWidget(b)
        review_page = QWidget()
        rl = QVBoxLayout(review_page)
        rl.addWidget(self.reviews)
        rl.addWidget(self.review_headline)
        rl.addWidget(self.review_timeline)
        rl.addWidget(self.review_spots, 5)
        rl.addWidget(self.review_scroll, 5)
        rl.addWidget(self.review_how)
        rl.addWidget(self.review_detail, 2)
        rl.addLayout(review_buttons)
        self.review_page = review_page

        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        self.banner.setToolTip("What just happened and what the app is doing about it.")
        self._queued_plans: list[str] = []
        self._running_plan: str | None = None
        self.progress_panel = ProgressPanel()
        self.progress_panel.space_refresh.connect(self.recheck_space)
        self.status_line = QLabel("")
        self.tracker: ProgressTracker | None = None
        self.tabs = QTabWidget()
        self.tabs.addTab(self.progress_panel, "Progress")
        self.tabs.addTab(self.values, "Manifest")
        self.tabs.addTab(self.jobs, "Jobs")
        self.tabs.addTab(review_page, "Reviews")
        from fstk.ui.queue_panel import QueuePanel

        self.queue_runner = services.queue_runner(actor=self.actor)
        self.queue_panel = QueuePanel(self.queue_runner, current_episode=lambda: self.episode,
                                      episodes=lambda: [str(e) for e in self.services.episodes()],
                                      notify=self.notify, sleep=self.services.sleep_computer)
        self.tabs.addTab(self.queue_panel, "Queue")
        if self.queue_runner.running and any(i.status in ("queued", "held") for i in self.queue_runner.store.items):
            QTimer.singleShot(3000, self._resume_queue)
        QTimer.singleShot(1500, self._check_volumes)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.remove_button = QPushButton("Remove…")
        self.remove_button.setToolTip("Remove the selected episode (and, if you choose, its files)")
        self.remove_button.clicked.connect(self.open_remove)
        self.pick_button = QPushButton("Episodes…")
        self.pick_button.clicked.connect(self.open_picker)
        self.intake_button = QPushButton("Run intake")
        self.intake_button.clicked.connect(self.run_intake)
        self.deliver_button = QPushButton("Run to delivery")
        self.deliver_button.setToolTip("Stack render → … → QC, then the A/V check (DVD or no-DVD route)")
        self.deliver_button.clicked.connect(self.run_delivery)
        self.dvd_button = QPushButton("DVD…")
        self.dvd_button.clicked.connect(self.open_dvd)
        self.efm_button = QPushButton("EFM capture…")
        self.efm_button.clicked.connect(self.open_efm_review)
        self.delivery_button = QPushButton("Delivery review…")
        self.delivery_button.clicked.connect(self.open_delivery_review)
        self.handoff_button = QPushButton("Hand off…")
        self.handoff_button.setToolTip("Freeze this episode here and hand it to another machine (shared workspace)")
        self.handoff_button.clicked.connect(self.ask_handoff)
        self.run_button = QPushButton("Run plan…")
        self.run_button.clicked.connect(self.choose_and_run)
        self.settings_button = QPushButton("Settings…")
        self.settings_button.setToolTip("Video and audio quality (defaults and per episode), folders, tools, logging")
        self.settings_button.clicked.connect(self.open_settings)
        self.folders_button = QPushButton("Folders…")
        self.folders_button.setToolTip("Where fstk keeps its state and does its work")
        self.folders_button.clicked.connect(self.open_folders)
        self.bundle_button = QPushButton("Save debug bundle…")
        self.bundle_button.setToolTip("Zip the logs, settings, job state and tool logs (no media) to send for a bug report")
        self.bundle_button.clicked.connect(self.save_bundle)
        self.logs_button = QPushButton("Open logs")
        self.logs_button.setToolTip("Open the folder with the application log and crash log")
        self.logs_button.clicked.connect(self.open_logs)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_run)
        self.pause_button = QPushButton("Pause")
        self.pause_button.setToolTip("Available while a plan is running in this window. Freezes the running tools "
                                     "where they are and starts nothing new; Resume continues exactly from here")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.notify_button = QPushButton("Notifications")
        self.notify_button.setCheckable(True)
        self.notify_button.setChecked(bool((services.profile.ui or {}).get("notifications", True)))
        self.notify_button.setToolTip("Desktop notification when a step needs you, a step fails, or the run ends")
        self._set_tips()
        self.tray = QSystemTrayIcon(self.windowIcon() or QApplication.windowIcon(), self)
        self.watch = QTimer(self)                         # follows a run that another window/process is doing
        self.watch.setInterval(3000)
        self.watch.timeout.connect(lambda: self.episode and self.thread is None and self.show_episode(self.episode))
        self.tick = QTimer(self)
        self.tick.setInterval(1000)
        self.tick.timeout.connect(self.update_progress)
        self._taskbar = None
        self._code_at_start = code_stamp()
        self.code_watch = QTimer(self)                    # the app's own files replaced while it runs (a sync)
        self.code_watch.setInterval(120_000)
        self.code_watch.timeout.connect(self.check_code_updated)
        self.code_watch.start()

        centre = QWidget()
        cl = QVBoxLayout(centre)
        cl.addWidget(self.title)
        cl.addWidget(self.override_flag)
        cl.addWidget(self.status_line)
        cl.addWidget(self.banner)
        cl.addWidget(self.tabs, 1)
        bar = QHBoxLayout()
        bar.addWidget(self.pick_button)
        bar.addWidget(self.intake_button)
        bar.addWidget(self.deliver_button)
        bar.addWidget(self.dvd_button)
        bar.addWidget(self.efm_button)
        bar.addWidget(self.delivery_button)
        bar.addWidget(self.handoff_button)
        bar.addWidget(self.run_button)
        bar.addWidget(self.pause_button)
        bar.addWidget(self.cancel_button)
        bar.addStretch(1)
        bar.addWidget(self.notify_button)
        bar.addWidget(self.settings_button)
        cl.addLayout(bar)
        bar2 = QHBoxLayout()
        bar2.addStretch(1)
        bar2.addWidget(self.logs_button)
        bar2.addWidget(self.bundle_button)
        cl.addLayout(bar2)

        split = QSplitter(Qt.Orientation.Vertical)
        top = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.episodes, 1)
        ll.addWidget(self.remove_button)
        top.addWidget(left)
        top.addWidget(centre)
        top.setStretchFactor(1, 4)
        split.addWidget(top)
        split.addWidget(self.log)
        split.setStretchFactor(0, 4)
        self.setCentralWidget(split)
        self.refresh()

    # ---------------------------------------------------------------- data
    def refresh(self) -> None:
        current = self.episodes.currentItem().text() if self.episodes.currentItem() else None
        self.episodes.clear()
        for ep in self.services.episodes():
            self.episodes.addItem(ep.canonical)
        if current:
            matches = self.episodes.findItems(current, Qt.MatchFlag.MatchExactly)
            if matches:
                self.episodes.setCurrentItem(matches[0])
            elif self.episodes.count():
                self.episodes.setCurrentRow(0)
        elif self.episodes.count():
            self.episodes.setCurrentRow(0)

    @property
    def episode(self) -> str | None:
        item = self.episodes.currentItem()
        return item.text() if item else None

    @Slot(str)
    def show_episode(self, episode: str) -> None:
        if not episode:
            return
        title = f"{episode} — {self.services.title(episode)}"
        frozen = self.services.frozen_reason(episode)
        others = [p for p in self.services.presence(episode) if not p.mine]
        if frozen:
            title += "   [handed off — read only on this machine]"
        elif others:
            title += "   [also on: " + ", ".join(f"{p.device} ({p.state})" for p in others) + "]"
        self.title.setText(title)
        self.title.setToolTip(frozen or "\n".join(p.describe() for p in others))
        self.show_override_flag(episode)
        self.run_button.setEnabled(not frozen)
        self.intake_button.setEnabled(not frozen)
        self.deliver_button.setEnabled(not frozen)
        self.handoff_button.setEnabled(not frozen)
        rows = self.services.manifest_rows(episode)
        self.values.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j, text in enumerate((r.key, r.value, r.unit, r.kind, f"{r.origin}/{r.by}" if r.set else "")):
                item = QTableWidgetItem(text)
                if j == 0:
                    item.setToolTip(describe_field(r.key))
                if not r.set:
                    item.setForeground(Qt.GlobalColor.gray)
                self.values.setItem(i, j, item)
        self.values.resizeColumnToContents(0)
        job_rows = []
        for job in self.services.jobs(episode):
            for sid, st in self.services.job_state(episode, job)["steps"].items():
                status = st.get("status", "")
                if status in ("running", "interrupted") and self.thread is None:
                    status = "pending"          # saved by a run that is no longer going: it will start again
                job_rows.append((job, sid, status, "; ".join(st.get("issues") or [])))
        self.jobs.setRowCount(len(job_rows))
        for i, row in enumerate(job_rows):
            for j, text in enumerate(row):
                item = QTableWidgetItem(text)
                if j == 2:
                    item.setForeground(Qt.GlobalColor.white)
                    item.setBackground(self._colour(text))
                self.jobs.setItem(i, j, item)
        self._show_saved_progress(episode)
        self._pending = self.services.pending_reviews(episode)
        self.reviews.clear()
        for p in self._pending:
            item = QListWidgetItem(f"{self.REVIEW_NAMES.get(p.review, p.review)} — waiting for you")
            item.setToolTip(f"{p.review} · job {p.job} · step {p.step}")
            self.reviews.addItem(item)
        if self._pending and self.reviews.currentRow() < 0:
            self.reviews.setCurrentRow(0)
        self.tabs.setTabText(3, f"Reviews ({len(self._pending)})")
        self.show_review(self.reviews.currentRow())

    @staticmethod
    def _colour(status: str):
        from PySide6.QtGui import QColor

        return QColor(STATUS_COLOURS.get(status, "#616161"))

    REVIEW_NAMES: ClassVar[dict[str, str]] = {"dvd-alignment": "DVD alignment", "cadence-review": "Film cadence",
                    "delivery": "Delivery A/V check"}

    @Slot(int)
    def show_review(self, row: int) -> None:
        from fstk.ui.visual import headline_html, load_review_view

        has = 0 <= row < len(getattr(self, "_pending", []))
        for b in (self.open_evidence, self.approve, self.reject):
            b.setEnabled(has)
        self.approve.setText("Approve…")
        if not has:
            self.review_headline.setText(headline_html("", "Nothing is waiting for you.", "neutral"))
            self.review_detail.setPlainText("No step is waiting for review.")
            self.review_how.setText("")
            self.review_timeline.setVisible(False)
            self.review_spots.set_spots(None, [])
            self._show_picture(None)
            return
        p = self._pending[row]
        name = self.REVIEW_NAMES.get(p.review, p.review)
        view, report = load_review_view(p.evidence)
        summary = self._review_summary(p.evidence)
        self.review_detail.setPlainText(
            (f"{summary}\n\n" if summary else "")
            + (("Why it was held: " + "; ".join(view["reasons"]) + "\n\n") if view.get("reasons") else "")
            + f"Review: {name} ({p.review})\nStage: {p.stage}\nJob/step: {p.job}/{p.step}\n\nEvidence:\n"
            + "\n".join(f"  {e}" for e in p.evidence) + f"\n\n{p.detail}")
        if p.review == "delivery":
            self.review_headline.setText(headline_html(
                "WATCH AND LISTEN", "The delivery is ready for the A/V check: play each chunk in the A/V check "
                "window and mark whether picture and sound are in step.", "info"))
            self.approve.setText("Open A/V check…")
            self.reject.setEnabled(False)
            self.review_how.setText("")
            self.review_timeline.setVisible(False)
            self.review_spots.set_spots(None, [])
            self._show_picture(None)
            return
        if view:
            self.review_headline.setText(headline_html(view.get("verdict", ""), f"{name}: {view.get('headline', '')}",
                                                       view.get("tone", "neutral")))
            spots = view.get("spots", [])
            sheet = report.parent / view["sheet"] if report is not None and view.get("sheet") else None
            self.review_timeline.set_data(view.get("duration_s", 0.0), view.get("timeline", []),
                                          [{"time_s": s["time_s"], "tone": s.get("tone"), "key": i,
                                            "label": f"{s.get('label', '')} — {s.get('verdict', '')}"}
                                           for i, s in enumerate(spots)])
            self.review_timeline.setVisible(True)
            self.review_spots.set_spots(sheet if sheet and sheet.is_file() else None, spots)
            self.review_how.setText(view.get("how_to_look", ""))
            self._show_picture(None if self.review_spots.spots else
                               (str(sheet) if sheet and sheet.is_file() else None))
            return
        # an older report without a picture description: its plain summary and its picture sheet, if any
        self.review_headline.setText(headline_html("LOOK", f"{name}: " + (summary.splitlines()[0] if summary else
                                                   "look at the picture and decide."), "info"))
        self.review_timeline.setVisible(False)
        self.review_spots.set_spots(None, [])
        self.review_how.setText("")
        self._show_picture(next((e for e in p.evidence if e.lower().endswith(".png")), None))

    @staticmethod
    def _review_summary(evidence) -> str:
        """The plain-language summary a stage wrote into its report (``review_summary``), if any."""
        import json

        for e in evidence:
            if e.lower().endswith(".json"):
                try:
                    text = json.loads(Path(e).read_text(encoding="utf-8")).get("review_summary")
                except (OSError, ValueError, AttributeError):
                    continue
                if isinstance(text, str) and text.strip():
                    return text.strip()
        return ""

    def _show_picture(self, path: str | None) -> None:
        """Show a picture sheet that has no spot description (older reports), scrollable at full size."""
        from PySide6.QtGui import QPixmap

        pix = QPixmap(path) if path else QPixmap()
        if pix.isNull():
            self.review_image.clear()
            self.review_scroll.setVisible(False)
            return
        self.review_image.setPixmap(pix)
        self.review_image.resize(pix.size())
        self.review_scroll.setVisible(True)

    def _selected_review(self):
        row = self.reviews.currentRow()
        return self._pending[row] if 0 <= row < len(self._pending) else None

    def open_selected_evidence(self) -> None:
        p = self._selected_review()
        for e in p.evidence if p else ():
            QDesktopServices.openUrl(QUrl.fromLocalFile(e))

    def decide(self, decision: str) -> None:
        p = self._selected_review()
        if not p or not self.episode:
            return
        if p.review == "delivery":   # delivery decisions go through the QC review window
            self.open_delivery_review()
            return
        note, ok = QInputDialog.getMultiLineText(self, f"{decision.title()} {p.review}",
                                                 "Explain the decision (required):")
        if not ok:
            return
        try:
            path = self.services.decide(self.episode, p, decision=decision, note=note, actor=self.actor)
        except FstkError as exc:
            QMessageBox.warning(self, "Decision not recorded", str(exc))
            return
        self.append_log(f"{decision} recorded: {path}")
        self.show_episode(self.episode)
        name = self.REVIEW_NAMES.get(p.review, p.review)
        if decision == "APPROVED":
            self.continue_job(p.job, what=f"{name} approved")
        elif self.ask_redo(name):
            self.continue_job(p.job, what=f"{name} rejected — redoing it")
        else:
            self.set_banner(f"{name} rejected. It is redone the next time this job runs (Run to delivery).", "warn")

    def ask_redo(self, name: str) -> bool:
        """After a rejection: redo the step now? (A method so tests can answer.)"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Redo it?")
        box.setText(f"{name} rejected. Redo this step now?")
        box.setInformativeText("It is measured again from the same files, and you get a new review. If the files "
                               "themselves are wrong (wrong DVD, bad render), fix that first and choose Later.")
        now = box.addButton("Redo now", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is now

    def continue_job(self, job: str, *, what: str = "Approved") -> bool:
        """Carry on a job that was waiting for a review that has just been approved.

        The run that stopped at the review has ended, so the saved plan is run again: finished steps are kept,
        the approved step is committed and the rest follows. If that job is the one running now it picks the
        approval up by itself; if another run is going, this one is queued and starts when that ends. Returns
        True when the job is running, queued or already picking it up."""
        if not self.episode:
            return False
        path = self.services.job_plan_path(self.episode, job)
        if path is None:
            self.append_log(f"approved, but the plan for job {job} is not saved; use Run plan… to continue")
            self.set_banner(f"{what}, but its plan was not found: press Run to delivery to carry on.", "warn")
            return False
        if self.thread is not None and self._running_plan and Path(self._running_plan).parent.name == job:
            self.set_banner(f"{what} — the running job picks it up and carries on.", "good")
            return True
        nxt = self._next_steps(job)
        self.append_log(f"continuing job {job} after the approval")
        started = self.start_plan(str(path))
        if started:
            self.set_banner(f"{what} — continuing: {nxt}. Follow it on the Progress tab.", "good")
        else:
            self.set_banner(f"{what} — queued: it continues ({nxt}) as soon as the current run ends.", "info")
        return True

    def _next_steps(self, job: str) -> str:
        """The steps still to do in a saved job, for the banner."""
        try:
            steps = self.services.job_state(self.episode, job)["steps"]
        except (OSError, ValueError, KeyError, FstkError):
            return "the remaining steps"
        todo = [sid for sid, st in steps.items() if st.get("status") != "passed"]
        if not todo:
            return "the remaining steps"
        return ", ".join(todo[:3]) + (f" and {len(todo) - 3} more" if len(todo) > 3 else "")

    def check_code_updated(self) -> bool:
        """The app's files changed since it started: parts loaded later would mix old and new code."""
        if code_stamp() == self._code_at_start:
            return False
        self.code_watch.stop()
        self.append_log("the app's files were updated while it was open; restart it to use the new version")
        self.set_banner("FromStunToKill was updated while it was open. Quit and start it again to use the new "
                        "version" + (" (after this run: finished steps are kept)." if self.thread else "."), "warn")
        return True

    def set_banner(self, text: str, tone: str = "info") -> None:
        from fstk.ui.visual import tone_colour

        self.banner.setText(text)
        self.banner.setStyleSheet(f"background: {tone_colour(tone)}; color: white; padding: 6px 10px; "
                                  "border-radius: 4px; font-weight: 600;")
        self.banner.setVisible(bool(text))

    def on_delivery_decided(self, path: str) -> None:
        import json

        self.append_log(f"delivery decision recorded: {path}")
        if self.episode:
            self.show_episode(self.episode)
        try:
            approved = json.loads(Path(path).read_text(encoding="utf-8")).get("decision") == "APPROVED"
        except (OSError, ValueError):
            approved = False
        self.raise_()
        self.activateWindow()
        if approved:
            self.continue_after_delivery_decision()
        else:
            self.set_banner("Delivery decision recorded.", "info")

    def continue_after_delivery_decision(self) -> None:
        """The QC window recorded an approval: continue the job whose delivery review it answered."""
        if not self.episode:
            return
        for pending in self.services.pending_reviews(self.episode):
            if pending.review == "delivery":
                self.continue_job(pending.job, what="Delivery approved")
                return

    # ---------------------------------------------------------------- review windows
    def open_dvd(self) -> None:
        from fstk.ui.dvd import DvdDialog

        if not self.episode:
            QMessageBox.information(self, "DVD", "Select an episode first.")
            return
        LOG.info("action open-dvd episode=%s", self.episode)
        dlg = DvdDialog(self.services, self.episode, self.actor, self)
        dlg.exec()
        if dlg.changed:
            self.log.appendPlainText(f"DVD for {self.episode}: " + str(self.services.dvd_status(self.episode)))
            self.show_episode(self.episode)

    def open_efm_review(self):
        from fstk.ui.reviews import EfmCaptureReview

        if not self.episode:
            return None
        dlg = EfmCaptureReview(self.services, self.episode, parent=self)
        dlg.plan_ready.connect(lambda path: self.run_from_window(path, "EFM capture chosen", dlg))
        dlg.open()
        return dlg

    def open_delivery_review(self):
        from fstk.ui.reviews import DeliveryReview

        if not self.episode:
            return None
        dlg = DeliveryReview(self.services, self.episode, actor=self.actor, parent=self)
        dlg.plan_ready.connect(lambda path: self.run_from_window(path, "Your choices are being applied", dlg))
        dlg.decided.connect(self.on_delivery_decided)
        dlg.open()
        return dlg

    def run_from_window(self, path: str, what: str, dlg=None) -> None:
        """A review window decided something that needs work: close it, start (or queue) the work, show it."""
        if dlg is not None and dlg.isVisible():
            dlg.accept()
        self.raise_()
        self.activateWindow()
        if self.start_plan(path):
            self.set_banner(f"{what} — the work has started. Follow it on the Progress tab.", "good")
        else:
            self.set_banner(f"{what} — queued: it starts as soon as the current run ends.", "info")

    def handoff_to(self, device: str, note: str) -> dict | None:
        if not self.episode:
            return None
        try:
            h = self.services.handoff(self.episode, to=device, actor=self.actor, note=note)
        except FstkError as exc:
            QMessageBox.warning(self, "Handoff not made", str(exc))
            return None
        self.append_log(f"handed {self.episode} to {device} (snapshot {h['snapshot']}); this machine will not "
                        "run it any more")
        self.show_episode(self.episode)
        return h

    def ask_handoff(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        if not self.episode:
            return
        peers = self.services.peers()
        if not peers:
            QMessageBox.information(self, "No other machine", self.services.shared_problem or
                                    "No other device has joined the shared workspace yet.")
            return
        device, ok = QInputDialog.getItem(self, "Hand off", f"Hand {self.episode} to:", peers, 0, False)
        if not ok:
            return
        note, ok = QInputDialog.getText(self, "Hand off", "Note for the other machine (what's done, what's next):")
        if ok:
            self.handoff_to(device, note)

    # ---------------------------------------------------------------- episodes
    def open_picker(self) -> None:
        from fstk.ui.picker import EpisodePicker

        dlg = EpisodePicker(self.services, actor=self.actor, parent=self)
        if dlg.exec() and dlg.chosen is not None:
            self.select_episode(dlg.chosen.canonical)

    def select_episode(self, canonical: str) -> None:
        self.refresh()
        matches = self.episodes.findItems(canonical, Qt.MatchFlag.MatchExactly)
        if matches:
            self.episodes.setCurrentItem(matches[0])

    def open_remove(self) -> None:
        episode = self.episode
        LOG.info("action remove-open episode=%s", episode)
        if not episode:
            return
        if self.thread is not None:
            QMessageBox.information(self, "Run in progress", "Wait for the running plan to finish, or cancel it, "
                                    "before removing an episode.")
            return
        from fstk.ui.remove import RemoveDialog

        try:
            dlg = RemoveDialog(self.services, episode, self)
        except FstkError as exc:
            QMessageBox.warning(self, "Cannot remove", str(exc))
            return
        if dlg.exec():
            self.append_log(f"removed {episode}: " + ("; ".join(dlg.removed) or "project record only"))
            self.tracker = None
            self.progress_panel.set_tracker(None)
            self.status_line.setText("")
            self.refresh()
            if not self.episodes.count():
                self.title.setText("No episode selected")
                self.values.setRowCount(0)
                self.jobs.setRowCount(0)

    def open_logs(self) -> None:
        from fstk.core.applog import log_dir

        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        LOG.info("action open-logs")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def save_bundle(self) -> None:
        from fstk.config.profile import default_app_home
        from fstk.diagnostics import build_bundle

        default = default_app_home() / "bundles" / f"fstk-debug-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        path, _ = QFileDialog.getSaveFileName(self, "Save debug bundle", str(default), "Zip (*.zip)")
        if not path:
            return
        LOG.info("action save-bundle path=%s episode=%s", path, self.episode)
        try:
            res = build_bundle(self.services.profile, path, episodes=[self.episode] if self.episode else None)
        except (OSError, FstkError) as exc:
            LOG.exception("debug bundle failed")
            QMessageBox.warning(self, "Could not save the bundle", str(exc))
            return
        self.append_log(f"debug bundle saved: {res['path']} ({res['files']} files, {res['bytes'] / 1e6:.1f} MB)")
        QMessageBox.information(self, "Debug bundle saved",
                                f"{res['path']}\n\n{res['files']} files, {res['bytes'] / 1e6:.1f} MB. "
                                "It contains logs, settings and job state, no media.")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(res["path"]).parent)))

    def open_settings(self) -> None:
        from fstk.ui.settings import SettingsDialog

        LOG.info("action open-settings episode=%s", self.episode)
        dlg = SettingsDialog(self.services, self.episode or None, self, actor=self.actor)
        if not dlg.exec():
            return
        prefs = self.services.app_prefs()
        self.notify_button.setChecked(bool(prefs["notifications"]))
        if prefs["operator"]:
            self.actor = str(prefs["operator"])
        self.refresh()
        if dlg.reencode_plan:
            self.start_plan(dlg.reencode_plan)

    def open_folders(self) -> None:
        from fstk.ui.folders import FoldersDialog

        dlg = FoldersDialog(self.services.profile, self)
        if dlg.exec() and dlg.changed:
            self.log.appendPlainText("folders: " + "; ".join(f"{k} → {n or '(default)'}" for k, _o, n in dlg.changed))
            self.refresh()

    def run_intake(self) -> None:
        LOG.info("action run-intake episode=%s", self.episode)
        if not self.episode:
            QMessageBox.information(self, "Select an episode", "Pick an episode in the list on the left first.")
            return
        try:
            path = self.services.write_intake_plan(self.episode)
        except FstkError as exc:
            QMessageBox.warning(self, "Cannot plan intake", str(exc))
            return
        self.start_plan(str(path))

    def run_delivery(self) -> None:
        LOG.info("action run-delivery episode=%s", self.episode)
        if not self.episode:
            QMessageBox.information(self, "Select an episode", "Pick an episode in the list on the left first.")
            return
        try:
            path = self.services.write_delivery_plan(self.episode)
        except FstkError as exc:
            QMessageBox.warning(self, "Cannot plan delivery", str(exc))
            return
        self.start_plan(str(path))

    # ---------------------------------------------------------------- run
    def choose_and_run(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Workflow plan", str(Path.home()), "Plan (*.json)")
        if path:
            self.start_plan(path)

    def start_plan(self, path: str) -> bool:
        """Run a plan now; while another run is going, queue it (it starts when that run ends). True if started."""
        if self.thread is not None:
            if path != self._running_plan and path not in self._queued_plans:
                self._queued_plans.append(path)
                self.append_log(f"queued {path}: it starts when the current run ends")
            return False
        self._running_plan = path
        self._begin_progress(path)
        self.thread = QThread(self)
        self.worker = PlanWorker(self.services, path, self.actor)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.event.connect(self.on_event)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.run_button.setEnabled(False)
        self.intake_button.setEnabled(False)
        self.deliver_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.pause_button.setEnabled(True)
        self.pause_button.setText("Pause")
        self.append_log(f"running {path}")
        self.thread.start()
        self.update_progress()
        return True

    def check_catalog_age(self) -> None:
        if self.catalog_thread is not None:
            return
        self.catalog_thread = QThread(self)
        self.catalog_worker = CatalogRefreshWorker(self.services)
        self.catalog_worker.moveToThread(self.catalog_thread)
        self.catalog_thread.started.connect(self.catalog_worker.run)
        self.catalog_worker.done.connect(self._catalog_done)
        self.catalog_worker.failed.connect(self._catalog_failed)
        self.catalog_thread.start()

    def _catalog_end(self) -> None:
        if self.catalog_thread:
            self.catalog_thread.quit()
            self.catalog_thread.wait()
        self.catalog_thread = None

    @Slot(str)
    def _catalog_done(self, path: str) -> None:
        if path:
            self.append_log(f"Archive catalog refreshed: {path}")
        self._catalog_end()

    @Slot(str)
    def _catalog_failed(self, message: str) -> None:
        self.append_log(f"Archive catalog check skipped (keeping current snapshot): {message}")
        self._catalog_end()

    def _set_tips(self) -> None:
        from fstk.ui.tips import apply_common, apply_tips

        apply_tips(self, {
            "episodes": "Episodes on this computer. Select one to see its values, jobs and reviews.",
            "override_flag": "This episode has its own output settings. Click to open them.",
            "values": "Every value recorded for this episode, and who or what set it (measured, imported from v38, "
                      "entered by hand). Hover a field name for its meaning.",
            "jobs": "The plans that have run, or are running, for this episode, step by step, with their status "
                    "and any issues.",
            "reviews": "Checks waiting for a person to look at the evidence. Select one to read what was measured.",
            "review_scroll": "The review's picture sheet (a stage's evidence image), shown at full size; scroll to see every row.",
            "review_detail": "The measurements behind the review, for reference.",
            "review_headline": "What the automatic checks concluded, in plain words.",
            "review_how": "What to look for before you decide.",
            "review_timeline": "The whole programme: coloured stretches are what the checks flagged, markers are the "
                               "places shown below. Click a marker to see it.",
            "review_spots": "The pictures at each checked place.",
            "open_evidence": "Open the evidence files for the selected review in other apps (images, reports).",
            "approve": "Record that you checked the evidence and it is right. Your name and a note are stored with "
                       "the decision, and the next step may start.",
            "reject": "Record that the evidence is wrong. The step stays blocked until it is fixed or redone.",
            "log": "What fstk is doing, oldest first. The full log is in the logs folder (Open logs).",
            "pick_button": "Find an episode in the archive catalog, or add one from a folder you already have, then "
                           "create or open its workspace.",
            "intake_button": "Intake: download the captures (if needed), inventory them, choose the stack reference "
                             "and decode the EFM audio. Heavy steps run one at a time.",
            "dvd_button": "Choose this episode's DVD (a file found by its episode number, or one you browse to), "
                          "or record that it has none. Needed before the delivery plan.",
            "efm_button": "Review the decoded EFM audio of this episode's captures and choose which one to use.",
            "delivery_button": "Review a finished delivery: the A/V check and the evidence, then approve or send it "
                               "back for corrections.",
            "run_button": "Run a saved plan file for this episode (advanced; Run intake and Run to delivery write "
                          "and run the usual plans for you).",
            "cancel_button": "Stop the running plan. Finished steps are kept; the step in progress starts over "
                             "next time.",
        })
        self.tabs.setTabToolTip(0, "Progress of the running plan: each step, how far it is, and the time left.")
        self.tabs.setTabToolTip(1, "All recorded values (the manifest) for the selected episode.")
        self.tabs.setTabToolTip(2, "Plans that ran for the selected episode.")
        self.tabs.setTabToolTip(3, "Checks waiting for a person.")
        self.tabs.setTabToolTip(4, "Run several episodes one after another, unattended.")
        apply_common(self)

    def show_override_flag(self, episode: str) -> None:
        """Flag an episode whose output settings are its own (not the defaults), with what differs and since when."""
        try:
            info = self.services.output_override_info(episode)
        except FstkError:
            info = None
        if not info:
            self.override_flag.setVisible(False)
            return
        from fstk.config.output import override_lines

        lines = override_lines(info)
        n = sum(len(v) for v in info["groups"].values())
        self.override_flag.setText("⚑ Own output settings"
                                   + (f" ({n} differ from the defaults)" if n else " (same as the defaults)")
                                   + ("  — defaults changed since" if info["defaults_changed_since"] else ""))
        self.override_flag.setToolTip("\n".join(lines) + "\n\nClick to open Settings for this episode.")
        self.override_flag.setVisible(True)

    def _check_volumes(self) -> None:
        """Say so up front when a folder sits on a drive that cannot hold the work (first run on a small system drive)."""
        from fstk.workspace.volumes import advise

        try:
            advice = [a for a in advise(self.services.profile) if a.level in ("warn", "block")]
        except Exception:
            LOG.exception("drive check failed")
            return
        for a in advice:
            self.append_log(f"drive check: {a.text}")
        blocking = [a for a in advice if a.level == "block"]
        seen = list(self.services.profile.ui.get("volume_advice_seen") or [])
        fresh = [a for a in blocking if f"{a.what}|{a.path}" not in seen]
        if not fresh:
            return
        self.show_volume_advice(fresh)
        self.services.profile.ui["volume_advice_seen"] = seen + [f"{a.what}|{a.path}" for a in fresh]
        with contextlib.suppress(Exception):
            self.services.profile.save()

    def show_volume_advice(self, advice) -> None:
        """One dialog per problem folder, once: the work cannot start there. (A method so tests can replace it.)"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Not enough room for the work")
        box.setText("\n\n".join(a.text for a in advice))
        box.setInformativeText("Open Settings → Folders to pick a drive with more room. This message is shown once "
                               "per folder; the same advice stays in Settings → Folders.")
        box.exec()

    def _resume_queue(self) -> None:
        """The queue was running when the app last closed: carry on with what is waiting."""
        try:
            self.queue_runner.start()
        except FstkError as exc:                       # another window already runs it
            LOG.info("queue not resumed: %s", exc)
            self.append_log(f"queue not resumed: {exc}")
        else:
            self.append_log("queue resumed: it was running when the app closed")
        self.queue_panel.refresh()

    def closeEvent(self, event) -> None:
        """Closing mid-run stops it cleanly: tools are ended, unfinished files removed, state saved."""
        queue_busy = getattr(self, "queue_runner", None) is not None and self.queue_runner.busy()
        if (self.thread is None or self.worker is None) and not queue_busy:
            if getattr(self, "queue_runner", None) is not None:
                self.queue_runner.shutdown(5)
            event.accept()
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("A run is in progress")
        box.setText(("The queue is working" if queue_busy and self.thread is None else "A plan is still running")
                    + ".\n\nStop it and close? Finished steps and captures are saved and "
                    "picked up again next time; only the piece being worked on right now starts over.")
        stop = box.addButton("Stop and close", QMessageBox.AcceptRole)
        box.addButton("Keep running", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not stop:
            event.ignore()
            return
        LOG.info("action close-while-running: stopping")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            stopped = True
            if queue_busy:              # queued items go back to queued and resume next time the app opens
                stopped = self.queue_runner.shutdown(120)
            if self.thread is not None and self.worker is not None:
                self.cancel_run()
                self.thread.quit()
                stopped = self.thread.wait(120_000) and stopped
        finally:
            QApplication.restoreOverrideCursor()
        if not stopped:
            LOG.warning("run did not stop within 120 s; closing anyway (saved state is kept)")
        event.accept()

    def cancel_run(self) -> None:
        if self.worker and self.worker.engine:
            self.worker.engine.cancel.set()
            self.worker.engine.resume()                  # a paused run must wake up to cancel
        self.append_log("cancel requested")

    def toggle_pause(self) -> None:
        LOG.info("action pause-toggle")
        engine = self.worker.engine if self.worker else None
        if engine is None:
            return
        from fstk.tools.process import PAUSE

        if PAUSE.is_paused:
            engine.resume()
            self.pause_button.setText("Pause")
        else:
            engine.pause()
            self.pause_button.setText("Resume")
        self.update_progress()

    def recheck_space(self) -> None:
        """Refresh icon: re-measure disk space for every step blocked by it; clear the ones that now fit."""
        if self.tracker is None or not self.episode:
            return
        job = self.tracker.plan["job"]
        engine = self.worker.engine if (self.thread is not None and self.worker) else None
        try:
            results = self.services.recheck_space(self.episode, job, events=self.on_event, engine=engine)
        except FstkError as exc:
            QMessageBox.warning(self, "Cannot re-check space", str(exc))
            return
        if not results:
            self.append_log("space re-check: no step is blocked by disk space")
        for sid, check in results.items():
            self.append_log(f"space re-check {sid}: " + ("enough space now, step is pending again" if check.ok
                                                          else check.describe()))
        self.update_progress()
        if self.thread is None:
            self.show_episode(self.episode)

    # ---------------------------------------------------------------- progress
    def _begin_progress(self, plan_path: str) -> None:
        from fstk.workflow.engine import load_plan

        try:
            plan = load_plan(plan_path)
        except FstkError:
            self.tracker = None
            return
        self.watch.stop()
        self.tracker = ProgressTracker(plan, timings=self.services.timings())
        with contextlib.suppress(OSError, ValueError, KeyError):   # first run of this job: nothing to resume
            self.tracker.load_state(self.services.job_state(plan["episode"], plan["job"]))
        self.progress_panel.set_tracker(self.tracker)
        self.tabs.setCurrentWidget(self.progress_panel)
        self.tick.start()
        self.update_progress()

    def _show_saved_progress(self, episode: str) -> None:
        """Between runs: the latest job's saved state, so progress survives closing the app."""
        if self.thread is not None:
            return
        jobs = self.services.jobs(episode)
        if not jobs:
            self.status_line.setText("")
            self.progress_panel.set_tracker(None)
            return
        best = None
        for job in jobs:
            state = self.services.job_state(episode, job)
            if best is None or state.get("created_utc", "") >= best[1].get("created_utc", ""):
                best = (job, state)
        job, state = best
        runner = self.services.job_runner(episode, job)
        if runner == os.getpid():
            runner = None
        note = f" · running in another window (pid {runner})" if runner else ""
        self.status_line.setText(f"{job}: {describe_job_state(state)}{note}")
        if runner:
            self.watch.start()                          # follow the other window's progress from the saved state
        else:
            self.watch.stop()
        plan = self.services.job_plan(episode, job)
        if plan:
            self.tracker = ProgressTracker(plan, timings=self.services.timings())
            self.tracker.load_state(state, live=bool(runner))
            self.progress_panel.set_tracker(self.tracker)
        else:
            self.progress_panel.set_tracker(None)

    def update_progress(self) -> None:
        snap = self.progress_panel.refresh()
        if snap is None:
            return
        name = f"{self.episode or ''} — {snap.overall:.0%} — {snap.headline()}" if self.thread else ""
        self.setWindowTitle(f"{name} · FromStunToKill" if name else f"FromStunToKill — {self.services.profile.machine}")
        self._taskbar_update(snap)

    def _taskbar_update(self, snap) -> None:
        from fstk.ui import taskbar

        if self._taskbar is None:
            self._taskbar = taskbar.TaskbarProgress(int(self.winId()))
        if self.thread is None:
            self._taskbar.set(taskbar.NOPROGRESS)
        elif snap.paused:
            self._taskbar.set(taskbar.PAUSED, snap.overall)
        elif snap.state == "failed":
            self._taskbar.set(taskbar.ERROR, snap.overall)
        else:
            self._taskbar.set(taskbar.NORMAL, snap.overall)

    def notify(self, title: str, text: str) -> None:
        if not self.notify_button.isChecked() or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray.show()
        self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 15000)

    @Slot(dict)
    def on_event(self, event: dict) -> None:
        if self.tracker is not None:
            self.tracker.on_event(event)
            if event["kind"] != "progress":
                self.update_progress()
        kind = event["kind"]
        if kind == "step-awaiting-review":
            self.notify("Needs your review", f"{self.episode}: {event.get('step')} is ready for you")
        elif kind in ("step-failed", "step-blocked"):
            self.notify("Step failed", f"{self.episode}: {event.get('step')} {'; '.join(event.get('issues') or [])}"[:200])
        if event["kind"] == "progress":
            frac = event.get("fraction")
            self.append_log(f"  [{event['step']}] {'' if frac is None else f'{frac:5.0%} '}{event.get('message', '')}")
        else:
            extra = event.get("issues") or event.get("detail") or ""
            self.append_log(f"{event['kind']} {event.get('step', '')} {extra}")

    def _done(self) -> None:
        if self.thread:
            self.thread.quit()
            self.thread.wait()
        self.thread, self.worker = None, None
        self.run_button.setEnabled(True)
        self.intake_button.setEnabled(True)
        self.deliver_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.pause_button.setText("Pause")
        self.tick.stop()
        self.update_progress()
        self._running_plan = None
        self.refresh()
        if self.episode:
            self.show_episode(self.episode)
        if self._queued_plans:
            nxt = self._queued_plans.pop(0)
            self.append_log(f"starting the queued plan {nxt}")
            QTimer.singleShot(0, lambda: self.start_plan(nxt))

    @Slot(dict)
    def on_finished(self, state: dict) -> None:
        self.append_log(f"job {state['job']} finished: " +
                        ", ".join(f"{k}={v['status']}" for k, v in state["steps"].items()))
        if self.tracker is not None:
            snap = self.tracker.snapshot()
            if snap.state == "finished":
                self.notify("Run complete", f"{self.episode}: all {snap.total} steps done")
        self._done()
        waiting = [s for s, st in state["steps"].items() if st.get("status") == "awaiting-review"]
        failed = [s for s, st in state["steps"].items() if st.get("status") in ("failed", "blocked")]
        if waiting and not self._queued_plans:
            self.set_banner(f"Waiting for you: {', '.join(waiting)} — look at it on the Reviews tab and decide.",
                            "warn")
            self.tabs.setCurrentWidget(self.review_page)
        elif failed:
            self.set_banner(f"Stopped: {', '.join(failed[:3])} needs attention (see Jobs and the log).", "bad")
        elif all(st.get("status") == "passed" for st in state["steps"].values()):
            self.set_banner(f"Finished: all {len(state['steps'])} steps of {state['job']} are done.", "good")

    @Slot(str)
    def on_failed(self, message: str) -> None:
        self.append_log(f"ERROR: {message}")
        self._done()
        first = message.splitlines()[0] if message else "The run stopped."
        self.set_banner(first, "bad")
        if message.startswith("Not started:"):            # a tool check failed before anything ran
            self.show_run_problem(message)

    def show_run_problem(self, message: str) -> None:
        """Say why a plan did not start (a method so tests can replace the dialog)."""
        QMessageBox.warning(self, "Plan not started", message)

    def append_log(self, text: str) -> None:
        LOG.info("%s", text)
        self.log.appendPlainText(text)


def main(argv: list[str] | None = None) -> int:
    real_run = argv is None
    argv = list(sys.argv[1:] if argv is None else argv)
    app = QApplication.instance() or QApplication([sys.argv[0], *argv])
    if real_run:                                     # not a test: keep an application log and catch crashes
        from fstk.core import applog

        applog.setup_logging()
        applog.install_qt_handler()
    profile = argv[argv.index("--profile") + 1] if "--profile" in argv else None
    try:
        services = Services.load(profile)
    except FstkError as exc:
        QMessageBox.critical(None, "FromStunToKill", str(exc))
        return 2
    from fstk.ui.services import apply_app_prefs

    apply_app_prefs(services.profile)
    if real_run:
        applog.log_environment(services.profile)
    window = MainWindow(services)
    if real_run:                                     # errors in buttons/windows are shown, not only logged
        _report_ui_error.stamp = window._code_at_start
        previous = sys.excepthook

        def hook(exc_type, exc, tb):
            previous(exc_type, exc, tb)
            _report_ui_error(exc_type, exc, tb)

        sys.excepthook = hook
    window.show()
    code = app.exec()
    if real_run:
        applog.shutdown()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
