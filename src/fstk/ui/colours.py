"""Status colours that follow the system palette (light, dark and high-contrast themes).

One place for the colours the app uses to say how a step or a review stands. On a dark window background the light
variant is used so the text stays readable; under a high-contrast theme the palette's own text colour wins.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

#: tone → (on a light background, on a dark background)
TONES: dict[str, tuple[str, str]] = {
    "good": ("#2e7d32", "#81c784"),
    "bad": ("#c62828", "#ef9a9a"),
    "warn": ("#e65100", "#ffb74d"),
    "info": ("#513037", "#a2afdb"),             # DS9 plum / periwinkle (fstk.ui.theme)
    "busy": ("#5a6385", "#a2afdb"),             # DS9 slate, darkened for text on white
    "quiet": ("#54585b", "#bdbdbd"),            # DS9 grey
}
#: step status → tone
STATUS_TONE: dict[str, str] = {
    "passed": "good", "failed": "bad", "blocked": "warn", "awaiting-review": "info", "running": "busy",
    "stale": "warn", "interrupted": "warn", "cancelled": "quiet", "not-started": "quiet", "pending": "quiet",
}


def dark_theme() -> bool:
    app = QApplication.instance()
    if app is None:
        return False
    return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


def tone(name: str) -> QColor:
    light, dark = TONES.get(name, TONES["quiet"])
    return QColor(dark if dark_theme() else light)


def status_colour(status: str) -> QColor:
    return tone(STATUS_TONE.get(status, "quiet"))


def quiet_css() -> str:
    """CSS colour for secondary text (hints, explanations), readable in both themes."""
    return tone("quiet").name()
