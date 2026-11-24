"""Picture spot checks for the DVD-alignment review."""
from __future__ import annotations

import numpy as np

from fstk.media import alignment_visual as vis
from fstk.media.alignment import Act, AlignmentResult

W, H = 320, 240


def picture(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 255, (24, 32, 3), dtype=np.uint8)
    return np.kron(small, np.ones((10, 10, 1), np.uint8))


def result(*, spans=(), offset=-5.0, start=0, end=3000, n=300) -> AlignmentResult:
    idxs = np.linspace(start, end, n).astype(np.int64)
    act = Act(start_frame=start, end_frame=end, knots=[float(start), float(end)], values=[offset, offset],
              samples=n, residual_rms=0.1, inconsistent_spans=[list(s) for s in spans])
    return AlignmentResult(
        idxs=idxs, offsets=np.full(n, int(offset)), sub=np.full(n, offset), matched=np.ones(n, bool),
        ncc=np.full(n, 0.95), margin=np.ones(n), flat=np.zeros(n, bool),
        params={"unmatched_score": 0.5}, acts=[act])


def test_choose_spots_covers_the_programme_and_puts_suspects_first():
    res = result(spans=[[40.0, 44.0]])
    spots = vis.choose_spots(res, dvd_frames=5000)
    assert 3 <= len(spots) <= vis.MAX_POINTS
    assert [s.kind for s in spots[:3]] == ["suspect"] * 3
    assert all(40 * vis.FPS - 2 <= s.master_frame <= 44 * vis.FPS + 2 for s in spots[:3])
    assert all(s.dvd_frame == s.master_frame - 5 for s in spots)
    assert spots[-1].master_frame >= 2900


def test_choose_spots_stays_inside_the_dvd_and_without_evidence_returns_nothing():
    spots = vis.choose_spots(result(offset=-5.0), dvd_frames=1500)
    assert spots and all(s.dvd_frame + vis.SEARCH < 1500 for s in spots)
    res = result()
    res.matched[:] = False
    assert vis.choose_spots(res, 5000) == []


def test_judge_agrees_when_the_predicted_frame_is_the_best_match():
    spot = vis.Spot(100, 95, "act", "x")
    frames = {95 + k: picture(10 + k) for k in range(-vis.SEARCH, vis.SEARCH + 1)}
    vis.judge(spot, frames[95], frames)
    assert spot.verdict == vis.GOOD and spot.best_shift == 0 and spot.ncc > 0.99


def test_judge_notices_a_shift_and_unlike_pictures():
    frames = {95 + k: picture(10 + k) for k in range(-vis.SEARCH, vis.SEARCH + 1)}
    shifted = vis.judge(vis.Spot(100, 95, "act", "x"), frames[97], frames)
    assert shifted.verdict == vis.SHIFTED and shifted.best_shift == 2
    other = vis.judge(vis.Spot(100, 95, "act", "x"), picture(999), frames)
    assert other.verdict == vis.UNSURE
    empty = vis.judge(vis.Spot(100, 95, "act", "x"), picture(1), {})
    assert empty.verdict == vis.UNSURE


def test_assess_wording():
    ok = vis.Spot(1, 1, "act", "a", verdict=vis.GOOD)
    bad = vis.Spot(2000, 2000, "suspect", "b", verdict=vis.SHIFTED, best_shift=2, ncc=0.9)
    assert vis.assess([ok, ok])["verdict"] == "LOOKS GOOD"
    mixed = vis.assess([ok, bad])
    assert mixed["verdict"] == "CHECK THE FLAGGED POINTS" and "+2" in mixed["text"]
    assert vis.assess([])["verdict"] == "NOT CHECKED"


def test_sheet_and_note_are_png_files(tmp_path):
    spot = vis.Spot(100, 95, "act", "act 1: middle")
    frames = {95 + k: picture(10 + k) for k in range(-vis.SEARCH, vis.SEARCH + 1)}
    vis.judge(spot, frames[95], frames)
    out = vis.render_sheet([spot], {100: frames[95]}, frames, tmp_path / "s.png", title="T", summary="S")
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    vis.render_note(tmp_path / "n.png", "could not check")
    assert (tmp_path / "n.png").stat().st_size > 0
