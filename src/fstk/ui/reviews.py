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

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
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
        self.resize(1100, 620)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
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
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(self.auto_button)
        bottom.addWidget(self.choose_button)
        bottom.addStretch(1)
        bottom.addWidget(buttons)
        lay = QVBoxLayout(self)
        lay.addWidget(self.summary)
        lay.addWidget(self.table, 1)
        lay.addLayout(jump_row)
        lay.addWidget(self.media)
        lay.addWidget(self.note)
        lay.addLayout(bottom)
        self.data: dict = {}
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
        if rows:
            self.table.selectRow(0)

    def selected_capture(self) -> str | None:
        items = self.table.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

    def on_select(self) -> None:
        label = self.selected_capture()
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
        self.chunks = QTableWidget(0, 3)
        self.chunks.setHorizontalHeaderLabels(["Chunk", "Time", "Verdict"])
        self.chunks.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.chunks.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.chunks.verticalHeader().setVisible(False)
        self.chunks.cellDoubleClicked.connect(lambda row, _col: self.play_chunk(row))
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
        self.approve_button.clicked.connect(lambda: self.decide("APPROVED"))
        self.rerun_button.clicked.connect(self.rerun)
        self.reject_button.clicked.connect(lambda: self.decide("REJECTED"))
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("A/V check — double-click a chunk to play it, then mark it:"))
        ll.addWidget(self.chunks, 3)
        ll.addWidget(self.gates, 2)
        ll.addWidget(QLabel("QC-flagged intervals (double-click to play):"))
        ll.addWidget(self.intervals, 1)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.quality_text = QPlainTextEdit()
        self.quality_text.setReadOnly(True)
        self.contact_image = QLabel("No contact sheet.")
        self.contact_image.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setWidget(self.contact_image)
        scroll.setWidgetResizable(True)
        quality = QWidget()
        ql = QVBoxLayout(quality)
        ql.setContentsMargins(0, 0, 0, 0)
        ql.addWidget(self.quality_text, 2)
        ql.addWidget(QLabel("Worst moments (contact sheet):"))
        ql.addWidget(scroll, 3)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.media, "Playback")
        self.tabs.addTab(quality, "Episode quality report")
        self._fill_quality()
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
        lay.addWidget(self.warnings)
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

    # ---------------------------------------------------------------- A/V chunks
    def _fill_chunks(self) -> None:
        chunks = (self.result.get("av_check") or {}).get("chunks", [])
        self.chunks.setRowCount(len(chunks))
        for i, c in enumerate(chunks):
            self.chunks.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.chunks.setItem(i, 1, QTableWidgetItem(f"{_fmt_time(c['start_s'])} – {_fmt_time(c['end_s'])}"))
            box = QComboBox()
            for value, label in VERDICT_CHOICES:
                box.addItem(label, value)
            box.currentIndexChanged.connect(self._update_buttons)
            self.chunks.setCellWidget(i, 2, box)
        pre = (self.result.get("av_check") or {}).get("prescreen") or {}
        if pre.get("confident"):
            # advice only: the reviewer still plays each chunk and can change any mark
            for i in range(len(chunks)):
                self.set_verdict(i, "ok")
            self.summary.setText(self.summary.text() + f"\nA/V pre-screen: {pre['reason']} — chunks pre-marked 'in sync'; "
                                                        "play each to confirm.")

    def set_verdict(self, row: int, verdict: str) -> None:
        box = self.chunks.cellWidget(row, 2)
        box.setCurrentIndex(next(i for i, (v, _) in enumerate(VERDICT_CHOICES) if v == verdict))

    def verdicts(self) -> list[dict]:
        chunks = (self.result.get("av_check") or {}).get("chunks", [])
        out = []
        for i, c in enumerate(chunks):
            value = self.chunks.cellWidget(i, 2).currentData()
            if value:
                out.append({"t_s": c["t_s"], "verdict": value, "start_s": c["start_s"], "end_s": c["end_s"]})
        return out

    def play_chunk(self, row: int) -> None:
        chunks = (self.result.get("av_check") or {}).get("chunks", [])
        if not 0 <= row < len(chunks):
            return
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
        n = len((self.result.get("av_check") or {}).get("chunks", []))
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
        self.warnings.setPlainText("Standing warnings:\n" + "\n".join(f"• {w}" for w in r.get("standing_warnings", [])))
        self.blockers = blockers
        self._fill_chunks()
        self._update_buttons()
        self.media.status.setText(f"{Path(r.get('deliverable_path', '')).name} (double-click an interval or press Play)")
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
        return str(path)
