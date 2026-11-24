

def test_json_content_scope_ignores_time_stamps_only(tmp_path):
    import json

    from fstk.core.hashing import SCOPE_JSON, Fingerprint

    p = tmp_path / "r.json"
    p.write_text(json.dumps({"created_utc": "2026-11-24T03:54:22.040654+00:00", "a": 1,
                             "rows": [{"utc": "x", "b": 2}]}))
    fp = Fingerprint.of(p, scope=SCOPE_JSON)
    p.write_text(json.dumps({"rows": [{"b": 2, "utc": "yy"}], "a": 1, "created_utc": "2026-11-24T04:30:00+00:00"},
                            indent=1))
    assert fp.matches()                                     # new stamps, other key order and layout: same content
    p.write_text(json.dumps({"created_utc": "z", "a": 2, "rows": [{"utc": "x", "b": 2}]}))
    assert not fp.matches()
    p.write_text("not json")
    assert Fingerprint.of(p, scope=SCOPE_JSON).matches() and not fp.matches()
