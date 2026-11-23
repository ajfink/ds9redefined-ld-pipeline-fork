"""Settings window: groups on the left, the selected group's options on the right.

Two scopes, switched at the top: **Defaults** (this machine, every episode) and **This episode**. Only the
Video and Audio groups exist per episode; there, "Use the defaults" ticked means the episode follows the
machine defaults, unticked means it keeps its own copy (recorded in the manifest with who and when). The
other groups are machine-wide and read-only in episode scope.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fstk.config.output import (
    AUDIO_CODECS,
    DEFAULTS,
    VIDEO_ENCODERS,
    OutputSettings,
    default_group,
    describe_audio,
    describe_video,
    validate_group,
)
from fstk.core.errors import FstkError

PAGES = ("General", "Folders", "Video", "Audio", "Output", "Processing", "Tools", "Logging")
PER_EPISODE = ("Video", "Audio")
CHROMA_TEXT = {"422": "4:2:2 (keeps the master's chroma)", "420": "4:2:0 (plays on more devices)"}
TUNE_TEXT = {"grain": "Grain", "film": "Film", "none": "None", "visual": "Visual quality", "psnr": "PSNR"}
PRIORITY_TEXT = {"normal": "Normal", "below_normal": "Below normal (recommended)", "idle": "Low (only when idle)"}


def _note(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet("color: palette(mid);")
    return lab


class VideoPage(QWidget):
    def __init__(self, available: set[str]):
        super().__init__()
        self.available = available
        lay = QVBoxLayout(self)
        self.use_defaults = QCheckBox("Use the defaults for this episode")
        lay.addWidget(self.use_defaults)
        self.body = QWidget()
        lay.addWidget(self.body)
        form = QFormLayout(self.body)
        self.encoder = QComboBox()
        for enc in VIDEO_ENCODERS.values():
            ok = not available or enc.codec in available
            self.encoder.addItem(enc.label + ("" if ok else "  (not in this FFmpeg)"), enc.id)
            if not ok:
                self.encoder.model().item(self.encoder.count() - 1).setEnabled(False)
        form.addRow("Video encoder:", self.encoder)
        self.encoder_note = _note("")
        form.addRow("", self.encoder_note)
        self.chroma = QComboBox()
        form.addRow("Chroma:", self.chroma)

        quality = QGroupBox("Quality")
        q = QVBoxLayout(quality)
        row = QHBoxLayout()
        self.mode_quality = QRadioButton("Constant quality:")
        self.quality_value = QLabel()
        row.addWidget(self.mode_quality)
        row.addWidget(self.quality_value)
        row.addStretch(1)
        q.addLayout(row)
        self.quality = QSlider(Qt.Orientation.Horizontal)
        self.quality.setTickPosition(QSlider.TickPosition.TicksBelow)
        q.addWidget(self.quality)
        ends = QHBoxLayout()
        ends.addWidget(QLabel("| Smaller file, lower quality"))
        ends.addStretch(1)
        ends.addWidget(QLabel("Higher quality, larger file |"))
        q.addLayout(ends)
        row = QHBoxLayout()
        self.mode_bitrate = QRadioButton("Average bitrate (kb/s):")
        self.bitrate = QSpinBox()
        self.bitrate.setRange(500, 100_000)
        self.bitrate.setSingleStep(250)
        row.addWidget(self.mode_bitrate)
        row.addWidget(self.bitrate)
        row.addWidget(_note("two passes; constant quality is recommended — a fixed bitrate wastes bits on grain"))
        q.addLayout(row)
        self.modes = QButtonGroup(self)
        self.modes.addButton(self.mode_quality)
        self.modes.addButton(self.mode_bitrate)
        form.addRow(quality)

        prow = QHBoxLayout()
        self.preset = QSlider(Qt.Orientation.Horizontal)
        self.preset.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.preset_value = QLabel()
        self.preset_value.setMinimumWidth(90)
        prow.addWidget(self.preset, 1)
        prow.addWidget(self.preset_value)
        form.addRow("Encoder preset:", prow)
        self.tune = QComboBox()
        form.addRow("Encoder tune:", self.tune)
        self.extra = QLineEdit()
        self.extra.setPlaceholderText("advanced: key=value:key=value (colour, range, tune and profile are fixed)")
        form.addRow("Extra options:", self.extra)
        form.addRow("Colour range:", QLabel("Limited (LaserDisc video; fixed)"))
        self.summary = _note("")
        form.addRow("Result:", self.summary)

        self.encoder.currentIndexChanged.connect(self._encoder_changed)
        self.use_defaults.toggled.connect(lambda on: self.body.setEnabled(not on))
        for w in (self.quality, self.preset):
            w.valueChanged.connect(self._refresh)
        for w in (self.mode_quality, self.mode_bitrate):
            w.toggled.connect(self._refresh)
        for w in (self.chroma, self.tune):
            w.currentIndexChanged.connect(self._refresh)
        self.bitrate.valueChanged.connect(self._refresh)
        self.extra.textChanged.connect(self._refresh)
        self._loading = False

    # -- helpers
    def _enc(self):
        return VIDEO_ENCODERS[self.encoder.currentData()]

    def _encoder_changed(self) -> None:
        if self._loading:
            return
        self._fill_for(default_group("video", self.encoder.currentData()))

    def _fill_for(self, v: dict[str, Any]) -> None:
        self._loading = True
        try:
            enc = VIDEO_ENCODERS[v["encoder"]]
            self.encoder.setCurrentIndex(self.encoder.findData(enc.id))
            self.encoder_note.setText(enc.note)
            self.chroma.clear()
            for c in enc.chromas:
                self.chroma.addItem(CHROMA_TEXT[c], c)
            self.chroma.setCurrentIndex(max(0, self.chroma.findData(v["chroma"])))
            has_q = enc.quality is not None
            for w in (self.mode_quality, self.quality, self.quality_value):
                w.setEnabled(has_q)
            self.mode_bitrate.setEnabled(enc.bitrate_mode)
            self.bitrate.setEnabled(enc.bitrate_mode)
            if has_q:
                lo, hi, _ = enc.quality
                self.quality.setRange(0, int(hi - lo))
                self.quality.setValue(int(hi - float(v["quality"])))     # right = better (lower CRF)
            self.bitrate.setValue(int(v.get("bitrate_kbps") or 6000))
            (self.mode_bitrate if v["mode"] == "bitrate" and enc.bitrate_mode else self.mode_quality).setChecked(True)
            self.preset.setEnabled(bool(enc.presets))
            if enc.presets:
                self.preset.setRange(0, len(enc.presets) - 1)
                pos = enc.presets.index(str(v["preset"])) if str(v["preset"]) in enc.presets else 0
                if enc.codec == "libsvtav1":                 # SVT: 0 is slowest; show slow → fast like x26x
                    pos = len(enc.presets) - 1 - pos
                self.preset.setValue(pos)
            self.tune.clear()
            for t in enc.tunes:
                self.tune.addItem(TUNE_TEXT.get(t, t), t)
            self.tune.setCurrentIndex(max(0, self.tune.findData(v["tune"])))
            self.tune.setEnabled(len(enc.tunes) > 1 and not enc.tune_locked)
            self.tune.setToolTip("x265 always uses tune grain (owner decision)" if enc.tune_locked else "")
            self.extra.setText(str(v.get("extra") or ""))
            self.extra.setEnabled(enc.codec in ("libx265", "libx264", "libsvtav1"))
        finally:
            self._loading = False
        self._refresh()

    def values(self) -> dict[str, Any]:
        enc = self._enc()
        preset = ""
        if enc.presets:
            pos = self.preset.value()
            if enc.codec == "libsvtav1":
                pos = len(enc.presets) - 1 - pos
            preset = enc.presets[pos]
        quality = (enc.quality[1] - self.quality.value()) if enc.quality else 0
        return {"encoder": enc.id, "mode": "bitrate" if self.mode_bitrate.isChecked() and enc.bitrate_mode
                else "quality", "quality": quality, "bitrate_kbps": self.bitrate.value(), "preset": preset,
                "tune": self.tune.currentData() or "", "chroma": self.chroma.currentData() or enc.chromas[0],
                "extra": self.extra.text().strip()}

    def _refresh(self) -> None:
        if self._loading:
            return
        enc, v = self._enc(), self.values()
        self.quality_value.setText(f"{enc.quality_label} {v['quality']}" if enc.quality else "lossless")
        self.preset_value.setText(f"{v['preset']}" + (" (slower = better)" if enc.codec == "libsvtav1" else "")
                                  if enc.presets else "—")
        problems = validate_group("video", v)
        self.summary.setText("; ".join(problems) if problems
                             else describe_video(OutputSettings(v, dict(DEFAULTS["audio"]))))

    def load(self, v: dict[str, Any], *, use_defaults: bool | None) -> None:
        self.use_defaults.setVisible(use_defaults is not None)
        self.use_defaults.setChecked(bool(use_defaults))
        self.body.setEnabled(not use_defaults)
        self._fill_for({**DEFAULTS["video"], **v})


class AudioPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        self.use_defaults = QCheckBox("Use the defaults for this episode")
        lay.addWidget(self.use_defaults)
        self.body = QWidget()
        lay.addWidget(self.body)
        b = QVBoxLayout(self.body)
        b.addWidget(_note("Delivered in this order: LD stereo (default track) · DVD 5.1 (only when the episode has "
                          "a DVD) · stereo compatibility copy. Sample rate and bit depth stay as restored; "
                          "no gain or dynamic-range compression is applied."))
        self.rows: dict[str, tuple[QComboBox, QComboBox]] = {}
        self.include_51 = QCheckBox("Include the DVD 5.1 track when the episode has a DVD")
        self.compat = QCheckBox("Add a stereo compatibility track (for players without FLAC)")
        for key, title, channels, lossy_only, toggle in (
                ("stereo", "LD stereo (from the LaserDisc digital audio)", 2, False, None),
                ("fiveone", "DVD 5.1 (retimed to the LaserDisc picture)", 6, False, self.include_51),
                ("compat", "Compatibility stereo", 2, True, self.compat)):
            box = QGroupBox(title)
            form = QFormLayout(box)
            if toggle is not None:
                form.addRow(toggle)
            codec, rate = QComboBox(), QComboBox()
            for c in AUDIO_CODECS.values():
                if lossy_only and c.lossless:
                    continue
                codec.addItem(c.label, c.id)
            codec.currentIndexChanged.connect(lambda _i, k=key, ch=channels: self._codec_changed(k, ch))
            form.addRow("Codec:", codec)
            form.addRow("Bitrate (kb/s):", rate)
            if toggle is not None:
                toggle.toggled.connect(lambda on, c=codec, r=rate, k=key, ch=channels: (
                    c.setEnabled(on), self._codec_changed(k, ch)))
            self.rows[key] = (codec, rate)
            b.addWidget(box)
        self.summary = _note("")
        b.addWidget(self.summary)
        b.addStretch(1)
        self.use_defaults.toggled.connect(lambda on: self.body.setEnabled(not on))
        for _codec, rate in self.rows.values():
            rate.currentIndexChanged.connect(self._refresh)
        self.include_51.toggled.connect(self._refresh)
        self.compat.toggled.connect(self._refresh)
        self._loading = False

    def _codec_changed(self, key: str, channels: int, bitrate: int | None = None) -> None:
        codec, rate = self.rows[key]
        c = AUDIO_CODECS[codec.currentData()]
        rate.blockSignals(True)
        rate.clear()
        for kbps in (c.bitrates or {}).get(channels, ()):
            rate.addItem(str(kbps), kbps)
        if not c.lossless:
            want = bitrate if bitrate in c.bitrates[channels] else c.default_bitrate[channels]
            rate.setCurrentIndex(rate.findData(want))
        enabled = codec.isEnabled() and not c.lossless
        rate.setEnabled(enabled)
        rate.blockSignals(False)
        self._refresh()

    def values(self) -> dict[str, Any]:
        out: dict[str, Any] = {"include_51": self.include_51.isChecked(), "compat": self.compat.isChecked()}
        for key, (codec, rate) in self.rows.items():
            out[f"{key}_codec"] = codec.currentData()
            out[f"{key}_bitrate"] = int(rate.currentData() or 0)
        return out

    def _refresh(self) -> None:
        if self._loading:
            return
        v = self.values()
        problems = validate_group("audio", v)
        self.summary.setText("Result: " + ("; ".join(problems) if problems
                                           else describe_audio(OutputSettings(dict(DEFAULTS["video"]), v))))

    def load(self, v: dict[str, Any], *, use_defaults: bool | None) -> None:
        v = {**DEFAULTS["audio"], **v}
        self.use_defaults.setVisible(use_defaults is not None)
        self.use_defaults.setChecked(bool(use_defaults))
        self.body.setEnabled(not use_defaults)
        self._loading = True
        self.include_51.setChecked(v["include_51"])
        self.compat.setChecked(v["compat"])
        for key, (codec, _rate) in self.rows.items():
            codec.setCurrentIndex(max(0, codec.findData(v[f"{key}_codec"])))
        self.rows["fiveone"][0].setEnabled(v["include_51"])
        self.rows["compat"][0].setEnabled(v["compat"])
        self._loading = False
        for key, channels in (("stereo", 2), ("fiveone", 6), ("compat", 2)):
            self._codec_changed(key, channels, int(v[f"{key}_bitrate"] or 0))


class SettingsDialog(QDialog):
    def __init__(self, services, episode: str | None = None, parent=None, *, actor: str = "operator"):
        super().__init__(parent)
        self.services, self.episode, self.actor = services, episode, actor
        self.reencode_plan = None
        self.setWindowTitle("Settings")
        self.resize(940, 640)
        outer = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Settings for:"))
        self.scope = QComboBox()
        self.scope.addItem("Defaults (this computer, every episode)", None)
        if episode:
            self.scope.addItem(f"This episode only — {episode}", episode)
        top.addWidget(self.scope, 1)
        outer.addLayout(top)
        self.scope_note = _note("")
        outer.addWidget(self.scope_note)

        middle = QHBoxLayout()
        self.groups = QListWidget()
        self.groups.addItems(PAGES)
        self.groups.setFixedWidth(170)
        self.stack = QStackedWidget()
        middle.addWidget(self.groups)
        middle.addWidget(self.stack, 1)
        outer.addLayout(middle, 1)

        self.pages: dict[str, QWidget] = {}
        self.video = VideoPage(services.available_encoders())
        self.audio = AudioPage()
        builders = {"General": self._general, "Folders": self._folders, "Video": lambda: self.video,
                    "Audio": lambda: self.audio, "Output": self._output, "Processing": self._processing,
                    "Tools": self._tools, "Logging": self._logging}
        for name in PAGES:
            page = builders[name]()
            self.pages[name] = page
            self.stack.addWidget(page)
        self.groups.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.groups.setCurrentRow(PAGES.index("Video"))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.scope.currentIndexChanged.connect(self._scope_changed)
        self._scope_changed()

    # ------------------------------------------------------------------ machine-wide pages
    def _general(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        prefs = self.services.app_prefs()
        self.operator = QLineEdit(str(prefs["operator"]))
        self.operator.setPlaceholderText("your name (recorded on reviews and decisions); blank = user@computer")
        form.addRow("Operator name:", self.operator)
        self.notifications = QCheckBox("Desktop notification when a step needs you, fails, or a run ends")
        self.notifications.setChecked(bool(prefs["notifications"]))
        form.addRow("Notifications:", self.notifications)
        self.catalog_days = QSpinBox()
        self.catalog_days.setRange(1, 365)
        self.catalog_days.setSuffix(" days")
        self.catalog_days.setValue(int(prefs["catalog_refresh_days"]))
        form.addRow("Re-check the Archive catalog after:", self.catalog_days)
        return w

    def _folders(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        p = self.services.profile
        self.folders_text = _note("")
        self._show_folders()
        lay.addWidget(self.folders_text)
        change = QPushButton("Change folders…")
        change.clicked.connect(self._change_folders)
        lay.addWidget(change, 0, Qt.AlignmentFlag.AlignLeft)
        form = QFormLayout()
        self.reserve = QDoubleSpinBox()
        self.reserve.setRange(1, 10_000)
        self.reserve.setSuffix(" GiB")
        self.reserve.setValue(float(p.limits.min_free_gb))
        form.addRow("Keep this much space free:", self.reserve)
        lay.addLayout(form)
        lay.addWidget(_note("A step that would leave less than this free on its disk waits (blocked by disk space) "
                            "instead of starting."))
        lay.addStretch(1)
        return w

    def _show_folders(self) -> None:
        p = self.services.profile
        self.folders_text.setText(f"Data (state, reports, logs): {p.data_root}\nWorking folder (media): "
                                  f"{p.media_root}\nFinished files: {p.roots.get('out') or 'inside the working folder'}")

    def _change_folders(self) -> None:
        from fstk.ui.folders import FoldersDialog

        dlg = FoldersDialog(self.services.profile, self)
        if dlg.exec():
            self._show_folders()

    def _output(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Container: Matroska (.mkv)"))
        lay.addWidget(_note("Every codec offered under Video and Audio, the subtitles and the track titles fit "
                            "in Matroska. Finished files are named <episode>-23.976-padfixed.mkv; a re-encode or "
                            "an A/V correction makes -r2, -r3 … and never overwrites an earlier file."))
        lay.addWidget(_note("Changing Video or Audio settings after an episode is finished re-runs only "
                            "encode → remux → QC from the lossless master; nothing earlier is redone."))
        lay.addStretch(1)
        return w

    def _processing(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.priority = QComboBox()
        for k, text in PRIORITY_TEXT.items():
            self.priority.addItem(text, k)
        self.priority.setCurrentIndex(self.priority.findData(self.services.app_prefs()["priority"]))
        form.addRow("Tool priority:", self.priority)
        form.addRow("", _note("Below normal keeps the computer responsive while Orc, VapourSynth and the encoder "
                              "use every core. Takes effect for tools started from now on."))
        form.addRow("Heavy jobs at once:", QLabel("1 (one stack/encode at a time; downloads can run alongside)"))
        return w

    def _tools(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(_note("Leave a path blank to use the copy found on PATH."))
        self.tool_table = QTableWidget(0, 3)
        self.tool_table.setHorizontalHeaderLabels(["Tool", "Path", ""])
        self.tool_table.horizontalHeader().setStretchLastSection(False)
        self.tool_table.setColumnWidth(0, 110)
        self.tool_table.setColumnWidth(1, 480)
        names = sorted({"ffmpeg", "ffprobe", "vspipe", "orc", "sevenzip", *self.services.profile.tools})
        for name in names:
            r = self.tool_table.rowCount()
            self.tool_table.insertRow(r)
            item = QTableWidgetItem(name)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.tool_table.setItem(r, 0, item)
            self.tool_table.setItem(r, 1, QTableWidgetItem(self.services.profile.tools.get(name, "")))
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda _c=False, row=r: self._browse_tool(row))
            self.tool_table.setCellWidget(r, 2, browse)
        lay.addWidget(self.tool_table, 1)
        check = QPushButton("Check tools")
        check.clicked.connect(self._check_tools)
        lay.addWidget(check, 0, Qt.AlignmentFlag.AlignLeft)
        self.tool_result = _note("")
        lay.addWidget(self.tool_result)
        return w

    def _browse_tool(self, row: int) -> None:
        path, _ = QFileDialog.getOpenFileName(self, f"Locate {self.tool_table.item(row, 0).text()}")
        if path:
            self.tool_table.item(row, 1).setText(path)

    def _tool_paths(self) -> dict[str, str]:
        return {self.tool_table.item(r, 0).text(): self.tool_table.item(r, 1).text().strip()
                for r in range(self.tool_table.rowCount())}

    def _check_tools(self) -> None:
        try:
            self.services.save_tools(self._tool_paths())
            rows = self.services.tool_status()
        except Exception as exc:  # show anything; this is a diagnostic
            self.tool_result.setText(f"could not check: {exc}")
            return
        self.tool_result.setText("\n".join(f"{r.get('name')}: {r.get('status')} — {r.get('detail')}" for r in rows))

    def _logging(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.log_level = QComboBox()
        for lvl, text in (("INFO", "Normal (INFO)"), ("DEBUG", "Detailed (DEBUG)"), ("WARNING", "Problems only")):
            self.log_level.addItem(text, lvl)
        self.log_level.setCurrentIndex(self.log_level.findData(self.services.app_prefs()["log_level"]))
        form.addRow("Log detail:", self.log_level)
        form.addRow("", _note("The application log and crash log are always written. The FSTK_LOG_LEVEL "
                              "environment variable, when set, wins over this."))
        parent = self.parent()
        for text, slot in (("Open logs folder", "open_logs"), ("Save debug bundle…", "save_bundle")):
            if parent is not None and hasattr(parent, slot):
                b = QPushButton(text)
                b.clicked.connect(getattr(parent, slot))
                form.addRow("", b)
        return w

    # ------------------------------------------------------------------ scope
    def _scope_changed(self) -> None:
        ep = self.scope.currentData()
        if ep:
            own = self.services.project_output(ep)
            defaults = self.services.output_defaults()
            self.video.load(own.get("video") or defaults["video"], use_defaults="video" not in own)
            self.audio.load(own.get("audio") or defaults["audio"], use_defaults="audio" not in own)
            self.scope_note.setText("Video and Audio can differ for this episode. Untick “Use the defaults” to give "
                                    "it its own settings. The other groups apply to the whole computer.")
        else:
            d = self.services.output_defaults()
            self.video.load(d["video"], use_defaults=None)
            self.audio.load(d["audio"], use_defaults=None)
            self.scope_note.setText("Defaults apply to every episode that does not have its own settings.")
        for name, page in self.pages.items():
            if name not in PER_EPISODE:
                page.setEnabled(ep is None)
        for i, name in enumerate(PAGES):
            item = self.groups.item(i)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled if (ep is None or name in PER_EPISODE)
                          else item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        if ep and PAGES[self.groups.currentRow()] not in PER_EPISODE:
            self.groups.setCurrentRow(PAGES.index("Video"))

    # ------------------------------------------------------------------ save
    def save(self) -> None:
        ep = self.scope.currentData()
        try:
            video, audio = self.video.values(), self.audio.values()
            for group, values in (("video", video), ("audio", audio)):
                problems = validate_group(group, values)
                if problems:
                    raise FstkError(f"{group.title()}: " + "; ".join(problems))
            if ep:
                changed = False
                before = self.services.project_output(ep)
                for group, values, page in (("video", video, self.video), ("audio", audio, self.audio)):
                    new = None if page.use_defaults.isChecked() else values
                    old = before.get(group)
                    if new != (None if old is None else {**DEFAULTS[group], **old}):
                        self.services.save_project_output(ep, group, new, actor=self.actor)
                        changed = True
                if changed and self.services.has_encode(ep):
                    self._offer_reencode(ep)
            else:
                self.services.save_output_defaults("video", video)
                self.services.save_output_defaults("audio", audio)
                self.services.save_app_prefs({"operator": self.operator.text().strip(),
                                              "notifications": self.notifications.isChecked(),
                                              "catalog_refresh_days": self.catalog_days.value(),
                                              "log_level": self.log_level.currentData(),
                                              "priority": self.priority.currentData()})
                if abs(self.reserve.value() - self.services.profile.limits.min_free_gb) > 1e-9:
                    self.services.save_limits(min_free_gb=self.reserve.value())
                self.services.save_tools(self._tool_paths())
        except (FstkError, ValueError) as exc:
            QMessageBox.warning(self, "Settings not saved", str(exc))
            return
        self.accept()

    def _offer_reencode(self, ep: str) -> None:
        ask = QMessageBox.question(
            self, "Re-encode this episode?",
            f"{ep} has already been encoded. Make a fresh encode from the lossless master with these settings now?"
            "\n\nOnly encode → remux → QC run again; the earlier files are kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if ask == QMessageBox.StandardButton.Yes:
            self.reencode_plan = str(self.services.write_reencode_plan(ep))
