<!-- AUTO-GENERATED: run scripts/generate_runbooks.py. Edit this template and the canonical runbook, not the generated output. -->
# DS9 LaserDisc Workflow — Qwen4B operator guide

**Generated from:** `DS9 LaserDisc OpenCode Runbook.md` and this template. The canonical runbook is the single source for technical procedure and thresholds.

## Model operating contract

Use short, literal ACTION/VERIFY steps. Copy commands only from the current canonical workflow; do not infer missing tool output or claim that a background job completed. If context is low, stop at a verified stage boundary, summarize the exact state and next command, and continue from the saved manifest/log.

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

# Canonical technical procedure
**Plain-text companion:** `DS9 LaserDisc Plain Text Instructions.txt`

This is a repeatable process, not a license to copy Emissary-specific trims or timing. Anything labeled **MEASURE** must be measured for the current episode. The final product is an upscale-ready MKV; do not launch Topaz automatically.

## At a glance

1. Confirm the episode/archive/DVD; inventory and hash TBC, JSON, and raw EFM inputs.
2. Build the weighted ProRes master from the complete capture set (the count varies); require stack evidence, expected frame count, and zero decode faults.
3. Measure cadence, repair only identified interlaced inserts, measure head/tail trims, and keep the frame domains consistent.
4. Decode raw EFM; measure absolute A/V offset and rate per episode; correct only the measured drift.
5. Measure the DVD↔LD picture map, then prepare replacements for only confirmed post-IVTC `PAD_1` blinks in the source-derived AVS.
6. Validate without encoding; encode once to x265 CRF 14 / slow / tune grain / Main 10, then stream-copy mux optional tracks.
7. Piecewise-retime DVD 5.1 and English SRT; full-decode and QC the final output.
8. Leave Topaz for manual use.

The numbered sections below are the actual procedure and gates; this list is only the orientation/quick review.

**Live-run authority:** `V*-QUEUE.md` files are planning records, not episode instructions or
evidence. Do not read, cite, or use queued proposals as measurements, approvals, stage
arguments, or gates while running an episode. Follow this current runbook and README plus the
released scripts matching `WORKFLOW_VERSION`; queue content has no effect until explicitly
implemented and released.

## Work ahead while the source transfer runs

The transfer is the clock, not a reason to wait idle. After the download-specific checks pass,
start the resumable transfer and work through every independent preparation task. Create/reuse
the stable device/episode workspace and copy `templates/episode-prep-checklist.template.md`
to `<instance_root>/logs/prep-checklist.md`. Record task status, evidence paths, and the exact
artifact or gate that unblocks deferred work.

Allowed during transfer: tool/version inventory and the user's update decision (apply changes
only after transfer completion), DVD copy/hash/stream/PTS/black/silence preparation, workspace
setup, readiness checks, stage space estimates, and staged commands. Do not initialize the
episode manifest or perform EFM decode/capture choice, stack/reference work, cadence/trims,
LD-dependent picture/audio measurements, PAD decisions/repairs, retiming, encode, or delivery
QC until the download manifest verifies every selected source file and the relevant readiness
gates pass. DVD-only prep never authorizes guessing an LD value. The checklist records progress;
it does not waive a gate.


## 1. OpenCode job brief and machine setup

**Start with `README.md` in this folder.** It is the current OpenCode entry point and
machine setup contract. This document remains the detailed procedure; the Qwen
variants are alternate presentation styles, not separate workflows.

The user supplies the verified Archive item URL, the destination folder for source
files, and a matching DVD episode/version if available. Verify episode/title from the
Archive/capture evidence and DVD when present; ask if they disagree. If no DVD is
available, ask whether the user explicitly wants the LD-only timing path. The final
output folder defaults to `roots.out` in `machine.json`; record any override.

### Tool choice and preservation gate

- `execute`/Code Mode in this OpenCode setup is JavaScript, not a Python or PowerShell
  terminal. If Python is sent there and the tool reports an `import`/`export`
  `sourceType: module` parse error, switch to `shell`; do not retry Python in `execute`.
- The Windows shell is PowerShell 5.1. Avoid `&&`, Unix/cmd flags, and textual
  `timeout 30s; command` wrappers. Configure timeout/background in the tool call and
  run destructive operations as a separate call with a verified full path.
- Do not treat truncated preflight output as success. Require the full `PREFLIGHT OK.`
  footer and exit code 0.
- “Clean up” does not mean permanent delete. Only the exact user-named download
  directory can be considered. Never infer a DVD, reference master, output, or sibling
  folder as a substitute target. Default to Recycle Bin; permanent removal requires
  explicit confirmation for that exact path. If it is already missing, report that
  fact; `PathNotFound` is not proof that deletion succeeded.
- Before any source cleanup, verify the final delivered file and current on-disk QC
  report, preserve the download manifest/QC provenance, and check that the target is
  under the configured downloads root—not `roots.dvd_root`, `roots.prores`, `roots.out`,
  or `reference-runs`.
- `download-manifest.csv` proves only source file size/MD5 verification; it is not
  delivery QC. Verify the delivered media and current `qc_report.py` output on disk,
  not from prior conversation memory or manifest presence.

The source download may start after its downloader-specific profile/curl, Archive
metadata/integrity and space checks; do not wait for the full tool update gate. Complete
the cross-platform key-tool inventory during the transfer, but apply no updates until it
finishes. Before processing/rendering, apply the user's update choice and run the full
capability/doctor gates. In device-workspace mode, keep the profile outside the README
folder and run Windows preflight with `-Profile <device-profile>`; on Mac use the
platform-specific checks in `PORTING.md`. Set undiscoverable roots and rerun Windows
preflight until it ends in `PREFLIGHT OK.` **and exit code 0**. Do not truncate the log,
infer a pass from partial output, or treat exit code 1 as expected. Do not replace
profile-resolved paths with drive-letter constants.
Record `WORKFLOW_VERSION` in the device/job log before starting. Confirm it has not changed
before every stage; if core files or the version change mid-job, pause before the next
stage, re-read its current instructions, and reconcile rather than mixing procedures.

### Per-device workspace and logs (every README location, V13)

The workspace belongs beside the README that started this episode, not to a hard-coded W:
share. In the device-local profile set `shared.enabled=true` and `shared.release_root` to
that README/code-bundle folder.
Treat every other file there as read-only: templates, scripts, runbooks, curated `Cut Pad
Images/`, `episodes/_template.json`, and other devices' workspaces are not episode output
locations. The permitted data subtree is:

```text
<folder containing the selected README.md>/
  devices/<device>-<stable-id>/episodes/<code>/
    manifest/  logs/  references/  cut-pad-images/
```

Creating/reusing this stable device folder and episode workspace is a **required setup instruction, but
not a processing gate**. Prefer
`python scripts/shared_workspace.py ensure --profile <local-profile> --episode <code>`.
`run_stage.py` also tries to reuse/create it automatically. If the README-folder `devices/`
path is missing or inaccessible, the tools warn and continue in an isolated device-local
fallback beneath the profile's `roots.logs` (or `roots.work`); they never fall back to
writing elsewhere in the README/release tree. Fallback artifacts remain local, not visible
to other devices. Pass the returned `instance_root` to `run_stage.py` to resume the same
workspace; do not run two instances against one root concurrently.
Initialize/reuse the workspace before creating the episode manifest; `init_episode.py` and
`run_stage.py` also do this automatically. If a legacy `episodes/<code>.json` already exists
from a pre-workspace run, preserve it and reconcile/migrate it deliberately at a safe
boundary—never guess which device owns it or overwrite it automatically.

In shared mode, keep `machine.json` outside the README folder and set
`shared.enabled=true`, `shared.release_root=<folder containing this README>`, and a device
name in that local profile. The stable suffix is persisted in a machine-local identity file
under `~/.config/ld-pipeline/device-identities/`, outside the release tree; episode data stay
under per-episode folders beneath that stable device folder. If legacy suffixes are ambiguous
or unverifiable, the helper does not guess, move, or delete them; explicitly select the intended
`shared.device_instance_id`/`LDP_DEVICE_INSTANCE_ID` or use the isolated local fallback. The
workspace owns the episode manifest/backups and its logs,
references, and cut-PAD inbox. Work/render/download/cache/final outputs stay on the
profile's local volumes. For direct helpers, use the `LDP_PROFILE`, `LDP_INSTANCE_ROOT`,
`LDP_MANIFEST_DIR`, `LDP_INSTANCE_LOGS`, `LDP_CUT_PAD_IMAGES`, and `LDP_INSTANCE_MODE`
exports returned by `ensure`.

Use `shared_workspace.py publish-reference` for reviewed small reports. Publish a cut-PAD
still only with a verified timestamped filename, source-DVD hash, exact output-PAD mapping,
and `--confirm-mapping`. It stays inside this workspace; only an authorized maintainer may
later promote it into curated `Cut Pad Images/`.

**ACL policy for any shared README folder:** the release root and all source files are
read/execute only. `devices/` is the only writable subtree; each device may create its own
instance folder and modify only that folder and descendants, never sibling device folders.
An administrator must enforce this in the OS/share ACL. The workflow does not change ACLs;
the doctor still blocks processing if the release root itself appears writable. Workspace
setup/fallback is separate from that ACL readiness gate.

**Keep debugging evidence useful:** preserve each unique stage `.log` and `.json` report,
exact command/arguments, workflow version, gate outcomes, full tool output, exit codes, Orc
JSON and stderr, FFmpeg command/version sidecars, hashes, and tool-version/preflight reports.
Keep complete output, not just filtered error lines; never overwrite or clean old runs.
Do not put credentials/tokens in commands. Each workspace `logs/README.md` repeats this
logging contract; path redaction is not a general secret scrubber.

**Record stage durations:** `run_stage.py` logs active elapsed time plus gate/action
breakdowns in each stage report. Time outside a stage invocation—including waiting for a
person to answer a gate—is not counted. For a manual stage, start
`python scripts/stage_clock.py start --profile <profile> --episode <code> --stage <name>`
and save its returned `clock_file`; pause with `stage_clock.py pause --clock-file <clock>`
before asking/waiting for human input, resume after the answer, then stop with
`stage_clock.py stop --clock-file <clock> --result PASS|FAIL|BLOCKED|STOP`. Keep the clock
JSON in the same workspace `logs/` folder.

Run commands from this folder and initialize the measured job variables from the
profile and the three user inputs (do not copy another episode's values):

```powershell
$profile = Get-Content .\machine.json -Raw | ConvertFrom-Json
$ep = '<derive and verify from Archive/DVD>'
$title = '<verified title>'
$url = '<user-provided Archive item URL>'
$source = '<the user-provided download destination; if omitted, derive it in section 2>'
$dvd = '<verified DVD MKV path, or NONE after explicit no-DVD approval>'
$work = $profile.roots.work
$orc = $profile.tools.orc
$ff = $profile.tools.ffmpeg
$fp = $profile.tools.ffprobe
```

- Per-episode measurements and file references: local `episodes\<code>.json`, or
  `<instance-root>\manifest\<code>.json` in shared-release mode.
- Bundle stage scripts: `scripts\`; Orc project template: `templates\stack_template.orcprj`.
- Final gate: `python scripts\qc_report.py --episode <code>`; require exit 0 and inspect
  `roots.probe\<code>-qc-report.md`.
- The detailed E01/E04/E05 figures below are historical regression evidence only. Never
  copy their frame counts, trim values, audio rates/skips, alignment maps, or retime steps.

### Copy/paste job prompt

```text
Read README.md and DS9 LaserDisc OpenCode Runbook.md in this folder.
Before full tool checks, verify the Archive item/title, local destination/profile, PowerShell
and profile-resolved curl, and the downloader's ListOnly metadata/space gate. Start the
verified download without waiting for unrelated tool inventory or doctor checks. While it
runs, complete the PORTING.md tool inventory and ask whether to install/update specific
components, Update all, or Continue anyway. Apply only the user's choice after the transfer;
do not upgrade mid-transfer. All full tool/preflight/doctor gates must pass before processing.
LaserDisc Archive URL: <https://archive.org/details/...>
Save source files to: <absolute folder path>
Matching DVD episode/version (optional; path or NONE): <path or NONE>
If NONE: ask whether to proceed using the LD-only duration/PAD audio path.
If NONE and Cut Pad Images/<episode> contains PNGs: ask whether to use them for confirmed cut PADs.
Final output folder (optional): <path, or use machine.json roots.out>
Derive and verify episode/title from the Archive and DVD. Run the complete workflow,
measure every episode-specific value, update episodes/<code>.json, and use the bundled
scripts. Stop if a tool/input/gate is missing or ambiguous. Do not reuse historical
numbers or overwrite files. Preserve all assets by default. If I explicitly request
cleanup, use only the exact download-folder path and Recycle Bin; never delete a
reference DVD. Permanent deletion requires a separate confirmation for that exact path.
Do not launch Topaz. Deliver only after QC exits 0.
```

The bundle is Windows x64 and measurement-led, not unattended. In particular, audio
rate fitting and DVD picture-map/silence-edge adjudication require the detailed
procedure. `scripts\retime_dvd_assets.py` applies only the measured manifest schedule;
if alignment/edge measurements cannot be validated, stop rather than using an E04/E05
script.

**One-off measurement-script check:** before trusting scratch analysis or recording its value
in the manifest, state the input units and sign/scale convention; print representative raw
rows before aggregate statistics; verify one row against a known episode anchor; and compare
the result with an independently derived measurement. For correlations, say explicitly
whether a value is similarity (`NCC`) or distance (`1-NCC`); for offsets, define the direction
and subtraction order. Empty output, implausible magnitudes, or disagreement means STOP and
reconcile the analysis. These checks supplement the bundled helper gates; they do not turn
an unreviewed scratch script into an authoritative measurement.

## 2. Archive pages and input inventory

Known Internet Archive item pages:

- Emissary Part I: <https://archive.org/details/DS9-S01E01-LaserDisc-Copies>
- Emissary Part II (item is named S01E02): <https://archive.org/details/DS9-S01E02-LaserDisc-Copies>
- A Man Alone, one-part example: <https://archive.org/details/DS9-S01E04-LaserDisc-Copies>
- Past Prologue, one-part example: <https://archive.org/details/DS9-S01E03-LaserDisc-Copies>
- Other items generally follow `https://archive.org/details/DS9-S01E##-LaserDisc-Copies`; verify the page and title.

Use the Archive metadata API through `scripts\download_archive.ps1 -ListOnly` to inventory the exact TBCs, paired JSON, raw EFM/EFM.7z, capture labels, sizes, and hashes. Capture sets vary; trust the complete selected filename list and capture count, not a remembered episode pattern. An Archive metadata title equal to the item slug identifies the item, but does not by itself establish the human episode title; verify it against the supplied DVD. The downloader prints aggregate bytes and a projected free-space gate. Require `SPACE GATE PASS` with `limits.min_free_gb` still available; never estimate total size from the TBC count.

Resolve the download folder once and use the same absolute `$source` path for preview and transfer. If the user did not provide one, derive the profile default:

```powershell
$source = Join-Path $profile.roots.downloads_root ($profile.roots.downloads_template.Replace('{episode}', $ep))
```

Do not lowercase it, remove spaces, or invent a new folder name. Preview with:
`powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\download_archive.ps1 -Url $url -Destination $source -ListOnly`.
Capture the complete process output and save `$LASTEXITCODE` immediately after it exits;
only then filter or truncate the captured output. Piping the running downloader directly
to `Select-Object -First` can close the pipe early and yield a false broken-pipe exit code.

The downloader accepts only Archive item URLs, fetches TBC + paired JSON + raw EFM/EFM.7z (never `.pcm`/`.log`), verifies Archive size/MD5, resumes partial files in the same destination, and never deletes an existing file. It writes `download-manifest.csv` only after all selected files are complete and verified; no manifest during an active/partial transfer is normal.

Launch large transfers with the OpenCode shell tool's **background** option and retain its job ID/log. Do not use a short foreground timeout or a textual `timeout` prefix. Run one downloader per item. If a tool call times out, check whether its process is still alive and sample the active file size twice. A stable size for five minutes indicates a stall only if the same transfer process is confirmed alive. If the process exited, report “interrupted”, then rerun the identical command to resume; do not claim it is still downloading. Preserve the final manifest.

If partial files are found in a different folder from `$source`, do not start a second
destination. Verify whether a downloader is active, inventory the exact partials, then
ask the user whether to resume there (and record the root override) or move them to the
profile-derived folder. Move only after confirming the destination has no conflicts;
do not delete the old partial folder until the resumed download verifies.

### Independent prep while the download runs

Do not leave a multi-hour transfer as dead time. After confirming the transfer is healthy,
use this period for work that does not depend on the LD capture files:

1. **Verify the DVD.** Check the exact supplied path (or an unambiguous exact match under
   the configured DVD root), confirm the episode/title independently of the Archive
   slug, and record file size/hash. If the identity is ambiguous or conflicts, stop and
   ask; never substitute another episode's DVD.
2. **Probe DVD streams and timeline.** Record video codec/dimensions/frame count and
   measured presentation-rate evidence, audio codec/channel layouts/durations, and
   subtitle types/languages. For an available English SRT, record its cue count and
   check cues are monotonic and within the DVD duration. Do not trust `r_frame_rate` by
   itself. If DVD packet PTS are non-monotonic, decode sequentially; never use `-ss`.
 3. **Check readiness.** Confirm the version inventory/update choice made before the
    transfer, profile roots, download/work free-space gates, and preflight/capabilities.
    Do not install updates mid-transfer/episode; if an update is discovered now, defer it
    to the next episode or ask the user whether to pause and redo the update gate. Save
    probe outputs and a short prep record under `roots.probe` or
   `roots.logs`, clearly labeling each fact with its source; do not edit the DVD or place
   analysis products beside user source files.
4. **Protect the transfer and evidence.** Prefer lightweight metadata/probe work while
   downloading. If a full sequential DVD decode or other prep would contend with the
   download's disk/network/system resources, defer it until the transfer completes. Keep
   one heavy job at a time. Record likely optional 5.1/SRT availability only as a
   candidate; leave optional deliverable tracks disabled until their sources and
   retiming data are verified.

Do **not** build, refresh, or render the weighted stack before every capture file is
verified. DVD-only prep cannot measure the DVD↔LD picture map, LD audio rate/offset,
cadence, trims, or retime schedule. Do not use another episode's measurements. Once
`init_episode.py` creates the manifest after download verification, populate only supported
manifest fields with confirmed facts; retain the detailed DVD probe and its provenance in
`roots.probe` / `roots.logs`. Do not add ad hoc manifest keys. Leave uncertain values unset.
If a proposed prep step requires an episode manifest or verified LD artifact, queue it in
the active episode's job checklist (not a `V*-QUEUE.md` release-planning file)
for its proper later gate; never fabricate a temporary manifest to bypass that requirement.

After download, initialize without guessing measurements:
`python scripts\init_episode.py --url <Archive-URL> --downloads <folder> --dvd <DVD.mkv>`.
Confirm the title/capture list and run `python scripts\validate_episode.py --episode <code> --stage inputs` before building the Orc graph. E01/E02 are rejected by the generic initializer because Emissary needs its two-part procedure.

## 3. Build the weighted ProRes master

1. After input validation, run `python scripts\select_stack_reference.py --episode <code>`.
   It builds a scratch project with a deterministic provisional input order, runs Orc's
   non-rendering refresh, maps `suggestedReference` to a capture label, and repeats with
   that capture first until the recommendation is stable (maximum three refreshes). It
    then records the selected `reference` and promotes the refreshed project to the
    manifest's stack-project path. The seed is scratch-only, never a guessed manifest
    value. If the manifest already has a different reference, use `--reselect` only when
    intentionally replacing it; an existing project is never overwritten. Read the
    selection report, verify all captures were analyzed and the final suggestion is input
    1, then record the measured `alignment_map`. If Orc fails, emits no valid suggestion,
    or does not stabilize, STOP; do not choose a reference from noise alone or hand-derive
    offsets. Output is streamed and a heartbeat is logged during quiet refreshes. The helper
    checkpoints only completed passes; if interrupted, resume with
    `python scripts\select_stack_reference.py --episode <code> --resume <logs/checkpoint.json>`.
    The checkpoint verifies the workflow/manifest/profile, capture inputs, Orc executable, and
    completed project/log hashes. Preserve the checkpoint and partial logs; do not restart with
    a different input set or add a harness timeout to a long background refresh.
2. Graph: one `tbc_source` and `frame_map` per capture → `source_align` → `stacker Weighted` → `video_sink`. Keep the measured reference/input order. Sink baseline: `mov-prores`, `prores_profile=hq`, `decoder_type=ntsc3d`, `adapt_threshold=0.1`, `chroma_weight=2.0`, `ntsc_phase_comp=true`, display aspect 4:3. Do not add unmeasured crops or post-stack filters.
3. Before the render, estimate the peak ProRes and scratch-space demand and run
   `python scripts\check_space_budget.py --stage stack-render --planned-gib <estimate>`.
   Preserve `limits.min_free_gb` after the planned writes; do not reuse another episode's
   output size as a default. Render the manifest project with the profile-resolved Orc CLI:
   `orc-cli <project> --process` (resolve `<project>` and executable from the profile/manifest).
   **Render from the local fast volume**: historical runs measured ~39 fps locally versus
   ~4.5 fps on an SMB share. In the first 60–90 seconds check one “Loaded” line per input,
   expected processing frame range, and StackerStage reports with the actual input count on
   every frame (proves a real multi-capture stack, not a pass-through). On completion,
   `Successfully wrote N frames` must equal the refreshed `source_align` output count, with
   zero unrecoverable samples.
4. The stack gate is an evidence review, not a separate StackerStage tool invocation. Use
   the refresh report and render log. Confirm every capture contributes, fit is trusted,
   no single-source-only coverage is unexplained, and predicted stack noise is better than
   the best single capture. If the report exposes pairwise comparisons, reconcile the count
   with the current input/pressing groups (E07's five captures in one pressing produced 10
   pairs, `5 choose 2`; never reuse 10 as a fixed threshold). Where a single-capture
   reference is available, compare it directly; confirm alignment with a small offset sweep
   and inspect pooled residual/correlation/noise reduction. Emissary's correlation
   0.998–0.999 and stack/single noise 0.80–0.83 are examples, not universal thresholds.
   If the evidence needed to rule out pass-through or interpret stack quality is absent,
   stop rather than marking stage 4 complete.

## 4. Cadence, AVS, and episode-specific trims

**TFM compatibility policy:** for workflow purposes, treat TFM as compatible when both
platforms use current supported software. Do not gate cadence on a separate parity flag,
evidence lookup, or clip run. A historical cross-machine test is recorded in
`M5 Mac LaserDisc Project Updates Log.md`, §8, as informational background only. This
assumption does not measure this episode's cadence.

Generate a full-side TFM log from the native 29.97 master: `TFM(order=-1, pp=0, chroma=true, slow=2)`. Add a motion trace for the native-30p gate, then run `scripts\build_map.ps1 -Episode <code>` and read the full summary and `VERDICT`. Measure cadence; do not infer it from `idet` or motion alone. For uniform 3:2 use `TDecimate(mode=1, cycle=5, cycleR=1)` → exactly 24000/1001. If measured interlaced inserts need repair, use only the generated targeted QTGMC map; `FPSDivisor=2` is mandatory for 1:1 replacement. Keep output 10-bit and progressive; do not globally deinterlace, denoise, interpolate, or RIFE. Targeted single-frame `PAD_1` repair is the sole exception; follow §6's PAD-frame procedure, preserve total frame count, and never alter cadence/timing.

**One frame domain for log and encode:** run the TFM log on the audio-aligned clip (`.Trim(pad,0)`, pad = the audio capture's measured alignment offset). The bundled `build_map.ps1 -Episode <code>` uses the same `audio.pad_frames`, measured output `video.head_trim` / `video.tail_trim`, TFM log, and motion trace from the manifest. The motion probe must run on a trim-only AviSynth script (a script that still contains TFM would re-run TFM and overwrite the log); its output is N-1 values (`tblend` drops frame 0), which the map builder pads automatically. It refuses to overwrite existing artifacts unless explicitly forced.

Measure per episode: AVS source `.Trim` alignment offset, front-matter/head trim, tail trim, output duration, `-AudioSkip`, audio capture offset, and any hybrid cadence ranges. The video Alignment Map offset is not the audio skip. Check the AVS frame domain carefully: pre-decimation trims are source/29.97 frames; post-decimation trims are 23.976 output frames. Log what domain each trim uses.

**Head-trim method (measured, not predicted):** (a) `blackdetect` the master head to find the front matter and the episode's first fade-up out of black; (b) map that master frame to an OUTPUT frame by descriptor-matching the encode AVS's output against the master around the region - black frames match each other ambiguously (corr ~0.94 against any black frame), so anchor on the fade-up itself (corr 1.0) and derive neighbors from it; (c) trim so the file starts 3 output frames before the fade-up (the E01/E04 rule); (d) `-AudioSkip` = the content-geometric WAV time of the first kept frame. The earlier `(master_frame - (pad + 1))` convention was one frame short when the EFM origin equals the master-frame pad; prove WAV0 from corrected-WAV vs IVTC duration, then use the measured origin. S01E05 found WAV0 = master frame 35 and used `(1487-35)/30000*1001 = 48.4484 s`. Likewise set `DELTA` to the raw master time of output frame 0 verified by descriptor match; S01E05 used master frame 1487, not 1486. Always remeasure absolute A/V on the delivered file with `python scripts/av_check_output.py --episode <code>`, not just between-track offset. Do NOT predict final frame count from TDecimate arithmetic; use the AVS metadata frame count reported by the non-encoding validate pass. SAR: `79:94` and `121:144` both resolve to 4:3 within 0.02%; preserve the measured display geometry.

**Trim to the DVD timeline, not by eye (adopted after S01E05):** head/tail membership is decided by the measured DVD<->LD picture map, never by whether a passage merely looks like filler.

- **Trim automatically:** anything *before the DVD timeline origin* is LD-only and never ships - disc text cards, the Paramount intro logo, trailers and other front matter. The deliverable still opens on the episode's fade-from-black: output frame 0 sits a few frames inside the opening black and the program fades up into content. Anchor the cut on the first fade-up (measured), not on a guessed front-matter duration, and re-measure per episode.
- **Keep:** anything the DVD itself contains stays - including end-credit text and the Paramount outro logo. Timeline conformity with the DVD is worth more than a shorter file: the piecewise retime of the 5.1 and SRT assumes both timelines describe the same program, and dropping DVD content would shift every later map.
- S01E05 worked example: DVD origin at master ~48.45 s; LD-only head master 0-48.75 s (studio logo/text); DVD opening black maps to master ~48.45-49.82 s; episode fade-up master 49.716 s. Trim = output `.Trim(1161, 0)` (master 49.583 s, 3 output frames before fade-up at output 1164), so no front matter survives. The DVD's own tail is retained unchanged (credit text DVD 2684.5-2719.2 s, Paramount outro logo DVD 2719.3-2723.2 s).

Emissary regression only:

- Part I: output `Trim(1167,70718)`; 69,552 frames / 2900.90 s; head trim 48.631917 s.
- Part II: `.Trim(20,0)` alignment and 61,214 output frames / 2553.134 s after its measured tail cut.
- Other episodes: measure; never paste these constants.

## 5. EFM audio: decode and verify, do not guess the rate

Decode raw EFM with EFMSink; it does not auto-find the sidecar and needs `decode_mode=audio`:

```powershell
$capture = '<capture label from the verified Archive inventory>'
$efmPlan = python scripts\efm_paths.py --episode $ep --work-root $work --capture $capture --check | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'EFM output path check failed; preserve existing decode/report files' }
$efmDir = Split-Path -Parent $efmPlan.output_wav
New-Item -ItemType Directory -Path $efmDir -Force | Out-Null
$efmOut = $efmPlan.output_wav
& $orc --source "tbc_source=input_path='$source\$capture.tbc':efm_path='$source\$capture.efm'" `
  --sink "EFMSink=output_path='$efmOut':decode_mode=audio:report=true"
```

Decode each capture to `roots.work/audio/<episode>/<capture>-efm.wav`; the matching report
is `<capture>-efm.wav.txt`. Never use a shared flat `audio/<capture>` destination across
episodes. Keep the exact capture label from the verified inventory and refuse existing WAV
or report paths rather than overwriting a prior decode.

If needed, unpack `.efm.7z` first. Keep the report and original WAV. Never assume an archive `.pcm` is valid: Emissary’s uploader `L1.pcm` disagreed with fresh EFMSink audio and was not safe to use.

When several captures include raw EFM, decode each candidate and compare reports before
choosing the WAV used for correction. Record post-C2 loss, concealment, uncorrectable and
missing sections, out-of-order sections, and Q-timecode continuity. Select from measured
report evidence; do not select solely from capture label or the summary word `POOR` (that
label can be triggered by missing sections even when data-loss percentages are small).
Preserve all native WAVs and reports. EFMSink report timestamps are `MM:SS:FF` (minutes,
seconds, frames), not `HH:MM:SS`.

After recording the selected capture and `audio.pad_frames`, the report-only
`python scripts\measure_pad_audio.py --episode <code>` compares audio energy in frame-map
PAD_1 windows with nearby controls. It can help resolve whether the PAD frames carry audio;
it does **not** select or recommend `asetrate`, approve a rate, or edit the manifest.

After recording `audio.capture`, set `audio.pad_frames` to that capture's offset in the
measured Orc `alignment_map`. Map the alignment-map index using the **final Orc input order**
in the selector report, not the manifest `captures` inventory order: the selector may put a
different reference first. Cross-check the chosen offset against Orc's per-source
`Padding prepended: N frames` report. This is a native/master-frame alignment measurement;
never substitute `video.head_trim` or `video.delta_s`. E07 verified the mapping against
three historical episodes and selected L1 at Orc input 2 with offset/padding 0; those are
examples, not defaults.

### V6 audio decision — matching DVD or explicit LD-only mode

1. Preserve every native EFMSink WAV/report; compare all available captures. Record the selected capture's Orc input-order `audio.pad_frames`, a **leading capture-alignment offset**, not the total number of frame-map PAD inserts.
2. Once output frame count, trims, and `video.delta_s` are measured, run `python scripts\audio_timing_diagnostic.py --episode <code>`. It reads native PCM, the stack project, and frame map only—not the DVD or DVD picture-alignment path. Record exact EFM samples/rate/duration, stack duration, full/projected PAD counts, structural trim, and fractional endpoint residual. If PAD coverage is ambiguous, also run the report-only `measure_pad_audio.py`; do not treat its energy comparison as a sample-rate recommendation.
3. **If a matching DVD is available**, use the DVD-reference path: build the DVD↔LD descriptor map before final encode, run raw-provisional/robust diagnostics, compare duration/PAD with picture-map and pad-corrected fade evidence, then approve a measured `ld_corrected` and `ld_synced`. Preserve the existing gates and do not use a DVD rate fit as a default.
   Optionally, after the native LD WAV and DVD reference are recorded, run
   `python scripts\ld_dvd_audio_compare.py --episode <code> --wav native` for a report-only
   envelope/content and mix-band comparison. It decodes the DVD sequentially and edits no
   manifest. Its lag is corroboration only; its same-start whole-source band means are
   explicitly **not lag-aligned**. Do not use this report to choose a rate, approve audio, or
   satisfy a gate. A rerun requires a new `--out` path because reports are never overwritten.
4. **If the user explicitly chose no-DVD mode**, run `python scripts\audio_timing_diagnostic.py --episode <code> --promote-dvd-free --report-out <new-unique.json>` after all required LD measurements exist. Use a new report path because the tool never overwrites prior evidence. It runs a stage-specific audio-volume space gate based on the native WAV size, applies the accepted duration/PAD candidate rate and capture-pad geometry, writes a full corrected FLAC plus exact-sample-count `ld_synced`, records hashes/recipe/endpoints, and updates the manifest with a backup. It refuses missing measurements, overwrites, DVD-track selections, and projected endpoint residuals beyond ±1 frame. The E07 A/B result passed at all seven windows (maximum difference 1 µs); that is the regression basis, not a claim that every future no-DVD episode has been externally cross-correlated.
5. No-DVD QC runs `ld_only_sync_check.py`: it verifies the approved report/source hashes, measured capture-pad skip, exact audio sample budget, and delivered packet identity. It must separately report that absolute lip-sync offset was **not** cross-correlated against an external reference. If PAD/endpoint reconciliation or capture-origin evidence is ambiguous, STOP rather than force a rate or offset.

For DVD-backed measurements, `measure_audio_rate.py` labels its fit `44100/slope`; the fade-midpoint fit has a different offset/slope convention. Keep rate and fixed offset separate. Do not correlate across an unmodeled act-step staircase, use raw fade slope on a padded master, or rerun x265 for audio-only corrections.

**Never assume 44.056 kHz because the disc is NTSC.** E06 measured essentially no intrinsic rate change and applied a separate pad stretch; E07's 44056 was an effective padded-timeline correction. Keep native EFM and all candidates. A constant residual after encode is corrected by rebuilding LD audio and stream-copy remuxing; do **not** rerun x265 for an audio-only correction.

Other discarded estimates (292/1875/2500 ppm) came from a reflected FFT lag convention or weak peaks. Correct FFT convention for the scripts used here: `irfft(conj(rfft(a))*rfft(b))`; compute Pearson on only the actual overlapping aligned samples. Correlation >1 is a normalization bug. Silence, repeated music, fade stings, and different mixes can produce false peaks. Use several long windows and a line fit.

## 6. Primary video encode

The final encode is the only full-program x265 encode. Windows uses the source-derived
AviSynth scripts; macOS uses the included VapourSynth chain. Validate metadata plus a
short null decode (no x265), record `video.frames`/`video.duration_s`, finish and validate
source-derived PAD repair, then run exactly one video-only CRF 14 / slow / grain / Main 10
encode (`encode_episode.ps1 -Pass final` on Windows or `encode_episode_mac.py --pass final`
on macOS). The platform remux helper stream-copies the already prepared `ld_synced` and
available tracks, then checks packet identity. If audio changes, rebuild it and remux; do
not run x265 again.

Quality target: grain-preserving HEVC Main 10, 23.976p, no bitrate ceiling. Emissary averaged ~10.35–10.82 Mbps and ~3.2–3.5 GB per part, around 9–10 fps on this machine; results vary. Do not force 15 Mbps CBR, use veryslow, or use NVENC as the final. NVENC p7 15 Mbps is optional preview/reference only. Do not denoise or discard genuine grain.

Geometry: 768x484 native captured raster in this workflow; maintain measured 4:3 display aspect and consistent SAR across parts. Exact 4:3 SAR for 768x484 is 121:144. The current Emissary mux reports 79:94, close but not identical; inspect the actual stream and ensure displayed 4:3 rather than blindly copying stale SAR values. Preserve limited range and SMPTE 170M tags.

Keep the matching final `ffmpeg-command-*.json` sidecar: it records the exact resolved
executable, argument array, and workflow version because the quiet x265 log level may
suppress its settings banner. Verify one video-only CRF 14/slow/grain encode, expected
frame count/duration, and no script error. Keep the `audio-sync-*.json` recipe from
`build_ld_synced.py` for AudioSkip/start-sample/target-sample evidence. After stream-copy
remux, probe final output: HEVC Main 10, 10-bit, 24000/1001, correct raster/SAR/DAR,
color tags, and FLAC LD stereo 44.1 kHz/16-bit. Fully decode/inspect before declaring
complete.

### Source-derived single-frame `PAD_1` repairs (post-IVTC; before the sole final encode)

#### Diagnosis and classification

1. Scan the source-derived `avs_encode` (Windows) or `.vpy` encode chain (macOS) after `TDecimate` and before any HEVC encode with `blackdetect=d=0:pix_th=0.10:pic_th=0.98`, retaining hits shorter than 0.2 s. Parse every `PAD_1` from the current episode's frame map; convert to post-`TDecimate` output frame and match within ±2 frames. Only map-matched frames in `pad-repair-frames.txt` may be replaced; isolated black content with no PAD-map match is not a repair candidate.
2. Confirm each map-matched candidate on the **full source raster** (max/mean RGB or luma), not only a 48x36 descriptor. A true pad is near-zero across almost the whole frame. Exclude dark real content: local E01's 1954.035 s hit (max 45/255) and E04's 33935/65240 hits (max 57/64) are not pads. Check `.tbc.json` decode faults/sequence gaps and compare the aligned DVD at genuine-content blacks.
3. **Cut test uses the two real flanks, not either step to/from black.** For the candidate at output frame `i`, compare `i-1` to `i+1` and calibrate the metric against unambiguous cuts and normal-motion frames for this episode. Black-to-frame differences are large by definition and make a pad look like a cut. In the E03 regression, the across-flank metric separated 382 cuts (min 29.9, p10 32.3) from pads (max 18.1); those numeric thresholds are E03-specific and are not portable to another metric/episode. E04 separately confirmed one cut PAD, frame 26160; do not classify every dark one-frame event as a pad.

#### Fill policy

- **Non-cut PAD:** use the upstream `rife-ncnn-vulkan` engine's RIFE midpoint between the two adjacent real **output-domain** frames. A tested best-quality command (768x484 PNG pair, model already installed) was `rife-ncnn-vulkan -0 prev.png -1 next.png -o pad.png -s 0.5 -g -1 -x -z -m <model-dir>` (`-x` spatial TTA, `-z` temporal TTA; CPU/fp32, UHD and de-duplication off). In the E03 identical-encode A/B at pad 22221, maintained-frame steps were 0.00 then 18.79; RIFE steps were 11.36 then 10.60. RIFE reduced high-frequency energy ~22% on that one frame, so constrain it to proven PADs. A tested CPU+TTA run was numerically tied with GPU fp32+TTA; GPU without TTA differed substantially, and NCNN Vulkan fp16 is the precision risk. Verify the installed engine, model, backend, and PNG color path on the current machine; none is implied by another host's setup. The tested RGB24 PNG path can be converted to YUV420 10-bit for output, but conversion cannot recreate source 10-bit precision.
- **PAD on a cut with a DVD:** never RIFE across two shots. Map through the independently verified DVD↔LD picture map, sequentially decode/IVTC the DVD to 23.976, and accept only a uniquely matching frame. Crop/scale to the LD raster and color-match gently to the prior good LD frame. Publish the reviewed timestamped PNG and provenance into this episode's `devices/<device>-<stable-id>/episodes/<code>/cut-pad-images/` with `shared_workspace.py publish-cut-pad` (include the verified DVD SHA-256, `--confirm-mapping`, and a non-empty `--color-match-note`), then pass the exact PNG as `--cut-pad-image <output-frame>=<PNG>`.
- **PAD on a cut without a DVD:** if the user selected shared cut-pad stills, use only the reviewed `Cut Pad Images/<episode>/DVD-HH-MM-SS.mmm-fNNNNNNNN.png` still whose scene/timestamp maps uniquely to this measured cut PAD; pass it as `--cut-pad-image <output-frame>=<PNG>`. The timestamp is a candidate locator, not proof of alignment. If no unique image match exists or the user declined stills, use `--hold-cut <output-frame>` to copy the immediately previous native 10-bit LD frame. Never cross-shot RIFE or guess a DVD-time mapping.

#### Injection, encode, and validation

1. Repair **after `TDecimate`**, never in the stack or before cadence selection; do not re-render the weighted master. Windows uses `build_padfix_avs.py`/`ReplaceFramesSimple`; macOS uses `build_padfix_vpy.py`/`std.FrameEval`. Both consume the source-derived script and confirmed repair report, assert source/replacement/output frame counts equal measured `N`, and never read an encoded MKV as the repair source.
2. Test several replacement identities across the episode. Output NCC against its **own** PNG should be ~1 and lower against other replacement images; verify both neighbors. The M5 color path reproduces AviSynth BT.601 limited-range 10-bit scaling at the source's own 4:2:0/4:2:2/4:4:4 subsampling. Do not rely on “not black” alone.
3. Complete PAD repairs **before** the final encode. Windows uses `encode_episode.ps1 -Pass final`; macOS uses `encode_episode_mac.py --episode <code> --pass final`. Each writes the single video-only `padfixed_video` with the V5 x265 CRF 14 / slow / tune grain / Main 10 settings. The platform remux helper stream-copies available tracks and checks packet identity. The second-pass encoder is retired.
4. Full-decode/black-scan the delivered MKV. Require zero PAD-aligned single-frame black hits, unchanged frame count/duration, expected streams/defaults, bit-exact copied audio/subtitle packet hashes, and the per-pad identity checks above. Genuine dark content/credits and cuts are not PADs and must remain untouched.

#### Cross-machine/tooling notes

- The E03 regression found 84 map pads, 83 zeroed master fills, and 82 in the delivered file after head trim; all six TBC JSONs were clean and there were no unexplained single-frame blacks. This proved a `PAD_1` fill defect, not an encoder/TBC failure.
- Flowframes 1.42's command wrapper can silently no-op without elevation/dev mode; its model downloader may be GUI-only. Prefer a verified headless engine for automation and check engine/model/backend availability on the current machine. Do not assume another host's GPU enumeration, weights, or binary path transfers.
- Large FFmpeg `select='eq(n,...) + ...'` lists can exceed expression limits (chunk ≤32 terms or seek/extract pairs); `-read_intervals` does not drive `-show_frames`, so use `showinfo` for PTS. Never make A/B copies as hardlinks then overwrite with `Copy-Item -Force` (it writes through the shared inode).

#### Local E01/E04 audit/results

- E01 Part II: 77 L1 map pads; 76 rendered near-black blips, all repaired by RIFE. E01 Part I has no periodic PAD blips; the isolated dark non-map frame is excluded.
- E04: 84 L1 map pads; 81 rendered near-black blips, all repaired: 80 by RIFE and cut PAD frame 26160 by aligned DVD progressive frame 26234 at 1094.191 s. That frame was scaled/cropped and color-matched to the preceding LD frame using stable-background pixels (245,358 pixels; RGB gains 0.941/0.982/0.974). The contact sheet is `Z:\work\probe\dvd-cut-pad-comparison-e04.png`.
- Final outputs and QC evidence are recorded in §11 and under the configured `roots.probe`/`roots.out`. Reusable scripts and the FFMS2/RemapFrames plugins live in this folder's `scripts\` and `avsplugins\` directories. Old `padfix-tools` copies are historical evidence only.


## 7. DVD video alignment, audio/sub retiming, and final mux

The DVD is used for timing and optional streams, not as the picture master. Probe every DVD file; one Emissary regression reference was `roots.dvd_root/DS9S1D1/B1_t00.mkv` (MPEG-2, AC-3 5.1, separate AC-3 stereo, VobSub, SRT). Resolve the actual path from the local profile/manifest. Select streams by type/title, not copied indexes.

### Measure DVD↔LD video map per episode

Create both descriptor grids with `python scripts\extract_video_descriptors.py --episode <code> --source master` and the same command with `--source dvd`. The DVD decode must be sequential (never seek) and retain the validated FFmpeg default rawvideo output-sync behavior. A controlled E05 probe produced the exact 81,654-frame reference with default sync; adding `-fps_mode passthrough` alone produced 68,555, while `-vsync 0` failed. Do not add output-sync overrides, `setpts`, or an `fps` filter. Check output frame counts and 29.97-fps coverage against the source duration before alignment. Then run `python scripts\measure_dvd_alignment.py --episode <code>` and review its range-edge hits, confidence margins, transitions, reverse matches, and scene-cut checks. The Viterbi path is evidence, not an adjudicated cut schedule.

1. Align native 29.97 LD ProRes frame descriptors to decoded DVD video descriptors (48x36 normalized luma is the established low-cost signature). Use a continuity-constrained/Viterbi search; single-frame greedy matches drift/hop on static shots. For video alignment, offsets use the recorded 29.97 descriptor-grid frame index (seconds = grid frames / 29.97); verify the decoded count/coverage before relying on it. The report's sampled master-frame range is authoritative: never extrapolate a head/tail offset outside that range. If the needed anchor is earlier/later, rerun with `--first-master-frame` covering it or obtain an independent local descriptor/content match before setting trims. When mapping to DVD audio or subtitle timestamps, separately respect those streams' actual time bases/PTS. **Sign convention that bit us on E04:** the state is `DVD index - master index` and it is NEGATIVE (the master runs ~1400 frames ahead from LD front matter); positive offsets index past the DVD's end at the tail - first a crash, and if clipped, silently garbage emissions. Cross-check every act-break step against black durations: `step = (DVD black - LD black) * 29.97` frames - E04's DP steps matched this to within threshold noise. But blackdetect durations are threshold-biased (LD analog fades vs DVD digital fades), so read final step values from the DP path at content-rich frames just OUTSIDE each black, not from raw black durations. **But even the DP medians can be fade-biased**: on E04 the dp window 0.15 s from break 8's black read -1466 while two independent 40-50 s content windows and the DVD black-start gave -1451/-1453. The authoritative edge measurement is a long content window (>=2 s clear of fades) matched over +/-40 offsets; use dp and the plateau fit as corroboration. Cache the descriptors (`desc-*.npy`); re-runs then take seconds instead of minutes.
2. Plot DVD-vs-LD offset over time. Constant plateaus with jumps inside black = extra LD black. A slope = rate mismatch; fit a line separately on each plateau. Use `blackdetect` to find candidate boundaries and image matching to verify them.
3. Use this map for both DVD audio and subtitles. A global `-itsoffset` is wrong if there are act-break steps or rate drift. For source DVD→LD mapping, if DVD time advances at ratio `r` per LD second, use `atempo=r` on that DVD audio segment (output duration becomes input/r) and insert silence for LD-only black extensions. Transform SRT cue times through the inverse affine map plus cumulative gaps.
4. DVD 5.1 is a discrete remix; DVD stereo is a separate mix, not a downmix or substitute for the LD’s original Dolby Surround stereo. LD 2.0 must stay default.

Before recording candidate `retime.breaks[].ld_black` intervals, run `python scripts\measure_ld_black.py --episode <code>`. It reports descriptor-luma dark runs at several thresholds and their sensitivity; it chooses no threshold and edits no manifest. Compare with the frame-level picture and `blackdetect` evidence, then record the adjudicated interval.

For candidate break windows in the manifest, run `python scripts\measure_break_edges.py --episode <code>`. It reports long-window content offsets, DP medians, score/runner-up margins, and differences from any recorded offsets. Before/after content windows are clipped clear of adjacent recorded black intervals; if too little textured content remains, the result is `insufficient-texture`, not a measurement. The helper does not decide which edge wins, whether a music-through black is merged, or edit the manifest. Resolve disagreements using the independent methods below; only then record `off_before`/`off_after`.

Emissary-only regression map:

- Part I DVD frame-offset plateaus: −1418/−1438/−1482/−1502/−1530, steps 20/44/20/28 frames.
- Part II first four plateaus: DVD/LD time ratio ~0.999; offset steps 44/34/43/37 frames; short final tail measured 1:1. Part II starts near DVD 2898.930 s.
- Part II local LD black ends used for the tested map: 676.801, 1271.520, 1642.141, 2501.290 s. The generated E01 schedule is `Z:\work\probe\dvd-retime-map.tsv`.
- The old `retime-dvd-assets.py` scripts are episode-specific history. For a new one-part episode, first measure/adjudicate all break edges and record `retime.breaks`, head/tail silence, tail offset, and source SRT in the manifest; then use `scripts\retime_dvd_assets.py --episode <code> --dry-run`, inspect every segment/gap/SRT count, and rerun without `--dry-run` only after it passes.

### Verify the DVD↔LD map against picture before retiming tracks (E03 regression lesson)

An audio-envelope map can locate content but can absorb the LD's own A/V offset into the DVD↔LD map. Before muxing, independently compare final 23.976 LD video with the IVTC'd DVD in the same active-area geometry. Require unique high-motion/high-NCC matches, reverse round-trip agreement, and an independent scene-cut check; report median/IQR, per-act medians, and drift. If picture shows a constant offset `d`, remux only: adjust LD audio, retimed 5.1, and SRT together; do not re-encode video. Record build/effective skips in the segment manifest. In the E03 regression, picture matching found +72 ms; it was corrected with a stream-copy video remux and +72 ms shift to both audio tracks and all SRT times. Measure `d` independently for every episode; never copy that value.

### Retimed DVD audio and English SRT

- Keep original DVD AC-3 as an intermediate/reference. Split at the measured source boundaries; apply segment-specific `atempo`; insert 5.1 silence at measured LD-only black intervals. Use 24-bit FLAC for the retimed track to avoid another lossy encode after AC-3 decoding.
- Retimed SRT should preserve all cue text, sorted times, and episode duration; cues spanning a boundary must remain visually sensible. VobSub is a backup unless its packet timestamps receive the same piecewise retiming. Do not mux it with a global offset.
- After every cut/offset has been independently adjudicated and recorded, run `python scripts\retime_dvd_assets.py --episode <code> --dry-run`; require nonnegative gaps inside the measured black, plausible tempo (0.99..1.01), and all source SRT cues retained. Then rerun without `--dry-run` to create the graph, retimed 5.1 FLAC, retimed SRT, segment map, and output-gap verification JSON. The helper scans the generated 5.1 output at the recorded threshold, coalesces only touching silence detections, and stops on a mismatch. Sub-50 ms gaps are `LIMIT`/unverified, not a confident pass; final QC reports that limitation as a warning.
- After both retimed assets pass validation, set `mux_tracks` and `deliverable_tracks` together to `['ld','51','srt']`; keep `ld_audio_source='ld_synced'`. V6's Windows/Mac remux maps `ld_synced`, `retimed_51`, and `retimed_srt` directly into the declared deliverable; a preassembled `pre_upscale` track container is not required. Without a DVD, leave retime fields unset and both track lists `['ld']`.
- E01 template outputs: `Z:\work\audio\dvd-51-retimed.flac`, `Z:\work\subs\dvd-eng-retimed.srt`. Its filter graph is at `Z:\work\audio\dvd-51-retimed.filter`.
- The bundled retime generator retains the graph as a review artifact and passes it with current FFmpeg `-filter_complex` (FFmpeg 9 does not accept the legacy `-filter_complex_script` option). It uses `asplit` when one DVD audio input feeds multiple trims; never seek the reference DVD with `-ss`.

### Per-episode retime construction (E04-proven)

Cut construction (edge-anchored, zero DVD audio content dropped):

- Cut exactly at DVD audio silence boundaries: the segment before a break ends at `silence_start`, the next begins at `silence_end`. The dropped span is silence by construction (no content lost or replayed); both cuts must satisfy the recorded detector threshold (no clicks).
- Place each cut in output time: `dst = X - off_edge/FPS - DELTA`, where `off_edge` is the DVD-master offset at that break edge and `DELTA` = master time of output frame 0. On E04 `DELTA = 1485/FPS = 49.5495 s` (frame 1485 = first kept frame; AudioSkip `(1485-4)/FPS` gives the identical content timeline for the LD stereo; output-vs-master descriptor matching confirms within +/-1 frame).
- Inserted gap = `silence_duration - step/FPS` (exact identity; print it both ways as a check). E04 gaps: 2.315/0.572/0.360/0.312/0.127/4.068/0.040 s, every one inside its LD black. If a step is longer than the silence the gap goes negative - do not force it: re-adjudicate the edge offsets first (on E04 the negative gap vanished once the fade-biased dp edge was replaced).
- Segment `atempo` = src_span/dst_span of the two anchored ends (E04: -1448/-598/-1033/-1035/-1007/-1070/-1002/0 ppm - do NOT reuse one plateau's rate for all segments).
- A break with no DVD audio silence (E04 break 2: music plays through the black, step ~0) = NO cut: merge the regions on both sides into a single segment; the DVD black passes through the plateau affine untouched.
- Head segment starts at `DELTA` mapped back to DVD time (lands inside the DVD head silence); tail segment ends at `(TOTAL + DELTA) + TAIL_OFF/FPS` using a content-verified constant tail offset; `apad=whole_dur=TOTAL` with `TOTAL = frames*1001/24000` exactly (E04: 65,347 -> 2725.514458 s - use this exact value for `-t` and `apad`, never a rounded 2725.55).

Adjudicate every break-edge offset three ways before trusting it:

1. Long-window content match: 40-50 s of master content just outside each black (>=2 s clear of fades) vs DVD descriptors over +/-40 offsets, mean cosine per offset (E04: corr 0.88-0.96, n~1400); drift-correct the window average to the edge using the plateau slope (windows span ~50 s, so ~1.5 frames of drift).
2. Plateau LS fit of the npy path (E04: rms <= 1.4 f on A0/A2-A5).
3. DP/npy median near the edge, and DVD `blackdetect` black STARTS as a fourth opinion (agree within 0-3 frames = LD fade lag; black DURATIONS stay threshold-biased).

Two E04 disagreements that mattered: (a) dp at break 8 read -1466 vs true -1451 (fade bias) - trusting dp forced a cut into audio content and a negative gap; (b) the npy path trended +0.224 f/s through the end credits (+7484 ppm) while four content sub-windows read a constant -1424 - the trend was a Viterbi credits artifact and would have pushed the tail cut past the end of the audio. When fit, dp, and content disagree, content windows win.

Probe DVD stream reality before converting indices to seconds: E04's DVD MKV stream is ~24 fps (65,675 decoded frames) while the validated FFmpeg descriptor output is on a 29.97 grid (81,747 rows). The descriptor index divided by the verified output-grid FPS is the alignment timebase; it is not the native DVD stream frame index. Count decoded/output frames and verify duration coverage rather than assuming `r_frame_rate`, `avg_frame_rate`, or one PTS sample is sufficient.

Graph/mux gotchas: the generated graph ends in `[out]` - pass `-map '[out]'` or ffmpeg fails with "output unconnected". Strip newlines from the graph text before `-filter_complex`. Encode the retimed track to 24-bit FLAC (`-sample_fmt s32 -bits_per_raw_sample 24`, `anullsrc=r=48000:cl=5.1` for gaps).

Validation gates for the retime: record the `silencedetect` noise/duration parameters used to measure each source DVD-silence boundary, and use those same parameters when checking the retimed FLAC. A different threshold can move a fade boundary by hundreds of milliseconds and create a false retime failure (E06: -45 dB measurements checked at -40 dB); remeasure the schedule if the threshold changes, do not shift the schedule to hide the mismatch. With matching detector parameters, require planned gap positions within threshold timing (~70 ms) and durations exactly. The SRT must keep every cue, be monotonic, and remain inside `[0,TOTAL]`; `python scripts/sync_check.py --episode <code>` must show near-constant between-track offset within the drift gate. A constant offset BETWEEN the two audio tracks is expected (each has its own fade relationship to video); drift, not a fixed offset, is the defect.

Operational gotchas: do not use the old episode-specific retime generators. Keep reviewed scripts under `scripts\`, use profile roots, and inspect `retime_dvd_assets.py` dry-run output. Use `scripts\sync_check.py --episode <code>` for the manifest-driven between-track check.

Mux pattern, where input 0 is the LD master, input 1 is retimed 5.1, input 2 is retimed SRT:

```powershell
$out = "$work\out\<ep>-<title>-23.976-pre-upscale.mkv"
& $ff -i "$work\out\<LD-master>.mkv" -i "$work\audio\dvd-51-retimed.flac" `
  -i "$work\subs\dvd-eng-retimed.srt" -map 0:v:0 -map 0:a:0 -map 1:a:0 -map 2:s:0 -c copy `
  -metadata:s:a:0 title='LD 2.0 (original Dolby Surround)' -metadata:s:a:0 language=eng `
  -metadata:s:a:1 title='DVD 5.1 (retimed)' -metadata:s:a:1 language=eng `
  -metadata:s:s:0 title='English SDH (retimed)' -metadata:s:s:0 language=eng `
  -disposition:a:0 default -disposition:a:1 0 -disposition:s:0 0 `
  -color_primaries smpte170m -color_trc smpte170m -colorspace smpte170m -color_range tv `
  -t <VIDEO_DURATION> -y $out
```

If Topaz drops streams, remux tracks from this pre-upscale MKV afterward without applying the map again.

## 8. One-part vs. two-part output

S01E01 Emissary only: encode Part I and Part II separately, then concatenate **those two episode parts** by stream-copy video. Join LD audio at sample/PCM level into one FLAC before mux; verify frame counts and the join. Then map DVD audio/subtitles across the combined timeline.

Every other episode: process one complete part and produce one MKV. Never concatenate capture files or concatenate separate episodes. No automatic Topaz handoff.

## 9. Objective DVD quality comparison

Only compare matched frames after cadence/time alignment and identical active-area geometry; PSNR/SSIM on mismatched frames is meaningless. Compare luma spectrum by spatial-frequency band, edge/sharpness, grain/noise, MPEG blocking, combing, and levels separately. E01 showed similar low-frequency power but more LD high-frequency detail/grain; that is not a reason to denoise. There is no evidence for a seamless episode-wide DVD/LD blend. The useful hybrid is selective: DVD can supply a **verified exact frame** at an LD defect/cut PAD, after 23.976 cadence normalization, active-area crop/scale, and a restrained match to the last good LD frame. Local E04's single cut-PAD test uses that method; its comparison is `Z:\work\probe\dvd-cut-pad-comparison-e04.png`. Do not paste DVD frames globally or trust nearest-time matches without picture confirmation.

## 10. Final QC checklist

Video: Main 10 HEVC, `yuv420p10le`, 24000/1001 progressive, native raster, 4:3 DAR, SMPTE170M/limited, expected frame count and duration. Inspect head, several evenly spaced points, every act break, seam if two-part, and tail. Also scan `blackdetect=d=0:pix_th=0.10:pic_th=0.98`, keep `black_duration < 0.2`: every hit must be either a genuine content fade that exists on the DVD or a confirmed PAD blip to repair using §6. No confirmed PAD_1 black blink may remain in a delivered file; an unexplained one-frame near-black frame is a defect.

Audio/subtitles: LD 2ch 44.1/16 FLAC is audio stream 0 and default; DVD 6ch 5.1(side) is selectable and non-default; English SDH SRT is selectable/non-default. Check sync at head, dialogue points, after each timing step, and end - the acceptance criterion is fade-in offsets CONSTANT across all act breaks (a changing offset = residual rate error, go back to the audio fit) and ~0, within +/-0.1 s on clean breaks; one noisy break (silence flickering on fading music) is not a failure. Check subtitle timestamps are sorted and within duration. Compare audio duration with video; pad a few milliseconds at the end if necessary, not by altering the episode timing map. Objective cross-checks: `silencedetect` on the retimed 5.1 must reproduce the planned act-break gap positions/durations, and an envelope cross-correlation of the two audio tracks (`sync-check-*.py`) must be drift-free vs video (constant inter-track offset is expected).

**Indexed-seek audio QA is mandatory for every final master.** `python scripts/qc_report.py --episode <code>` now seeks to the center of each timeline quarter and decodes a 5-second window for **every audio track**, using both fast input seek (`-ss` before `-i`) and sequential output-seek control (`-ss` after `-i`). Each decoded window must be within 0.15 s of 5 seconds. This is platform-independent FFmpeg QA; do not infer seekability from a full sequential decode. A truncated input-seek window with a full output-seek control is the known Matroska interleave/index failure signature.

**Known fix if seekability fails:** preserve the suspect master (do not delete or overwrite it), then re-run the manifest-driven remux with `-max_interleave_delta 0`. Both `remux_padfixed.ps1` and `remux_padfixed_mac.py` now apply this Matroska muxer option. It disables the default 10-second interleave flush that produced video-only clusters while Matroska Cues indexed video; the tested M5 A/B reduced audio-less clusters from 20 to 0, and the repaired E10 master had 0/1067 audio-less clusters with all packet-identity checks intact. Re-run `qc_report.py` after remux. The QC automatically uses a fresh absolute-A/V report filename, so repeating final QC no longer fails on `av_check_output.py`'s no-overwrite rule.

Before reporting: include final path/size; stream list and dispositions; encode command/settings; measured trims/rate map; frame count/duration; audio-sync results at multiple points; subtitle count/range; any unresolved issue. Do not clean intermediates unless asked; if asked, keep the audio-fixed master, enriched pre-upscale master, and designated references.

## 11. Emissary current regression outputs

The `Z:\` paths in this section are historical locations on the original workstation.
Use `machine.json` and the per-episode manifest for current paths; the numeric values
below are reference measurements, never defaults for a new episode.

- DVD: Emissary regression file under the historical `roots.dvd_root` (`DS9S1D1/B1_t00.mkv`)
- Audio-fixed LD-only master: `Z:\work\out\S01E01-Emissary-23.976.mkv`
- **PAD-fixed:** `Z:\work\out\S01E01-Emissary-23.976-padfixed.mkv` (7,237,820,859 bytes / 6.741 GiB; 130,766 frames / 5454.034 s; Main 10, 768x484, SAR 79:94, SMPTE170M; original 44.1 kHz/16-bit FLAC audio packet MD5 `1453a146c418b0b562f8c8eb3008576f`, unchanged).
- PAD fix: 76 E01 Part II black pads replaced with RIFE 4.26 CPU TTA midpoint frames (`-s 0.5 -g -1 -x -z`); original source left intact. Video packet MD5 `b10fec0a94db5c520528416772252110`. Full video decode/blackdetect passed; **0/76 PAD-aligned single-black hits remain** (`Z:\work\probe\blackscan-padfixed-E01.txt`). The remaining 1-frame near-black events at 1954.035, 2898.854/2898.938, and 5071.567/5071.692 s are dark/seam content, not mapped PADs.
- Enriched pre-upscale master: `Z:\DS9 LD to Upscale\S01E01-Emissary-23.976-pre-upscale.mkv` (moved from `work\out`; confirm the actual path before use).
- Enriched file: 9.05 GiB, 5454.034 s, HEVC Main 10/23.976, LD stereo FLAC default, retimed DVD 5.1 FLAC 24-bit non-default, retimed English SDH SRT non-default.
- PAD-fixed enriched master (stream-copy mux; originals retained): `Z:\DS9 LD to Upscale\S01E01-Emissary-23.976-pre-upscale-padfixed.mkv` (9,760,194,990 bytes / 9.09 GiB; 130,766 frames / 5454.034 s; HEVC Main 10 768x484 SAR 79:94; LD stereo default, DVD 5.1 24-bit selectable/non-default, English SDH selectable/non-default). Full decode passed. Packet hashes match sources: video `b10fec0a94db5c520528416772252110`, LD `1453a146c418b0b562f8c8eb3008576f`, DVD 5.1 `7ec670de64ac1f47d26ef1d40f2d5c9a`, SDH `f638c0c380146b78c8b24173828c7c91`. QC: `Z:\work\logs\qc-E01-padfixed-alltracks-session.log`.
- E01 retime data: `Z:\work\probe\dvd-retime-map.tsv`, `Z:\work\audio\dvd-51-retimed.filter`, `Z:\work\subs\dvd-eng-retimed.srt`.

A Man Alone (S01E04) outputs:

- DVD: `roots.dvd_root/Deep Space 9 - S01E04 - A Man Alone.mkv` (historical E04 regression)
- Download folder + manifest: `Z:\DS9 S01E04 LaserDisc\` (16 verified files, 453.69 GiB)
- Stack master: `Z:\work\prores\S01E04-stack6-master.mov` (83,170 frames, L2-referenced, alignment `1+0,2+3,3+1,4+176,5+32,6+24`)
- Cadence: 93.17 % period-5, 0 unmatched, 5 inserts, repair set 3.07 %, VERDICT uniform 3:2
- Rate-corrected audio: `Z:\work\audio\S01E04-L1-44056.wav` (fit -992.8 +/- 53 ppm)
- DVD alignment: `Z:\work\probe\dvd-align-s01e04.npy` (state DVD-master is negative; base about -1426. DP content-edge steps: +2/0/+27/+43/+43/+27/-85/+37. Use the adjudicated silence-edge schedule in `dvd-retime-map-s01e04.tsv`, not early greedy/black-duration estimates; see §7.)
- Head trim: output `.Trim(1186, 0)` (fade-up = output 1189), AudioSkip 49.416033 s, 65,347 frames / 2725.514458 s (= frames x 1001/24000; use this exact value for `-t`/`apad`)
- DVD retime: schedule `Z:\work\probe\dvd-retime-map-s01e04.tsv`, graph `Z:\work\audio\dvd-51-retimed-s01e04.filter`, retimed 5.1 `Z:\work\audio\dvd-51-retimed-s01e04.flac` (s32/24-bit 48k 6ch, 2725.514458 s), retimed SRT `Z:\work\subs\dvd-eng-retimed-s01e04.srt` (867/867 cues), edge data `Z:\work\probe\edges-longwin-s01e04.npy`
- **Pre-upscale:** `Z:\work\out\S01E04-23.976-pre-upscale.mkv` - 4,663,902,131 bytes (4.34 GiB), container 2725.554 s; HEVC Main 10 768x484 SAR 79:94 tv/smpte170m; FLAC 44.1/16 stereo DEFAULT; FLAC 48k/24 5.1 non-default; subrip non-default; all eng with titles; envelope sync 5.1 vs LD 76 +/- 13 ms (5.1 vs video 0..-35 ms)
- **PAD-fixed pre-upscale:** `Z:\work\out\S01E04-23.976-pre-upscale-padfixed.mkv` (4,694,688,433 bytes / 4.372 GiB; 65,347 frames / 2725.514458 s; same stream order/dispositions/titles/color tags as source). 80 non-cut PADs use RIFE 4.26 CPU TTA; cut PAD frame 26160 uses the matched DVD progressive frame 26234 at 1094.191 s, cropped/scaled and stable-background color-matched to LD frame 26159; E04 dark-content hits 33935 and 65240 were not pads and were left untouched. Original audio/subtitle packet MD5s are unchanged: LD `4aab3478a270854433adf303efc712c4`, DVD 5.1 `ac7f8c4db605397ebf032bf65d58ae8c`, SRT `03192babaddd52b4db340cf9571ef77d`. Video packet MD5 `22606cdbe6d42b755110e197072998a8`. Full video decode/blackdetect passed; **0/81 PAD-aligned single-black hits remain** (`Z:\work\probe\blackscan-padfixed-E04.txt`). The remaining dark hits at output 1415.372 s (max 57/255) and 2721.052 s (max 64/255) are non-PAD content.

E01's completed audio tracks were user-confirmed correctly timed. For every new episode, retain the rule: remeasure the absolute A/V offset and rate; never infer another episode's correction from E01/E03/E04.

### Babel (S01E05) outputs

- DVD: `roots.dvd_root/Deep Space 9 - S01E05 - Babel.mkv` (historical E05 regression; MPEG-2 29.97, 2724.544 s; AC3 5.1 + AC3 stereo, 819-cue English SRT).
- Download folder: `Z:\DS9 S01E05 LaserDisc\` - **12 verified files, 305.82 GiB**: four captures L1/M1/P1/P2, each with TBC + paired JSON + raw EFM (MD5 manifest `download-manifest.csv`). All four JSONs: zero decode faults, no sequence gaps. Archive `.pcm`/`.log` intentionally not downloaded.
- Stack: project `Z:\work\stack\stack-full-s01e05.orcprj`, **M1 is the reference**, alignment `1+0, 2+2, 3+35, 4+29` (a first L1-reference run was saved but discarded: re-order inputs so the recommended reference is first). Master `Z:\work\prores\S01E05-stack4-master.mov` - **83,230 frames / 23.91 GiB / 2777.1077 s**, 768x484 ProRes HQ 10-bit, SAR 121:144. Two pressing groups {M1,L1} and {P1,P2}; `fitTrusted=true`, `fellBackToMean=false`, all four contributing (shares 18/19/27/36 %); predicted stack noise 1.49 codes vs 2.38 for the best single capture.
- Audio: all four raw EFMs decoded by EFMSink (`Z:\work\audio\S01E05-Babel-*-efm.wav`, all valid contiguous Q timecodes, no uncorrectable sections). **P1 chosen** (post-C2 loss 0.0118 %, concealment 0.0005 % - best of the four). Rate measured, never assumed: fade-midpoint fit **-1006.7 +/- 79 ppm**; independently confirmed by the picture map (-1321 ppm) and the corrected-envelope check (-1321 ppm). Applied `asetrate=44056,aresample=44100` -> `S01E05-Babel-P1-44056.wav`, 2775.938118 s (matches the IVTC duration to 1.7 ms, which also proves WAV0 = master frame 35). Native decodes kept.
- Cadence: uniform 3:2 across the side (VERDICT), 92.19 % period-5, **0 unmatched**, 892 combed at mic>80, **6 interlaced inserts** (135-255 frames), repair set **2,855 frames / 3.43 %**; targeted QTGMC only, `FPSDivisor=2`. Evidence: `cadence-summary-s01e05.txt`, `di-map-s01e05.txt`, `full-cadence-s01e05.txt`.
- Head trim: output `.Trim(1161, 0)` (fade-up = output 1164 = master 1490); `DELTA` = 49.616044 s (raw master frame 1487 of output frame 0, descriptor-verified); `AudioSkip` = **48.448400 s** (content-geometric). 65,395 frames / 2727.516458 s exact.
- PAD_1: M1 map has 84 pads -> 1 removed by the head trim, 1 sits inside the act-break black, 1 never rendered (only one source padded it, so the stacker rejected the lone black as an outlier) => **81 isolated blinks, all repaired by RIFE 4.26 CPU TTA**. Full-raster RGB on all 81: `rgb_max<=2`, `rgb_mean<=0.0004`. Cut test: no cut pads (max flank 1-NCC 0.3705 vs this episode's cut range >=0.73; top candidate visually confirmed same-shot). Two single-frame blacks are **content, not pads** (frames 55735 and 65264 - a dark space shot and a cut into a dark Paramount-logo fade, both corroborated on the DVD) and were left untouched.
- DVD retime: **all 7 act breaks agree across all three adjudication methods** (long-window content, DP median, plateau fit). Three further long video blacks (LD 339-351, 2291, 2647) have no DVD audio silence - music plays through - and measured +1/-1/-2 frames of step, so they are merged, not cut. Gaps 1.885/0.619/2.238/1.531/1.735/1.914/1.672 s, all >=0 and inside the LD black (break 3 exceeds by 36 ms, within fade tolerance). SRT `Z:\work\subs\S01E05-Babel-dvd-eng-retimed.srt`: **819/819 cues**, monotonic, in range. Retime graph `Z:\work\audio\dvd-51-retimed-s01e05.filter`, schedule `dvd-retime-map-s01e05.tsv`, edges `adjudicated-edges-s01e05.tsv`.
- **Delivered:** `Z:\work\out\S01E05-Babel-23.976-pre-upscale.mkv` - 4,583,968,064 bytes (4.27 GiB), 65,395 frames / 2727.516458 s, HEVC Main 10 768x484 SAR 79:94 tv/smpte170m. Streams: HEVC default; FLAC 44.1/16 **LD stereo default**; FLAC 48k/24 **5.1(side) non-default**; subrip **819 cues non-default**, all eng with titles. Packet MD5s (bit-exact vs sources): video `ebf9fed0af692f1e3285d61327dea430` (identical in padfixed-video, padfixed and pre-upscale), LD `1d39dfbeb07aedb1cc30a856fb68d227`, 5.1 `21549d9c1c7721678ec6f3d16165ccdb`, SRT `267a9c8b2d1b925dead635034dda549f`.
- QC: full decode 0 error lines; **0/81 PAD-aligned single-black hits** (`blackscan-padfixed-E05.txt`); replacement-image identity own NCC 0.99892-0.99991 vs best-unrelated <=0.66 (`pad-identity-samples.tsv`); bundled QC remeasurement of the delivered file: absolute A/V **-10.3 ms constant, -11.0 ppm, rms 15.9 ms** (range -53.3..-0.3 ms; `S01E05-av-check-S01E05-Babel-23.976-pre-upscale.tsv`); between-track 5.1-vs-LD **+7.8 ms constant, +13.7 ppm, rms 12.6 ms** (range +4..+46 ms; `S01E05-sync-check-S01E05-Babel-23.976-pre-upscale.tsv`). These are measured outputs of the current checker, not defaults.
