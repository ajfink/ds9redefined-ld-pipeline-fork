#!/usr/bin/env python3
"""Compact EFMSink report reader -> one comparison table per episode.

Decoding five ~16-18 KB EFMSink reports by hand is verbose and error-prone; this
extracts exactly the fields the runbook says to compare (post-C2 loss, concealment,
muting, C1/C2 uncorrectable, missing/out-of-order sections, Q-timecode continuity)
and prints one table.

REPORT-ONLY. It selects no capture and writes no manifest: the runbook requires the
selection to be a measured, reasoned decision (never "the label says POOR"), so this
only presents the evidence.

  python scripts/efm_report_table.py --episode S01E12
  python scripts/efm_report_table.py --dir Z:\\work\\audio\\S01E12

The reader is format-driven, not episode-driven, so it works on any episode's reports.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ldcommon import Ld, PipelineError, load_episode  # noqa: E402

# EFMSink writes "<value> (x.xx %)" and bare "<value>" variants; capture both.
PCT = re.compile(r"(?P<n>[\d,]+)\s*\((?P<p>[\d.]+)\s*%\)")
PLAIN = re.compile(r"(?P<n>[\d,]+)")


def _num(text: str) -> int | None:
    m = PLAIN.fullmatch(text.strip())
    return int(m.group("n").replace(",", "")) if m else None


def _pair(text: str) -> tuple[int | None, float | None]:
    """'1,572 (0.0077 %)' -> (1572, 0.0077)."""
    m = PCT.search(text)
    if m:
        return int(m.group("n").replace(",", "")), float(m.group("p"))
    n = _num(text)
    return (n, None) if n is not None else (None, None)


def _field(body: str, label: str) -> str | None:
    """Value after 'Label :' on a line (EFMSink's own layout)."""
    m = re.search(rf"^\s*{re.escape(label)}\s*:\s*(.+?)\s*$", body, re.MULTILINE)
    return m.group(1) if m else None


def _capture_label(path: Path, text: str) -> str:
    """Resolve labels from native EFMSink output names and archived device-log copies."""
    output_file = _field(text, "Output file")
    if output_file:
        basename = output_file.strip().replace("\\", "/").rsplit("/", 1)[-1]
        match = re.search(r"-(?P<label>[^/\\-]+)-efm\.wav$", basename, re.IGNORECASE)
        if match:
            return match.group("label")
    archived = re.search(r"-efm-report-(?P<label>[^.]+)\.txt$", path.name, re.IGNORECASE)
    if archived:
        return archived.group("label")
    native = re.search(r"(?P<prefix>.+)-efm\.wav\.txt$", path.name, re.IGNORECASE)
    if native:
        # Native names are commonly <capture>-efm.wav.txt or
        # <episode>-<capture>-efm.wav.txt. Capture labels are the final token.
        return native.group("prefix").rsplit("-", 1)[-1]
    return path.stem


def parse_report(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    out: dict = {"file": path.name, "capture": _capture_label(path, text)}

    for key, label in (
        ("assessment", "Overall assessment"),
        ("disc_duration", "Disc duration"),
        ("tracks_recovered", "Tracks recovered"),
        ("q_timecodes", "Q-channel timecodes"),
        ("samples_decoded", "Samples decoded"),
    ):
        out[key] = _field(text, label)

    # "Disc duration : 46:08:21   (207,542 sections)" -> keep only the time so the
    # table column does not bleed the sections parenthetical into it.
    if out.get("disc_duration"):
        out["disc_duration"] = out["disc_duration"].split("(")[0].strip()

    # Sections counter appears in "Disc duration : ... (N sections)"
    m = re.search(r"Disc duration\s*:\s*\S+\s*\(([\d,]+) sections\)", text)
    out["sections"] = int(m.group(1).replace(",", "")) if m else None

    # Audio integrity (programme area)
    prog = re.search(
        r"Audio integrity \(programme area\)(.*?)(?:Audio integrity \(outside|PART [A-Z]|\Z)",
        text, re.DOTALL)
    body = prog.group(1) if prog else text
    out["concealed"], out["concealed_pct"] = _pair(
        _field(body, "Concealed (interp)") or "")
    out["muted"], out["muted_pct"] = _pair(
        _field(body, "Silenced (muted)") or "")

    # Post-C2 / CIRC section: label text varies slightly across Orc builds, so match loosely.
    def loose(*labels: str) -> str | None:
        for lab in labels:
            v = _field(text, lab)
            if v is not None:
                return v
        return None

    # Part A's "Data loss (post-C2): 0.0064 %" is a bare percentage (no sample count).
    # Part D also prints a differently-rounded "Data loss: 0.007%" (developer detail) --
    # always take the Part A post-C2 value; it is the runbook's selection metric.
    m = re.search(r"Data loss \(post-C2\)\s*:\s*([\d.]+)\s*%", text)
    out["post_c2_pct"] = float(m.group(1)) if m else None
    out["post_c2"] = None  # no count is reported for post-C2

    out["c1_unc"], out["c1_unc_pct"] = _pair(loose(
        "C1 uncorrectable", "C1 unc") or "")
    out["c2_unc"], out["c2_unc_pct"] = _pair(loose(
        "C2 uncorrectable", "C2 unc") or "")

    # Section-level integrity (Part C) is authoritative for these four.
    out["missing"] = _num(loose(
        "Missing (interpolated)", "Missing sections", "Missing") or "")
    m = re.search(r"Sections out-of-order\s*/\s*uncorrectable\s*:\s*([\d,]+)\s*/\s*([\d,]+)",
                  text)
    if m:
        out["out_of_order"] = int(m.group(1).replace(",", ""))
        out["sections_uncorrectable"] = int(m.group(2).replace(",", ""))
    else:
        out["out_of_order"] = _num(loose("Out of order", "Sections out-of-order") or "")
        out["sections_uncorrectable"] = None

    for key, labels in (
        ("uncorrectable", ("Uncorrectable",)),
        ("timeline_resyncs", ("Timeline resyncs", "Timeline resync")),
        ("padding", ("Padding",)),
    ):
        v = loose(*labels)
        out[key] = _num(v) if v is not None else None

    # Q span: first/last Q timecodes reported anywhere in the file.
    times = re.findall(r"\b(\d{2}:\d{2}:\d{2})\b", text)
    out["q_span"] = f"{times[0]} -> {times[-1]}" if times else None

    return out


def collect(directory: Path) -> list[dict]:
    # Prefer the decoder's canonical <wav>.txt outputs when present; otherwise
    # accept the preserved per-device log copies named *-efm-report-<capture>.txt.
    files = sorted(directory.glob("*-efm.wav.txt"))
    if not files:
        files = sorted(directory.glob("*-efm-report-*.txt"))
    if not files:
        raise PipelineError(f"no EFMSink reports matching canonical or archived names in {directory}")
    return [parse_report(f) for f in files]


def render(rows: list[dict]) -> str:
    cols = [
        ("capture", "capture"), ("disc_duration", "disc dur"),
        ("sections", "sections"), ("post_c2_pct", "post-C2%"),
        ("concealed", "concealed"), ("muted", "muted"),
        ("c1_unc", "C1 unc"), ("c2_unc", "C2 unc"),
        ("missing", "missing"), ("out_of_order", "ooo"),
        ("q_timecodes", "Q timecodes"),
        ("samples_decoded", "samples"),
    ]
    table = []
    for r in rows:
        cells = []
        for key, _heading in cols:
            value = r.get(key)
            if key == "q_timecodes" and isinstance(value, str):
                if "valid" in value.casefold() and "contiguous" in value.casefold():
                    value = "valid+contiguous"
                else:
                    q_count = re.fullmatch(r"([\d,]+)\s*\(([\d.]+)\s*%\)", value.strip())
                    if q_count:
                        percent = q_count.group(2).rstrip("0").rstrip(".")
                        value = f"{q_count.group(1)} ({percent}%)"
            cells.append(str(value if value is not None else "-")[:16])
        table.append(cells)
    head = [c[1] for c in cols]
    widths = [max(len(h), *(len(t[i]) for t in table)) for i, h in enumerate(head)]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(head)),
             "  ".join("-" * w for w in widths)]
    for t in table:
        lines.append("  ".join(t[i].ljust(widths[i]) for i in range(len(head))))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", help="manifest code, e.g. S01E12")
    ap.add_argument("--dir", help="directory holding *-efm.wav.txt (overrides --episode)")
    args = ap.parse_args()

    if args.dir:
        directory = Path(args.dir)
    elif args.episode:
        ld = Ld()
        load_episode(args.episode, ld)
        directory = ld.root("audio") / args.episode
    else:
        raise PipelineError("pass --episode <code> or --dir <path>")

    rows = collect(directory)
    print(f"EFMSink reports in {directory}: {len(rows)}")
    print()
    print(render(rows))
    print()
    print("REPORT-ONLY: this table selects nothing. Choose audio.capture from measured")
    print("evidence (post-C2 loss first), never from the summary word POOR.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
