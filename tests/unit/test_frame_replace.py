"""Replacement frames borrowed from the DVD for operator frame fixes (media.frame_replace)."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from fstk.media import frame_replace as pr


def scene(seed: int, w=384, h=242) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
    img = cv2.GaussianBlur(img, (0, 0), 6)
    return cv2.normalize(img, None, 40, 215, cv2.NORM_MINMAX)


def shift(img: np.ndarray, dx: int) -> np.ndarray:
    return np.roll(img, dx, axis=1)


def make_case(gain=(0.94, 0.98, 1.03)):
    """LD flanks of a cut and a DVD window where the LD lost one frame of shot A (scaled, tinted)."""
    a, b = scene(10), scene(20)
    a_frames = [shift(a, 3 * k) for k in range(6)]
    b_frames = [shift(b, 3 * k) for k in range(6)]
    ld_prev, truth, ld_next = a_frames[3], a_frames[4], b_frames[0]
    dvd_seq = a_frames[:5] + b_frames[:6]            # truth sits between the flanks
    g = np.array(gain, np.float32)

    def to_dvd(f):
        small = cv2.resize(f, (360, 240), interpolation=cv2.INTER_AREA)
        return np.clip(small.astype(np.float32) / g, 0, 255).astype(np.uint8)   # the DVD is tinted the other way

    return ld_prev, truth, ld_next, np.stack([to_dvd(f) for f in dvd_seq]), a_frames, b_frames


def test_flank_match_requires_two_apart_and_uniqueness():
    ld_prev, _, ld_next, dvd, *_ = make_case()
    d = np.stack([pr.desc48(f) for f in dvd])
    m = pr.match_flanks(pr.desc48(ld_prev), pr.desc48(ld_next), d)
    assert m is not None and (m.p, m.q, m.candidate) == (3, 5, 4)
    # an LD pair that are adjacent on the DVD (no missing frame between them) is refused
    adjacent = pr.match_flanks(pr.desc48(dvd[2]), pr.desc48(dvd[3]), d)
    assert adjacent is None


def test_borrowed_frame_is_scaled_and_colour_matched_to_the_ld_neighbour():
    ld_prev, truth, ld_next, dvd, *_ = make_case()
    got = pr.borrow_dvd_frame(ld_prev, ld_next, dvd)
    assert got is not None and got.reference == "prev"
    assert got.image.shape == truth.shape
    assert got.image.dtype == np.uint16
    err = np.abs(got.image[10:-10, 10:-10].astype(float) / 257 - truth[10:-10, 10:-10].astype(float)).mean()
    assert err < 6.0, err                       # tint corrected (uncorrected error is ≈ 10+)
    raw = cv2.resize(dvd[4], (truth.shape[1], truth.shape[0]))
    raw_err = np.abs(raw[10:-10, 10:-10].astype(int) - truth[10:-10, 10:-10].astype(int)).mean()
    assert err < raw_err
    assert got.post_ncc >= pr.MIN_POST_NCC


def test_borrow_declines_when_the_dvd_has_no_such_frame():
    ld_prev, _, ld_next, dvd, *_ = make_case()
    unrelated = np.stack([cv2.resize(scene(50 + i), (360, 240)) for i in range(12)])
    assert pr.borrow_dvd_frame(ld_prev, ld_next, unrelated) is None


def test_a_gap_borrow_needs_the_flanks_gap_plus_one_apart():
    ld_prev, _, ld_next, dvd, a_frames, b_frames = make_case()
    seq2 = [to for to in dvd]                        # one missing frame in `dvd`
    d = np.stack([pr.desc48(f) for f in seq2])
    assert pr.match_flanks(pr.desc48(ld_prev), pr.desc48(ld_next), d, gap=1) is not None
    assert pr.match_flanks(pr.desc48(ld_prev), pr.desc48(ld_next), d, gap=2) is None


def test_av_proxy_is_h264_8bit_with_aac_and_atomic(tmp_path):
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg")
    from fstk.tools.ffmpeg import FFmpeg

    src = tmp_path / "s.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=24000/1001",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "4", "-c:v", "libx265",
                    "-pix_fmt", "yuv420p10le", "-x265-params", "log-level=none", "-c:a", "flac", str(src)], check=True)
    ff = FFmpeg("ffmpeg", "ffprobe")
    out = ff.av_proxy(src, 1.0, 2.0, tmp_path / "p" / "chunk-01.mp4")
    info = ff.probe(out)
    v = info.only("video").raw
    assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p"
    assert any(s.raw.get("codec_name") == "aac" for s in info.streams)
    assert 1.8 < info.duration_s < 2.3 and not list((tmp_path / "p").glob("*.part*"))
