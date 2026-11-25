"""DVD opening packs: the DVD's opening run as descriptors, found on an LD without the DVD (ADR 0006)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from fstk.media import alignment as al
from fstk.media import opening_pack as op

FEAT = al.FEATURES


def pictures(n: int, seed: int) -> np.ndarray:
    """Textured frames that change smoothly (a moving picture: neighbours correlate, seconds apart do not)."""
    rng = np.random.default_rng(seed)
    keys = rng.integers(0, 255, (n // 30 + 2, FEAT)).astype(np.float32)
    t = np.arange(n) / 30.0
    i = t.astype(int)
    w = (t - i)[:, None]
    return np.clip(keys[i] * (1 - w) + keys[i + 1] * w, 0, 255).astype(np.uint8)


def black(n: int) -> np.ndarray:
    return np.full((n, FEAT), 16, np.uint8)


def dvd(seed: int = 1, lead: int = 40, still_s: float = 0.0) -> np.ndarray:
    body = pictures(round(140 * al.FPS), seed)
    if still_s:
        n = round(still_s * al.FPS)
        noise = np.random.default_rng(seed + 50).integers(-2, 3, (n, FEAT))
        body[:n] = np.clip(body[0].astype(int) + noise, 0, 255).astype(np.uint8)
    return np.vstack((black(lead), body))


def ld_with(programme: np.ndarray, *, junk: int = 900, gap: int = 60, seed: int = 9) -> np.ndarray:
    """LD-only cards, black, then the programme from its first picture (LD noise added)."""
    noise = np.random.default_rng(seed).normal(0, 6, programme.shape)
    prog = np.clip(programme.astype(float) + noise, 0, 255).astype(np.uint8)
    return np.vstack((pictures(junk, seed + 100), black(gap), prog))


def make(desc: np.ndarray, code: str = "S01E20") -> op.OpeningPack:
    return op.make_pack(desc[:round(op.EXPORT_S * al.FPS)], series="DS9", code=code, title="t", ffmpeg="ffmpeg x",
                        source={"disc": "D1"}, check_against=desc)


def test_pack_holds_the_opening_run_and_round_trips(tmp_path):
    d = dvd()
    pack = make(d)
    assert pack.opening_dvd_frame == 40
    assert pack.seconds == pytest.approx(12.0, abs=0.02)
    assert len(pack.descriptors) == pack.meta["frames"] == round(12 * al.FPS) + round(op.SPARE_S * al.FPS)
    assert (pack.descriptors == d[40:40 + len(pack.descriptors)]).all()
    path = op.save_pack(pack, op.pack_path("ds9", "s01e20", tmp_path))
    assert path == tmp_path / "DS9" / "S01E20.npz"
    back = op.load_pack(path)
    assert (back.descriptors == pack.descriptors).all() and back.meta == json.loads(json.dumps(pack.meta))
    assert op.find_pack("DS9", "S01E20", tmp_path) is not None
    assert op.find_pack("DS9", "S01E21", tmp_path) is None


def test_a_nearly_still_opening_gets_a_longer_run():
    pack = make(dvd(still_s=16))
    assert pack.seconds > 12.1
    assert pack.meta["self_ncc_half_s"] <= al.OPENING["max_self_ncc"]


def test_pack_finds_the_programme_start_behind_ld_only_cards():
    d = dvd()
    pack = make(d)
    ld = ld_with(d[40:])
    r = op.find_start(ld, pack, search_s=120)
    assert r["found"], r
    assert r["master_frame"] == 960                       # 900 cards + 60 black
    assert r["dvd_frame"] == 40 and r["offset"] == 40 - 960
    assert r["pack"]["code"] == "S01E20" and r["ncc"] > 0.9


def test_another_episodes_pack_is_not_found():
    pack = make(dvd(seed=1))
    r = op.find_start(ld_with(dvd(seed=2)[40:]), pack, search_s=120)
    assert not r["found"] and r["tried"]


def test_load_rejects_a_damaged_pack(tmp_path):
    pack = make(dvd())
    pack.meta["frames"] += 1
    path = op.save_pack(pack, tmp_path / "x.npz")
    with pytest.raises(ValueError, match="not a fstk-dvd-opening-v1 pack"):
        op.load_pack(path)


def test_packaged_packs_load_and_match_the_index():
    index = json.loads((op.PACK_ROOT / "index.json").read_text(encoding="utf-8"))
    rows = index["series"]["DS9"]
    files = sorted(op.PACK_ROOT.glob("DS9/*.npz"))
    assert [r["code"] for r in rows] == [p.stem for p in files]
    for row, path in zip(rows, files, strict=True):
        pack = op.load_pack(path)
        assert pack.meta["code"] == row["code"] and pack.opening_dvd_frame == row["opening_dvd_frame"]
        assert pack.seconds == row["seconds"]
        # every pack stands out within its own DVD's first minutes
        assert pack.meta["distinctiveness"][str(pack.seconds)]["runner_up_ncc"] < al.OPENING["min_ncc"] - 0.15
    assert op.find_pack("DS9", "S01E20").opening_dvd_frame == 61


def test_find_opening_ignores_the_slope_of_a_long_first_shot():
    """A long first shot: sliding the run a second off itself still scores high, but that is the same peak."""
    d = dvd(still_s=10)
    r = al.find_opening(ld_with(d[40:]), d, search_s=120)
    assert r["found"] and r["master_frame"] == 960, r
