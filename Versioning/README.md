# Versioning archive

This folder collects historical release notes, app-branch change records, and the detailed
workflow-versioning policy. It is reference material, not an episode
instruction source. Use the project-root `README.md`, current runbooks, active manifests, and
scripts for live workflow decisions.

The machine-readable `WORKFLOW_VERSION` marker intentionally remains at the project root;
preflight and scripts read that fixed path. `WORKFLOW_VERSIONING.md` contains the policy and
full release/app-branch index.

## Contents

- `V*-RELEASE.md` — historical release scope and validation boundaries.
- `FROMSTUNTOKILL-V*-APP.md` — app-branch increments, which do not imply a production/W sync.
- `WORKFLOW_VERSIONING.md` — versioning policy and the detailed chronology.
- `FROMSTUNTOKILL-V25-APP.md` — historical V25 scope and validation.
- `FROMSTUNTOKILL-V26-APP.md` — historical V26 stack-reference report-path fix and validation.
- `FROMSTUNTOKILL-V27-APP.md` — historical V27 existing-manifest intake-retry handling.
- `FROMSTUNTOKILL-V28-APP.md` — historical V28 one-click retry and duplicate-plan note.
- `FROMSTUNTOKILL-V29-APP.md` — historical resume fix, GUI simplification, and pause controls.
- `FROMSTUNTOKILL-V30-APP.md` — historical incomplete-job recovery and image-review label.
- `FROMSTUNTOKILL-V31-APP.md` — historical report-review skips and exact-evidence approval reuse.
- `FROMSTUNTOKILL-V32-APP.md` — historical version-tolerant resume and output-restart behavior.
- `FROMSTUNTOKILL-V33-APP.md` — historical automated Orc/EFMSink decode and provenance handoff.
- `FROMSTUNTOKILL-V34-APP.md` — historical audio evidence automation and no-DVD promotion handoff.
- `FROMSTUNTOKILL-V35-APP.md` — historical report-bound A/V timing samples.
- `FROMSTUNTOKILL-V36-APP.md` — historical visual DVD/LD alignment adjudication and boundary handoff.
- `FROMSTUNTOKILL-V37-APP.md` — current verified cadence generation and visual review.
