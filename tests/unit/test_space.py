from __future__ import annotations

import json
from collections import namedtuple

import pytest

from fstk.core.errors import SpaceError
from fstk.workspace.space import GIB, SCHEMA, SpaceLedger

Usage = namedtuple("Usage", "total used free")


def ledger(tmp_path, free_gib=100.0, reserve=40.0):
    return SpaceLedger(tmp_path / "ledger.json", reserve_gib=reserve,
                       disk_usage=lambda p: Usage(0, 0, int(free_gib * GIB)))


def test_check_passes_and_fails_on_projected_reserve(tmp_path):
    led = ledger(tmp_path)
    assert led.check(tmp_path, 60).ok
    fail = led.check(tmp_path, 61)
    assert not fail.ok and "SPACE FAIL" in fail.describe()


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_estimate_must_be_positive_and_finite(tmp_path, bad):
    with pytest.raises(SpaceError, match="measured estimate"):
        ledger(tmp_path).check(tmp_path, bad)


def test_reserve_cannot_be_zero(tmp_path):
    with pytest.raises(SpaceError):
        SpaceLedger(tmp_path / "l.json", reserve_gib=0)


def test_parallel_reservations_count_against_each_other(tmp_path):
    led = ledger(tmp_path)
    with led.reserve(tmp_path, 35, label="stack-render"):
        assert led.check(tmp_path, 25).ok
        assert not led.check(tmp_path, 26).ok  # 100 - 35 - 26 < 40
        with pytest.raises(SpaceError, match="never lowered"):
            with led.reserve(tmp_path, 30, label="efm-decode"):
                pass
    assert led.reservations() == []
    assert led.check(tmp_path, 60).ok


def test_dead_owner_reservations_are_pruned(tmp_path):
    led = ledger(tmp_path)
    import socket

    (tmp_path / "ledger.json").write_text(json.dumps({"schema": SCHEMA, "reservations": [
        {"id": "x", "volume": "v", "bytes": 10 * GIB, "pid": 999999999, "host": socket.gethostname()}]}))
    assert led.reservations() == []


def test_nonexistent_target_uses_nearest_existing_parent(tmp_path):
    led = ledger(tmp_path)
    check = led.check(tmp_path / "not" / "yet" / "created", 1)
    assert check.ok and check.target == tmp_path


def test_corrupt_ledger_fails_closed(tmp_path):
    (tmp_path / "ledger.json").write_text("{not json")
    with pytest.raises(SpaceError, match="unreadable"):
        ledger(tmp_path).check(tmp_path, 1)


def test_ledger_lives_beside_the_profile_and_migrates_once(tmp_path):
    from fstk.config.profile import SCHEMA as PROFILE_SCHEMA
    from fstk.config.profile import Profile
    from fstk.workspace.space import LEDGER_NAME
    data = tmp_path / "data"
    data.mkdir()
    (data / LEDGER_NAME).write_text('{"old": true}')
    prof = Profile.from_dict({"schema": PROFILE_SCHEMA, "machine": "T", "os": "linux",
                              "data_root": str(data), "media_root": str(tmp_path / "media")},
                             path=tmp_path / "cfg" / "profile.json")
    led = SpaceLedger.for_profile(prof)
    assert led.path == tmp_path / "cfg" / LEDGER_NAME
    assert led.path.read_text() == '{"old": true}' and not (data / LEDGER_NAME).exists()
    # A profile without a file keeps the data_root fallback.
    bare = Profile.from_dict({"schema": PROFILE_SCHEMA, "machine": "T", "os": "linux",
                              "data_root": str(data), "media_root": str(tmp_path / "media")})
    assert SpaceLedger.for_profile(bare).path == data / LEDGER_NAME
