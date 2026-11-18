#!/usr/bin/env python3
"""Safe, testable helpers for constructing per-capture Orc EFM decode projects."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


VIDEO_FORMATS = {"NTSC", "PAL", "PAL-M"}
SOURCE_FORMATS = {"Composite", "YC"}
AMPLITUDE_UNITS = {"IRE", "mV", "10bit"}


def project_formats(path: Path) -> tuple[str, str, str]:
    """Read the already-validated episode Orc project's source format contract."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"cannot read stack Orc project to establish source formats: {exc}") from exc
    video = re.search(r"(?m)^  video_format:\s*([A-Za-z0-9-]+)\s*$", text)
    source = re.search(r"(?m)^  source_format:\s*([A-Za-z0-9-]+)\s*$", text)
    amplitude = re.search(r"(?m)^  amplitude_unit:\s*([A-Za-z0-9]+)\s*$", text)
    video_format = video.group(1).upper() if video else ""
    source_value = source.group(1).casefold() if source else ""
    source_format = {"composite": "Composite", "yc": "YC"}.get(source_value, "")
    amplitude_unit = amplitude.group(1) if amplitude else ""
    if (video_format not in VIDEO_FORMATS or source_format not in SOURCE_FORMATS or
            amplitude_unit not in AMPLITUDE_UNITS):
        raise ValueError(
            f"stack Orc project must declare a supported video/source format; "
            f"found {video_format!r}/{source_format!r}/{amplitude_unit!r}")
    return video_format, source_format, amplitude_unit


def _yaml_string(value: str | Path) -> str:
    """JSON quoted strings are valid YAML double-quoted scalars."""
    return json.dumps(str(value), ensure_ascii=False)


def build_decode_project(*, episode: str, capture: str, tbc: Path, efm: Path,
                         wav: Path, video_format: str, source_format: str,
                         amplitude_unit: str) -> str:
    """Create a two-node Orc v2 project; the EFM sidecar path is always explicit."""
    if (video_format not in VIDEO_FORMATS or source_format not in SOURCE_FORMATS or
            amplitude_unit not in AMPLITUDE_UNITS):
        raise ValueError("unsupported Orc source format")
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
        f"          value: {_yaml_string(tbc.resolve())}\n"
        "        efm_path:\n"
        "          type: string\n"
        f"          value: {_yaml_string(efm.resolve())}\n"
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
        f"          value: {_yaml_string(wav.resolve())}\n"
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


def sevenzip_efm_member(listing: str, expected_name: str) -> tuple[str, int]:
    """Select exactly one regular .efm member with the expected name from `7z l -slt`."""
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
        if (row.get("Symbolic Link") or row.get("Hard Link") or
                row.get("Reparse Point") or "l" in attributes):
            continue
        if (member.casefold().endswith(".efm") and
                Path(member).name.casefold() == wanted and
                not Path(member).is_absolute() and
                all(part not in ("", ".", "..") for part in parts)):
            matches.append((member, int(row["Size"])))
    if len(matches) != 1:
        raise ValueError(
            f"7z archive must contain exactly one regular {expected_name!r} member; "
            f"found {len(matches)}")
    member, size = matches[0]
    if size <= 0:
        raise ValueError("7z EFM member has an invalid uncompressed size")
    return member, size


def extract_7z_member(sevenzip: Path, archive: Path, member: str,
                      expected_size: int, output: Path) -> None:
    """Stream one validated archive member to a new file without extracting paths."""
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"refusing to overwrite extracted EFM temp file: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [str(sevenzip), "e", "-so", "-y", "-bso0", "-bsp0", "-bse2",
               str(archive), member]
    with output.open("xb") as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.PIPE,
                                check=False)
        stream.flush()
    if result.returncode:
        output.unlink(missing_ok=True)
        detail = result.stderr.decode("utf-8", "replace")[-800:]
        raise RuntimeError(f"7-Zip EFM extraction failed ({result.returncode}): {detail}")
    if output.stat().st_size != expected_size:
        actual = output.stat().st_size
        output.unlink(missing_ok=True)
        raise ValueError(f"extracted EFM size {actual} differs from archive listing {expected_size}")
