"""Two simulated machines (a Windows PC and a Mac) sharing one shared_root: presence, handoff, adoption."""
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
                              "volumes": {"media-hdd": str(volume_mount)}, "limits": {"min_free_gb": 0.001}},
                             path=base / "cfg" / "machine.json")
    return prof


@pytest.fixture
def pair(tmp_path):
    shared = tmp_path / "share"
    shared.mkdir()
    media_disk = tmp_path / "media-hdd"          # the same disk, mounted differently on each machine
    media_disk.mkdir()
    (tmp_path / "mac-mounts").mkdir()
    (tmp_path / "mac-mounts" / "media").symlink_to(media_disk)
    pc_a = machine(tmp_path, "PC-A", shared, media_disk)
    mac = machine(tmp_path, "MAC", shared, tmp_path / "mac-mounts" / "media")
    folder = _local_folder(media_disk)
    return pc_a, mac, folder


def test_device_identity_is_stable_and_never_shared(tmp_path, pair):
    pc_a, mac, _ = pair
    a, b = load_or_create(pc_a), load_or_create(pc_a)
    assert a == b and a.name.startswith("PC-A-") and len(a.instance_id) == 8
    shutil.copy(pc_a.path.with_name("device.json"), mac.path.with_name("device.json"))
    with pytest.raises(ConfigError, match="belongs to machine"):
        load_or_create(mac)
    pinned = Profile.from_dict({**pc_a.to_dict(), "instance_id": "0BADF00D"}, path=pc_a.path)
    with pytest.raises(ConfigError, match="conflicts"):
        load_or_create(pinned)


def test_portable_paths_round_trip_across_os_spellings():
    win = PathMap(True, {"data_root": r"C:\Users\user\fstk", "volume:media-hdd": r"\\FILESERVER\media",
                         "media_root": r"D:\media", "root:stack": r"E:\stack"})
    mac = PathMap(False, {"data_root": "/Users/user/fstk", "volume:media-hdd": "/Volumes/media",
                          "media_root": "/Volumes/fast/media"})
    doc = {"a": r"\\FILESERVER\media\DS9-S01E20-LaserDisc-Copies\x-L1.tbc", "b": r"D:\media\ds9-s01e20\audio\L1.wav",
           "c": r"E:\stack\DS9\S01E20\master.mov", "d": "plain words C:\\not a whole path? no", "e": [r"C:\elsewhere\x"]}
    left: set[str] = set()
    enc = encode(doc, win, leftovers=left)
    assert enc["a"] == "{volume:media-hdd}/DS9-S01E20-LaserDisc-Copies/x-L1.tbc"
    assert enc["c"] == "{root:stack}/DS9/S01E20/master.mov" and left == {r"C:\elsewhere\x"}
    out = decode(enc, mac)
    assert out["a"] == "/Volumes/media/DS9-S01E20-LaserDisc-Copies/x-L1.tbc"
    assert out["b"] == "/Volumes/fast/media/ds9-s01e20/audio/L1.wav"
    assert out["c"] == "{root:stack}/DS9/S01E20/master.mov" and mac.unmapped == {"root:stack"}
    assert out["d"] == doc["d"]


def test_presence_handoff_adopt_and_freeze(pair):
    pc_a, mac, folder = pair
    create_from_local(pc_a, folder, actor="op@pc-a")
    lt, mt = SharedTree(pc_a), SharedTree(mac)
    (p,) = mt.presence(EP)
    assert p.device == lt.device and p.state == "active" and not p.mine
    with pytest.raises(SharedError, match="is active on"):
        create_from_local(mac, folder, actor="op@mac")
    # pc_a runs intake; the engine publishes after the run
    events = []
    state = Engine(pc_a, intake_plan(EP, download=False, job="inv", include_stack_reference=False,
                                       include_efm=False), actor="op@pc-a", events=events.append).run()
    assert state["steps"]["inventory"]["status"] == "passed"
    assert any(e["kind"] == "shared-publish" and "published" in e["outcome"] for e in events)
    ws = EpisodeWorkspace(pc_a, EP)
    report = pc_a.root("reports", EP) / "capture-inventory.json"
    ApprovalBook(ws.state_dir / "approvals").record(review="inventory", decision="APPROVED", actor="op",
                                                    note="captures look right", evidence=[report])
    status = lt.publish(EP)
    assert status["jobs"][0]["steps"]["inventory"] == "passed"
    # hand off to the Mac: pc_a is frozen
    with pytest.raises(SharedError, match="no device"):
        lt.handoff(EP, to="NOT-A-DEVICE-00000000", actor="op")
    handoff = lt.handoff(EP, to=mt.device, actor="op", note="Mac finishes the audio")
    assert handoff["to"] == mt.device and handoff["snapshot"]
    with pytest.raises(WorkflowError, match="handed off"):
        Engine(pc_a, intake_plan(EP, download=False, job="again", include_stack_reference=False,
                                   include_efm=False), actor="op").run()
    with pytest.raises(SharedError, match="has not published"):
        mt.adopt(EpisodeId.parse("DS9:S01E20"), source=lt.device, actor="op")
    # the Mac adopts: paths re-rooted onto its own mounts, approval still valid there
    rep = mt.adopt(EP, source=lt.device, actor="op@mac")
    assert rep["from"]["device"] == lt.device and rep["unmapped_tokens"] == []
    m = EpisodeWorkspace(mac, EP).store.load()
    assert m.doc["sources"]["downloads_override"] == str(mac.volumes["media-hdd"] + "/" + folder.name)
    assert m.capture_labels == ["L1", "P2"]
    mws = EpisodeWorkspace(mac, EP)
    assert mws.store.journal()[0]["origin"] == "shared-adopt"
    assert (mws.state_dir / "manifest" / f"journal.source-{lt.device}.jsonl").is_file()
    st = mws.approvals.status("inventory", manifest=m)
    assert st.state == "approved", st.reasons
    assert st.record["bindings"]["evidence"][0]["path"].startswith(str(mac.data_root))
    # the Mac can run work; PC-A cannot take it back now
    state = Engine(mac, intake_plan(EP, download=False, job="mac-inv", include_stack_reference=False,
                                    include_efm=False), actor="op@mac").run()
    assert state["steps"]["inventory"]["status"] == "passed"
    assert lt.adopted_by(EP) == [mt.device]
    with pytest.raises(SharedError, match="already adopted"):
        lt.reclaim(EP, actor="op")
    states = {p.device: p.state for p in lt.presence(EP)}
    assert states == {lt.device: "handed-off", mt.device: "active"}


def test_reclaim_before_adoption_and_fork(pair):
    pc_a, mac, folder = pair
    create_from_local(pc_a, folder, actor="a")
    lt, mt = SharedTree(pc_a), SharedTree(mac)
    lt.handoff(EP, to=mt.device, actor="a")
    lt.reclaim(EP, actor="a")
    assert {p.device: p.state for p in lt.presence(EP)}[lt.device] == "active"
    with pytest.raises(SharedError, match="has not handed"):
        mt.adopt(EP, source=lt.device, actor="b")
    rep = mt.adopt(EP, source=lt.device, actor="b", fork=True)
    assert rep["from"]["fork"] is True
    assert sorted(p.state for p in mt.presence(EP)) == ["active", "active"]


def test_tampered_snapshot_and_existing_workspace_are_refused(pair):
    pc_a, mac, folder = pair
    create_from_local(pc_a, folder, actor="a")
    lt, mt = SharedTree(pc_a), SharedTree(mac)
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
    pc_a, mac, folder = pair
    create_from_local(pc_a, folder, actor="a")
    ws = EpisodeWorkspace(pc_a, EP)
    wav = pc_a.root("audio", EP) / "P2-efm.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(b"RIFF" + b"x" * 5000)
    master = pc_a.root("prores", EP) / "master.mov"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"m" * 3000)
    with ws.store.transaction(by="t", reason="media") as m:
        m.set_artifact("ld_native", root="audio", name="P2-efm.wav")
        m.set_artifact("stack_master", root="prores", name="master.mov")
    lt, mt = SharedTree(pc_a), SharedTree(mac)
    lt.handoff(EP, to=mt.device, actor="a")
    copied = mac.root("audio", EP) / "P2-efm.wav"          # the operator copied the WAV, not the master
    copied.parent.mkdir(parents=True)
    copied.write_bytes(wav.read_bytes())
    rep = mt.adopt(EP, source=lt.device, actor="b")
    status = {r["artifact"]: r["status"] for r in rep["media"]}
    assert status == {"ld_native": "present", "stack_master": "missing"}
    assert json.loads((mac.episode_state_dir(EP) / "adopted.json").read_text())["media"] == rep["media"]


def test_unreachable_share_never_blocks_local_work(tmp_path):
    prof = machine(tmp_path, "PC-B", tmp_path / "offline-share", tmp_path)
    folder = _local_folder(tmp_path / "caps")
    store = create_from_local(prof, folder, actor="a")         # share offline: created anyway
    assert store.exists()
    with pytest.raises(SharedError, match="not reachable"):
        SharedTree(prof)


def test_never_writes_into_a_v38_release_tree_or_device_folder(tmp_path):
    release = tmp_path / "ld-pipeline"
    (release / "devices").mkdir(parents=True)
    (release / "WORKFLOW_VERSION").write_text("38\n")
    prof = machine(tmp_path, "PC-A", release, tmp_path)
    with pytest.raises(SharedError, match="v38 release tree"):
        SharedTree(prof)
    share = tmp_path / "share"
    pinned = Profile.from_dict({**machine(tmp_path, "PC2", share, tmp_path).to_dict(), "shared_root": str(share),
                                "instance_id": "0A1B2C3D"}, path=tmp_path / "PC2" / "cfg" / "machine.json")
    v38_folder = share / "devices" / "PC2-0A1B2C3D"
    (v38_folder / "episodes" / "S01E20").mkdir(parents=True)
    with pytest.raises(ConfigError, match="without an fstk device marker"):
        SharedTree(pinned)
    assert sorted(p.name for p in v38_folder.iterdir()) == ["episodes"]


def test_doctor_reports_shared_state_read_only(pair):
    from fstk.tools.doctor import run_doctor

    pc_a, mac, _ = pair
    SharedTree(mac)
    checks = {c["name"]: c for c in run_doctor(pc_a)["checks"]}
    assert "not joined yet" in checks["shared"]["detail"] and "MAC-" in checks["shared"]["detail"]
    assert not pc_a.path.with_name("device.json").exists()          # doctor created nothing
    assert checks["volume:media-hdd"]["status"] == "PASS"


def test_cli_share_commands(pair, capsys):
    from fstk.cli.main import main

    pc_a, mac, folder = pair
    pc_a.save()
    mac.save()
    create_from_local(pc_a, folder, actor="a")
    assert main(["--profile", str(mac.path), "share", "device"]) == 0
    mac_device = SharedTree(mac).device
    assert main(["--profile", str(pc_a.path), "share", "handoff", "VOY:S01E01", "--to", mac_device,
                 "--note", "over to the Mac"]) == 0
    assert main(["--profile", str(mac.path), "share", "status"]) == 0
    out = capsys.readouterr().out
    assert "VOY:S01E01" in out and "handed-off" in out and f"→ {mac_device}" in out
    assert main(["--profile", str(mac.path), "share", "adopt", "VOY:S01E01", "--from",
                 SharedTree(pc_a).device]) == 0
    assert "adopted VOY:S01E01" in capsys.readouterr().out
    assert main(["--profile", str(pc_a.path), "share", "reclaim", "VOY:S01E01"]) == 2
    assert "already adopted" in capsys.readouterr().err
