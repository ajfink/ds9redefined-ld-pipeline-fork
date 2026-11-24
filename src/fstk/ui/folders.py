"""Folders dialog: where fstk keeps its state and does its work (set by the owner, saved in the profile)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from fstk.config.profile import (
    change_folders,
    change_shared_root,
    change_sources,
    default_data_root,
    default_media_root,
)
from fstk.core.errors import FstkError


class FoldersDialog(QDialog):
    """Data folder (manifests, plans, reports, logs), working folder (master, audio, encodes), and an optional
    separate folder for finished files. Defaults live in the user's home on this machine."""

    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.profile = profile
        self.setWindowTitle("Folders")
        self.resize(760, 360)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("The capture and DVD folders are only read. Everything fstk writes goes to the data, working "
                             "and finished-files folders; keep the working folder on a fast local disk "
                             "(it holds a ~40 GB master per episode)."))
        form = QFormLayout()
        self.data = self._row(form, "Data (state, reports, logs)", str(profile.data_root), default_data_root())
        self.media = self._row(form, "Working folder (media)", str(profile.media_root), default_media_root())
        self.out = self._row(form, "Finished files (optional)", profile.roots.get("out", ""), None,
                             placeholder="inside the working folder")
        sources = profile.sources or {}
        self.downloads = self._row(form, "LaserDisc captures (read only)", sources.get("downloads_root", ""), None,
                                   placeholder="folder that holds DS9-S01E20-LaserDisc-Copies and the like")
        self.dvd = self._row(form, "DVD rips (read only)", sources.get("dvd_root", ""), None,
                             placeholder="folder with the DVD video files (optional)")
        self.shared = self._row(form, "Shared workspace (optional)", str(profile.shared_root or ""), None,
                                placeholder="a folder both computers can reach, for Hand off…")
        lay.addLayout(form)
        self.changed: list = []
        self._sources: list = []          # saved first, so a declined 'work exists' prompt does not lose them
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        from fstk.ui.tips import apply_common, apply_tips

        apply_tips(self, {
            "data": "Where fstk keeps its own records: manifests, plans, reports and logs. Small; keep it on a "
                    "drive that is always there.",
            "media": "Where the big working files go: the stacked master (~40 GB per episode), decoded audio and "
                     "encodes. Use a fast local drive with plenty of room.",
            "out": "Optional separate folder for the finished files. Blank keeps them inside the working folder.",
            "downloads": "The folder that holds the LaserDisc capture folders (for example DS9-S01E20-LaserDisc-Copies). "
                         "It is only read. Intake looks here first and downloads what is missing into it. Required "
                         "before an episode's intake can run.",
            "shared": "A folder that every computer in your setup can reach (for example W:\\fstk-shared), used only by "
                      "Hand off…: episode snapshots and who-is-working-on-what are published there. Give fstk its own "
                      "empty folder. Blank turns the shared workspace off on this computer.",
            "dvd": "The folder that holds the DVD video files. Only read; the DVD… button on the main window searches "
                   "it for an episode's file. Optional.",
        })
        apply_common(self)

    def _row(self, form, label, value, default: Path | None, placeholder: str = "") -> QLineEdit:
        edit = QLineEdit(value)
        edit.setPlaceholderText(placeholder)
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: self._browse(edit))
        h.addWidget(browse)
        if default is not None:
            reset = QPushButton("Default")
            reset.setToolTip(str(default))
            reset.clicked.connect(lambda: edit.setText(str(default)))
            h.addWidget(reset)
        form.addRow(label, w)
        return edit

    def _browse(self, edit: QLineEdit) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose a folder", edit.text() or str(Path.home()))
        if d:
            edit.setText(d)

    def save(self, force: bool = False) -> None:
        try:
            self._sources += change_sources(self.profile, {"downloads_root": self.downloads.text(),
                                                           "dvd_root": self.dvd.text()})
            self._sources += change_shared_root(self.profile, self.shared.text())
            folders = change_folders(self.profile, data_root=self.data.text().strip() or None,
                                     media_root=self.media.text().strip() or None,
                                     roots={"out": self.out.text().strip() or None}, force=force)
            self.changed = self._sources + folders
        except FstkError as exc:
            if force:
                QMessageBox.warning(self, "Folders not changed", str(exc))
                return
            ask = QMessageBox.question(self, "Work exists there", f"{exc}\n\nChange the folders anyway?")
            if ask == QMessageBox.StandardButton.Yes:
                self.save(force=True)
            return
        self.accept()
