"""The per-episode quality report: one place that says how good the delivered episode is and where to look.

Pure functions over the JSON the quality and QC stages already wrote. Nothing here measures anything; it
collects, ranks and words. A finding is ``PASS``, ``NOTE`` (informational), ``FLAG`` (look at it) or ``FAIL``.
"""
from __future__ import annotations

from typing import Any

SCHEMA = "fstk-episode-quality-v1"
MAX_MOMENTS = 24


def _row(area: str, check: str, status: str, detail: str) -> dict[str, str]:
    return {"area": area, "check": check, "status": status, "detail": detail}


def _video_rows(v: dict) -> list[dict]:
    rows: list[dict] = []
    lv = v.get("levels", {})
    for f in lv.get("clipping", []):
        rows.append(_row("Levels", f["check"], f["status"], f"{f['value']} (limit {f['limit']})"))
    cmp_ = lv.get("compare")
    if cmp_ is not None:
        bad = [c for c in cmp_ if c.get("status") != "PASS"]
        rows.append(_row("Levels", "black/white/mid vs DVD reference", "FLAG" if bad else "PASS",
                         "; ".join(f"{c['check']}: {c.get('delta')}" for c in bad) if bad else "within tolerance"))
    g = v.get("geometry", {})
    if g:
        rows.append(_row("Geometry", "active area / aspect lock", "NOTE" if g.get("warnings") else "PASS",
                         f"crop {g.get('crop')}, SAR {g.get('sar')}, DAR {g.get('dar')}"
                         + (f"; {'; '.join(g['warnings'])}" if g.get("warnings") else "")))
    cd = v.get("chroma_decision", {})
    if cd:
        rows.append(_row("Chroma", "chroma noise", "NOTE" if cd.get("applied") else "PASS",
                         f"measured {cd.get('measured_chroma_std')} (trigger {cd.get('trigger')}); "
                         + ("denoise applied to chroma only, luma untouched" if cd.get("applied") else "no denoise")))
    if v.get("cross_colour_ratio") is not None:
        rows.append(_row("Chroma", "cross-colour / dot crawl ratio", "NOTE", str(v["cross_colour_ratio"])))
    dr = v.get("dropouts", {})
    if dr:
        resid = dr.get("residual_count", 0)
        rows.append(_row("Dropouts", "detected / concealed from LD neighbours / left", "FLAG" if resid else "PASS",
                         f"{dr.get('found', 0)} found, {dr.get('concealed', 0)} concealed, {resid} left as captured"))
    gr = v.get("grain", {})
    if gr:
        rows.append(_row("Grain", "grain texture (residual std, flat areas)", "NOTE",
                         ", ".join(f"{k} {val}" for k, val in gr.items())))
    pr = v.get("proof", {})
    if pr:
        issues = pr.get("issues", [])
        rows.append(_row("Grain", "finishing proof (luma/grain preserved outside fixes)", "FAIL" if issues else "PASS",
                         "; ".join(issues) if issues else "luma bit-identical outside fixes; grain retained"))
    h = v.get("hybrid", {})
    if h:
        if h.get("status") == "CONVERTED":
            detail = (f"{h['video_segments']} video-origin stretch(es), {h['video_seconds']} s at constant 23.976p "
                      f"({h.get('interpolated_segments', 0)} interpolated, rest nearest real field); "
                      f"{h.get('plate_segments', 0)} film plate(s) under an overlay kept as film")
        else:
            detail = h.get("reason", "all film cadence")
        rows.append(_row("Cadence", "video-origin stretches", "NOTE" if h.get("status") == "CONVERTED" else "PASS",
                         f"{h.get('status')}: {detail}"))
    return rows


def _audio_rows(a: dict) -> list[dict]:
    rows: list[dict] = []
    ld = a.get("ld", {})
    if ld:
        left = len(ld.get("left_as_captured", []))
        rows.append(_row("Audio", "LD clicks/gaps", "FLAG" if left else "PASS",
                         f"{ld.get('events_found', 0)} found, {ld.get('repaired', 0)} repaired by prediction, "
                         f"{left} left as captured"))
        lo = ld.get("loudness") or {}
        if lo:
            rows.append(_row("Audio", "loudness (R128, not normalised)", "NOTE",
                             f"{lo.get('integrated_lufs')} LUFS, LRA {lo.get('lra')}, true peak {lo.get('true_peak_dbtp')} dBTP"))
    for f in a.get("flags", []):
        rows.append(_row("Audio", f.get("check", "channel/phase"), "FLAG", str(f.get("detail", f))))
    f5 = a.get("fiveone", {})
    if f5:
        rows.append(_row("Audio", "DVD 5.1 path", "NOTE", str(f5.get("path", f5.get("codec", "recorded")))))
    sub = a.get("subtitles", {})
    if sub:
        off = sub.get("best_shift_s")
        rows.append(_row("Subtitles", "timing vs speech", "FLAG" if sub.get("status") == "FLAG" else "PASS",
                         f"best shift {off} s" if off is not None else str(sub.get("status", ""))))
    return rows


def _qc_rows(q: dict) -> list[dict]:
    rows = []
    s = q.get("summary", {})
    rows.append(_row("Delivery", "QC gates", "FAIL" if s.get("fail") else ("FLAG" if s.get("warn") else "PASS"),
                     ", ".join(f"{k} {s[k]}" for k in ("pass", "warn", "fail", "skip") if k in s)))
    for g in q.get("results", []):
        if g.get("status") in ("FAIL", "WARN"):
            rows.append(_row("Delivery", g.get("name", g.get("gate", "gate")), "FAIL" if g["status"] == "FAIL" else "FLAG",
                             str(g.get("detail", ""))))
    return rows


def _tag_rows(tags: list[dict]) -> list[dict]:
    return [_row(t.get("area", "Delivered stream"), t["check"], t["status"] if t["status"] in ("FAIL", "FLAG") else "PASS",
                 f"{t['value']} (expected {t['expected']})") for t in tags]


def pick_moments(video: dict | None, audio: dict | None, qc: dict | None, fixes: list[dict], *,
                 duration_s: float, frames: int, limit: int = MAX_MOMENTS) -> list[dict]:
    """The worst (and the most-worth-checking) moments, ranked, spread out so no two are within 2 s.

    Left-over dropouts and QC-flagged intervals rank first, then concealed dropouts (so the repair can be judged),
    then audio defects left as captured (shown at their picture).
    """
    spf = duration_s / frames if frames else 1001 / 24000
    cand: list[tuple[float, float, str]] = []
    for r in (video or {}).get("dropouts", {}).get("residual", []):
        cand.append((100.0, r["frame"] * spf, f"dropout left as captured ({r.get('reason', '')})".strip()))
    for iv in (qc or {}).get("review_intervals", []):
        cand.append((90.0, (iv["start_s"] + iv["end_s"]) / 2, f"QC: {iv.get('reason', iv.get('gate', ''))}"))
    for fx in fixes:
        cand.append((50.0, fx["frame"] * spf, "dropout concealed from LD neighbour frame"))
    for e in (audio or {}).get("ld", {}).get("left_as_captured", []):
        cand.append((40.0, float(e["time_s"]), f"audio {e['kind']} left as captured"))
    cand.sort(key=lambda c: (-c[0], c[1]))
    picked: list[dict] = []
    for score, t, why in cand:
        if 0 <= t <= duration_s and all(abs(t - p["time_s"]) >= 2.0 for p in picked):
            picked.append({"time_s": round(t, 3), "reason": why, "score": score})
        if len(picked) >= limit:
            break
    return sorted(picked, key=lambda p: p["time_s"])


def build(episode: str, title: str, *, video: dict | None, audio: dict | None, qc: dict | None,
          fixes: list[dict], duration_s: float, frames: int, tags: list[dict] | None = None,
          operator_fixes: list[dict] | None = None) -> dict[str, Any]:
    rows = (_video_rows(video) if video else [_row("Video", "video-quality", "FLAG", "stage has not run")])
    rows += (_audio_rows(audio) if audio else [_row("Audio", "audio-quality", "FLAG", "stage has not run")])
    rows += (_qc_rows(qc) if qc else [_row("Delivery", "QC gates", "FLAG", "qc-report has not run")])
    rows += _tag_rows(tags or [])
    for f in operator_fixes or []:
        src = {"dvd": "DVD frames (operator choice)", "interpolate": "interpolated from LD neighbours",
               "ld-patch": "region copied from the neighbouring LD frame", "leave": "left as captured"}.get(f["action"], f["action"])
        grain = (f"; grain {f['grain_ratio_vs_neighbour']} of the neighbour's" if "grain_ratio_vs_neighbour" in f else "")
        rows.append(_row("Operator fixes", f"frames {f['first']}..{f['last']}", "FLAG" if f.get("grain_flag") else "NOTE",
                         f"{src}{' in ' + 'x'.join(map(str, f['rect'][2:])) + ' area' if f.get('rect') else ''}{grain}: "
                         f"{f.get('note', '')}"))
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("PASS", "NOTE", "FLAG", "FAIL")}
    verdict = "FAIL" if counts["FAIL"] else ("REVIEW" if counts["FLAG"] else "CLEAN")
    return {"schema": SCHEMA, "episode": episode, "title": title, "verdict": verdict, "counts": counts,
            "rows": rows, "moments": pick_moments(video, audio, qc, fixes, duration_s=duration_s, frames=frames)}


def render_markdown(rep: dict) -> str:
    c = rep["counts"]
    lines = [f"# Quality report: {rep['episode']} {rep.get('title') or ''}".rstrip(), "",
             f"**Verdict: {rep['verdict']}**  (pass {c['PASS']}, note {c['NOTE']}, flag {c['FLAG']}, fail {c['FAIL']})", "",
             "| Area | Check | Status | Detail |", "|---|---|---|---|"]
    for r in rep["rows"]:
        lines.append(f"| {r['area']} | {r['check']} | {r['status']} | {str(r['detail']).replace('|', '/')} |")
    if rep["moments"]:
        lines += ["", "## Moments worth a look", ""]
        lines += [f"- {m['time_s']:.1f} s: {m['reason']}" for m in rep["moments"]]
    if rep.get("contact_sheet"):
        lines += ["", f"Contact sheet: {rep['contact_sheet']}"]
    return "\n".join(lines) + "\n"
