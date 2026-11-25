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
    first_off = 30 - info["front"]                      # DVD index − LD index for the opening
    assert len(res.acts) == 2, [(a.start_frame, a.values) for a in res.acts]
    one, two = res.acts
    assert np.allclose(one.values, first_off, atol=0.6) and np.allclose(two.values, first_off - 25, atol=0.6)
    assert len(res.breaks) == 1 and abs(res.breaks[0]["jump_frames"] + 25) < 1
    # the LD-only front matter is unmatched, and the origin is measured, not assumed
    assert not res.matched[0]
    assert abs(res.origin_master_frame - info["front"]) <= PARAMS["step"]
    assert res.consistency >= 0.98 and res.reverse_agreement >= 0.95
    assert res.decision == "AUTO-ACCEPTED", res.reasons
    path = al.path_array(res)
    assert al.offset_at(path, info["front"] + 10) == first_off
    assert al.offset_at(path, len(ld) - 100) == first_off - 25


def test_speed_difference_is_followed_not_flagged():
    """LD repeats one frame in every 500 (2000 ppm slower transfer): the offset drifts by a frame
    every 500 LD frames. A constant-offset model would see a staircase of short segments; the act
    curve follows it and every frame stays consistent."""
    rng = np.random.default_rng(11)
    dvd = programme(rng, lengths=(150, 120, 180, 140, 160, 130, 170, 150, 140, 160, 150, 130))
    keep = np.repeat(np.arange(len(dvd)), np.where(np.arange(len(dvd)) % 500 == 250, 2, 1))
    ld = dvd[keep]
    res = al.measure(ld, dvd, params={**PARAMS, "min_offset": -100, "max_offset": 100})
    assert len(res.acts) == 1, res.breaks
    assert abs(res.acts[0].slope * 1e6 + 2000) < 600
    assert res.decision == "AUTO-ACCEPTED", res.reasons


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


def test_real_s01e20_samples_give_the_act_structure(fixtures):
    """Forward-path samples from S01E20's real descriptors (owner-approved v38 alignment)."""
    z = np.load(fixtures / "s01e20-alignment-samples.npz")
    res = al.AlignmentResult(z["idxs"], z["offsets"], z["sub"].astype(float), z["matched"], z["ncc"], z["margin"],
                             z["flat"], dict(al.DEFAULTS))
    al.fit_acts(res)
    assert res.origin_master_frame == 1515                 # v38 anchor: delta 50.5505 s
    assert abs(res.common_slope * 1e6 + 988) < 15          # ≈ 1000:1001
    big = [(round(b["before_s"]), round(b["jump_frames"])) for b in res.breaks if abs(b["jump_frames"]) >= 4]
    assert [t for t, _ in big] == [283, 664, 971, 1455, 1803]
    assert [j for _, j in big] == [5, 6, -4, 15, 15]


def test_ld_only_cards_that_loosely_match_random_dvd_pictures_are_not_the_programme_start():
    """Flex 2026-11-24, S02E02: the warning cards and "Feature Presentation" (4–40 s) each matched some DVD
    picture loosely, the path hopped across the DVD (offsets +566 → +54, RMS 52 frames), that became "act 1",
    and the programme origin — the head trim — was put at 4.0 s instead of 49.5 s."""
    rng = np.random.default_rng(11)
    prog = programme(rng)
    dvd = np.vstack([black(30), prog])
    cards = []
    for k in range(8):           # 8 still cards of 50 frames, each loosely like some unrelated DVD picture nearby
        src = dvd[50 * k + int(rng.integers(40, 160))].astype(float)
        card = 0.6 * (src - 128) + rng.normal(0, 6, (50, al.FEATURES))
        cards.append(np.clip(128 + card, 0, 255).astype(np.uint8))
    lead = np.vstack(cards)
    ld = np.vstack([lead, black(20), prog, black(40)])
    start = len(lead) + 20
    raw = al.align(ld, dvd, params=PARAMS)
    al.fit_acts(raw)
    assert raw.acts and raw.acts[0].start_frame < len(lead), "the test must reproduce the junk first act"
    assert raw.acts[0].residual_rms > al.MODEL["edge_act_max_rms"]
    res = al.measure(ld, dvd, params=PARAMS)
    # the DVD's opening pictures, found as one run on the LD, fix the programme start to the frame
    assert res.opening["found"] and res.opening["dvd_frame"] == 30, res.opening
    assert res.origin_master_frame == start, (res.origin_master_frame, start)
    assert res.ld_only and res.ld_only[0]["where"] == "start" and res.ld_only[0]["end_s"] <= start / al.FPS
    assert np.allclose(res.acts[0].values, 30 - start, atol=0.6) and res.acts[0].start_frame >= start
    assert not any("search edge" in r or "opening" in r for r in res.reasons), res.reasons
    # without the opening search the edge rule alone still keeps the junk out of the programme
    raw2 = al.align(ld, dvd, params=PARAMS)
    al.fit_acts(raw2)
    al.drop_unmatched_edges(raw2)
    assert abs(raw2.origin_master_frame - start) <= PARAMS["step"]


def test_a_dvd_that_opens_with_its_own_logo_is_still_found_after_the_black_gap():
    rng = np.random.default_rng(3)
    prog = programme(rng)
    logo = scene(90, np.random.default_rng(42))                  # DVD-only logo, then black, then the programme
    dvd = np.vstack([black(10), logo, black(25), prog])
    lead = scene(300, np.random.default_rng(77))                  # LD-only front matter
    ld = np.vstack([lead, black(30), prog, black(40)])
    op = al.find_opening(ld, dvd)
    assert op["found"] and op["dvd_frame"] == 125 and op["master_frame"] == 330, op
    assert len(op["tried"]) == 2 and not op["tried"][0]["ncc"] >= al.OPENING["min_ncc"]


def test_an_opening_that_cannot_be_found_sends_the_alignment_to_review(episode):
    ld, dvd, info = episode
    other = programme(np.random.default_rng(1234))
    res = al.measure(other[:len(ld)], dvd, params=PARAMS)           # a different episode's LD
    assert not res.opening["found"] and res.decision == "NEEDS-REVIEW"
    assert any("opening pictures were not found" in r for r in res.reasons)
