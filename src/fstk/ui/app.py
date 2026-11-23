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
import socket
import sys
from pathlib import Path

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
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
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

STATUS_COLOURS = {"passed": "#2e7d32", "failed": "#c62828", "blocked": "#ef6c00",
                  "awaiting-review": "#1565c0", "running": "#6a1b9a", "pending": "#616161",
                  "cancelled": "#616161"}


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
        self.actor = actor or f"{getpass.getuser()}@{socket.gethostname()}"
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

        self.reviews = QListWidget()
        self.reviews.currentRowChanged.connect(self.show_review)
        self.review_detail = QPlainTextEdit()
        self.review_detail.setReadOnly(True)
        self.open_evidence = QPushButton("Open evidence")
        self.open_evidence.clicked.connect(self.open_selected_evidence)
        self.approve = QPushButton("Approve…")
        self.reject = QPushButton("Reject…")
        self.approve.clicked.connect(lambda: self.decide("APPROVED"))
        self.reject.clicked.connect(lambda: self.decide("REJECTED"))
        review_buttons = QHBoxLayout()
        for b in (self.open_evidence, self.approve, self.reject):
            review_buttons.addWidget(b)
        review_page = QWidget()
        rl = QVBoxLayout(review_page)
        rl.addWidget(self.reviews, 1)
        rl.addWidget(self.review_detail, 1)
        rl.addLayout(review_buttons)

        self.progress_panel = ProgressPanel()
        self.status_line = QLabel("")
        self.tracker: ProgressTracker | None = None
        self.tabs = QTabWidget()
        self.tabs.addTab(self.progress_panel, "Progress")
        self.tabs.addTab(self.values, "Manifest")
        self.tabs.addTab(self.jobs, "Jobs")
        self.tabs.addTab(review_page, "Reviews")

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
        self.efm_button = QPushButton("EFM capture…")
        self.efm_button.clicked.connect(self.open_efm_review)
        self.delivery_button = QPushButton("Delivery review…")
        self.delivery_button.clicked.connect(self.open_delivery_review)
        self.handoff_button = QPushButton("Hand off…")
        self.handoff_button.setToolTip("Freeze this episode here and hand it to another machine (shared workspace)")
        self.handoff_button.clicked.connect(self.ask_handoff)
        self.run_button = QPushButton("Run plan…")
        self.run_button.clicked.connect(self.choose_and_run)
        self.folders_button = QPushButton("Folders…")
        self.folders_button.setToolTip("Where fstk keeps its state and does its work")
        self.folders_button.clicked.connect(self.open_folders)
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
        self.notify_button.setChecked(True)
        self.notify_button.setToolTip("Desktop notification when a step needs you, a step fails, or the run ends")
        self.tray = QSystemTrayIcon(self.windowIcon() or QApplication.windowIcon(), self)
        self.tick = QTimer(self)
        self.tick.setInterval(1000)
        self.tick.timeout.connect(self.update_progress)
        self._taskbar = None

        centre = QWidget()
        cl = QVBoxLayout(centre)
        cl.addWidget(self.title)
        cl.addWidget(self.status_line)
        cl.addWidget(self.tabs, 1)
        bar = QHBoxLayout()
        bar.addWidget(self.pick_button)
        bar.addWidget(self.intake_button)
        bar.addWidget(self.deliver_button)
        bar.addWidget(self.efm_button)
        bar.addWidget(self.delivery_button)
        bar.addWidget(self.handoff_button)
        bar.addWidget(self.run_button)
        bar.addWidget(self.pause_button)
        bar.addWidget(self.cancel_button)
        bar.addStretch(1)
        bar.addWidget(self.notify_button)
        bar.addWidget(self.folders_button)
        cl.addLayout(bar)

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
                job_rows.append((job, sid, st.get("status", ""), "; ".join(st.get("issues") or [])))
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
            self.reviews.addItem(f"{p.review}  ({p.job} / {p.step})")
        self.tabs.setTabText(3, f"Reviews ({len(self._pending)})")
        self.show_review(self.reviews.currentRow())

    @staticmethod
    def _colour(status: str):
        from PySide6.QtGui import QColor

        return QColor(STATUS_COLOURS.get(status, "#616161"))

    @Slot(int)
    def show_review(self, row: int) -> None:
        has = 0 <= row < len(getattr(self, "_pending", []))
        for b in (self.open_evidence, self.approve, self.reject):
            b.setEnabled(has)
        if not has:
            self.review_detail.setPlainText("No step is waiting for review.")
            return
        p = self._pending[row]
        self.review_detail.setPlainText(
            f"Review: {p.review}\nStage: {p.stage}\nJob/step: {p.job}/{p.step}\n\nEvidence you are deciding on:\n"
            + "\n".join(f"  {e}" for e in p.evidence) + f"\n\n{p.detail}")

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

    # ---------------------------------------------------------------- review windows
    def open_efm_review(self):
        from fstk.ui.reviews import EfmCaptureReview

        if not self.episode:
            return None
        dlg = EfmCaptureReview(self.services, self.episode, parent=self)
        dlg.plan_ready.connect(self.start_plan)
        dlg.open()
        return dlg

    def open_delivery_review(self):
        from fstk.ui.reviews import DeliveryReview

        if not self.episode:
            return None
        dlg = DeliveryReview(self.services, self.episode, actor=self.actor, parent=self)
        dlg.plan_ready.connect(self.start_plan)
        dlg.decided.connect(lambda path: (self.append_log(f"delivery decision recorded: {path}"),
                                          self.show_episode(self.episode)))
        dlg.open()
        return dlg

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

    def open_folders(self) -> None:
        from fstk.ui.folders import FoldersDialog

        dlg = FoldersDialog(self.services.profile, self)
        if dlg.exec() and dlg.changed:
            self.log.appendPlainText("folders: " + "; ".join(f"{k} → {n or '(default)'}" for k, _o, n in dlg.changed))
            self.refresh()

    def run_intake(self) -> None:
        if not self.episode:
            return
        try:
            path = self.services.write_intake_plan(self.episode)
        except FstkError as exc:
            QMessageBox.warning(self, "Cannot plan intake", str(exc))
            return
        self.start_plan(str(path))

    def run_delivery(self) -> None:
        if not self.episode:
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

    def start_plan(self, path: str) -> None:
        if self.thread is not None:
            return
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

    def cancel_run(self) -> None:
        if self.worker and self.worker.engine:
            self.worker.engine.cancel.set()
            self.worker.engine.resume()                  # a paused run must wake up to cancel
        self.append_log("cancel requested")

    def toggle_pause(self) -> None:
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

    # ---------------------------------------------------------------- progress
    def _begin_progress(self, plan_path: str) -> None:
        from fstk.workflow.engine import load_plan

        try:
            plan = load_plan(plan_path)
        except FstkError:
            self.tracker = None
            return
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
        self.status_line.setText(f"{job}: {describe_job_state(state)}")
        plan = self.services.job_plan(episode, job)
        if plan:
            self.tracker = ProgressTracker(plan, timings=self.services.timings())
            self.tracker.load_state(state)
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
        self.refresh()
        if self.episode:
            self.show_episode(self.episode)

    @Slot(dict)
    def on_finished(self, state: dict) -> None:
        self.append_log(f"job {state['job']} finished: " +
                        ", ".join(f"{k}={v['status']}" for k, v in state["steps"].items()))
        if self.tracker is not None:
            snap = self.tracker.snapshot()
            if snap.state == "finished":
                self.notify("Run complete", f"{self.episode}: all {snap.total} steps done")
        self._done()

    @Slot(str)
    def on_failed(self, message: str) -> None:
        self.append_log(f"ERROR: {message}")
        self._done()

    def append_log(self, text: str) -> None:
        self.log.appendPlainText(text)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    app = QApplication.instance() or QApplication([sys.argv[0], *argv])
    profile = argv[argv.index("--profile") + 1] if "--profile" in argv else None
    try:
        services = Services.load(profile)
    except FstkError as exc:
        QMessageBox.critical(None, "FromStunToKill", str(exc))
        return 2
    window = MainWindow(services)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
