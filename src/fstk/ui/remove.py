"""Remove-episode dialog: choose how much to delete, then confirm."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from fstk.core.errors import FstkError

CHOICES = (
    ("project", "None (just remove the project)",
     "Removes the project from FromStunToKill only. Every file stays where it is."),
    ("files", "Just the project files (leaves the downloaded episode)",
     "Also deletes this episode's working files: the stack master, audio, scripts, encodes and the finished video."),
    ("all", "Everything",
     "Also deletes the downloaded episode (the captures fstk downloaded)."),
)


def gib(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


class RemoveDialog(QDialog):
    def __init__(self, services, episode: str, parent=None):
        super().__init__(parent)
        self.services, self.episode = services, episode
        self.plan = services.removal_plan(episode)
        self.scope = "project"
        self.removed: list[str] = []
        self.setWindowTitle(f"Remove {episode}")
        self.resize(640, 460)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>Remove {episode}</b> — what should be deleted?"))
        self.group = QButtonGroup(self)
        self.radios: dict[str, QRadioButton] = {}
        for key, label, hint in CHOICES:
            radio = QRadioButton(label)
            radio.setToolTip(hint)
            self.group.addButton(radio)
            self.radios[key] = radio
            lay.addWidget(radio)
            note = QLabel("    " + hint)
            note.setWordWrap(True)
            note.setStyleSheet("color: gray;")
            lay.addWidget(note)
            radio.toggled.connect(lambda on, k=key: on and self.choose(k))
        if not self.plan.captures:
            self.radios["all"].setToolTip(self.plan.captures_note)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        lay.addWidget(self.detail, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.remove_button = QPushButton("Remove")
        cancel = QPushButton("Cancel")
        self.remove_button.clicked.connect(self.confirm)
        cancel.clicked.connect(self.reject)
        row.addWidget(self.remove_button)
        row.addWidget(cancel)
        lay.addLayout(row)
        from fstk.ui.tips import apply_tips

        apply_tips(self, {
            "detail": "Exactly which folders and how much space each choice would free.",
            "remove_button": "Delete what is listed, after one more confirmation. Nothing is deleted if the episode "
                             "is running.",
        })
        cancel.setToolTip("Close without removing anything.")
        self.radios["project"].setChecked(True)
        self.choose("project")

    def choose(self, scope: str) -> None:
        self.scope = scope
        lines = []
        for item in self.plan.items(scope):
            state = gib(item.bytes) if item.exists else "not found"
            lines.append(f"{item.label}\n    {item.path}   ({state})")
        if scope == "all" and not self.plan.captures:
            lines.append(f"\n{self.plan.captures_note or 'No downloaded copy found.'}")
        elif scope != "all" and self.plan.captures_note and scope == "files":
            lines.append("\n" + self.plan.captures_note)
        total = self.plan.total_bytes(scope)
        if scope != "project":
            lines.append(f"\nTotal to free: {gib(total)}")
        self.detail.setPlainText("\n".join(lines))
        # "None" removes nothing from disk, so the list of files is shown greyed out
        self.detail.setEnabled(scope != "project")

    def confirm(self) -> None:
        what = {"project": "the project only (no files are deleted)",
                "files": "the project and its working files",
                "all": "the project, its working files and the downloaded episode"}[self.scope]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Are you sure?")
        box.setText(f"Remove {what} for {self.episode}?")
        box.setInformativeText("This cannot be undone." if self.scope != "project"
                               else "You can add the episode again later from the Episodes list.")
        yes = box.addButton("Remove", QMessageBox.ButtonRole.DestructiveRole)
        no = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        box.exec()
        if box.clickedButton() is not yes:
            return
        try:
            self.removed = self.services.remove_episode(self.episode, self.scope)
        except (FstkError, OSError) as exc:
            QMessageBox.warning(self, "Could not remove everything", str(exc))
            return
        self.accept()
