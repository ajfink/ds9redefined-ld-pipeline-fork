"""Two simulated machines (Legion, Mac) sharing one shared_root: presence, handoff, adoption."""
from __future__ import annotations

import json
import shutil

import pytest

from fstk.config.profile import SCHEMA, Profile
from fstk.core.errors import ConfigError, WorkflowError
from fstk.core.identity import EpisodeId
from fstk.evidence.approvals import ApprovalBook
from fstk.workflow.engine import Engine, EpisodeWorkspace
from fstk.workflow.plans import intake_plan
from fstk.workspace.device import load_or_create
from fstk.workspace.episodes import create_from_local
from fstk.workspace.portable import PathMap, decode, encode
from fstk.workspace.shared import SharedError, SharedTree
from tests.integration.test_intake_flow import _local_folder

EP = EpisodeId.parse("VOY:S01E01")


def machine(tmp_path, name, shared, volume_mount):
    base = tmp_path / name
    (base / "cfg").mkdir(parents=True)
    prof = Profile.from_dict({"schema": SCHEMA, "machine": name, "os": "linux", "data_root": str(base / "data"),
                              "media_root": str(base / "media"), "shared_root": str(shared),
                              "volumes": {"flex-e-hdd": str(volume_mount)}, "limits": {"min_free_gb": 0.001}},
                             path=base / "cfg" / "machine.json")
    return prof


@pytest.fixture
def pair(tmp_path):
    shared = tmp_path / "share"
    shared.mkdir()
    flex_disk = tmp_path / "flex-e-hdd"          # the same disk, mounted differently on each machine
    flex_disk.mkdir()
    (tmp_path / "mac-mounts").mkdir()
    (tmp_path / "mac-mounts" / "e hdd").symlink_to(flex_disk)
    legion = machine(tmp_path, "LENOVO-LEGION-5", shared, flex_disk)
    mac = machine(tmp_path, "MAC-M5", shared, tmp_path / "mac-mounts" / "e hdd")
    folder = _local_folder(flex_disk)
    return legion, mac, folder


def test_device_identity_is_stable_and_never_shared(tmp_path, pair):
    legion, mac, _ = pair
    a, b = load_or_create(legion), load_or_create(legion)
    assert a == b and a.name.startswith("LENOVO-LEGION-5-") and len(a.instance_id) == 8
    shutil.copy(legion.path.with_name("device.json"), mac.path.with_name("device.json"))
    with pytest.raises(ConfigError, match="belongs to machine"):
        load_or_create(mac)
    pinned = Profile.from_dict({**legion.to_dict(), "instance_id": "0BADF00D"}, path=legion.path)
    with pytest.raises(ConfigError, match="conflicts"):
        load_or_create(pinned)


def test_portable_paths_round_trip_across_os_spellings():
    win = PathMap(True, {"data_root": r"C:\Users\finke\fstk", "volume:flex-e-hdd": r"\\LENOVOFLEX\e hdd",
                         "media_root": r"D:\media", "root:stack": r"E:\stack"})
    mac = PathMap(False, {"data_root": "/Users/finke/fstk", "volume:flex-e-hdd": "/Volumes/e hdd",
                          "media_root": "/Volumes/fast/media"})
    doc = {"a": r"\\lenovoflex\E HDD\DS9-S01E20-LaserDisc-Copies\x-L1.tbc", "b": r"D:\media\ds9-s01e20\audio\L1.wav",
           "c": r"E:\stack\DS9\S01E20\master.mov", "d": "plain words C:\\not a whole path? no", "e": [r"C:\elsewhere\x"]}
    left: set[str] = set()
    enc = encode(doc, win, leftovers=left)
    assert enc["a"] == "{volume:flex-e-hdd}/DS9-S01E20-LaserDisc-Copies/x-L1.tbc"
    assert enc["c"] == "{root:stack}/DS9/S01E20/master.mov" and left == {r"C:\elsewhere\x"}
    out = decode(enc, mac)
    assert out["a"] == "/Volumes/e hdd/DS9-S01E20-LaserDisc-Copies/x-L1.tbc"
    assert out["b"] == "/Volumes/fast/media/ds9-s01e20/audio/L1.wav"
    assert out["c"] == "{root:stack}/DS9/S01E20/master.mov" and mac.unmapped == {"root:stack"}
    assert out["d"] == doc["d"]


def test_presence_handoff_adopt_and_freeze(pair):
    legion, mac, folder = pair
    create_from_local(legion, folder, actor="andrew@legion")
    lt, mt = SharedTree(legion), SharedTree(mac)
    (p,) = mt.presence(EP)
    assert p.device == lt.device and p.state == "active" and not p.mine
    with pytest.raises(SharedError, match="is active on"):
        create_from_local(mac, folder, actor="andrew@mac")
    # legion runs intake; the engine publishes after the run
    events = []
    state = Engine(legion, intake_plan(EP, download=False, job="inv", include_stack_reference=False,
                                       include_efm=False), actor="andrew@legion", events=events.append).run()
    assert state["steps"]["inventory"]["status"] == "passed"
    assert any(e["kind"] == "shared-publish" and "published" in e["outcome"] for e in events)
    ws = EpisodeWorkspace(legion, EP)
    report = legion.root("reports", EP) / "capture-inventory.json"
    ApprovalBook(ws.state_dir / "approvals").record(review="inventory", decision="APPROVED", actor="andrew",
                                                    note="captures look right", evidence=[report])
    status = lt.publish(EP)
    assert status["jobs"][0]["steps"]["inventory"] == "passed"
    # hand off to the Mac: legion is frozen
    with pytest.raises(SharedError, match="no device"):
        lt.handoff(EP, to="NOT-A-DEVICE-00000000", actor="andrew")
    handoff = lt.handoff(EP, to=mt.device, actor="andrew", note="Mac finishes the audio")
    assert handoff["to"] == mt.device and handoff["snapshot"]
    with pytest.raises(WorkflowError, match="handed off"):
        Engine(legion, intake_plan(EP, download=False, job="again", include_stack_reference=False,
                                   include_efm=False), actor="andrew").run()
    with pytest.raises(SharedError, match="has not published"):
        mt.adopt(EpisodeId.parse("DS9:S01E20"), source=lt.device, actor="andrew")
    # the Mac adopts: paths re-rooted onto its own mounts, approval still valid there
    rep = mt.adopt(EP, source=lt.device, actor="andrew@mac")
    assert rep["from"]["device"] == lt.device and rep["unmapped_tokens"] == []
    m = EpisodeWorkspace(mac, EP).store.load()
    assert m.doc["sources"]["downloads_override"] == str(mac.volumes["flex-e-hdd"] + "/" + folder.name)
    assert m.capture_labels == ["L1", "P2"]
    mws = EpisodeWorkspace(mac, EP)
    assert mws.store.journal()[0]["origin"] == "shared-adopt"
    assert (mws.state_dir / "manifest" / f"journal.source-{lt.device}.jsonl").is_file()
    st = mws.approvals.status("inventory", manifest=m)
    assert st.state == "approved", st.reasons
    assert st.record["bindings"]["evidence"][0]["path"].startswith(str(mac.data_root))
    # the Mac can run work; Legion cannot take it back now
    state = Engine(mac, intake_plan(EP, download=False, job="mac-inv", include_stack_reference=False,
                                    include_efm=False), actor="andrew@mac").run()
    assert state["steps"]["inventory"]["status"] == "passed"
    assert lt.adopted_by(EP) == [mt.device]
    with pytest.raises(SharedError, match="already adopted"):
        lt.reclaim(EP, actor="andrew")
    states = {p.device: p.state for p in lt.presence(EP)}
    assert states == {lt.device: "handed-off", mt.device: "active"}


def test_reclaim_before_adoption_and_fork(pair):
    legion, mac, folder = pair
    create_from_local(legion, folder, actor="a")
    lt, mt = SharedTree(legion), SharedTree(mac)
    lt.handoff(EP, to=mt.device, actor="a")
    lt.reclaim(EP, actor="a")
    assert {p.device: p.state for p in lt.presence(EP)}[lt.device] == "active"
    with pytest.raises(SharedError, match="has not handed"):
        mt.adopt(EP, source=lt.device, actor="b")
    rep = mt.adopt(EP, source=lt.device, actor="b", fork=True)
    assert rep["from"]["fork"] is True
    assert sorted(p.state for p in mt.presence(EP)) == ["active", "active"]


def test_tampered_snapshot_and_existing_workspace_are_refused(pair):
    legion, mac, folder = pair
    create_from_local(legion, folder, actor="a")
    lt, mt = SharedTree(legion), SharedTree(mac)
    handoff = lt.handoff(EP, to=mt.device, actor="a")
    manifest = lt.episode_dir(lt.device, EP) / "snapshots" / handoff["snapshot"] / "manifest" / "manifest.json"
    original = manifest.read_bytes()
    manifest.write_bytes(original.replace(b"Caretaker", b"Caretaken"))
    with pytest.raises(SharedError, match="changed since publication"):
        mt.adopt(EP, source=lt.device, actor="b")
    manifest.write_bytes(original)
    create_from_local(mac, folder, actor="b", allow_parallel=True)
    with pytest.raises(SharedError, match="never overwritten"):
        mt.adopt(EP, source=lt.device, actor="b")


def test_media_inventory_reports_missing_and_different(pair):
    legion, mac, folder = pair
    create_from_local(legion, folder, actor="a")
    ws = EpisodeWorkspace(legion, EP)
    wav = legion.root("audio", EP) / "P2-efm.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(b"RIFF" + b"x" * 5000)
    master = legion.root("prores", EP) / "master.mov"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"m" * 3000)
    with ws.store.transaction(by="t", reason="media") as m:
        m.set_artifact("ld_native", root="audio", name="P2-efm.wav")
        m.set_artifact("stack_master", root="prores", name="master.mov")
    lt, mt = SharedTree(legion), SharedTree(mac)
    lt.handoff(EP, to=mt.device, actor="a")
    copied = mac.root("audio", EP) / "P2-efm.wav"          # the operator copied the WAV, not the master
    copied.parent.mkdir(parents=True)
    copied.write_bytes(wav.read_bytes())
    rep = mt.adopt(EP, source=lt.device, actor="b")
    status = {r["artifact"]: r["status"] for r in rep["media"]}
    assert status == {"ld_native": "present", "stack_master": "missing"}
    assert json.loads((mac.episode_state_dir(EP) / "adopted.json").read_text())["media"] == rep["media"]


def test_unreachable_share_never_blocks_local_work(tmp_path):
    prof = machine(tmp_path, "FLEX", tmp_path / "offline-share", tmp_path)
    folder = _local_folder(tmp_path / "caps")
    store = create_from_local(prof, folder, actor="a")         # share offline: created anyway
    assert store.exists()
    with pytest.raises(SharedError, match="not reachable"):
        SharedTree(prof)


def test_never_writes_into_a_v38_release_tree_or_device_folder(tmp_path):
    release = tmp_path / "ld-pipeline"
    (release / "devices").mkdir(parents=True)
    (release / "WORKFLOW_VERSION").write_text("38\n")
    prof = machine(tmp_path, "LENOVO-LEGION-5", release, tmp_path)
    with pytest.raises(SharedError, match="v38 release tree"):
        SharedTree(prof)
    share = tmp_path / "share"
    pinned = Profile.from_dict({**machine(tmp_path, "LEGION2", share, tmp_path).to_dict(), "shared_root": str(share),
                                "instance_id": "D02C1DA9"}, path=tmp_path / "LEGION2" / "cfg" / "machine.json")
    v38_folder = share / "devices" / "LEGION2-D02C1DA9"
    (v38_folder / "episodes" / "S01E20").mkdir(parents=True)
    with pytest.raises(ConfigError, match="without an fstk device marker"):
        SharedTree(pinned)
    assert sorted(p.name for p in v38_folder.iterdir()) == ["episodes"]


def test_doctor_reports_shared_state_read_only(pair):
    from fstk.tools.doctor import run_doctor

    legion, mac, _ = pair
    SharedTree(mac)
    checks = {c["name"]: c for c in run_doctor(legion)["checks"]}
    assert "not joined yet" in checks["shared"]["detail"] and "MAC-M5-" in checks["shared"]["detail"]
    assert not legion.path.with_name("device.json").exists()          # doctor created nothing
    assert checks["volume:flex-e-hdd"]["status"] == "PASS"


def test_cli_share_commands(pair, capsys):
    from fstk.cli.main import main

    legion, mac, folder = pair
    legion.save()
    mac.save()
    create_from_local(legion, folder, actor="a")
    assert main(["--profile", str(mac.path), "share", "device"]) == 0
    mac_device = SharedTree(mac).device
    assert main(["--profile", str(legion.path), "share", "handoff", "VOY:S01E01", "--to", mac_device,
                 "--note", "over to the Mac"]) == 0
    assert main(["--profile", str(mac.path), "share", "status"]) == 0
    out = capsys.readouterr().out
    assert "VOY:S01E01" in out and "handed-off" in out and f"→ {mac_device}" in out
    assert main(["--profile", str(mac.path), "share", "adopt", "VOY:S01E01", "--from",
                 SharedTree(legion).device]) == 0
    assert "adopted VOY:S01E01" in capsys.readouterr().out
    assert main(["--profile", str(legion.path), "share", "reclaim", "VOY:S01E01"]) == 2
    assert "already adopted" in capsys.readouterr().err
