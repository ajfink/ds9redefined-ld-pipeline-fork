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
