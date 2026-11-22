from __future__ import annotations

import pytest

from fstk.core.errors import ArtifactError
from fstk.qc.gates import (
    F24,
    Gate,
    allowed_skips,
    pad_black_hits,
    parse_srt,
    retime_gap_status,
    seek_points,
    seekability,
    stream_inventory,
    subtitle_timeline,
    summarize,
)


def test_pad_black_hits_align_within_one_and_a_quarter_frames():
    events = [(240 / F24 + 0.02, 240 / F24 + 0.06, 0.042),    # single-frame black at PAD 240
              (500 / F24, 500 / F24 + 2.0, 2.0),               # long fade: not a blip
              (900 / F24 + 0.06, 900 / F24 + 0.10, 0.042)]     # 1.44 frames away from PAD 900
    hits = pad_black_hits([100, 240, 500, 900], events)
    assert [h["frame"] for h in hits] == [240]


def stream(kind, **kw):
    return {"codec_type": kind, **kw}


GOOD = [stream("video", profile="Main 10", pix_fmt="yuv420p10le", r_frame_rate="24000/1001", width=768, height=484,
               disposition={"default": 1}, tags={"title": "LaserDisc P1, 3:2 pulldown removed"}),
        stream("audio", codec_name="flac", channels=2, sample_rate="44100", bits_per_raw_sample="16",
               disposition={"default": 1}, tags={"language": "eng", "title": "LD 2.0 (original Dolby Surround)"})]


def test_stream_inventory_expects_the_chosen_chroma():
    assert stream_inventory(GOOD, {"duration": "100"}, ["ld"], 100.0, "420").status == "PASS"
    assert stream_inventory(GOOD, {"duration": "100"}, ["ld"], 100.0, "422").status == "FAIL"   # the 4:2:2 default
    g422 = [dict(GOOD[0], profile="Rext", pix_fmt="yuv422p10le"), *GOOD[1:]]
    assert stream_inventory(g422, {"duration": "100"}, ["ld"], 100.0).status == "PASS"          # default is 422


def test_stream_inventory():
    assert stream_inventory(GOOD, {"duration": "100.001", "size": "5"}, ["ld"], 100.0, "420").status == "PASS"
    g = stream_inventory(GOOD, {"duration": "100.01"}, ["ld"], 100.0, "420")
    assert g.status == "FAIL" and "container duration" in g.detail
    bad = [dict(GOOD[0], width=720), GOOD[1]]
    assert "raster=720x484" in stream_inventory(bad, {"duration": "100"}, ["ld"], 100.0, "420").detail
    g = stream_inventory(GOOD, {"duration": "100"}, ["ld", "51"], 100.0, "420")
    assert g.status == "FAIL" and "audio streams=1 want 2" in g.detail


def test_seekability_points_and_failures():
    assert seek_points(19.9) is None and seek_points(40.0) == [2.5, 12.5, 22.5, 32.5]
    ok = seekability(40.0, 2, lambda s, at, w, inp: w)
    assert ok.status == "PASS" and len(ok.metrics["samples"]) == 8
    trunc = seekability(40.0, 1, lambda s, at, w, inp: 1.0 if inp and at > 20 else w)
    assert trunc.status == "FAIL" and "max_interleave_delta 0" in trunc.detail

    def boom(s, at, w, inp):
        raise ArtifactError("decode failed")
    assert seekability(40.0, 1, boom).status == "ERROR"
    assert seekability(40.0, 0, boom).status == "FAIL"
    assert seekability(10.0, 1, boom).status == "ERROR"


SRT = "1\n00:00:01,000 --> 00:00:02,000\nA\n\n2\n00:00:03,000 --> 00:00:04,500\nB\n"


def test_subtitles():
    n, starts, ends, seq, mono = parse_srt(SRT)
    assert (n, starts, ends, seq, mono) == (2, [1.0, 3.0], [2.0, 4.5], True, True)
    assert subtitle_timeline(SRT, 10.0, 2).status == "PASS"
    assert subtitle_timeline(SRT, 10.0, 3).status == "FAIL"
    assert subtitle_timeline(SRT, 4.0, None).status == "FAIL"            # cue past the end
    assert subtitle_timeline(SRT.replace("2\n00:00:03", "3\n00:00:03"), 10.0, None).status == "FAIL"
    assert subtitle_timeline(None, 10.0, None).status == "FAIL"


def test_retime_gap_status_never_promotes_limit():
    assert retime_gap_status(None, False)[0] == "SKIP"
    assert retime_gap_status(None, True)[0] == "WARN"
    assert retime_gap_status({"status": "PASS", "gaps": [{"status": "PASS"}]}, True)[0] == "PASS"
    assert retime_gap_status({"status": "PASS", "gaps": [{"status": "LIMIT"}]}, True)[0] == "WARN"
    assert retime_gap_status({"status": "FAIL", "gaps": [{"status": "FAIL"}]}, True)[0] == "FAIL"
    assert retime_gap_status({"status": "PASS"}, True)[0] == "ERROR"


def test_summary_counts_unexpected_skips_as_unverified():
    gates = [Gate("frame count", "PASS"), Gate("absolute A/V", "SKIP"), Gate("replacement identity", "SKIP"),
             Gate("between-track sync", "SKIP"), Gate("stream inventory", "FAIL")]
    s = summarize(gates, allowed_skips(dvd_available=False, tracks=["ld"]))
    assert s["unverified_gates"] == ["replacement identity"] and s["failed_gates"] == ["stream inventory"]
    assert s["fail"] == 2
    s = summarize(gates, allowed_skips(dvd_available=None, tracks=["ld"]))
    assert "absolute A/V" in s["unverified_gates"]
    with pytest.raises(ValueError):
        Gate("x", "MAYBE")
