import numpy as np

from fstk.quality.frames import Planes, split_frames
from fstk.quality.measure import (
    checker_energy,
    chroma_noise,
    clipping_findings,
    compare_levels,
    dropouts,
    grain_power,
    grain_retention,
    level_stats,
)

rng = np.random.default_rng(3)


def grainy(h=96, w=128, std=3.0, level=120.0):
    return (level + rng.normal(0, std, (h, w))).astype(np.float32)


def test_split_frames_10bit_scale():
    w, h = 4, 2
    y = np.full((h, w), 400, "<u2"); u = np.full((h, w // 2), 512, "<u2"); v = np.full((h, w // 2), 600, "<u2")
    (p,) = split_frames(y.tobytes() + u.tobytes() + v.tobytes(), w, h, "yuv422p10le")
    assert p.y[0, 0] == 100 and p.u[0, 0] == 128 and p.v[0, 0] == 150


def test_levels_and_compare():
    ys = [np.clip(grainy(level=60), 0, 255) for _ in range(3)] + [np.full((96, 128), 235.0, np.float32)]
    a = level_stats(ys)
    b = level_stats([y + 8 for y in ys])
    rows = compare_levels(a, b)
    assert {r["check"]: r["status"] for r in rows}["black point"] == "FLAG"
    assert all(r["status"] == "PASS" for r in compare_levels(a, a))
    crushed = level_stats([np.zeros((50, 50), np.float32)])
    assert clipping_findings(crushed)[0]["status"] == "FLAG"


def test_grain_power_and_retention():
    y = grainy(std=4.0)
    g = grain_power(y)
    assert 3.0 < g["std"] < 5.0
    import cv2
    smoothed = cv2.GaussianBlur(y, (0, 0), 2.0)
    assert grain_retention(y, y)["ratio"] == 1.0
    assert grain_retention(y, smoothed)["ratio"] < 0.4


def test_chroma_noise_and_dot_crawl():
    y = grainy(std=1.0)
    u = (128 + rng.normal(0, 2, (96, 64))).astype(np.float32)
    v = (128 + rng.normal(0, 2, (96, 64))).astype(np.float32)
    n = chroma_noise(Planes(y, u, v))
    assert 1.0 < n["cb_std"] < 3.0
    yy, xx = np.mgrid[0:96, 0:128]
    crawl = y + 6 * np.where((yy + xx) % 2 == 0, 1, -1)
    assert checker_energy(crawl) > 5 * checker_energy(y)


def test_dropout_detected_only_when_neighbours_agree():
    base = np.full((96, 128), 100.0, np.float32)
    cur = base.copy(); cur[40, 30:60] = 235          # a 30-px bright streak
    found = dropouts(base, cur, base)
    assert len(found) == 1 and found[0].w >= 30 and found[0].peak > 100
    moving_next = base.copy(); moving_next[40, 30:60] = 20     # neighbours disagree: not a defect
    assert dropouts(base, cur, moving_next) == []
    grain_peak = base.copy(); grain_peak[10, 10] = 255          # single pixel: not a streak
    assert dropouts(base, grain_peak, base) == []


def test_geometry_crop_and_lock():
    from fractions import Fraction

    from fstk.quality.geometry import Crop, active_crop, check_colour_tags, lock_geometry, snap_dar

    ys = []
    for level in (60, 150, 200):
        y = np.full((100, 160), 16.0, np.float32)
        y[4:96, 10:150] = level + rng.normal(0, 3, (92, 140))
        ys.append(y)
    crop = active_crop(ys)
    assert crop == Crop(10, 4, 10, 4)
    assert snap_dar(Fraction(1331, 1000))[1] == "4:3"
    info = lock_geometry(master_w=160, master_h=100, master_sar=Fraction(1, 1), ld_crop=crop,
                         dvd_dar=Fraction(4, 3) * Fraction(997, 1000))
    assert info["dar"] == "4:3" and info["width"] == 140 and info["height"] == 92 and info["source"].startswith("dvd")
    num, den = (int(x) for x in info["sar"].split(":"))
    assert abs(Fraction(num * 140, den * 92) - Fraction(4, 3)) < Fraction(1, 500)
    rows = check_colour_tags({"color_primaries": "smpte170m", "color_transfer": "smpte170m",
                              "color_space": "bt709", "color_range": "tv"})
    assert [r["status"] for r in rows] == ["PASS", "PASS", "FAIL", "PASS"]
