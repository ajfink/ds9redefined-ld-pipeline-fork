from __future__ import annotations

from pathlib import Path

import pytest

from fstk.core.errors import ArtifactError
from fstk.tools.orc import project_source_labels

PRJ = Path(__file__).parents[1] / "fixtures" / "v38" / "orc" / "stack-full-s01e18.orcprj"


def test_real_s01e18_project_labels_and_ids():
    labels = project_source_labels(PRJ)
    assert labels[:4] == ["L1", "M1", "P1", "P2"]


def _captures(tmp_path, labels):
    paths = {}
    for label in labels:
        p = tmp_path / f"DS9-S01E20-In the Hands of the Prophets-{label}.tbc"
        p.write_bytes(b"")
        (tmp_path / (p.name + ".json")).write_text("{}")
        paths[label] = p
    return paths


@pytest.mark.parametrize("count", [2, 6, 12])
def test_build_stack_project_any_capture_count(tmp_path, count):
    from fstk.tools.orc_project import build_stack_project, bundled_template, find_capture_file

    labels = ["P6"] + [f"C{i}" for i in range(1, count)]
    paths = _captures(tmp_path, labels)
    assert find_capture_file(tmp_path, "P6") == paths["P6"]
    text = build_stack_project(bundled_template(), labels, paths, tmp_path / "master.mov", "S01E20 stack")
    prj = tmp_path / "stack.orcprj"
    prj.write_text(text)
    assert project_source_labels(prj) == labels
    assert text.count("stage: frame_map") == count and "Emissary" not in text
    assert f"value: {tmp_path / 'master.mov'}" in text
    # every map feeds the single source_align node
    align_id = 2 * count + 1
    assert text.count(f"      to: {align_id}") == count


def test_build_rejects_duplicates_and_single_capture(tmp_path):
    from fstk.tools.orc_project import build_stack_project, bundled_template

    paths = _captures(tmp_path, ["A", "B"])
    with pytest.raises(ArtifactError):
        build_stack_project(bundled_template(), ["A"], paths, tmp_path / "m.mov", "x")
    with pytest.raises(ArtifactError):
        build_stack_project(bundled_template(), ["A", "A"], paths, tmp_path / "m.mov", "x")


# ------------------------------------------------------------------------------ CLV time-code pads (ADR 0004)
S01E20 = Path(__file__).parents[1] / "fixtures" / "orc" / "s01e20-stack-with-timecode-pads.orcprj"


def test_timecode_pads_are_taken_out_of_every_capture_and_nothing_else_changes():
    from fstk.tools.orc_project import strip_timecode_pads

    text = S01E20.read_text(encoding="utf-8")
    r = strip_timecode_pads(text)
    assert r.count == 83 and r.master_positions[:3] == (900, 1800, 2700) and 9000 not in r.master_positions
    assert set(r.per_capture.values()) == {83} and len(r.per_capture) == 12
    assert r.kept == {"P6": 0, "L1": 1, "L2": 1, "L3": 4, "M1": 0, "P1": 0, "P2": 0, "P3": 2, "P4": 1, "P5": 1,
                      "P7": 0, "P8": 0}                    # single-capture faults stay for the stacker to fill
    assert "value: 0-83251\n" in r.text                    # P6: one contiguous range
    assert "0-49880,PAD_1,49882-51467,PAD_1" in r.text     # L3: its own faults kept
    keep = [ln for ln in text.splitlines() if "value: 0-" not in ln]
    assert keep == [ln for ln in r.text.splitlines() if "value: 0-" not in ln]
    again = strip_timecode_pads(r.text)
    assert again.count == 0 and again.text == r.text       # already stripped: unchanged


def test_v38_s01e18_project_has_the_same_timecode_pads():
    from fstk.tools.orc_project import strip_timecode_pads

    r = strip_timecode_pads(PRJ.read_text(encoding="utf-8"))
    assert r.count > 50 and all(p % 900 == r.master_positions[0] % 900 for p in r.master_positions)


def test_timecode_pads_that_do_not_line_up_across_captures_fail_closed():
    import re

    from fstk.core.errors import ArtifactError
    from fstk.tools.orc_project import strip_timecode_pads

    text = S01E20.read_text(encoding="utf-8")
    # move one capture's grid by 3 frames: the time code is the disc's, so this cannot be a time-code PAD pattern
    broken = re.sub(r"(Frame Map L1.*?value: )(\S+)", lambda m: m.group(1) + m.group(2).replace(
        "0-883,PAD_1,884-", "0-880,PAD_1,881-", 1), text, count=1, flags=re.S)
    broken = broken.replace("2+16", "2+19")
    with pytest.raises(ArtifactError, match=r"line up|grid"):
        strip_timecode_pads(broken)
