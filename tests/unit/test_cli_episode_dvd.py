from fstk.cli.main import find_dvd, main
from fstk.config.profile import SCHEMA, Profile
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.workflow.engine import EpisodeWorkspace

EP = EpisodeId.parse("DS9:S01E20")


def test_find_and_record_the_dvd(tmp_path):
    dvd_root = tmp_path / "Video"
    (dvd_root / "DS9S1D5").mkdir(parents=True)
    f = dvd_root / "DS9S1D5" / "DS9 S01E20 - In The Hands of the Prophets.mkv"
    f.write_bytes(b"x")
    (dvd_root / "DS9S1D5" / "Deep Space 9 - S01E17 - The Forsaken.mkv").write_bytes(b"x")
    assert find_dvd(dvd_root, "S01E20") == [f]
    prof = Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), "sources": {"dvd_root": str(dvd_root)}})
    pp = tmp_path / "p.json"
    prof.save(pp)
    EpisodeWorkspace(prof, EP).store.create(Manifest.new(EP, title="t"), by="t", reason="setup")
    assert main(["--profile", str(pp), "episode", "dvd", "DS9:S01E20", "--find"]) == 0
    m = EpisodeWorkspace(prof, EP).store.load()
    assert m.get("mode.dvd_reference_available") is True and m.artifact("dvd") == {"path": str(f)}
    assert main(["--profile", str(pp), "episode", "dvd", "DS9:S01E20", "--none"]) == 0
    assert EpisodeWorkspace(prof, EP).store.load().get("mode.dvd_reference_available") is False
