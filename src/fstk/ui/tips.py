"""Hover tooltips: every control says what it does, in plain words.

``apply_tips(owner, {"attribute": "text"})`` sets the tip on ``owner.attribute`` (a widget). ``missing_tips(root)``
lists the interactive widgets under ``root`` that still have none; a test runs it over every window and dialog so a
new control cannot be added without one.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSlider,
    QTabBar,
    QTableWidget,
    QToolButton,
    QTreeWidget,
    QWidget,
)

#: tips for buttons that mean the same thing wherever they appear (keyed by their text)
COMMON = {
    "Save": "Save the changes and close.",
    "Cancel": "Close without saving.",
    "OK": "Accept and close.",
    "Browse…": "Pick a folder or file with the file browser.",
    "Open": "Open the selected item.",
    "Close": "Close this window.",
    "Default": "Put back the default folder.",
}

INTERACTIVE = (QPushButton, QToolButton, QCheckBox, QRadioButton, QComboBox, QAbstractSpinBox, QLineEdit,
               QPlainTextEdit, QListWidget, QTableWidget, QTreeWidget, QSlider, QTabBar)


def apply_tips(owner: object, tips: dict[str, str]) -> None:
    for name, text in tips.items():
        widget = getattr(owner, name)
        if hasattr(widget, "setToolTip"):
            widget.setToolTip(text)


def apply_common(root: QWidget) -> None:
    """Give the standard buttons (Save, Cancel, Browse…) their usual tip where nothing more specific was set."""
    for b in root.findChildren(QPushButton):
        if not b.toolTip().strip() and b.text().replace("&", "") in COMMON:
            b.setToolTip(COMMON[b.text().replace("&", "")])


def _internal(widget: QWidget) -> bool:
    parent = widget.parent()
    if isinstance(parent, (QAbstractSpinBox, QComboBox)):
        return True                                   # the editor inside a spin box / combo box
    return widget.objectName().startswith("qt_") or type(widget).__name__ in ("ScrollLeftButton", "ScrollRightButton") \
        or widget.objectName() in ("ScrollLeftButton", "ScrollRightButton", "qt_scrollarea_viewport")


def missing_tips(root: QWidget) -> list[str]:
    """Interactive widgets under ``root`` without a tooltip, described by class, text and parent."""
    out = []
    for w in root.findChildren(QWidget):
        if not isinstance(w, INTERACTIVE) or _internal(w) or w.toolTip().strip():
            continue
        text = ""
        if hasattr(w, "text") and callable(w.text):
            try:
                text = str(w.text())
            except TypeError:
                text = ""
        if not text and hasattr(w, "placeholderText"):
            text = w.placeholderText()
        out.append(f"{type(w).__name__} {text[:40]!r} in {type(w.parent()).__name__}")
    return out
