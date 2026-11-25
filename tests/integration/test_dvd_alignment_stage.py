"""dvd-alignment against real FFmpeg-decoded videos (sequential descriptors, auto-acceptance)."""
from __future__ import annotations

import json
import shutil
import subprocess

import numpy as np
import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.tools.ffmpeg import FFmpeg
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace

pytestmark = [pytest.mark.media, pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg")]
EP = EpisodeId.parse("DS9:S01E20")
RATE = "30000/1001"
CONTENT = 1200
FRONT = 300
CONTENT_SRC = (f"testsrc2=size=160x120:rate={RATE}[a];cellauto=s=160x120:rate={RATE}:rule=30:seed=1:full=0,"
               "format=yuv420p[b];[a][b]blend=all_mode=average,format=yuv420p")


def make(path, graph, frames):
    out_map = ["-map", "[o]"] if graph.endswith("[o]") else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-filter_complex", graph, *out_map, "-frames:v", str(frames),
                    "-c:v", "mpeg2video", "-q:v", "3", str(path)], check=True, timeout=300)


@pytest.fixture
def env(tmp_path):
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "limits": {"min_free_gb": 0.001}})
    master_dir, dvd_dir = prof.root("prores", EP), tmp_path / "dvd"
    master_dir.mkdir(parents=True)
    dvd_dir.mkdir()
    return prof, master_dir, dvd_dir


def setup(prof, master, dvd):
    m = Manifest.new(EP, title="In the Hands of the Prophets")
    m.set("mode.dvd_reference_available", True, by="op")
    m.set_artifact("stack_master", root="prores", name=master.name)
    m.set_artifact("dvd", path=str(dvd))
    EpisodeWorkspace(prof, EP).store.create(m, by="t", reason="setup")


def run(prof, job, **params):
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": job,
            "steps": [{"id": "align", "stage": "dvd-alignment", "params": params, "after": []}]}
    return Engine(prof, plan, actor="op", tool_overrides={"ffmpeg": FFmpeg("ffmpeg", "ffprobe")}).run()


def test_alignment_is_measured_and_auto_accepted(env):
    prof, master_dir, dvd_dir = env
    dvd, master = dvd_dir / "dvd.mkv", master_dir / "S01E20-stack-master.mkv"
    make(dvd, CONTENT_SRC, CONTENT)
    front = f"life=s=160x120:rate={RATE}:seed=5:mold=10:ratio=0.5,format=yuv420p,trim=end_frame={FRONT}[f]"
    make(master, f"{front};{CONTENT_SRC},trim=end_frame={CONTENT}[c];[f][c]concat=n=2:v=1[o]",
         FRONT + CONTENT)
    setup(prof, master, dvd)
    st = run(prof, "a1")["steps"]["align"]
    assert st["status"] == "passed", st.get("issues")                      # no review needed
    m = EpisodeWorkspace(prof, EP).store.load()
    rec = m.get("video.dvd_alignment")
    assert rec["decision"] == "AUTO-ACCEPTED", rec["reasons"]
    assert len(rec["acts"]) == 1 and all(abs(v + FRONT) < 0.6 for v in rec["acts"][0]["values"])
    assert abs(m.get("video.dvd_origin_master_frame") - FRONT) <= 15
    path = np.load(prof.root("probe", EP) / m.artifact("align_npy")["name"])
    assert set(path[:, 1]) == {-FRONT}
    meta = json.loads((prof.root("probe", EP) / (m.artifact("master_descriptors")["name"] + ".meta.json")).read_text())
    assert meta["frames"] == FRONT + CONTENT and meta["output_grid_fps"] == "30000/1001"
    # the picture spot checks agree and the sheet exists as evidence
    rep = json.loads(next(prof.root("reports", EP).glob("dvd-alignment-*.json")).read_text())
    assert rep["picture_check"]["verdict"] == "LOOKS GOOD", rep["picture_check"]
    assert rep["picture_check"]["checked"] >= 3 and all(p["verdict"] == "agrees" for p in rep["picture_check"]["points"])
    sheet = prof.root("reports", EP) / rep["picture_sheet"]
    assert sheet.is_file() and sheet.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert m.artifact("alignment_sheet")["name"] == rep["picture_sheet"]
    # a second run reuses both descriptor sets (same sources) and agrees
    st = run(prof, "a2")["steps"]["align"]
    assert st["status"] == "passed" and "master reused, dvd reused" in st["plan_summary"]


def test_a_dvd_of_other_content_blocks(env):
    prof, master_dir, dvd_dir = env
    dvd, master = dvd_dir / "dvd.mkv", master_dir / "S01E20-stack-master.mkv"
    make(dvd, f"mandelbrot=s=160x120:rate={RATE},format=yuv420p", CONTENT)
    make(master, CONTENT_SRC, CONTENT)
    setup(prof, master, dvd)
    st = run(prof, "b1")["steps"]["align"]
    assert st["status"] == "blocked" and "right DVD" in st["issues"][0], st.get("issues")
    assert not EpisodeWorkspace(prof, EP).store.load().is_set("video.dvd_alignment")


def test_ld_only_material_mid_programme_is_held_for_review(env):
    prof, master_dir, dvd_dir = env
    dvd, master = dvd_dir / "dvd.mkv", master_dir / "S01E20-stack-master.mkv"
    make(dvd, CONTENT_SRC, CONTENT)
    other = f"mandelbrot=s=160x120:rate={RATE},format=yuv420p,trim=end_frame=450[x]"
    make(master, f"{CONTENT_SRC},split[c1][c2];[c1]trim=end_frame=600,setpts=PTS-STARTPTS[a1];"
                 f"[c2]trim=start_frame=600:end_frame={CONTENT},setpts=PTS-STARTPTS[a2];{other};"
                 "[a1][x][a2]concat=n=3:v=1[o]", CONTENT + 450)
    setup(prof, master, dvd)
    st = run(prof, "c1")["steps"]["align"]
    assert st["status"] == "awaiting-review", st.get("issues")
    m = EpisodeWorkspace(prof, EP).store.load()
    assert not m.is_set("video.dvd_alignment")                             # held until approved
    rep = json.loads(next(prof.root("reports", EP).glob("dvd-alignment-*.json")).read_text())
    assert rep["decision"] == "NEEDS-REVIEW" and any("no DVD match" in r for r in rep["reasons"]), rep["reasons"]
    assert rep["picture_check"]["checked"] >= 3 and rep["picture_sheet"].endswith("-sheet.png")
    assert (prof.root("reports", EP) / rep["picture_sheet"]).is_file()
    assert "Automatic picture check" in rep["review_summary"] and "numbers held it for review" in rep["review_summary"]
    # held, so nothing was recorded in the manifest; a new run still reuses the descriptor files on disk
    assert not m.artifact("master_descriptors")
    st = run(prof, "c2")["steps"]["align"]
    assert st["status"] == "awaiting-review" and "master reused, dvd reused" in st["plan_summary"]
