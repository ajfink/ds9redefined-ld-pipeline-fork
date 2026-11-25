from pathlib import Path

from fstk.media import hybrid
from fstk.stages.stretchpreview import HANDLE_FRAMES, MAX_PREVIEW_FRAMES, output_window, preview_wrapper


def test_window_has_handles_is_capped_and_respects_the_head_trim():
    s = hybrid.Segment(hybrid.VIDEO_I, 100, 130)            # output frames 400..519
    assert output_window(s, 40, 10_000) == (400 - 40 - HANDLE_FRAMES, 519 - 40 + HANDLE_FRAMES)
    long = hybrid.Segment(hybrid.VIDEO_I, 100, 1000)
    a, b = output_window(long, 0, 100_000)
    assert b - a + 1 == MAX_PREVIEW_FRAMES
    assert output_window(hybrid.Segment(hybrid.VIDEO_I, 0, 6), 1000, 10_000) is None    # trimmed away


def test_wrapper_compiles_and_labels_both_sides():
    text = preview_wrapper(Path("n.vpy"), Path("i.vpy"), 10, 99)
    compile(text, "w.vpy", "exec")
    assert "StackHorizontal" in text and "INTERPOLATED" in text and "Trim(_load('n.vpy'), 10, 99)" in text


class _Ctx:
    def __init__(self):
        import threading
        self.cancel, self.messages = threading.Event(), []

    def report(self, fraction=None, message=""):
        self.messages.append(message)

    def check_cancel(self):
        pass


class _FlakyVS:
    """vspipe dies silently on the first try (PC-A 2026-11-24 14:53), works on the second."""

    def __init__(self, fail_times):
        self.fail_times, self.calls = fail_times, 0

    def encode(self, wrapper, args, *, name, cancel=None):
        from fstk.tools.process import ProcessResult
        self.calls += 1
        if self.calls <= self.fail_times:
            return ProcessResult(["vspipe"], 4294967295, 8.3, None, ["[ffmpeg#1] Header too large."], None)
        Path(args[-1]).write_bytes(b"mkv")
        return ProcessResult(["vspipe"], 0, 40.0, None, [], None)


def test_a_preview_whose_first_try_dies_is_tried_once_more(tmp_path):
    from fstk.stages.stretchpreview import encode_preview

    ctx, vs = _Ctx(), _FlakyVS(1)
    encode_preview(ctx, vs, tmp_path / "w.vpy", tmp_path / "c.mkv", 1220)
    assert vs.calls == 2 and (tmp_path / "c.mkv").is_file()
    assert any("first try failed" in m and "free memory" in m for m in ctx.messages)


def test_a_preview_failing_twice_says_so_with_the_memory_at_each_failure(tmp_path):
    import pytest

    from fstk.core.errors import ArtifactError
    from fstk.stages.stretchpreview import encode_preview

    with pytest.raises(ArtifactError, match="failed twice") as e:
        encode_preview(_Ctx(), _FlakyVS(2), tmp_path / "w.vpy", tmp_path / "c.mkv", 1220)
    assert str(e.value).count("free memory") == 2 and "Header too large" in str(e.value)


def test_the_wrapper_closes_the_scripts_it_reads():
    text = preview_wrapper(Path("n.vpy"), Path("i.vpy"), 10, 99)
    assert "with open(p, encoding='utf-8') as f:" in text
