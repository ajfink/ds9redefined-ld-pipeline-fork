"""Windows taskbar button progress (green fill, yellow when paused, red on failure) through ITaskbarList3.

Qt 6 dropped QWinTaskbarButton, so this talks to the shell directly with ctypes. Everything is best-effort: on any
other platform, or if COM refuses, it quietly does nothing.
"""
from __future__ import annotations

import sys

NOPROGRESS, INDETERMINATE, NORMAL, ERROR, PAUSED = 0, 1, 2, 4, 8


class TaskbarProgress:
    def __init__(self, hwnd: int):
        self.hwnd = int(hwnd)
        self._tb = None
        self._vt = None
        if sys.platform == "win32":
            self._init()

    def _init(self) -> None:                                     # pragma: no cover - Windows only
        try:
            import ctypes
            from ctypes import POINTER, WINFUNCTYPE, c_int, c_uint, c_ulonglong, c_void_p, wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort),
                            ("d", ctypes.c_ubyte * 8)]

            def guid(text: str) -> GUID:
                import uuid

                u = uuid.UUID(text)
                g = GUID()
                g.a, g.b, g.c = u.time_low, u.time_mid, u.time_hi_version
                for i, byte in enumerate(u.bytes[8:]):
                    g.d[i] = byte
                return g

            ole32 = ctypes.windll.ole32
            ole32.CoInitialize(None)
            ptr = c_void_p()
            hr = ole32.CoCreateInstance(ctypes.byref(guid("56FDF344-FD6D-11d0-958A-006097C9A090")), None, 1,
                                        ctypes.byref(guid("ea1afb91-9e28-4b86-90e9-9e9f8a5eefaf")), ctypes.byref(ptr))
            if hr != 0 or not ptr.value:
                return
            vtable = ctypes.cast(ctypes.cast(ptr, POINTER(c_void_p))[0], POINTER(c_void_p))
            hr_init = WINFUNCTYPE(c_int, c_void_p)(vtable[3])
            if hr_init(ptr) != 0:
                return
            self._set_value = WINFUNCTYPE(c_int, c_void_p, wintypes.HWND, c_ulonglong, c_ulonglong)(vtable[9])
            self._set_state = WINFUNCTYPE(c_int, c_void_p, wintypes.HWND, c_uint)(vtable[10])
            self._tb, self._vt = ptr, vtable
        except Exception:
            self._tb = None

    @property
    def available(self) -> bool:
        return self._tb is not None

    def set(self, state: int, fraction: float = 0.0) -> None:
        if self._tb is None:
            return
        try:                                                      # pragma: no cover - Windows only
            self._set_state(self._tb, self.hwnd, state)
            if state in (NORMAL, ERROR, PAUSED):
                self._set_value(self._tb, self.hwnd, int(max(0.0, min(1.0, fraction)) * 1000), 1000)
        except Exception:
            self._tb = None
