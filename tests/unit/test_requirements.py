from fstk.tools.requirements import Requirement, compare_versions, judge, parse_version, record_versions


def test_parse_version():
    assert parse_version("ffmpeg version n7.1-3-gabc") == (7, 1)
    assert parse_version("4.4.2-0ubuntu") == (4, 4, 2)
    assert parse_version("nothing") is None


def test_range_policy():
    r = Requirement("vsjetpack", "2.2", "2")
    assert judge(r, "2.2.7")[0] == "PASS"
    assert judge(r, "2.1.9")[0] == "FAIL"          # below the floor
    assert judge(r, "3.0.0")[0] == "WARN"          # newer than tested: warn, never fail
    assert judge(Requirement("ffmpeg", "4.4"), "8.0")[0] == "PASS"   # no ceiling known
    assert judge(Requirement("x", "1"), None)[0] == "FAIL"
    assert judge(Requirement("orc", pinned=True), None)[0] == "PASS"


def test_drift_and_history(tmp_path):
    prev = {"tools": {"orc": "a", "rife_model": "rife-v4.26"}, "packages": {"numpy": "2.0"}}
    cur = {"tools": {"orc": "b", "rife_model": "rife-v4.26"}, "packages": {"numpy": "2.0"}}
    assert compare_versions(prev, cur) == ["tool orc: 'a' -> 'b'"]
    assert compare_versions(None, cur) == []

    class P:
        def tool(self, name):
            raise KeyError(name)

    assert record_versions(P(), tmp_path) == []
    assert record_versions(P(), tmp_path) == []      # unchanged -> history not extended
    import json
    assert len(json.loads((tmp_path / "tool-versions.json").read_text())["history"]) == 1
