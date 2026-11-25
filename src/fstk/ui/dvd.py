"""Choose the DVD for one episode: a file found by its episode number, one you browse to, or "no DVD"."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from fstk.ui.services import Services

VIDEO_FILTER = "Video (*.mkv *.mp4 *.m4v *.mov *.ts *.m2ts *.vob *.iso);;All files (*)"


class DvdDialog(QDialog):
    def __init__(self, services: Services, episode: str, actor: str, parent=None):
        super().__init__(parent)
        self.services, self.episode, self.actor = services, episode, actor
        self.changed = False
        self.setWindowTitle(f"DVD for {episode}")
        self.resize(760, 460)
        outer = QVBoxLayout(self)
        # what is recorded, first: the file, whether it can be reached, and whether its opening pack is saved
        card = QFrame()
        card.setObjectName("dvdCard")
        card.setStyleSheet("#dvdCard { border: 1px solid #a2afdb; border-radius: 4px; background: palette(base); }")
        cl = QVBoxLayout(card)
        self.state = QLabel("")
        self.state.setWordWrap(True)
        self.state.setStyleSheet("font-weight: 600;")
        self.facts = QLabel("")
        self.facts.setWordWrap(True)
        self.facts.setTextFormat(Qt.TextFormat.RichText)
        cl.addWidget(self.state)
        cl.addWidget(self.facts)
        about = QLabel("The DVD is the independent reference for this episode's alignment, audio and subtitles. "
                       "It is never used as a picture source unless you choose it frame by frame.")
        about.setWordWrap(True)
        cl.addWidget(about)
        self.change_button = QPushButton("Change DVD…")
        self.change_button.setCheckable(True)
        self.change_button.toggled.connect(self._show_chooser)
        crow = QHBoxLayout()
        crow.addStretch(1)
        crow.addWidget(self.change_button)
        cl.addLayout(crow)
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        outer.addWidget(card)
        self.chooser = QFrame()
        lay = QVBoxLayout(self.chooser)
        lay.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.chooser, 1)
        self.spacer = QWidget()                          # takes the room while the search is closed
        outer.addWidget(self.spacer, 1)

        row = QHBoxLayout()
        self.folder = QLineEdit(str((services.profile.sources or {}).get("dvd_root") or ""))
        self.folder.setPlaceholderText("Folder to search (a DVD rips folder, local, mapped drive or share)")
        self.folder.returnPressed.connect(self.search)
        self.folder.textChanged.connect(lambda *_: self._update_remember())
        self.browse_folder = QPushButton("Folder…")
        self.browse_folder.clicked.connect(self._browse_folder)
        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self.search)
        self.remember = QPushButton("Remember folder")
        self.remember.clicked.connect(self.remember_folder)
        for w in (self.folder, self.browse_folder, self.search_button, self.remember):
            row.addWidget(w)
        lay.addLayout(row)
        self.every = QCheckBox("List every video file, not only those named with the episode number")
        self.every.toggled.connect(self.search)
        lay.addWidget(self.every)
        self.status = QLabel("")
        lay.addWidget(self.status)
        self.found = QListWidget()
        self.found.itemDoubleClicked.connect(lambda *_: self.use_selected())
        lay.addWidget(self.found, 1)

        buttons = QHBoxLayout()
        self.use_button = QPushButton("Use selected file")
        self.use_button.clicked.connect(self.use_selected)
        self.file_button = QPushButton("Choose a file…")
        self.file_button.clicked.connect(self.choose_file)
        self.none_button = QPushButton("This episode has no DVD")
        self.none_button.clicked.connect(self.no_dvd)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.accept)
        for w in (self.use_button, self.file_button, self.none_button):
            buttons.addWidget(w)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self.close_button)
        outer.addLayout(bottom)

        self.folder.setToolTip("Where to look for this episode's DVD file. Starts as the DVD folder in Settings → Folders.")
        self.browse_folder.setToolTip("Pick the folder to search.")
        self.search_button.setToolTip("Look under this folder for video files named with the episode number.")
        self.remember.setToolTip("Save this folder as the DVD folder in Settings → Folders, so every episode starts here.")
        self.every.setToolTip("Show every video file under the folder, for a rip whose name does not carry S01E20-style numbering.")
        self.found.setToolTip("Matching files. Double-click one to use it for this episode.")
        self.use_button.setToolTip("Record the selected file as this episode's DVD.")
        self.file_button.setToolTip("Pick the DVD file yourself, from anywhere this computer can reach.")
        self.none_button.setToolTip("Record that no DVD exists for this episode: it takes the LaserDisc-only route.")
        self.close_button.setToolTip("Close this window.")
        self.change_button.setToolTip("Search for another file, pick one, or record that this episode has no DVD.")
        from fstk.ui.tips import apply_common

        apply_common(self)
        self._show_state()
        self._update_remember()
        recorded = self.services.dvd_status(self.episode)
        # a DVD already recorded: lead with it; the search opens on "Change DVD…"
        self.change_button.setChecked(recorded["state"] != "set")
        self._show_chooser(recorded["state"] != "set")
        if self.folder.text().strip():
            self.search()
        elif recorded["state"] == "set":
            self._show_recorded(recorded["path"])          # no DVD folder set: show where the recorded file lives
        else:
            self.status.setText("No DVD folder is set. Type or choose a folder and press Remember folder, or pick a file.")

    def _show_chooser(self, on: bool) -> None:
        self.chooser.setVisible(on)
        self.spacer.setVisible(not on)
        self.resize(self.width(), 460 if on else 230)
        self.change_button.setText("Hide the search" if on else "Change DVD…")

    def _facts(self, st: dict) -> str:
        if st["state"] != "set":
            return ""
        f = Path(st["path"])
        try:
            size = f.stat().st_size
            reach = f"{size / 2**30:.2f} GiB, reachable"
        except OSError:
            reach = "<span style='color:#c62828'>not reachable from this computer now</span>"
        pack = ""
        try:
            target = self.services.opening_pack_target(self.episode)
            pack = ("opening pack saved" if target.is_file() else
                    "no opening pack yet (save one from the DVD probe step once it has run)")
        except Exception:  # the pack location is a convenience; the DVD record stands without it
            pack = ""
        return reach + (f" · {pack}" if pack else "")

    def _show_state(self) -> None:
        st = self.services.dvd_status(self.episode)
        self.facts.setText(self._facts(st))
        self.facts.setVisible(st["state"] == "set")
        self.state.setText({"set": f"Recorded DVD: {st['path']}",
                            "unfiled": "This episode is marked as having a DVD, but no file is recorded. Choose the file below.",
                            "none": "Recorded: this episode has no DVD (LaserDisc-only route).",
                            "unset": "No DVD is recorded for this episode yet. Choose one below, or say it has none."}[st["state"]])

    def _browse_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder with DVD rips", self.folder.text())
        if d:
            self.folder.setText(d)
            self.search()

    def search(self, *_args) -> None:
        self.found.clear()
        text = self.folder.text().strip().strip('"')
        if not text:
            self.status.setText("Type or choose a folder to search.")
            return
        try:
            reachable = Path(text).is_dir()
        except OSError:
            reachable = False
        if not reachable:
            self.status.setText("That folder cannot be reached from this computer (is the drive or share connected?).")
            return
        hits = self.services.dvd_search(self.episode, text, any_video=self.every.isChecked())
        for h in hits:
            self.found.addItem(str(h))
        if hits:
            self.found.setCurrentRow(0)
            self.status.setText(f"{len(hits)} file(s) found; choose one.")
        else:
            self.status.setText("Nothing found for this episode number here. Tick 'List every video file', "
                                "search another folder, or choose the file yourself.")

    def _record(self, file: str | None) -> None:
        try:
            self.services.set_dvd(self.episode, file, self.actor)
        except Exception as exc:                         # shown, never swallowed: the manifest was not changed
            QMessageBox.warning(self, "DVD not recorded", str(exc))
            return
        self.changed = True
        self._show_state()
        if file:
            self._show_recorded(str(file))
        else:
            self.status.setText("Recorded: no DVD for this episode.")

    def _show_recorded(self, file: str) -> None:
        """Make the choice visible: the file's folder in the search box, the file listed and selected."""
        if not self.folder.text().strip():
            self.folder.setText(str(Path(file).parent))
        self.search()
        match = self.found.findItems(file, Qt.MatchFlag.MatchExactly)
        if not match:
            self.found.addItem(file)
            match = self.found.findItems(file, Qt.MatchFlag.MatchExactly)
        self.found.setCurrentItem(match[0])
        self.status.setText(f"Recorded for {self.episode}: {Path(file).name}")
        self._update_remember()

    def _update_remember(self) -> None:
        text = self.folder.text().strip().strip('"')
        self.remember.setEnabled(bool(text) and text != str((self.services.profile.sources or {}).get("dvd_root") or ""))

    def remember_folder(self) -> None:
        text = self.folder.text().strip().strip('"')
        try:
            self.services.set_dvd_root(text)
        except Exception as exc:
            QMessageBox.warning(self, "Folder not saved", str(exc))
            return
        self.status.setText(f"{text} is now the DVD folder for every episode.")
        self._update_remember()

    def use_selected(self) -> None:
        item = self.found.currentItem()
        if item is not None:
            self._record(item.text())

    def choose_file(self) -> None:
        start = self.folder.text().strip() or str(Path.home())
        f, _ = QFileDialog.getOpenFileName(self, "DVD file", start, VIDEO_FILTER)
        if f:
            self._record(f)

    def no_dvd(self) -> None:
        self._record(None)
