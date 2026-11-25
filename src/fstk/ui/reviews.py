"""Review windows: EFM capture choice (with A/B audition) and delivery QC review.

Both windows only *show* evidence and *record* human decisions through ``Services``; they never
write the manifest directly. The EFM window turns a choice into an ``efm-select`` plan (the stage
re-verifies every decode before recording it); the delivery window records an approval only for a
QC report that the engine is holding for review, and only when that report has no failing or
unverified gate.

Media playback (QtMultimedia) is created lazily on first use so the windows open on machines or
test runs without an audio/video backend.
"""
from __future__ import annotations

import contextlib
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from fstk.core.errors import FstkError
from fstk.tools.orc_efm import PARETO_METRICS
from fstk.ui.visual import DetailsToggle, TimelineBar, headline_html

STATUS_COLOURS = {"PASS": "#2e7d32", "WARN": "#ef6c00", "FAIL": "#c62828", "ERROR": "#6a1b9a", "SKIP": "#616161"}
EFM_COLUMNS = (("capture", "Capture"), ("assessment", "Assessment"), ("post_c2_data_loss_pct", "Post-C2 %"),
               ("c2_uncorrectable_pct", "C2 unc %"), ("c1_uncorrectable_pct", "C1 unc %"),
               ("missing_sections", "Missing"), ("concealed_samples", "Concealed"), ("muted_samples", "Muted"),
               ("disc_duration", "Disc duration"), ("q_timecodes", "Q timecodes"))


def _fmt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{int(seconds // 3600)}:{int(seconds // 60 % 60):02d}:{seconds % 60:06.3f}"


class MediaPane(QWidget):
    """Lazy QMediaPlayer with a position slider; optional video surface; keeps position on source swap."""

    def __init__(self, *, video: bool = False, parent=None):
        super().__init__(parent)
        self._video = video
        self.player = None
        self.audio_out = None
        self.surface = None
        self._duration_ms = 0
        self.status = QLabel("not loaded")
        self.play_button = QPushButton("Play")
        self.pause_button = QPushButton("Pause")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.position = QLabel(_fmt_time(0))
        self.play_button.clicked.connect(self.play)
        self.pause_button.clicked.connect(self.pause)
        self.slider.sliderMoved.connect(self.seek_ms)
        self.play_button.setToolTip("Play from the current position.")
        self.pause_button.setToolTip("Pause playback, keeping the position.")
        self.slider.setToolTip("Drag to move through the clip.")
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.video_holder = QVBoxLayout()
        self.layout_.addLayout(self.video_holder, 1)
        row = QHBoxLayout()
        for w in (self.play_button, self.pause_button, self.slider, self.position):
            row.addWidget(w, 1 if w is self.slider else 0)
        self.layout_.addLayout(row)
        self.layout_.addWidget(self.status)
        self.source: str | None = None

    def ensure_player(self) -> bool:
        if self.player is not None:
            return True
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        except ImportError as exc:  # pragma: no cover - depends on the PySide6 build
            self.status.setText(f"playback unavailable: {exc}")
            return False
        self.player = QMediaPlayer(self)
        self.audio_out = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_out)
        if self._video:
            try:
                from PySide6.QtMultimediaWidgets import QVideoWidget

                self.surface = QVideoWidget(self)
                self.surface.setMinimumHeight(240)
                self.video_holder.addWidget(self.surface)
                self.player.setVideoOutput(self.surface)
            except ImportError as exc:  # pragma: no cover
                self.status.setText(f"video unavailable: {exc}")
        self.player.durationChanged.connect(self._on_duration)
        self.player.positionChanged.connect(self._on_position)
        self.player.errorOccurred.connect(lambda *_: self.status.setText(f"playback error: {self.player.errorString()}"))
        return True

    def load(self, path: str, *, keep_position: bool = True) -> None:
        if not Path(path).is_file():
            self.status.setText(f"missing: {path}")
            return
        if not self.ensure_player():
            return
        pos = self.player.position() if keep_position and self.source else 0
        playing = self.is_playing()
        self.source = path
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.setPosition(pos)
        self.status.setText(Path(path).name)
        if playing:
            self.player.play()

    def is_playing(self) -> bool:
        if self.player is None:
            return False
        from PySide6.QtMultimedia import QMediaPlayer

        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def play(self) -> None:
        if self.player is not None and self.source:
            self.player.play()

    def pause(self) -> None:
        if self.player is not None:
            self.player.pause()

    def seek_ms(self, ms: int) -> None:
        if self.player is not None:
            self.player.setPosition(int(ms))

    def seek_s(self, seconds: float) -> None:
        self.seek_ms(int(seconds * 1000))

    def _on_duration(self, ms: int) -> None:
        self._duration_ms = ms
        self.slider.setRange(0, int(ms))

    def _on_position(self, ms: int) -> None:
        if not self.slider.isSliderDown():
            self.slider.setValue(int(ms))
        self.position.setText(_fmt_time(ms / 1000))


# ------------------------------------------------------------------------------ EFM capture review
class EfmCaptureReview(QDialog):
    """Compare every verified EFM decode, audition them A/B at the same position, choose one with a note."""

    plan_ready = Signal(str)

    def __init__(self, services, episode: str, parent=None):
        super().__init__(parent)
        self.services, self.episode = services, episode
        self.setWindowTitle(f"EFM capture review — {episode}")
        self.resize(1100, 720)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.headline = QLabel("")
        self.headline.setWordWrap(True)
        self.headline.setTextFormat(Qt.TextFormat.RichText)
        self.strips_box = QWidget()
        self.strips_layout = QVBoxLayout(self.strips_box)
        self.strips_layout.setContentsMargins(0, 0, 0, 0)
        self.strips_scroll = QScrollArea()
        self.strips_scroll.setWidget(self.strips_box)
        self.strips_scroll.setWidgetResizable(True)
        self.strips: dict[str, tuple] = {}
        self._overviews: dict[str, dict] = {}
        self._waiting = 0
        self.table = QTableWidget(0, len(EFM_COLUMNS) + 1)
        self.table.setHorizontalHeaderLabels([h for _, h in EFM_COLUMNS] + ["Pad frames"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self.on_select)
        self.media = MediaPane(video=False)
        self.jump = QDoubleSpinBox()
        self.jump.setRange(0, 4 * 3600)
        self.jump.setDecimals(1)
        self.jump.setSuffix(" s")
        go = QPushButton("Go to")
        go.clicked.connect(lambda: self.media.seek_s(self.jump.value()))
        jump_row = QHBoxLayout()
        jump_row.addWidget(QLabel("Audition the selected capture; switching rows keeps the playback position."))
        jump_row.addStretch(1)
        jump_row.addWidget(self.jump)
        jump_row.addWidget(go)
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText("Why this capture? (required for a manual choice, e.g. "
                                     "'L1 has audible dropouts at 12:40; P1 clean there')")
        self.note.setMaximumHeight(70)
        self.choose_button = QPushButton("Choose selected capture")
        self.auto_button = QPushButton("Accept automatic choice")
        self.choose_button.clicked.connect(self.choose)
        self.auto_button.clicked.connect(self.accept_auto)
        from fstk.ui.tips import apply_tips

        apply_tips(self, {
            "table": "One row per EFM decode with its quality measures. Select a row to audition that capture.",
            "headline": "Which capture the app would use, and why, in plain words.",
            "strips_scroll": "One strip per capture: its sound over the whole disc side. Red marks are places where "
                             "the sound suddenly drops out. Click anywhere on a strip to listen to that capture there.",
            "jump": "A position in the audio, in seconds, to jump to.",
            "note": "Why you chose this capture. Required for a manual choice; stored with the decision.",
            "choose_button": "Use the selected capture for this episode. Needs a note explaining why.",
            "auto_button": "Accept the capture fstk picked by its quality measures.",
        })
        go.setToolTip("Jump the playback to the position on the left.")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setToolTip("Close this window.")
        buttons.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(self.auto_button)
        bottom.addWidget(self.choose_button)
        bottom.addStretch(1)
        bottom.addWidget(buttons)
        self.details = QWidget()
        dl = QVBoxLayout(self.details)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(self.table)
        dl.addWidget(self.summary)
        self.details_toggle = DetailsToggle(self.details, "the decoder's measurements")
        lay = QVBoxLayout(self)
        lay.addWidget(self.headline)
        lay.addWidget(QLabel("<b>Listen and compare.</b> Click a strip to hear that capture at that moment; "
                             "red marks are sudden drop-outs."))
        lay.addWidget(self.strips_scroll, 3)
        lay.addLayout(jump_row)
        lay.addWidget(self.media)
        lay.addWidget(self.details_toggle)
        lay.addWidget(self.details, 2)
        lay.addWidget(self.note)
        lay.addLayout(bottom)
        self.data: dict = {}
        import queue

        self._results: queue.Queue = queue.Queue()
        self._poll = QTimer(self)
        self._poll.setInterval(200)
        self._poll.timeout.connect(self._collect)
        self.populate()

    def populate(self) -> None:
        try:
            self.data = self.services.efm_comparison(self.episode)
        except FstkError as exc:
            self.data = {"rows": [], "problems": [str(exc)], "auto": None, "best": {}, "pads": {}, "wavs": {},
                         "pad_problem": "", "current": None, "labels": []}
        d = self.data
        rows = d["rows"]
        self.table.setRowCount(len(rows))
        bold = QFont()
        bold.setBold(True)
        for i, row in enumerate(rows):
            for j, (key, _) in enumerate(EFM_COLUMNS):
                v = row.get(key)
                item = QTableWidgetItem("—" if v is None else str(v))
                if key in PARETO_METRICS and v is not None and v == d["best"].get(key):
                    item.setFont(bold)
                    item.setForeground(QColor("#2e7d32"))
                if key == "capture" and v == d.get("auto"):
                    item.setText(f"{v}  ★ automatic")
                if key == "capture" and v == d.get("current"):
                    item.setText(item.text() + "  (current)")
                item.setData(Qt.ItemDataRole.UserRole, row["capture"])
                self.table.setItem(i, j, item)
            pad = d["pads"].get(row["capture"])
            self.table.setItem(i, len(EFM_COLUMNS), QTableWidgetItem("—" if pad is None else str(pad)))
        lines = []
        if d["problems"]:
            lines.append("Not ready: " + "; ".join(d["problems"]))
        if d["pad_problem"]:
            lines.append("Pad frames unavailable until the stack reference is measured: " + d["pad_problem"])
        if d.get("auto"):
            lines.append(f"Automatic choice: {d['auto']} — {d['auto_note']}")
        elif rows and not d["problems"]:
            lines.append("A report is missing a measure, so nothing can be chosen automatically: re-decode it. "
                         "Green bold = best value in that column.")
        if d.get("current"):
            lines.append(f"Currently recorded: {d['current']}")
        self.summary.setText("\n".join(lines) or "No verified EFM decodes yet.")
        ready = bool(rows) and not d["problems"] and not d["pad_problem"]
        self.auto_button.setEnabled(ready and bool(d.get("auto")))
        self.choose_button.setEnabled(ready)
        if d["problems"] or d["pad_problem"]:
            self.headline.setText(headline_html("NOT READY", "; ".join([*d["problems"], d["pad_problem"]][:2])
                                                or "Not ready.", "bad"))
        elif d.get("auto"):
            self.headline.setText(headline_html("AUTOMATIC CHOICE", f"{d['auto']} — the capture with the least "
                                                "audible damage. Listen to the red places on the others if you "
                                                "want to be sure, then accept or choose another.", "good"))
        elif rows:
            self.headline.setText(headline_html("YOUR CHOICE", "No capture is clearly best: listen and choose one.",
                                                "warn"))
        else:
            self.headline.setText(headline_html("", "No decoded captures yet: run intake first.", "neutral"))
        self._build_strips(rows)
        if rows:
            self.table.selectRow(0)

    def _build_strips(self, rows: list[dict]) -> None:
        import threading

        from PySide6.QtWidgets import QRadioButton

        from fstk.ui.visual import WaveStrip

        while self.strips_layout.count():
            item = self.strips_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.strips = {}
        from PySide6.QtWidgets import QButtonGroup

        self.capture_group = QButtonGroup(self)
        self.capture_group.setExclusive(True)
        todo = []
        for i, row in enumerate(rows):
            label = row["capture"]
            line = QWidget()
            hl = QHBoxLayout(line)
            hl.setContentsMargins(0, 0, 0, 0)
            name = label + ("  ★" if label == self.data.get("auto") else "") + \
                ("  (in use)" if label == self.data.get("current") else "")
            radio = QRadioButton(name)
            radio.setMinimumWidth(130)
            radio.setToolTip("Choose this capture (★ = the automatic choice).")
            radio.toggled.connect(lambda on, i=i: on and self.table.selectRow(i))
            self.capture_group.addButton(radio, i)
            strip = WaveStrip()
            strip.setToolTip("This capture's sound over the whole side. Click to listen here.")
            strip.clicked.connect(lambda t, label=label, i=i: self._listen(i, label, t))
            words = QLabel(f"{row.get('assessment') or ''}")
            words.setMinimumWidth(190)
            words.setWordWrap(True)
            words.setToolTip("What the drop-out check found in this capture.")
            hl.addWidget(radio)
            hl.addWidget(strip, 1)
            hl.addWidget(words)
            self.strips_layout.addWidget(line)
            self.strips[label] = (radio, strip, words)
            wav = self.data.get("wavs", {}).get(label)
            if label in self._overviews:
                self._show_overview(label, self._overviews[label], "")
            elif wav and Path(wav).is_file():
                todo.append((label, wav))
            else:
                strip.set_overview(None, "no decoded audio")
        self.strips_layout.addStretch(1)
        if todo:
            def work(jobs=tuple(todo)):
                for label, wav in jobs:
                    try:
                        self._results.put((label, self.services.efm_overview(self.episode, label, wav), ""))
                    except Exception as exc:  # unreadable WAV: say so on its strip, keep the others
                        self._results.put((label, None, f"cannot read the audio: {exc}"))

            self._waiting = len(todo)
            threading.Thread(target=work, name="efm-overview", daemon=True).start()
            self._poll.start()

    def _collect(self) -> None:
        """Show each capture's strip as soon as its audio has been read (the reading runs in the background)."""
        import queue

        while True:
            try:
                label, doc, message = self._results.get_nowait()
            except queue.Empty:
                break
            self._waiting -= 1
            if doc is not None:
                self._overviews[label] = doc
            self._show_overview(label, doc, message)
        if self._waiting <= 0:
            self._poll.stop()

    def _show_overview(self, label: str, doc: dict | None, message: str) -> None:
        from fstk.media.audio_overview import describe
        from fstk.ui.visual import tone_colour

        if label not in self.strips:
            return
        _radio, strip, words = self.strips[label]
        strip.set_overview(doc, message)
        if doc is not None:
            text, tone = describe(doc)
            words.setText(f"<span style='color:{tone_colour(tone)};font-weight:600'>{text}</span>")

    def _listen(self, row: int, label: str, seconds: float) -> None:
        """Choose the capture and play it from just before the clicked moment."""
        self.table.selectRow(row)
        wav = self.data.get("wavs", {}).get(label)
        if not wav:
            return
        self.media.load(wav, keep_position=False)
        self.media.seek_s(max(0.0, seconds - 1.0))
        self.media.play()
        with contextlib.suppress(RuntimeError, TypeError):
            self.media.play_button.clicked.disconnect()
        self.media.play_button.clicked.connect(self.media.play)
        for other, (_r, strip, _w) in self.strips.items():
            strip.set_cursor(seconds if other == label else None)

    def selected_capture(self) -> str | None:
        items = self.table.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

    def on_select(self) -> None:
        label = self.selected_capture()
        if label in self.strips and not self.strips[label][0].isChecked():
            self.strips[label][0].setChecked(True)
        if label and label in self.data.get("wavs", {}) and self.media.player is not None:
            self.media.load(self.data["wavs"][label], keep_position=True)
        elif label and label in self.data.get("wavs", {}):
            self.media.status.setText(f"{label}: {Path(self.data['wavs'][label]).name} (press Play)")
            self.media.play_button.clicked.disconnect()
            self.media.play_button.clicked.connect(self._first_play)

    def _first_play(self) -> None:
        label = self.selected_capture()
        if label:
            self.media.load(self.data["wavs"][label], keep_position=False)
            self.media.play()
        self.media.play_button.clicked.disconnect()
        self.media.play_button.clicked.connect(self.media.play)

    def _emit_plan(self, capture: str | None, note: str) -> None:
        try:
            path = self.services.write_efm_select_plan(self.episode, capture=capture, note=note)
        except FstkError as exc:
            QMessageBox.warning(self, "Choice not recorded", str(exc))
            return
        self.plan_ready.emit(str(path))
        self.accept()

    def choose(self) -> None:
        label = self.selected_capture()
        if label:
            self._emit_plan(label, self.note.toPlainText())

    def accept_auto(self) -> None:
        self._emit_plan(None, "")


# ------------------------------------------------------------------------------ delivery review
VERDICT_CHOICES = (("", "— not checked —"), ("ok", "In sync"), ("early", "Audio too early"),
                   ("late", "Audio too late"))


class DeliveryReview(QDialog):
    """The delivery A/V check (owner decision 2026-11-21: the only human review).

    The QC report offers several 15 s chunks spread over the episode. The reviewer plays each and
    marks it *in sync*, *audio too early* or *audio too late*. **Approve** needs every chunk in
    sync and no failed gate. **Rerun with corrections** records the marks, and the app solves the
    timing correction, rebuilds the audio and remux, reruns QC, and offers new chunks.
    """

    decided = Signal(str)
    plan_ready = Signal(str)

    def __init__(self, services, episode: str, *, actor: str, parent=None):
        super().__init__(parent)
        self.services, self.episode, self.actor = services, episode, actor
        self.setWindowTitle(f"A/V check and delivery — {episode}")
        self.resize(1240, 800)
        self.reports = QComboBox()
        self.reports.currentIndexChanged.connect(self.load_report)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.headline = QLabel("")
        self.headline.setWordWrap(True)
        self.headline.setTextFormat(Qt.TextFormat.RichText)
        self.timeline = TimelineBar()
        self.timeline.tick_clicked.connect(self._timeline_tick)
        self.timeline.clicked.connect(self.play_at)
        self.chunk_row = QHBoxLayout()
        self.chunk_buttons: list[QPushButton] = []
        self.current = -1
        self._marks: dict[int, str] = {}
        self.now_label = QLabel("Pick a chunk to watch.")
        self.now_label.setWordWrap(True)
        self.mark_ok = QPushButton("✓  In sync")
        self.mark_early = QPushButton("Sound comes too early")
        self.mark_late = QPushButton("Sound comes too late")
        for b, v, colour in ((self.mark_ok, "ok", "#2e7d32"), (self.mark_early, "early", "#ef6c00"),
                             (self.mark_late, "late", "#ef6c00")):
            b.setMinimumHeight(40)
            b.setStyleSheet(f"QPushButton {{ font-weight: 600; border: 2px solid {colour}; border-radius: 6px; "
                            "padding: 4px 12px; }")
            b.clicked.connect(lambda _c=False, v=v: self.mark(v))
        self.gates = QTableWidget(0, 3)
        self.gates.setHorizontalHeaderLabels(["Gate", "Status", "Detail"])
        self.gates.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.gates.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.gates.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.gates.verticalHeader().setVisible(False)
        self.gates.setWordWrap(True)
        self.intervals = QListWidget()
        self.intervals.itemDoubleClicked.connect(self.play_interval)
        self.media = MediaPane(video=True)
        self.warnings = QPlainTextEdit()
        self.warnings.setReadOnly(True)
        self.warnings.setMaximumHeight(90)
        self.approve_button = QPushButton("Approve delivery")
        self.rerun_button = QPushButton("Rerun with corrections")
        self.reject_button = QPushButton("Reject…")
        from fstk.ui.tips import apply_tips

        apply_tips(self, {
            "reports": "The QC reports for this episode, newest first.",
            "headline": "Where this delivery stands, in plain words.",
            "timeline": "The whole episode. Markers are the chunks to watch (grey: not judged yet, green: in sync, "
                        "orange: out of step); red stretches are places the checks want you to look at. Click to "
                        "play there.",
            "now_label": "The chunk you are judging.",
            "mark_ok": "Picture and sound are in step for this chunk. The next chunk starts by itself.",
            "mark_early": "You hear the sound before you see it (for example the voice before the lips move).",
            "mark_late": "You see it before you hear it (the lips move, then the voice comes).",
            "gates": "Every automatic check with its result. The A/V check below needs your eyes.",
            "intervals": "Places the checks flagged for a look (for example black frames inside a repaired frame "
                         "range). Double-click to play.",
            "warnings": "Anything that stops you approving yet.",
            "approve_button": "Accept this delivery. Needs every chunk marked in sync; your name is recorded.",
            "rerun_button": "Re-run the audio with a correction measured from the chunks you marked early or late "
                            "(audio only; the video is not re-encoded).",
            "reject_button": "Reject this delivery and say why.",
        })
        self.approve_button.clicked.connect(lambda: self.decide("APPROVED"))
        self.rerun_button.clicked.connect(self.rerun)
        self.reject_button.clicked.connect(lambda: self.decide("REJECTED"))
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setToolTip("Close this window.")
        close.rejected.connect(self.reject)

        self.details = QWidget()
        dl = QVBoxLayout(self.details)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(self.gates, 2)
        dl.addWidget(QLabel("Places the checks flagged (double-click to play):"))
        dl.addWidget(self.intervals, 1)
        dl.addWidget(self.warnings)
        self.details_toggle = DetailsToggle(self.details, "the automatic checks")
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.headline)
        ll.addWidget(QLabel("<b>Watch each chunk, then say whether picture and sound are in step:</b>"))
        ll.addWidget(self.timeline)
        ll.addLayout(self.chunk_row)
        ll.addWidget(self.now_label)
        marks = QHBoxLayout()
        for b in (self.mark_ok, self.mark_early, self.mark_late):
            marks.addWidget(b)
        ll.addLayout(marks)
        ll.addStretch(1)
        ll.addWidget(self.details_toggle)
        ll.addWidget(self.details, 3)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.quality_text = QPlainTextEdit()
        self.quality_text.setReadOnly(True)
        self.quality_text.setToolTip("The episode quality report: the verdict and each measurement.")
        self.contact_image = QLabel("No contact sheet.")
        self.contact_image.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setWidget(self.contact_image)
        scroll.setWidgetResizable(True)
        quality = QWidget()
        ql = QVBoxLayout(quality)
        ql.setContentsMargins(0, 0, 0, 0)
        ql.addWidget(QLabel("The worst moments the quality check found:"))
        ql.addWidget(scroll, 4)
        self.quality_toggle = DetailsToggle(self.quality_text, "the quality measurements")
        ql.addWidget(self.quality_toggle)
        ql.addWidget(self.quality_text, 2)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.media, "Playback")
        self.tabs.addTab(quality, "Worst moments")
        self.tabs.addTab(self._stretch_tab(), "Video-origin stretches")
        self.tabs.addTab(self._fix_tab(), "Frame fixes")
        self._fill_quality()
        self._fill_fixes()
        self._fill_stretches()
        split.addWidget(left)
        split.addWidget(self.tabs)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        top = QHBoxLayout()
        top.addWidget(QLabel("QC report:"))
        top.addWidget(self.reports, 1)
        bottom = QHBoxLayout()
        bottom.addWidget(self.approve_button)
        bottom.addWidget(self.rerun_button)
        bottom.addWidget(self.reject_button)
        bottom.addStretch(1)
        bottom.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.summary)
        lay.addWidget(split, 1)
        lay.addLayout(bottom)
        self.result: dict = {}
        self.pending = None
        self.blockers: list[str] = []
        for p in self.services.qc_reports(episode):
            self.reports.addItem(p.name, str(p))
        if not self.reports.count():
            self.summary.setText("No QC report yet: run the qc-report stage first.")
            for b in (self.approve_button, self.rerun_button, self.reject_button):
                b.setEnabled(False)

    def _fill_quality(self) -> None:
        text, png = self.services.episode_quality(self.episode)
        self.quality_text.setPlainText(text or "No episode quality report yet: run the episode-report stage.")
        if png:
            self.contact_image.setPixmap(QPixmap(str(png)))
        else:
            self.contact_image.setText("No contact sheet.")

    # ---------------------------------------------------------------- operator frame fixes
    FIX_ACTIONS = (("interpolate", "Interpolate from LD neighbours (1–3 frames)"),
                   ("ld-patch", "Region patch from the clean LD frame next to it"),
                   ("dvd", "Use the DVD (whole frame, or region if a rectangle is given)"),
                   ("leave", "Leave as captured"))

    def _fix_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("Worst moments (double-click to add a fix there). Frames are delivered-episode frames; "
                             "a rectangle is x,y,w,h in the full picture (even numbers)."))
        self.moments = QListWidget()
        self.moments.setToolTip("The frames the quality check found worst. Double-click one to add a fix there.")
        self.moments.itemDoubleClicked.connect(self._add_fix_from_moment)
        lay.addWidget(self.moments, 1)
        self.fix_table = QTableWidget(0, 6)
        self.fix_table.setHorizontalHeaderLabels(["First", "Last", "Action", "Rectangle", "From (±1)", "What is wrong"])
        self.fix_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.fix_table.verticalHeader().setVisible(False)
        self.fix_table.setToolTip("Frame ranges to fix. Pick how each is fixed: interpolate from the LD neighbours, "
                                  "patch a region from a clean LD frame, use the DVD, or leave as captured.")
        lay.addWidget(self.fix_table, 2)
        row = QHBoxLayout()
        add = QPushButton("Add range")
        add.clicked.connect(lambda: self.add_fix(0, 0))
        rem = QPushButton("Remove selected")
        rem.clicked.connect(lambda: self.fix_table.removeRow(self.fix_table.currentRow()))
        self.preview_button = QPushButton("Preview selected")
        self.preview_button.clicked.connect(self.preview_fix)
        show = QPushButton("Show latest preview")
        show.clicked.connect(self.show_fix_preview)
        self.apply_fixes_button = QPushButton("Apply fixes and re-encode…")
        self.apply_fixes_button.clicked.connect(self.apply_fixes)
        add.setToolTip("Add a row for a range of frames to fix.")
        rem.setToolTip("Remove the selected row.")
        self.preview_button.setToolTip("Make a short before/after preview of the selected fix (nothing is "
                                       "re-encoded).")
        show.setToolTip("Show the most recent preview again.")
        self.apply_fixes_button.setToolTip("Apply every fix in the table and make a new encode from the lossless "
                                           "master. Earlier files are kept.")
        for b in (add, rem, self.preview_button, show, self.apply_fixes_button):
            row.addWidget(b)
        lay.addLayout(row)
        self.fix_preview_text = QLabel("")
        self.fix_preview_text.setWordWrap(True)
        lay.addWidget(self.fix_preview_text)
        self.fix_strips = QLabel("")
        strips = QScrollArea()
        strips.setWidget(self.fix_strips)
        strips.setWidgetResizable(True)
        lay.addWidget(strips, 2)
        clips = QHBoxLayout()
        self.play_captured = QPushButton("Play as captured")
        self.play_fixed = QPushButton("Play with the fix")
        self.play_captured.setToolTip("Play the preview clip of the frames as captured.")
        self.play_fixed.setToolTip("Play the preview clip with the fix applied.")
        self.play_captured.clicked.connect(lambda: self._play_fix_clip("captured"))
        self.play_fixed.clicked.connect(lambda: self._play_fix_clip("fixed"))
        clips.addWidget(self.play_captured)
        clips.addWidget(self.play_fixed)
        clips.addStretch(1)
        lay.addLayout(clips)
        self.fix_clip_paths: dict = {}
        return w

    def _fill_fixes(self) -> None:
        self.fix_ctx = ctx = self.services.fix_context(self.episode)
        self.moments.clear()
        for mo in ctx["moments"]:
            item = QListWidgetItem(f"{_fmt_time(mo['time_s'])}  frame {mo['frame']}  {mo['reason']}")
            item.setData(Qt.ItemDataRole.UserRole, mo)
            self.moments.addItem(item)
        self.fix_table.setRowCount(0)
        for f in ctx["fixes"]:
            self.add_fix(f["first"], f["last"], f.get("action", "interpolate"), f.get("rect"), f.get("src"),
                         f.get("note", ""))
        self._update_fix_buttons()

    def _add_fix_from_moment(self, item: QListWidgetItem) -> None:
        mo = item.data(Qt.ItemDataRole.UserRole)
        self.add_fix(mo["frame"], mo["frame"], note="")

    def add_fix(self, first: int, last: int, action: str = "interpolate", rect=None, src=None, note: str = "") -> int:
        r = self.fix_table.rowCount()
        self.fix_table.insertRow(r)
        self.fix_table.setItem(r, 0, QTableWidgetItem(str(first)))
        self.fix_table.setItem(r, 1, QTableWidgetItem(str(last)))
        box = QComboBox()
        box.setToolTip("How to fix these frames: interpolate between good neighbours, patch a region from a clean "
                       "LD frame, use the DVD (only if one is recorded), or leave them as captured.")
        for key, label in self.FIX_ACTIONS:
            if key == "dvd" and not self.fix_ctx.get("dvd"):
                continue
            box.addItem(label, key)
        idx = box.findData(action)
        box.setCurrentIndex(max(idx, 0))
        box.currentIndexChanged.connect(lambda _i: self._update_fix_buttons())
        self.fix_table.setCellWidget(r, 2, box)
        self.fix_table.setItem(r, 3, QTableWidgetItem(",".join(map(str, rect)) if rect else ""))
        self.fix_table.setItem(r, 4, QTableWidgetItem("" if src is None else str(src)))
        self.fix_table.setItem(r, 5, QTableWidgetItem(note))
        self._update_fix_buttons()
        return r

    def fix_rows(self) -> list[dict]:
        out = []
        for r in range(self.fix_table.rowCount()):
            def txt(c, r=r):
                it = self.fix_table.item(r, c)
                return it.text().strip() if it else ""
            try:
                f = {"first": int(txt(0)), "last": int(txt(1)), "action": self.fix_table.cellWidget(r, 2).currentData(),
                     "note": txt(5)}
                if txt(3):
                    f["rect"] = [int(v) for v in txt(3).split(",")]
                if txt(4):
                    f["src"] = int(txt(4))
            except ValueError as exc:
                raise FstkError(f"row {r + 1}: frames, rectangle and source must be whole numbers") from exc
            out.append(f)
        return out

    def _update_fix_buttons(self) -> None:
        n = self.fix_table.rowCount()
        self.preview_button.setEnabled(n > 0)
        self.apply_fixes_button.setEnabled(n > 0 or bool(self.fix_ctx.get("fixes")))

    def preview_fix(self) -> str | None:
        row = max(self.fix_table.currentRow(), 0)
        try:
            fix = self.fix_rows()[row]
            path = self.services.write_fix_preview_plan(self.episode, fix)
        except (FstkError, IndexError) as exc:
            QMessageBox.warning(self, "Preview not started", str(exc))
            return None
        self.fix_preview_text.setText("Rendering the preview… press 'Show latest preview' when the job finishes.")
        self.plan_ready.emit(str(path))
        return str(path)

    def show_fix_preview(self) -> None:
        rep = self.services.latest_fix_preview(self.episode)
        if not rep:
            self.fix_preview_text.setText("No preview yet.")
            return
        f, det = rep["fix"], rep.get("detail", {})
        bits = [f"{f['action']} {f['first']}..{f['last']}"]
        if "grain_ratio_vs_neighbour" in det:
            bits.append(f"grain {det['grain_ratio_vs_neighbour']:.2f} of the neighbour's"
                        + (" — smoother than the real frames" if det.get("grain_flag") else ""))
        if "post_ncc" in det:
            bits.append(f"DVD match {det['post_ncc']:.3f} after registration and colour match")
        self.fix_preview_text.setText("Top: as captured. Bottom: with the fix" + (" (DVD frames conformed to the LD)"
                                      if f["action"] == "dvd" else "") + ". " + "; ".join(bits))
        strips = rep.get("strips", {})
        from PySide6.QtGui import QPainter

        tops = [QPixmap(strips[k]) for k in ("captured", "fixed") if strips.get(k)]
        if tops:
            wdt, hgt = max(p.width() for p in tops), sum(p.height() for p in tops)
            canvas = QPixmap(wdt, hgt)
            canvas.fill(QColor("black"))
            painter = QPainter(canvas)
            y = 0
            for p in tops:
                painter.drawPixmap(0, y, p)
                y += p.height()
            painter.end()
            self.fix_strips.setPixmap(canvas)
        self.fix_clip_paths = rep.get("clips", {})
        self.play_captured.setEnabled("captured" in self.fix_clip_paths)
        self.play_fixed.setEnabled("fixed" in self.fix_clip_paths)

    def _play_fix_clip(self, name: str) -> None:
        clip = self.fix_clip_paths.get(name)
        if clip:
            self.tabs.setCurrentIndex(0)                     # Playback
            self.media.load(clip, keep_position=False)
            self.media.play()

    def apply_fixes(self) -> str | None:
        try:
            path = self.services.write_fixes_plan(self.episode, fixes=self.fix_rows(), actor=self.actor)
            if self.pending is not None:
                self.services.decide(self.episode, self.pending, decision="REJECTED", actor=self.actor,
                                     note=f"re-encode with {self.fix_table.rowCount()} operator frame fix(es)")
        except FstkError as exc:
            QMessageBox.warning(self, "Re-encode not started", str(exc))
            return None
        self.plan_ready.emit(str(path))
        return str(path)

    # ---------------------------------------------------------------- video-origin stretches
    def _stretch_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        intro = QLabel("These stretches were finished on video, not film: mostly visual effects (space shots, phasers, "
                       "transporter beams, composites) and some titles. Video has a new picture 60 times a second "
                       "and no hidden film pictures to recover, so delivering them at the film's 24 a second means "
                       "choosing which moments to show. <b>Nearest real frames</b> (the default) shows only real "
                       "captured pictures, with very slightly uneven motion. <b>Interpolated</b> gives perfectly even "
                       "motion, but half its pictures are made by the computer and can warp effects or soften grain. "
                       "Press Preview to compare them side by side (left: nearest, right: interpolated), choose for "
                       "each stretch, then re-encode.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.stretch_box = QWidget()
        self.stretch_layout = QVBoxLayout(self.stretch_box)
        self.stretch_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidget(self.stretch_box)
        scroll.setWidgetResizable(True)
        scroll.setToolTip("One card per video-shot stretch: where it is, a preview to watch, and how to deliver it.")
        lay.addWidget(scroll, 1)
        self.stretch_button = QPushButton("Re-encode with these choices…")
        self.stretch_button.setToolTip("Make a new encode with the choice made for each stretch. Earlier files are "
                                       "kept.")
        self.stretch_button.clicked.connect(self.apply_stretches)
        lay.addWidget(self.stretch_button)
        self.stretch_cards: list[QComboBox] = []
        return w

    def _fill_stretches(self) -> None:
        from PySide6.QtWidgets import QFrame

        self.stretch_rows = self.services.video_stretches(self.episode)
        while self.stretch_layout.count():
            item = self.stretch_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.stretch_cards = []
        for i, r in enumerate(self.stretch_rows):
            card = QFrame()
            card.setFrameShape(QFrame.Shape.StyledPanel)
            row = QHBoxLayout(card)
            text = QLabel(f"<b>{_fmt_time(r['start_s'])[:-4]}</b> · {r['seconds']:.1f} s · {r['kind']}"
                          + (f" · current encode: {r['encoded']}" if r["encoded"] else ""))
            text.setToolTip("Where the stretch starts, how long it lasts, and what the current encode used.")
            play = QPushButton("▶  Preview")
            play.setToolTip("Watch this stretch: nearest real frames on the left, interpolated on the right.")
            play.setEnabled(bool(r.get("preview")))
            play.clicked.connect(lambda _c=False, i=i: self.play_stretch(i))
            box = QComboBox()
            box.setToolTip("Nearest real frames (default): every picture is a real captured one; motion is very slightly "
                           "uneven. Interpolated: perfectly even motion, but half the pictures are computer-made and "
                           "can warp beams, explosions or starfields and soften grain.")
            box.addItem("Nearest real frames (default)", "nearest")
            box.addItem("Interpolated (even motion)", "interpolate")
            box.setCurrentIndex(0 if r["treatment"] == "nearest" else 1)
            box.currentIndexChanged.connect(lambda _i: self._update_stretch_button())
            row.addWidget(text, 1)
            row.addWidget(play)
            row.addWidget(box)
            self.stretch_layout.addWidget(card)
            self.stretch_cards.append(box)
        if not self.stretch_rows:
            none = QLabel("No video-shot stretches in this episode.")
            none.setToolTip("Every stretch of this episode is film.")
            self.stretch_layout.addWidget(none)
        self.stretch_layout.addStretch(1)
        self._update_stretch_button()

    def stretch_choices(self) -> dict[str, str]:
        return {str(r["first_cycle"]): self.stretch_cards[i].currentData() for i, r in enumerate(self.stretch_rows)}

    def _update_stretch_button(self) -> None:
        changed = any(self.stretch_choices()[str(r["first_cycle"])] != (r["encoded"] or "nearest")
                      for r in self.stretch_rows)
        self.stretch_button.setEnabled(bool(self.stretch_rows) and changed)

    def play_stretch(self, row: int) -> None:
        clip = self.stretch_rows[row].get("preview")
        if not clip:
            QMessageBox.information(self, "No preview", "Run the stretch-preview stage to render this stretch.")
            return
        self.tabs.setCurrentIndex(0)                     # Playback
        self.media.load(clip, keep_position=False)
        self.media.play()

    def apply_stretches(self, note: str | None = None) -> str | None:
        if note is None:
            from PySide6.QtWidgets import QInputDialog

            note, ok = QInputDialog.getMultiLineText(self, "Re-encode with these choices",
                                                     "What did you see in the previews?")
            if not ok:
                return None
        try:
            path = self.services.write_stretch_plan(self.episode, choices=self.stretch_choices(),
                                                    actor=self.actor, note=note)
            if self.pending is not None:
                self.services.decide(self.episode, self.pending, decision="REJECTED", actor=self.actor,
                                     note=f"re-encode with chosen video-stretch treatments: {note.strip()}")
        except FstkError as exc:
            QMessageBox.warning(self, "Re-encode not started", str(exc))
            return None
        self.plan_ready.emit(str(path))
        return str(path)

    # ---------------------------------------------------------------- A/V chunks
    def _chunks(self) -> list[dict]:
        return (self.result.get("av_check") or {}).get("chunks", [])

    def _fill_chunks(self) -> None:
        for btn in self.chunk_buttons:
            self.chunk_row.removeWidget(btn)
            btn.deleteLater()
        self.chunk_buttons, self._marks, self.current = [], {}, -1
        for i, c in enumerate(self._chunks()):
            btn = QPushButton(f"{i + 1}\n{_fmt_time(c['start_s'])[:-4]}")
            btn.setCheckable(True)
            btn.setMinimumHeight(44)
            btn.setToolTip(f"Chunk {i + 1}: {_fmt_time(c['start_s'])} – {_fmt_time(c['end_s'])}. Click to play it.")
            btn.clicked.connect(lambda _c=False, i=i: self.select_chunk(i, play=True))
            self.chunk_row.addWidget(btn)
            self.chunk_buttons.append(btn)
        pre = (self.result.get("av_check") or {}).get("prescreen") or {}
        if pre.get("confident"):
            # advice only: the reviewer still plays each chunk and can change any mark
            for i in range(len(self.chunk_buttons)):
                self.set_verdict(i, "ok")
            self.summary.setText(self.summary.text() + f"\nA/V pre-screen: {pre['reason']} — chunks pre-marked 'in sync'; "
                                                        "play each to confirm.")
        if self.chunk_buttons:
            self.select_chunk(next((i for i in range(len(self.chunk_buttons)) if i not in self._marks), 0), play=False)
        self._paint_chunks()

    def _paint_chunks(self) -> None:
        colours = {"ok": "#2e7d32", "early": "#ef6c00", "late": "#ef6c00"}
        for i, btn in enumerate(self.chunk_buttons):
            v = self._marks.get(i)
            colour = colours.get(v, "#616161")
            btn.setStyleSheet(f"QPushButton {{ background: {colour}; color: white; border-radius: 6px; "
                              f"border: {'3px solid white' if i == self.current else '1px solid #333'}; "
                              "padding: 2px 10px; }")
            btn.setChecked(i == self.current)
        chunks = self._chunks()
        ticks = [{"time_s": (c["start_s"] + c["end_s"]) / 2, "key": ("c", i), "label": f"chunk {i + 1}",
                  "tone": {"ok": "good", "early": "warn", "late": "warn"}.get(self._marks.get(i), "neutral")}
                 for i, c in enumerate(chunks)]
        ivs = self.result.get("review_intervals", [])
        marks = [{"start_s": iv["start_s"], "end_s": max(iv["end_s"], iv["start_s"] + 1), "tone": "bad",
                  "label": f"{iv.get('gate', '')}: {iv.get('reason', '')}"} for iv in ivs]
        ends = [c["end_s"] for c in chunks] + [iv["end_s"] for iv in ivs]
        duration = float(self.result.get("duration_s") or (max(ends) if ends else 0.0))
        self.timeline.set_data(duration, marks, ticks)
        self.timeline.select_tick(("c", self.current) if self.current >= 0 else None)
        has = 0 <= self.current < len(chunks)
        for b in (self.mark_ok, self.mark_early, self.mark_late):
            b.setEnabled(has and self.pending is not None)
        if has:
            c = chunks[self.current]
            v = self._marks.get(self.current)
            said = dict(VERDICT_CHOICES).get(v or "", "").lower() if v else "not judged yet"
            self.now_label.setText(f"Chunk {self.current + 1} of {len(chunks)} · {_fmt_time(c['start_s'])} – "
                                   f"{_fmt_time(c['end_s'])} · {said}")

    def select_chunk(self, row: int, *, play: bool = True) -> None:
        if not 0 <= row < len(self.chunk_buttons):
            return
        self.current = row
        self._paint_chunks()
        if play:
            self.play_chunk(row)

    def mark(self, verdict: str) -> None:
        """Mark the current chunk and move on to the next one not judged yet (and play it)."""
        if self.current < 0:
            return
        self.set_verdict(self.current, verdict)
        nxt = next((i for i in range(len(self.chunk_buttons)) if i not in self._marks), None)
        if nxt is not None:
            self.select_chunk(nxt, play=True)

    def _timeline_tick(self, key) -> None:
        if isinstance(key, tuple) and key[0] == "c":
            self.select_chunk(int(key[1]), play=True)

    def play_at(self, seconds: float) -> None:
        if any(isinstance(t.get("key"), tuple) and abs(float(t["time_s"]) - seconds) < 1e-6
               for t in self.timeline.ticks):
            return                                  # a chunk marker: the chunk's own clip is playing
        if not self.result.get("deliverable_path"):
            return
        self.tabs.setCurrentIndex(0)                     # Playback
        if self.media.source != self.result.get("deliverable_path"):
            self.media.load(self.result.get("deliverable_path", ""), keep_position=False)
        self.media.seek_s(float(seconds))
        self.media.play()

    def set_verdict(self, row: int, verdict: str) -> None:
        if verdict:
            self._marks[row] = verdict
        else:
            self._marks.pop(row, None)
        self._paint_chunks()
        self._update_buttons()

    def verdicts(self) -> list[dict]:
        out = []
        for i, c in enumerate(self._chunks()):
            value = self._marks.get(i)
            if value:
                out.append({"t_s": c["t_s"], "verdict": value, "start_s": c["start_s"], "end_s": c["end_s"]})
        return out

    def play_chunk(self, row: int) -> None:
        chunks = self._chunks()
        if not 0 <= row < len(chunks):
            return
        self.tabs.setCurrentIndex(0)                     # Playback
        proxy = chunks[row].get("proxy")
        if proxy and Path(proxy).is_file():
            # H.264/AAC excerpt of exactly this chunk (plays where HEVC Main 10 does not); starts at 0
            self.media.load(proxy, keep_position=False)
            self.media.seek_s(0.0)
        else:
            if self.media.source != self.result.get("deliverable_path"):
                self.media.load(self.result.get("deliverable_path", ""), keep_position=False)
            self.media.seek_s(float(chunks[row]["start_s"]))
        self.media.play()

    def _update_buttons(self) -> None:
        n = len(self._chunks())
        marks = [v["verdict"] for v in self.verdicts()]
        pending = self.pending is not None
        self.approve_button.setEnabled(pending and not self.blockers and n > 0 and len(marks) == n
                                       and all(v == "ok" for v in marks))
        self.rerun_button.setEnabled(pending and any(v in ("early", "late") for v in marks))
        self.reject_button.setEnabled(pending)

    def rerun(self) -> str | None:
        marks = self.verdicts()
        summary = ", ".join(f"{_fmt_time(v['start_s'])} {dict(VERDICT_CHOICES)[v['verdict']].lower()}" for v in marks)
        try:
            path = self.services.write_av_rerun_plan(self.episode, qc_report=self.reports.currentData(),
                                                     verdicts=[{"t_s": v["t_s"], "verdict": v["verdict"]}
                                                               for v in marks])
            self.services.decide(self.episode, self.pending, decision="REJECTED", actor=self.actor,
                                 note=f"A/V check — rerun with corrections: {summary}")
        except FstkError as exc:
            QMessageBox.warning(self, "Rerun not started", str(exc))
            return None
        self.plan_ready.emit(str(path))
        self.load_report(self.reports.currentIndex())
        return str(path)

    def load_report(self, index: int) -> None:
        path = self.reports.itemData(index)
        if not path:
            return
        self.result = r = self.services.qc_result(path)
        gates = r.get("results", [])
        self.gates.setRowCount(len(gates))
        for i, g in enumerate(gates):
            status = QTableWidgetItem(g["status"])
            status.setForeground(QColor("white"))
            status.setBackground(QColor(STATUS_COLOURS.get(g["status"], "#616161")))
            self.gates.setItem(i, 0, QTableWidgetItem(g["gate"]))
            self.gates.setItem(i, 1, status)
            self.gates.setItem(i, 2, QTableWidgetItem(g.get("detail", "")))
        self.gates.resizeRowsToContents()
        self.intervals.clear()
        for iv in r.get("review_intervals", []):
            item = QListWidgetItem(f"{_fmt_time(iv['start_s'])} – {_fmt_time(iv['end_s'])}  {iv['gate']}: {iv['reason']}")
            item.setData(Qt.ItemDataRole.UserRole, iv)
            self.intervals.addItem(item)
        s = r.get("summary", {})
        self.pending = self.services.delivery_review_for(self.episode, path)
        blockers = [*s.get("failed_gates", []), *s.get("unverified_gates", [])]
        lines = [f"{r.get('episode')} {r.get('title', '')} — {Path(r.get('deliverable_path', '')).name}",
                 f"{s.get('pass', 0)} pass · {s.get('warn', 0)} warn · {s.get('skip', 0)} skip · "
                 f"{s.get('fail', 0)} fail/unverified"]
        if blockers:
            lines.append("Delivery cannot be approved on this report: " + ", ".join(blockers))
        elif self.pending is None:
            lines.append("This report is not waiting for a delivery decision (already decided, or superseded).")
        corr = ((r.get("av_check") or {}).get("applied_correction") or {}).get("ld", {}).get("current")
        if corr:
            lines.append(f"LD audio timing correction in this file: {corr['offset_ms']:+.0f} ms, "
                         f"{corr['drift_ppm']:+.0f} ppm drift")
        lines.append("Approve needs every chunk marked in sync; mark any chunk 'audio too early' or "
                     "'audio too late' and press Rerun to let the app re-correct.")
        self.summary.setText("\n".join(lines))
        if blockers:
            self.headline.setText(headline_html("CANNOT BE APPROVED", "An automatic check failed or could not be "
                                                "verified: " + ", ".join(blockers) + ". Reject it or fix and rerun.",
                                                "bad"))
        elif self.pending is None:
            self.headline.setText(headline_html("DECIDED", "This delivery is not waiting for a decision.", "neutral"))
        else:
            self.headline.setText(headline_html("WATCH AND LISTEN", f"Every automatic check passed. Watch the "
                                                f"{len(self._chunks())} chunks and mark each one.", "info"))
        self.warnings.setPlainText("Standing warnings:\n" + "\n".join(f"• {w}" for w in r.get("standing_warnings", [])))
        self.blockers = blockers
        self._fill_chunks()
        self._update_buttons()
        self.media.status.setText(f"{Path(r.get('deliverable_path', '')).name} (pick a chunk above, or click the timeline to play from there)")
        self.media.play_button.clicked.disconnect()
        self.media.play_button.clicked.connect(self._first_play)

    def _first_play(self) -> None:
        self.media.load(self.result.get("deliverable_path", ""), keep_position=False)
        self.media.play()
        self.media.play_button.clicked.disconnect()
        self.media.play_button.clicked.connect(self.media.play)

    def play_interval(self, item: QListWidgetItem) -> None:
        iv = item.data(Qt.ItemDataRole.UserRole)
        if self.media.source != self.result.get("deliverable_path"):
            self.media.load(self.result.get("deliverable_path", ""), keep_position=False)
        self.media.seek_s(float(iv["start_s"]))
        self.media.play()

    def decide(self, decision: str, note: str | None = None) -> str | None:
        if self.pending is None:
            return None
        if note is None and decision == "APPROVED":
            note = "A/V check: every chunk in sync — " + ", ".join(
                _fmt_time(v["start_s"]) for v in self.verdicts())
        if note is None:
            from PySide6.QtWidgets import QInputDialog

            note, ok = QInputDialog.getMultiLineText(self, f"{decision.title()} delivery",
                                                     "What did you watch/listen to, and what did you conclude?")
            if not ok:
                return None
        try:
            path = self.services.decide(self.episode, self.pending, decision=decision, note=note, actor=self.actor)
        except FstkError as exc:
            QMessageBox.warning(self, "Decision not recorded", str(exc))
            return None
        self.decided.emit(str(path))
        self.load_report(self.reports.currentIndex())
        if decision == "APPROVED" and self.isVisible():
            self.accept()                       # the main window shows the work carrying on
        return str(path)
