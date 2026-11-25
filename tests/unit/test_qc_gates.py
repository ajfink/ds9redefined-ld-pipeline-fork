from __future__ import annotations

import pytest

from fstk.core.errors import ArtifactError
from fstk.qc.gates import (
    F24,
    Gate,
    allowed_skips,
    parse_srt,
    retime_gap_status,
    seek_points,
    seekability,
    single_black_frames,
    stream_inventory,
    subtitle_timeline,
    summarize,
)


def test_single_black_frames_are_blips_not_fades():
    events = [(240 / F24, 240 / F24 + 0.042, 0.042),           # one black frame
              (500 / F24, 500 / F24 + 2.0, 2.0)]                # long fade: not a blip
    blips = single_black_frames(events)
    assert [b["frame"] for b in blips] == [240]


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


def _av(constant=2.0, slope=1.0, rms=6.0, local=(3.0, -4.0)):
    rows = [{"out_time_s": 100.0 + 600 * i, "window_duration_s": 20.0, "av_ms": constant + d}
            for i, d in enumerate(local)]
    return {"constant_ms": constant, "slope_ppm": slope, "rms_ms": rms, "rows": rows, "pass": True}


def test_av_prescreen_is_confident_only_for_a_tight_fit_under_every_chunk():
    from fstk.qc.gates import av_prescreen

    chunks = [{"start_s": 95.0, "end_s": 110.0}, {"start_s": 695.0, "end_s": 710.0}]
    ok = av_prescreen(_av(), chunks)
    assert ok["available"] and ok["confident"]
    assert not av_prescreen(_av(constant=35.0), chunks)["confident"]            # inside the ±100 ms gate, outside ±20
    assert not av_prescreen(_av(slope=15.0), chunks)["confident"]
    drifting = av_prescreen(_av(local=(3.0, 55.0)), chunks)
    assert not drifting["confident"] and "chunk at 695 s" in drifting["reason"]
    gap = av_prescreen(_av(), [*chunks, {"start_s": 1500.0, "end_s": 1515.0}])
    assert not gap["confident"] and "no fit window" in gap["reason"]
    none = av_prescreen(None, chunks)
    assert not none["available"] and not none["confident"]                       # no DVD → full manual check


def test_inventory_follows_the_recorded_crop_and_the_finished_ld_rate():
    """S01E20 (PC-A, CP74): the picture was cropped to 754x484 and the LD track finished at 48 kHz; both are
    what the pipeline records, not faults."""
    from fstk.qc.gates import expected_raster

    assert expected_raster(None) == (768, 484)
    assert expected_raster({"left": 10, "right": 4, "top": 0, "bottom": 0}) == (754, 484)
    cropped = [dict(GOOD[0], width=754), dict(GOOD[1], sample_rate="48000", bits_per_raw_sample="24")]
    assert stream_inventory(cropped, {"duration": "100"}, ["ld"], 100.0, "420", raster=(754, 484)).status == "PASS"
    g = stream_inventory(cropped, {"duration": "100"}, ["ld"], 100.0, "420")          # uncropped expectation
    assert g.status == "FAIL" and "raster=754x484" in g.detail
    odd = [GOOD[0], dict(GOOD[1], sample_rate="32000")]
    g = stream_inventory(odd, {"duration": "100"}, ["ld"], 100.0, "420")
    assert g.status == "FAIL" and "32000Hz" in g.detail


def test_a_lossy_track_may_end_on_a_whole_codec_frame_but_never_short():
    def tagged(s, secs):
        return dict(s, tags={**s.get("tags", {}), "DURATION": f"00:01:{secs:06.3f}"})

    layout = [{"role": "ld", "codec_name": "flac", "channels": 2, "default": True,
               "title": "LD 2.0 (original Dolby Surround)"},
              {"role": "compat", "codec_name": "aac", "channels": 2, "default": False, "title": "compat"}]
    aac = stream("audio", codec_name="aac", channels=2, disposition={"default": 0},
                 tags={"language": "eng", "title": "compat"})
    base = [tagged(GOOD[0], 40.0), tagged(GOOD[1], 40.001)]
    ok = stream_inventory([*base, tagged(aac, 40.021)], {"duration": "100.021"}, ["ld"], 100.0, "420",
                          audio_layout=layout)
    assert ok.status == "PASS", ok.detail
    short = stream_inventory([*base, tagged(aac, 39.9)], {"duration": "100.001"}, ["ld"], 100.0, "420",
                             audio_layout=layout)
    assert short.status == "FAIL" and "aac track duration" in short.detail
    long_flac = stream_inventory([base[0], tagged(GOOD[1], 40.021), tagged(aac, 40.021)], {"duration": "100.021"},
                                 ["ld"], 100.0, "420", audio_layout=layout)
    assert long_flac.status == "FAIL" and "audio flac duration" in long_flac.detail


def test_av_cuts_come_from_the_alignment_breaks_when_no_ld_blacks_are_recorded():
    from fstk.qc.sync import alignment_cuts, fit_windows

    assert alignment_cuts(None) is None
    assert alignment_cuts({"breaks": []}) == []
    cuts = alignment_cuts({"breaks": [{"after_s": 280.28, "before_s": 280.781}, {"after_s": 224.725, "before_s": 225.225}]})
    assert cuts == [(224.725, 225.225), (280.28, 280.781)]
    wins, _ = fit_windows(cuts, 600.0, require_cuts=False)
    assert [round(a) for a, _ in wins] == [0, 227, 283]
    assert fit_windows([], 600.0, require_cuts=False)[0] == [(0.0, 600.0)]
    with pytest.raises(ArtifactError):
        fit_windows([], 600.0)                                   # v38 manifests must still have their blacks
    with pytest.raises(ArtifactError):
        alignment_cuts({"breaks": [{"after_s": 5.0, "before_s": 4.0}]})


def test_between_track_windows_keep_away_from_retime_switches():
    from fstk.qc.sync import retime_switch_gaps, sync_sample_points

    rmap = {"schema": "fstk-retime-map-v1",
            "switches": [{"out_s": 600.0}, {"out_s": 1200.0}],
            "pieces": [{"source": "dvd", "out_start_s": 0.0, "out_end_s": 600.0},
                       {"source": "ld", "out_start_s": 600.0, "out_end_s": 600.1},
                       {"source": "dvd", "out_start_s": 600.1, "out_end_s": 1800.0}]}
    gaps = retime_switch_gaps(rmap)
    assert gaps == [(598.0, 602.0), (1198.0, 1202.0)]           # the fill piece merges into its switch's span
    pts = sync_sample_points(gaps, 1800.0)
    assert len(pts) == 3 and all(not (g0 - 30 < p < g1 + 30) for p in pts for g0, g1 in gaps)
    with pytest.raises(ArtifactError):
        retime_switch_gaps({"schema": "other"})
