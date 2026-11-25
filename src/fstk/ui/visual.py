"""Visual building blocks for every review: a clickable timeline, a picture-spot viewer and a headline.

A review is decided by looking and listening, not by reading tables. Stages describe what to show in a small JSON
block (``review_view``, see ``fstk.media.review_view``); these widgets draw it inside the app:

* ``TimelineBar`` – the whole programme as a bar: coloured stretches (acts, flagged spans) and ticks (the spots that
  were checked, the chunks to watch). Click to jump there.
* ``SpotViewer`` – the pictures for each checked spot, cut from the stage's picture sheet: thumbnails to choose a
  spot, a large view, buttons to switch between views (LaserDisc / DVD / difference …) and *Blink* to flip between
  the first two views so a shift or a difference jumps out.
* ``headline_html`` – the plain-language verdict with a coloured tag.
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

#: banner and headline tones: status colours stay semantic; "info" and "neutral" use the DS9 palette (fstk.ui.theme)
TONES = {"good": "#2e7d32", "warn": "#e65100", "bad": "#c62828", "info": "#513037", "neutral": "#54585b"}


def tone_colour(tone: str | None) -> str:
    return TONES.get(tone or "neutral", tone if (tone or "").startswith("#") else TONES["neutral"])


def clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def headline_html(verdict: str, text: str, tone: str) -> str:
    colour = tone_colour(tone)
    tag = (f"<span style='background:{colour};color:white;padding:2px 8px;border-radius:4px;"
           f"font-weight:600'>&nbsp;{_esc(verdict)}&nbsp;</span>&nbsp; ") if verdict else ""
    return f"<div style='font-size:14px'>{tag}{_esc(text)}</div>"


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_review_view(evidence) -> tuple[dict, Path | None]:
    """The ``review_view`` block a stage wrote into its JSON report, and that report's path."""
    for e in evidence:
        if str(e).lower().endswith(".json"):
            try:
                doc = json.loads(Path(e).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and isinstance(doc.get("review_view"), dict):
                return doc["review_view"], Path(e)
    return {}, None


# ------------------------------------------------------------------------------ timeline
class TimelineBar(QWidget):
    """The programme as a bar. ``marks``: stretches ``{start_s, end_s, tone|colour, label}``; ``ticks``: points
    ``{time_s, tone|colour, label, key}``. Clicking emits ``clicked(seconds)``; clicking near a tick also emits
    ``tick_clicked(key)``."""

    clicked = Signal(float)
    tick_clicked = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.duration = 0.0
        self.marks: list[dict] = []
        self.ticks: list[dict] = []
        self.cursor: float | None = None
        self.selected_key = None
        self.setMinimumHeight(46)
        self.setMaximumHeight(52)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_data(self, duration_s: float, marks=(), ticks=()) -> None:
        self.duration = max(float(duration_s or 0.0), 1e-6)
        self.marks, self.ticks = list(marks), list(ticks)
        self.update()

    def set_cursor(self, seconds: float | None) -> None:
        self.cursor = seconds
        self.update()

    def select_tick(self, key) -> None:
        self.selected_key = key
        self.update()

    def _x(self, t: float) -> float:
        w = self.width() - 12
        return 6 + w * min(max(t / self.duration, 0.0), 1.0)

    def _t(self, x: float) -> float:
        w = max(self.width() - 12, 1)
        return min(max((x - 6) / w, 0.0), 1.0) * self.duration

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar = QRectF(6, 8, self.width() - 12, self._bar_height())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#3a3a3a"))
        p.drawRoundedRect(bar, 4, 4)
        self._paint_inside(p, bar)
        for m in self.marks:
            c = QColor(m.get("colour") or tone_colour(m.get("tone")))
            c.setAlpha(int(m.get("alpha", 200)))
            x0, x1 = self._x(float(m["start_s"])), self._x(float(m["end_s"]))
            p.setBrush(c)
            p.drawRect(QRectF(x0, bar.top(), max(x1 - x0, 2.0), bar.height()))
        for t in self.ticks:
            x = self._x(float(t["time_s"]))
            c = QColor(t.get("colour") or tone_colour(t.get("tone")))
            sel = t.get("key") is not None and t.get("key") == self.selected_key
            p.setBrush(c)
            p.setPen(QPen(QColor("white"), 1.5) if sel else Qt.PenStyle.NoPen)
            size = 7 if sel else 5
            p.drawPolygon(QPolygonF([QPointF(x, bar.bottom() + 1), QPointF(x - size, bar.bottom() + 2 + size * 1.4),
                                     QPointF(x + size, bar.bottom() + 2 + size * 1.4)]))
            p.setPen(QPen(c, 2))
            p.drawLine(QPointF(x, bar.top()), QPointF(x, bar.bottom()))
        if self.cursor is not None:
            p.setPen(QPen(QColor("white"), 2))
            x = self._x(self.cursor)
            p.drawLine(QPointF(x, 4), QPointF(x, bar.bottom() + 4))
        p.setPen(QColor("#9e9e9e"))
        f = p.font()
        f.setPointSizeF(max(f.pointSizeF() - 2, 7))
        p.setFont(f)
        p.drawText(QRectF(6, 0, 80, 9), Qt.AlignmentFlag.AlignLeft, "0:00")
        p.drawText(QRectF(self.width() - 86, 0, 80, 9), Qt.AlignmentFlag.AlignRight, clock(self.duration))
        p.end()

    def _bar_height(self) -> float:
        return 18.0

    def _paint_inside(self, p: QPainter, bar: QRectF) -> None:
        """Hook for subclasses: draw inside the bar, under the marks."""

    def _hit(self, x: float):
        best, dist = None, 8.0
        for t in self.ticks:
            d = abs(self._x(float(t["time_s"])) - x)
            if d <= dist:
                best, dist = t, d
        return best

    def mousePressEvent(self, event) -> None:
        x = event.position().x()
        tick = self._hit(x)
        if tick is not None and tick.get("key") is not None:
            self.tick_clicked.emit(tick["key"])
        self.clicked.emit(float(tick["time_s"]) if tick is not None else self._t(x))

    def mouseMoveEvent(self, event) -> None:
        x = event.position().x()
        tick = self._hit(x)
        t = self._t(x)
        if tick is not None:
            tip = f"{clock(float(tick['time_s']))}  {tick.get('label', '')}"
        else:
            inside = [m for m in self.marks if float(m["start_s"]) <= t <= float(m["end_s"])]
            tip = clock(t) + ("  " + "; ".join(m.get("label", "") for m in inside) if inside else "")
        self.setToolTip(tip)


class WaveStrip(TimelineBar):
    """A capture's sound as a waveform (loudest sample per half second), with drop-outs marked in red."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.peaks: list[float] = []
        self.step_s = 0.5
        self.message = "reading the audio…"
        self.setMinimumHeight(58)
        self.setMaximumHeight(64)

    def set_overview(self, doc: dict | None, message: str = "") -> None:
        self.message = message
        if not doc:
            self.peaks = []
            self.update()
            return
        self.peaks, self.step_s = list(doc.get("peaks", [])), float(doc.get("step_s", 0.5))
        gaps = [{"start_s": a, "end_s": max(b, a + self.step_s), "tone": "bad", "alpha": 255,
                 "label": f"sound drops out for {(b - a) * 1000:.0f} ms"} for a, b in doc.get("gaps", [])]
        self.set_data(float(doc.get("duration_s", 0.0)), gaps, [])

    def _bar_height(self) -> float:
        return max(float(self.height() - 20), 18.0)

    def _paint_inside(self, p: QPainter, bar: QRectF) -> None:
        if not self.peaks:
            p.setPen(QColor("#9e9e9e"))
            p.drawText(bar, Qt.AlignmentFlag.AlignCenter, self.message)
            return
        n = len(self.peaks)
        width = int(bar.width())
        mid = bar.center().y()
        p.setPen(QPen(QColor("#64b5f6"), 1))
        for px in range(width):
            a, b = int(px * n / width), max(int((px + 1) * n / width), int(px * n / width) + 1)
            v = max(self.peaks[a:b] or [0.0])
            h = v * (bar.height() / 2 - 2)
            p.drawLine(QPointF(bar.left() + px, mid - h), QPointF(bar.left() + px, mid + h))


# ------------------------------------------------------------------------------ spot viewer
class SpotViewer(QWidget):
    """Pictures of the checked spots, cut from one picture sheet (``spots[i]["views"][j]["rect"]``)."""

    spot_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sheet = QPixmap()
        self.spots: list[dict] = []
        self.index = -1
        self.view = 0
        self.thumbs = QListWidget()
        self.thumbs.setViewMode(QListWidget.ViewMode.IconMode)
        self.thumbs.setFlow(QListWidget.Flow.LeftToRight)
        self.thumbs.setWrapping(False)
        self.thumbs.setIconSize(QSize(120, 90))
        self.thumbs.setFixedHeight(140)
        self.thumbs.setMovement(QListWidget.Movement.Static)
        self.thumbs.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.thumbs.currentRowChanged.connect(self.show_spot)
        self.thumbs.setToolTip("Each checked spot. The colour says how it looked; pick one to see it large.")
        self.caption = QLabel("")
        self.caption.setWordWrap(True)
        self.caption.setTextFormat(Qt.TextFormat.RichText)
        self.caption.setToolTip("Where this spot is and what the check found there.")
        self.big = QLabel("")
        self.big.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.big.setMinimumSize(320, 200)
        self.big.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.big.setStyleSheet("background:#151515;")
        self.big.setToolTip("The selected spot, large. Use the buttons below to switch pictures, or Blink to flip.")
        self.views_row = QHBoxLayout()
        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        self.view_group.idClicked.connect(self.set_view)
        self.blink = QToolButton()
        self.blink.setText("Blink")
        self.blink.setCheckable(True)
        self.blink.setToolTip("Flip between the first two pictures twice a second: anything that moves or changes "
                              "between them stands out.")
        self.blink.toggled.connect(self._blink_toggled)
        self.prev_button = QToolButton()
        self.prev_button.setText("◀")
        self.prev_button.setToolTip("Previous spot.")
        self.prev_button.clicked.connect(lambda: self.step(-1))
        self.next_button = QToolButton()
        self.next_button.setText("▶")
        self.next_button.setToolTip("Next spot.")
        self.next_button.clicked.connect(lambda: self.step(1))
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._flip)
        controls = QHBoxLayout()
        controls.addWidget(self.prev_button)
        controls.addWidget(self.next_button)
        controls.addLayout(self.views_row)
        controls.addStretch(1)
        controls.addWidget(self.blink)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.thumbs)
        lay.addWidget(self.caption)
        lay.addWidget(self.big, 1)
        lay.addLayout(controls)
        self._view_names: list[str] = []

    def set_spots(self, sheet: str | Path | None, spots: list[dict]) -> None:
        self.timer.stop()
        self.blink.setChecked(False)
        self.sheet = QPixmap(str(sheet)) if sheet else QPixmap()
        self.spots = [s for s in spots if s.get("views")] if not self.sheet.isNull() else []
        self.thumbs.blockSignals(True)
        self.thumbs.clear()
        for s in self.spots:
            item = QListWidgetItem(QIcon(self._crop(s, 0)), f"{clock(float(s.get('time_s', 0)))}\n{s.get('verdict', '')}")
            item.setForeground(QBrush(QColor(tone_colour(s.get("tone")))))
            item.setToolTip(s.get("label", ""))
            self.thumbs.addItem(item)
        self.thumbs.blockSignals(False)
        self.index = -1
        self.setVisible(bool(self.spots))
        if self.spots:
            self.thumbs.setCurrentRow(0)
            self.show_spot(0)

    def _crop(self, spot: dict, view: int) -> QPixmap:
        views = spot.get("views") or []
        if not views or self.sheet.isNull():
            return QPixmap()
        x, y, w, h = (int(v) for v in views[min(view, len(views) - 1)]["rect"])
        return self.sheet.copy(QRect(x, y, w, h))

    def show_spot(self, index: int) -> None:
        if not 0 <= index < len(self.spots):
            return
        self.index = index
        if self.thumbs.currentRow() != index:
            self.thumbs.setCurrentRow(index)
        s = self.spots[index]
        names = [v.get("name", f"view {i + 1}") for i, v in enumerate(s["views"])]
        if names != self._view_names:
            self._rebuild_view_buttons(names)
        self.view = min(self.view, len(names) - 1)
        colour = tone_colour(s.get("tone"))
        self.caption.setText(f"<b>{clock(float(s.get('time_s', 0)))}</b> &nbsp;{_esc(s.get('label', ''))} &nbsp;"
                             f"<span style='color:{colour};font-weight:600'>{_esc(s.get('verdict', ''))}</span>"
                             + (f"<br><span style='color:#9e9e9e'>{_esc(s['detail'])}</span>" if s.get("detail") else ""))
        self._paint()
        self.spot_changed.emit(index)

    def _rebuild_view_buttons(self, names: list[str]) -> None:
        for b in self.view_group.buttons():
            self.view_group.removeButton(b)
            b.deleteLater()
        for i, n in enumerate(names):
            b = QPushButton(n)
            b.setCheckable(True)
            b.setToolTip(f"Show the {n.lower()} picture of this spot.")
            self.view_group.addButton(b, i)
            self.views_row.addWidget(b)
        self._view_names = names
        self.blink.setEnabled(len(names) >= 2)

    def set_view(self, view: int) -> None:
        self.view = view
        self._paint()

    def step(self, delta: int) -> None:
        if self.spots:
            self.show_spot((self.index + delta) % len(self.spots))

    def _blink_toggled(self, on: bool) -> None:
        if on:
            self.timer.start()
        else:
            self.timer.stop()

    def _flip(self) -> None:
        if self.index >= 0 and len(self._view_names) >= 2:
            self.set_view(1 if self.view == 0 else 0)

    def _paint(self) -> None:
        if not 0 <= self.index < len(self.spots):
            self.big.clear()
            return
        b = self.view_group.button(self.view)
        if b is not None and not b.isChecked():
            b.setChecked(True)
        pix = self._crop(self.spots[self.index], self.view)
        if pix.isNull():
            self.big.clear()
            return
        room = self.big.size()
        scaled = pix.scaled(room, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.FastTransformation if pix.width() * 2 <= room.width()
                            else Qt.TransformationMode.SmoothTransformation)
        self.big.setPixmap(scaled)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._paint()


class DetailsToggle(QToolButton):
    """'Show details' switch for the numbers behind a review (hidden unless asked for)."""

    def __init__(self, target: QWidget, label: str = "details", parent=None):
        super().__init__(parent)
        self.target, self.label = target, label
        self.setCheckable(True)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setArrowType(Qt.ArrowType.RightArrow)
        self.setToolTip("Show or hide the measurements behind this review.")
        self.toggled.connect(self._toggled)
        self._toggled(False)

    def _toggled(self, on: bool) -> None:
        self.target.setVisible(on)
        self.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.setText(("Hide " if on else "Show ") + self.label)
