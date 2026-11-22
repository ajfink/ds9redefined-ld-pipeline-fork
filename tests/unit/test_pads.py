"""PAD audit math and repair-frame construction (owner rules 2026-11-21)."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from fstk.core.errors import ArtifactError
from fstk.media import pad_repair as pr
from fstk.media import pads as pm


def scene(seed: int, w=384, h=242) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
    img = cv2.GaussianBlur(img, (0, 0), 6)
    return cv2.normalize(img, None, 40, 215, cv2.NORM_MINMAX)


def shift(img: np.ndarray, dx: int) -> np.ndarray:
    return np.roll(img, dx, axis=1)


# ------------------------------------------------------------------ audit math
def test_blips_and_spans_from_blackdetect_events():
    blips, spans = pm.blips_from_events([(1.0, 1.0417, 0.0417), (5.0, 6.0, 1.0)])
    assert blips == [24] and spans == [(120, 143)]


def test_project_and_match_within_tolerance():
    out = pm.project_map_pads([100, 200, 300], pad_trim=20, head_trim=10)
    assert out == [(80 * 4 // 5) - 10, (180 * 4 // 5) - 10, (280 * 4 // 5) - 10]
    assert pm.match_blips([54, 134, 214], [55, 133, 400]) == {54: 55, 134: 133}


def test_classify_accounts_for_every_pad_and_leaves_dark_content_alone():
    projected = {10: -3, 20: 30, 30: 60, 40: 90, 50: 150}
    r = pm.classify_pads(projected, frames=100, blips=[31, 70], spans=[(88, 95)])
    st = {row["map_pad"]: row["status"] for row in r.rows}
    assert st == {10: "trimmed_away", 20: "matched", 30: "never_rendered", 40: "inside_break_black",
                  50: "trimmed_away"}
    assert r.candidates == [31] and r.orphans == [70]
    assert r.counts == {"map_pads": 5, "rendered_isolated": 1, "trimmed_away": 2, "inside_break_black": 1,
                        "never_rendered": 1}


def test_a_matched_pad_on_the_clip_boundary_fails_closed():
    with pytest.raises(ArtifactError):
        pm.classify_pads({1: 0}, frames=50, blips=[0])


def test_flank_scores_separate_cuts_from_motion_and_unknown_counts_as_cut():
    a, b = scene(1), scene(2)
    frames = [a, shift(a, 2), shift(a, 4), b, shift(b, 2)]
    thumbs = np.stack([cv2.resize(cv2.cvtColor(f, cv2.COLOR_RGB2GRAY), (48, 36), interpolation=cv2.INTER_AREA)
                       .ravel() for f in frames])
    s = pm.flank_scores(thumbs)
    assert np.isnan(s[0]) and np.isnan(s[-1])
    assert s[1] < 0.2 and s[2] > pm.CUT_THRESHOLD and s[3] > pm.CUT_THRESHOLD   # frames 2,3 straddle the cut
    assert not pm.is_cut(s[1]) and pm.is_cut(s[2]) and pm.is_cut(float("nan"))
    assert pm.is_cut(0.6)   # ambiguous band → the safe (cut) branch


def test_near_black_requires_the_whole_raster():
    z = np.zeros((20, 20, 3), np.uint8)
    assert pm.near_black(z)[0]
    z[3, 3] = 40
    assert not pm.near_black(z)[0]


# ------------------------------------------------------------------ repair method choice
def test_method_choice_follows_the_owner_rules():
    assert pr.choose_method(cut=False, dvd=True) == pr.RIFE == pr.choose_method(cut=False, dvd=False)
    assert pr.choose_method(cut=True, dvd=True) == pr.DVD_CUT
    assert pr.choose_method(cut=True, dvd=False) == pr.HOLD


# ------------------------------------------------------------------ DVD borrow
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
    err = np.abs(got.image[10:-10, 10:-10].astype(int) - truth[10:-10, 10:-10].astype(int)).mean()
    assert err < 6.0, err                       # tint corrected (uncorrected error is ≈ 10+)
    raw = cv2.resize(dvd[4], (truth.shape[1], truth.shape[0]))
    raw_err = np.abs(raw[10:-10, 10:-10].astype(int) - truth[10:-10, 10:-10].astype(int)).mean()
    assert err < raw_err
    assert got.post_ncc >= pr.MIN_POST_NCC


def test_borrow_declines_when_the_dvd_has_no_such_frame():
    ld_prev, _, ld_next, dvd, *_ = make_case()
    unrelated = np.stack([cv2.resize(scene(50 + i), (360, 240)) for i in range(12)])
    assert pr.borrow_dvd_frame(ld_prev, ld_next, unrelated) is None


# ------------------------------------------------------------------ script
BASE = "import vapoursynth as vs\ncore = vs.core\nout = core.std.BlankClip(length=100)\nout.set_output()\n"


def test_padfix_script_splices_without_changing_the_frame_count():
    text = pr.padfix_script(BASE, repairs=[{"frame": 10, "method": pr.RIFE, "slot": 0},
                                           {"frame": 40, "method": pr.HOLD, "slot": None}],
                            payload="C:/p/payload.mkv", expected_frames=100)
    assert text.count("out.set_output()") == 1 and text.rstrip().endswith("out.set_output()")
    assert "PAD_REPAIRS = [(10, 'rife', 0), (40, 'hold-prev-scene-cut', None)]" in text
    assert pr.padfix_matches_base(text, BASE) and not pr.padfix_matches_base(text, BASE + "# changed\n")
    compile(text, "padfix", "exec")


def test_hold_only_needs_no_payload_and_bad_inputs_are_refused():
    text = pr.padfix_script(BASE, repairs=[{"frame": 7, "method": pr.HOLD, "slot": None}], payload=None,
                            expected_frames=100)
    assert "_payload" not in text.split("PAD_REPAIRS")[1].split("_pieces")[0]
    with pytest.raises(ArtifactError):
        pr.padfix_script(BASE, repairs=[{"frame": 7, "method": pr.RIFE, "slot": 0}], payload=None, expected_frames=100)
    with pytest.raises(ArtifactError):
        pr.padfix_script(BASE, repairs=[{"frame": 7, "method": pr.HOLD}, {"frame": 8, "method": pr.HOLD}],
                         payload=None, expected_frames=100)
    with pytest.raises(ArtifactError):
        pr.padfix_script(BASE, repairs=[{"frame": 0, "method": pr.HOLD}], payload=None, expected_frames=100)
    with pytest.raises(ArtifactError):
        pr.padfix_script("out = 1\n", repairs=[], payload=None, expected_frames=1)


def test_replacements_tsv_round_trips_through_the_identity_reader(tmp_path):
    from fstk.qc.identity import read_replacements

    png = tmp_path / "a.png"
    png.write_bytes(b"x")
    tsv = tmp_path / "r.tsv"
    tsv.write_text(pr.replacements_tsv("S01E20", [{"frame": 12, "method": "dvd-cut-frame", "png": str(png)}]))
    assert read_replacements(tsv, "S01E20", tmp_path)[12]["method"] == "dvd-cut-frame"


# ------------------------------------------------------------------ A/V review proxy (P15)
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
