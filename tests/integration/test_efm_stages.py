"""EFM decode + selection through the engine, against a fake Orc that writes real WAVs, reports
and Orc logs (and, like the real CLI, may exit non-zero on success)."""
from __future__ import annotations

import hashlib
import json
import re
import struct
import wave
from collections import namedtuple
from pathlib import Path

import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.tools.process import ProcessResult
from fstk.workflow.engine import PLAN_SCHEMA, Engine, EpisodeWorkspace
from fstk.workspace.space import GIB, SpaceLedger

EP = EpisodeId.parse("DS9:S01E20")
TITLE = "In the Hands of the Prophets"
LABELS = ["L1", "P1", "P6"]
REPORT = """Overall assessment : {assessment}
Disc duration : 46:08:21 (207,542 sections)
Audio integrity (programme area)
Concealed (interp) : {concealed:,} (0.10 %)
Silenced (muted) : {muted:,} (0.00 %)
Data loss (post-C2) : {loss} %
C1 uncorrectable : 45 (0.01 %)
C2 uncorrectable : 0 (0.00 %)
Missing (interpolated) : {missing}
Sections out-of-order / uncorrectable : 0 / 0
"""
GOOD = dict(assessment="GOOD", concealed=10, muted=0, loss="0.0010", missing=0)


def _value(text: str, key: str) -> str:
    m = re.search(rf"(?m)^        {key}:\n          type: \w+\n          value: (.+)$", text)
    return json.loads(m.group(1))


class FakeOrc:
    def __init__(self, quality=None, exit_code=1, wrong_tbc_in_log=False, mono=False):
        self.quality = quality or {}
        self.exit_code, self.wrong_tbc_in_log, self.mono = exit_code, wrong_tbc_in_log, mono
        self.calls = []

    def decode_efm(self, project, orc_log, *, log_name="", cancel=None, on_line=None):
        text = Path(project).read_text(encoding="utf-8")
        tbc, efm, wav = _value(text, "input_path"), _value(text, "efm_path"), _value(text, "output_path")
        label = re.search(r'user_label: "TBC Source (.+)"', text).group(1)
        self.calls.append((label, efm))
        with wave.open(wav, "wb") as w:
            w.setnchannels(1 if self.mono else 2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(struct.pack("<h", hash(label) % 1000) * 4410 * (1 if self.mono else 2))
        report = wav + ".txt"
        Path(report).write_text(REPORT.format(**{**GOOD, **self.quality.get(label, {})}), encoding="utf-8")
        logged_tbc = str(Path(tbc).with_name("other.tbc")) if self.wrong_tbc_in_log else tbc
        Path(orc_log).write_text("\n".join((
            f"tbc_source: using legacy JSON metadata: {tbc}.json",
            f"tbc_source: Loaded '{logged_tbc}' — NTSC TBC Composite (100 frames)",
            f"EFMSink: mode=audio, output={wav}",
            f"Writing decoding report to: {report}",
            f"EFMSink: Success: EFM decoded to {wav}")), encoding="utf-8")
        return ProcessResult(["orc"], self.exit_code, 0.1, None, [], None)


class FakeSevenZip:
    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads

    def list_slt(self, archive):
        name = Path(archive).name[:-3]
        return f"----------\nPath = {name}\nSize = {len(self.payloads[name])}\nFolder = -\n"

    def extract_member(self, archive, member, size, output):
        assert not Path(output).exists()
        Path(output).write_bytes(self.payloads[member])


Usage = namedtuple("Usage", "total used free")


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


def make_env(tmp_path):
    dl = tmp_path / "dl" / "DS9-S01E20-LaserDisc-Copies"
    dl.mkdir(parents=True)
    captures = []
    for x in LABELS:
        base = f"DS9-S01E20-{TITLE}-{x}"
        tbc, meta, efm = (dl / f"{base}.tbc", dl / f"{base}.tbc.json", dl / f"{base}.efm")
        tbc.write_bytes(x.encode() * 1000)
        meta.write_text(json.dumps({"videoParameters": {"numberOfSequentialFields": 600, "system": "NTSC"}}))
        efm.write_bytes(b"EFM" + x.encode() * 500)
        captures.append({"label": x, "files": {
            k: {"name": p.name, "bytes": p.stat().st_size, "md5": _md5(p.read_bytes())}
            for k, p in (("tbc", tbc), ("json", meta), ("efm", efm))}})
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "data"),
                              "media_root": str(tmp_path / "media"),
                              "sources": {"downloads_root": str(tmp_path / "dl")}, "limits": {"min_free_gb": 1}})
    m = Manifest.new(EP, title=TITLE)
    m.set_sources({"captures": captures})
    ws = EpisodeWorkspace(prof, EP)
    ws.store.create(m, by="t", reason="setup")
    ledger = SpaceLedger(tmp_path / "ledger.json", reserve_gib=1, disk_usage=lambda p: Usage(0, 0, 100 * GIB))
    return prof, ws, ledger, dl


def _stack(env, order=("P6", "L1", "P1"), amap="1+0, 2+3, 3+12"):
    prof, ws, _, _ = env
    project = prof.root("stack", EP) / "stack.orcprj"
    project.parent.mkdir(parents=True, exist_ok=True)
    nodes = "".join(f"    - id: {i + 1}\n      stage: tbc_source\n      user_label: TBC Source {x}\n"
                    for i, x in enumerate(order))
    project.write_text("project:\n  video_format: NTSC\n  source_format: Composite\n  amplitude_unit: 10bit\n"
                       f"dag:\n  nodes:\n{nodes}", encoding="utf-8")
    with ws.store.transaction(by="test", reason="stack measured") as m:
        m.set("stack.reference", order[0], by="test", note="fixture")
        m.set("stack.alignment_map", amap, by="test")
        m.set_artifact("stack_project", root="stack", name="stack.orcprj")


def run(env, steps, job, **tools):
    prof, ws, ledger, _ = env
    plan = {"schema": PLAN_SCHEMA, "episode": str(EP), "job": job, "steps": steps}
    return Engine(prof, plan, actor="op", ledger=ledger, tool_overrides=tools).run()


DECODE = {"id": "efm", "stage": "efm-decode", "params": {}, "after": []}
SELECT = {"id": "select", "stage": "efm-select", "params": {}, "after": ["efm"]}


def test_decode_all_then_auto_select_unique_pareto(env):
    prof, ws, _, _ = env
    _stack(env)
    orc = FakeOrc(quality={"L1": dict(concealed=50), "P1": dict(muted=3)})   # P6 dominates both
    state = run(env, [DECODE, SELECT], "j1", orc=orc)
    assert state["steps"]["efm"]["status"] == "passed", state["steps"]["efm"]
    assert state["steps"]["select"]["status"] == "passed", state["steps"]["select"]
    m = ws.store.load()
    prov = m.get("audio.efm_provenance")
    assert sorted(prov) == LABELS and all(len(v) == 1 for v in prov.values())
    assert m.get("audio.efm_provenance_required") is True
    assert m.get("audio.capture") == "P6" and m.get("audio.pad_frames") == 0
    sel = m.get("audio.capture_selection")
    assert sel["method"] == "unique-pareto" and sel["selected_by"] == "op"
    wav = prof.root("audio", EP) / m.artifact("ld_native")["name"]
    assert wav.name == "P6-efm.wav" and wav.is_file()
    report = json.loads((ws.state_dir / prov["P6"][0]["report_path"]).read_text())
    assert report["status"] == "VERIFIED" and report["sources"]["efm"]["md5_matches_archive"] is True
    assert report["quality"]["missing_sections"] == 0 and report["wav"]["channels"] == 2
    assert all(report["path_checks"].values())
    # a second run keeps verified decodes instead of decoding again
    state = run(env, [DECODE], "j2", orc=orc)
    assert state["steps"]["efm"]["status"] == "passed" and len(orc.calls) == 3
    assert "keeping verified" in state["steps"]["efm"]["plan_summary"]


def test_ambiguous_quality_picks_least_bad_and_operator_can_override(env):
    _, ws, _, _ = env
    _stack(env)
    orc = FakeOrc(quality={"L1": dict(concealed=5, muted=9), "P1": dict(concealed=99), "P6": dict(muted=4)})
    state = run(env, [DECODE, SELECT], "a1", orc=orc)
    sel = state["steps"]["select"]
    assert sel["status"] == "passed", sel                                   # no human needed
    m = ws.store.load()
    assert m.get("audio.capture") == "P1"                                   # zero silenced samples
    assert m.get("audio.capture_selection")["method"] == "least-audible-damage-v1"
    short = dict(SELECT, params={"capture": "P1", "note": "ok"})
    state = run(env, [DECODE, short], "a2", orc=orc)
    assert state["steps"]["select"]["status"] == "blocked" and "note" in state["steps"]["select"]["issues"][0]
    chosen = dict(SELECT, params={"capture": "P1", "note": "listened: L1 has audible dropouts at 12:40"})
    state = run(env, [DECODE, chosen], "a3", orc=orc)
    assert state["steps"]["select"]["status"] == "passed", state["steps"]["select"]
    m = ws.store.load()
    assert m.get("audio.capture") == "P1" and m.get("audio.pad_frames") == 12
    assert m.get("audio.capture_selection")["method"] == "operator"
    assert len(orc.calls) == 3                                              # decodes reused across jobs
    # re-selecting keeps history
    again = dict(SELECT, params={"capture": "L1", "note": "second opinion after QC listen"})
    run(env, [DECODE, again], "a4", orc=orc)
    m = ws.store.load()
    assert m.get("audio.capture") == "L1" and m.get("audio.capture_selection_history")[0]["capture"] == "P1"


def test_tampered_wav_blocks_selection_and_is_redecoded(env):
    prof, ws, _, _ = env
    _stack(env)
    orc = FakeOrc(quality={"L1": dict(concealed=50), "P1": dict(muted=3)})
    run(env, [DECODE], "t1", orc=orc)
    prov = ws.store.load().get("audio.efm_provenance")
    report = json.loads((ws.state_dir / prov["P6"][0]["report_path"]).read_text())
    wav = prof.root("audio", EP) / report["wav"]["relative_path"]
    data = bytearray(wav.read_bytes())
    data[-1] ^= 0xFF
    wav.write_bytes(bytes(data))
    state = run(env, [{**SELECT, "after": []}], "t2", orc=orc)
    assert state["steps"]["select"]["status"] == "blocked"
    assert "WAV changed" in state["steps"]["select"]["issues"][0]
    state = run(env, [DECODE, SELECT], "t3", orc=orc)
    assert state["steps"]["select"]["status"] == "passed"
    assert len(orc.calls) == 4                                              # only P6 decoded again
    assert len(ws.store.load().get("audio.efm_provenance")["P6"]) == 2
    assert wav.is_file()                                                    # tampered file kept, not deleted


def test_log_bound_to_another_tbc_fails_and_records_nothing(env):
    _, ws, _, _ = env
    state = run(env, [DECODE], "x1", orc=FakeOrc(wrong_tbc_in_log=True))
    st = state["steps"]["efm"]
    assert st["status"] == "failed" and "loaded_tbc" in st["issues"][0]
    assert not ws.store.load().is_set("audio.efm_provenance")


def test_non_native_wav_fails(env):
    state = run(env, [DECODE], "x2", orc=FakeOrc(mono=True))
    assert state["steps"]["efm"]["status"] == "failed" and "44.1 kHz" in state["steps"]["efm"]["issues"][0]


def test_md5_mismatch_stops_before_orc(env):
    _, _, _, dl = env
    orc = FakeOrc()
    efm = next(dl.glob("*-L1.efm"))
    efm.write_bytes(b"X" + efm.read_bytes()[1:])                            # same size, different bytes
    state = run(env, [DECODE], "x3", orc=orc)
    assert state["steps"]["efm"]["status"] == "failed" and "MD5" in state["steps"]["efm"]["issues"][0]
    assert orc.calls == []


def test_compressed_efm_is_extracted_into_work_and_decoded(env):
    prof, ws, _, dl = env
    payload = b"EFMRAW" * 100
    with ws.store.transaction(by="test", reason="P1 ships as .efm.7z") as m:
        caps = m.captures
        for cap in caps:
            if cap["label"] == "P1":
                old = dl / cap["files"]["efm"]["name"]
                archive = old.with_name(old.name + ".7z")
                archive.write_bytes(b"7z" + payload[:10])
                cap["files"]["efm"] = {"name": archive.name, "bytes": archive.stat().st_size}
        m.set_sources({**m.doc["sources"], "captures": caps})
    member = next(c for c in ws.store.load().captures if c["label"] == "P1")["files"]["efm"]["name"][:-3]
    orc = FakeOrc()
    state = run(env, [{**DECODE, "params": {"captures": ["P1"]}}], "z1", orc=orc,
                sevenzip=FakeSevenZip({member: payload}))
    assert state["steps"]["efm"]["status"] == "passed", state["steps"]["efm"]
    (label, efm_used), = orc.calls
    assert label == "P1" and Path(efm_used).is_relative_to(prof.root("work", EP)) and efm_used.endswith(".efm")
    prov = ws.store.load().get("audio.efm_provenance")["P1"][-1]
    report = json.loads((ws.state_dir / prov["report_path"]).read_text())
    assert report["decoded_efm"]["root"] == "work"
    assert report["decoded_efm"]["sha256"] == hashlib.sha256(payload).hexdigest()


def test_plan_estimates_space_from_field_count(env):
    from fstk.stages.efm import estimate_wav_bytes

    _, _, _, dl = env
    size, basis = estimate_wav_bytes(next(dl.glob("*-L1.tbc.json")), "NTSC")
    assert basis.startswith("600 fields") and size == int(600 / (60000 / 1001) * 176400 * 1.02)
