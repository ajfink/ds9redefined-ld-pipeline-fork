"""Turn a running tool's output lines into progress (fraction + a message with speed) for ``ctx.report``.

Understands what the pipeline's tools print:

* vspipe:   ``Frame: 1234/83335 (18.45 fps)``                         (needs no total from us)
* FFmpeg:   ``frame= 1234 fps=18 ... time=00:00:51.5`` and ``-progress`` lines ``frame=1234`` /
            ``out_time_us=51500000``                                  (needs ``total_frames`` or ``total_seconds``)
* generic:  ``37%`` / ``37.5 %``                                      (Orc and others, when they print it)

Reports are throttled, and a source of progress that appears is never allowed to move the bar backwards.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable

VSPIPE = re.compile(r"Frame:\s*(\d+)/(\d+)(?:\s*\(([\d.]+)\s*fps\))?")
FF_FRAME = re.compile(r"(?:^|\s)frame=\s*(\d+)")
FF_FPS = re.compile(r"fps=\s*([\d.]+)")
FF_TIME = re.compile(r"(?:^|\s)time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
FF_OUT_US = re.compile(r"^out_time_us=(\d+)\s*$")
PERCENT = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d+)?)\s*%")


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


class LineProgress:
    """``on_line`` callback: ``(stream, line)``. ``report(fraction, message)`` is ``StageContext.report`` shaped
    with the arguments swapped: it is called as ``report(base + span * fraction, message)``."""

    def __init__(self, report: Callable[[float | None, str], None], *, total_frames: int | None = None,
                 total_seconds: float | None = None, base: float = 0.0, span: float = 1.0, label: str = "",
                 min_interval: float = 1.0, clock: Callable[[], float] = time.monotonic):
        self.report, self.total_frames, self.total_seconds = report, total_frames, total_seconds
        self.base, self.span, self.label = base, span, label
        self.min_interval, self.clock = min_interval, clock
        self._last = -1e9
        self._best = 0.0

    def __call__(self, stream: str, line: str) -> None:
        got = self.parse(line)
        if got is None:
            return
        fraction, text = got
        now = self.clock()
        if now - self._last < self.min_interval and fraction < 1.0:
            return
        self._last = now
        self._best = max(self._best, min(max(fraction, 0.0), 1.0))
        self.report(self.base + self.span * self._best, f"{self.label} {text}".strip())

    def parse(self, line: str) -> tuple[float, str] | None:
        m = VSPIPE.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            if total > 0:
                speed = f" · {m.group(3)} fps" if m.group(3) else ""
                return done / total, f"frame {done}/{total}{speed}"
        m = FF_OUT_US.match(line)
        if m and self.total_seconds:
            t = int(m.group(1)) / 1e6
            return t / self.total_seconds, f"{_hms(t)} of {_hms(self.total_seconds)}"
        m = FF_FRAME.search(line)
        if m and self.total_frames:
            done = int(m.group(1))
            fps = FF_FPS.search(line)
            speed = f" · {fps.group(1)} fps" if fps else ""
            return done / self.total_frames, f"frame {done}/{self.total_frames}{speed}"
        m = FF_TIME.search(line)
        if m and self.total_seconds:
            t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            return t / self.total_seconds, f"{_hms(t)} of {_hms(self.total_seconds)}"
        m = PERCENT.search(line)
        if m and 0 <= float(m.group(1)) <= 100:
            return float(m.group(1)) / 100.0, f"{float(m.group(1)):.0f}%"
        return None
