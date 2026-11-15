#!/usr/bin/env python3
"""Regenerate model-specific runbooks from the canonical procedure + templates."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANONICAL = ROOT / "DS9 LaserDisc OpenCode Runbook.md"
OUTPUTS = {
    "DS9 LaserDisc OpenCode Runbook - Qwen4B.md": {
        "template": ROOT / "templates/runbooks/qwen4b.template.md",
        "guidance": (
            "Use short, literal ACTION/VERIFY steps. Copy commands only from the current "
            "canonical workflow; do not infer missing tool output or claim that a background "
            "job completed. If context is low, stop at a verified stage boundary, summarize "
            "the exact state and next command, and continue from the saved manifest/log."
        ),
    },
    "DS9 LaserDisc OpenCode Runbook - Qwen4B-Luna.md": {
        "template": ROOT / "templates/runbooks/qwen4b-luna.template.md",
        "guidance": (
            "Qwen4B is primary. Use `opencode-go/gpt-6-luna` only at an explicit DELEGATE "
            "trigger in README or the canonical workflow, and include the relevant artifacts, "
            "measurements, and already-attempted checks. Luna advises on that bounded question; "
            "it does not authorize a gate bypass, overwrite, install, or release claim. If the "
            "fallback is unavailable, ask the user rather than silently changing models."
        ),
    },
}


def generated_text(filename: str) -> str:
    spec = OUTPUTS[filename]
    template = spec["template"].read_text(encoding="utf-8")
    canonical = CANONICAL.read_text(encoding="utf-8").rstrip()
    # Keep one document title: the generated wrapper is the title; the canonical
    # runbook begins as a named section while retaining all of its subordinate headings.
    first_line, separator, remainder = canonical.partition("\n")
    if not separator or not first_line.startswith("# "):
        raise ValueError(f"canonical runbook has no first-level title: {CANONICAL}")
    canonical = "# Canonical technical procedure\n" + remainder.lstrip("\n")
    result = template.replace("{{MODEL_GUIDANCE}}", spec["guidance"])
    result = result.replace("{{CANONICAL_RUNBOOK}}", canonical)
    if "{{" in result or "}}" in result:
        raise ValueError(f"unexpanded template token in {filename}")
    return result.rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="check outputs are current without writing")
    args = ap.parse_args(argv)
    stale = []
    for filename in OUTPUTS:
        out = ROOT / filename
        text = generated_text(filename)
        if args.check:
            if not out.is_file() or out.read_text(encoding="utf-8") != text:
                stale.append(filename)
        else:
            out.write_text(text, encoding="utf-8", newline="\n")
            print(f"wrote {out}")
    if args.check and stale:
        print("runbooks out of date: " + ", ".join(stale), file=sys.stderr)
        return 1
    if args.check:
        print("generated runbooks are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
