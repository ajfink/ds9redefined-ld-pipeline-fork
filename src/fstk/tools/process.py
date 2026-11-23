"""Run external tools: one implementation for every subprocess in the pipeline.

Ported from v38 ``run_workflow._run_child``/``_stop_child_tree``, ``streamutil`` and
``pipeline.ps1``'s ``Invoke-LdFfmpeg`` (which existed because PowerShell 5.1 corrupted native
stderr). Behaviour:

* each tool starts in its own process group/session so the whole tree can be stopped;
* stderr (and optionally stdout) is streamed line by line, splitting on ``\\n`` *and* ``\\r``
  so ffmpeg/x265 progress lines arrive as they are printed;
* every line is written to a log file (optionally redacted) and offered to ``on_line``;
* a ``cancel`` event or ``timeout`` stops the process tree (CTRL_BREAK → taskkill /T on
  Windows, SIGTERM → SIGKILL to the process group on POSIX);
* producer→consumer pipelines (``vspipe | ffmpeg``) run with both processes monitored; a
  failure in either stops both.

The result never claims success. Callers must validate the artifact (PORTING §5: Orc,
blackdetect and the downloader exit non-zero on success and zero on truncation).
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from fstk.core.errors import ToolError
from fstk.core.timeutil import utc_now_iso

LineCallback = Callable[[str, str], None]  # (stream_name, line)
TAIL_LINES = 60
STOP_GRACE_SECONDS = 8.0


@dataclass
class ProcessResult:
    args: list[str]
    returncode: int | None
    duration_s: float
    stdout: bytes | None
    tail: list[str]
    log_path: Path | None
    cancelled: bool = False
    timed_out: bool = False
    stages: list[ProcessResult] = field(default_factory=list)

    @property
    def exited_cleanly(self) -> bool:
        """Exit status 0 and not stopped. Not proof of success — validate the artifact."""
        return self.returncode == 0 and not (self.cancelled or self.timed_out)

    def describe(self) -> str:
        status = ("cancelled" if self.cancelled else "timed out" if self.timed_out
                  else f"exit {self.returncode}")
        tail = "\n".join(self.tail[-12:])
        return f"{Path(self.args[0]).name} {status} after {self.duration_s:.1f}s\n{tail}"


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class PauseControl:
    """Pause/resume every tool this process runs: running trees are suspended (SIGSTOP / NtSuspendProcess via
    psutil) and continued exactly where they stopped. Python-side stages wait at ``StageContext.report`` /
    ``check_cancel``. One switch for the whole run (the GUI runs one plan at a time)."""

    def __init__(self) -> None:
        self._flag = threading.Event()
        self._lock = threading.Lock()
        self._suspended: dict[int, list] = {}          # pid -> psutil processes suspended for this pause
        self.paused_total = 0.0
        self._since = 0.0

    @property
    def is_paused(self) -> bool:
        return self._flag.is_set()

    def pause(self) -> None:
        if not self._flag.is_set():
            self._since = time.monotonic()
            self._flag.set()

    def resume(self) -> None:
        if self._flag.is_set():
            self.paused_total += time.monotonic() - self._since
            self._flag.clear()

    def wait_while_paused(self, cancel: threading.Event | None = None, poll: float = 0.2) -> None:
        while self._flag.is_set() and not (cancel is not None and cancel.is_set()):
            time.sleep(poll)

    def sync(self, procs: Sequence[subprocess.Popen]) -> None:
        """Called by each running pipeline's monitor loop: bring its processes in line with the flag."""
        try:
            import psutil
        except ImportError:                            # pragma: no cover - psutil is a dependency
            return
        with self._lock:
            for p in procs:
                if p.poll() is not None:
                    self._suspended.pop(p.pid, None)
                    continue
                if self._flag.is_set() and p.pid not in self._suspended:
                    self._suspended[p.pid] = self._tree(psutil, p.pid, suspend=True)
                elif not self._flag.is_set() and p.pid in self._suspended:
                    for q in self._suspended.pop(p.pid):
                        with contextlib.suppress(psutil.Error):
                            q.resume()

    @staticmethod
    def _tree(psutil, pid: int, *, suspend: bool) -> list:
        try:
            root = psutil.Process(pid)
            tree = [root, *root.children(recursive=True)]
        except psutil.Error:
            return []
        done = []
        for q in tree:
            with contextlib.suppress(psutil.Error):
                q.suspend()
                done.append(q)
        return done

    def release(self, p: subprocess.Popen) -> None:
        """Continue ``p`` if it is suspended (before stopping it: a stopped process cannot handle a signal)."""
        with self._lock:
            for q in self._suspended.pop(p.pid, []):
                with contextlib.suppress(Exception):
                    q.resume()


PAUSE = PauseControl()


def stop_tree(proc: subprocess.Popen, *, grace: float = STOP_GRACE_SECONDS) -> None:
    """Stop ``proc`` and its descendants. Safe to call on an exited process."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            proc.terminate()
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, check=False)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


class _LineSplitter:
    """Incremental decoder splitting on \\n, \\r\\n and bare \\r."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[str]:
        self._buf += data
        out: list[str] = []
        while True:
            idx_n, idx_r = self._buf.find(b"\n"), self._buf.find(b"\r")
            candidates = [i for i in (idx_n, idx_r) if i >= 0]
            if not candidates:
                break
            i = min(candidates)
            if i == idx_r and i + 1 == len(self._buf):
                break  # may be the first half of \r\n; wait for more data
            line = self._buf[:i]
            skip = 2 if self._buf[i:i + 2] == b"\r\n" else 1
            self._buf = self._buf[i + skip:]
            out.append(line.decode("utf-8", errors="replace"))
        return out

    def flush(self) -> list[str]:
        rest, self._buf = self._buf, b""
        text = rest.rstrip(b"\r").decode("utf-8", errors="replace")
        return [text] if text else []


class _Log:
    def __init__(self, path: Path | None, redact: Callable[[str], str] | None):
        self.path = path
        self.redact = redact or (lambda s: s)
        self._lock = threading.Lock()
        self._stream: IO[str] | None = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = path.open("a", encoding="utf-8", newline="\n")

    def write(self, text: str) -> None:
        if self._stream:
            with self._lock:
                self._stream.write(self.redact(text))
                self._stream.flush()

    def close(self) -> None:
        if self._stream:
            self._stream.close()


def _pump(stream: IO[bytes], name: str, log: _Log, tail: deque, on_line: LineCallback | None,
          sink: list[bytes] | None) -> None:
    splitter = _LineSplitter()
    try:
        while chunk := stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536):
            if sink is not None:
                sink.append(chunk)
                continue
            for line in splitter.feed(chunk):
                _emit(name, line, log, tail, on_line)
        if sink is None:
            for line in splitter.flush():
                _emit(name, line, log, tail, on_line)
    finally:
        stream.close()


def _emit(name: str, line: str, log: _Log, tail: deque, on_line: LineCallback | None) -> None:
    log.write(f"[{name}] {line}\n")
    tail.append(f"[{name}] {line}")
    if on_line:
        try:
            on_line(name, line)
        except Exception as exc:  # a broken observer must not kill a long encode
            log.write(f"[runner] on_line callback raised {exc!r}\n")


def _check_args(args: Sequence[str]) -> list[str]:
    out = [str(a) for a in args]
    if not out:
        raise ToolError("empty command")
    return out


def run(args: Sequence[str | os.PathLike], *, log_path: Path | None = None, cwd: Path | None = None,
        env: Mapping[str, str] | None = None, capture_stdout: bool = False,
        stdout_path: Path | None = None, stdin_bytes: bytes | None = None,
        on_line: LineCallback | None = None, timeout: float | None = None,
        cancel: threading.Event | None = None, redact: Callable[[str], str] | None = None,
        poll_interval: float = 0.2) -> ProcessResult:
    """Run one tool. stdout is captured in memory, written to ``stdout_path``, or streamed as lines."""
    return run_pipeline([args], log_path=log_path, cwd=cwd, env=env, capture_stdout=capture_stdout,
                        stdout_path=stdout_path, stdin_bytes=stdin_bytes, on_line=on_line,
                        timeout=timeout, cancel=cancel, redact=redact, poll_interval=poll_interval)


def run_pipeline(commands: Sequence[Sequence[str | os.PathLike]], *, log_path: Path | None = None,
                 cwd: Path | None = None, env: Mapping[str, str] | None = None,
                 capture_stdout: bool = False, stdout_path: Path | None = None,
                 stdin_bytes: bytes | None = None, on_line: LineCallback | None = None,
                 timeout: float | None = None, cancel: threading.Event | None = None,
                 redact: Callable[[str], str] | None = None,
                 poll_interval: float = 0.2) -> ProcessResult:
    """Run ``cmd1 | cmd2 | ...``; stderr of every stage is logged as ``[name#i]``."""
    if capture_stdout and stdout_path:
        raise ToolError("choose capture_stdout or stdout_path, not both")
    argv = [_check_args(c) for c in commands]
    if not argv:
        raise ToolError("no commands")
    log = _Log(log_path, redact)
    names = [f"{Path(a[0]).stem}#{i}" if len(argv) > 1 else Path(a[0]).stem for i, a in enumerate(argv)]
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    started = time.monotonic()
    log.write(f"\n[{utc_now_iso()}] RUN: {' | '.join(_display(a) for a in argv)}\n")
    procs: list[subprocess.Popen] = []
    threads: list[threading.Thread] = []
    sink: list[bytes] | None = [] if capture_stdout else None
    out_file = stdout_path.open("wb") if stdout_path else None
    full_env = {**os.environ, **(env or {})} if env is not None else None
    try:
        for i, a in enumerate(argv):
            last = i == len(argv) - 1
            stdin = procs[-1].stdout if procs else (subprocess.PIPE if stdin_bytes is not None
                                                    else subprocess.DEVNULL)
            if last:
                stdout = out_file if out_file else subprocess.PIPE
            else:
                stdout = subprocess.PIPE
            try:
                p = subprocess.Popen(a, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, cwd=cwd,
                                     env=full_env, **_popen_kwargs())
            except OSError as exc:
                for q in procs:
                    stop_tree(q)
                raise ToolError(f"could not start {a[0]}: {exc}") from exc
            if procs and procs[-1].stdout:
                procs[-1].stdout.close()  # allow SIGPIPE to reach the producer
            procs.append(p)
            threads.append(threading.Thread(target=_pump, args=(p.stderr, names[i], log, tail, on_line, None),
                                            daemon=True, name=f"stderr-{names[i]}"))
        last_proc = procs[-1]
        if not out_file and last_proc.stdout is not None:
            threads.append(threading.Thread(target=_pump, args=(last_proc.stdout, "stdout", log, tail,
                                                                on_line, sink), daemon=True))
        for t in threads:
            t.start()
        if stdin_bytes is not None and procs[0].stdin:
            try:
                procs[0].stdin.write(stdin_bytes)
            except BrokenPipeError:
                pass
            finally:
                procs[0].stdin.close()
        cancelled = timed_out = False
        paused_before = PAUSE.paused_total
        while any(p.poll() is None for p in procs):
            if cancel is not None and cancel.is_set():
                cancelled = True
            elif (timeout is not None and not PAUSE.is_paused
                  and time.monotonic() - started - (PAUSE.paused_total - paused_before) > timeout):
                timed_out = True
            if cancelled or timed_out:
                for p in reversed(procs):
                    PAUSE.release(p)
                    stop_tree(p)
                break
            PAUSE.sync(procs)
            time.sleep(poll_interval)
        for p in procs:
            p.wait()
        for t in threads:
            t.join(timeout=10)
        duration = time.monotonic() - started
        stage_results = [ProcessResult(argv[i], p.returncode, duration, None, [], log_path,
                                       cancelled, timed_out) for i, p in enumerate(procs)]
        failing = next((r.returncode for r in stage_results if r.returncode not in (0,)), 0)
        rc = failing if len(procs) > 1 else procs[0].returncode
        log.write(f"[{utc_now_iso()}] EXIT: {[p.returncode for p in procs]} after {duration:.1f}s"
                  f"{' (cancelled)' if cancelled else ''}{' (timed out)' if timed_out else ''}\n")
        return ProcessResult(argv[-1] if len(argv) == 1 else argv[0], rc, duration,
                             b"".join(sink) if sink is not None else None, list(tail), log_path,
                             cancelled, timed_out, stage_results if len(procs) > 1 else [])
    except BaseException:
        for p in procs:
            stop_tree(p)
        raise
    finally:
        if out_file:
            out_file.close()
        log.close()


def _display(args: Sequence[str]) -> str:
    return " ".join(f'"{a}"' if (" " in a or not a) else a for a in args)
