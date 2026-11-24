"""Read-only parsing of Orc ``.orcprj`` projects (YAML-like text).

The node-editing helpers and ``build_stack_project`` come from v38 ``scripts/build_stack_project.py`` (its
``main`` made pure: no manifest/profile access). ``strip_timecode_pads`` removes the CLV time-code PAD frames
Orc inserts (ADR 0004); it replaces v38 ``scripts/pad_audit.py``'s ``source_id_for_capture`` /
``reference_map_ranges``, which only located those PADs for repair.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from fstk.core.errors import ArtifactError as PipelineError

MIN_STACK_CAPTURES = 2


def split_nodes(text: str) -> tuple[list[str], dict[int, list[str]], list[str]]:
    lines = text.splitlines()
    try:
        ni = lines.index("  nodes:")
        ei = lines.index("  edges:")
    except ValueError as e:
        raise PipelineError("Orc template has no nodes/edges sections") from e
    head = lines[:ni]
    node_lines = lines[ni + 1:ei]
    tail = lines[ei + 1:]
    starts = [i for i, line in enumerate(node_lines) if re.match(r"^    - id: \d+\s*$", line)]
    blocks: dict[int, list[str]] = {}
    for j, start in enumerate(starts):
        end = starts[j + 1] if j + 1 < len(starts) else len(node_lines)
        block = node_lines[start:end]
        node_id = int(re.search(r"id: (\d+)", block[0]).group(1))
        blocks[node_id] = block
    return head, blocks, tail


def set_label(block: list[str], label: str) -> None:
    for i, line in enumerate(block):
        if re.match(r"^      user_label:", line):
            block[i] = f"      user_label: {label}"
            return


def set_node_id_and_x(block: list[str], node_id: int) -> list[str]:
    out = list(block)
    out[0] = f"    - id: {node_id}"
    x = (node_id - 1) * 200
    for i, line in enumerate(out):
        if re.match(r"^      x: \d+\s*$", line):
            out[i] = f"      x: {x}"
    return out


def set_input_path(block: list[str], value: str) -> None:
    for i, line in enumerate(block):
        if re.match(r"^\s+value: .*\.tbc\s*$", line, re.IGNORECASE):
            indent = re.match(r"^\s+", line).group(0)
            block[i] = f"{indent}value: {value}"
            return
    raise PipelineError("source node template has no TBC input_path value")


def clear_frame_map(block: list[str]) -> None:
    active = False
    for i, line in enumerate(block):
        if re.match(r"^\s+ranges:\s*$", line):
            active = True
        elif active and re.match(r"^\s+value:", line):
            indent = re.match(r"^\s+", line).group(0)
            block[i] = f"{indent}value:"
            return
    # The bundled clean template intentionally leaves the field to Orc's refresh.
    # Other project templates may carry stale measured ranges; those are cleared.
    return


def clear_alignment(block: list[str]) -> None:
    for i, line in enumerate(block):
        m = re.match(r"^(\s+value:)\s*[^\n]*\+[^\n]*$", line)
        if m:
            block[i] = m.group(1)
            return
    raise PipelineError("source_align template has no alignment-map value")


LOSSLESS_SINK_FORMAT = "mkv-ffv1"


def set_sink_path(block: list[str], value: str) -> None:
    """Point the video sink at ``value`` and force the lossless FFV1/Matroska format.

    The stack master is the source of every later picture step, so it must be lossless (owner rule: grain is
    paramount, the final encode is the only lossy step). An older template asking for ProRes is corrected here.
    """
    path_done = fmt_done = False
    for i, line in enumerate(block):
        if not path_done and re.match(r"^\s+value: .*\.(mov|mkv)\s*$", line, re.IGNORECASE):
            indent = re.match(r"^\s+", line).group(0)
            block[i] = f"{indent}value: {value}"
            path_done = True
        elif re.match(r"^\s+ffmpeg_format:\s*$", line) and i + 2 < len(block):
            m = re.match(r"^(\s+value: ).*$", block[i + 2])
            if m:
                block[i + 2] = m.group(1) + LOSSLESS_SINK_FORMAT
                fmt_done = True
    if not path_done:
        raise PipelineError("video_sink template has no .mov/.mkv output value")
    if not fmt_done:
        raise PipelineError("video_sink template has no ffmpeg_format to set to the lossless format")
    # a stale ProRes profile parameter is meaningless for FFV1; drop it so the project reads honestly
    for i in range(len(block) - 3, -1, -1):
        if re.match(r"^\s+prores_profile:\s*$", block[i]):
            del block[i:i + 3]


def lossless_render_project(text: str, master_out: Path) -> str:
    """A copy of a stack project whose video sink writes the lossless FFV1 master to ``master_out``.

    Used to re-render an episode whose recorded project (v38's, or an older fstk one) still asks for ProRes.
    Only the sink node changes: sources, frame maps and the measured alignment are kept exactly.
    """
    head, blocks, tail = split_nodes(text)
    sinks = [i for i, b in blocks.items() if re.search(r"(?m)^      stage: video_sink\s*$", "\n".join(b))]
    if len(sinks) != 1:
        raise PipelineError(f"stack project must have exactly one video_sink node (found {len(sinks)})")
    block = list(blocks[sinks[0]])
    set_sink_path(block, str(master_out))
    blocks[sinks[0]] = block
    lines = [*head, "  nodes:"]
    for i in blocks:
        lines.extend(blocks[i])
    lines.append("  edges:")
    lines.extend(tail)
    return "\n".join(lines) + "\n"


def find_capture_file(folder: Path, label: str) -> Path:
    hits = [p for p in folder.glob("*.tbc") if p.stem.rsplit("-", 1)[-1].casefold() == label.casefold()]
    if len(hits) != 1:
        raise PipelineError(f"expected one .tbc ending in -{label}.tbc under {folder}, found {len(hits)}")
    path = hits[0]
    sidecar = Path(str(path) + ".json")
    if not sidecar.exists():
        raise PipelineError(f"paired TBC JSON missing: {sidecar}")
    return path

def build_stack_project(template_text: str, ordered: list[str], capture_paths: dict[str, Path],
                        master_out: Path, name: str) -> str:
    """Generate an Orc weighted-stack project (v38 ``build_stack_project.main`` body, made pure).

    ``ordered`` is the Orc input order with the reference first. Frame maps and the
    source alignment are cleared: Orc's refresh measures them; this never invents offsets.
    """
    if len(ordered) < MIN_STACK_CAPTURES:
        raise PipelineError(f"weighted stack needs at least {MIN_STACK_CAPTURES} captures; found {len(ordered)}")
    if len(set(ordered)) != len(ordered):
        raise PipelineError("capture labels must be unique")
    source_head, templates, _ = split_nodes(template_text)
    source_templates = [templates[i] for i in sorted(templates)
                       if re.search(r"(?m)^      stage: tbc_source\s*$", "\n".join(templates[i]))]
    map_templates = [templates[i] for i in sorted(templates)
                     if re.search(r"(?m)^      stage: frame_map\s*$", "\n".join(templates[i]))]
    align_template = next((b for b in templates.values() if re.search(r"(?m)^      stage: source_align\s*$", "\n".join(b))), None)
    stack_template = next((b for b in templates.values() if re.search(r"(?m)^      stage: stacker\s*$", "\n".join(b))), None)
    sink_template = next((b for b in templates.values() if re.search(r"(?m)^      stage: video_sink\s*$", "\n".join(b))), None)
    if not source_templates or not map_templates or not all((align_template, stack_template, sink_template)):
        raise PipelineError("Orc template does not contain the required source/map and align/stack/sink nodes")
    source_prototype = source_templates[-1]
    map_prototype = map_templates[-1]

    nodes: list[list[str]] = []
    edges: list[tuple[int, int]] = []
    for index, label in enumerate(ordered):
        source_id = index + 1
        map_id = len(ordered) + index + 1
        src = set_node_id_and_x(source_templates[index] if index < len(source_templates)
                                else source_prototype, source_id)
        set_label(src, f"TBC Source {label}")
        set_input_path(src, str(capture_paths[label]))
        fmap = set_node_id_and_x(map_templates[index] if index < len(map_templates)
                                 else map_prototype, map_id)
        set_label(fmap, f"Frame Map {label}")
        clear_frame_map(fmap)
        nodes.extend((src, fmap))
        edges.extend(((source_id, map_id), (map_id, 2 * len(ordered) + 1)))

    align_id, stack_id, sink_id = 2 * len(ordered) + 1, 2 * len(ordered) + 2, 2 * len(ordered) + 3
    align = set_node_id_and_x(align_template, align_id)
    set_label(align, "Source Align")
    clear_alignment(align)
    stack = set_node_id_and_x(stack_template, stack_id)
    set_label(stack, "Stacker")
    sink = set_node_id_and_x(sink_template, sink_id)
    set_label(sink, "Video Sink")
    set_sink_path(sink, str(master_out))
    nodes.extend((align, stack, sink))
    edges.extend(((align_id, stack_id), (stack_id, sink_id)))

    head = list(source_head)
    for i, line in enumerate(head):
        if line.startswith("  name:"):
            head[i] = f"  name: {name}"
            break
    out_lines = [*head, "  nodes:"]
    for block in nodes:
        out_lines.extend(block)
    out_lines.append("  edges:")
    for source_id, target_id in edges:
        out_lines.extend((f"    - from: {source_id}", f"      to: {target_id}"))
    text = "\n".join(out_lines) + "\n"
    if text.count("stage: tbc_source") != len(ordered) or text.count("stage: frame_map") != len(ordered):
        raise PipelineError("generated project node-count check failed")
    if "@@placeholder@@" in text or re.search(r"(?m)^\s+value:\s*\d+\+\d+(?:,\d+\+\d+)*\s*$", text):
        raise PipelineError("generated project still contains a stale alignment/frame map value")
    return text


def bundled_template() -> str:
    from importlib.resources import files

    return files("fstk.templates").joinpath("stack_template.orcprj").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------ CLV time-code pads
#: Orc maps a CLV disc's time code to picture numbers at 30 per second and inserts a PAD frame for every number the
#: time code skips. The disc's time code skips numbers on the drop-frame rule (two a minute, none every tenth
#: minute: one per 900 frames, every tenth one absent) to stay in step with real time; no picture is missing there.
#: Measured 2026-11-24 (S02E03, S01E20): the frames either side of each such PAD match consecutive DVD frames, the
#: EFM audio is as long as the video without them, and the PAD renders as an all-zero frame that shifts everything
#: after it by one frame. See docs/adr/0004-clv-timecode-pads.md.
TIMECODE_PAD_PERIOD = 900


@dataclass(frozen=True)
class TimecodePads:
    text: str                                   # the project with the time-code PADs removed from every frame map
    master_positions: tuple[int, ...]           # where they were in the rendered (aligned) output, ascending
    per_capture: dict[str, int]                 # capture label -> time-code PADs removed from its map
    kept: dict[str, int]                        # capture label -> other PADs left in place (capture faults)

    @property
    def count(self) -> int:
        return len(self.master_positions)


def _range_tokens(value: str) -> list[str]:
    return [t.strip() for t in value.split(",") if t.strip()]


def _pad_positions(tokens: list[str]) -> list[tuple[int, int]]:
    """(token index, output position) of every PAD_1."""
    out, found = 0, []
    for i, tok in enumerate(tokens):
        if tok == "PAD_1":
            found.append((i, out))
            out += 1
        elif "-" in tok:
            a, b = map(int, tok.split("-"))
            out += b - a + 1
        else:
            out += 1
    return found


def _alignment_offsets(blocks: dict[int, list[str]]) -> dict[int, int]:
    """Source order (1-based, as Orc numbers them) -> lead-in offset from the source_align node's alignmentMap."""
    for b in blocks.values():
        text = "\n".join(b)
        if re.search(r"(?m)^      stage: source_align\s*$", text):
            m = re.search(r"alignmentMap:\s*\n\s*type: string\s*\n\s*value:\s*(.+)$", text, re.M)
            if not m or not m.group(1).strip():
                return {}
            pairs = re.findall(r"(\d+)\s*\+\s*(-?\d+)", m.group(1))
            return {int(i): int(o) for i, o in pairs}
    return {}


def strip_timecode_pads(text: str) -> TimecodePads:
    """Remove the CLV time-code PADs from every frame map of a refreshed stack project (see TIMECODE_PAD_PERIOD).

    A capture's time-code PADs are the single PADs on its own 900-frame grid (gaps of 900, or 1800 where the
    tenth one is absent). Once each capture's lead-in from the alignment map is added they must fall on exactly
    the same positions in every capture: the time code belongs to the disc, not to a player. Anything else fails
    closed. Other PADs (a player skip in one capture) are kept; the stacker fills them from the other captures.
    A project without such PADs (a CAV disc, or one already stripped) comes back unchanged.
    """
    head, blocks, tail = split_nodes(text)
    stage_of = {i: (re.search(r"(?m)^      stage: (\S+)", "\n".join(b)) or [None, ""])[1] for i, b in blocks.items()}
    edges = [(int(a), int(b)) for a, b in re.findall(r"(?m)^    - from: (\d+)\s*$\n      to: (\d+)", "\n".join(tail))]
    sources = [i for i in blocks if stage_of[i] == "tbc_source"]
    offsets = _alignment_offsets(blocks)
    per: dict[int, dict] = {}
    for order, sid in enumerate(sources, start=1):
        block = "\n".join(blocks[sid])
        label_m = re.search(r"(?m)^      user_label: TBC Source (.+?)\s*$", block)
        label = label_m.group(1) if label_m else str(sid)
        map_id = next((b for a, b in edges if a == sid and stage_of.get(b) == "frame_map"), None)
        if map_id is None:
            continue
        lines = blocks[map_id]
        vi = next((k for k, ln in enumerate(lines) if re.match(r"^\s*value:\s*[0-9PAD_,\-]+\s*$", ln)), None)
        if vi is None:
            continue
        tokens = _range_tokens(lines[vi].split("value:", 1)[1])
        pads = _pad_positions(tokens)
        grid: list[tuple[int, int]] = []
        if len(pads) >= 3:
            phase = max(range(TIMECODE_PAD_PERIOD),
                        key=lambda ph: sum(1 for _, p in pads if p % TIMECODE_PAD_PERIOD == ph))
            grid = [(i, p) for i, p in pads if p % TIMECODE_PAD_PERIOD == phase
                    and (i == 0 or tokens[i - 1] != "PAD_1") and (i + 1 >= len(tokens) or tokens[i + 1] != "PAD_1")]
            if len(grid) < 2:
                grid = []
            gaps = {b - a for (_, a), (_, b) in pairwise(grid)}
            if grid and not gaps <= {TIMECODE_PAD_PERIOD, 2 * TIMECODE_PAD_PERIOD}:
                raise PipelineError(f"frame map of {label}: PADs on a {TIMECODE_PAD_PERIOD}-frame grid with gaps "
                                    f"{sorted(gaps)}; not the CLV time-code pattern, nothing was changed")
        per[sid] = {"label": label, "order": order, "map": map_id, "line": vi, "tokens": tokens,
                    "grid": grid, "other": len(pads) - len(grid), "offset": offsets.get(order)}
    with_grid = [v for v in per.values() if v["grid"]]
    if not with_grid:
        return TimecodePads(text, (), {v["label"]: 0 for v in per.values()},
                            {v["label"]: v["other"] for v in per.values()})
    if len(with_grid) != len(per):
        raise PipelineError("only some captures have CLV time-code PADs "
                            f"({sorted(v['label'] for v in with_grid)} of {sorted(v['label'] for v in per.values())})"
                            "; nothing was changed")
    if any(v["offset"] is None for v in per.values()):
        raise PipelineError("the project has no measured alignment map for every capture; refresh it first")
    aligned = {sid: [p + v["offset"] for _, p in v["grid"]] for sid, v in per.items()}
    union = sorted(set().union(*aligned.values()))
    for sid, pos in aligned.items():
        inside = [u for u in union if pos[0] <= u <= pos[-1]]
        if pos != inside:
            raise PipelineError(f"CLV time-code PADs of {per[sid]['label']} do not line up with the other captures "
                                f"({len(pos)} vs {len(inside)} in the same span); nothing was changed")
    for v in per.values():
        drop = {i for i, _ in v["grid"]}
        merged: list[str] = []
        for i, tok in enumerate(v["tokens"]):
            if i in drop:
                continue
            if (merged and "-" in tok and "-" in merged[-1] and merged[-1] != "PAD_1" and (i - 1) in drop
                    and int(merged[-1].split("-")[1]) + 1 == int(tok.split("-")[0])):
                merged[-1] = f"{merged[-1].split('-')[0]}-{tok.split('-')[1]}"
            else:
                merged.append(tok)
        line = blocks[v["map"]][v["line"]]
        blocks[v["map"]][v["line"]] = line.split("value:", 1)[0] + "value: " + ",".join(merged)
    out = [*head, "  nodes:"]
    for i in blocks:
        out.extend(blocks[i])
    out.append("  edges:")
    out.extend(tail)
    return TimecodePads("\n".join(out) + "\n", tuple(union),
                        {v["label"]: len(v["grid"]) for v in per.values()},
                        {v["label"]: v["other"] for v in per.values()})
