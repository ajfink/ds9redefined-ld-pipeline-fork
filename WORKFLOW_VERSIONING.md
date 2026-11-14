# Workflow versioning

**Authoritative workflow version: 5** (`WORKFLOW_VERSION` is the machine-readable
counter). The authoritative bundle is `Z:\ld-pipeline` on the authoring machine.

## What requires a version change

Treat a change as **core** if it changes workflow instructions, stage order, profile or
manifest contracts, scripts used by a required stage, templates, preflight/readiness or
quality gates, download/space behavior, or delivery requirements. On the authoring
machine, every accepted core workflow update increments the integer in
`WORKFLOW_VERSION` by exactly one. Record the same change in the authoring device's
execution/workflow log as `FROM Version N -> Version N+1`, with affected files, reason,
and validation evidence. Do not reuse a version number for different core content.

Machine profiles, measured episode data, logs, outputs, and non-core local configuration
do not by themselves change the authoritative workflow version. Preserve those files
when distributing a bundle update.

## Changes on another machine

Do not independently relabel the authoritative bundle. Record every device-local core
adaptation in that device's own log as a change **FROM its current workflow version**;
include whether it is local-only or proposed for merge, plus affected files, reason, and
validation evidence. When the authoring machine accepts that change into the authoritative
bundle, increment its version and record the source device/version. When another machine
adopts a newer authoritative bundle, log its transition from its current version to the
new version before replacing files; preserve its `machine.json`, episode manifests, logs,
and work data.

Use `templates/device-workflow-change.template.md` for a consistent device-log entry.

## Version history

| Version | Origin | Summary | Validation status |
|---|---|---|---|
| 1 | `LENOVO-LEGION-5`, 2026-11-14; from an unversioned baseline | Integrated the fresh workflow, Mac-validated preflight/download fixes from the M5 log, the tested `build_map_vpy.py` helper, the current descriptor-alignment route, and the corresponding documentation. | Windows-side syntax/unit/synthetic checks are recorded in the device log. The adapted Mac preflight and downloader still require a run on the target Mac; no Mac end-to-end episode pass is claimed. |
| 2 | `LENOVO-LEGION-5`, 2026-11-14; from Version 1 | Added the Windows AviSynth cadence/source smoke gate; made Orc render invocation and stack-log evidence explicit; added stage-level projected-space budgeting, ListOnly exit-code capture guidance, initializer-key regression checks, multi-capture EFM report selection, Orc-input-order `audio.pad_frames` mapping, and workflow-version freeze checks. | Python and PowerShell checks, 12 unit tests, and a local three-frame TFM/TDecimate/LSMASH smoke passed. Current work-volume free space is below the 80 GiB reserve. Mac execution and full E07 QC remain unverified. |
| 3 | `LENOVO-LEGION-5`, 2026-11-14; from Version 2 | Incorporated cross-device fixes: backup existing machine profiles before `-Write`, refuse ambiguous root auto-selection, report Python import failures without aborting preflight, support an optional profile-resolved AviSynth loader, and save exact FFmpeg argument/version sidecars. Added the non-workflow reference pool and index, retime detector-threshold matching, and Temp-intermediate retention guidance. | Windows preflight/AVS smoke and sidecar tests, PowerShell parse checks, and the full unit suite are recorded in the device log. The 80 GiB free-space gate still fails on the current Z: volume; no threshold was lowered. |
| 4 | `LENOVO-LEGION-5`, 2026-11-14; from Version 3 | Moved confirmed PAD repair before the sole final encode: audit and replacement frames now come from the source-derived post-IVTC AVS, the padfix AVS imports that original chain, and the final CRF 14/slow/grain x265 output is encoded once. Replaced the full-side validation encode with AVS metadata probing plus a three-frame null decode; retired the second-pass encoder; restricted replacements to map-matched repair frames. Made “work ahead during downloads” explicit in the job prompt and Qwen runbooks. | **14 unit tests passed**; changed Python files compiled; PowerShell AST parsing passed. Temporary source-import PAD (including source-format match), no-PAD wrapper, non-encoding validate, and final-command dry-run checks passed. No full episode encode; Mac VapourSynth path remains unadapted/unverified. |
| 5 | `LENOVO-LEGION-5`, 2026-11-14; from Version 4 | Made audio timing an explicit pre-video decision: added a DVD-free duration/PAD diagnostic with separately reported full-map/trimmed PAD counts and optional disposable candidates; added robust Theil–Sen/leave-one-window-out checks to the DVD-picture diagnostic; added sample-accurate `ld_synced` production with recipe/hash evidence; made final x265 video-only and audio-only remux/AB checks possible. Preserved DVD-based audio as the default pending head-to-head evidence. | 19 unit tests passed; Python compilation and PowerShell parsing passed. Synthetic duration/PAD, robust-fit, saved-report-only, sample-offset, video-only encode, and stream-copy remux checks passed. No E07 candidate-rate A/B test or full encode was run; M5 audio behavior remains untested. |

Version records are in `W:\ld-pipeline-execution-log.md`: Version 1 §16, Version 2 §17,
Version 3 §§18–19, Version 4 §21, and Version 5 §§22–23. E07 findings are in `Z:\S01E07-flow-log.md`.
The M5 source/adoption evidence is in `W:\M5 Mac LaserDisc Project Updates Log.md`;
its last recorded adoption is Version 2, and its current on-device marker still needs
confirmation before any sync. Record future adoption or local changes FROM that machine's
actual current version.
