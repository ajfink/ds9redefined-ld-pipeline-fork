from __future__ import annotations

import sys
import threading
import time

import pytest

from fstk.core.errors import ToolError
from fstk.tools.process import _LineSplitter, run, run_pipeline

PY = sys.executable


def py(code: str) -> list[str]:
    return [PY, "-c", code]


def test_line_splitter_handles_cr_progress_and_crlf():
    s = _LineSplitter()
    out = s.feed(b"frame=1\rframe=2\rdone\r\nnext\npartial")
    assert out == ["frame=1", "frame=2", "done", "next"]
    assert s.feed(b"\r") == []          # a trailing \r may be half of \r\n
    assert s.feed(b"\nx") == ["partial"]
    assert s.flush() == ["x"]


def test_run_captures_stdout_and_logs_stderr(tmp_path):
    log = tmp_path / "logs" / "tool.log"
    seen = []
    res = run(py("import sys; print('{\"ok\": 1}'); sys.stderr.write('warn A\\rwarn B\\n')"),
              log_path=log, capture_stdout=True, on_line=lambda n, line: seen.append(line))
    assert res.exited_cleanly and res.stdout.strip() == b'{"ok": 1}'
    assert seen == ["warn A", "warn B"]
    text = log.read_text()
    assert "RUN:" in text and "warn B" in text and "EXIT: [0]" in text


def test_nonzero_exit_is_reported_not_raised(tmp_path):
    res = run(py("import sys; sys.stderr.write('boom\\n'); sys.exit(3)"))
    assert res.returncode == 3 and not res.exited_cleanly and "boom" in res.describe()


def test_missing_executable_raises_tool_error(tmp_path):
    with pytest.raises(ToolError, match="could not start"):
        run([str(tmp_path / "nope")])


def test_timeout_stops_process_tree(tmp_path):
    # parent spawns a child that would outlive it; the whole group must die
    marker = tmp_path / "child.txt"
    code = (f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
            f"'import time; time.sleep(3); open(r\"{marker}\",\"w\").write(\"alive\")']); time.sleep(30)")
    started = time.monotonic()
    res = run(py(code), timeout=0.5)
    assert res.timed_out and not res.exited_cleanly and time.monotonic() - started < 15
    time.sleep(3.5)
    assert not marker.exists(), "child process survived the tree stop"


def test_cancel_event(tmp_path):
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    res = run(py("import time; time.sleep(30)"), cancel=cancel)
    assert res.cancelled and not res.exited_cleanly


def test_pipeline_to_file(tmp_path):
    out = tmp_path / "out.bin"
    res = run_pipeline([py("import sys; sys.stdout.buffer.write(b'abc'*1000)"),
                        py("import sys; d=sys.stdin.buffer.read(); sys.stdout.buffer.write(d[::-1])")],
                       stdout_path=out)
    assert res.exited_cleanly and out.read_bytes() == (b"abc" * 1000)[::-1]
    assert [s.returncode for s in res.stages] == [0, 0]


def test_pipeline_reports_failing_producer(tmp_path):
    res = run_pipeline([py("import sys; sys.stdout.write('x'); sys.exit(5)"),
                        py("import sys; sys.stdin.read()")], capture_stdout=True)
    assert res.returncode == 5 and [s.returncode for s in res.stages] == [5, 0]


def test_stdin_bytes_and_redaction(tmp_path):
    log = tmp_path / "r.log"
    res = run(py("import sys; d=sys.stdin.read(); sys.stderr.write(d)"), stdin_bytes=b"/secret/path\n",
              log_path=log, redact=lambda t: t.replace("/secret", "<root>"))
    assert res.exited_cleanly and "<root>/path" in log.read_text() and "/secret" not in log.read_text()
