# Workflow versioning

**Authoritative workflow version: 7** (`WORKFLOW_VERSION` is the machine-readable
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

`V7-RELEASE.md` records the current V7 tooling additions and platform-validation
boundaries. The historical V6 queue was retired and removed in the shared-release follow-up;
its former proposals are not workflow instructions or permission to bypass an accepted gate.

## Version history

| Version | Origin | Summary | Validation status |
|---|---|---|---|
| 1 | `LENOVO-LEGION-5`, 2026-11-14; from an unversioned baseline | Integrated the fresh workflow, Mac-validated preflight/download fixes from the M5 log, the tested `build_map_vpy.py` helper, the current descriptor-alignment route, and the corresponding documentation. | Windows-side syntax/unit/synthetic checks are recorded in the device log. The adapted Mac preflight and downloader still require a run on the target Mac; no Mac end-to-end episode pass is claimed. |
| 2 | `LENOVO-LEGION-5`, 2026-11-14; from Version 1 | Added the Windows AviSynth cadence/source smoke gate; made Orc render invocation and stack-log evidence explicit; added stage-level projected-space budgeting, ListOnly exit-code capture guidance, initializer-key regression checks, multi-capture EFM report selection, Orc-input-order `audio.pad_frames` mapping, and workflow-version freeze checks. | Python and PowerShell checks, 12 unit tests, and a local three-frame TFM/TDecimate/LSMASH smoke passed. Current work-volume free space is below the 80 GiB reserve. Mac execution and full E07 QC remain unverified. |
| 3 | `LENOVO-LEGION-5`, 2026-11-14; from Version 2 | Incorporated cross-device fixes: backup existing machine profiles before `-Write`, refuse ambiguous root auto-selection, report Python import failures without aborting preflight, support an optional profile-resolved AviSynth loader, and save exact FFmpeg argument/version sidecars. Added the non-workflow reference pool and index, retime detector-threshold matching, and Temp-intermediate retention guidance. | Windows preflight/AVS smoke and sidecar tests, PowerShell parse checks, and the full unit suite are recorded in the device log. The 80 GiB free-space gate still fails on the current Z: volume; no threshold was lowered. |
| 4 | `LENOVO-LEGION-5`, 2026-11-14; from Version 3 | Moved confirmed PAD repair before the sole final encode: audit and replacement frames now come from the source-derived post-IVTC AVS, the padfix AVS imports that original chain, and the final CRF 14/slow/grain x265 output is encoded once. Replaced the full-side validation encode with AVS metadata probing plus a three-frame null decode; retired the second-pass encoder; restricted replacements to map-matched repair frames. Made “work ahead during downloads” explicit in the job prompt and Qwen runbooks. | **14 unit tests passed**; changed Python files compiled; PowerShell AST parsing passed. Temporary source-import PAD (including source-format match), no-PAD wrapper, non-encoding validate, and final-command dry-run checks passed. No full episode encode; Mac VapourSynth path remains unadapted/unverified. |
| 5 | `LENOVO-LEGION-5`, 2026-11-14; from Version 4 | Made audio timing an explicit pre-video decision: added a DVD-free duration/PAD diagnostic with separately reported full-map/trimmed PAD counts and optional disposable candidates; added robust Theil–Sen/leave-one-window-out checks to the DVD-picture diagnostic; added sample-accurate `ld_synced` production with recipe/hash evidence; made final x265 video-only and audio-only remux/AB checks possible. Preserved DVD-based audio as the default pending head-to-head evidence. | 19 unit tests passed; Python compilation and PowerShell parsing passed. Synthetic duration/PAD, robust-fit, saved-report-only, sample-offset, video-only encode, and stream-copy remux checks passed. No E07 candidate-rate A/B test or full encode was run; M5 audio behavior remains untested. |
| 6 | `LENOVO-LEGION-5`, 2026-11-14; from Version 5 | Added the explicit optional no-DVD route using the E07-validated duration/PAD rate, capture-pad geometry, exact output sample budget, and a distinct QC result that does not claim external absolute A/V cross-correlation. Made DVD input optional at initialization; added timestamped shared cut-PAD stills and no-cross-shot-RIFE handling. Integrated the M5 VapourSynth/macOS helper set, Mac profile example, preflight/tests, direct-source optional-track remux, and fresh-Mac setup instructions. Removed the hard-coded WinGet FFmpeg path from `motion-probe.ps1`; added the low-cost-model design goal and retrospective `changelog.txt`. | E07 candidate and final-format test are documented in `Z:\work\logs\S01E07-v5-rerun.md`; the official V5 E07 manifest/deliverable remain unchanged. V6 unit tests, Python compilation, PowerShell parsing, and a profile-resolved FFmpeg smoke check passed. Mac-only tests were skipped on Windows; M5 stage evidence is cited, but no fresh-Mac V6 end-to-end delivery is claimed. Linux and ARM64 remain untested. |
| 7 | `LENOVO-LEGION-5`, 2026-11-14; from Version 6 | Added the unified read-only `scripts/doctor.py` readiness report, platform-specific direct-Python version locks and explicit `bootstrap_deps.py --install`, an allowlisted dry-run-first `run_stage.py` dispatcher with preflight/manifest gates and unique logs, deterministic Qwen runbook generation, README-relative per-device/instance/episode workspaces with a non-gating local fallback and per-workspace logging guidance, mandatory indexed-seek audio QC, and an early-download path that skips the full doctor while retaining downloader-specific verification/space gates. Both platform remuxers set the M5-verified Matroska `-max_interleave_delta 0` fix; absolute A/V QC reports are uniquely named for reruns. Shared-mode JSON path redaction is safe for escaped Windows drive roots in both report implementations. Shared mode redirects manifests/logs and restricts app writes to each active instance's manifest/logs/references/cut-PAD paths; ACLs remain administrator-managed. Added a Linux x86_64 dependency/environment candidate without claiming workflow support. | Follow-up suite: **60 passed, 7 Mac/VapourSynth-only skipped**; Python compilation, PowerShell parsing, direct-lock check, Windows doctor, runbook generation, and stage dry-run passed. The M5 Mac completed S01E10 end to end, including final remux; post-fix seek windows and all packet-identity checks passed. Fresh-Mac installation, Linux x86_64 workflow, Linux ARM64, and Windows ARM64 remain unvalidated. The W release root still grants the connected account Full Control; no ACL change was made, so shared-mode doctor/stages remain blocked pending administrator ACL enforcement. |

Version records are in `W:\ld-pipeline-execution-log.md`: Version 1 §16, Version 2 §17,
Version 3 §§18–19, Version 4 §21, Version 5 §§22–23, Version 6 §27, and Version 7 §§28–31. E07 findings are in `Z:\S01E07-flow-log.md` and `Z:\work\logs\S01E07-v5-rerun.md`.
The M5 source/adoption evidence is in `W:\M5 Mac LaserDisc Project Updates Log.md`;
its last recorded adoption is Version 2, and its current on-device marker still needs
confirmation before any sync. Record future adoption or local changes FROM that machine's
actual current version.
