"""Exit 0xC000013A that fstk did not cause (2026-11-24 debug bundle: an encode failed after all frames were out)."""
from __future__ import annotations

from pathlib import Path

from fstk.stages.dvdborrow import dvd_window_script
from fstk.stages.encode import finished_anyway
from fstk.tools import process
from fstk.tools.process import STATUS_CONTROL_C_EXIT, ProcessResult


def res(rc, *, dur=0.4, cancelled=False, stages=()):
    return ProcessResult(["x"], rc, dur, b"", [], None, cancelled, False, list(stages))


def test_a_short_run_interrupted_unasked_is_run_once_more(monkeypatch):
    seq = [res(STATUS_CONTROL_C_EXIT), res(0)]
    calls = []
    monkeypatch.setattr(process, "run_pipeline", lambda cmds, **kw: calls.append(cmds) or seq.pop(0))
    assert process.run(["ffprobe", "-version"]).returncode == 0 and len(calls) == 2


def test_no_second_run_when_stopped_long_or_already_streamed(monkeypatch):
    for first, kw in ((res(STATUS_CONTROL_C_EXIT, cancelled=True), {}),
                      (res(STATUS_CONTROL_C_EXIT, dur=600), {}),
                      (res(STATUS_CONTROL_C_EXIT), {"on_stdout": lambda b: None}),
                      (res(1), {})):
        calls = []
        monkeypatch.setattr(process, "run_pipeline", lambda cmds, _f=first, _c=calls, **k: _c.append(1) or _f)
        assert process.run(["t"], **kw) is first and calls == [1]


def test_encode_accepts_vspipe_interrupted_only_after_serving_every_frame():
    done = res(STATUS_CONTROL_C_EXIT, stages=[res(STATUS_CONTROL_C_EXIT), res(0)])
    assert finished_anyway(done, {"frames": 65392}, 65392)
    assert not finished_anyway(done, {"frames": 65000}, 65392)                  # stopped early
    assert not finished_anyway(done, {}, 65392)                                 # never said it finished
    ff_failed = res(STATUS_CONTROL_C_EXIT, stages=[res(STATUS_CONTROL_C_EXIT), res(1)])
    assert not finished_anyway(ff_failed, {"frames": 65392}, 65392)
    stopped = res(STATUS_CONTROL_C_EXIT, stages=[res(STATUS_CONTROL_C_EXIT, cancelled=True), res(0)])
    assert not finished_anyway(stopped, {"frames": 65392}, 65392)


def test_dvd_window_honours_pulldown_flags_so_frames_match_the_alignment_grid():
    script = dvd_window_script(Path("dvd.mkv"), 100, 108)
    assert "core.bs.VideoSource('dvd.mkv', rff=True)" in script
    assert "Trim(src, 100, 108)" in script
