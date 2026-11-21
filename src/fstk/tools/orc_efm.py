"""Orc/EFMSink EFM audio decoding: project builder, log/report parsing, WAV checks, selection.

Ported from v38 ``efm_decode.py`` (project builder, 7-Zip member selection),
``verify_efm_capture.py`` (log path-binding checks, quality summary),
``efm_report_table.py`` (comparison table parser), ``decode_efm_captures._check_audio_wav``
and ``workflow_app._unique_pareto_efm_capture``. Rules they encode:

* the decode project always names the EFM sidecar explicitly (two nodes: tbc_source → EFMSink);
* Orc's exit code is not trusted: the log must show that *this* TBC, *this* metadata file,
  *this* WAV and *this* report were the ones used, and that EFMSink reported success;
* the WAV must be native 44.1 kHz / 16-bit / stereo PCM with at least one frame;
* a capture is chosen automatically only when it is no worse on every error-correction
  metric and strictly better on at least one (unique Pareto winner); otherwise a human picks.

One unification (finding 0003): v38 had two report parsers that read the missing-sections
count from different labels ("Sections missing" vs "Missing (interpolated)"). Here a single
parser accepts either and records which label it found; the Pareto rule still fails closed
if any metric is absent.
"""
from __future__ import annotations

import json
import math
import os
import re
import wave
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from fstk.core.errors import ArtifactError

VIDEO_FORMATS = {"NTSC", "PAL", "PAL-M"}
SOURCE_FORMATS = {"Composite", "YC"}
AMPLITUDE_UNITS = {"IRE", "mV", "10bit"}

EFM_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


# --------------------------------------------------------------------------- project
def project_formats(text: str) -> tuple[str, str, str]:
    """Read video/source format and amplitude unit from an Orc project's ``project:`` block."""
    video = re.search(r"(?m)^  video_format:\s*([A-Za-z0-9-]+)\s*$", text)
    source = re.search(r"(?m)^  source_format:\s*([A-Za-z0-9-]+)\s*$", text)
    amplitude = re.search(r"(?m)^  amplitude_unit:\s*([A-Za-z0-9]+)\s*$", text)
    video_format = video.group(1).upper() if video else ""
    source_format = {"composite": "Composite", "yc": "YC"}.get(source.group(1).casefold() if source else "", "")
    amplitude_unit = amplitude.group(1) if amplitude else ""
    if (video_format not in VIDEO_FORMATS or source_format not in SOURCE_FORMATS
            or amplitude_unit not in AMPLITUDE_UNITS):
        raise ArtifactError("Orc project must declare a supported video/source format; found "
                            f"{video_format!r}/{source_format!r}/{amplitude_unit!r}")
    return video_format, source_format, amplitude_unit


def _yaml_string(value: str | Path) -> str:
    """JSON quoted strings are valid YAML double-quoted scalars."""
    return json.dumps(str(value), ensure_ascii=False)


def build_decode_project(*, episode: str, capture: str, tbc: Path, efm: Path, wav: Path,
                         video_format: str, source_format: str, amplitude_unit: str) -> str:
    """Two-node Orc v2 project (verbatim v38 layout); the EFM sidecar path is always explicit."""
    if (video_format not in VIDEO_FORMATS or source_format not in SOURCE_FORMATS
            or amplitude_unit not in AMPLITUDE_UNITS):
        raise ArtifactError("unsupported Orc source format")
    if not EFM_LABEL_RE.fullmatch(capture):
        raise ArtifactError(f"unsafe capture label: {capture!r}")
    name = f"EFM audio decode {episode.upper()} {capture}"
    return (
        "# ORC Project File\n"
        "# Version: 2.0\n\n"
        "project:\n"
        f"  name: {_yaml_string(name)}\n"
        "  version: 2.0\n"
        f"  video_format: {video_format}\n"
        f"  source_format: {source_format}\n"
        f"  amplitude_unit: {amplitude_unit}\n"
        "dag:\n"
        "  nodes:\n"
        "    - id: 1\n"
        "      stage: tbc_source\n"
        "      node_type: SOURCE\n"
        "      display_name: TBC Source\n"
        f"      user_label: {_yaml_string('TBC Source ' + capture)}\n"
        "      x: 0\n"
        "      y: 0\n"
        "      parameters:\n"
        "        input_path:\n"
        "          type: string\n"
        f"          value: {_yaml_string(tbc)}\n"
        "        efm_path:\n"
        "          type: string\n"
        f"          value: {_yaml_string(efm)}\n"
        "    - id: 2\n"
        "      stage: EFMSink\n"
        "      node_type: SINK\n"
        "      display_name: EFM Decoder Sink\n"
        f"      user_label: {_yaml_string('EFM Decoder Sink ' + capture)}\n"
        "      x: 200\n"
        "      y: 0\n"
        "      parameters:\n"
        "        output_path:\n"
        "          type: string\n"
        f"          value: {_yaml_string(wav)}\n"
        "        decode_mode:\n"
        "          type: string\n"
        "          value: audio\n"
        "        report:\n"
        "          type: bool\n"
        "          value: true\n"
        "  edges:\n"
        "    - from: 1\n"
        "      to: 2\n"
    )


def report_path_for(wav: Path) -> Path:
    """EFMSink writes its decoding report next to the WAV as ``<wav>.txt``."""
    return Path(str(wav) + ".txt")


# --------------------------------------------------------------------------- 7-Zip
def sevenzip_efm_member(listing: str, expected_name: str) -> tuple[str, int]:
    """Select exactly one regular ``.efm`` member with the expected name from ``7z l -slt``."""
    entries: list[dict[str, str]] = []
    for block in re.split(r"(?m)^-{5,}\s*$", listing):
        row: dict[str, str] = {}
        for line in block.splitlines():
            if " = " in line:
                key, value = line.split(" = ", 1)
                row[key.strip()] = value.strip()
        if row.get("Path") and row.get("Size", "").isdigit() and row.get("Folder") != "+":
            entries.append(row)
    wanted = expected_name.casefold()
    matches = []
    for row in entries:
        member = row["Path"].replace("\\", "/")
        parts = Path(member).parts
        attributes = row.get("Attributes", "").casefold()
        if row.get("Symbolic Link") or row.get("Hard Link") or row.get("Reparse Point") or "l" in attributes:
            continue
        if (member.casefold().endswith(".efm") and Path(member).name.casefold() == wanted
                and not Path(member).is_absolute() and not PureWindowsPath(member).drive
                and all(part not in ("", ".", "..") for part in parts)):
            matches.append((member, int(row["Size"])))
    if len(matches) != 1:
        raise ArtifactError(f"7z archive must contain exactly one regular {expected_name!r} member; "
                            f"found {len(matches)}")
    member, size = matches[0]
    if size <= 0:
        raise ArtifactError("7z EFM member has an invalid uncompressed size")
    return member, size


# --------------------------------------------------------------------------- log binding
TBC_JSON_RE = re.compile(r"using legacy JSON metadata:\s*(.+?\.tbc\.json)\s*$", re.IGNORECASE)
TBC_LOADED_RE = re.compile(r"tbc_source:\s*Loaded\s+'(.+?\.tbc)'", re.IGNORECASE)
EFM_OUTPUT_RE = re.compile(r"EFMSink:\s*mode=audio,\s*output=(.+?)\s*$", re.IGNORECASE)
EFM_REPORT_RE = re.compile(r"(?:Writing decoding report to:|Report file\s*:\s*)(.+?)\s*$", re.IGNORECASE)
EFM_SUCCESS_RE = re.compile(r"EFMSink:\s*Success:\s*EFM decoded to\s+(.+?)\s*$", re.IGNORECASE)


def decode_text(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def _logged(text: str, pattern: re.Pattern[str]) -> list[str]:
    out = []
    for line in text.splitlines():
        match = pattern.search(line.strip())
        if match:
            out.append(match.group(1).strip().strip("\"'"))
    return out


def _norm_path_text(p: str) -> str:
    """Windows-insensitive comparison form: forward slashes, case-folded, '.'/'..' collapsed."""
    text = p.replace("\\", "/")
    unc = text.startswith("//")
    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == ".." and parts:
            parts.pop()
            continue
        parts.append(part)
    return ("//" if unc else ("/" if text.startswith("/") else "")) + "/".join(parts).casefold()


def _same_path(logged: str, expected) -> bool:
    """Logged path == expected path, textually (Windows rules) or after resolving on this OS."""
    if _norm_path_text(logged) == _norm_path_text(str(expected)):
        return True
    if isinstance(expected, Path):
        try:
            return os.path.normcase(str(Path(logged).resolve(strict=False))) == \
                os.path.normcase(str(expected.resolve(strict=False)))
        except OSError:
            return False
    return False


def verify_efm_log(text: str, *, tbc: Path, metadata: Path, wav: Path, report: Path) -> dict:
    """Prove the Orc log belongs to this decode (v38 ``verify_efm_log``). Raises on mismatch."""
    found = {
        "loaded_tbc": _logged(text, TBC_LOADED_RE),
        "tbc_metadata": _logged(text, TBC_JSON_RE),
        "efm_output": _logged(text, EFM_OUTPUT_RE),
        "efm_report": _logged(text, EFM_REPORT_RE),
        "efm_success": _logged(text, EFM_SUCCESS_RE),
    }
    expected = {"loaded_tbc": tbc, "tbc_metadata": metadata, "efm_output": wav,
                "efm_report": report, "efm_success": wav}
    checks = {key: bool(found[key]) and all(_same_path(p, expected[key]) for p in found[key]) for key in found}
    if not all(checks.values()):
        failed = ", ".join(k for k, ok in checks.items() if not ok)
        raise ArtifactError(f"EFMSink log provenance mismatch ({failed}); the log may belong to another "
                            f"episode/capture: expected { {k: str(v) for k, v in expected.items()} }, "
                            f"logged {found}")
    return checks


# --------------------------------------------------------------------------- report parsing
PCT = re.compile(r"(?P<n>[\d,]+)\s*\((?P<p>[\d.]+)\s*%\)")
PLAIN = re.compile(r"(?P<n>[\d,]+)")
MISSING_LABELS = ("Sections missing", "Missing (interpolated)", "Missing sections")

#: Metrics compared for automatic capture selection (lower is better for every one).
PARETO_METRICS = ("post_c2_data_loss_pct", "c2_uncorrectable_pct", "c1_uncorrectable_pct",
                  "missing_sections", "concealed_samples", "muted_samples")


def _field(body: str, label: str) -> str | None:
    m = re.search(rf"^\s*{re.escape(label)}\s*:\s*(.+?)\s*$", body, re.MULTILINE)
    return m.group(1) if m else None


def _num(text: str | None) -> int | None:
    if text is None:
        return None
    m = PLAIN.fullmatch(text.strip())
    return int(m.group("n").replace(",", "")) if m else None


def _pair(text: str | None) -> tuple[int | None, float | None]:
    if not text:
        return None, None
    m = PCT.search(text)
    if m:
        return int(m.group("n").replace(",", "")), float(m.group("p"))
    n = _num(text)
    return (n, None) if n is not None else (None, None)


def parse_efm_report(text: str) -> dict:
    """Every field the v38 runbook compares, plus the six Pareto metrics, from one EFMSink report."""
    out: dict = {}
    for key, label in (("assessment", "Overall assessment"), ("disc_duration", "Disc duration"),
                       ("tracks_recovered", "Tracks recovered"), ("q_timecodes", "Q-channel timecodes"),
                       ("samples_decoded", "Samples decoded")):
        out[key] = _field(text, label)
    if out["assessment"]:
        out["assessment"] = out["assessment"].strip().upper()
    m = re.search(r"Disc duration\s*:\s*\S+\s*\(([\d,]+) sections\)", text)
    out["sections"] = int(m.group(1).replace(",", "")) if m else None
    if out["disc_duration"]:
        out["disc_duration"] = out["disc_duration"].split("(")[0].strip()

    prog = re.search(r"Audio integrity \(programme area\)(.*?)(?:Audio integrity \(outside|PART [A-Z]|\Z)",
                     text, re.DOTALL)
    body = prog.group(1) if prog else text
    out["concealed_samples"], out["concealed_pct"] = _pair(_field(body, "Concealed (interp)"))
    out["muted_samples"], out["muted_pct"] = _pair(_field(body, "Silenced (muted)"))

    # Part A's post-C2 loss is the runbook's metric (Part D prints a differently rounded copy).
    m = re.search(r"Data loss \(post-C2\)\s*:\s*([\d.]+)\s*%", text)
    out["post_c2_data_loss_pct"] = float(m.group(1)) if m else None
    out["c1_uncorrectable"], out["c1_uncorrectable_pct"] = _pair(_field(text, "C1 uncorrectable")
                                                                 or _field(text, "C1 unc"))
    out["c2_uncorrectable"], out["c2_uncorrectable_pct"] = _pair(_field(text, "C2 uncorrectable")
                                                                 or _field(text, "C2 unc"))
    out["missing_sections"] = None
    out["missing_sections_label"] = None
    for label in MISSING_LABELS:   # Part A "Sections missing : 19   (reconstructed by interpolation)"
        raw = _field(text, label)
        lead = re.match(r"\s*([\d,]+)\b", raw) if raw else None
        value = int(lead.group(1).replace(",", "")) if lead else None
        if value is not None:
            out["missing_sections"], out["missing_sections_label"] = value, label
            break
    m = re.search(r"Sections out-of-order\s*/\s*uncorrectable\s*:\s*([\d,]+)\s*/\s*([\d,]+)", text)
    if m:
        out["out_of_order"] = int(m.group(1).replace(",", ""))
        out["sections_uncorrectable"] = int(m.group(2).replace(",", ""))
    else:
        out["out_of_order"] = _num(_field(text, "Out of order") or _field(text, "Sections out-of-order"))
        out["sections_uncorrectable"] = None
    out["timeline_resyncs"] = _num(_field(text, "Timeline resyncs") or _field(text, "Timeline resync"))
    out["padding"] = _num(_field(text, "Padding"))
    times = re.findall(r"\b(\d{2}:\d{2}:\d{2})\b", text)
    out["q_span"] = f"{times[0]} -> {times[-1]}" if times else None
    return out


TABLE_COLUMNS = (("capture", "capture"), ("assessment", "assessment"), ("disc_duration", "disc dur"),
                 ("sections", "sections"), ("post_c2_data_loss_pct", "post-C2%"),
                 ("concealed_samples", "concealed"), ("muted_samples", "muted"),
                 ("c1_uncorrectable", "C1 unc"), ("c2_uncorrectable", "C2 unc"),
                 ("missing_sections", "missing"), ("out_of_order", "ooo"),
                 ("q_timecodes", "Q timecodes"), ("samples_decoded", "samples"))


def render_table(rows: list[dict]) -> str:
    """Plain-text comparison table (v38 ``efm_report_table.render``)."""
    table = []
    for r in rows:
        cells = []
        for key, _ in TABLE_COLUMNS:
            value = r.get(key)
            if key == "q_timecodes" and isinstance(value, str):
                if "valid" in value.casefold() and "contiguous" in value.casefold():
                    value = "valid+contiguous"
                else:
                    q = re.fullmatch(r"([\d,]+)\s*\(([\d.]+)\s*%\)", value.strip())
                    if q:
                        value = f"{q.group(1)} ({q.group(2).rstrip('0').rstrip('.')}%)"
            cells.append(str(value if value is not None else "-")[:16])
        table.append(cells)
    head = [c[1] for c in TABLE_COLUMNS]
    widths = [max(len(h), *(len(t[i]) for t in table)) if table else len(h) for i, h in enumerate(head)]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(head)), "  ".join("-" * w for w in widths)]
    lines += ["  ".join(t[i].ljust(widths[i]) for i in range(len(head))) for t in table]
    return "\n".join(lines)


# --------------------------------------------------------------------------- WAV
def check_audio_wav(path: Path) -> dict:
    """EFMSink output must be native 44.1 kHz / 16-bit / stereo PCM."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 44:
        raise ArtifactError(f"EFMSink WAV is missing, unsafe, or empty: {path}")
    try:
        with wave.open(str(path), "rb") as src:
            info = {"frames": src.getnframes(), "channels": src.getnchannels(),
                    "sample_rate": src.getframerate(), "sample_width_bytes": src.getsampwidth(),
                    "compression": src.getcomptype()}
    except (OSError, wave.Error, EOFError) as exc:
        raise ArtifactError(f"EFMSink output is not a readable PCM WAV: {path}: {exc}") from exc
    if (info["frames"] <= 0 or info["channels"] != 2 or info["sample_rate"] != 44100
            or info["sample_width_bytes"] != 2 or info["compression"] != "NONE"):
        raise ArtifactError(f"EFMSink output is not native 44.1 kHz/16-bit/stereo PCM: {info}")
    info["duration_s"] = info["frames"] / info["sample_rate"]
    return info


# --------------------------------------------------------------------------- selection
@dataclass(frozen=True)
class AutoSelection:
    capture: str
    note: str


def unique_pareto(quality: dict[str, dict]) -> AutoSelection | None:
    """v38 rule: pick only a capture no worse on every metric and strictly better on one.

    Returns None (a human must choose) if any metric is missing/non-finite for any capture or
    no unique dominant capture exists.
    """
    vectors: dict[str, tuple[float, ...]] = {}
    for label, q in quality.items():
        values = []
        for name in PARETO_METRICS:
            v = q.get(name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                return None
            values.append(float(v))
        vectors[label] = tuple(values)
    if not vectors:
        return None
    if len(vectors) == 1:
        return AutoSelection(next(iter(vectors)), "Automatically selected the only available verified EFM "
                             "capture; no alternative decoded capture exists for comparison.")

    def dominates(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
        return all(x <= y for x, y in zip(a, b, strict=True)) and any(x < y for x, y in zip(a, b, strict=True))

    winners = [label for label, v in vectors.items()
               if all(other == label or dominates(v, ov) for other, ov in vectors.items())]
    if len(winners) != 1:
        return None
    return AutoSelection(winners[0], "Automatically selected the unique Pareto-dominant verified EFM report: "
                         "no worse on post-C2 loss, C2/C1 uncorrectable rates, missing sections, concealed "
                         "samples, or muted samples; strictly better on at least one metric.")


PAIR_RE = re.compile(r"\s*(\d+)\s*\+\s*(-?\d+)\s*")


def parse_alignment_map(value: str, labels: list[str]) -> dict[str, int]:
    """``'1+0, 2+5, ...'`` in Orc input order → prepended-pad frames per capture (v38)."""
    parts = [item.strip() for item in str(value).split(",") if item.strip()]
    if len(parts) != len(labels):
        raise ArtifactError("alignment_map must contain exactly one entry for each Orc source")
    offsets: dict[str, int] = {}
    for expected_index, (part, label) in enumerate(zip(parts, labels, strict=True), start=1):
        match = PAIR_RE.fullmatch(part)
        if not match or int(match.group(1)) != expected_index:
            raise ArtifactError("alignment_map source indices do not match the Orc project order")
        offset = int(match.group(2))
        if offset < 0:
            raise ArtifactError(f"alignment_map has a negative prepended-pad count for {label}")
        offsets[label] = offset
    return offsets
