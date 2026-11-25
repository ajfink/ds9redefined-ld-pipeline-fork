"""Remove-episode dialog: tick what should go (owner, 2026-11-25: checkboxes, not one exclusive choice), confirm."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from fstk.core.errors import FstkError

#: (part, checkbox text, what it does)
PARTS = (
    ("entry", "Just the episode entry in the app",
     "Removes the episode from the list. The project and every file stay where they are; adding the episode again "
     "brings it back."),
    ("project", "Project (manifest, jobs, approvals, logs)",
     "Deletes the episode's project record. The entry in the app goes with it."),
    ("files", "Working files",
     "Deletes the stack master, audio, scripts, encodes and the finished video."),
    ("captures", "LaserDisc captures archive",
     "Deletes the captures fstk downloaded for this episode."),
)


def gib(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


def _where(items) -> str:
    return "\n".join(f"{it.path}   ({gib(it.bytes) if it.exists else 'not found'})" for it in items)


class RemoveDialog(QDialog):
    def __init__(self, services, episode: str, parent=None):
        super().__init__(parent)
        self.services, self.episode = services, episode
        self.plan = services.removal_plan(episode)
        self.removed: list[str] = []
        self.setWindowTitle(f"Remove {episode}")
        self.resize(660, 480)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>Remove {episode}</b> — tick what should go:"))
        self.boxes: dict[str, QCheckBox] = {}
        self.notes: dict[str, QLabel] = {}
        where = {"entry": "", "project": _where(self.plan.project), "files": _where(self.plan.files),
                 "captures": _where(self.plan.captures)}
        if not self.plan.captures:
            kept = self.plan.captures_kept
            where["captures"] = ((_where([kept]) + "\n") if kept else "") + \
                (self.plan.captures_note or "No downloaded copy of this episode was found.")
        for part, label, hint in PARTS:
            box = QCheckBox(label)
            box.setToolTip(hint)
            box.toggled.connect(self._update)
            self.boxes[part] = box
            lay.addWidget(box)
            note = QLabel(hint + (f"\n{where[part]}" if where[part] else ""))
            note.setWordWrap(True)
            note.setContentsMargins(24, 0, 0, 6)
            note.setStyleSheet("color: gray;")
            self.notes[part] = note
            lay.addWidget(note)
        if not self.plan.captures:                     # nothing fstk may delete there
            self.boxes["captures"].setEnabled(False)
            self.boxes["captures"].setToolTip(self.plan.captures_note or "No downloaded copy of this episode was found.")
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line)
        self.everything = QCheckBox("Everything")
        self.everything.setToolTip("All of the above that this episode has. The choices above are greyed out while "
                                   "this is ticked.")
        self.everything.toggled.connect(self._everything)
        lay.addWidget(self.everything)
        lay.addStretch(1)
        self.total = QLabel("")
        lay.addWidget(self.total)
        row = QHBoxLayout()
        row.addStretch(1)
        self.remove_button = QPushButton("Remove")
        cancel = QPushButton("Cancel")
        self.remove_button.clicked.connect(self.confirm)
        cancel.clicked.connect(self.reject)
        row.addWidget(self.remove_button)
        row.addWidget(cancel)
        lay.addLayout(row)
        self.remove_button.setToolTip("Delete what is ticked, after one more confirmation. Nothing is deleted while "
                                      "the episode is running.")
        cancel.setToolTip("Close without removing anything.")
        cancel.setDefault(True)
        self._update()

    # ------------------------------------------------------------------ state
    def available(self, part: str) -> bool:
        return part != "captures" or bool(self.plan.captures)

    @property
    def parts(self) -> frozenset[str]:
        """What will go. The project's deletion takes its entry along."""
        if self.everything.isChecked():
            chosen = {p for p, *_ in PARTS if self.available(p)}
        else:
            chosen = {p for p, box in self.boxes.items() if box.isChecked() and box.isEnabled()}
            if self.boxes["project"].isChecked():
                chosen.add("entry")
        if "project" in chosen:
            chosen.add("entry")
        return frozenset(chosen)

    def _everything(self, on: bool) -> None:
        for part, box in self.boxes.items():
            box.blockSignals(True)
            if on and self.available(part):
                box.setChecked(True)
            box.setEnabled(not on and self.available(part))
            box.blockSignals(False)
        self._update()

    def _update(self, *_args) -> None:
        if not self.everything.isChecked():
            entry = self.boxes["entry"]
            entry.blockSignals(True)
            if self.boxes["project"].isChecked():          # no project, no entry
                entry.setChecked(True)
                entry.setEnabled(False)
            else:
                entry.setEnabled(True)
            entry.blockSignals(False)
        parts = self.parts
        for part, note in self.notes.items():
            note.setEnabled(part in parts or not parts)
        freed = self.plan.total_bytes(parts - {"entry"}) if parts - {"entry"} else 0
        if not parts:
            self.total.setText("Nothing is ticked.")
        elif freed:
            self.total.setText(f"<b>Total to free: {gib(freed)}</b>")
        else:
            self.total.setText("No files are deleted.")
        self.remove_button.setEnabled(bool(parts))

    # ------------------------------------------------------------------ do it
    def describe(self, parts: frozenset[str]) -> str:
        names = {"entry": "the entry in the app", "project": "the project", "files": "the working files",
                 "captures": "the downloaded captures"}
        shown = [names[p] for p, *_ in PARTS if p in parts and not (p == "entry" and "project" in parts)]
        return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]

    def confirm(self) -> None:
        parts = self.parts
        if not parts:
            return
        deletes = bool(parts - {"entry"})
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Are you sure?")
        box.setText(f"Remove {self.describe(parts)} for {self.episode}?")
        box.setInformativeText("This cannot be undone." if deletes and parts != {"entry"} and
                               (parts & {"project", "files", "captures"})
                               else "You can add the episode again later; its project is kept.")
        yes = box.addButton("Remove", QMessageBox.ButtonRole.DestructiveRole)
        no = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        box.exec()
        if box.clickedButton() is not yes:
            return
        try:
            self.removed = self.services.remove_episode(self.episode, set(parts))
        except (FstkError, OSError) as exc:
            QMessageBox.warning(self, "Could not remove everything", str(exc))
            return
        self.accept()
