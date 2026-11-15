<!-- AUTO-GENERATED: run scripts/generate_runbooks.py. Edit this template and the canonical runbook, not the generated output. -->
# DS9 LaserDisc Workflow — Qwen4B operator guide

**Generated from:** `DS9 LaserDisc OpenCode Runbook.md` and this template. The canonical runbook is the single source for technical procedure and thresholds.

## Model operating contract

{{MODEL_GUIDANCE}}

### Required behavior

1. Read `README.md` and this complete generated guide before starting a job.
2. Work one bounded stage at a time. After each command, inspect the exit code and declared artifacts; never infer success from a command being launched.
3. Keep measured values in `episodes/<code>.json`; never copy values from historical examples. Unknowns remain null or unset.
4. Preserve inputs, manifests, reports, and backups. Never delete or overwrite an existing output unless the script's explicit gate allows it.
5. Ask when the DVD choice, cut-PAD mapping, or another user decision is required. Follow every VERIFY/STOP gate, including the no-DVD absolute-sync limitation.
6. If a tool is missing, a capability check fails, or evidence disagrees, stop and report the exact command/output rather than inventing a workaround.

### ACTION / VERIFY / STOP format

- **ACTION:** one command or one bounded inspection, plus the expected artifact.
- **VERIFY:** exact exit status, report fields, count/hash, or gate evidence that establishes success.
- **STOP:** state the failed prerequisite/evidence and ask for help; never guess a number or claim a skipped check passed.

---

## Canonical technical workflow

{{CANONICAL_RUNBOOK}}
