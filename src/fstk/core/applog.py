"""Application log: one rotating file for everything that is not a tool's own output.

* ``fstk.log``         (5 x 5 MB)  GUI actions, every engine event, warnings, errors, uncaught exceptions with tracebacks
* ``fstk-crash.log``   Python ``faulthandler`` output: hard crashes (native library faults) that no Python code sees
Location: ``<FromStunToKill home>\\logs`` (``FSTK_HOME`` overrides), next to the work folders but never on a share.

Installed by the GUI and CLI entry points. Safe to call twice.
"""
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
import traceback
from pathlib import Path

LOG_NAME = "fstk.log"
CRASH_NAME = "fstk-crash.log"
_state: dict = {}


def log_dir() -> Path:
    from fstk.config.profile import default_app_home

    return default_app_home() / "logs"


def log_path() -> Path:
    return log_dir() / LOG_NAME


def setup_logging(*, directory: Path | None = None, console: bool = False, level: int | None = None) -> Path:
    """Start file logging, the exception hooks and faulthandler. Returns the log file path.

    On by default for the GUI and the command line. Level INFO; set ``FSTK_LOG_LEVEL=DEBUG`` for more."""
    if level is None:
        level = getattr(logging, os.environ.get("FSTK_LOG_LEVEL", "INFO").upper(), logging.INFO)
    d = Path(directory) if directory else log_dir()
    path = d / LOG_NAME
    if _state.get("path") == path:
        return path
    d.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("fstk")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s"))
    root.addHandler(handler)
    if console:
        root.addHandler(logging.StreamHandler(sys.stderr))
    root.propagate = False
    _install_hooks(d)
    session = logging.getLogger("fstk.session")
    session.info("=== session start pid=%s python=%s %s %s argv=%s", os.getpid(), platform.python_version(),
                 platform.system(), platform.release(), sys.argv)
    _state["path"] = path
    return path


def log_environment(profile=None) -> None:
    """One block at session start: versions, tools, folders and free space, so the log stands on its own."""
    from importlib import metadata

    log = logging.getLogger("fstk.session")
    versions = {}
    for name in ("fstk", "PySide6", "numpy", "opencv-python", "psutil", "VapourSynth", "soxr"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "-"
    log.info("packages %s", versions)
    if profile is None:
        return
    import shutil

    log.info("profile machine=%s os=%s path=%s", profile.machine, profile.os, profile.path)
    log.info("folders data=%s media=%s roots=%s downloads=%s", profile.data_root, profile.media_root,
             dict(profile.roots), profile.sources.get("downloads_root"))
    log.info("tools %s", dict(profile.tools))
    for label, p in (("data", profile.data_root), ("media", profile.media_root)):
        try:
            q = Path(p)
            while not q.exists() and q.parent != q:
                q = q.parent
            log.info("free space %s: %.1f GiB (%s)", label, shutil.disk_usage(q).free / 1024 ** 3, p)
        except OSError as exc:
            log.warning("free space %s: unknown (%s)", label, exc)


def _install_hooks(d: Path) -> None:
    log = logging.getLogger("fstk.crash")

    def excepthook(exc_type, exc, tb):
        log.critical("uncaught exception\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
        sys.__excepthook__(exc_type, exc, tb)

    def thread_hook(args):
        log.critical("uncaught exception in thread %s\n%s", args.thread.name if args.thread else "?",
                     "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    try:
        crash = (d / CRASH_NAME).open("a", encoding="utf-8")
        crash.write(f"\n--- session {time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()}\n")
        crash.flush()
        _state["crash_file"] = crash                      # kept open for the life of the process
        faulthandler.enable(file=crash, all_threads=True)
    except (OSError, RuntimeError, ValueError):
        pass                                              # a console-less pythonw has no stderr; best effort


def install_qt_handler() -> None:
    """Route Qt's own warnings/errors (``Exceptions caught in Qt event loop`` and the like) into the log."""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:                                   # pragma: no cover
        return
    log = logging.getLogger("fstk.qt")
    levels = {QtMsgType.QtDebugMsg: logging.DEBUG, QtMsgType.QtInfoMsg: logging.INFO,
              QtMsgType.QtWarningMsg: logging.WARNING, QtMsgType.QtCriticalMsg: logging.ERROR,
              QtMsgType.QtFatalMsg: logging.CRITICAL}

    def handler(mode, context, message):
        log.log(levels.get(mode, logging.WARNING), "%s", message)

    qInstallMessageHandler(handler)


def shutdown() -> None:
    logging.getLogger("fstk.session").info("=== session end pid=%s", os.getpid())
    logging.shutdown()
