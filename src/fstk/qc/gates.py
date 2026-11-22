"""Delivery QC gate logic (pure), ported from v38 ``qc_report.py`` and ``scan_padfixed_black.py``.

Statuses: PASS · WARN · FAIL · ERROR · SKIP. A SKIP counts as *unverified* (a failure for the
delivery decision) unless the gate is genuinely not applicable to this deliverable (no DVD → no
absolute A/V; no 5.1 → no between-track sync or retime gaps; no SRT → no subtitle check).
Gates that fstk has not ported yet report SKIP with ``not ported`` and are therefore unverified:
the report is still produced for review, but delivery cannot be approved on it.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fstk.core.errors import FstkError

F24 = 24000.0 / 1001.0
BLIP_MAX_S = 0.10          # one frame is 0.042 s; a little encoder slack
ALIGN_TOL_S = 1.25 / F24   # ±1.25 output frames
SEEK_WINDOW_S = 5.0
SEEK_TOLERANCE_S = 0.15
DURATION_TOLERANCE_S = 0.002

STATUSES = ("PASS", "WARN", "FAIL", "ERROR", "SKIP")
NOT_PORTED = "not ported to fstk yet; this gate is unverified"

STANDING_WARNINGS = [
    "validate artifacts, not exit codes: Orc refresh, blackdetect and curl have all returned non-zero "
    "while reporting success - check the produced report/log/frame count",
    "reference DVDs have non-monotonic PTS: never seek them with -ss; decode sequentially and index with showinfo",
    "ffmpeg select expressions: keep each expression to <=32 terms or extraction silently mis-selects",
    "never make A/B copies as hardlinks then overwrite them in place (writes through the inode)",
    "respect the machine profile's resource limits; never run two downloaders for one item",
    "measure every episode: never copy another episode's AudioSkip, DELTA, rate or trim",
    "DVD-free deliveries can verify the capture-origin/duration/PAD model and exact audio/video endpoint, "
    "but cannot claim an external absolute A/V cross-correlation",
    "retime output gaps below the verifier's position-resolution limit remain WARN/LIMIT, not a confident PASS",
    "if input-seek audio is truncated while output-seek control succeeds, remux Matroska with "
    "-max_interleave_delta 0; this prevents audio-less clusters caused by the default 10 s interleave flush",
]


@dataclass
class Gate:
    name: str
    status: str
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown QC status {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"gate": self.name, "status": self.status, "detail": self.detail, "metrics": self.metrics}


# --------------------------------------------------------------------------- A/V check chunks
AV_CHUNK_S = 15.0
AV_CHUNKS = 8


def av_check_chunks(duration_s: float, black: list[tuple[float, float, float]], *, n: int = AV_CHUNKS,
                    length: float = AV_CHUNK_S, margin: float = 20.0) -> list[dict]:
    """Chunks for the human A/V check: ``n`` windows of ``length`` s spread across the programme.

    One per equal-width bin, as close to the bin centre as possible while avoiding black (fades,
    act breaks), where lip sync cannot be judged. Short files get fewer, shorter chunks.
    """
    if duration_s <= 0:
        return []
    if duration_s <= length + 2:
        return [{"start_s": 0.0, "end_s": round(duration_s, 3), "t_s": round(duration_s / 2, 3)}]
    lo, hi = min(margin, duration_s * 0.1), duration_s - min(margin, duration_s * 0.1)
    n = max(1, min(n, int((hi - lo) // length)))
    width = (hi - lo) / n
    spans = [(float(a), float(b)) for a, b, *_ in black]

    def clear(a: float) -> bool:
        return all(b0 >= a + length or b1 <= a for b0, b1 in spans)

    out = []
    for i in range(n):
        b0, b1 = lo + i * width, lo + (i + 1) * width
        centre = (b0 + b1 - length) / 2
        best = None
        for step in range(int(width)):
            for cand in (centre - step, centre + step):
                if b0 <= cand <= b1 - min(length, width) and clear(cand):
                    best = cand
                    break
            if best is not None:
                break
        start = centre if best is None else best
        end = min(start + length, duration_s)
        out.append({"start_s": round(start, 3), "end_s": round(end, 3), "t_s": round((start + end) / 2, 3),
                    "black_free": best is not None})
    return out


# --------------------------------------------------------------------------- PAD black
def pad_black_hits(pad_frames: list[int], events: list[tuple[float, float, float]]) -> list[dict]:
    """Confirmed PAD positions that still carry a single-frame black (v38 rule)."""
    out = []
    for frame in sorted(set(pad_frames)):
        t = frame / F24
        hits = [(a, b, d) for a, b, d in events if d <= BLIP_MAX_S and abs(a - t) <= ALIGN_TOL_S]
        if hits:
            out.append({"frame": frame, "time_s": round(t, 3),
                        "hits": [{"start_s": round(a, 3), "end_s": round(b, 3), "duration_s": d} for a, b, d in hits]})
    return out


def read_frame_list(path: Path) -> list[int]:
    return sorted({int(x) for x in path.read_text(encoding="utf-8").split() if x.strip()})


# --------------------------------------------------------------------------- stream inventory
def stream_inventory(streams: list[dict], fmt: dict, tracks: list[str], duration_s: float) -> Gate:
    vid = [s for s in streams if s.get("codec_type") == "video"]
    aud = [s for s in streams if s.get("codec_type") == "audio"]
    sub = [s for s in streams if s.get("codec_type") == "subtitle"]
    want_a = 2 if "51" in tracks else 1
    want_s = 1 if "srt" in tracks else 0
    problems = []

    def disp(s: dict) -> dict:
        return s.get("disposition") or {}

    def tags(s: dict) -> dict:
        return s.get("tags") or {}

    if len(vid) != 1:
        problems.append(f"video streams={len(vid)}")
    else:
        v = vid[0]
        if v.get("profile") != "Main 10":
            problems.append(f"profile={v.get('profile')}")
        if v.get("pix_fmt") != "yuv420p10le":
            problems.append(f"pix_fmt={v.get('pix_fmt')}")
        if v.get("r_frame_rate") not in ("24000/1001", "23.976"):
            problems.append(f"rate={v.get('r_frame_rate')}")
        if (v.get("width"), v.get("height")) != (768, 484):
            problems.append(f"raster={v.get('width')}x{v.get('height')}")
        if disp(v).get("default") != 1:
            problems.append("video not default")
        if "LaserDisc" not in tags(v).get("title", ""):
            problems.append("video title missing LaserDisc provenance")
    if len(aud) != want_a:
        problems.append(f"audio streams={len(aud)} want {want_a}")
    else:
        a0 = aud[0]
        if (a0.get("codec_name") != "flac" or a0.get("channels") != 2 or str(a0.get("sample_rate")) != "44100"
                or str(a0.get("bits_per_raw_sample")) != "16"):
            problems.append("LD track is not 44.1 kHz/16-bit FLAC stereo")
        if disp(a0).get("default") != 1:
            problems.append("LD is not the default track")
        if tags(a0).get("language") != "eng":
            problems.append("LD missing language=eng")
        if tags(a0).get("title") != "LD 2.0 (original Dolby Surround)":
            problems.append("LD track title is missing or unexpected")
        if want_a == 2:
            a1 = aud[1]
            if (a1.get("codec_name") != "flac" or a1.get("channels") != 6 or str(a1.get("sample_rate")) != "48000"
                    or str(a1.get("bits_per_raw_sample")) != "24"):
                problems.append(f"5.1 track wrong: {a1.get('codec_name')} {a1.get('channels')}ch/"
                                f"{a1.get('sample_rate')}Hz/{a1.get('bits_per_raw_sample')}bit")
            if disp(a1).get("default") != 0:
                problems.append("5.1 must be non-default")
            if tags(a1).get("language") != "eng":
                problems.append("5.1 missing language=eng")
            if tags(a1).get("title") != "DVD 5.1 (retimed)":
                problems.append("5.1 track title is missing or unexpected")
    if len(sub) != want_s:
        problems.append(f"subtitle streams={len(sub)} want {want_s}")
    elif sub:
        s0 = sub[0]
        if s0.get("codec_name") != "subrip":
            problems.append(f"subtitle codec={s0.get('codec_name')} (wanted SubRip)")
        if disp(s0).get("default") != 0:
            problems.append("subtitles must be non-default")
        if tags(s0).get("language") != "eng":
            problems.append("subtitle missing language=eng")
        if tags(s0).get("title") != "English SDH (retimed)":
            problems.append("subtitle title is missing or unexpected")
    dur = float(fmt.get("duration") or 0.0)
    if abs(dur - duration_s) > DURATION_TOLERANCE_S:
        problems.append(f"container duration {dur} != manifest {duration_s}")
    summary = {"video": len(vid), "audio": len(aud), "subtitle": len(sub), "duration_s": dur,
               "size_bytes": int(fmt.get("size") or 0)}
    if problems:
        return Gate("stream inventory", "FAIL", "; ".join(problems), summary)
    return Gate("stream inventory", "PASS",
                f"1 video Main10 768x484 23.976, {len(aud)} audio (LD default), {len(sub)} subtitle, "
                f"{dur:.6f}s, {fmt.get('size')} bytes", summary)


# --------------------------------------------------------------------------- seekability
def seek_points(duration_s: float, window_s: float = SEEK_WINDOW_S) -> list[float] | None:
    """Centre a full window inside each timeline quarter (v38); None if the file is too short."""
    quarter = duration_s / 4.0
    if quarter < window_s:
        return None
    return [i * quarter + (quarter - window_s) / 2.0 for i in range(4)]


def seekability(duration_s: float, audio_count: int,
                decode: Callable[[int, float, float, bool], float],
                *, window_s: float = SEEK_WINDOW_S, tolerance_s: float = SEEK_TOLERANCE_S) -> Gate:
    """Input-seek and output-seek control decodes at four points for every audio track."""
    name = "audio seekability"
    if audio_count < 1:
        return Gate(name, "FAIL", "deliverable contains no audio streams to seek-test")
    points = seek_points(duration_s, window_s)
    if points is None:
        return Gate(name, "ERROR", f"duration {duration_s:.3f}s is too short for a {window_s:.1f}s window "
                                   "inside each timeline quarter")
    samples, failures = [], []
    for stream in range(audio_count):
        for at in points:
            try:
                got_in = decode(stream, at, window_s, True)
            except FstkError as exc:
                return Gate(name, "ERROR", f"{exc}. If this is Matroska, keep the suspect file, remux with "
                                           "-max_interleave_delta 0, and rerun QC")
            try:
                got_out = decode(stream, at, window_s, False)
            except FstkError as exc:
                return Gate(name, "ERROR", str(exc))
            samples.append({"stream": stream, "at_s": round(at, 1), "input_s": got_in, "control_s": got_out})
            if abs(got_in - window_s) > tolerance_s:
                failures.append(f"a:{stream}@{at:.1f}s input seek decoded {got_in:.3f}s (wanted {window_s:.3f}s)")
            if abs(got_out - window_s) > tolerance_s:
                failures.append(f"a:{stream}@{at:.1f}s output-seek control decoded {got_out:.3f}s "
                                f"(wanted {window_s:.3f}s)")
    if failures:
        detail = "; ".join(failures)
        if any("input seek" in f for f in failures):
            detail += (". Likely Matroska interleave/index defect: re-remux with -max_interleave_delta 0, "
                       "then rerun QC")
        return Gate(name, "FAIL", detail, {"samples": samples})
    return Gate(name, "PASS", f"{audio_count} audio track(s), 4 points each; input and output seeks decoded "
                              f"{window_s:.1f}s windows (±{tolerance_s:.2f}s)", {"samples": samples})


# --------------------------------------------------------------------------- subtitles
def parse_srt(text: str) -> tuple[int, list[float], list[float], bool, bool]:
    items = re.findall(r"(?m)^\s*(\d+)\s*\n(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", text)

    def sec(t: str) -> float:
        h, m, rest = t.split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    starts = [sec(a) for _, a, _ in items]
    ends = [sec(b) for _, _, b in items]
    seq = all(int(items[i][0]) == i + 1 for i in range(len(items)))
    mono = all(starts[i] <= starts[i + 1] for i in range(len(starts) - 1)) if len(starts) > 1 else True
    return len(items), starts, ends, seq, mono


def subtitle_timeline(srt_text: str | None, duration_s: float, recorded_cues: int | None) -> Gate:
    name = "subtitle timeline"
    if srt_text is None:
        return Gate(name, "FAIL", "retimed SRT file not found; cannot verify the requested subtitle track")
    n, starts, ends, seq, mono = parse_srt(srt_text)
    if not starts:
        return Gate(name, "FAIL", "retimed SRT has no cues")
    in_range = min(starts) >= 0 and max(ends) <= duration_s + 1e-6
    detail = f"{n} cues, monotonic={mono}, sequential={seq}, {min(starts):.3f}..{max(ends):.3f}s"
    if recorded_cues is not None and n != recorded_cues:
        return Gate(name, "FAIL", detail + f"; manifest says {recorded_cues}")
    if not (mono and seq and in_range):
        return Gate(name, "FAIL", detail + f", in_range={in_range}")
    return Gate(name, "PASS", detail, {"cues": n})


# --------------------------------------------------------------------------- retime gaps
def retime_gap_status(report: dict | None, has_fiveone: bool) -> tuple[str, str]:
    """Sub-resolution/legacy gap evidence stays visibly non-PASS (v38 verbatim)."""
    if not has_fiveone:
        return "SKIP", "no retimed DVD 5.1 track in this deliverable"
    if report is None:
        return "WARN", "retime output-gap verification report is missing; gap positions are unverified"
    status = report.get("status")
    gaps = report.get("gaps")
    if not isinstance(gaps, list):
        return "ERROR", "retime output-gap verification report has no gap-results array"
    if status == "PASS" and all(row.get("status") == "PASS" for row in gaps):
        return "PASS", f"{len(gaps)} inserted gaps independently matched output silence"
    if status == "LIMIT" or any(row.get("status") == "LIMIT" for row in gaps):
        limited = sum(row.get("status") == "LIMIT" for row in gaps)
        return "WARN", f"{limited} sub-resolution gap(s) remain LIMIT/unverified; see verification report"
    if status == "FAIL" or any(row.get("status") == "FAIL" for row in gaps):
        failed = sum(row.get("status") == "FAIL" for row in gaps)
        return "FAIL", f"{failed} retime output gap(s) disagree with the plan"
    return "ERROR", f"unsupported retime output-gap verification status: {status!r}"


# --------------------------------------------------------------------------- summary
def allowed_skips(*, dvd_available: bool | None, tracks: list[str]) -> set[str]:
    allowed = set()
    if dvd_available is False:
        allowed.add("absolute A/V")
    if "51" not in tracks:
        allowed.update(("between-track sync", "retime output gaps"))
    if "srt" not in tracks:
        allowed.add("subtitle timeline")
    return allowed


def summarize(gates: list[Gate], allowed: set[str]) -> dict[str, Any]:
    unverified = [g.name for g in gates if g.status == "SKIP" and g.name not in allowed]
    failed = [g.name for g in gates if g.status in ("FAIL", "ERROR")]
    return {"pass": sum(g.status == "PASS" for g in gates), "warn": sum(g.status == "WARN" for g in gates),
            "skip": sum(g.status == "SKIP" for g in gates), "fail": len(failed) + len(unverified),
            "failed_gates": failed, "unverified_gates": unverified}


def pad_review_intervals(hits: list[dict]) -> list[dict]:
    out = []
    for h in hits:
        for e in h["hits"]:
            out.append({"id": f"pad-black-f{h['frame']}", "gate": "full-decode + PAD black",
                        "start_s": max(0.0, e["start_s"] - 2.0), "end_s": e["end_s"] + 2.0,
                        "reason": "detected PAD-aligned single-frame black",
                        "metrics": {"frame": h["frame"], **e}})
    return out


def retime_review_intervals(report: dict | None) -> list[dict]:
    out = []
    for row in (report or {}).get("gaps", []) or []:
        if row.get("status") not in ("FAIL", "LIMIT"):
            continue
        try:
            start, end = float(row.get("expected_start_s")), float(row.get("expected_end_s"))
        except (TypeError, ValueError):
            continue
        out.append({"id": f"retime-gap-{row.get('label', 'unknown')}", "gate": "retime output gaps",
                    "start_s": start, "end_s": end, "reason": "retimed audio gap verification needs review",
                    "metrics": row})
    return out


def render_markdown(title: str, deliverable: str, gates: list[Gate], summary: dict[str, Any]) -> str:
    lines = [f"# {title} - delivery QC", "", f"- deliverable: `{deliverable}`", "",
             "| gate | status | detail |", "|---|---|---|"]
    lines += [f"| {g.name} | {g.status} | {g.detail.replace('|', '/')} |" for g in gates]
    lines += ["", f"**{summary['pass']} pass, {summary['warn']} warn, {summary['skip']} skip, "
                  f"{summary['fail']} fail/unverified**", "", "## Standing warnings", ""]
    lines += [f"- {w}" for w in STANDING_WARNINGS]
    return "\n".join(lines) + "\n"
