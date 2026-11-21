"""Characterization: v38 tests/test_efm_decode.py, test_efm_report_table.py and the app's
unique-Pareto rule, ported against fstk.tools.orc_efm (ValueError/PipelineError → ArtifactError)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fstk.core.errors import ArtifactError
from fstk.tools.orc_efm import (
    build_decode_project,
    parse_efm_report,
    project_formats,
    render_table,
    sevenzip_efm_member,
    unique_pareto,
)

# tests/test_efm_report_table.py SYNTHETIC_REPORT (verbatim)
SYNTHETIC_REPORT = """\
Overall assessment : POOR
Disc duration : 46:08:21 (207,542 sections)
Tracks recovered : 2
Q-channel timecodes : 207,542 (100.00 %)
Samples decoded : 244,593,888
Audio integrity (programme area)
Concealed (interp) : 1,234 (0.10 %)
Silenced (muted) : 2,345 (0.20 %)
Data loss (post-C2) : 0.0073 %
C1 uncorrectable : 45 (0.01 %)
C2 uncorrectable : 12 (0.00 %)
Missing (interpolated) : 7
Sections out-of-order / uncorrectable : 0 / 0
Timeline resyncs : 0
Padding : 0
Q start 00:00:00
Q end 00:46:08
"""

#: sha256 of v38 efm_decode.build_decode_project for the kwargs below (generated 2026-11-21 from v38)
V38_PROJECT_SHA256 = "50762de1eb360727b486256bff426589dd297735ffdbe70707a16e22a0763561"


def test_project_is_byte_identical_to_v38():
    project = build_decode_project(
        episode="s01e18", capture="P1", tbc=Path("/data/DS9 Capture/capture 'P1' é.tbc"),
        efm=Path("/data/DS9 Capture/capture 'P1'.efm"), wav=Path("/audio/S01E18/P1-efm.wav"),
        video_format="NTSC", source_format="Composite", amplitude_unit="10bit")
    assert hashlib.sha256(project.encode()).hexdigest() == V38_PROJECT_SHA256
    for needle in ("stage: tbc_source", "        efm_path:", "stage: EFMSink", "          value: audio",
                   "          value: true", "capture 'P1'.efm", "  edges:"):
        assert needle in project


def test_source_formats_are_read_from_existing_orc_project():
    assert project_formats("project:\n  video_format: NTSC\n  source_format: YC\n  amplitude_unit: 10bit\n") == \
        ("NTSC", "YC", "10bit")
    with pytest.raises(ArtifactError, match="supported video/source format"):
        project_formats("project:\n  video_format: SECAM\n  source_format: Weird\n  amplitude_unit: IRE\n")


LISTING = """Header
----------
Path = sample.7z
Type = 7z

----------
Path = nested/S01E18-P1.efm
Size = 1234
Packed Size = 1000
Folder = -
"""


def test_archive_member_must_be_one_exact_safe_efm():
    assert sevenzip_efm_member(LISTING, "S01E18-P1.efm") == ("nested/S01E18-P1.efm", 1234)
    with pytest.raises(ArtifactError, match="exactly one"):
        sevenzip_efm_member(LISTING, "other.efm")
    with pytest.raises(ArtifactError, match="exactly one"):
        sevenzip_efm_member(LISTING.replace("nested/S01E18-P1.efm", "../S01E18-P1.efm"), "S01E18-P1.efm")
    with pytest.raises(ArtifactError, match="exactly one"):
        sevenzip_efm_member(LISTING.replace("Folder = -", "Folder = -\nSymbolic Link = target.efm"), "S01E18-P1.efm")
    with pytest.raises(ArtifactError, match="exactly one"):   # fstk addition: Windows drive paths
        sevenzip_efm_member(LISTING.replace("nested/S01E18-P1.efm", "C:/S01E18-P1.efm"), "S01E18-P1.efm")


def test_report_fields_match_v38_report_table():
    row = parse_efm_report(SYNTHETIC_REPORT)
    # v38 efm_report_table.parse_report values for the same text
    assert row["disc_duration"] == "46:08:21" and row["sections"] == 207542
    assert row["post_c2_data_loss_pct"] == 0.0073
    assert (row["concealed_samples"], row["muted_samples"]) == (1234, 2345)
    assert (row["c1_uncorrectable"], row["c2_uncorrectable"]) == (45, 12)
    assert (row["c1_uncorrectable_pct"], row["c2_uncorrectable_pct"]) == (0.01, 0.0)
    assert row["missing_sections"] == 7 and row["out_of_order"] == 0 and row["sections_uncorrectable"] == 0
    assert row["timeline_resyncs"] == 0 and row["padding"] == 0 and row["assessment"] == "POOR"
    table = render_table([{"capture": "L1", **row}])
    assert "post-C2%" in table and "0.0073" in table and "1234" in table and "207,542 (100%)" in table


# Layout of a real Orc 2.11.1 EFMSink report (box drawing, both missing-section labels); numbers invented.
REAL_LAYOUT = """\
 ╔══════════════════════════════════════════════════════════════════════════════╗
 ║  PART A - DECODE SUMMARY                                                     ║
 ╚══════════════════════════════════════════════════════════════════════════════╝

   Overall assessment : POOR
   Disc duration      : 46:16:23   (208,224 sections)
   Tracks recovered   : 1
   Q-channel timecodes: Valid and contiguous

   Audio integrity (programme area)
     Samples decoded    : 244,823,208
     Concealed (interp) : 2,111   (0.0009 %)
     Silenced (muted)   : 51,000   (0.0208 %)

   Audio integrity (outside programme area)
     Lead-in            : 0 concealed, 0 muted   (of 0 decoded)
     Pause (pre-gap)    : 0 concealed, 0 muted   (of 45,864 decoded)

   Error-correction health (fully-populated codewords only)
     C1 uncorrectable   : 13,000   (0.0637 %)
     C2 uncorrectable   : 5,000   (0.0245 %)
     Data loss (post-C2): 0.0245 %
     Sections missing   : 17   (reconstructed by interpolation)
     Sections out-of-order / uncorrectable : 0 / 0

 ╔══════════════════════════════════════════════════════════════════════════════╗
 ║  PART C - SIGNAL & ERROR-CORRECTION QUALITY                                  ║
 ╚══════════════════════════════════════════════════════════════════════════════╝

   Section-level integrity (F2)
     Total sections         : 208,224   (20,405,952 F2 frames)
     Missing (interpolated) : 17
     Padding                : 0
     Out of order           : 0
     Timeline resyncs       : 0   (0 section(s) skipped, 0 discarded)
"""


def test_real_orc_layout_parses_all_pareto_metrics():
    row = parse_efm_report(REAL_LAYOUT)
    assert row["missing_sections"] == 17 and row["missing_sections_label"] == "Sections missing"
    assert (row["post_c2_data_loss_pct"], row["c1_uncorrectable_pct"], row["c2_uncorrectable_pct"]) == \
        (0.0245, 0.0637, 0.0245)
    assert (row["concealed_samples"], row["muted_samples"]) == (2111, 51000)
    assert row["sections"] == 208224 and row["q_timecodes"] == "Valid and contiguous" and row["padding"] == 0


def test_missing_sections_read_from_either_label_finding_0003():
    # v38 verify_efm_capture only read "Sections missing"; its report table only "Missing (interpolated)".
    assert parse_efm_report(SYNTHETIC_REPORT)["missing_sections_label"] == "Missing (interpolated)"
    other = SYNTHETIC_REPORT.replace("Missing (interpolated) : 7", "Sections missing : 7")
    assert parse_efm_report(other)["missing_sections"] == 7


def q(**kw):
    base = dict(post_c2_data_loss_pct=0.01, c2_uncorrectable_pct=0.0, c1_uncorrectable_pct=0.01,
                missing_sections=0, concealed_samples=10, muted_samples=0)
    return {**base, **kw}


def test_unique_pareto_rules():
    assert unique_pareto({"L1": q()}).capture == "L1"
    assert unique_pareto({"L1": q(), "P1": q(concealed_samples=11)}).capture == "L1"
    assert unique_pareto({"L1": q(), "P1": q()}) is None                       # tie: no strict improvement
    assert unique_pareto({"L1": q(muted_samples=5), "P1": q(concealed_samples=50)}) is None  # trade-off
    assert unique_pareto({"L1": q(), "P1": q(missing_sections=None)}) is None  # missing metric fails closed
    assert unique_pareto({"L1": q(), "P1": q(muted_samples=float("nan"))}) is None
    assert unique_pareto({"L1": q(), "P1": q(muted_samples=True)}) is None
