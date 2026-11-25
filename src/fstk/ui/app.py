"""FromStunToKill desktop app (PySide6). First shell over ``fstk.ui.services``.

Layout (UI clean-up 2026-11-25, see the project's UI-ASSESSMENT)
  menus  – File (settings, notifications), Episode (find, DVD, reviews, hand off, remove), Run, Help (log, bundle)
  left   – episodes, each with its title and where it stands (needs you, a step failed, running, delivered)
  centre – the title with the one Run button (offers intake or delivery, whichever is next) and the notifications
           bell; a banner saying what needs doing next, with buttons that do it; tabs: Progress (headline, overall
           bar with Pause/Cancel, run picker, steps), Reviews (everything waiting for a person), Queue, Details
           (the manifest, grouped and searchable)
  bottom – the latest log line; the full log opens on demand or when something goes wrong

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
import html
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import QDir, QObject, QProcess, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSystemTrayIcon,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from fstk.core.errors import FstkError
from fstk.core.identity import EpisodeId
from fstk.ui.colours import quiet_css, tone
from fstk.ui.details import DetailsPanel
from fstk.ui.progress_panel import ProgressPanel
from fstk.ui.services import Services
from fstk.ui.step_help import step_name
from fstk.workflow.progress import STATUS_WORDS, ProgressTracker, worst_first

LOG = logging.getLogger("fstk.ui")


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


class CallWorker(QObject):
    """Runs one service call off the GUI thread (the opening pack's DVD decode takes about a minute)."""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, call):
        super().__init__()
        self.call = call

    @Slot()
    def run(self) -> None:
        try:
            self.done.emit(self.call())
        except FstkError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # surface anything unexpected instead of dying silently
            self.failed.emit(f"internal error: {exc!r}")


class Banner(QFrame):
    """The one place that says what needs doing next, with buttons that do it (and what just happened)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.label = QLabel("")
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(6)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 6, 6)
        lay.addWidget(self.label, 1)
        lay.addLayout(self.buttons)
        self.close_button = QToolButton()
        self.close_button.setText("✕")
        self.close_button.setAutoRaise(True)
        self.close_button.setToolTip("Hide this message")
        self.close_button.clicked.connect(lambda: self.setVisible(False))
        lay.addWidget(self.close_button)
        self.kind = ""                                  # "needs" (what to do next) or "event" (what just happened)
        self.setObjectName("banner")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setVisible(False)

    def text(self) -> str:
        return self.label.text()

    def show_message(self, text: str, tone: str, actions=(), kind: str = "event") -> None:
        from fstk.ui.visual import tone_colour

        while self.buttons.count():
            w = self.buttons.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self.label.setText(text)
        self.kind = kind
        self.setStyleSheet(f"#banner {{ background: {tone_colour(tone)}; border-radius: 4px; }} "
                           "#banner QLabel { color: white; font-weight: 600; background: transparent; } "
                           "#banner QToolButton { color: white; }")
        for label, slot in actions:
            b = QPushButton(label)
            b.clicked.connect(slot)
            self.buttons.addWidget(b)
        self.setVisible(bool(text))


class MainWindow(QMainWindow):
    def __init__(self, services: Services, *, actor: str | None = None):
        super().__init__()
        from fstk.ui import theme

        theme.apply()
        self.services = services
        self.actor = actor or str((services.profile.ui or {}).get("operator") or "").strip() \
            or f"{getpass.getuser()}@{socket.gethostname()}"
        self.thread: QThread | None = None
        self.worker: PlanWorker | None = None
        self.setWindowTitle(f"FromStunToKill — {services.profile.machine}")
        self.resize(1280, 800)
        self.catalog_thread: QThread | None = None
        self._picked_job: str | None = None             # a run chosen in the run picker (None: the latest)
        QTimer.singleShot(1500, self.check_catalog_age)

        # ---- episode list (left): title and where each episode stands
        self.episodes = QListWidget()
        self.episodes.setObjectName("episodeList")
        self.episodes.currentItemChanged.connect(lambda cur, _prev: self.show_episode(self._canonical(cur)))
        self.episodes.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.episodes.customContextMenuRequested.connect(self._episode_menu)
        self.episodes.setSpacing(1)

        # ---- header: title, the one Run button, notifications
        self.title = QLabel("No episode selected")
        self.title.setObjectName("episodeTitle")
        self.title.setWordWrap(True)
        self.override_flag = QPushButton()
        self.override_flag.setFlat(True)
        self.override_flag.setStyleSheet(f"text-align: left; color: {tone('warn').name()}; font-weight: 600;")
        self.override_flag.setCursor(Qt.CursorShape.PointingHandCursor)
        self.override_flag.clicked.connect(self.open_settings)
        self.override_flag.setVisible(False)
        self._build_actions()
        self.run_menu = QMenu(self)
        for a in (self.intake_button, self.deliver_button):
            self.run_menu.addAction(a)
        self.run_menu.addSeparator()
        self.run_menu.addAction(self.run_button)
        self.main_run = QToolButton()
        self.main_run.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.main_run.setMenu(self.run_menu)
        self.main_run.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.main_run.setObjectName("primary")
        self.main_run.setDefaultAction(self.deliver_button)
        self.notify_bell = QToolButton()
        self.notify_bell.setDefaultAction(self.notify_button)
        self.notify_bell.setAutoRaise(True)
        self.notify_button.toggled.connect(self._bell)
        self._bell(self.notify_button.isChecked())

        # ---- the Reviews tab: one front door for everything waiting for a person
        from fstk.ui.visual import DetailsToggle, SpotViewer, TimelineBar

        self.reviews = QListWidget()
        self.reviews.setMaximumHeight(72)
        self.reviews.currentRowChanged.connect(self.show_review)
        self.review_headline = QLabel("")
        self.review_headline.setWordWrap(True)
        self.review_headline.setTextFormat(Qt.TextFormat.RichText)
        self.review_how = QLabel("")
        self.review_how.setWordWrap(True)
        self.review_how.setStyleSheet(f"color: {quiet_css()};")
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
        self.review_empty = QLabel("Nothing is waiting for you.")
        self.review_empty.setStyleSheet("font-size: 14px;")
        self.review_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.review_other = QLabel('Also: <a href="efm">EFM capture choice…</a> · '
                                   '<a href="delivery">Delivery A/V check…</a>')
        self.review_other.setToolTip("The review windows you can open at any time, waiting or not.")
        self.review_other.linkActivated.connect(lambda which: self.open_efm_review() if which == "efm"
                                                else self.open_delivery_review())
        review_buttons = QHBoxLayout()
        review_buttons.addWidget(self.review_details_toggle)
        review_buttons.addStretch(1)
        for b in (self.open_evidence, self.reject, self.approve):
            review_buttons.addWidget(b)
        self.review_controls = QWidget()
        rc = QVBoxLayout(self.review_controls)
        rc.setContentsMargins(0, 0, 0, 0)
        rc.addWidget(self.reviews)
        rc.addWidget(self.review_headline)
        rc.addWidget(self.review_timeline)
        rc.addWidget(self.review_spots, 5)
        rc.addWidget(self.review_scroll, 5)
        rc.addWidget(self.review_how)
        rc.addWidget(self.review_detail, 2)
        rc.addLayout(review_buttons)
        review_page = QWidget()
        rl = QVBoxLayout(review_page)
        rl.addWidget(self.review_empty, 1)
        rl.addWidget(self.review_controls, 1)
        rl.addWidget(self.review_other)
        self.review_page = review_page

        self.banner = Banner()
        self._queued_plans: list[str] = []
        self._running_plan: str | None = None
        self.progress_panel = ProgressPanel()
        self.progress_panel.final_file_source = lambda: self.services.final_file(self.episode) if self.episode else None
        self.progress_panel.reveal_file.connect(lambda p: self.reveal_file(Path(p)))
        self.progress_panel.space_refresh.connect(self.recheck_space)
        self.progress_panel.pack_save.connect(self.save_opening_pack)
        self.progress_panel.restart_from.connect(self.restart_step)
        self.progress_panel.open_step_log.connect(self.open_step_log)
        self.progress_panel.open_step_report.connect(self.open_step_report)
        self.progress_panel.pause_clicked.connect(self.toggle_pause)
        self.progress_panel.cancel_clicked.connect(self.cancel_run)
        self.progress_panel.run_chosen.connect(self.choose_run)
        self.pause_button = self.progress_panel.pause_button
        self.cancel_button = self.progress_panel.cancel_button
        self.pack_thread: QThread | None = None
        self.progress_panel.set_column_widths(self.services.app_prefs().get("progress_columns"))
        self.progress_panel.columns_resized.connect(self._remember_progress_columns)
        self.tracker: ProgressTracker | None = None
        self.details = DetailsPanel()
        self.values = self.details
        self.tabs = QTabWidget()
        self.tabs.addTab(self.progress_panel, "Progress")
        self.tabs.addTab(review_page, "Reviews")
        from fstk.ui.queue_panel import QueuePanel

        self.queue_runner = services.queue_runner(actor=self.actor)
        self.queue_panel = QueuePanel(self.queue_runner, current_episode=lambda: self.episode,
                                      episodes=lambda: [str(e) for e in self.services.episodes()],
                                      notify=self.notify, sleep=self.services.sleep_computer)
        self.tabs.addTab(self.queue_panel, "Queue")
        self.tabs.addTab(self.details, "Details")
        if self.queue_runner.running and any(i.status in ("queued", "held") for i in self.queue_runner.store.items):
            QTimer.singleShot(3000, self._resume_queue)
        QTimer.singleShot(1500, self._check_volumes)

        # ---- the log: a one-line strip with the latest message; the full log opens on demand or on a problem
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self._log_lines: list[tuple[str, str]] = []
        self.log_last = QLabel("")
        self.log_last.setStyleSheet(f"color: {quiet_css()};")
        self.log_last.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.log_toggle = QToolButton()
        self.log_toggle.setCheckable(True)
        self.log_toggle.setAutoRaise(True)
        self.log_toggle.toggled.connect(self.set_log_open)
        self.log_problems = QCheckBox("Only problems")
        self.log_problems.setToolTip("Show only warnings and errors in the log")
        self.log_problems.toggled.connect(self._refill_log)
        strip = QHBoxLayout()
        strip.setContentsMargins(4, 0, 4, 0)
        strip.addWidget(self.log_toggle)
        strip.addWidget(self.log_last, 1)
        strip.addWidget(self.log_problems)
        self.log_strip = QWidget()
        self.log_strip.setLayout(strip)

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
        head = QHBoxLayout()
        head.addWidget(self.title, 1)
        head.addWidget(self.main_run)
        head.addWidget(self.notify_bell)
        cl.addLayout(head)
        cl.addWidget(self.override_flag)
        cl.addWidget(self.banner)
        cl.addWidget(self.tabs, 1)

        self.split = QSplitter(Qt.Orientation.Vertical)
        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(self.episodes)
        top.addWidget(centre)
        top.setStretchFactor(1, 4)
        top.setSizes([230, 1050])
        self.split.addWidget(top)
        self.split.addWidget(self.log)
        self.split.setStretchFactor(0, 4)
        outer = QWidget()
        ol = QVBoxLayout(outer)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(0)
        ol.addWidget(self.split, 1)
        ol.addWidget(self.log_strip)
        self.setCentralWidget(outer)
        self.set_log_open(bool(self.services.app_prefs().get("log_open", False)), remember=False)
        self.refresh()

    # ---------------------------------------------------------------- menus and actions
    def _act(self, text: str, slot, tip: str = "", shortcut: str | None = None) -> QAction:
        a = QAction(text, self)
        a.triggered.connect(lambda _=False: slot())
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        return a

    def _build_actions(self) -> None:
        """Every command, once, as an action: the menus, the Run button and right-click menus share them."""
        A = self._act
        self.pick_button = A("Find or add episode…", self.open_picker,
                             "Find an episode in the archive catalog, or add one from a folder you already have, then "
                             "create or open its workspace.", "Ctrl+O")
        self.dvd_button = A("Choose DVD…", self.open_dvd,
                            "Choose this episode's DVD (a file found by its episode number, or one you browse to), "
                            "or record that it has none. Needed before the delivery plan.")
        self.efm_button = A("EFM capture choice…", self.open_efm_review,
                            "Review the decoded EFM audio of this episode's captures and choose which one to use.")
        self.delivery_button = A("Delivery A/V check…", self.open_delivery_review,
                                 "Review a finished delivery: the A/V check and the evidence, then approve or send it "
                                 "back for corrections.")
        self.handoff_button = A("Hand off to another machine…", self.ask_handoff,
                                "Freeze this episode here and hand it to another machine (shared workspace)")
        self.leftovers_action = A("Remove leftover files…", self.open_leftovers,
                                  "After the final output is approved: delete files of failed or replaced attempts "
                                  "that cannot be used again (it lists them first). Done for you on approval; use this "
                                  "when that was skipped.")
        self.remove_button = A("Remove episode…", self.open_remove,
                               "Remove the selected episode (and, if you choose, its files)")
        self.intake_button = A("Run intake", self.run_intake,
                               "Intake: download the captures (if needed), inventory them, choose the stack reference "
                               "and decode the EFM audio. Heavy steps run one at a time.")
        self.deliver_button = A("Run to delivery", self.run_delivery,
                                "Stack render → … → QC, then the A/V check (DVD or no-DVD route)", "Ctrl+R")
        self.run_button = A("Run a plan file… (advanced)", self.choose_and_run,
                            "Run a saved plan file for this episode (advanced; Run intake and Run to delivery write "
                            "and run the usual plans for you).")
        self.settings_button = A("Settings…", self.open_settings,
                                 "Video and audio quality (defaults and per episode), folders, tools, logging",
                                 "Ctrl+,")
        self.notify_button = A("Desktop notifications", lambda: None,
                               "Desktop notification when a step needs you, a step fails, or the run ends")
        self.notify_button.setCheckable(True)
        self.notify_button.setChecked(bool((self.services.profile.ui or {}).get("notifications", True)))
        self.logs_button = A("Open logs", self.open_logs, "Open the folder with the application log and crash log")
        self.bundle_button = A("Save debug bundle…", self.save_bundle,
                               "Zip the logs, settings, job state and tool logs (no media) to send for a bug report")
        self.show_log_action = A("Show the log", lambda: None, "Show or hide the full log at the bottom", "Ctrl+L")
        self.show_log_action.setCheckable(True)
        self.show_log_action.toggled.connect(lambda on: self.set_log_open(on))
        self.pause_action = A("Pause / Resume", self.toggle_pause, "Freeze or continue the running tools")
        self.cancel_action = A("Cancel the run", self.cancel_run, "Stop the run; finished steps are kept")
        quit_action = A("Quit", self.close, "", "Ctrl+Q")

        bar = self.menuBar()
        m = bar.addMenu("&File")
        m.addAction(self.settings_button)
        m.addAction(self.notify_button)
        m.addSeparator()
        m.addAction(quit_action)
        m = bar.addMenu("&Episode")
        for a in (self.pick_button, None, self.dvd_button, self.efm_button, self.delivery_button, None,
                  self.leftovers_action, self.handoff_button, self.remove_button):
            m.addSeparator() if a is None else m.addAction(a)
        m = bar.addMenu("&Run")
        for a in (self.intake_button, self.deliver_button, None, self.pause_action, self.cancel_action, None,
                  self.run_button):
            m.addSeparator() if a is None else m.addAction(a)
        m = bar.addMenu("&Help")
        for a in (self.show_log_action, self.logs_button, self.bundle_button):
            m.addAction(a)

    def _bell(self, on: bool) -> None:
        self.notify_button.setText("🔔" if on else "🔕")
        self.notify_button.setIconText("🔔" if on else "🔕")
        self.notify_button.setToolTip(("Desktop notifications are on" if on else "Desktop notifications are off")
                                      + ": a notice when a step needs you, a step fails, or the run ends. Click to "
                                        "turn them " + ("off." if on else "on."))

    def set_run_enabled(self, enabled: bool) -> None:
        for a in (self.intake_button, self.deliver_button, self.run_button):
            a.setEnabled(enabled)
        self.main_run.setEnabled(enabled)
        self.pause_action.setEnabled(not enabled and self.thread is not None)
        self.cancel_action.setEnabled(self.thread is not None)

    def _update_run_button(self, episode: str | None) -> None:
        """The Run button offers the next sensible job: intake until it is done, then delivery."""
        intake_next = False
        if episode:
            with contextlib.suppress(FstkError, OSError, ValueError):
                intake_next = not self.services.intake_done(episode)
        self.main_run.setDefaultAction(self.intake_button if intake_next else self.deliver_button)

    # ---------------------------------------------------------------- log pane
    def set_log_open(self, on: bool, remember: bool = True) -> None:
        self.log.setVisible(on)
        for w in (self.log_toggle, self.show_log_action):
            w.blockSignals(True)
            w.setChecked(on)
            w.blockSignals(False)
        self.log_toggle.setText("▾ Hide log" if on else "▴ Show log")
        self.log_problems.setVisible(on)
        if on:
            sizes = self.split.sizes()
            if len(sizes) == 2 and sizes[1] < 80:
                total = sum(sizes) or 800
                self.split.setSizes([int(total * 0.75), int(total * 0.25)])
        if remember:
            with contextlib.suppress(Exception):
                self.services.save_app_prefs({"log_open": bool(on)})

    def _refill_log(self) -> None:
        only = self.log_problems.isChecked()
        self.log.clear()
        for text, level in self._log_lines[-5000:]:
            if not only or level != "info":
                self._put_log(text, level)

    def _put_log(self, text: str, level: str) -> None:
        if level == "info":
            self.log.appendPlainText(text)
        else:
            colour = tone("bad" if level == "error" else "warn").name()
            self.log.appendHtml(f'<span style="color:{colour}">{html.escape(text)}</span>')

    # ---------------------------------------------------------------- data
    @staticmethod
    def _canonical(item) -> str:
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else ""

    def refresh(self) -> None:
        current = self.episode
        self.episodes.blockSignals(True)
        self.episodes.clear()
        for ep in self.services.episodes():
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, ep.canonical)
            self._label_episode(item, ep.canonical)
            self.episodes.addItem(item)
        self.episodes.blockSignals(False)
        target = self._find_episode(current) if current else None
        if target is None and self.episodes.count():
            target = self.episodes.item(0)
        if target is not None:
            self.episodes.setCurrentItem(target)
            if current and self._canonical(target) == current:
                self.show_episode(current)

    def _label_episode(self, item, canonical: str) -> None:
        try:
            title = self.services.title(canonical)
        except (FstkError, OSError, ValueError):
            title = ""
        status, tn = "", "quiet"
        with contextlib.suppress(FstkError, OSError, ValueError, KeyError):
            status, tn = self.services.episode_status(canonical)
        if self.thread is not None and self._running_plan and canonical == self.episode and self.tracker:
            status, tn = f"running {self.tracker.snapshot().overall:.0%}", "busy"
        code = canonical.split(":", 1)[-1]
        item.setText(f"{code}  {title}\n    {status}" if status else f"{code}  {title}")
        item.setToolTip(f"{canonical} — {title}" + (f"\n{status}" if status else ""))
        if tn in ("bad", "warn", "info", "busy"):
            item.setForeground(tone(tn))
        item.setData(Qt.ItemDataRole.UserRole + 1, tn)

    def _find_episode(self, canonical: str):
        for i in range(self.episodes.count()):
            if self._canonical(self.episodes.item(i)) == canonical:
                return self.episodes.item(i)
        return None

    def _episode_menu(self, pos) -> None:
        item = self.episodes.itemAt(pos)
        if item is not None:
            self.episodes.setCurrentItem(item)
        menu = QMenu(self)
        for a in (self.pick_button, None, self.dvd_button, self.efm_button, self.delivery_button, None,
                  self.leftovers_action, self.handoff_button, self.remove_button):
            menu.addSeparator() if a is None else menu.addAction(a)
        menu.exec(self.episodes.viewport().mapToGlobal(pos))

    @property
    def episode(self) -> str | None:
        item = self.episodes.currentItem()
        return self._canonical(item) or None if item else None

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
        self.set_run_enabled(not frozen and self.thread is None)
        self.handoff_button.setEnabled(not frozen)
        self._update_run_button(episode)
        self.details.set_rows(self.services.manifest_rows(episode))
        self._update_pack_target(episode)
        self._show_saved_progress(episode)
        self._pending = self.services.pending_reviews(episode)
        self.reviews.clear()
        for p in self._pending:
            item = QListWidgetItem(f"{self.REVIEW_NAMES.get(p.review, p.review)} — waiting for you")
            item.setToolTip(f"{p.review} · job {p.job} · step {p.step}")
            self.reviews.addItem(item)
        if self._pending and self.reviews.currentRow() < 0:
            self.reviews.setCurrentRow(0)
        n = len(self._pending)
        idx = self.tabs.indexOf(self.review_page)
        self.tabs.setTabText(idx, f"Reviews ({n})" if n else "Reviews")
        self.tabs.tabBar().setTabTextColor(idx, tone("warn") if n else self.palette().color(self.foregroundRole()))
        self.show_review(self.reviews.currentRow())
        current = self._find_episode(episode)
        if current is not None:
            self._label_episode(current, episode)
        self._needs_you(episode)

    REVIEW_NAMES: ClassVar[dict[str, str]] = {"dvd-alignment": "DVD alignment", "cadence-review": "Film cadence",
                    "delivery": "Delivery A/V check"}

    @Slot(int)
    def show_review(self, row: int) -> None:
        from fstk.ui.visual import headline_html, load_review_view

        has = 0 <= row < len(getattr(self, "_pending", []))
        for b in (self.open_evidence, self.approve, self.reject):
            b.setEnabled(has)
        self.approve.setText("Approve…")
        anything = bool(getattr(self, "_pending", []))
        self.review_controls.setVisible(anything)       # nothing waiting: one sentence, not empty controls
        self.review_empty.setVisible(not anything)
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
        self._log_cleanup(getattr(self.services, "last_cleanup", None))
        self.show_episode(self.episode)
        name = self.REVIEW_NAMES.get(p.review, p.review)
        if decision == "APPROVED":
            self.continue_job(p.job, what=f"{name} approved")
        elif self.ask_redo(name):
            self.continue_job(p.job, what=f"{name} rejected — redoing it")
        else:
            self.set_banner(f"{name} rejected. It is redone the next time this job runs (Run to delivery).", "warn",
                            [("Redo now", lambda: self.continue_job(p.job, what=f"{name} rejected — redoing it"))])

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
        self.set_banner("FromStunToKill was updated while it was open. Start it again to use the new version"
                        + (" (after this run: finished steps are kept)." if self.thread else "."), "warn",
                        [] if self.thread else [("Restart the app", self.restart_app)])
        return True

    def restart_app(self) -> None:
        """Start a fresh copy of the app and close this one (only when nothing is running here)."""
        if self.thread is not None:
            QMessageBox.information(self, "A run is going", "Restart the app after the run ends (or cancel it).")
            return
        LOG.info("action restart-app")
        if QProcess.startDetached(sys.executable, list(sys.argv)):
            self.close()
        else:
            QMessageBox.warning(self, "Could not restart", "Start FromStunToKill again yourself.")

    def set_banner(self, text: str, tone: str = "info", actions=(), kind: str = "event") -> None:
        """Say what just happened (``kind="event"``) or what needs doing next (``"needs"``), with buttons."""
        self.banner.show_message(text, tone, actions, kind)

    def _needs_you(self, episode: str) -> None:
        """The banner's standing message for this episode: a review waiting, or a step that stopped the run.
        A message about something that just happened is left alone; a standing one that no longer applies goes."""
        msg, tn, actions = "", "info", []
        pending = getattr(self, "_pending", [])
        snap = self.tracker.snapshot() if self.tracker is not None else None
        if pending:
            p = pending[0]
            name = self.REVIEW_NAMES.get(p.review, p.review)
            more = f" (and {len(pending) - 1} more)" if len(pending) > 1 else ""
            msg, tn = f"{name} is waiting for your review{more}.", "info"
            actions = [("Review now", self.open_first_review)]
        elif snap is not None and snap.failed and self.thread is None:
            worst = snap.failed[0]
            label = self.progress_panel.step_label(worst)
            issue = (worst.detail or "").splitlines()[0].split("; see ")[0][:160] if worst.detail else ""
            word = {"failed": "failed", "blocked": "is blocked", "interrupted": "was interrupted",
                    "stale": "is out of date"}[worst.status]
            msg = f"{label} {word}" + (f": {issue}" if issue else ".")
            tn = "bad" if worst.status == "failed" else "warn"
            row = list(self.tracker.steps).index(worst.id)
            actions = [("Show details", lambda r=row: self.progress_panel.show_message(r))]
            if self.progress_panel.can_restart(worst.id, worst.status):
                actions.append(("Restart from here", lambda sid=worst.id: self.restart_step(sid)))
            else:                                       # the steps before it need doing again first
                actions.append(("Run again", lambda: self.main_run.defaultAction().trigger()))
        if msg:
            if self.banner.kind != "event" or self.banner.isHidden():
                self.set_banner(msg, tn, actions, kind="needs")
        elif self.banner.kind == "needs":
            self.banner.setVisible(False)

    def open_first_review(self) -> None:
        """"Review now": the Reviews tab on the first waiting review (the A/V check opens its own window)."""
        pending = getattr(self, "_pending", [])
        if not pending:
            return
        if pending[0].review == "delivery":
            self.open_delivery_review()
            return
        self.tabs.setCurrentWidget(self.review_page)
        self.reviews.setCurrentRow(0)

    def _log_cleanup(self, doc: dict | None) -> None:
        """After the final output is approved: what was cleared away (files of attempts that cannot be reused)."""
        if not doc:
            return
        if doc.get("error"):
            self.append_log(f"leftover files were not removed: {doc['error']}")
            return
        if not doc["removed"]:
            self.append_log("final output approved: no leftover files to remove")
            return
        self.append_log(f"final output approved: removed {len(doc['removed'])} leftover file(s), "
                        f"{doc['bytes'] / 2**30:.1f} GiB (list: {doc.get('report')})")
        for x in doc["removed"]:
            self.append_log(f"  removed {x['path']} — {x['why']}")

    def on_delivery_decided(self, path: str) -> None:
        import json

        self.append_log(f"delivery decision recorded: {path}")
        self._log_cleanup(getattr(self.services, "last_cleanup", None))
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
            self.append_log(f"DVD for {self.episode}: " + str(self.services.dvd_status(self.episode)))
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
        item = self._find_episode(EpisodeId.parse(canonical).canonical if ":" in canonical or canonical else canonical)
        if item is not None:
            self.episodes.setCurrentItem(item)

    def open_leftovers(self) -> None:
        """Episode › Remove leftover files…: the approval-time cleanup, again (it was refused or skipped)."""
        episode = self.episode
        LOG.info("action leftovers-open episode=%s", episode)
        if not episode:
            return
        if not self.services.final_output_approved(episode):
            QMessageBox.information(self, "Not yet", "Leftover files are removed only once the final output is "
                                    "approved (Delivery A/V check), so nothing that might still be needed goes.")
            return
        try:
            items = self.services.leftovers(episode)
        except (FstkError, OSError) as exc:
            QMessageBox.warning(self, "Cannot list leftover files", str(exc))
            return
        if not items:
            QMessageBox.information(self, "Nothing to remove", "There are no leftover files for this episode.")
            return
        total = sum(x.bytes for x in items) / 2**30
        box = QMessageBox(QMessageBox.Icon.Question, "Remove leftover files",
                          f"Remove {len(items)} leftover file(s), {total:.1f} GiB? They are files of failed or "
                          "replaced attempts that cannot be used again. This cannot be undone.", parent=self)
        box.setDetailedText("\n".join(f"{x.path} — {x.why} ({x.bytes / 2**20:.0f} MiB)" for x in items))
        remove = box.addButton("Remove", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is not remove:
            return
        self._log_cleanup(self.services.clean_leftovers(episode, actor=self.actor))

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
            self.progress_panel.set_runs([], None)
            self.refresh()
            if not self.episodes.count():
                self.title.setText("No episode selected")
                self.details.set_rows([])

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
        self._bell(self.notify_button.isChecked())
        if prefs["operator"]:
            self.actor = str(prefs["operator"])
        self.refresh()
        if dlg.reencode_plan:
            self.start_plan(dlg.reencode_plan)

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
        self.set_run_enabled(False)
        self.progress_panel.set_running(True)
        if self.banner.kind == "needs":
            self.banner.setVisible(False)
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
            "episodes": "Episodes on this computer, with where each stands. Select one to see it; right-click for "
                        "its actions (DVD, reviews, hand off, remove).",
            "override_flag": "This episode has its own output settings. Click to open them.",
            "reviews": "Checks waiting for a person. Select one to read what was measured.",
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
            "log": "What fstk is doing, oldest first; warnings and errors in colour. The full log is in the logs "
                   "folder (Help › Open logs).",
            "log_toggle": "Show or hide the full log (Ctrl+L). It opens by itself when a step fails.",
            "log_last": "The latest thing the app did.",
            "main_run": "Run the next job for this episode: intake until it is done, then delivery. The arrow offers "
                        "the other one and running a plan file. Finished steps are always kept.",
            "notify_bell": "Desktop notifications on or off: a notice when a step needs you, a step fails, or the "
                           "run ends.",
        })
        self.tabs.setTabToolTip(0, "The run: each step, how far it is, how long it took, and the time left.")
        self.tabs.setTabToolTip(1, "Everything waiting for a person to look or listen, and the review windows.")
        self.tabs.setTabToolTip(2, "Run several episodes one after another, unattended.")
        self.tabs.setTabToolTip(3, "Every value recorded for this episode (the manifest), grouped and searchable.")
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
        if getattr(self, "pack_thread", None) is not None:   # an opening pack being made: stop its DVD read
            self.pack_cancel.set()
            self.pack_thread.quit()
            self.pack_thread.wait(10000)
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
        else:
            engine.pause()
        self.progress_panel.set_running(True, paused=PAUSE.is_paused)
        self.update_progress()

    def _remember_progress_columns(self, widths: dict) -> None:
        """Keep the Progress table's column widths the person chose for the next session."""
        try:
            self.services.save_app_prefs({"progress_columns": widths})
        except Exception as exc:  # a preference that cannot be saved must not disturb a run
            LOG.warning("could not remember the progress column widths: %s", exc)

    # ------------------------------------------------------------------ restart from a step
    def restart_step(self, step_id: str) -> None:
        """"Restart from here" on a Progress row: confirm, mark the step and the steps after it, run the job."""
        if self.thread is not None:
            QMessageBox.information(self, "A job is running", "Wait for the current run to end, or cancel it, "
                                                              "before restarting a step.")
            return
        episode, plan = self.episode, (self.tracker.plan if self.tracker else None)
        if not episode or not plan:
            return
        job = plan["job"]
        from fstk.workflow.engine import _descendants

        later = [s["id"] for s in plan["steps"] if s["id"] in _descendants(plan, step_id)]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Restart from here")
        box.setText("Are you sure?  This will overwrite previous work.")
        box.setInformativeText(
            f"{step_id} runs again in {job} and its previous result is replaced."
            + (f"\n\nAfter it: {', '.join(later)} — each is kept only if {step_id} gives exactly the same "
               "result back; otherwise it runs again too." if later else ""))
        go = box.addButton("Continue", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole))
        box.exec()
        if box.clickedButton() is not go:
            LOG.info("action restart-from cancelled episode=%s job=%s step=%s", episode, job, step_id)
            return
        LOG.info("action restart-from episode=%s job=%s step=%s", episode, job, step_id)
        try:
            path, affected = self.services.restart_from(episode, job, step_id, self.actor)
        except FstkError as exc:
            QMessageBox.warning(self, "Cannot restart", str(exc))
            return
        self.append_log(f"restart from {step_id} in {job}: {', '.join(affected)} marked to run again")
        self.start_plan(str(path))

    # ------------------------------------------------------------------ DVD opening pack
    def _update_pack_target(self, episode: str) -> None:
        """The Save icon beside dvd-probe: shown only when the episode has a DVD; its tip names the target file."""
        target = None
        with contextlib.suppress(FstkError, OSError, ValueError):
            if self.services.dvd_status(episode).get("state") == "set":
                target = self.services.opening_pack_target(episode)
        self.progress_panel.set_pack_target(str(target) if target else None, bool(target and target.is_file()))

    def save_opening_pack(self, force: bool = False) -> None:
        """Save icon on dvd-probe: write this DVD's opening pack (ADR 0006) where the tooltip says."""
        if not self.episode or self.pack_thread is not None:
            return
        episode = self.episode
        target = self.services.opening_pack_target(episode)
        if target.is_file() and not force and QMessageBox.question(
                self, "Replace the opening pack?",
                f"An opening pack for {episode} is already saved:\n{target}\n\nReplace it with one made from this "
                f"episode's DVD?") != QMessageBox.StandardButton.Yes:
            return
        LOG.info("action save-opening-pack episode=%s force=%s", episode, force)
        self.append_log(f"saving the DVD opening pack for {episode} → {target} …")
        self.pack_thread = QThread(self)
        import threading

        self.pack_cancel = threading.Event()
        cancel = self.pack_cancel
        self.pack_worker = CallWorker(lambda: self.services.save_opening_pack(episode, force=force, cancel=cancel))
        self.pack_worker.moveToThread(self.pack_thread)
        self.pack_thread.started.connect(self.pack_worker.run)
        self.pack_worker.done.connect(lambda res: self._pack_done(episode, res))
        self.pack_worker.failed.connect(self._pack_failed)
        self.pack_thread.start()

    def _pack_end(self) -> None:
        if self.pack_thread:
            self.pack_thread.quit()
            self.pack_thread.wait()
        self.pack_thread = None

    def _pack_done(self, episode: str, res: dict) -> None:
        self._pack_end()
        pack = res["pack"]
        if not res["written"]:
            self.append_log(f"opening pack for {episode} not saved: {res['why']}")
            if QMessageBox.question(
                    self, "Opening pack not distinctive",
                    f"Not saved: {res['why']}.\n\nWithout the DVD, such a pack could start the episode in the "
                    f"wrong place. Save it anyway?") == QMessageBox.StandardButton.Yes:
                self.save_opening_pack(force=True)
            return
        how = "decoded from the DVD" if res["decoded"] else "from DVD alignment's thumbnails"
        msg = (f"Saved the DVD opening pack for {episode} ({how}): {pack.seconds:g} s of opening from DVD frame "
               f"{pack.opening_dvd_frame}, next best match elsewhere {res['runner_up_ncc']}.\n{res['path']}")
        self.append_log(msg)
        self._update_pack_target(episode)
        QMessageBox.information(self, "Opening pack saved", msg)

    def _pack_failed(self, why: str) -> None:
        self._pack_end()
        self.append_log(f"opening pack not saved: {why}")
        QMessageBox.warning(self, "Opening pack not saved", why)

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
        self.progress_panel.allow_restart = False
        self.tracker = ProgressTracker(plan, timings=self.services.timings())
        with contextlib.suppress(OSError, ValueError, KeyError):   # first run of this job: nothing to resume
            self.tracker.load_state(self.services.job_state(plan["episode"], plan["job"]))
        self.progress_panel.set_tracker(self.tracker)
        self.tabs.setCurrentWidget(self.progress_panel)
        self.tick.start()
        self.update_progress()

    def _show_saved_progress(self, episode: str) -> None:
        """Between runs: a job's saved state (the latest, or the one picked in the run picker), so progress survives
        closing the app and earlier runs can be looked at."""
        if self.thread is not None:
            return
        runs = self.services.runs(episode)
        if not runs:
            self.progress_panel.set_runs([], None)
            self.progress_panel.set_tracker(None)
            return
        jobs = [j for j, _label in runs]
        if self._picked_job not in jobs:
            self._picked_job = None
        job = self._picked_job or jobs[0]
        self.progress_panel.set_runs(runs, job)
        state = self.services.job_state(episode, job)
        runner = self.services.job_runner(episode, job)
        if runner == os.getpid():
            runner = None
        if runner:
            self.watch.start()                          # follow the other window's progress from the saved state
            self.progress_panel.detail.setText(f"running in another window (pid {runner})")
        else:
            self.watch.stop()
        self.progress_panel.allow_restart = not runner and job == jobs[0]
        plan = self.services.job_plan(episode, job)
        if plan:
            self.tracker = ProgressTracker(plan, timings=self.services.timings())
            self.tracker.load_state(state, live=bool(runner))
            self.progress_panel.set_tracker(self.tracker)
        else:
            self.progress_panel.set_tracker(None)

    def choose_run(self, job: str) -> None:
        """The run picker: show an earlier run's steps (restarts are offered only on the latest run)."""
        if self.thread is not None or not self.episode:
            return
        runs = [j for j, _ in self.services.runs(self.episode)]
        self._picked_job = None if (not runs or job == runs[0]) else job
        self.show_episode(self.episode)

    def _step_state(self, step_id: str) -> tuple[str, dict]:
        job = self.tracker.plan["job"] if self.tracker else ""
        try:
            return job, self.services.job_state(self.episode, job)["steps"].get(step_id, {})
        except (OSError, ValueError, FstkError):
            return job, {}

    def open_step_report(self, step_id: str) -> None:
        job, st = self._step_state(step_id)
        report = st.get("report")
        if report and Path(report).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(report))
        else:
            QMessageBox.information(self, "No report", f"{step_id} has no report file in {job}.")

    def open_step_log(self, step_id: str) -> None:
        job, _st = self._step_state(step_id)
        folder = self.services.workspace(self.episode).log_dir(job) / step_id
        if folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        else:
            QMessageBox.information(self, "No tool logs", f"{step_id} wrote no tool logs in {job}.")

    def update_progress(self) -> None:
        snap = self.progress_panel.refresh()
        if snap is None:
            return
        name = (f"{(self.episode or '').split(':')[-1]} — {snap.overall:.0%} — "
                f"{snap.headline(self.progress_panel.step_label)}") if self.thread else ""
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
        passed = self.tracker.steps.get(event.get("step") or "") if self.tracker is not None else None
        if kind == "step-passed" and passed is not None and passed.stage == "remux":
            self.progress_panel.update_final_file()              # the "Finished file" row gets its link
        name = self._name_of(event.get("step"))
        ep = (self.episode or "").split(":")[-1]
        if kind == "step-awaiting-review":
            self.notify("Needs your review", f"{ep}: {name} is ready for you")
        elif kind in ("step-failed", "step-blocked"):
            self.notify(f"{name} {STATUS_WORDS[kind.removeprefix('step-')]}",
                        f"{ep}: {'; '.join(event.get('issues') or [])}"[:200])
        if event["kind"] == "progress":
            frac = event.get("fraction")
            self.append_log(f"  [{event['step']}] {'' if frac is None else f'{frac:5.0%} '}{event.get('message', '')}")
        else:
            extra = event.get("issues") or event.get("detail") or ""
            level = "error" if kind == "step-failed" else "warn" if kind in ("step-blocked", "step-stale") else "info"
            self.append_log(f"{event['kind']} {event.get('step', '')} {extra}", level)
            if kind == "step-failed":
                self.set_log_open(True, remember=False)    # a failure opens the log once; closing it is remembered

    def _done(self) -> None:
        if self.thread:
            self.thread.quit()
            self.thread.wait()
        self.thread, self.worker = None, None
        self.progress_panel.set_running(False)
        self.progress_panel.set_allow_restart(True)
        self.set_run_enabled(True)
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
        steps = state["steps"]
        waiting = [s for s, st in steps.items() if st.get("status") == "awaiting-review"]
        failed = [s for s in worst_first(list(steps), lambda k: steps[k].get("status"))
                  if steps[s].get("status") in ("failed", "blocked")]
        if waiting and not self._queued_plans:
            self.set_banner(f"Waiting for you: {', '.join(self._name_of(w) for w in waiting)} — look at it and "
                            "decide.", "warn", [("Review now", self.open_first_review)], kind="needs")
            self.tabs.setCurrentWidget(self.review_page)
        elif failed:
            first = self._name_of(failed[0])
            issue = "; ".join(steps[failed[0]].get("issues") or []).split("; see ")[0][:160]
            actions = []
            if self.tracker is not None and failed[0] in self.tracker.steps:
                row = list(self.tracker.steps).index(failed[0])
                actions.append(("Show details", lambda r=row: self.progress_panel.show_message(r)))
                if self.progress_panel.can_restart(failed[0], steps[failed[0]].get("status", "")):
                    actions.append(("Restart from here", lambda sid=failed[0]: self.restart_step(sid)))
            self.set_banner(f"Stopped: {first} {STATUS_WORDS[steps[failed[0]]['status']]}"
                            + (f": {issue}" if issue else "."), "bad", actions, kind="needs")
        elif all(st.get("status") == "passed" for st in steps.values()):
            final = self._final_output_of_run()
            if final is not None:
                self.append_log(f"final output: {final}")
                self.set_banner(f"Finished: all {len(steps)} steps are done. Final output: {final.name}", "good",
                                [("Show in folder", lambda p=final: self.reveal_file(p))])
                self.reveal_file(final)
            else:
                self.set_banner(f"Finished: all {len(steps)} steps are done.", "good")

    def _final_output_of_run(self) -> Path | None:
        """After a delivery or re-encode run: its final file, when it exists."""
        if self.tracker is None or not self.episode \
                or not any(s.stage == "remux" for s in self.tracker.steps.values()):
            return None
        try:
            path = self.services.final_file(self.episode)
        except (FstkError, OSError):
            return None
        return path if path is not None and path.is_file() else None

    def reveal_file(self, path: Path) -> None:
        """Open the folder holding ``path``, with the file selected where the system can do that."""
        import sys

        if sys.platform == "win32":
            QProcess.startDetached("explorer", [f"/select,{QDir.toNativeSeparators(str(path))}"])
        elif sys.platform == "darwin":
            QProcess.startDetached("open", ["-R", str(path)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    @Slot(str)
    def on_failed(self, message: str) -> None:
        self.append_log(f"ERROR: {message}", "error")
        self.set_log_open(True, remember=False)
        self._done()
        first = message.splitlines()[0] if message else "The run stopped."
        self.set_banner(first, "bad")
        if message.startswith("Not started:"):            # a tool check failed before anything ran
            self.show_run_problem(message)

    def show_run_problem(self, message: str) -> None:
        """Say why a plan did not start (a method so tests can replace the dialog)."""
        QMessageBox.warning(self, "Plan not started", message)

    def _name_of(self, step_id: str | None) -> str:
        if not step_id:
            return ""
        if self.tracker is not None and step_id in self.tracker.steps:
            return self.progress_panel.step_label(self.tracker.steps[step_id])
        return step_name("", step_id)

    def append_log(self, text: str, level: str = "info") -> None:
        if level == "info" and text.startswith(("ERROR", "WARNING")):
            level = "error" if text.startswith("ERROR") else "warn"
        (LOG.warning if level != "info" else LOG.info)("%s", text)
        self._log_lines.append((text, level))
        del self._log_lines[:-5000]
        if not self.log_problems.isChecked() or level != "info":
            self._put_log(text, level)
        self.log_last.setText(text.splitlines()[0][:200] if text else "")


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
