# DS9 LaserDisc OpenCode Operator Pack

**Workflow version: 21 (FromStunToKill app branch)** — check `WORKFLOW_VERSION` and follow the
core-change/device-log rules in `WORKFLOW_VERSIONING.md` before editing or syncing workflow files.
This V21 increment is scoped to the app branch; production `main` and the W shared tree remain
at V15 and are not modified by this release.

**Design goal:** make the workflow clear and reliable enough for capable free or low-cost
models to run with its bounded steps, explicit ACTION/VERIFY/STOP gates, and deterministic
helpers. Qwen3.5 4B and Big Pickle are examples of target users, not a guarantee or a claim
that every model/version has been tested. Model convenience never lowers QC or safety gates.

**GitHub source-only note:** this repository intentionally omits machine profiles, measured
episode data, reference archives, media/caches, and third-party AviSynth DLLs. See
`GITHUB-EXPORT.md` before treating a GitHub clone as a complete runnable bundle.

**Configure a job first:** start with the root `CONFIGURE HERE FIRST.json`. Edit only the
`value` members in `_operator_inputs_EDIT`; keep its `_comment` descriptions and
`_instructions_DO_NOT_EDIT` section intact. The template is a no-DVD intake example and
must be filled with this job's confirmed values before it can be planned. Job IDs and
approval identity/time are generated automatically.

**Live-workflow source boundary:** `V*-QUEUE.md` files are planning/backlog records only.
Do not read, cite, or apply queue entries as episode instructions, measurements, approvals,
stage arguments, or gates during a live job. Use this README, the canonical runbook, and
released scripts at `WORKFLOW_VERSION`; a queued proposal becomes operative only after it is
implemented and released.

**Changelog boundary:** `changelog.txt` and release/version-history summaries are retrospective
records only, not live workflow inputs. Ignore them as inputs to a live job: do not derive
episode identity, title,
measurements, stage arguments, approvals, or gates from changelog text. If a current runbook,
helper, manifest, or explicit user decision is missing or ambiguous, stop and clarify.

**Start here.** An OpenCode instance given this folder and the three job inputs
below should read this README, then follow `DS9 LaserDisc OpenCode Runbook.md`.
The runbook is the detailed technical procedure; this README is the machine setup,
job contract, stage index, and stop/go checklist. If the active model is local
Qwen3.5 4B, also follow `DS9 LaserDisc OpenCode Runbook - Qwen4B.md` for its explicit
ACTION/VERIFY steps. If the configured fallback `opencode-go/gpt-6-luna` is available,
use `DS9 LaserDisc OpenCode Runbook - Qwen4B-Luna.md` for its narrowly gated fallback
protocol. The main runbook remains the technical source of truth.

Windows x64 is the authoritative/reference workflow. The Apple-silicon macOS VapourSynth
workflow has now completed an end-to-end S01E10 delivery on the M5 Mac, including the
final remux and seek-defect repair. This validates the tested M5/macOS arm64 configuration;
a separate fresh-Mac installation is not implied. Linux x86_64 dependency locks are
provided, but the Linux workflow is still unvalidated; Linux ARM64 and Windows ARM64 also
remain untested. This is not a single unattended encoder: OpenCode
measures per-episode values, records them in a manifest, runs gated helpers, and stops at
ambiguity/failure. Topaz is never launched automatically.

---

## The job prompt

The user provides either an Archive item URL or a show/season/episode request, the folder
in which the LaserDisc files should be saved, and—if available—the matching DVD
episode/version. For a show/season/episode request, use the included resolver and verify
the selected item/title from Archive metadata, capture names, and DVD evidence.
If no matching DVD is available, ask whether the user explicitly wants to proceed
with the LD-only timing path; it cannot provide DVD 5.1/SRT or an external absolute
A/V cross-correlation.
The final destination defaults to `roots.out` in `machine.json`; the user may
supply an override.

If a supplied DVD is outside the configured `roots.dvd_root`, set that root to the
DVD library folder for this job, rerun preflight, and record the exact DVD file in
`files.dvd`. This is machine configuration, not an episode timing measurement. If
the user chooses no-DVD mode, initialize with `--no-dvd`; do not invent a `files.dvd`
path or DVD-derived tracks.

```text
Read README.md and DS9 LaserDisc OpenCode Runbook.md in this folder. Run the
workflow; do not stop after downloading.
To start the transfer, first verify the Archive item/title, destination, local profile,
PowerShell/curl availability, and the downloader's ListOnly metadata/integrity/space gate.
Start the verified download without waiting for the full tool inventory or doctor.
While it runs, complete the Windows or macOS key-tool update check in PORTING.md and
ask before making any changes; do not install/upgrade tools during the transfer.
All selected updates, preflight, Python lock, and doctor gates must pass before processing.
TFM is treated as compatible when both platforms use current supported software; no
separate parity flag or parity-clip check is required.
If you are local Qwen3.5 4B, also follow DS9 LaserDisc OpenCode Runbook - Qwen4B.md.
Use the Qwen4B-Luna variant and opencode-go/gpt-6-luna only at its explicit fallback triggers.

LaserDisc Archive URL (if known): <https://archive.org/details/... or blank>
Series/season/episode (if URL is blank): <DS9, VOY, or TNG / season / episode>
Save source files to: <absolute folder path>
Matching DVD episode/version (optional; path or NONE): <path or NONE>
If NONE, ask whether to proceed with LD-only timing before initializing.
If NONE and Cut Pad Images/<episode> contains stills, ask whether the user wants to use them.
Final output folder (optional; default is machine.json roots.out): <path or blank>

If the URL is blank because the user named a series/season/episode, resolve the request
with `scripts/resolve_archive_episode.py`. Continue only when it returns exactly one
`status: matched` standalone episode-copy item. A combined-item-only or not-found result
requires clarification; do not infer a URL from a season stack or extra archive.

If the DVD is NONE and the user chooses to proceed, use the V6 duration/PAD route to
correct and sample-align the original LD audio to the measured, trimmed and pad-fixed
video timeline. DVD 5.1/SRT and DVD-picture/act-break analysis are unavailable. QC verifies
the capture-pad origin model and exact audio/video endpoint; it must disclose that no
external absolute A/V cross-correlation was possible. If shared cut-pad stills exist, use
them only after confirming the episode, DVD timestamp/scene, and exact cut-PAD mapping.
Each still is a single prepared DVD frame for a confirmed one-frame black dropout at a
shot cut—not a substitute for a DVD reference or a source for other tracks.

Derive and verify the episode code/title. Preserve inputs. Measure every episode-
specific value; never copy timings from a reference episode. Follow each runbook
VERIFY/STOP gate. Use the bundled helpers and keep machine paths in the device-local
profile and measured values in the active episode manifest (`episodes/<code>.json` for legacy
DS9, `episodes/VOY/<code>.json` for Voyager; shared workspaces use `episodes/<code>` for DS9
and `episodes/VOY-<code>` for Voyager, each with `manifest/<code>.json`). Never delete/clean files unless I name
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

Start the download as soon as the **download-specific** checks pass: confirmed Archive
identifier/title, intended local destination, selected profile and working curl >=7.71.0,
Archive metadata/size/MD5 inventory, paired TBC JSON sidecars, and projected-free-space gate. The
downloader enforces these checks itself; `-ListOnly` previews them without transfer. The
download stage intentionally does not wait for the full doctor/toolchain lock. While the
transfer runs, inventory key tools in PORTING.md, show missing/outdated versions, ask
whether to update specific components, **Update all**, or **Continue anyway**, and record
the choice. Apply changes only after the transfer completes, never mid-transfer. Also
verify/probe the matching DVD, check its stream/timeline facts, run full readiness checks,
prepare stage space estimates, and stage next-step commands. Keep a checklist of done vs
blocked work. Do not create the episode manifest or make LD-dependent measurements until
`download-manifest.csv` verifies every selected file; do not start processing until tool,
preflight, dependency-lock, and doctor gates pass. Save preliminary evidence under
`roots.probe` or `roots.logs` and resume blocked stages after verification/readiness.
```

The prompt starts the job; it does not waive machine setup or quality gates.

## Work ahead while the transfer runs

The transfer is the clock, not a reason to wait idle. After the download-specific checks
pass, start the resumable transfer and work through every independent preparation task.
Create/reuse the stable device/episode workspace, then copy
`templates/episode-prep-checklist.template.md` to
`<instance_root>/logs/prep-checklist.md`. Record task status, evidence paths, and the exact
artifact or gate that will unblock each deferred task.

**Allowed during transfer:** tool/version inventory and the user's update decision (apply
changes only after transfer completion), DVD copy/hash/stream/PTS/black/silence preparation,
workspace setup, readiness checks, stage space estimates, and staged commands.
**Blocked until `download-manifest.csv` verifies every selected source file:** episode
manifest initialization, EFM decode/capture choice, stack/reference work, cadence/trims,
LD-dependent picture/audio measurements, PAD decisions/repairs, retiming, encode, and
delivery QC. DVD-only prep never authorizes guessing an LD value. The checklist records
progress; it does not waive a gate.

## Resolve an Archive item from a series/episode request

If the user gives a show, season, and episode instead of an Archive URL, resolve the
included item tree before downloading:

```powershell
python scripts/resolve_archive_episode.py "Download Episode 18 of season 2 of DS9"
python scripts/resolve_archive_episode.py "Download episode 1 of season 1 of Star Trek Voyager"
```

Aliases include DS9/Deep Space Nine/Deep Space 9, VOY/Voyager, and TNG/The Next Generation;
`Star Trek` or `Trek` may prefix a show name. Only one exact standalone episode-copy match
returns `status: matched`. Use the returned URL, choose the user's destination or the
resolver's show-prefixed folder name under `roots.downloads_root`, and run the normal
`download_archive.ps1 -ListOnly` gate before transfer. The included snapshot covers DS9 and
VOY seasons 1–5; it does not invent missing seasons/items. TNG has no standalone episode-copy
items in this account; its multi-episode extra is reported but never auto-downloaded for one
episode. A catalog match only selects an item link; it does not prove a human title or bypass
live Archive hash/space checks.

## Series-aware episode identity and migration

The canonical identity is `SERIES:SxxExx` (for example `VOY:S01E01`). New workflow plans store
`series: "VOY"` alongside `episode: "S01E01"`; stage CLIs accept `--series VOY`, and direct
stage helpers accept `--episode VOY:S01E01`. An unqualified legacy code still means DS9. New
manifests persist `series` and `identity`; a legacy manifest without `series` is interpreted as
DS9 and its old path is unchanged.

Voyager manifests live in `episodes/VOY/<code>.json` locally or in a series-specific workspace
folder named `VOY-<code>`. DS9 retains its existing manifest/workspace locations. All generated
VOY artifacts, logs, workflow status, and resume locks are namespaced under the VOY output roots
or VOY workspace, preventing same-code overwrite/resume collisions. Profile roots remain the
configured roots; VOY output roots are created below a `VOY/` child directory. Download
directories must also be show-prefixed (for example `VOY S01E01 LaserDisc`).

The Archive resolver remains metadata-only. It routes only exact standalone DS9/VOY items into
the series-aware intake. The approved workflow still runs the live downloader `-ListOnly`, hash,
paired-sidecar, and free-space gates; initialization still requires the verified download
manifest and existing human decisions. The TNG combined extra remains blocked. DS9 S01E01/E02
retain the Emissary special workflow, while VOY S01E01 is a normal VOY identity.

**Migration:** no existing DS9 manifest, workspace, profile, or output needs renaming. Add
`series: "VOY"` / `identity: "VOY:SxxExx"` only to new Voyager manifests through
`init_episode.py`; do not re-label an existing DS9 manifest. For an existing VOY staging setup,
move/copy nothing automatically—review its files and choose a `VOY/` output namespace, then
re-run stage planning. Resume state never crosses series, and legacy DS9 state without series
fields remains DS9-only resumable. Human approval and QC gates remain unchanged.

For Qwen3.5 4B, apply the Qwen4B step format. Route to `opencode-go/gpt-6-luna`
only when a Luna **DELEGATE** rule is triggered after the local checks; if the
fallback route is unavailable, ask the user rather than silently changing models.

---

## One-time setup on each machine

1. **Desktop app:** `python scripts/workflow_app.py` bootstraps a unique
   `devices/<DEVICE>-<ID>/` folder first, then creates that device's Git-ignored
   `local device config/machine.json` inside it. The app validates this exception against
   a machine-local identity marker; do not copy that profile to another device.

   **CLI workflow:** keep profiles outside the folder containing this README. For Windows,
   copy `machine.example.json` to a device-local temporary profile (or use the existing local
   profile), create a new workspace profile with `shared_workspace.py configure-profile`,
   then run discovery against that external output profile:

   ```powershell
   New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\ld-pipeline\profiles" | Out-Null
   $seed = "$env:LOCALAPPDATA\ld-pipeline\profiles\machine-seed.json"
   $profilePath = "$env:LOCALAPPDATA\ld-pipeline\profiles\machine-$env:COMPUTERNAME.json"
   if (Test-Path $seed) { throw "Seed already exists; choose another local seed path: $seed" }
   Copy-Item .\machine.example.json $seed
   python scripts/shared_workspace.py configure-profile --source $seed --output $profilePath --device-name $env:COMPUTERNAME
   powershell -NoProfile -ExecutionPolicy Bypass -File preflight.ps1 -Profile $profilePath -Write
   ```

   This initializes/discovers paths; it is not the final readiness pass. It saves an
   existing profile to a unique dated backup before writing; keep that backup. If several
   plausible work roots exist, discovery warns and requires an explicit `roots.work` rather
   than silently selecting one. On Mac, do not
   run the Windows preflight: copy `machine-macos.example.json` to a local
   `machine.json`, replace the example paths, and follow the fresh-Mac bootstrap in
   `PORTING.md`. Set custom roots that discovery could not determine, especially
   `roots.work` and `roots.dvd_root`; set `upscale_root` only for legacy E01 files.
   On Linux x86_64, a local copy may start from `machine-linux-x86_64.example.json` for
   dependency/capability checks only; the Linux production workflow is not validated and
   `doctor.py` will not mark it episode-ready.
   For manual Mac/Linux CLI setup, seed a profile from the matching example outside the README folder, set
   machine-local paths, then use `configure-profile` to create a separate profile pointing
   at this README folder. Always pass that external profile to `doctor.py` and
   `run_stage.py`; never use the authoring bundle's root-level `machine.json` as a device profile.
2. With the local profile paths known, the download may start after its own profile/curl,
   Archive metadata, file-integrity metadata, and destination-space checks; do not wait
   for full tool inventory. Run the Tool-update gate below during the transfer. If components are
   missing or out of date, present each installed/latest version and ask whether to
   install/update specific components, **Update all**, or **Continue anyway**. Apply only
   the user's choice and record it. On Windows, check Windows x64, Decode-Orc, AviSynth+,
   FFmpeg with the AviSynth demuxer and `libx265`, RIFE engine/model, Python and required
   packages. The bundle includes `ffms2.dll` and `RemapFrames_x64.dll`; machine-specific
   TFM/TDecimate/QTGMC plugins must also be available to AviSynth+. On Mac, use the Mac
   component list in `PORTING.md`.
3. Check pinned direct Python package versions with the read-only default:
   `python scripts/bootstrap_deps.py --print --json`. If packages are missing/mismatched,
   show the delta and get the user's install/update choice before running
   `python scripts/bootstrap_deps.py --install`. It modifies only the selected Python
   interpreter's listed direct packages; native tools/plugins are separate. Linux x86_64
   is a dependency candidate, not production-workflow support.
4. Run `python scripts/doctor.py --json` after profile/tool selection (this may run while
   the download is in progress). It delegates to
   Windows or Mac preflight, checks the Python lock, and emits one report. Require
   `ready_for_episode: true`; macOS end-to-end completion is demonstrated on the M5
   configuration, while fresh-device setup remains a separate validation. Linux
   deliberately returns not-ready/untested. Continue anyway does not waive required
    capability/QC gates. In device-workspace mode, `shared_workspace.py ensure` is the
    recommended setup; `run_stage.py` also creates/reuses the workspace or warns and uses
    local roots if the README-folder `devices/` path is unavailable. A standalone doctor
    without an active instance reports machine diagnostics, not an episode-ready result;
    this is not a workspace-creation gate for the stage runner.
5. For a job-specific output location, update `roots.out` or pass it as the manifest's
   file root. Do not modify scripts to insert drive letters.

Windows `preflight.ps1` checks FFmpeg/AviSynth/x265 capabilities and functionally
smoke-tests LSMASHSource, TFM, and TDecimate through the configured FFmpeg/AviSynth path.
QTGMC remains conditional: if measured inserts require it, validate QTGMC and its
dependencies before that repair stage. Preflight also checks RIFE weights, bundled AVS
DLLs, DVD-root mapping, Python dependencies, and free space. The Mac
`preflight_mac.py` checks its applicable FFmpeg/VapourSynth/Orc toolchain, profile roots,
and free space. Neither platform check is a quality pass or an end-to-end episode
validation. `scripts/doctor.py` is the unified read-only entry point; it installs nothing.

### Required tool-update gate (Windows and Mac)

Before processing and during one-time setup, follow the cross-platform **Tool-update gate**
and key-tool matrix in `PORTING.md`. For a new episode, the verified download may start
first after its narrower gates; do the full version inventory while it runs. Check the
exact versions resolved by that machine's profile against current upstream releases. If
components are missing or out of date, show each with its installed state/latest version
and ask which specific components to install/update, whether to **Update all**, or
**Continue anyway**. Apply only the user's choice and record it in the machine's local
`roots.logs` after the transfer finishes, then rerun preflight/capability checks. If the
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
- The V20 app's post-run cleanup review is opt-in and appears only after a successful workflow
  containing QC. It additionally requires the current final deliverable to match the QC-stage
  fingerprint and a clean QC report (zero WARN/LIMIT/FAIL). It offers only **Keep everything for
  now** or **Free up space**: the latter reviews the exact episode download folder and
  manifest-associated audio files at least 100 MiB. Small download manifest/TBC-sidecar/text
  metadata is hash-verified into the episode `references/` folder first; logs, plans, manifests,
  QC reports, provenance and deliverables remain untouched. Selected paths go to the OS
  Recycle Bin/Trash after a second exact-path confirmation; permanent-delete fallback is forbidden.

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

The M5 log now records a complete S01E10 macOS arm64 workflow, final multi-track deliverable,
remux, and seekability repair (§22). This establishes end-to-end success on the tested M5
configuration; it does not certify every fresh Mac installation or hardware variant. The
Mac helper set and source-available regression tests are included in `scripts/` and `tests/`.
The historical TFM parity result is background evidence; current supported software needs
no recurring parity test. Before Windows or Mac setup and every new episode, follow the **Tool-update
gate** in `PORTING.md`, record version choices, rerun capability checks, and freeze the
toolchain during the episode.

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
 3. **Initialize and validate the episode manifest.** With a DVD, run
     `python scripts/init_episode.py --url <URL> --downloads <folder> --dvd <DVD.mkv>`.
     If the user explicitly approved no-DVD mode, run
     `python scripts/init_episode.py --url <URL> --downloads <folder> --no-dvd`
     (and add `--cut-pad-images use|skip` when shared cut-pad PNGs are available).
     The helper checks the verified download and Archive metadata, records whether a DVD
     was supplied, and emits platform script paths (`.avs` Windows / `.vpy` macOS).
     `ld_native` and `ld_corrected` remain unset until measured at the audio stage. Confirm
     the title and uncertain capture metadata, then run
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
   5. **Measure cadence, trims, and capture quality.** Generate the full-side TFM log and
       motion trace; on Windows use `build_map.ps1`, on macOS use the included
       `build_motion_trace.py` + `build_map_vpy.py`. Inspect hybrid/interlaced inserts and
       apply only measured trims. With a matching DVD, use its visual and audio fade
       boundaries as the common start/end reference: map the DVD picture points to LD output
       frames and independently locate the corresponding native-LD audio sample points.
       Preserve any measured DVD picture/audio offset; never reuse raw DVD frame numbers or
       equate audio samples with video trim frames. Recompute `head_trim`, `tail_trim`,
       `delta_s`, `AudioSkip`, audio origin/endpoint, frame count, and duration after changing
       a boundary. Decode all available raw EFMs with Orc and compare the reports before
       selecting a WAV. Preserve each native WAV/report and set `audio.pad_frames` from the
       selected capture's final Orc input-order alignment.
    6. **Choose the audio/reference path.** If a DVD is available, keep the DVD-reference
       workflow: decode it sequentially (never `-ss` a non-monotonic DVD), extract the DVD
       and master descriptor grids, adjudicate the picture map/edges and robust-rate/fade
       evidence, then build `ld_corrected` and sample-accurate `ld_synced`. At midpoint
       commercial-break fades, map both edges of the same visual break and its audio fade
       points to the DVD reference, preserving the DVD-measured picture/audio offset. Only
       consider trimming excess LD duration if that matched DVD interval is measurably
       shorter; retain DVD-length breaks when equal/longer. The current
       pipeline does not execute internal LD picture/audio cuts, so record such a candidate
       and stop rather than changing `retime.breaks` or cutting by eye. Do not use the
       DVD-rate fit as an assumed value. If the user explicitly chose no-DVD mode, run
       `python scripts/audio_timing_diagnostic.py --episode <code> --promote-dvd-free --report-out <new-unique.json>` only
       after measured video frames/DELTA and PAD counts/endpoints are available. It applies
       the E07-validated duration/PAD rate and capture-pad geometry, runs a measured
       audio-volume space gate, and records its report and
      hashes, and refuses endpoint residuals beyond one frame. It produces LD stereo only.
      QC then verifies exact audio sample count/origin method but must state that no external
      absolute A/V cross-correlation was possible; do not report a DVD-based A/V pass.
   7. **Prepare confirmed PAD repairs before final encoding.** Validate the source-derived
      script without x265, then run `pad_audit.py`, `fullres_check.py`, `rife_padframes.py`,
      and the platform's padfix builder (`build_padfix_avs.py` on Windows,
       `build_padfix_vpy.py` on macOS). Use RIFE only for confirmed non-cut PADs. For a
       DVD-backed cut PAD, publish the independently mapped, cropped/color-reviewed DVD
        still and provenance to this episode's per-device `cut-pad-images/` with
        `shared_workspace.py publish-cut-pad` (verified DVD SHA-256, `--confirm-mapping`,
        and a non-empty `--color-match-note`), then pass that exact PNG via
       `--cut-pad-image <output-frame>=<PNG>`; `--hold-cut` is only the explicit no-DVD
       fallback. No cross-shot RIFE. In no-DVD mode, use curated timestamp-named
       `Cut Pad Images/<episode>/` stills only if the user chose them, or hold the prior frame.
       Record the exact measured `pads.cut_pad_frames` list so the image-to-pad
       mapping is auditable. Require source-raster and replacement-identity checks; do not
       repair genuine dark content or derive repairs from an encode.
    8. **Retime DVD tracks only when available and requested.** With a DVD, adjudicate every
       silence edge, then dry-run and review `retime_dvd_assets.py` before writing 5.1/SRT.
       This retimer maps DVD tracks onto the current LD timeline; it does not perform internal
       LD edits. If a shorter-DVD midpoint cut is proposed, stop until a reviewed common edit
       map can trim LD picture and audio together and rebuild the DVD retime/SRT schedule.
       After both retimed assets pass their gates, set `mux_tracks` and `deliverable_tracks`
       together to `['ld','51','srt']`; keep `ld_audio_source='ld_synced'`. V6 remux maps
       `retimed_51` and `retimed_srt` directly, so no preassembled `pre_upscale` input is
       needed. Without a DVD, leave DVD-derived retime fields unset and keep both track
       lists at `['ld']`; do not invent 5.1 or subtitles.
   9. **Encode video once, then remux audio.** After PAD repairs and measured frame count/
      duration, validate without x265, then run the platform's single-pass encoder:
      `encode_episode.ps1 -Pass final` on Windows or
      `python scripts/encode_episode_mac.py --episode <code> --pass final` on macOS.
      Both produce video-only `padfixed_video` with the V5 CRF 14 / slow / grain / Main 10
      recipe. Build/verify `ld_synced` first; use `remux_padfixed.ps1` or
      `remux_padfixed_mac.py` to stream-copy and packet-check the available tracks. Audio-only
      corrections rebuild `ld_synced` and remux—never rerun x265.
   10. **Deliver only after QC.** Run `python scripts/qc_report.py --episode <code>` and
       inspect the report. Require exit code 0. DVD mode checks the normal absolute A/V,
       retimed-track, and subtitle gates. No-DVD mode uses the explicit LD-only timebase and
       exact endpoint check; QC records that independent absolute A/V was not available.

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

Every measured value belongs in the active series manifest (`episodes/<code>.json` for
DS9, `episodes/VOY/<code>.json` for Voyager); every generated report goes
to the machine's `roots.probe`/`roots.logs`. If a command fails, inspect its artifact
and logs before retrying. Do not overwrite or clean user files without explicit
permission.

## Optional FromStunToKill desktop app

Launch the local GUI with `python scripts/workflow_app.py`. It uses the V21 allowlisted
runner and automatically saves concrete, immutable workflow plans under the device's
`devices/<DEVICE>-<ID>/plans/` folder when **Preview** is clicked; no separate save step is
needed. Intake fields remain editable after preview and until **Run** is confirmed; edits
cause the next Preview/Run to create another plan rather than changing a saved plan. A direct
**Run** click on a new or edited intake saves and previews first, then requires a second click
to execute. It does not require an AI service to select an episode, build a plan, or run the
workflow. In **Archive URL** mode, the
Series control is hidden and the picker/Archive metadata resolves the series automatically.
In **Open local** mode, the Series selector is shown when filenames do not identify DS9 or
VOY. The canonical identity is displayed as `SERIES:SxxExx`, and existing bare DS9 plans
remain supported.

On first launch, the app reserves a stable device identity, creates
`devices/<DEVICE>-<ID>/` first, then creates that device's machine profile at
`devices/<DEVICE>-<ID>/local device config/machine.json`. The `devices/` tree is
Git-ignored. Device manifests/logs and configured work folders stay in that unique subtree;
a small identity marker remains in the user's local config folder so this device can verify
its shared profile on the next launch. Profiles from the general per-user profile folder are
left untouched and do not block startup. **Open machine profile…** is only needed to inspect
or edit the automatically selected device profile.

The Archive picker is metadata-only. Selecting an item fills its item URL and resolves its
live title/workspace; it does not download media or bypass `-ListOnly`, integrity, sidecar,
or free-space checks. TNG combined-item entries and Emissary remain blocked from generic
single-episode intake. Local intake inventories decoded captures and records fingerprints
without asserting Archive origin. A selected local capture folder or exact DVD may be outside
the profile's default download/DVD roots; its path is retained in that device's episode
manifest so local validation can reopen it. Archive download destinations remain constrained
by the machine profile's download template.

The episode title is optional in local intake; if omitted, the plan records `Title not
supplied (SERIES:SxxExx)` and instructs the operator to replace it with an evidence-confirmed
title before downstream processing. The no-DVD cut-PAD choice is required only for no-DVD
intake; it is not required in DVD-backed mode.

The GUI can prepare V15 retime-break candidate reports, then show every pairing/status in
a review table. It never chooses thresholds or pairings for the operator. Recording is a
separate plan requiring explicit unique-pair/merged-through selections, a rationale, a
human review gate, report/input hash checks, a backup, and the V15 atomic manifest writer;
offsets and steps remain unmeasured. Final encode/remux is not started by candidate tools.

The V17 PAD flow uses the existing episode motion-baseline percentile: candidates below P95 are
recorded as automatic non-cut/RIFE decisions when their metrics are valid. P95+ outliers, missing
metrics, prior cut-PADs, and above-threshold residuals remain in the human review queue. Reviewers
approve actual cuts or reject cut-like outliers as non-cut; the exact prior-cut warning and
DVD/no-DVD safeguards remain. Decisions are saved immutably, and RIFE runs only after the manifest
update succeeds. EFM verification binds each decode log,
TBC/metadata source, WAV, and EFMSink report to one manifest capture; capture selection is a
separate human decision with an explicit rationale and Orc input-order PAD count. These tools
do not authorize a final encode or remux.

The V18 episode dashboard runs the existing stage validator on demand and keeps PASS, WARN/LIMIT,
and FAIL distinct. Guided workbenches create report-only audio diagnostics, cadence plans from
measured inputs, and mode-aware QC-only/remux/full-delivery plans. Final encode and remux are
separately review-gated and still require the Run confirmation. The process monitor shows PID,
elapsed time, and console inactivity; a stall only offers a diagnostic snapshot and never kills or
retries automatically.

**Get OpenCode help** is optional, audit-logged, and disabled by default; the operator can
explicitly enable it in the app. The V18 app workflow, local-source intake, Archive picker,
candidate analysis, and human approval gates work without it. AI output never selects
measurements or grants approval.

---

## Bundled tools

| Tool | Purpose |
|---|---|
| `preflight.ps1` | Backup-safe profile discovery, ambiguity-safe root selection, and checks; Windows also smoke-tests LSMASHSource/TFM/TDecimate, with QTGMC conditional on measured inserts |
| `scripts/preflight_mac.py` | Mac profile/tool/root/VapourSynth capability check (`--episode` and `--json` supported); checks workflow version and does not prove full workflow readiness |
| `scripts/doctor.py` | Unified read-only profile/dependency/platform report; Linux x86_64 remains explicitly not episode-ready |
| `scripts/bootstrap_deps.py` / `dependencies/` | Direct Python version locks for Windows x64, Mac arm64, and candidate Linux x86_64; check-only by default, `--install` is explicit |
| `scripts/run_stage.py` | Allowlisted stage dispatcher; dry-run plan by default, explicit `--execute`, series-aware stage identity, downloader-specific checks for early download, and full gates before other writes |
| `scripts/run_workflow.py` | Ordered JSON-plan orchestration; series-scoped status/log/locks/resume, input/output fingerprints, conservative resume, and per-step progress bar |
| `scripts/workflow_app.py` | Optional Tk desktop intake, episode status, PAD cut review, EFM provenance/selection, process monitoring, guided workbenches, and post-run cleanup review |
| `scripts/post_run_cleanup.py` | Clean-QC-gated inventory and opt-in Recycle Bin/Trash operation that preserves small download metadata and workflow evidence |
| `scripts/pad_classification_review.py` / `record_pad_classifications.py` | P95 motion-baseline PAD defaults, human cut/outlier review, and immutable manifest recording before RIFE |
| `scripts/verify_efm_capture.py` / `record_efm_capture_selection.py` | Bind each EFMSink decode to its manifest TBC/log/WAV/report, then record an explicit human capture choice |
| `scripts/backup_device_workspace.py` | Checksum-verified per-episode/device evidence archives and dry-run-first no-merge restore |
| `scripts/episode_dashboard.py` / `scripts/job_monitor.py` | Read-only stage validation with unique gate logs/reports, plus advisory process-stall diagnostics; no automatic termination |
| `scripts/workflow_workbenches.py` | Immutable report-only audio/cadence plans and separately approved delivery/QC plan templates |
| `scripts/retime_break_review.py` | GUI table for explicit V15 candidate classification; creates a separately reviewed record plan |
| `scripts/measure_ld_black.py`, `scripts/prepare_retime_breaks.py`, `scripts/record_retime_breaks.py` | Threshold-sensitivity report, ambiguous-safe DVD/LD pairing report, and explicit backed-up recording of reviewed intervals; offsets remain unmeasured until edge adjudication |
| `scripts/retime_break_candidates.py` | Pure pairing/record-validation helpers used by the report and recorder; no manifest write on report path |
| `CONFIGURE HERE FIRST.json` | Sanitized no-DVD intake-plan template; choose DS9 or VOY in the editable `series` value |
| `scripts/stage_clock.py` | Pause/resume timer for manual stages; separates active work from human gate waits and writes a timestamped record in the device workspace |
| `scripts/shared_workspace.py` | Ensure one stable per-machine device folder with series-scoped episode workspaces; publish no-overwrite references and reviewed cut-PAD provenance |
| `scripts/generate_runbooks.py` / `templates/runbooks/` | Regenerate model variants from the canonical runbook plus model-policy templates; `--check` detects drift |
| `WORKFLOW_VERSION` / `WORKFLOW_VERSIONING.md` | Authoritative workflow version and core-change/device-log rules |
| `V7-RELEASE.md` | V7 tooling overview and platform validation boundaries |
| `V8-RELEASE.md` | V8 stage timing and README-relative device workspace changes |
| `V9-RELEASE.md` | V9 ordered workflow orchestration and intake configuration |
| `V10-RELEASE.md` | V10 retime verification, measurement diagnostics, workspace compatibility, and analysis safeguards |
| `V11-RELEASE.md` | Queue-use boundary and `init_episode` shared-mode regression fix |
| `V12-RELEASE.md` | Optional report-only LD/DVD audio comparator and regression coverage |
| `V13-RELEASE.md` | Fail-closed break edges, cut-PAD policy/provenance, stable workspaces, Orc resume, and bounded extraction |
| `V14-RELEASE.md` | Section 31 Archive tree, natural resolver, and series-aware VOY workflow release record |
| `V15-RELEASE.md` | Changelog source boundary, fail-closed break candidate intake, and retime output verification improvements |
| `FROMSTUNTOKILL-V15-APP.md` | Optional desktop app integration, review boundaries, validation, and platform limits |
| `V16-RELEASE.md` | FromStunToKill app-branch PAD/EFM provenance and workspace-safety hardening; production `main` remains V15 |
| `V16-QUEUE.md` | Non-operative record of unresolved reconstructed episode evidence; not live instructions |
| `FROMSTUNTOKILL-V16-APP.md` | V16 app-branch integration scope, validation, and publication boundary |
| `V17-RELEASE.md` | P95 motion-baseline automatic PAD classification and human review of cut-like candidates |
| `FROMSTUNTOKILL-V17-APP.md` | Historical V17 desktop integration and validation boundary |
| `V18-RELEASE.md` | Historical V18 dashboard, guided workbenches, monitoring, and delivery-plan changes |
| `FROMSTUNTOKILL-V18-APP.md` | Historical V18 desktop integration and validation boundary |
| `V19-RELEASE.md` | Historical CLI workflow progress display and validation |
| `FROMSTUNTOKILL-V19-APP.md` | Historical V19 app-branch integration and validation boundary |
| `V20-RELEASE.md` | Historical post-run download/audio cleanup review and safety gates |
| `FROMSTUNTOKILL-V20-APP.md` | Historical V20 cleanup integration and validation boundary |
| `V21-RELEASE.md` | DVD-referenced visual/audio boundary-alignment policy |
| `FROMSTUNTOKILL-V21-APP.md` | Current V21 boundary-review integration and validation boundary |
| `V9-QUEUE.md` | Historical V9 device-log review and inherited carry-overs; planning only |
| `V10-QUEUE.md` | Historical V10 scope and implementation record; planning only |
| `V11-QUEUE.md` | Historical V11 record and dependency-policy proposal; planning only |
| `V12-QUEUE.md` | V12 planning/review record; not live workflow instructions |
| `V13-QUEUE.md` | V13 implementation/evidence record and held episode findings; non-operative |
| `V14-QUEUE.md` | Archive catalog/resolver scope and coverage limitations; planning only |
| `V15-QUEUE.md` | V15 implementation record and acceptance evidence; planning only, never live instructions |
| `changelog.txt` | Short release history; retrospective V1–V5 notes and accepted V6 changes |
| `Cut Pad Images/<episode>/` | Curated single-frame DVD cut-PAD repair stills, named by verified DVD timestamp/frame; not a DVD reference substitute |
| `scripts/download_archive.ps1` | Metadata-verified, resumable source download; checks profile curl (>=7.71.0), and ListOnly prints inventory, integrity metadata, aggregate bytes, and projected free space |
| `scripts/check_space_budget.py` | Projected per-stage free-space gate that preserves the configured reserve |
| `scripts/init_episode.py` | Verified Archive/download + optional DVD -> draft manifest; `--no-dvd` records explicit approval, and macOS gets `.vpy` script paths |
| `scripts/validate_episode.py` | Stage-specific manifest/artifact prerequisites; optional expensive source MD5 recheck |
| `scripts/select_stack_reference.py` | Streamed Orc refresh loop with heartbeat and verified completed-pass resume; selects/stabilizes a reference and never renders |
| `scripts/resolve_archive_episode.py` | Resolve a natural series/season/episode request to one standalone Archive item; no transfer is started |
| `scripts/build_archive_catalog.py` / `scripts/archive_catalog.py` | Build/read the metadata-only Section 31 item/file tree and resolve aliases; `--force` is required to replace the snapshot |
| `archive_catalog/section_31_tree.json` | Included metadata-only Section 31 account/item/file tree snapshot; no media bytes |
| `scripts/build_stack_project.py` | Manifest-driven Orc graph skeleton; scratch `--reference-override` does not edit the manifest |
| `scripts/efm_paths.py` | Plan episode-scoped EFMSink WAV/report paths and refuse existing outputs |
| `scripts/extract_video_descriptors.py` | Sequential master/DVD descriptor extraction on the validated FFmpeg rawvideo grid |
| `scripts/measure_dvd_alignment.py` | Continuity-constrained DVD-minus-master Viterbi candidate path and sampled-range report |
| `scripts/measure_break_edges.py` | Long-window content offsets, DP comparison, score/runner-up; clips windows away from adjacent recorded blacks |
| `scripts/extract_candidate_frames.py` | Bounded exact-index PNG extraction; caps output/select terms and records hashes |
| `scripts/measure_audio_rate.py` | Multi-window audio-vs-picture-map diagnostic with OLS and robust checks; never auto-selects a correction |
| `scripts/ld_dvd_audio_compare.py` | Optional report-only LD/DVD envelope and band comparison; never selects a rate or gates QC |
| `scripts/measure_pad_audio.py` | Report-only PAD_1 audio occupancy vs nearby PCM controls; never recommends a rate |
| `scripts/measure_ld_black.py` | Report-only descriptor-luma dark-run intervals across selected thresholds |
| `scripts/efm_paths.py` | Episode-scoped EFM WAV/report path plan with existing-output refusal |
| `scripts/audio_timing_diagnostic.py` | DVD-free duration/PAD report; candidate-only by default, or `--promote-dvd-free` for the user's explicit no-DVD selection |
| `scripts/build_ld_synced.py` | Sample-accurate AudioSkip + exact target sample count into FLAC; audio-only, refuses overwrites and records recipe/hashes |
| `scripts/build_map.ps1` | TFM log + optional motion trace to cadence report, deinterlace map, AVS |
| `scripts/build_map_vpy.py` | macOS VapourSynth rendering of measured cadence outputs; does not re-measure and refuses an unselected deinterlace path |
| `scripts/build_motion_trace.py` | macOS N−1 raw-unit motion trace paired with the TFM log frame domain |
| `scripts/build_padfix_vpy.py` / `scripts/encode_episode_mac.py` | macOS source-derived PAD replacement and non-encoding validation/single video-only final x265 pass via `vspipe` |
| `scripts/remux_padfixed_mac.py` / `scripts/vspipe_util.py` | macOS stream-copy remux with packet identity; shared safe VapourSynth→FFmpeg transport |
| `scripts/encode_episode.ps1` | Windows non-encoding AVS validation and the single final video-only x265 encode from the source-derived PAD-repaired AVS |
| `scripts/retime_dvd_assets.py` | Measured break schedule -> DVD 5.1 FLAC/SRT/map plus output-silence gap verification; short gaps remain LIMIT |
| `scripts/pad_audit.py` / `fullres_check.py` | Detect map-matched PADs in the source-derived post-IVTC `.avs`/`.vpy` and confirm them at full raster |
| `scripts/rife_padframes.py` / `build_padfix_avs.py` | Generate replacements only for confirmed PADs and wrap the source-derived AVS without changing frame count |
| `scripts/encode_padfixed.ps1` / `remux_padfixed.ps1` | Former second-pass encoder is retired; remux stream-copies tracks with packet-identity checks |
| `scripts/scan_padfixed_black.py` / `verify_pad_identities.py` | PAD-black and replacement identity gates |
| `scripts/av_check_output.py` / `scripts/ld_only_sync_check.py` / `sync_check.py` | DVD-referenced absolute A/V, no-DVD exact timeline/sample-budget validation, and between-track drift checks |
| `scripts/qc_report.py` | Final delivery report; skipped/unverified required checks fail closed |
| `scripts/regen_plain_text.py` | Rebuild the ASCII companion from the main Markdown runbook |

### V14 entry points (authoring candidate)

```text
python scripts/bootstrap_deps.py --print --json
python scripts/doctor.py --json [--episode S01E07]
python scripts/run_stage.py --list
python scripts/run_stage.py --episode S01E07 --stage encode-final        # plan only
python scripts/run_stage.py --episode S01E07 --stage encode-final --execute
python scripts/generate_runbooks.py --check
```

The stage runner dispatches one allowlisted action at a time; it is not an unattended
whole-project pipeline. `download` requires `--execute` and the downloader's own
profile/curl, Archive integrity, sidecar, and free-space checks, but intentionally skips
the full doctor so the transfer can start early. Every other write-capable stage requires
`--execute`, a doctor pass, and its declared manifest gates. It never runs arbitrary shell
text, performs cleanup, or replaces helper-level refusal/QC checks. Linux x86_64 can check its candidate dependency lock but
production stage execution is blocked until the complete Linux workflow is validated.

### V9 ordered workflow orchestration (retained in V10)

`scripts/run_workflow.py` executes an ordered JSON plan by invoking only allowlisted
`run_stage.py` stages. It is dry-run by default, does not run arbitrary shell text, choose
measurements, install tools, or bypass stage gates, and is not a complete automatic episode
graph. The root `CONFIGURE HERE FIRST.json` demonstrates a no-DVD intake ending at input
validation. DVD-backed intake requires a separate plan with the exact matching `--dvd` path.
The intake plan records choices for audit; it does not infer or verify them against media.
Review the initialized manifest's identity before proceeding to a processing plan.

Fill the template's placeholders, editing only `_operator_inputs_EDIT.*.value`; do not add
machine profile, output, or approver values elsewhere. The plan rejects unresolved
placeholders and a DVD mode that conflicts with this no-DVD template. The executor creates
the job ID from episode code, confirmed title, and UTC timestamp. It records `approved_by`
from the local account and `approved_at_utc` automatically; neither is an operator input.

```text
python scripts/run_workflow.py --plan "CONFIGURE HERE FIRST.json"                 # plan only
python scripts/run_workflow.py --plan "<completed plan.json>" --execute
python scripts/run_workflow.py --plan "<same plan.json>" --execute --resume
```

Execution prints an ASCII progress bar to stderr for every stage and review step. It updates
when steps start/finish and periodically during long or quiet stages, showing completed plan
steps, the current stage, elapsed time, and time since child output. In terminals updates are
frequent; redirected output receives less-frequent heartbeat lines. The bar is advisory: it
tracks plan-step completion, not percent-complete inside an encoder or renderer, and never
changes cancellation/retry behavior.

Job state and the combined stage log are written under the selected episode workspace's
`logs/` or the profile's local `roots.logs`. A per-job OS lock, exact plan/profile/workflow
identity checks, and declared input/output fingerprints protect state. Completed steps are
skipped only when their fingerprints still match. Large files use size/mtime plus
first/last-block hashes by default; `fingerprint_policy: "full"` requests full SHA-256.
Only a plan step explicitly marked `resume_policy: "download"` can retry an interrupted
download. Other failed/interrupted steps stop for inspection. The lock is per job ID; it is
not a cross-job same-episode claim mechanism.

V9 orchestration validation was fixture/unit testing only. V10 adds report-only PAD/LD-black
diagnostics, episode-scoped EFM path planning, refusal of extrapolated A/V offsets, clipped
break-edge windows, workspace legacy compatibility, and generated-retime silence verification.
The new retime verifier's real-media output check and the new measurement diagnostics have
not been run against a production episode; synthetic tests do not establish full end-to-end
or new macOS support. Linux production remains blocked, and the Mac cadence-dispatch issue
remains open.

### V10 measurement and retime safeguards

`retime_dvd_assets.py` now verifies the generated DVD 5.1 output against planned gap
positions. A measurable mismatch stops the stage; sub-50 ms gaps are explicitly `LIMIT` and
appear as a QC warning, never as a confident gap-verification pass. The tool does not move a
cut automatically. `measure_ld_black.py` and `measure_pad_audio.py` write report-only
threshold/energy evidence; neither picks a threshold/rate or edits the episode manifest.
`measure_break_edges.py` excludes adjacent black intervals from content windows, and A/V
offset checks refuse to extrapolate beyond sampled alignment coverage. V10 also preserves
legacy workspace creation metadata while inferring missing storage mode and prevents
repeated `shared_workspace.py init` from silently creating another matching workspace.

V10 tests are synthetic/unit and local-fixture based. No real episode was reprocessed to
validate the new retime detector or measurement reports, and no new macOS or Linux production
support claim is made.

### Per-device episode workspaces (every README location)

The workspace is relative to the **parent folder of the README.md that starts the job**;
it is not tied to W: or to one shared-drive copy. The expected layout is:

```text
<README folder>/                       # release/source tree: read-only
  README.md
  devices/                             # the only writable area beneath the README folder
    <device>-<stable-id>/                # one stable folder per machine
      episodes/<code>/                  # DS9 and legacy bare codes
      episodes/VOY-<code>/              # Voyager, same-code safe
        manifest/
        logs/
        references/
        cut-pad-images/
```

Keep machine profiles and heavy work/render/download roots outside the README folder. Use
this read-only release/device-workspace mode for any `ld-pipeline` location, not just W: or
network shares: set `shared.enabled=true` and `shared.release_root` to the parent folder of
the README being followed. `shared_workspace.py configure-profile --source <local-profile>
--output <new-local-profile>` creates a new external profile for that README folder.

**Required setup, not a workflow gate:** at the start of each episode, create/reuse the
machine's stable device folder and episode workspace with `python scripts/shared_workspace.py ensure --profile
<local-profile> --episode <code>`. `run_stage.py` also attempts this automatically. If
`devices/<device>-<stable-id>` cannot be created or accessed, it warns and continues using an
isolated fallback under the device profile's local `roots.logs` (or `roots.work`). It never
falls back to writing elsewhere in the README/release tree. The fallback data is local and
won't be visible to other devices until deliberately published/copied. Keep the returned
`instance_root` for this episode to resume in the same workspace; do not start a second
concurrent run against it.
The suffix is stored in a machine-local identity file under
`~/.config/ld-pipeline/device-identities/`, outside the release tree. If legacy folders are
ambiguous or unverifiable, the helper does not guess, move, or delete them; use
an explicitly reviewed `shared.device_instance_id`/`LDP_DEVICE_INSTANCE_ID` or the local
fallback.
Initialize/reuse it before creating the manifest; the initializer and stage runner also
resolve it automatically. Preserve and explicitly reconcile any legacy root-level manifest
rather than copying or overwriting it without confirming ownership.

```powershell
$profilePath = '<device-local profile outside the README folder>'
$url = '<verified Archive item URL>'
$source = '<local source-download destination>'
$workspace = python scripts/shared_workspace.py ensure --profile $profilePath --episode S01E07 | ConvertFrom-Json
$instance = $workspace.workspace.instance_root
if ($workspace.warning) { Write-Warning $workspace.warning }
python scripts/run_stage.py --profile $profilePath --instance-root $instance `
  --episode S01E07 --stage download --execute `
  --script-arg=-Url --script-arg=$url --script-arg=-Destination --script-arg=$source
python scripts/run_stage.py --profile $profilePath --instance-root $instance `
  --episode S01E07 --stage init-episode --execute `
  --script-arg=--url --script-arg=$url --script-arg=--downloads --script-arg=$source `
  --script-arg=--no-dvd
```

For a DVD-backed episode, pass `--script-arg=--dvd --script-arg=<DVD path>` instead of
`--no-dvd`. If cut-PAD stills are available, ask whether to use them before adding
`--script-arg=--cut-pad-images --script-arg=use` or `skip`. The download goes to the
configured local source folder; manifests, logs, references, and cut-PAD images go to this
episode's workspace when available, otherwise to its local fallback.

The device should alter only its own `devices/<device>-<stable-id>/` subtree. Do not edit
release files or another device's folder. For shared-drive ACLs, the administrator should
grant read/execute on the README/release tree and make `devices/` its sole writable child
(pre-provision it or allow creation of that exact directory). Grant each device Modify
only to its own instance subtree. The workflow does not change ACLs. Application
guards reject release-root and peer-instance writes; the doctor still blocks processing if
the release root itself appears writable.

Logs are intended as bugfix evidence, not just summaries: keep the unique stage `.log` and
JSON report, exact commands, workflow version, gate outcomes, complete tool stdout/stderr,
exit codes, Orc JSON refresh output, FFmpeg command/version sidecars, hashes, preflight and
tool-version reports. Keep full artifacts rather than filtered error lines, never overwrite
or clean prior runs, and do not put credentials/tokens in command lines. The workspace's
`logs/README.md` records this policy. Shared-mode logs redact configured machine paths; they
do not scrub arbitrary secrets. `run_stage.py` records active duration and gate/action
breakdowns per invocation; time between invocations is excluded. For manual stages, use
`scripts/stage_clock.py` and pause it while waiting for a human gate response.

**ACL status for W:** no ACL changes were made. The current inherited root ACL still grants
the connected LenovoFlex account Full Control, so the W release root is not OS-enforced
read-only and processing remains doctor-blocked until an administrator restricts it.

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
  (`roots.downloads_root` + `roots.downloads_template`). `limits.heavy_jobs` is an
  operator policy hint only; the current runner does not enforce a global heavy-job
  scheduler, so do not start another heavy job concurrently unless you have verified
  available resources.
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

### Mac-side completed end-to-end evidence

The M5 log records a verified 18/18-file E10 download and a complete S01E10 workflow through
final encode, multi-track remux, and delivery. It reports all nine workflow stages green
(225 pass, 0 warn, 0 fail); initial QC exposed a seek-only audio dropout, which was
diagnosed, fixed, and reverified. The corrected master had 0/1067 audio-less clusters, all
six formerly failing seeks decoded full windows, and all four packet-identity checks passed.
The M5-local preflight also passed (36 pass, 1 warn, 0 fail). This validates the tested M5
macOS arm64 configuration, not a fresh install or every Mac model.

`download_archive.ps1` uses `System.IO.DriveInfo` for POSIX free-space checks with a
Windows `Get-PSDrive` fallback. `align_dvd.py` from the snapshot remains excluded: the
authoritative descriptor pair supplies `align_npy`. For a new Mac, run current preflight,
tests, and ListOnly checks from its local bundle/profile before processing; the M5
completion does not replace machine setup checks. Recompute free-space budgets on every
host; the M5 measurements do not transfer.

## Runbook files in this folder

- `DS9 LaserDisc OpenCode Runbook.md` - canonical detailed technical procedure.
- `DS9 LaserDisc OpenCode Runbook - Qwen4B.md` - concise ACTION/VERIFY/STOP variant.
- `DS9 LaserDisc OpenCode Runbook - Qwen4B-Luna.md` - reasoning/ambiguity-focused variant.
- `DS9 LaserDisc Plain Text Instructions.txt` - ASCII companion of the canonical runbook.

`README.md` governs startup and machine setup. The canonical runbook governs
measurement and quality rules. A model-specific variant may change presentation,
not the workflow, thresholds, manifest contract, or stop conditions.
