"""video-trim: exact IVTC frame map → trims and DELTA (DVD-referenced)."""
from __future__ import annotations

import json
import re

import numpy as np
import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.media.trim import FPS, decide, fade_up_frame, tail_ld_only_start
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace

PAD, STACK = 18, 9000
EP = EpisodeId.parse("DS9:S01E20")


def master_of(k: int) -> int:
    """A TDecimate-like map: 4 of every 5 source frames survive (drop phase 2)."""
    return PAD + (k // 4) * 5 + [0, 1, 3, 4][k % 4]


def flat_master() -> np.ndarray:
    flat = np.ones(STACK, bool)
    flat[300:1200] = False            # LD-only front matter (logos)
    flat[1513:8700] = False           # programme, fades up at 1513
    flat[8800:8950] = False           # 5 s of LD-only picture after the programme
    return flat


def probe(n: int, head: int, tail: int) -> dict:
    idx = sorted(set(range(min(head, n))) | set(range(max(n - tail, 0), n)))
    return {"frames": n, "source_pad": PAD, "map": {str(k): master_of(k) for k in idx}}


def test_head_is_two_black_frames_before_the_fade_up_and_delta_is_exact():
    n = (STACK - PAD) * 4 // 5
    dec = decide(probe(n, 2000, 2000), flat=flat_master(), origin=1515, tail_cut=None)
    assert fade_up_frame(flat_master(), 1515) == 1513
    assert dec.head_master_frame >= 1511 and master_of(dec.head_trim - 1) < 1511
    assert dec.delta_s == master_of(dec.head_trim) / FPS
    assert dec.tail_trim == 0 and dec.frames == n - dec.head_trim


def test_ld_only_tail_is_cut_and_the_probe_must_cover_it():
    n = (STACK - PAD) * 4 // 5
    samples = [{"master_frame": f, "state": "unmatched" if 8700 <= f < 8960 else "matched",
                "flat": "1" if (f >= 8700 and not 8800 <= f < 8950) else "0"} for f in range(0, STACK, 15)]
    cut = tail_ld_only_start(samples, 8690, 15)
    assert cut is not None and 8800 <= cut <= 8815
    dec = decide(probe(n, 2000, 2000), flat=flat_master(), origin=1515, tail_cut=cut)
    assert dec.tail_trim > 0 and dec.tail_last_master_frame < cut <= master_of(n - 1 - dec.tail_trim + 1)
    with pytest.raises(ValueError):
        decide(probe(n, 2000, 50), flat=flat_master(), origin=1515, tail_cut=cut)


class FakeVS:
    """Honours the probe: writes the exact map like the real script would."""

    def __init__(self):
        self.calls = []

    def info(self, script):
        text = script.read_text()
        self.calls.append(script.name)
        n = (STACK - PAD) * 4 // 5
        m = re.search(r"open\('([^']+)', 'w'", text)
        if m:
            head = int(re.search(r"range\(min\((\d+), _n\)\)", text).group(1))
            tail = int(re.search(r"range\(max\(_n - (\d+), 0\)", text).group(1))
            with open(m.group(1), "w") as fh:
                json.dump(probe(n, head, tail), fh)
            return {"frames": n, "fps_num": 24000, "fps_den": 1001}
        ts = int(re.search(r"TRIM_START = (\d+)", text).group(1))
        te = int(re.search(r"TRIM_END = (\d+)", text).group(1))
        return {"frames": n - ts - te, "fps_num": 24000, "fps_den": 1001}


def test_stage_writes_trims_delta_and_a_verified_encode_script(tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m")})
    probe_dir, master_dir = prof.root("probe", EP), prof.root("prores", EP)
    probe_dir.mkdir(parents=True)
    master_dir.mkdir(parents=True)
    (master_dir / "master.mov").write_bytes(b"x")
    (probe_dir / "di.txt").write_text("[400 401]")
    desc = np.zeros((STACK, 1728), np.uint8)
    desc[~flat_master()] = np.random.default_rng(1).integers(0, 255, (int((~flat_master()).sum()), 1728))
    np.save(probe_dir / "master-desc.npy", desc)
    with open(probe_dir / "align.tsv", "w") as f:
        f.write("master_frame\tstate\tflat\n" + "".join(f"{x}\tmatched\t0\n" for x in range(1515, 8690, 15)))
    m = Manifest.new(EP, title="t")
    m.set("mode.dvd_reference_available", True, by="op")
    m.set("video.dvd_alignment", {"acts": [{"start_frame": 1515, "end_frame": 8685}], "origin_master_frame": 1515,
                                  "params": {"step": 15}}, by="t")
    for k, v in (("audio.pad_frames", PAD), ("stack.frames", STACK), ("video.repair_frames", 2)):
        m.set(k, v, by="t")
    m.set_artifact("stack_master", root="prores", name="master.mov")
    m.set_artifact("di_map", root="probe", name="di.txt")
    m.set_artifact("master_descriptors", root="probe", name="master-desc.npy")
    m.set_artifact("alignment_report", root="probe", name="align.tsv")
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(m, by="t", reason="setup")
    vs = FakeVS()
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": "trim",
            "steps": [{"id": "trim", "stage": "video-trim", "params": {}, "after": []}]}
    st = Engine(prof, plan, actor="op", tool_overrides={"vapoursynth": vs}).run()["steps"]["trim"]
    assert st["status"] == "passed", st.get("issues")
    m = ws.store.load()
    assert m.get("video.delta_s") == pytest.approx(master_of(m.get("video.head_trim")) / FPS)
    assert m.get("video.tail_trim") == 0 and m.get("video.frames") == (STACK - PAD) * 4 // 5 - m.get("video.head_trim")
    script = (prof.root("scripts", EP) / m.artifact("encode_script")["name"]).read_text()
    assert f"TRIM_START = {m.get('video.head_trim')}" in script and "FstkN" not in script
    assert f"assert out.num_frames == {m.get('video.frames')}" in script
    assert len(vs.calls) == 2


def test_delivery_plan_orders_every_stage_for_each_route():
    from fstk.core.errors import FstkError
    from fstk.workflow.plans import delivery_plan

    m = Manifest.new(EP, title="t")
    with pytest.raises(FstkError):
        delivery_plan(m)
    m.set("mode.dvd_reference_available", True, by="op")
    m.set("stack.frames", 83335, by="t")
    plan = delivery_plan(m)
    assert [s["stage"] for s in plan["steps"]] == [
        "stack-render", "cadence-evidence", "dvd-probe", "dvd-alignment", "video-trim", "dvd-ld-audio",
        "dvd-retime", "pad-audit", "pad-repair", "video-quality", "encode-final", "audio-quality", "remux", "qc-report", "episode-report"]
    render = plan["steps"][0]["params"]
    assert 25 < render["planned_gib"] < 30 and "287 KB" in render["estimate_basis"]
    m.set("mode.dvd_reference_available", False, by="op")
    assert [s["stage"] for s in delivery_plan(m)["steps"]] == [
        "stack-render", "cadence-evidence", "video-trim", "dvd-free-audio", "pad-audit", "pad-repair", "video-quality", "encode-final", "audio-quality", "remux", "qc-report", "episode-report"]


def test_no_dvd_programme_start_uses_the_v38_prior():
    from fstk.media.trim import no_dvd_programme_start

    flat = np.ones(int(120 * FPS), bool)
    flat[60:1200] = False                       # front matter (logos)
    flat[1515:2400] = False                     # programme fades up at 50.55 s
    flat[3285:3500] = False                     # main titles after the teaser
    assert no_dvd_programme_start(flat) == (1515, [])
    flat[1515:2400] = True                      # no fade-up in 45–58 s: widen, nearest to 50.6 s
    flat[2000:2400] = False                     # 66.7 s
    start, notes = no_dvd_programme_start(flat)
    assert start == 2000 and notes


class FakeFF:
    def gray_thumbnails(self, path, w, h, *, cancel=None, max_frames=None):
        desc = np.zeros((max_frames, w * h), np.uint8)
        tex = ~flat_master()[:max_frames]
        desc[tex] = np.random.default_rng(2).integers(0, 255, (int(tex.sum()), w * h))
        return desc


def test_stage_no_dvd_route(tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m")})
    probe_dir, master_dir = prof.root("probe", EP), prof.root("prores", EP)
    probe_dir.mkdir(parents=True)
    master_dir.mkdir(parents=True)
    (master_dir / "master.mov").write_bytes(b"x")
    (probe_dir / "di.txt").write_text("[400 401]")
    m = Manifest.new(EP, title="t")
    m.set("mode.dvd_reference_available", False, by="op")
    m.set("mode.no_dvd_proceed_approved", True, by="op")
    for k, v in (("audio.pad_frames", PAD), ("stack.frames", STACK), ("video.repair_frames", 2)):
        m.set(k, v, by="t")
    m.set_artifact("stack_master", root="prores", name="master.mov")
    m.set_artifact("di_map", root="probe", name="di.txt")
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(m, by="t", reason="setup")
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": "trim",
            "steps": [{"id": "trim", "stage": "video-trim", "params": {}, "after": []}]}
    st = Engine(prof, plan, actor="op", tool_overrides={"vapoursynth": FakeVS(), "ffmpeg": FakeFF()}).run()
    assert st["steps"]["trim"]["status"] == "passed", st["steps"]["trim"].get("issues")
    m = ws.store.load()
    assert master_of(m.get("video.head_trim")) >= 1511 > master_of(m.get("video.head_trim") - 1)
    assert m.get("video.tail_trim") == 0
