"""Choose captures already on disk: a local folder, a mapped drive, or a network share (\\\\nas\\share\\...).

Type or paste any path, browse, or start from the profile's downloads folder. A folder that holds a capture set
is used directly; a folder of episodes (a whole downloads share) lists the capture folders under it to pick from.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
)

from fstk.workspace.local_source import find_capture_folders


def running_elevated() -> bool:
    """Windows: an Administrator process has a separate logon session and cannot see the user's mapped drives."""
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


ELEVATED_NOTE = ("This window is running as Administrator, so Windows hides your mapped network drives (X:, Y:, W:) "
                 "from it. Close fstk and start it again from a normal (non-admin) PowerShell, or type the share "
                 "path instead, e.g. \\\\Lenovoflex\\e hdd.")


class LocalFolderDialog(QDialog):
    def __init__(self, start: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Open captures from a folder or network share")
        self.resize(720, 420)
        self.chosen: str = ""
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Folder with the TBC/JSON captures, or a folder of episodes (a local disk, mapped "
                             "drive such as X:\\, or a share such as \\\\server\\share). Type or paste a path."))
        row = QHBoxLayout()
        self.path = QLineEdit(start)
        self.path.returnPressed.connect(self.scan)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        find = QPushButton("Find episodes")
        find.clicked.connect(self.scan)
        self.path.setToolTip("The folder with the TBC/JSON captures, or a folder of episodes. Paste a path or use "
                             "Browse.")
        find.setToolTip("Look in this folder and the ones directly below it for capture sets.")
        for w in (self.path, browse, find):
            row.addWidget(w)
        lay.addLayout(row)
        self.status = QLabel("")
        if running_elevated():
            warn = QLabel(ELEVATED_NOTE)
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #b71c1c;")
            lay.addWidget(warn)
        lay.addWidget(self.status)
        self.found = QListWidget()
        self.found.itemDoubleClicked.connect(lambda *_: self.accept_selection())
        self.found.setToolTip("Capture folders found. Double-click one to use it.")
        lay.addWidget(self.found, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept_selection)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        from fstk.ui.tips import apply_common

        apply_common(self)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder with TBC/JSON captures", self.path.text())
        if d:
            self.path.setText(d)
            self.scan()

    def scan(self) -> None:
        self.found.clear()
        text = self.path.text().strip().strip('"')
        if not text:
            return
        folder = Path(text)
        try:
            ok = folder.is_dir()
        except OSError:
            ok = False
        if not ok:
            self.status.setText("That folder cannot be reached from this machine (is the share connected?).")
            return
        hits = find_capture_folders(folder)
        for h in hits:
            self.found.addItem(str(h))
        if not hits:
            self.status.setText("No .tbc captures found here or in the folders directly below it.")
        else:
            self.status.setText(f"{len(hits)} capture folder(s) found; choose one.")
            self.found.setCurrentRow(0)

    def accept_selection(self) -> None:
        if not self.found.count():
            self.scan()
        item = self.found.currentItem()
        if item is None:
            return
        self.chosen = item.text()
        self.accept()
