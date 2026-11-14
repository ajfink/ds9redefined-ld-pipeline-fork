# DS9 LaserDisc OpenCode Operator Pack

**Workflow version: 5** — check `WORKFLOW_VERSION` and follow the core-change/device-log
rules in `WORKFLOW_VERSIONING.md` before editing or syncing workflow files.

**GitHub source-only note:** this repository intentionally omits machine profiles, measured
episode data, reference archives, media/caches, and third-party AviSynth DLLs. See
`GITHUB-EXPORT.md` before treating a GitHub clone as a complete runnable bundle.

**Start here.** An OpenCode instance given this folder and the three job inputs
below should read this README, then follow `DS9 LaserDisc OpenCode Runbook.md`.
The runbook is the detailed technical procedure; this README is the machine setup,
job contract, stage index, and stop/go checklist. If the active model is local
Qwen3.5 4B, also follow `DS9 LaserDisc OpenCode Runbook - Qwen4B.md` for its explicit
ACTION/VERIFY steps. If the configured fallback `opencode-go/gpt-6-luna` is available,
use `DS9 LaserDisc OpenCode Runbook - Qwen4B-Luna.md` for its narrowly gated fallback
protocol. The main runbook remains the technical source of truth.

This is a **Windows x64 OpenCode workflow pack**, not a single unattended encoder.
OpenCode performs episode-specific measurements and adjudications, records them
in a manifest, runs the bundled helpers, and stops at ambiguous or failed gates.
Topaz is never launched automatically.

---

## The job prompt

The user only needs to provide the Archive item URL, the folder in which the
LaserDisc files should be saved, and the matching DVD path. Episode/title should
be independently verified from the Archive metadata and DVD; ask if they disagree.
The final destination defaults to `roots.out` in `machine.json`; the user may
supply an override.

If the supplied DVD is outside the configured `roots.dvd_root`, set that root to the
DVD library folder for this job, rerun preflight, and record the exact DVD file in
`files.dvd`. This is machine configuration, not an episode timing measurement.

```text
Read README.md and DS9 LaserDisc OpenCode Runbook.md in this folder. Run the
workflow; do not stop after downloading.
Before setup/download, follow the Windows or macOS key-tool update check in PORTING.md.
TFM is treated as compatible when both platforms use current supported software; no
separate parity flag or parity-clip check is required.
If you are local Qwen3.5 4B, also follow DS9 LaserDisc OpenCode Runbook - Qwen4B.md.
Use the Qwen4B-Luna variant and opencode-go/gpt-6-luna only at its explicit fallback triggers.

LaserDisc Archive URL: <https://archive.org/details/...>
Save source files to: <absolute folder path>
Matching DVD file: <absolute path to the correct DVD MKV>
Final output folder (optional; default is machine.json roots.out): <path or blank>

Derive and verify the episode code/title. Preserve inputs. Measure every episode-
specific value; never copy timings from a reference episode. Follow each runbook
VERIFY/STOP gate. Use the bundled helpers and keep machine paths in machine.json
and measured values in episodes/<code>.json. Never delete/clean files unless I name
the exact path and action; reference DVDs, masters, outputs, and reference-runs are
out of scope for download cleanup. If a required stage is missing from
the bundle, build a reusable parameterized helper in scripts/ and test it against
the documented regression data; do not run a hard-coded *-s01e05 or *-e04 script.
Any core workflow change must increment WORKFLOW_VERSION on the authoring machine and
be recorded in that device's log. Other machines log local changes as FROM their current
workflow version; preserve profiles and episode data when syncing.
If a measurement is ambiguous or gates disagree, stop and ask me. Never launch
Topaz. Deliver only after the full delivery QC exits 0, and report the final path,
measured timing, track defaults, and QC report.

Before starting the source download, inventory key tools in PORTING.md. For any missing
or outdated components, show the user the installed/latest versions and ask whether to
install/update specific components, **Update all**, or **Continue anyway**. Apply only
the user's choice and record it. **Do not wait idle for a long download:** as soon as the
transfer is running, work ahead on every useful task independent of the unfinished LD
files. In particular, verify/probe the matching DVD, record its stream and timeline facts,
check machine readiness, prepare episode-specific space estimates, and stage the exact
next-step commands. Keep a short checklist of what is complete and what is blocked on
verified LD files. Do not install/upgrade software mid-transfer, create the episode
manifest, or make LD-dependent measurements before `download-manifest.csv` verifies all
selected files. Save preliminary evidence under `roots.probe` or `roots.logs` and resume
blocked stages immediately after verification.
```

The prompt starts the job; it does not waive machine setup or quality gates.
For Qwen3.5 4B, apply the Qwen4B step format. Route to `opencode-go/gpt-6-luna`
only when a Luna **DELEGATE** rule is triggered after the local checks; if the
fallback route is unavailable, ask the user rather than silently changing models.

---

## One-time setup on each machine

1. Copy this whole folder to the machine. On a different Windows computer, the shipped
   `machine.json` belongs to its authoring PC; run the profile-discovery step:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File preflight.ps1 -Write
   ```

   This initializes/discovers paths; it is not the final readiness pass. It saves an
   existing profile to a unique dated backup before writing; keep that backup. If several
   plausible work roots exist, discovery warns and requires an explicit `roots.work` rather
   than silently selecting one. On Mac, do not
   run the Windows preflight: create a local POSIX `machine.json` as documented in
   `PORTING.md`. Set custom roots that discovery could not determine, especially
   `roots.work` and `roots.dvd_root`; set `upscale_root` only for legacy E01 files.
2. With the local profile paths known, run the Tool-update gate below. If components are
   missing or out of date, present each installed/latest version and ask whether to
   install/update specific components, **Update all**, or **Continue anyway**. Apply only
   the user's choice and record it. On Windows, check Windows x64, Decode-Orc, AviSynth+,
   FFmpeg with the AviSynth demuxer and `libx265`, RIFE engine/model, Python and required
   packages. The bundle includes `ffms2.dll` and `RemapFrames_x64.dll`; machine-specific
   TFM/TDecimate/QTGMC plugins must also be available to AviSynth+. On Mac, use the Mac
   component list in `PORTING.md`.
3. After updates, or after the user chooses **Continue anyway**, rerun Windows preflight
   and require **PREFLIGHT OK** plus exit code 0. On Mac, run
   `python scripts/preflight_mac.py`; resolve required failures, but do not treat its
   capability result as end-to-end workflow validation. `Continue anyway` does not
   waive required capability/QC gates.
4. For a job-specific output location, update `roots.out` or pass it as the manifest's
   file root. Do not modify scripts to insert drive letters.

Windows `preflight.ps1` checks FFmpeg/AviSynth/x265 capabilities and functionally
smoke-tests LSMASHSource, TFM, and TDecimate through the configured FFmpeg/AviSynth path.
QTGMC remains conditional: if measured inserts require it, validate QTGMC and its
dependencies before that repair stage. Preflight also checks RIFE weights, bundled AVS
DLLs, DVD-root mapping, Python dependencies, and free space. The Mac
`preflight_mac.py` checks its applicable FFmpeg/VapourSynth/Orc toolchain, profile roots,
and free space. Neither check is a quality pass or an end-to-end episode validation.

### Required tool-update gate (Windows and Mac)

Before one-time setup and before each new episode, follow the cross-platform **Tool-update
gate** and key-tool matrix in `PORTING.md`. Check the exact versions resolved by that
machine's profile against current upstream releases. If components are missing or out of
date, show each with its installed state/latest version and ask which specific components
to install/update, whether to **Update all**, or **Continue anyway**. Apply only the user's choice and record
it in the machine's local `roots.logs`, then rerun preflight/capability checks. If the
copied profile belongs to another computer, initialize the local profile first, then
repeat the version inventory using its resolved paths. Do not blanket-upgrade packages
or change versions mid-episode. On Windows,
`winget upgrade --source winget` is an inventory check, not
authorization to upgrade everything; check manually managed plugins against their
upstream release pages. On Mac, check the actual VapourSynth Python environment and
upstream source-built plugins, not a different system Python or an unusable package
manager. For workflow purposes, current supported software on Windows and Mac is treated
as sufficient for TFM compatibility. Do not require a TFM parity flag, evidence lookup,
or parity-clip run before cadence. The historical test result is informational in
`M5 Mac LaserDisc Project Updates Log.md`, §8; it is not a workflow gate.

### Tool/runtime contract (Windows OpenCode)

- In this OpenCode setup, `execute`/Code Mode accepts JavaScript only. The error
  `'import' and 'export' may appear only with 'sourceType: module'` means Python was
  sent to the JavaScript parser; it is not a Python dependency or pipeline failure.
  Run Python and PowerShell commands with the `shell` tool.
- The shell is Windows PowerShell 5.1. Do not use `&&`, Unix `dir` flags, `rmdir /S /Q`,
  or prefix a command with `timeout 30s`. Set the timeout/background mode in the tool
  call. Use one command per call: PowerShell continues after `;`, so a failed
  preamble can be followed by a destructive command.
- Capture full preflight output. Success requires both `PREFLIGHT OK.` and exit code 0.
  A truncated first-30-lines view, or exit code 1, is not a pass.
- After one syntax/tool error, correct the shell/tool choice once; do not create ad hoc
  scripts in the working directory or repeat guessed quoting variants. Use native
  `Test-Path`, `Get-Item`, `Get-ChildItem`, `Get-Process`, and `Get-PSDrive` checks.
  Do not scan all of `Z:\` recursively or use invented path-search cmdlets; resolve the
  exact root from `machine.json`, then list only the named directory.
  For example:

  ```powershell
  Get-Item -LiteralPath $dvd | Select-Object FullName,Length,LastWriteTime
  Get-ChildItem -LiteralPath $downloadDir -Force -File | Select-Object Name,Length,LastWriteTime
  Get-PSDrive -Name Z | Select-Object Name,@{N='FreeGiB';E={[math]::Round($_.Free/1GB,1)}}
  ```

### Destructive cleanup boundary

- “Clean up” is **not** authorization for permanent deletion. Default to an inventory
  and proposal; if cleanup is explicitly requested, operate only on the exact
  user-named source-download directory. Never infer a DVD, master, output, or sibling
  directory as an alternate cleanup target.
- Never delete anything under `roots.dvd_root`, `roots.prores`, `roots.out`, or
  `reference-runs` as part of download cleanup. A reference DVD is a separate asset
  even when its filename has the same episode code.
- Verify the exact full path, its contents, the completed download manifest, and the
  on-disk delivery/QC report before cleanup. Preserve small provenance/QC evidence.
  Move to the Recycle Bin by default; permanent deletion requires the user to confirm
  that exact path and permanent action after seeing the inventory/size.
- PowerShell `Remove-Item`/`rmdir` is a permanent removal here, not a Recycle Bin
  action. Do not use it for the default cleanup. If a supported Recycle Bin operation is
  unavailable or fails, stop and ask; never fall back to recursive force deletion.
- `download-manifest.csv` proves source file size/MD5 verification; it is **not** a
  delivery QC report. Read the current `qc_report.py` output and verify the delivered
  file on disk; never substitute prior chat memory or a manifest status field.
- If the target is already missing, report “not found” and stop. Never treat a
  `PathNotFound` error as proof that your prior command succeeded. Never issue
  `Remove-Item -Recurse -Force` as a fallback for a failed Recycle Bin operation. If a
  tool call times out during a mutation, check the exact target/log before any retry.
- When cleanup is combined with a new episode job, keep the tasks separate and record
  cleanup as `done`, `not found`, or `blocked`. A missing download folder is not a reason
  to search for/delete its DVD or another sibling asset. Do not reopen a user decision
  that has already been answered.

### Important current limitation

The portable folder contains a verified Archive downloader, Orc stack-project
generator, cadence-map builder, first-encode helper, manifest-driven DVD retime
generator, PAD repair, remux, and QC helpers. **The full workflow is still
measurement-led, not one-click.** Orc refresh/render are verified with the installed
Orc tool; audio-rate fitting and DVD picture-map/edge adjudication still require the
detailed runbook and episode-specific analysis. The retime generator only applies
adjudicated values already in the manifest; it does not infer them. Never reuse an
E04/E05 alignment or edge script. No README can make a missing measurement safe by
pretending it is automated.

The M5 log records native VapourSynth TFM logging via `vapoursynth-tivtc`, Orc/project
and Python smoke checks, and a `vspipe`→FFmpeg Y4M encode smoke test. It also reports a
Mac-local `build_map_vpy.py` and a successful local preflight. The helper source was
recovered from the byte-verified Mac reference snapshot and is now included in this
bundle. Its M5 test used a ProRes stand-in and does not establish a full episode run.
The current bundled `preflight_mac.py` has passed syntax, `--help`, and the expected
non-Mac guard here, but has not been run on macOS; the M5 42/1 result is from the Mac
local copy. The M5 TFM parity result is historical; current workflow assumes compatibility
when supported software is current and does not require a parity flag or clip. Component
checks are **not** end-to-end Mac validation. Before Windows or Mac setup and before
every new episode, follow the **Tool-update gate** in `PORTING.md`: check upstream
versions, ask the user which detected updates to install (specific components, **Update
all**, or **Continue anyway**), record the choice, and rerun capability checks. Freeze
versions mid-episode; get explicit approval for any older compatibility pin.

---

## Per-job workflow

OpenCode follows the main runbook in order. The expected contract is:

**Pre-job gate:** complete the key-tool inventory, ask for an update choice, apply only
selected updates (or record **Continue anyway**), and run platform preflight in
`PORTING.md` before starting the download. TFM compatibility is assumed when both
platforms use current supported software; no separate parity flag or parity-clip check is
required. Freeze the toolchain and `WORKFLOW_VERSION` for the episode. Record the version
at job start and confirm it before each stage. If workflow files change or the version
increments mid-job, pause before starting the next stage, reread its current instructions,
and reconcile the active job with the new version; do not silently mix procedures.

**Write-stage space gate:** `min_free_gb` is a reserve, not just a download-time check.
Before any stage with significant outputs (stack render, EFM decode, descriptor extraction,
encode, retime, or PAD repair), estimate the peak additional bytes required through the next
check (including temporary/intermediate files) and run:

```powershell
python scripts/check_space_budget.py --stage stack-render --planned-gib <measured-estimate>
```

Use the output volume's profile root and a defensible episode-specific estimate; do not
reuse a prior episode's file size as a default. Recheck after each large output. If the
projected free space falls below `limits.min_free_gb`, stop before the next write and ask
the user to make space or move the work root. Never lower the configured reserve or delete
files without the user's specific authorization. If no defensible estimate is available,
stop and ask rather than treating a passing preflight as a stage budget.

Keep any cache/intermediate needed after the current command under `roots.probe`,
`roots.logs`, or its declared output root. Treat OS `Temp` as disposable scratch only; do
not rely on it across stages or run cleanup while a job is active. If a required cache is
missing, verify and regenerate it before continuing.

1. **Resolve inputs.** Validate the Archive URL/item identifier and inspect the
   supplied DVD with FFprobe. An Archive metadata title that merely repeats the item
   slug is not proof of the human episode title; verify the DVD identity independently.
   Never substitute the Emissary DVD or guess a title.
2. **Preview, space-check, then download.** Resolve one canonical destination from
   the supplied input or profile; never invent a lowercased/renamed folder. For the
   profile default, compute and verify its full path with:

   ```powershell
   $downloadDir = Join-Path $profile.roots.downloads_root ($profile.roots.downloads_template.Replace('{episode}', $code))
   ```

   Run `scripts/download_archive.ps1 -Url $url -Destination $downloadDir -ListOnly`;
    inspect its complete capture-label/file list, aggregate bytes, and
    `SPACE GATE` result. Require the projected free space to remain at least
    `limits.min_free_gb`; do not estimate the total from the number of captures.
    Do not pipe the downloader directly to `Select-Object -First` or another early-terminating
    consumer: it can close the output pipe and corrupt `$LASTEXITCODE`. Capture
    the complete output, save `$LASTEXITCODE` immediately after the process completes, and
    only then inspect or filter it. Accept the preview only with the true exit code 0 and
    `SPACE GATE PASS`.
   The downloader selects TBCs, paired TBC JSON sidecars, and raw EFM/EFM.7z only;
   it checks Archive size/MD5, resumes partial files in that same destination, and
   never deletes an existing file.

   Start large transfers with the OpenCode shell tool's **background** option and
   record its job ID. Do not run the transfer in a foreground call with a short
   timeout, and do not put a textual `timeout` command before it. One downloader per
   Archive item at a time. The manifest is written only after every selected file
   finishes and verifies; its absence during transfer is normal. If the tool call
   times out, first check the returned job/process and sample the active file size.
   If no transfer process remains, call it interrupted—not “still downloading”—then
   rerun the same command to resume the existing partials. A stable size for five
   minutes is a stall only after process identity and two timed size samples confirm it.
    Preserve the final `download-manifest.csv`.

    **Use the transfer time for independent prep; do not wait idle.** If the matching
    DVD was supplied or is unambiguously present under the configured `roots.dvd_root`,
    verify its exact path and episode identity against the Archive item. Probe the video,
    audio, and subtitle streams; record codecs, channel layouts, frame count/rate evidence,
    durations, subtitle languages/formats, and file size/hash. Check the DVD's measured
    presentation timeline rather than trusting `r_frame_rate`; if packet PTS are
    non-monotonic, decode sequentially and do not use `-ss`. If an English SRT is present,
    check cue count, monotonicity, and that cue times lie within the DVD duration. Record
    whether AC-3 5.1/SRT sources appear available, but leave optional tracks disabled until
    the source and retiming data are verified.

    Confirm the dated tool-version report and machine profile/preflight readiness before
    heavy processing. Do not install or upgrade software while the transfer/episode is
    active; if an update is discovered, defer it to the next episode or pause only with
    user approval, then rerun all relevant checks. Save probe output and preliminary notes in
    `roots.probe` / `roots.logs`, clearly marked as prep evidence. If a full sequential DVD
    scan would compete with the transfer for the same disk or system resources, defer that
    scan until the download finishes; do not slow or destabilize the transfer for optional
    prep.

    This prep cannot replace LD-dependent work. Do not build/refresh/render the weighted
    stack until all capture files verify; do not measure the DVD↔LD picture map, LD audio
    rate/offset, cadence, trims, or retime schedule without their required LD artifacts.
    Never copy a prior episode's values. After initialization, populate only supported
    manifest fields with confirmed facts; keep the detailed DVD probe and its provenance
    in `roots.probe` / `roots.logs`. Do not add ad hoc manifest keys. Keep uncertain values
    unset.
 3. **Initialize and validate the episode manifest.** Run
    `python scripts/init_episode.py --url <URL> --downloads <folder> --dvd <DVD.mkv>`.
    It checks the verified download manifest and Archive metadata, records the DVD and
    capture files, and emits capture-independent AVS/DVD-audio paths in `files`. The
     `ld_native` and `ld_corrected` paths depend on the measured audio capture and rate;
     add them only at the audio stage, never invent them during initialization. Confirm
     the title and any uncertain capture metadata, then run
   `python scripts/validate_episode.py --episode <code> --stage inputs`. Set expected
   output file roots/keys, `deliverable`, `mux_tracks`, and `ld_audio_source`. Keep the
   default track set LD stereo only; add DVD 5.1/SRT only when verified sources and
   retiming data exist. Never use zeros as unknown values. Two-part E01 is explicitly
   rejected by the generic initializer/encoder/QC.
 4. **Select the Orc reference and refresh the weighted stack.** Require 2-6 verified
    captures; with one capture there is no weighted-stack gain, and more than six
    requires extending the Orc template. Run
    `python scripts/select_stack_reference.py --episode <code>`. It builds a scratch
    project using a deterministic provisional input order, asks Orc's refresh for
    `suggestedReference`, maps that source index to a capture label, and repeats with
    that capture first until stable (maximum three refreshes). It then records the chosen
    `reference` and promotes the refreshed project; the provisional seed is never written
    to the manifest. If a previously declared reference conflicts, it stops unless you
    deliberately use `--reselect`; an existing stack project is never overwritten.
     Inspect the report (including runner-up, source coverage, stack-noise estimates, and
     fit flags), then record the measured alignment map. Orc's own recommendation is used;
     no noise-only custom score is substituted. The helper only refreshes; it never renders.
     Stop if Orc fails, gives no valid recommendation, or fails to stabilize. Before rendering, run
     `python scripts/check_space_budget.py --stage stack-render --planned-gib <estimate>`
     with the current episode's estimated master and scratch peak. Then render the project
     referenced by the manifest with the profile-resolved Orc executable, for example
     `& $orc $project --process`; launch/monitor it as a long background job. Validate the
     completed artifact, not just the exit code: one `Loaded` line per capture, real
     StackerStage multi-input evidence (and any reported pair count consistent with the
     current input/pressing groups), zero unrecoverable samples, and sink frame count equal
     to the refreshed `source_align` output. E07's five-capture/one-pressing report had 10
     pairs (= 5 choose 2); that is an example, not a fixed expectation. Record stack metrics
     and run `python scripts/validate_episode.py --episode <code> --stage stack`.
  5. **Measure cadence, trims, and capture quality.** Follow the detailed runbook. Generate a
    full-side TFM log and motion trace, inspect any hybrid/interlaced inserts, use
    `scripts/build_map.ps1` to create the cadence/deinterlace map, and then apply the
    *measured* trims. Decode the available raw EFM captures with Orc and compare their
    reports before selecting a source WAV: report post-C2 loss, concealment,
    uncorrectable/missing sections, and Q-timecode continuity. Interpret `POOR` against
    the report definition; missing sections can trigger it even when loss is low. Report
      times are `MM:SS:FF`, not `HH:MM:SS`. Preserve each native WAV/report, then choose
      based on the measured evidence. Run `scripts/audio_timing_diagnostic.py --report-out
      <new-json>` for the DVD-free duration/PAD candidate; it never commits a rate.
      Set `audio.pad_frames` to the selected
     capture's offset in `alignment_map`, mapping by the selector report's final Orc input
     order (not the manifest capture inventory); cross-check Orc's `Padding prepended`.
     Never substitute `video.head_trim` or `video.delta_s`.
  6. **Align DVD and corroborate audio before final encoding.** Decode sequentially; never seek a reference
     DVD with non-monotonic PTS. Extract both grids with
     `scripts/extract_video_descriptors.py --episode <code> --source master` and
   `--source dvd`. It intentionally matches the validated FFmpeg default rawvideo
   sync behavior. In the controlled E05 probe, default sync produced the exact
   81,654-frame reference; adding `-fps_mode passthrough` alone produced 68,555,
   while `-vsync 0` failed. Do not add output-sync overrides, `setpts`, or an `fps`
   filter. Verify descriptor counts and 29.97-fps coverage before running
   `scripts/measure_dvd_alignment.py`, then independently check reverse/scene matches.
     After the picture map and `video.delta_s` are measured, run
      `scripts/measure_audio_rate.py --raw-provisional --out <new.tsv> --summary-out
      <new.json>` on the native WAV; this centers
     windows geometrically and does not approve/write an origin or rate. Compare its robust
     and OLS fits against the duration/PAD estimate and pad-corrected fade fit. Resolve by
      evidence, never by method label alone. Generate the selected `ld_corrected` as a new
      file with `audio_timing_diagnostic.py --rate-hz <selected> --candidate-out <new.flac>
      --report-out <new.json>`; record the approved path as `files.ld_corrected`. Then build
      `ld_synced` with `scripts/build_ld_synced.py --dry-run` and the real audio-only command.
      Do not run an x265 encode to revise audio.
    Put candidate LD black intervals in `retime.breaks[].ld_black` and run
   `scripts/measure_break_edges.py`. These scripts emit evidence; they do not
   adjudicate ambiguous edges or modify the manifest.
   7. **Prepare confirmed PAD repairs before final encoding.** First run the non-encoding
      AVS validation to record `video.frames` and `video.duration_s`. Then run `pad_audit.py`
      against the source-derived `avs_encode` (post-IVTC, still 10-bit), then `fullres_check.py`,
     `rife_padframes.py` for confirmed non-cut PADs, and `build_padfix_avs.py`. Use the
     verified DVD picture map for cut PADs. Only map-matched repair frames may be replaced;
     genuine dark frames stay untouched. This creates `avs_padfix` from the original
     source-derived AVS, never from an encoded MKV.
  8. **Retime DVD tracks.** Adjudicate every silence edge and
   record `off_before`/`off_after`, head/tail silence and `tail_off`. Then run
   `python scripts/retime_dvd_assets.py --episode <code> --dry-run`; inspect every
   segment/gap/tempo and cue count before rerunning without `--dry-run`. It generates
   the piecewise 5.1 FLAC, retimed SRT, filter graph, and map; it refuses missing
   measurements, invalid gaps, dropped cues, or overwriting files.
    9. **Encode video once, then remux audio.** After `avs_padfix` and the measured frame
       count/duration are ready, validate the patched AVS (metadata plus a short null decode;
       no x265 encode), then run `encode_episode.ps1 -Pass final` once. It writes video-only
       `padfixed_video` with x265 CRF 14 / slow / tune grain / Main 10. Independently build
       and validate `ld_synced`, then `remux_padfixed.ps1` stream-copies it with the picture;
       packet identity is checked. Audio-only corrections rebuild `ld_synced` and remux—never
       rerun x265. Do not run the retired `encode_padfixed.ps1`.
  10. **Deliver only after QC.** Run `python scripts/qc_report.py --episode <code>`.
   Require exit code 0 and inspect the report. QC targets the manifest's declared
   deliverable, checks frame count/stream inventory/titles/dispositions/packet
   hashes, PAD blinks, replacement identity, absolute A/V, between-track drift, and
   subtitle timeline. Optional tracks may be omitted only when the manifest says so.

Use `scripts/validate_episode.py --episode <code> --stage cadence|audio|retime|encode|padfix|deliverable`
at the corresponding gates. It reports missing prerequisites without filling them in.

### Download troubleshooting regression

The S01E07 Archive item selected 15 files across five captures: 409.91 GB decimal
(381.76 GiB). A foreground shell call with a 120-second limit ended while curl showed
steady 30–39 MB/s and a multi-minute ETA. The EFM had completed and the TBC was partial;
there was no longer a curl process afterward. That was an interrupted tool call, not a
network stall. The downloader resumes the same partial file on rerun, and does not
write its manifest until all 15 files verify. Use the current ListOnly aggregate and
SPACE GATE for every item; these historical numbers are not a default for another
Archive item. The same attempt used a hand-invented lowercase destination instead of
the profile template and left partials outside the expected folder. If that happens,
stop and resolve the partial folder before starting another destination; do not
silently create a second copy. After adding the aggregate/space summary, ListOnly was
retested for this item: it reported 15 files, labels L1/M1/P1/P2/P3, and
`SPACE GATE PASS`; no new transfer was started during this documentation review.

Every measured value belongs in `episodes/<code>.json`; every generated report goes
to the machine's `roots.probe`/`roots.logs`. If a command fails, inspect its artifact
and logs before retrying. Do not overwrite or clean user files without explicit
permission.

---

## Bundled tools

| Tool | Purpose |
|---|---|
| `preflight.ps1` | Backup-safe profile discovery, ambiguity-safe root selection, and checks; Windows also smoke-tests LSMASHSource/TFM/TDecimate, with QTGMC conditional on measured inserts |
| `scripts/preflight_mac.py` | Mac profile/tool/root/VapourSynth capability check (`--episode` and `--json` supported); checks workflow version and does not prove full workflow readiness |
| `WORKFLOW_VERSION` / `WORKFLOW_VERSIONING.md` | Authoritative workflow version and core-change/device-log rules |
| `scripts/download_archive.ps1` | Metadata-verified, resumable source download; ListOnly prints capture/file totals, aggregate bytes, and a projected free-space gate |
| `scripts/check_space_budget.py` | Projected per-stage free-space gate that preserves the configured reserve |
| `scripts/init_episode.py` | Three job inputs + verified download manifest -> draft episode manifest; no timings guessed |
| `scripts/validate_episode.py` | Stage-specific manifest/artifact prerequisites; optional expensive source MD5 recheck |
| `scripts/select_stack_reference.py` | Orc refresh loop that chooses/stabilizes the reference and writes a refreshed project; never renders |
| `scripts/build_stack_project.py` | Manifest-driven Orc graph skeleton; scratch `--reference-override` does not edit the manifest |
| `scripts/extract_video_descriptors.py` | Sequential master/DVD descriptor extraction on the validated FFmpeg rawvideo grid |
| `scripts/measure_dvd_alignment.py` | Continuity-constrained DVD-minus-master Viterbi candidate path and confidence report |
| `scripts/measure_break_edges.py` | Long-window content offsets, DP comparison, score/runner-up for each candidate break |
| `scripts/measure_audio_rate.py` | Multi-window audio-vs-picture-map diagnostic with OLS and robust checks; never auto-selects a correction |
| `scripts/audio_timing_diagnostic.py` | DVD-free exact-duration/PAD candidate report; optionally creates a separate experimental audio candidate; never edits the manifest |
| `scripts/build_ld_synced.py` | Sample-accurate AudioSkip + exact target sample count into FLAC; audio-only, refuses overwrites and records recipe/hashes |
| `scripts/build_map.ps1` | TFM log + optional motion trace to cadence report, deinterlace map, AVS |
| `scripts/build_map_vpy.py` | Re-express measured cadence outputs as a VapourSynth chain; does not re-measure |
| `scripts/encode_episode.ps1` | Non-encoding AVS validation and the single final video-only x265 encode from the source-derived PAD-repaired AVS |
| `scripts/retime_dvd_assets.py` | Measured manifest break schedule -> DVD 5.1 FLAC, retimed SRT, and retime map |
| `scripts/pad_audit.py` / `fullres_check.py` | Detect map-matched PADs in the source-derived post-IVTC AVS and confirm them at full raster |
| `scripts/rife_padframes.py` / `build_padfix_avs.py` | Generate replacements only for confirmed PADs and wrap the source-derived AVS without changing frame count |
| `scripts/encode_padfixed.ps1` / `remux_padfixed.ps1` | Former second-pass encoder is retired; remux stream-copies tracks with packet-identity checks |
| `scripts/scan_padfixed_black.py` / `verify_pad_identities.py` | PAD-black and replacement identity gates |
| `scripts/av_check_output.py` / `sync_check.py` | Absolute A/V and between-track drift checks |
| `scripts/qc_report.py` | Final delivery report; skipped/unverified required checks fail closed |
| `scripts/regen_plain_text.py` | Rebuild the ASCII companion from the main Markdown runbook |

The example manifests (`S01E01`, `S01E04`, `S01E05`) and `reference-runs/` are
**measured historical regression data only**. They help interpret expected file
formats, thresholds, and known traps. They are not defaults for another episode.

### Non-workflow reference pool

`reference-pool/` archives selected artifacts from another machine for inspection only.
They are **not** scripts, workflow instructions, defaults, or regression gates. Do not
execute archived scripts or copy episode values into a new manifest. The archive's
provenance, SHA-256 fingerprints, and caveats are in `reference-pool/README.md`.

| Reference item | What it contains / why it is retained |
|---|---|
| `reference-pool/S01E03/qc-report-padfixed.md` | E03 padfixed delivery audit with stream/hash/PAD/sync checks; notes a preservation deviation and missing replacement-identity evidence. Historical audit, not a complete golden QC fixture. |
| `reference-pool/S01E03/blackscan-padfixed.txt` | Full-decode E03 `blackdetect=d=0` evidence referenced by the audit. |
| `reference-pool/S01E03/pad-index-validation.md` | E03 known-answer note that analytic PAD indices can be off by one and should be snapped using measured pre-padfix black hits. Reconcile its corrected 37/82 count with the archived script's stale 56/82 comment before using it as a numeric fixture. |
| `reference-pool/S01E06-in-progress/manifest-in-progress.json` | Captive Pursuit measured manifest snapshot; marked `in_progress`, with blank QC fields and non-schema `_measured` extensions. Never install as an active episode manifest. |
| `reference-pool/S01E06-in-progress/NEXT-historical-not-current.md` | E06 working checklist and calculations. It contains machine paths and obsolete commands; it is archived for rationale only, not current instructions. |
| `reference-pool/S01E06-in-progress/measurements/dvd-probe.json` | E06 DVD stream/timeline probe. |
| `reference-pool/S01E06-in-progress/measurements/levels.json` | E06 measured act-lag levels. |
| `reference-pool/S01E06-in-progress/measurements/head-trim.json` | E06 head-trim, `skip_build`, and separate master/clip-domain measurements. |
| `reference-pool/S01E06-in-progress/measurements/rate-fit.json` | E06 per-act rate fit recorded after pad stretch; review with `head-trim.json` and the manifest before interpreting. |
| `reference-pool/S01E06-in-progress/measurements/retime-map.json` | E06 measured retime map; episode-specific, not reusable as a schedule. |
| `reference-pool/S01E06-in-progress/picture-offset-experiment.py` | E06 experimental cut/high-motion picture-only verifier. It has machine-specific paths and E06 input assumptions; not a supported script. |
| `reference-pool/S01E06-in-progress/picture-offset-results.tsv` | Result table from that picture-only experiment. Raw descriptor caches/source inputs are not archived, so the result is not independently reproducible from this pool. |
| `reference-pool/S01E06-in-progress/efm-reports/{L1,M1,P1,P3}-report.txt` | Four E06 EFMSink reports. P2 is absent from the inspected work folder; this is not a complete five-capture comparison. |

The W E01/E04/E05 manifests' core measurements match those already in `episodes/`; the
authoritative manifests contain newer file-key coverage, so those duplicate copies were
not imported. Large media, WAVs, PNGs, raw descriptors, and temporary files were not copied.

## Validation status

Validation outcomes below are **historical regression evidence**, not current-machine
readiness. Run the profile-specific update gate and preflight/capability checks on each
host; never carry a preflight count or a missing-file warning from one machine to another.

- The E05 QC report records **12 pass, 0 warn/skip/fail**. The source download directory
  and its `download-manifest.csv` are not in `reference-runs`, so E05 input validation
  correctly fails closed if its source files are absent. E04's recorded deliverable
  validation passes 10/10.
- The generic Archive downloader's E05 `-ListOnly` regression returned 12 selected files.
  `init_episode.py` was tested against that verified inventory without inserting timing
  values. The generic Orc builder produced a fresh E05 graph skeleton; historical
  Orc refresh/render evidence is kept separately. The new `select_stack_reference.py`
  passes unit tests and maps the preserved E05 Orc report to M1, but has not yet run a
  live refresh on a fresh episode in this bundle.
- The generic retime generator was dry-run on E04/E05 and rendered E05 to a temporary
  6-channel 48 kHz/24-bit FLAC at the manifest duration with all 819 SRT cues preserved.
  Fresh E04/E05 descriptor extraction and E05 alignment matched the established arrays;
  break-edge measurements reproduced the adjudicated E05 values and E04 within one frame.

### S01E07 input verification (before download)

Verified read-only while the E07 transfer ran; these are input facts, not episode
measurements:

- **DVD:** the supplied matching file under that machine's `roots.dvd_root` - Matroska,
  duration **2729.827 s**, video `mpeg2video` 720x480 yuv420p with declared
  `r_frame_rate`/`avg_frame_rate` both `30000/1001`.
- **Streams:** index 1 AC-3 **5.1** 48 kHz, index 2 AC-3 **stereo** 48 kHz (separate
  mix, not a substitute for LD 2.0), index 3 `dvd_subtitle`, index 4 **`subrip`** -
  so the English SRT source exists as a soft subtitle track.
- **No `title` tag** in the container. The Archive metadata title is only the item
  slug (`DS9-S01E07-LaserDisc-Copies`), so the human title comes from the DVD
  filename: **Q-Less**. Confirm at `init_episode.py` time.
- **Video frame reality:** `-count_packets` and `-count_frames` both return
  **67,191**, but PTS is **non-monotonic** (0.000, 0.133, 0.050, 0.083, 0.250, 0.167).
  Never seek this DVD with `-ss`. Against the 2729.827 s container duration, 67,191
  native frames is not 29.97 - the same native-vs-regridded split seen on E04
  (65,675 native -> 81,747 rows) and E05 (68,555 native -> 81,654 rows).
- **Descriptor expectation:** at the 29.97 alignment grid, expect about
  2729.827 x 29.97 = **81,813 rows**. After extraction, verify the actual count and
  coverage; do not trust `r_frame_rate` or the native packet count as the timebase.
- **Archive item:** 15 files across **5 captures (L1, M1, P1, P2, P3)**, totaling
  409,910,282,896 bytes = 409.91 GB decimal / 381.76 GiB. Five captures is inside
  the 2-6 weighted-stack range.
- **Destination:** resolve from the requested path or that machine's profile
  (`roots.downloads_root` + `roots.downloads_template`). `limits.heavy_jobs` controls
  whether another heavy job may start while the transfer runs.
The Orc graph builder produced a new E05 graph skeleton (Orc refresh/render not run),
the cadence builder generated a measured E05 AVS that passed an FFmpeg five-frame
AviSynth probe, and initial encoding produced a dry-run command. The generic retime
generator was dry-run on E04 and E05 and actually rendered E05 to a temporary 6-channel
48 kHz/24-bit FLAC at the exact manifest duration with all 819 SRT cues preserved; that
temporary output was not substituted for the delivered master.
Fresh master/DVD descriptor extraction matched the established E04/E05 arrays exactly:
masters 83,170/83,230 frames and DVDs 81,747/81,654 frames, with zero pixel-value
differences. The fresh metadata-checked alignment reproduced E05's reference path at
all 5,329 samples. E04 matched at 5,384/5,392; its eight differences cluster in the
known low-texture credits region, so that stretch remains diagnostic. Break-edge
measurements matched E05's adjudicated offsets exactly and E04 within one frame.
The controlled comparison found that explicit `-fps_mode passthrough` reproduced the
68,555-frame failure and `-vsync 0` failed; retain FFmpeg's validated default rawvideo
output-sync behavior.

### Mac-side component smoke evidence

The M5 log records that 16 Python scripts compiled and their CLIs answered `--help`
under Python 3.15, `build_stack_project.py` passed on the E05 regression manifest, Orc
2.11.1 parsed its generated project, and `init_episode.py` passed a synthetic verified
download-manifest test without inventing measurements. The initial Mac-local preflight
reported **42 pass, 1 warn, 0 fail**. After adopting the fresh workflow, macOS execution
found and fixed defects in the embedded Python probe, Orc core-stage query, binary Y4M
frame count, and POSIX download-space query. The corrected Mac-local checker reported
**33 pass, 1 warn, 0 fail** (optional 7-Zip absent); the downloader ListOnly gate projected
260.9 GiB free after the E10 transfer with the 80 GiB reserve. These are M5-machine
results, not a full Mac episode run or validation of the exact adapted Z scripts.

The Z `preflight_mac.py` now includes the M5 fixes; its embedded probe and binary Y4M
parser have local regression tests, but the adapted checker has **not** been run on macOS.
The M5 log also reports `build_map_vpy.py` passed a synthetic `vspipe`→FFmpeg→x265 test
(1301→1040 frames); that exact source is included from the byte-verified Mac snapshot, but
has not been rerun on the current bundle/profile. `download_archive.ps1` now uses
`System.IO.DriveInfo` for POSIX space checks with a Windows `Get-PSDrive` fallback; the
Z copy still needs a Mac ListOnly verification. `align_dvd.py` from the snapshot was not
copied: Z's `extract_video_descriptors.py` and `measure_dvd_alignment.py` provide the
current `align_npy` path, and `av_check_output.py` points to them. Do not claim Mac
readiness until the current checker, helper, and ListOnly gate are exercised on the target
Mac and a complete episode workflow passes. The latest M5 log still had E10 downloading
(163/457.3 GiB); it deferred manifest creation and all downstream stages until verified
completion. E10's ~227 GiB
projected post-job headroom is specific to that Mac/profile; recompute free space on every
machine and keep the configured minimum unchanged.

## Runbook files in this folder

- `DS9 LaserDisc OpenCode Runbook.md` - canonical detailed technical procedure.
- `DS9 LaserDisc OpenCode Runbook - Qwen4B.md` - concise ACTION/VERIFY/STOP variant.
- `DS9 LaserDisc OpenCode Runbook - Qwen4B-Luna.md` - reasoning/ambiguity-focused variant.
- `DS9 LaserDisc Plain Text Instructions.txt` - ASCII companion of the canonical runbook.

`README.md` governs startup and machine setup. The canonical runbook governs
measurement and quality rules. A model-specific variant may change presentation,
not the workflow, thresholds, manifest contract, or stop conditions.
