"""DVD↔LD picture alignment (port of v38 measure_dvd_alignment.py, plus automation)."""
from __future__ import annotations

import numpy as np
import pytest

from fstk.media import alignment as al

RNG = np.random.default_rng(20)


def scene(n: int, rng) -> np.ndarray:
    base = rng.normal(0, 1, (36 + 8, 48 + 8))
    k = np.ones(5) / 5                     # smooth so neighbouring pans stay similar
    base = np.apply_along_axis(lambda r: np.convolve(r, k, "same"), 1, base)
    base = np.apply_along_axis(lambda c: np.convolve(c, k, "same"), 0, base)
    frames = []
    for t in range(n):
        dx, dy = (t // 6) % 8, (t // 11) % 8
        img = base[dy:dy + 36, dx:dx + 48] + rng.normal(0, 0.03, (36, 48))
        frames.append(np.clip(128 + 60 * img, 0, 255))
    return np.array(frames, np.uint8).reshape(n, -1)


def black(n: int) -> np.ndarray:
    return np.full((n, al.FEATURES), 16, np.uint8)


def programme(rng, lengths=(140, 90, 200, 160, 120, 180, 150, 110, 170, 130, 190, 100, 160, 140)):
    return np.vstack([scene(n, rng) for n in lengths])


@pytest.fixture(scope="module")
def episode():
    """DVD = 30 black + programme. LD = 400 frames of LD-only front matter + programme with a
    25-frame LD-only insert at an act break (black on both sides), so the offset steps by −25."""
    rng = np.random.default_rng(7)
    prog = programme(rng)
    cut = 1000
    dvd = np.vstack([black(30), prog[:cut], black(20), prog[cut:]])
    front = programme(np.random.default_rng(99), lengths=(150, 130, 120))
    insert = scene(25, np.random.default_rng(5))
    ld = np.vstack([front, black(20), prog[:cut], black(20), insert, prog[cut:], black(40)])
    return ld, dvd, {"front": len(front) + 20, "cut": cut}


PARAMS = {"min_offset": -600, "max_offset": 200, "step": 5}


def test_l1_max_matches_brute_force():
    for _ in range(20):
        prev = RNG.normal(size=57)
        lam = float(RNG.uniform(0, 0.5))
        best, arg = al._l1_max(prev, lam)
        i = np.arange(57)
        brute = prev[None, :] - lam * np.abs(i[:, None] - i[None, :])
        assert np.allclose(best, brute.max(axis=1))
        assert np.allclose(brute[i, arg], brute.max(axis=1))


def test_viterbi_without_unmatched_state_equals_v38_matrix_form():
    em = RNG.uniform(-1, 1, (300, 41)).astype(np.float32)
    offs = np.arange(41)
    penalty = 0.05 * np.abs(offs[:, None] - offs[None, :])
    scores = em[0].astype(np.float64)
    back = np.zeros(em.shape, int)
    for r in range(1, len(em)):                         # v38 measure_dvd_alignment.py
        prev = scores[None, :] - penalty
        back[r] = np.argmax(prev, axis=1)
        scores = em[r] + prev[np.arange(41), back[r]]
    v38 = np.empty(len(em), int)
    v38[-1] = int(np.argmax(scores))
    for r in range(len(em) - 2, -1, -1):
        v38[r] = back[r + 1, v38[r + 1]]
    ours = al.viterbi(em, penalty=0.05, unmatched_score=None, unmatched_switch=0)
    assert (ours == v38).all()


def test_recovers_origin_offsets_and_break_automatically(episode):
    ld, dvd, info = episode
    res = al.measure(ld, dvd, params=PARAMS)
    matched = [s for s in res.segments if s.offset is not None]
    first_off = 30 - info["front"]                      # DVD index − LD index for the opening
    assert matched[0].offset == first_off
    assert matched[-1].offset == first_off - 25
    assert {s.offset for s in matched} <= {first_off, first_off - 25}
    # the LD-only front matter is labelled unmatched, and the origin is measured, not assumed
    assert res.segments[0].offset is None
    assert abs(res.origin_master_frame - info["front"]) <= PARAMS["step"]
    assert all(s.dense_within1 >= 0.95 for s in matched if s.dense_frames)
    assert res.reverse_agreement >= 0.95
    assert res.decision == "AUTO-ACCEPTED", res.reasons
    path = al.path_array(res)
    assert al.offset_at(path, info["front"] + 10) == first_off
    assert al.offset_at(path, len(ld) - 100) == first_off - 25


def test_wrong_dvd_content_is_held_for_review(episode):
    ld, dvd, _ = episode
    bad = dvd.copy()
    bad[600:900] = programme(np.random.default_rng(3), lengths=(100, 100, 100))   # a different scene
    res = al.measure(ld, bad, params=PARAMS)
    assert res.decision == "NEEDS-REVIEW" and res.reasons


def test_search_edge_is_never_auto_accepted(episode):
    ld, dvd, info = episode
    first_off = 30 - info["front"]
    res = al.measure(ld, dvd, params={**PARAMS, "min_offset": first_off})
    assert res.decision == "NEEDS-REVIEW"
    assert any("search edge" in r or "matched" in r for r in res.reasons)
