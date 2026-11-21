# Finding 0003 — EFM "missing sections" label (withdrawn: not a defect)

Date: 2026-11-21 · Status: **withdrawn after checking real Orc 2.11.1 output — v38 was right**

## Original suspicion
v38 has two parsers for the EFMSink decoding report: `verify_efm_capture.py` (feeds the app's
unique-Pareto auto-selection) reads `Sections missing : N`; `efm_report_table.py` reads
`Missing (interpolated) : N`. Run against v38's own report-table fixture, the provenance parser
found no missing-sections value, which would have disabled automatic selection.

## What real output shows
The S01E20 decode of 12 captures (Orc 2.11.1, 2026-11-20, logs in v38's device workspace) prints
**both** lines with the same number:

* Part A: `Sections missing   : 19   (reconstructed by interpolation)`
* Part C: `Missing (interpolated) : 19`

v38's provenance parser matched Part A on every capture, so its auto-selection had all six metrics.
For S01E20 no capture was best on every metric (M1 had the lowest loss/muting, P2 the fewest
concealed samples), so v38 correctly asked the operator, who chose P2.

## In fstk
`parse_efm_report` reads Part A first (leading integer), falling back to the Part C label, and records
which label it used. `tools/validate_v38_efm_evidence.py` re-checks fstk against real v38 Orc evidence:
for all 12 S01E20 captures the rebuilt Orc project is byte-identical, the log path binding passes,
and all six metrics equal v38's recorded values; the unique-Pareto rule returns "operator must choose".
