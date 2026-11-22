#!/usr/bin/env python3
"""Check fstk's EFM code against a real v38 EFM decode (Orc logs, projects, provenance).

Reads a v38 device episode folder (``devices/<machine>/episodes/<code>``) and, for every capture
with an ``efm-decode-*.log`` / ``.orcprj`` / ``efm-provenance-*.json`` triple:

1. rebuilds the Orc project with ``fstk.tools.orc_efm.build_decode_project`` from the paths in the
   v38 project and requires byte equality;
2. runs ``verify_efm_log`` on the real Orc log with the expected TBC / JSON / WAV / report paths;
3. extracts the decoding report that Orc also writes into its log and parses it with
   ``parse_efm_report``; the six Pareto metrics must equal v38's recorded ``quality_summary``;
4. applies ``unique_pareto`` to all captures and prints the comparison table.

Nothing is copied into the code tree (device data stays on the device); the script prints a
summary and exits non-zero on any mismatch.

    python tools/validate_v38_efm_evidence.py <episode-folder>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PureWindowsPath

from fstk.core.errors import FstkError
from fstk.tools.orc_efm import (
    PARETO_METRICS,
    build_decode_project,
    parse_efm_report,
    project_formats,
    render_table,
    unique_pareto,
    verify_efm_log,
)

LOG_PREFIX = re.compile(r"^\[[^\]]*\] \[[^\]]*\] \[[^\]]*\] ?")


def report_from_log(text: str) -> str:
    lines = text.splitlines()
    start = next(i for i, x in enumerate(lines) if "Writing decoding report to:" in x)
    end = next(i for i, x in enumerate(lines) if "EFMSink: Success" in x)
    return "\n".join(LOG_PREFIX.sub("", x) for x in lines[start + 1:end])


def param(text: str, key: str) -> str:
    m = re.search(rf"(?m)^        {key}:\n          type: \w+\n          value: (.+)$", text)
    return json.loads(m.group(1))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("episode_folder", type=Path)
    args = ap.parse_args()
    root = args.episode_folder
    problems, quality = [], {}
    for prov_path in sorted(root.glob("references/efm-provenance-*-*-*Z-*.json")):
        prov = json.loads(prov_path.read_text(encoding="utf-8-sig"))
        label = prov["capture"]
        project_path = root / "references" / prov["orc_project_name"]
        log_path = root / "logs" / prov["orc_log_name"]
        project = project_path.read_text(encoding="utf-8")
        tbc, efm, wav = (PureWindowsPath(param(project, k)) for k in ("input_path", "efm_path", "output_path"))
        video, source, amp = project_formats(project)
        episode = re.search(r'name: "EFM audio decode (\S+) ', project).group(1)
        rebuilt = build_decode_project(episode=episode, capture=label, tbc=tbc, efm=efm, wav=wav,
                                       video_format=video, source_format=source, amplitude_unit=amp)
        ok_project = rebuilt == project
        log = log_path.read_bytes().decode("utf-8-sig", errors="replace")
        try:
            checks = verify_efm_log(log, tbc=tbc, metadata=PureWindowsPath(str(tbc) + ".json"), wav=wav,
                                    report=PureWindowsPath(str(wav) + ".txt"))
            ok_log = all(checks.values())
        except FstkError as exc:
            ok_log, checks = False, str(exc)
        parsed = parse_efm_report(report_from_log(log))
        v38 = prov.get("quality_summary") or {}
        diffs = {k: (parsed.get(k), v38.get(k)) for k in (*PARETO_METRICS, "assessment") if parsed.get(k) != v38.get(k)}
        quality[label] = parsed
        print(f"{label}: project {'==' if ok_project else '!='} v38, log binding {'OK' if ok_log else checks}, "
              f"metrics {diffs if diffs else '== v38'}, missing label {parsed['missing_sections_label']!r}")
        if not ok_project:
            problems.append(f"{label}: rebuilt project differs")
        if not ok_log:
            problems.append(f"{label}: log binding failed")
        if diffs:
            problems.append(f"{label}: metric differences {diffs}")
    if not quality:
        print("no EFM provenance found", file=sys.stderr)
        return 2
    auto = unique_pareto(quality)
    print("\n" + render_table([{"capture": k, **v} for k, v in quality.items()]))
    print(f"\nunique Pareto choice: {auto.capture if auto else 'none'}")
    if auto is None:
        from fstk.tools.orc_efm import least_bad, pareto_front
        lb = least_bad(quality)
        print(f"Pareto front: {sorted(pareto_front(quality))}")
        print(f"least-bad choice: {lb.capture if lb else 'none (metrics missing)'}")
        if lb:
            print(lb.note)
    if problems:
        print("\nPROBLEMS:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    print(f"\nPASS: {len(quality)} capture(s) match v38's real Orc evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
