from __future__ import annotations

from pathlib import Path

import pytest

from fstk.core.errors import ArtifactError
from fstk.tools.orc import project_source_labels
from fstk.tools.orc_project import reference_map_ranges, source_id_for_capture

PRJ = Path(__file__).parents[1] / "fixtures" / "v38" / "orc" / "stack-full-s01e18.orcprj"


def test_real_s01e18_project_labels_and_ids():
    labels = project_source_labels(PRJ)
    assert labels[:4] == ["L1", "M1", "P1", "P2"]
    assert source_id_for_capture(PRJ, labels[0]) >= 1
    with pytest.raises(ArtifactError, match="no TBC source node"):
        source_id_for_capture(PRJ, "Z9")


def test_reference_map_pads_are_output_indices():
    sid = source_id_for_capture(PRJ, "L1")
    pads = reference_map_ranges(PRJ, sid)
    assert pads == sorted(pads) and all(p >= 0 for p in pads)


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
    assert source_id_for_capture(prj, "P6") == 1
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
