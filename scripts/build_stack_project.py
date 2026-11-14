#!/usr/bin/env python3
"""Create a fresh Orc weighted-stack project from an episode manifest.

The template is structural only: source paths, source labels, frame-map values,
alignment values, and output path are replaced/cleared. Orc refresh still must
measure the actual maps and recommended reference before rendering.

  python scripts\\build_stack_project.py --episode S01E06
  python scripts\\build_stack_project.py --episode S01E06 --downloads D:\\DS9-S01E06

The manifest's `captures` inventory is ordered with the selected `reference`
first in the generated Orc graph. For automatic reference discovery only,
`--reference-override` can set a provisional first input without editing the
manifest. `alignment_map` must be measured after Orc's refresh; this builder
never invents offsets or frame maps.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ldcommon import BUNDLE, Ld, PipelineError, load_episode  # noqa: E402

TEMPLATE = BUNDLE / "templates" / "stack_template.orcprj"
MIN_CAPTURES = 2
MAX_CAPTURES = 6


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


def set_sink_path(block: list[str], value: str) -> None:
    for i, line in enumerate(block):
        if re.match(r"^\s+value: .*\.mov\s*$", line, re.IGNORECASE):
            indent = re.match(r"^\s+", line).group(0)
            block[i] = f"{indent}value: {value}"
            return
    raise PipelineError("video_sink template has no .mov output value")


def find_capture_file(folder: Path, label: str) -> Path:
    hits = [p for p in folder.glob("*.tbc") if p.stem.rsplit("-", 1)[-1].casefold() == label.casefold()]
    if len(hits) != 1:
        raise PipelineError(f"expected one .tbc ending in -{label}.tbc under {folder}, found {len(hits)}")
    path = hits[0]
    sidecar = Path(str(path) + ".json")
    if not sidecar.exists():
        raise PipelineError(f"paired TBC JSON missing: {sidecar}")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--downloads", help="override the manifest/profile download folder")
    ap.add_argument("--template", help="override the bundled Orc project template")
    ap.add_argument("--project-out", help="test/alternate .orcprj output (does not update the manifest)")
    ap.add_argument("--master-out", help="test/alternate ProRes sink output path")
    ap.add_argument("--reference-override", help="provisional reference/order for a scratch project; does not update the manifest")
    args = ap.parse_args()

    ld = Ld()
    ep = load_episode(args.episode, ld)
    if ep.data.get("format") == "two_part":
        raise PipelineError("two-part E01 needs one project per side; this builder handles one-part episodes only")
    captures = ep.capture_set
    ref = args.reference_override if args.reference_override is not None else ep.data.get("reference")
    if not ref:
        raise PipelineError(
            f"{ep.code}: no stack reference is measured; run scripts\\select_stack_reference.py first"
        )
    if ref not in captures:
        raise PipelineError(f"reference {ref!r} is not in captures {captures}")
    if not MIN_CAPTURES <= len(captures) <= MAX_CAPTURES:
        raise PipelineError(f"weighted stack needs {MIN_CAPTURES}..{MAX_CAPTURES} captures; found {len(captures)}")
    ordered = [ref] + [c for c in captures if c != ref]
    downloads = Path(args.downloads) if args.downloads else ep.downloads()
    project_out = Path(args.project_out) if args.project_out else ep.path("stack_project")
    master_out = Path(args.master_out) if args.master_out else ep.path("stack_master")
    template = Path(args.template) if args.template else TEMPLATE
    if not template.exists():
        raise PipelineError(f"Orc template missing: {template}")
    if not downloads.exists():
        raise PipelineError(f"download directory missing: {downloads}")
    if project_out.exists():
        raise PipelineError(f"refusing to overwrite an existing Orc project: {project_out}")

    source_head, templates, _ = split_nodes(template.read_text(encoding="utf-8", errors="replace"))
    source_templates = [templates[i] for i in sorted(templates)
                       if re.search(r"(?m)^      stage: tbc_source\s*$", "\n".join(templates[i]))]
    map_templates = [templates[i] for i in sorted(templates)
                     if re.search(r"(?m)^      stage: frame_map\s*$", "\n".join(templates[i]))]
    align_template = next((b for b in templates.values() if re.search(r"(?m)^      stage: source_align\s*$", "\n".join(b))), None)
    stack_template = next((b for b in templates.values() if re.search(r"(?m)^      stage: stacker\s*$", "\n".join(b))), None)
    sink_template = next((b for b in templates.values() if re.search(r"(?m)^      stage: video_sink\s*$", "\n".join(b))), None)
    if len(source_templates) < len(ordered) or len(map_templates) < len(ordered) or not all((align_template, stack_template, sink_template)):
        raise PipelineError("bundled Orc template does not contain enough source/map nodes and the required align/stack/sink nodes")

    nodes: list[list[str]] = []
    edges: list[tuple[int, int]] = []
    for index, label in enumerate(ordered):
        source_id = index + 1
        map_id = len(ordered) + index + 1
        src = set_node_id_and_x(source_templates[index], source_id)
        set_label(src, f"TBC Source {label}")
        set_input_path(src, str(find_capture_file(downloads, label)))
        fmap = set_node_id_and_x(map_templates[index], map_id)
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
            head[i] = f"  name: {ep.code} {ep.title} weighted stack"
            break
    out_lines = head + ["  nodes:"]
    for block in nodes:
        out_lines.extend(block)
    out_lines.append("  edges:")
    for source_id, target_id in edges:
        out_lines.extend((f"    - from: {source_id}", f"      to: {target_id}"))
    project_out.parent.mkdir(parents=True, exist_ok=True)
    project_out.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    check = project_out.read_text(encoding="utf-8")
    if check.count("stage: tbc_source") != len(ordered) or check.count("stage: frame_map") != len(ordered):
        raise PipelineError(f"generated project node-count check failed: {project_out}")
    if "@@placeholder@@" in check or re.search(r"(?m)^\s+value:\s*\d+\+\d+(?:,\d+\+\d+)*\s*$", check):
        raise PipelineError(f"generated project still contains a stale alignment/frame map value: {project_out}")
    print(f"wrote {project_out}")
    print(f"captures in Orc input order: {', '.join(ordered)} (reference first: {ref})")
    print(f"cleared {len(ordered)} frame maps and source alignment; refresh Orc before render")
    print(f"expected weighted master: {master_out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
