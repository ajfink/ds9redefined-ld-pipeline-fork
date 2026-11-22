"""Read-only parsing of Orc ``.orcprj`` projects (YAML-like text).

``source_id_for_capture`` and ``reference_map_ranges`` are ported verbatim from v38
``scripts/pad_audit.py``; the node-editing helpers and ``build_stack_project`` from v38
``scripts/build_stack_project.py`` (its ``main`` made pure: no manifest/profile access).
"""
from __future__ import annotations

import re
from pathlib import Path

from fstk.core.errors import ArtifactError as PipelineError

MIN_STACK_CAPTURES = 2


def source_id_for_capture(project: Path, capture: str) -> int:
    """Find the Orc source node by its human label, not manifest array order.

    Orc project node order can differ from the manifest's captures order (E05's
    M1 reference is source node 1 while M1 is second in captures[]).
    """
    text = project.read_text(encoding="utf-8", errors="replace")
    for block in re.split(r"(?m)^    - id: ", text)[1:]:
        m = re.match(r"(\d+)", block)
        stage = re.search(r"(?m)^      stage: (\S+)", block)
        label = re.search(r"(?m)^      user_label: TBC Source (.+?)\s*$", block)
        if m and stage and stage.group(1) == "tbc_source" and label and label.group(1) == capture:
            return int(m.group(1))
    raise PipelineError(f"{project}: no TBC source node labeled {capture!r}")


def reference_map_ranges(project: Path, source_id: int = 1) -> list[int]:
    """PAD_1 output indices of the frame_map fed by source_id.

    Every project we generate lists sources and their maps in the same order and
    edges each source into exactly one map, so following the edge from source 1
    selects the reference capture's map regardless of how maps are labelled.
    """
    text = project.read_text(encoding="utf-8", errors="replace")
    stages: dict[int, str] = {}
    blocks: dict[int, str] = {}
    for block in re.split(r"(?m)^    - id: ", text)[1:]:
        m = re.match(r"(\d+)", block)
        if not m:
            continue
        node_id = int(m.group(1))
        stage = re.search(r"(?m)^      stage: (\S+)", block)
        stages[node_id] = stage.group(1) if stage else ""
        blocks[node_id] = block
    edges = [(int(a), int(b))
             for a, b in re.findall(r"(?m)^    - from: (\d+)\s*$\n      to: (\d+)", text)]
    map_id = next((b for a, b in edges if a == source_id and stages.get(b) == "frame_map"), None)
    if map_id is None:
        raise PipelineError(f"{project}: no frame_map fed by source {source_id}")
    m = re.search(r"value:\s*([0-9PAD_,\-]+)", blocks[map_id])
    if not m:
        raise PipelineError(f"{project}: frame_map node {map_id} has no ranges value")
    out, pads = 0, []
    for tok in m.group(1).split(","):
        if tok == "PAD_1":
            pads.append(out)
            out += 1
        elif "-" in tok:
            a, b = map(int, tok.split("-"))
            out += b - a + 1
        else:
            out += 1
    return pads


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
