# Episode transfer-time prep checklist

> This is a tracking artifact, not workflow authority. Use the current README, runbook, and
> released gates. Never enter guessed episode measurements or treat a completed checklist
> row as approval to bypass a gate.

| Field | Value |
|---|---|
| Episode / verified title | `<SxxExx> / <title>` |
| Workflow version | `<WORKFLOW_VERSION>` |
| Device / workspace | `<device name> / <instance_root>` |
| Workspace storage mode | `shared-release / local-fallback` |
| Archive item | `<verified identifier>` |
| Download-manifest status | `pending / verified (time, row count, total bytes)` |
| Matching DVD | `path or NONE` |
| Prep started / updated | `<timestamps>` |

## Allowed while transfer is running

| Task | Status | Evidence path / result | Exact blocker or next action |
|---|---|---|---|
| Confirm Archive item/title, local destination, download inventory and space gate | `pending` | | |
| Tool inventory/version gate and user decision | `pending` | | Apply approved changes only after transfer |
| Copy/hash/probe matching DVD; record stream, PTS, black/silence facts | `pending / N/A` | | DVD facts do not establish LD measurements |
| Ensure the stable device/episode workspace and preserve its exports | `pending` | | |
| Run readiness/doctor checks and record exact blocking checks | `pending` | | |
| Estimate space for planned stages from this host's evidence | `pending` | | Recheck at each stage gate |
| Stage next commands and stage-specific evidence destinations | `pending` | | |

## Blocked until the download manifest verifies every selected source file

| Stage/task | Status | Exact artifact/gate required to unblock | Evidence/result |
|---|---|---|---|
| Initialize/update episode manifest | `blocked` | Verified manifest rows, sizes, hashes, and total bytes | |
| EFM decode and capture selection | `blocked` | Verified source inputs and EFMSink reports | |
| Stack/reference/render | `blocked` | Inputs gate and measured reference selection | |
| Cadence, trims, picture map, audio rate/origin | `blocked` | Required LD captures and measured stage prerequisites | |
| PAD classification/repair and retiming | `blocked` | Source-derived chain and episode-specific evidence | |
| Encode, remux, and delivery QC | `blocked` | All prior gates and verified deliverable inputs | |

## Handoff after transfer verification

- Download manifest: `<path>` — verified rows: `<n>`; total bytes: `<bytes>`; completion time: `<UTC>`.
- Re-run/record the input and readiness gates before any blocked stage starts.
- Unresolved prep items and their exact owner/blocker: `<list>`.
- This checklist records status only; measurements and approvals belong in their prescribed
  reports and the active episode manifest after their gates pass.

## Delivery closeout (fill only after final QC)

- Detailed QC report: `<path>`; SHA-256: `<digest>`; result: `<pass/warn/skip/fail counts>`.
- Device QC closeout: `<episode logs>/<code>-qc-closeout-<run-id>.md`.
- Critical source retention at QC time: `<present/missing for DVD, selected EFM/WAVs, retimed tracks/SRT>`.
- If this run used `local-fallback`, shared sync status: `pending / complete / conflict`; record
  the `shared_workspace.py sync-local` report and leave any conflicts untouched for manual review.
