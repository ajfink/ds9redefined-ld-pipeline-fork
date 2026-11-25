"""The app's look: a light base with the Deep Space Nine palette as accents (owner, 2026-11-25).

Palette (sampled from the owner's swatch):

* ``PLUM``      #513037  menu bar, headings, the episode header band
* ``PERIWINKLE`` #a2afdb  progress fill, selected tab marker, selection on light backgrounds
* ``SLATE``     #7d86a7  selection/highlight, "info" banners and the review accents
* ``GREY``      #54585b  secondary text, borders
* ``MAROON``    #462224  the primary Run button

Status colours stay semantic (green done, red failed, amber blocked) so meaning never depends on the theme; the
palette carries the chrome around them. Under a dark system theme the same accents are used and the base stays the
system's own.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

PLUM = "#513037"
PERIWINKLE = "#a2afdb"
SLATE = "#7d86a7"
GREY = "#54585b"
MAROON = "#462224"
MAROON_HOVER = "#5c2f33"

STYLE = f"""
QMenuBar {{ background: {PLUM}; color: #f3eef0; padding: 2px; }}
QMenuBar::item {{ background: transparent; padding: 4px 10px; }}
QMenuBar::item:selected {{ background: {MAROON}; border-radius: 3px; }}
QMenu::item:selected {{ background: {SLATE}; color: white; }}
QProgressBar {{ border: 1px solid {GREY}; border-radius: 3px; text-align: center; }}
QProgressBar::chunk {{ background: {PERIWINKLE}; border-radius: 2px; }}
QToolButton#primary, QPushButton#primary {{
    background: {MAROON}; color: white; font-weight: 600; border: 1px solid {PLUM}; border-radius: 4px;
    padding: 5px 14px; }}
QToolButton#primary:hover, QPushButton#primary:hover {{ background: {MAROON_HOVER}; }}
QToolButton#primary:disabled, QPushButton#primary:disabled {{ background: {GREY}; color: #d0d0d0; }}
QToolButton#primary::menu-button {{ border-left: 1px solid {PLUM}; width: 18px; }}
QLabel#episodeTitle {{ color: {PLUM}; font-size: 17px; font-weight: 700; }}
QListWidget#episodeList::item:selected {{ background: {SLATE}; color: white; }}
QHeaderView::section {{ background: #eceef5; color: {GREY}; border: 0; border-right: 1px solid #d8dbe6;
    border-bottom: 1px solid #d8dbe6; padding: 3px; }}
"""


def apply(app: QApplication | None = None) -> None:
    """Accent the running application with the palette (idempotent)."""
    app = app or QApplication.instance()
    if app is None or app.property("fstk_theme"):
        return
    pal = app.palette()
    dark = pal.color(QPalette.ColorRole.Window).lightness() < 128
    pal.setColor(QPalette.ColorRole.Highlight, QColor(SLATE))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    pal.setColor(QPalette.ColorRole.Link, QColor(PERIWINKLE if dark else PLUM))
    app.setPalette(pal)
    style = STYLE
    if dark:                                         # keep the system's dark header sections
        style = style.split("QHeaderView::section")[0]
    app.setStyleSheet(app.styleSheet() + style)
    app.setProperty("fstk_theme", True)
