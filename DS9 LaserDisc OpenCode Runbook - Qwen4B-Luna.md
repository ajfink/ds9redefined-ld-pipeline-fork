# DS9 LaserDisc → Upscale-Ready Master: Qwen4B + GPT-6 Luna Fallback

**Primary model:** local `Qwen3.5 4B`.
**Fallback model:** `opencode-go/gpt-6-luna`, invoked only for the unresolved cases
listed in this file after the local model completes the prescribed checks.

This variant uses exactly the same technical procedure as `Qwen4B.md`; only the
escalation policy differs. Qwen3.5 4B remains responsible for commands, measurements,
manifest edits, and all deterministic pass/fail gates.

**Canonical companion:** `DS9 LaserDisc OpenCode Runbook.md`
**Plain-text companion:** `DS9 LaserDisc Plain Text Instructions.txt`

This is the same ACTION / VERIFY procedure as `Qwen4B.md`, with an explicit, rarely used GPT-6 Luna fallback policy. The technical procedure is intentionally kept identical to reduce model-variant drift.

**This is a repeatable process, not a license to copy Emissary-specific trims or timing.** Anything marked **MEASURE** must be measured for the current episode. Never reuse a number from §11.

The final product is an upscale-ready MKV. **Do not launch Topaz.**

---

## 0. How to use this runbook

### Your operating rules

1. Execute every mechanical step in order. A mechanical step is one where both the action and the success condition are written down below.
2. Do **not** ask before doing a mechanical step. Just do it and check the VERIFY line.
3. On an ambiguous measurement, finish every local check in the relevant section first. If an explicit fallback trigger below applies, use GPT-6 Luna once, then rerun the VERIFY. Otherwise STOP and ask the user. Hard failures and numeric gate failures are never sent to a model for overruling.
4. A failed hash, missing file, command error, frame-count mismatch, or failed numeric gate is a factual failure. Do not ask GPT-6 to overrule it; report **step, command, actual result, expected result, and artifact path**.
5. Never guess a number. Never claim a gate passed without running the check.
6. A cleanup request applies only to the exact user-named download folder. Use the
   Recycle Bin by default; permanent deletion needs explicit confirmation for that
  exact path. Never delete a reference DVD, master, output, or sibling asset. If the
  target is missing, report that and stop.
- If cleanup and a new episode job are in one prompt, mark cleanup `done/not found/blocked`
  and keep its target scope closed. A missing download folder is not a reason to search
  for or delete its DVD. Do not ask the user again after they already chose a sequence.
- In PowerShell, `Remove-Item`/`rmdir` permanently removes; it does not send data to the
  Recycle Bin. Do not use it to implement the default cleanup. If a supported Recycle
  Bin operation is unavailable, stop and ask.

### Tool, download, and cleanup guard

- In this OpenCode setup, `execute`/Code Mode accepts JavaScript only. The
  `import`/`export` `sourceType: module` error means Python went to the JS parser;
  use the `shell` tool for Python/PowerShell. Do not retry the same code in `execute`.
- Shell is Windows PowerShell 5.1: no `&&`, Unix `dir` flags, `rmdir /S /Q`, or textual
  `timeout 30s; ...`. Set timeouts/background on the shell tool itself. Use one command
  per call; a semicolon does not stop later commands when an earlier command fails.
  Correct a shell/tool syntax error once; do not create ad hoc check scripts in CWD.
- Do not search an entire drive or invent `Search-Path`/`Find-Path` commands. Read
  `machine.json`, then list/check only the exact profile-rooted path.
- A preflight passes only with the full `PREFLIGHT OK.` footer and exit code 0. Never
  call it passed from truncated output or an exit code of 1.
- "Clean up" is not permanent-delete authorization. Only the exact user-named
  download directory is in scope; default to the Recycle Bin. Permanent deletion needs
  explicit confirmation after showing the exact full path, inventory, and size. Never
  delete or substitute anything under `roots.dvd_root`, `roots.prores`, `roots.out`, or
  `reference-runs`. If a target is missing, report "not found"; do not infer success.
  Verify final output and current on-disk QC, and preserve download-manifest/QC evidence.
- `download-manifest.csv` verifies source hashes/sizes; it is not delivery QC. Read the
  current on-disk QC report and verify the delivered file; never rely on prior chat
  memory or manifest presence as proof of QC success.
- Keep intermediates needed after the current stage under `roots.probe`, `roots.logs`, or
  their declared output root; OS Temp is disposable only. Do not run Temp cleanup during a
  job or continue if a required intermediate vanished without verifying/regenerating it.
- Before each new episode, follow the key-tool update gate and Windows/Mac tool matrix
  in `README.md` / `PORTING.md`. Check upstream versions for the exact profile-resolved
  tools. If any are missing or outdated, show the component/state/version list and ask
  whether to install/update specific components, **Update all**, or **Continue anyway**. Apply only the user's
  choice, record it, and freeze the toolchain after the episode starts. Do not use blanket
  package upgrades.
- Resolve one exact `$source` from the user's path or `roots.downloads_template` and
  use it unchanged for preview and download. Never lowercase/rename it. The downloader
  preview prints exact labels/count, aggregate bytes, and `SPACE GATE`; require PASS.
- Run large transfers with the shell tool's background mode and save its job ID. A
  short foreground timeout is an interrupted command, not evidence of a network stall.
  The downloader writes its manifest only after every selected file is complete and
  verified. If a command times out, check process status and timed file-size samples;
  resume the same destination only after confirming the previous process is stopped.
  Never run two downloaders for the same item.
 - Sequence is: key-tool update inventory -> ask update choice -> apply selected updates
    or record Continue anyway -> machine preflight -> list-only/space gate -> start one
    verified download -> immediately work ahead on independent prep (DVD probe, readiness,
    space estimates, next-step commands; do not wait idle) -> complete verified download
    -> `init_episode.py` -> `validate_episode.py --stage inputs`. Do not upgrade mid-episode,
   validate before download completion, or create the manifest before the download manifest exists.
 - Record `WORKFLOW_VERSION` at job start and confirm it before each stage. If workflow files
   or version change mid-job, stop before the next stage and reconcile the current gate.
   Before write-heavy stages, run `scripts/check_space_budget.py` with an episode-specific
   peak-space estimate; preserve the configured reserve.

### Legend

| Marker | Meaning |
|---|---|
| **ACTION** | Do this exactly. |
| **VERIFY** | Check this. It must pass before continuing. |
| **DELEGATE** | After local checks, send a batched evidence packet to `opencode-go/gpt-6-luna` via the configured `/deep` route. |
| **STOP** | Hard failure or ambiguity unresolved after one permitted fallback. |
| **TRAP** | A known failure. Read before acting. |
| **MEASURE** | A value you must obtain for *this* episode. Never copy one from §11. |

### GPT-6 Luna fallback contract (rare, evidence-only)

1. Qwen3.5 4B runs all commands and obtains all measurements first.
2. Use `/deep <task>` with target `opencode-go/gpt-6-luna` only for a listed ambiguous case. Do not delegate routine file checks, arithmetic, path lookup, command execution, or explicit numeric gates.
3. Send one batch containing: episode/section; exact question; relevant measurements and their units; log/report paths; methods that agree/disagree; and which VERIFY checks passed.
4. Require the response format: `Decision: PASS / FAIL / MEASURE MORE / STOP; Evidence: ...; Next action: ...`.
5. Apply the answer only if the next VERIFY passes. If `/deep` cannot select this model, or the answer is inconclusive/contradicts a hard gate, STOP and ask the user. Never silently substitute another model.

### 0.5 Exact fallback triggers

Use `/deep` only after local checks are complete and one of these remains unresolved:

1. Archive/DVD identity still conflicts after metadata and FFprobe checks.
2. Stack metrics conflict or the alignment sweep has no unique, supported peak.
3. Cadence/insert treatment remains ambiguous after the full TFM and motion review.
4. Multiple fade-up/trim candidates remain indistinguishable after descriptor checks.
5. Audio-rate fits are statistically inconclusive or independent methods conflict.
6. PAD/content/cut classification remains ambiguous after full-raster, flank, and DVD checks.
7. Break-edge methods still disagree by more than 3 frames after one remeasurement; send one batched request.
8. A retime gap remains negative after verifying signs and silence boundaries.
9. Independent tail-alignment windows conflict after remeasurement.
10. A black hit remains unclassified after PAD-map/full-raster/DVD checks.

Never invoke the fallback for a missing file, hash/size mismatch, command error, frame-count mismatch, out-of-threshold QC value, or routine script execution.

### Stop/fallback checkpoints

Read these now. If an ambiguity-only STOP appears, check the exact fallback triggers in section 0.5 first. Factual failures remain STOP.

1. §1 — DVD or archive item is ambiguous.
2. §3 — stack refresh reports a reference that is not input 1, or a gate fails.
3. §4 — cadence VERDICT is not a clean uniform 3:2, or the fade-up cannot be anchored.
4. §5 — the audio rate fit is inconclusive or the act breaks disagree.
5. §6 — a dark single-frame event cannot be classified as PAD vs real content.
6. §7 — the DVD↔LD offset measurements disagree beyond tolerance.
7. §7 — a retime gap comes out negative.
8. §10 — any QC item fails.

---

## 1. Job brief and machine setup

### ACTION - read the canonical entry point first

Read `README.md` in this folder. It defines the current machine/profile setup, the
three user inputs, the bundled helpers, and the final QC contract. This Qwen variant
provides the ACTION / VERIFY / STOP presentation of the detailed procedure; it does
not override the README or manifest.

### ACTION - use the profile, never fixed drive letters

Run `preflight.ps1` and continue only after it reports `PREFLIGHT OK`. Resolve tools
and roots from `machine.json` (`roots.work`, `roots.downloads_root`, `roots.dvd_root`,
`roots.out`, etc.). On a copied bundle, create a fresh local profile with
`preflight.ps1 -Write`, fill any custom roots, and rerun preflight. Do not paste the
current machine's X:/Z: paths into commands.

Run commands from this bundle folder. Initialize the job variables from the profile
and the three inputs supplied by the user:

```powershell
$profile = Get-Content .\machine.json -Raw | ConvertFrom-Json
$ep = '<derive and verify from Archive/DVD>'
$title = '<verified title>'
$url = '<user-provided Archive item URL>'
$source = '<user-provided destination; if omitted, derive it from the profile template>'
$dvd = '<user-provided, verified DVD MKV path>'
$work = $profile.roots.work
$orc = $profile.tools.orc
$ff = $profile.tools.ffmpeg
$fp = $profile.tools.ffprobe
```

### ACTION - confirm the DVD and Archive item

The user's job prompt supplies an Archive item URL, a download destination, and the
DVD MKV path. Confirm the Archive item identifier and DVD identity with metadata/FFprobe.
An Archive metadata title that repeats the item slug is not, by itself, a human episode
title. Derive the title from verified DVD/Archive evidence; if they disagree or the
URL/DVD is ambiguous, STOP and ask. Emissary's DVD is never a substitute.

### VERIFY

- Archive URL resolves to the intended episode.
- DVD exists and its title/streams match the episode.
- Destination and profile work/output roots have sufficient free space.
- The episode manifest will be created by `init_episode.py` after all source files verify;
  do not create a placeholder manifest to get ahead of the download. Unknown measurements
  remain null/unset, not copied from section 11.

### Other rules

- Derive `$source` from the profile template if the user supplied no destination:
  `Join-Path $profile.roots.downloads_root ($profile.roots.downloads_template.Replace('{episode}', $ep))`.
- Run `scripts/download_archive.ps1 -Url $url -Destination $source -ListOnly` first;
  require exact capture labels/count and `SPACE GATE PASS`. Do not estimate the item
  size by hand. Capture complete output and save `$LASTEXITCODE` before inspecting it;
  do not pipe the running command to `Select-Object -First` because that can break the
  pipe and produce a false `-1` exit. Launch the same command without `-ListOnly` in
  shell background mode.
- Wait for the downloader's final all-files-verified message and `download-manifest.csv`
  before running `init_episode.py`; only then run `validate_episode.py --episode <ep>
  --stage inputs`. No manifest during a partial download is expected.
- Use `scripts/select_stack_reference.py` for a fresh graph/reference refresh; it never
  renders. Follow ACTION 3.4 for the explicit render command and output gates.
- `scripts/build_map.ps1`, `scripts/encode_episode.ps1`, and the PAD/QC scripts resolve
  paths from the profile and manifest.
- Preserve inputs and logs; never overwrite or clean files without explicit approval.
- DVD picture-map/edge adjudication and audio-rate fitting remain measurement-led.
  Once the manifest contains the adjudicated schedule, use
  `scripts\retime_dvd_assets.py`; never run E04/E05-specific retime scripts on a new
  episode. If alignment/edge measurements cannot be validated, STOP.
- Emissary is the only two-part episode. Its per-part workflow is not covered by the
  generic one-part encoder/QC.

### Copy/paste starter prompt (for the user)

```text
Read README.md, then DS9 LaserDisc OpenCode Runbook - Qwen4B-Luna.md.
LaserDisc Archive URL: <https://archive.org/details/...>
Save source files to: <absolute folder path>
Matching DVD file: <absolute path to the correct DVD MKV>
Final output folder (optional): <path, or machine.json roots.out>
Derive and verify episode/title. While the verified download runs, do the independent
DVD/machine prep in section 2. Measure every timing only when its required artifacts
exist; update episodes/<code>.json and follow each VERIFY/STOP gate. Preserve inputs and
deliver only after qc_report exits 0.
Never delete a reference DVD or any path outside an exact user-named download folder.
No Topaz.
```

---

## 2. Archive pages and input inventory

### ACTION — locate the archive item

Known item pages:

- Emissary Part I: `https://archive.org/details/DS9-S01E01-LaserDisc-Copies`
- Emissary Part II (item is named S01E02): `https://archive.org/details/DS9-S01E02-LaserDisc-Copies`
- A Man Alone (one-part example): `https://archive.org/details/DS9-S01E04-LaserDisc-Copies`
- Past Prologue (one-part example): `https://archive.org/details/DS9-S01E03-LaserDisc-Copies`
- Others generally follow `https://archive.org/details/DS9-S01E##-LaserDisc-Copies`.

**VERIFY** — the page title matches the episode.

**Luna fallback - trigger 1 (section 0.5):** only if the local Archive/DVD identity check remains ambiguous.
**STOP** — if the page and title disagree.

### ACTION — inventory the captures

Use the item's "Show All"/original files and `*_files.xml` metadata. Record for every capture:

- capture label (L1, L2, M1, P1, P2, P3, ...)
- `.tbc` size + checksum
- `.tbc.json` size + checksum
- whether a **raw `.efm`** (or `.efm.7z`) exists
- file sizes and checksums

**TRAP** — labels and counts vary per item. Emissary used L1/M1/P1/P2/P3/P4; A Man Alone and Past Prologue have L1/L2/M1/P1/P2/P3. Use the **actual complete set**, never a remembered set.

**TRAP** — raw EFM availability varies per capture. E04 shipped raw EFM for L1/P1/P2/P3 but only uploader `.pcm` for L2/M1. **A capture without raw EFM cannot be audio-verified.** Record this before choosing the audio source.

**VERIFY** — every large TBC and JSON has an individually checked hash/size. Do **not** trust a generic "PARTIAL" status without checking each file.

**ACTION** - check free space on the user-provided download destination and configured local `roots.work`; do not assume Z: or X:.

**STOP** — if hashes/sizes cannot be verified, or free space is insufficient and you are unsure which drive to use.

### ACTION — download with a resumable, verified job

Use the bundled `scripts\download_archive.ps1`; first run with `-ListOnly`, then download. The downloader must:

1. Fetch **only** required inputs: TBC + JSON + raw EFM. **Never** the archive `.pcm` or `.log`.
2. Check each file's size and MD5 against item metadata.
3. Write a `download-manifest.csv`.
4. Be idempotent: skip verified files, resume partials with `curl --continue-at -`.
5. Run detached so it survives interruptions.

**Monitoring** — every ~30 min: sample the active curl target's size over 30 s.

| Observation | Meaning | Action |
|---|---|---|
| Growing | healthy (40–55 MiB/s; ~454 GiB ≈ 2.5 h) | keep waiting |
| **No growth for ~5 min** | stalled | kill only the curl process, restart the downloader |

**VERIFY** — on restart, the script re-verifies finished files by MD5 (a few minutes per 74 GB) and resumes the partial from its exact byte offset.

**TRAP** — never run two downloaders for the same item concurrently.

### ACTION — use download time for independent prep

Once the transfer is confirmed healthy, do useful prep that does not require LD captures:

1. Verify the exact matching DVD path and episode/title against the Archive item. If the
   supplied DVD is missing, only consider an exact, unambiguous match under configured
   `roots.dvd_root`; if identity is uncertain or conflicts, STOP and ask. Never substitute
   another episode's disc.
2. Probe and record DVD video, audio, and subtitle streams: codecs, dimensions, frame
   count/rate evidence, durations, audio channel layouts, subtitle format/language, and
   source size/hash. For English SRT, check cue count, monotonicity, and range. Do not
   trust `r_frame_rate` alone; if packet PTS are non-monotonic, decode sequentially and
   never use `-ss`.
3. Confirm the version inventory/update choice made before the transfer, profile roots,
   free-space gates, and preflight/capabilities. Do not install updates mid-transfer or
   mid-episode; if an update is discovered now, defer it to the next episode or ask whether
   to pause and redo the update gate. Save probe output and a provenance-labeled prep note
   under `roots.probe` or `roots.logs`; do not modify the source DVD.
4. Prefer lightweight prep during the transfer. Defer a full DVD decode if it would
   contend with the download's disk or system resources. Record possible DVD 5.1/SRT
   availability as a candidate only; keep optional deliverable tracks disabled until
   source and retiming data are verified.

**VERIFY** — DVD facts and their evidence are saved outside source folders, with
uncertain facts marked unknown. Do not populate the episode manifest yet.

**TRAP** — do not build/refresh/render the stack until every capture verifies. DVD-only
prep cannot measure the DVD↔LD picture map, LD audio rate/offset, cadence, trims, or
retiming schedule. Never copy another episode's values or fabricate a temporary manifest.

After all files verify, run `python scripts\\init_episode.py --url <Archive-URL>
--downloads <folder> --dvd <DVD.mkv>`. Confirm the derived episode/title/capture
inventory and capture-independent AVS/DVD-audio file keys, then run
`python scripts\\validate_episode.py --episode <ep> --stage inputs`. Add `ld_native` and
`ld_corrected` file keys only after the audio capture and correction rate are measured;
do not guess those filenames during initialization.
Set `capture_count = len(episodes[ep].captures)` from that manifest and use it in all
source/map/render checks. Do not fill stack/audio/video measurements yet; obtain them
in the following stages.

---

## 3. Build the weighted ProRes master

### ACTION 3.1 — select Orc's reference and create the refreshed project

The weighted template supports 2-6 verified captures. With one capture, STOP (no weighted-stack gain); with more than six, extend and test the template first.

Run `python scripts\select_stack_reference.py --episode <ep>`. If no reference is
declared, the helper uses the first manifest capture only as a scratch seed, asks Orc
for `suggestedReference`, maps that source index to its capture label, and repeats with
the suggestion first until stable (at most three refreshes). It records the selected
reference and promotes the refreshed project; it never renders the master.

**VERIFY** — report says the refresh completed/saved, `source_count == capture_count`, the final suggestion is input 1, and the manifest's `reference` matches that capture. Inspect runner-up, coverage, stack-noise and fit metrics in the report. The helper does not use a custom noise-only score.

**STOP** — if there is no valid recommendation, Orc does not finish/save, the suggestion does not stabilize, or a pre-existing reference conflicts. `--reselect` is only for an intentional replacement. The helper never overwrites an existing stack project.

**TRAP** - a careless label replacement once left `@@placeholder@@` strings and a doubled label in the YAML. After any hand-edit, verify the project file. Safe recovery: clear everything and let refresh rebuild it.

**TRAP** - an empty `value:` may print `invalid range spec 'null'` during refresh. Verify Orc reports success and refreshed graph values are present; do not accept an unsaved refresh.

### ACTION 3.2 — record the measured alignment map

The selector's final Orc refresh has already measured/saved the maps and alignment.
Read the alignment map from its report/project and record it in
`episodes/<ep>.json` as measured. Do not hand-derive offsets or run a duplicate refresh.

**VERIFY** — every capture appears once; all maps and source alignment are populated;
the selected reference is input 1; the project sink matches `files.stack_master`; and
the full refresh reports the expected source count. If any check fails, STOP.

### VERIFY 3.3 — graph configuration

Confirm the auto-selected, refreshed project contains exactly this graph:

```
one tbc_source + one frame_map per capture
        -> source_align
        -> stacker Weighted
        -> video_sink
```

The selector has already put the stable Orc-recommended reference first; do not reorder
or hand-edit the graph after refresh.

Sink baseline (all required):

```
mov-prores, prores_profile=hq, decoder_type=ntsc3d,
adapt_threshold=0.1, chroma_weight=2.0, ntsc_phase_comp=true,
display aspect 4:3
```

**TRAP** — do **not** add unmeasured crops or post-stack filters.

### ACTION 3.4 — render

The reference selector only refreshes; it does not render. Before a render, estimate peak
master plus scratch space and require:

```
python scripts/check_space_budget.py --stage stack-render --planned-gib <estimate>
```

Then run the manifest project with the profile-resolved Orc CLI, for example
`orc-cli <project> --process`, targeting manifest `files.stack_master` under the profile
`prores` root. Launch/monitor it as a background job; validate the artifact/log rather than
relying only on the exit code.

**TRAP — the single biggest speed lever:** render from the machine profile's local fast work volume. Historical local-vs-SMB throughput differs by machine; stage/measure on this host and do not assume a drive letter.

**VERIFY (first 60–90 seconds)** — all must hold:

- [ ] One "Loaded" line per input.
- [ ] Expected processing frame range shown.
- [ ] StackerStage reports `"inputs":[capture_count,capture_count]` on **every** frame — this proves every declared capture contributes, not a pass-through.
- [ ] If the report shows pair comparisons, reconcile the count with this episode's
      capture/pressing groups; do not copy E07's five-capture `10` as a fixed count.

**VERIFY (on completion)** — `Successfully wrote <stack.frames> frames` equals the refreshed source_align output count recorded in the manifest, with **zero** unrecoverable samples.

**STOP** — if `inputs` is not `[capture_count,capture_count]`, frame counts disagree, or there are unrecoverable samples.

### ACTION 3.5 — stack gate

This is an evidence review, not a separate StackerStage tool invocation. Use the refresh
report and render log. Compare against a single-capture reference where available.

**VERIFY** — all of:

- [ ] It is proven **not** a pass-through.
- [ ] Alignment confirmed by a small offset sweep.
- [ ] Pooled picture residual, correlation, and noise reduction checked.

**TRAP** — Emissary's example was correlation 0.998–0.999 and stack/single noise 0.80–0.83. **Do not copy those as universal thresholds** without understanding the tool's capture/reference assumptions.

**Luna fallback - trigger 2 (section 0.5):** only if local stack metrics remain contradictory; clear failures are rework/STOP.
**STOP** — if the gate result is outside a defensible range for this episode, or you cannot interpret it.

---

## 4. Cadence, AVS, and episode-specific trims

**TFM compatibility policy** — current supported software on Windows and Mac is treated
as sufficient for TFM compatibility. No parity flag, evidence lookup, or clip run is a
cadence prerequisite. A historical parity test is recorded in the M5 log §8, but is
informational only. This assumption does not measure the current episode's cadence.

### ACTION 4.1 — generate the TFM log

Run a full-side TFM log from the native 29.97 master:

```
TFM(order=-1, pp=0, chroma=true, slow=2)
```

Use bundled `scripts\build_map.ps1 -Episode <ep>`. Read its full summary and its `VERDICT`. Add motion trace for the native-30p gate.

**VERIFY** — you have a `VERDICT` value.

**TRAP** — measure cadence. **Do not infer it from `idet` or motion alone.**

### ACTION 4.2 — cadence decision

| Measured result | Action |
|---|---|
| Uniform 3:2 | `TDecimate(mode=1, cycle=5, cycleR=1)` → exactly 24000/1001 |
| Interlaced inserts present | Repair **only** the generated targeted QTGMC map; `FPSDivisor=2` is **mandatory** for 1:1 replacement |

**VERIFY** — output is 10-bit and progressive.

**TRAP — never do these:** globally deinterlace, denoise, interpolate, or RIFE.

**Exception:** targeted single-frame `PAD_1` repair only — follow §6's PAD procedure, preserve total frame count, and **never alter cadence/timing**.

**Luna fallback - trigger 3 (section 0.5):** only if cadence/insert treatment remains ambiguous after full TFM/motion review.
**STOP** — if the VERDICT is ambiguous, or the cadence is not uniform 3:2 and the insert map is unclear.

### ACTION 4.3 — one frame domain (critical)

Run the TFM log on the **audio-aligned clip** (`.Trim(pad,0)`, where `pad` = the audio capture's alignment offset), and hand-edit the **same** `.Trim(pad,0)` into the generated encode AVS's `src` line.

**VERIFY** — the `.Trim(pad,0)` in the log script and the one in the encode AVS are identical.

**TRAP** — if they differ, **every repair-map index is off by `pad` frames.**

**TRAP** — the motion probe must run on a **trim-ONLY** AviSynth script. A script that still contains TFM would re-run TFM and overwrite the log. Its output is N-1 values (tblend drops frame 0); `build_map.ps1` pads this automatically.

**TRAP** — `scripts\build_map.ps1` is bundled and resolves roots from `machine.json`.

### ACTION 4.4 — measure per-episode values (**MEASURE**)

Record each of these for the current episode:

- [ ] AVS source `.Trim` alignment offset
- [ ] front-matter/head trim
- [ ] tail trim
- [ ] output duration
- [ ] `-AudioSkip`
- [ ] audio capture offset
- [ ] any hybrid cadence ranges

**TRAP** — the video Alignment Map offset is **not** the audio skip.

**TRAP — frame domains:** pre-decimation trims are source/29.97 frames; post-decimation trims are 23.976 output frames. Log which domain each trim uses.

### ACTION 4.5 — head trim (measured, never predicted)

| Step | Action |
|---|---|
| (a) | `blackdetect` the master head to find the front matter and the episode's first fade-up out of black |
| (b) | Map that master frame to an OUTPUT frame by descriptor-matching the encode AVS output against the master around the region |
| (c) | Trim so the file starts **3 output frames before the fade-up** (the E01/E04 rule) |
| (d) | Compute `-AudioSkip` from the formula below |

**Formula for `-AudioSkip`:**

```
wav_t = (master_frame - (pad + 1)) * 1001/30000
```

where `pad` = the audio capture's alignment offset. (E04: master 1485, pad 3, skip 49.416033 s.) The shortcut `(K-1)*1001/24000` agrees to sub-milliseconds.

**TRAP** — black frames match each other **ambiguously** (corr ~0.94 against any black frame). Anchor on the **fade-up itself** (corr 1.0) and derive neighbors from it.

**TRAP** — do **NOT** predict the final frame count from TDecimate arithmetic. The trailing partial cycle can drop an extra frame. Take the actual count from the non-encoding AVS metadata probe. (E04: 65,347 actual vs 65,349 predicted.)

**TRAP — SAR:** `79:94` and `121:144` both resolve to 4:3 within 0.02 %. The encode emitted `79:94` on both E01 and E04. **Accept it** rather than fighting the mux.

**Luna fallback - trigger 4 (section 0.5):** only if descriptor checks leave multiple fade-up/origin candidates.
**STOP** — if the fade-up cannot be anchored (no clean corr 1.0 match), or the descriptor match is ambiguous.

### ACTION 4.6 — trim to the DVD timeline, not by eye

Head/tail membership is decided by the **measured DVD↔LD picture map**, never by whether a passage merely looks like filler.

**RULE — trim automatically:** anything *before the DVD timeline origin* is LD-only and never ships — disc text cards, the Paramount intro logo, trailers, and other front matter.

**RULE — keep:** anything the DVD itself contains stays — including end-credit text and the Paramount outro logo.

**TRAP** — timeline conformity with the DVD is worth more than a shorter file. The piecewise retime of the 5.1 and SRT assumes both timelines describe the same program; dropping DVD content would shift every later map.

**ACTION** — derive the DVD timeline origin from the DVD↔LD map, then anchor the cut on the first fade-up (**MEASURE** per episode), not on a guessed front-matter duration.

**VERIFY** — output frame 0 sits a few frames inside the opening black and the program fades up into content; no front matter survives.

**ORDERING NOTE** — this rule needs the DVD↔LD picture map, which is built in **§7**. If you have not built the map yet, either build it now or **defer the final head-trim decision until §7**. Do not guess the origin without the map.

**Reference (do not copy):** S01E05 worked example — DVD origin at master ~48.45 s; LD-only head master 0–48.75 s; DVD opening black maps to master ~48.45–49.82 s; episode fade-up master 49.716 s; trim = output `.Trim(1161, 0)` (master 49.583 s, 3 output frames before fade-up at output 1164); DVD tail retained unchanged (credit text DVD 2684.5–2719.2 s, Paramount outro logo DVD 2719.3–2723.2 s). **These are S01E05 numbers — measure your own.**

**Luna fallback - trigger 4 (section 0.5):** only if descriptor checks leave multiple fade-up/origin candidates.
**STOP** — if the DVD timeline origin cannot be determined from the map.

---

## 5. EFM audio: decode and verify, never guess the rate

### ACTION 5.1 — decode

Decode raw EFM with EFMSink. It does **not** auto-find the sidecar and needs `decode_mode=audio`:

```powershell
& $orc --source "tbc_source=input_path='$source\<capture>.tbc':efm_path='$source\<capture>.efm'" `
  --sink "EFMSink=output_path='$work\audio\<capture>-efm.wav':decode_mode=audio:report=true"
```

Unpack `.efm.7z` first if needed. Keep the report and the original WAV.

**TRAP** — never assume an archive `.pcm` is valid. Emissary's uploader `L1.pcm` disagreed with fresh EFMSink audio and was not safe to use.

When multiple captures have raw EFM, decode and compare all candidate reports before
choosing the source WAV. Record post-C2 loss, concealment, uncorrectable/missing sections,
out-of-order sections, and Q-timecode continuity. `POOR` can be triggered by missing
sections even when loss percentages are low; choose from the measured report evidence, not
the label alone. Preserve all native WAVs/reports. Report times are `MM:SS:FF`, not
`HH:MM:SS`. Once `audio.capture` is set, derive `audio.pad_frames` from that capture's
offset in `alignment_map`, using the selector report's final Orc input order (not the
manifest `captures` order); cross-check Orc's `Padding prepended` value. Do not substitute
`video.head_trim` or `video.delta_s`.

### ACTION 5.2 — the four audio gates (all must pass)

**Gate 1 — decoder/capture consistency**
Decode a second raw EFM when available. Cross-correlate long non-silent portions.

- **VERIFY** — high Pearson score and constant offset.
- **TRAP** — this proves decoder/capture consistency **only**. It does **not** prove absolute sync to the video.

**Gate 2 — DVD-free duration/PAD candidate (primary candidate, not automatic approval)**

Run `python scripts\audio_timing_diagnostic.py --episode <ep> --report-out <unique.json>`. It uses the exact native-WAV sample count, full stack duration and Orc frame-map PAD list, and does not read the DVD or write the manifest. Review full-map PADs, head-trimmed PADs, `stack.frames - audio_frames`, and any mismatch. The report may create a separate full-side candidate with `--candidate-out <new.flac>`; preserve native audio and never overwrite the DVD-based candidate.

**TRAP** — `audio.pad_frames` is the selected capture's leading alignment, not the total video `PAD_1` count. Do not substitute one for the other or infer the PAD count by subtracting rounded durations.

**Gate 3 — map first, then DVD corroboration (when available)**

Build the DVD↔LD picture map from the stack master **before final x265**. Once `video.delta_s` and `audio.pad_frames` are measured, run `python scripts\measure_audio_rate.py --episode <ep> --raw-provisional --out <unique.tsv> --summary-out <unique.json>`. It uses geometric skip only to center windows; the intercept absorbs constant origin error. Review every correlation, runner-up and edge peak. Use its OLS and robust Theil–Sen / leave-one-window-out fit as corroboration; it is diagnostic, never an automatic correction.

- Use several long content windows on stable picture-map plateaus, including the tail.
- Exclude act steps, fades/music-through windows, weak/ambiguous matches and edge peaks.
- Never globally fit across an unmodeled edit staircase.
- A fixed offset, rate drift and edit steps are three different quantities; correct them separately.

**Gate 4 — fade-midpoint fit (corroboration only)**

At clean act breaks, use the fade MIDPOINT of video-black versus audio-silence. Fit only after accounting for the video PAD clock contribution and excluding flickering/marginal silences. Report the precise slope convention, sign and uncertainty. Gate 4 cannot validate its own fitted residual.

**RATE STOP** — do not commit a rate if the duration/PAD model, DVD-picture fit and pad-corrected fade fit disagree beyond combined uncertainty, if PAD counts/origin do not reconcile, or if a fit is driven by weak/edge windows. Keep the DVD-backed value as the official choice until resolved.

### ACTION 5.3 — resolve origin/skip, build audio-only outputs

1. Verify WAV0 against the exact matching timeline endpoints; preserve the raw sample count and structural-trim evidence.
2. Compute AudioSkip from the measured master frame of output zero and WAV0; cross-check `delta_s - audio.pad_frames/FPS`. Never use head trim as the audio origin.
3. Record the selected effective rate, method, uncertainty, PAD counts (full map / trimmed / delivered), WAV0, skip sample and report paths. Do not merge incompatible slope signs: `measure_audio_rate` fits master-equivalent time versus raw-WAV time and labels its candidate `44100/slope`; the fade-offset fit has a different convention.
4. After the rate is selected, create a new corrected full-side candidate with `audio_timing_diagnostic.py --rate-hz <selected-effective-Hz> --candidate-out <new-corrected.flac> --report-out <new-report.json>`. Verify its source hash/rate recipe, then record it as `files.ld_corrected`. Run `python scripts\build_ld_synced.py --episode <ep> --dry-run` and then for real; it trims by sample index, pads/trims to the exact video-frame sample count, and writes `ld_synced` plus a recipe/hash report without overwriting audio.

**VERIFY** — corrected duration/origin agrees with the measured target within the declared tolerance; synced FLAC is 44.1 kHz/16-bit stereo, starts at the measured sample, has the exact target sample count, and passes the audio-duration gate.

**DVD-free limit:** for the E07 A/B test, generate the disposable synced candidate with `audio_timing_diagnostic.py --synced-candidate-out <new.flac>` (add `--wav0-master-frame <measured>` only when independent EFM/video evidence supports it). Compare it to the preserved DVD-backed track with the same picture reference: `av_check_output.py --episode <ep> --video <same-reference.mkv> --audio <candidate.flac> --out <unique.tsv>`. Do not promote the candidate or alter `audio.skip_s`. Duration/PAD can estimate rate without a DVD, but absolute fixed sync still needs a trustworthy WAV0/video timebase or an independent content/manual sync check; without that, mark offset provisional and STOP before claiming full sync.

**Luna fallback — trigger 5 (section 0.5):** only if the duration/PAD, picture-map, and pad-corrected fade estimates remain statistically inconclusive or conflict.

### TRAP — rejected estimates

Discarded estimates (292/1875/2500 ppm) came from a **reflected FFT lag convention** or weak peaks.

- Correct FFT convention for these scripts: `irfft(conj(rfft(a))*rfft(b))`.
- Compute Pearson on **only** the actual overlapping aligned samples.
- Correlation >1 is a normalization bug.
- Silence, repeated music, fade stings, and different mixes can produce false peaks.
- Use several long windows and a line fit.

**The rule:** never resample on NTSC theory alone. Reconcile the duration/PAD candidate and raw-provisional DVD-picture fit; use the pad-corrected fade fit only as corroboration. Apply no candidate automatically.

---

## 6. Primary video encode

### ACTION 6.1 — encode arguments

`scripts\encode_episode.ps1` is the bundled one-part encoder. `validate` only probes source-AVS frame metadata and decodes three frames to a null muxer; it performs **no video encode**. `final` is run once, only after the DVD map and PAD-repair AVS are ready:

```text
-c:v libx265 -profile:v main10 -pix_fmt yuv420p10le
-crf 14 -preset slow -tune grain
-color_primaries smpte170m -color_trc smpte170m -colorspace smpte170m -color_range tv
```

Example:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\encode_episode.ps1 -Episode <ep> -Pass validate
# Record base-AVS video.frames/duration_s; build the picture map and avs_padfix (§6.4/§7).
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\encode_episode.ps1 -Episode <ep> -Pass validate
# Build/validate sample-accurate ld_synced (ACTION 5.3), then make the one final video encode.
python scripts\build_ld_synced.py --episode <ep> --dry-run
python scripts\build_ld_synced.py --episode <ep>
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\encode_episode.ps1 -Episode <ep> -Pass final
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\remux_padfixed.ps1 -Episode <ep>
```

**VERIFY** — inspect the matching `ffmpeg-command-*.json` sidecar for the exact arguments
and workflow version (the quiet x265 log may suppress its settings banner). The final sidecar
must show exactly one **video-only** x265 encode from `avs_padfix` at CRF 14 / slow / grain.
The `audio-sync-*.json` separately records AudioSkip/start-sample/target-sample evidence.
`remux_padfixed.ps1` attaches `ld_synced` by stream copy and verifies packet identity.

**TRAP — never:** force 15 Mbps CBR, use `veryslow`, or use NVENC as the final. NVENC p7 15 Mbps is optional preview/reference only. Never denoise or discard genuine grain.

**Reference:** quality target is grain-preserving HEVC Main 10, 23.976p, no bitrate ceiling. Emissary averaged ~10.35–10.82 Mbps, ~3.2–3.5 GB per part, ~9–10 fps. Results vary.

### ACTION 6.2 — geometry

- Native captured raster: **768x484**.
- Maintain measured 4:3 display aspect, consistent SAR across parts.
- Exact 4:3 SAR for 768x484 is `121:144`; the current Emissary mux reports `79:94` (close, not identical).

**VERIFY** — inspect the **actual stream** and confirm displayed 4:3. Do not blindly copy stale SAR values.

**VERIFY** — limited range and SMPTE 170M tags preserved.

### ACTION 6.3 — validate, then final

1. Run non-encoding validation on `avs_encode`; record its measured frame count/duration.
2. Complete the source-AVS PAD audit and build `avs_padfix` (ACTION 6.4); run validation
   again to test that version-stamped wrapper. Both validates use only a three-frame null
   decode and perform no x265 encode.
3. After building `ld_synced`, run the final video-only x265 encode once, stream-copy remux,
   then probe the combined output.

**VERIFY (probe)** — all of:

- [ ] HEVC Main 10, 10-bit
- [ ] 24000/1001
- [ ] correct raster / SAR / DAR
- [ ] color tags
- [ ] After remux: FLAC LD stereo 44.1 kHz / 16-bit

**VERIFY** — fully decode/inspect before declaring complete.

### 6.4 Single-frame `PAD_1` repairs (post-IVTC; all episodes)

Build the §7 DVD↔LD picture map before classifying any cut PAD. §7 can be run before the
final encode because it uses the stack master, not the encoded file.

#### Diagnosis and classification

**ACTION D1** — scan the source-derived `avs_encode` output (post-IVTC, before HEVC) with:

```
blackdetect=d=0:pix_th=0.10:pic_th=0.98
```

Retain hits shorter than 0.2 s.

**ACTION D2** — parse every `PAD_1` position from the current episode's `frame_map`; convert it to the post-`TDecimate` output frame and match within the audit's **±2-frame** tolerance. Only map-matched frames in `pad-repair-frames.txt` may be replaced; isolated black content without a map match is not a repair candidate.

**TRAP** — do **not** infer pad positions from a ~30 s cadence alone.

**ACTION D3** — confirm each candidate on the **full raster** (max/mean RGB or luma), not only a 48x36 descriptor.

- A true pad is near-zero across almost the whole frame.
- Check the source-derived post-IVTC frame before encoding; the delivered file receives a separate final black/PAD QC scan.
- **Exclude dark real content.** Local E01's 1954.035 s hit (max 45/255) and E04's 33935/65240 hits (max 57/64) are **not** pads.
- Check `.tbc.json` decode faults/sequence gaps and compare the aligned DVD at genuine-content blacks.

**ACTION D4 — cut test.** For candidate at output frame `i`, compare `i-1` to `i+1`, and calibrate the metric against unambiguous cuts and normal-motion frames **for this episode**.

**TRAP** — use the **two real flanks**, not either step to/from black. Black-to-frame differences are large by definition and make a pad look like a cut.

**TRAP** — thresholds are **episode-specific**. In the E03 regression, the metric separated 382 cuts (min 29.9, p10 32.3) from pads (max 18.1) — those numbers are **not portable** to another metric or episode.

**Luna fallback - trigger 6 (section 0.5):** only if full-raster/flank/PAD-map/DVD evidence still cannot classify a candidate.
**STOP** — if you cannot cleanly separate pads from real dark content for this episode. Do not classify every dark one-frame event as a pad.

#### Fill policy

| Classification | Action |
|---|---|
| **Non-cut PAD** | RIFE midpoint between the two adjacent real **output-domain** frames |
| **PAD on a cut** | Never RIFE across two different shots — see below |

**Non-cut PAD — tested best-quality command** (768x484 PNG pair; verify the model and backend on the current machine):

```
rife-ncnn-vulkan -0 prev.png -1 next.png -o pad.png -s 0.5 -g -1 -x -z -m <model-dir>
```

- `-x` spatial TTA, `-z` temporal TTA; CPU/fp32; UHD and de-duplication **off**.

**TRAP** — constrain RIFE to **proven PADs only**. In the E03 regression, RIFE reduced high-frequency energy ~22% on one frame.

**TRAP** — a tested CPU+TTA run tied numerically with GPU fp32+TTA; **GPU without TTA differed substantially**; NCNN Vulkan fp16 is the precision risk. Verify the local backend rather than assuming these results transfer.

**TRAP** — the tested CPU PNG path is RGB24; converting to YUV420 10-bit preserves the output format but **cannot recreate source 10-bit precision**.

**PAD on a cut:**

1. Map the target time through the episode's independently verified DVD↔LD picture map (§7).
2. IVTC/resample DVD to 23.976.
3. Accept a DVD frame **only** if its scene and timing match uniquely.
4. Crop/scale to the LD raster; color-match gently to the last good LD frame.
5. **If no precise DVD frame exists**, hold the immediately previous native 10-bit LD frame (`src.Trim(i-1,i-1)`) for one output frame. This preserves the cut and is **preferable to a cross-shot morph**.

#### Injection, encode, and validation

**ACTION I1** — repair **after `TDecimate`**, never in the stack or before cadence selection. **Do not re-render the weighted master.**

1. Run `build_padfix_avs.py`; it imports the source-derived `avs_encode` and constructs a replacement clip with exactly the source's `N` output frames.
2. Apply `ReplaceFramesSimple` only at confirmed repair-list indices.
3. Use `ImageSource(..., start=0, end=0, fps=24000/1001)` explicitly.
4. Convert to the source's 10-bit chroma format.
5. `AssumeFPS(img, src)`.

**TRAP** — a default one-image `ImageSource` can unexpectedly return 1001 frames; a prior bad layout produced 39 black and 43 wrong-content pads.

**TRAP** — `Overlay(start=...,end=...)` is not supported by every AviSynth+ build; test the exact local version or use the documented replacement mechanism.

**TRAP** — never use an encoded MKV as the repair source. The final encoder must read `avs_padfix`, not an HEVC decode.

**ACTION I2 — assert inside AVS before encoding:**

```
src.FrameCount()==N  AND  replacement.FrameCount()==N
```

Load the machine's `RemapFrames` plugin.

**VERIFY (identity test)** — test several pads spread across the episode:

- [ ] Output NCC against its **own** replacement is approximately 1.
- [ ] NCC is **lower** against other replacement images.
- [ ] Both neighboring frames verified.

**Reference:** local check on 11 pads gave own-image NCC 0.99940–0.99996, best unrelated-image NCC <=0.738 (`pad-identity-samples.tsv`).

**TRAP** — do **not** rely on "not black" alone. A prior one-pad preview passed while 43 other pads silently used the wrong image.

**ACTION I3** — complete PAD repair before encoding. Run `encode_episode.ps1 -Pass final` once with the normal x265 CRF 14 / slow / tune grain / Main 10 settings. Then stream-copy the existing audio/subtitle tracks with `remux_padfixed.ps1`.

- The video-only output is `padfixed_video`; it is created directly from the source-derived repaired AVS.
- Record the new video MD5.

**STOP** — `encode_padfixed.ps1` is retired; it refuses to perform a second full-program encode.

**VERIFY (I4)** — full-decode / black-scan the delivered MKV:

- [ ] Zero PAD-aligned single-frame black hits
- [ ] Unchanged frame count / duration
- [ ] Expected streams / defaults
- [ ] Bit-exact copied audio/subtitle packet hashes
- [ ] Per-pad identity checks above pass

**VERIFY** — genuine dark content, credits, and cuts are **untouched**.

#### Cross-machine / tooling notes (TRAP)

- The E03 regression found 84 map pads, 83 zeroed master fills, and 82 in the delivered file after head trim; all six TBC JSONs were clean. This proved a `PAD_1` **fill defect**, not an encoder/TBC failure.
- Flowframes 1.42's command wrapper can silently no-op without elevation/dev mode; its model downloader may be GUI-only. Prefer a verified headless engine for automation. **Check model/backend availability on the current machine; do not assume another host's GPU enumeration, weights, or binary path transfers.**
- Large FFmpeg `select='eq(n,...) + ...'` lists can exceed expression limits (chunk <=32 terms, or use seek/extract pairs).
- `-read_intervals` does not drive `-show_frames`; use `showinfo` for PTS.
- **Never** make A/B copies as hardlinks then overwrite with `Copy-Item -Force` (it writes through the shared inode).

---

## 7. DVD video alignment, audio/sub retiming, and final mux

The DVD is used for **timing and optional streams only** — not as the picture master.

### ACTION 7.1 — probe the DVD

Probe every DVD file. Select streams **by type/title, not copied indexes**.

**Reference:** one Emissary regression DVD was `roots.dvd_root/DS9S1D1/B1_t00.mkv` (MPEG-2, AC-3 5.1, separate AC-3 stereo, VobSub, SRT). Resolve the actual path from the local profile/manifest.

### ACTION 7.2 — measure the DVD↔LD video map

**Step 1 — align descriptors.** Align native 29.97 LD ProRes frame descriptors to decoded DVD video descriptors (48x36 normalized luma is the established low-cost signature).

Generate the arrays using the bundle helper:

```powershell
python scripts\extract_video_descriptors.py --episode <ep> --source master
python scripts\extract_video_descriptors.py --episode <ep> --source dvd
python scripts\measure_dvd_alignment.py --episode <ep>
```

The DVD decode is sequential. Keep FFmpeg's validated default rawvideo output-sync behavior: a controlled E05 probe produced the exact 81,654-frame reference with default sync; adding `-fps_mode passthrough` alone produced 68,555, while `-vsync 0` failed. Do **not** add output-sync overrides, `setpts`, or an `fps` filter. Check both descriptor counts and 29.97-fps coverage before using the candidate path; it is not an adjudicated edge map.

- Use a **continuity-constrained / Viterbi** search.
- **TRAP** — single-frame greedy matches drift/hop on static shots.
- Preserve actual DVD PTS where converting frame offsets to seconds.
- Cache the descriptors (`desc-*.npy`); re-runs then take seconds instead of minutes.

> **TRAP — the sign convention that bit us on E04**
> The state is `DVD index minus master index` and it is **NEGATIVE** (the master runs ~1400 frames ahead from LD front matter).
> Positive offsets index past the DVD's end at the tail → first a crash, and if clipped, **silently garbage emissions**.

**Cross-check every act-break step against black durations:**

```
step = (DVD black - LD black) * 29.97   frames
```

E04's DP steps matched this to within threshold noise.

**TRAP** — `blackdetect` durations are **threshold-biased** (LD analog fades vs DVD digital fades). Read final step values from the DP path at content-rich frames just **OUTSIDE** each black, not from raw black durations.

> **TRAP — even the DP medians can be fade-biased**
> On E04 the dp window 0.15 s from break 8's black read **-1466**, while two independent 40–50 s content windows and the DVD black-start gave **-1451 / -1453**.
> The **authoritative** edge measurement is a long content window (**>=2 s clear of fades**) matched over **+/-40 offsets**. Use dp and the plateau fit as corroboration only.

**Step 2 — plot.** Plot DVD-vs-LD offset over time.

| Shape | Meaning |
|---|---|
| Constant plateaus with jumps inside black | extra LD black |
| A slope | rate mismatch — fit a line **separately on each plateau** |

Use `blackdetect` to find candidate boundaries and image matching to verify them.

After recording candidate `retime.breaks[].ld_black` intervals, run `python scripts\measure_break_edges.py --episode <ep>`. Review content offsets, confidence margins, and DP comparisons; the helper reports evidence only and does not choose offsets or edit the manifest.

**Step 3 — use the map** for both DVD audio and subtitles.

**TRAP** — a global `-itsoffset` is **wrong** if there are act-break steps or rate drift.

- Source DVD-to-LD mapping: if DVD time advances at ratio `r` per LD second, use `atempo=r` on that DVD audio segment (output duration becomes input/r).
- Insert silence for LD-only black extensions.
- Transform SRT cue times through the inverse affine map plus cumulative gaps.

**Step 4 — stream roles.**

- DVD 5.1 is a **discrete remix**.
- DVD stereo is a **separate mix**, not a downmix or substitute for the LD's original Dolby Surround stereo.
- **LD 2.0 must stay default.**

**Luna fallback - trigger 7 (section 0.5):** only if edge methods still differ by >3 frames after the prescribed remeasurement; batch unresolved breaks.
**STOP** — if the offset measurements disagree beyond tolerance, or the sign convention cannot be confirmed on this episode.

### ACTION 7.3 — verify the map against picture **before** retiming tracks

**TRAP (E03 regression lesson)** — an audio-envelope map can locate content but can absorb the LD's own A/V offset into the DVD-to-LD map.

Before muxing, independently compare final 23.976 LD video with the IVTC'd DVD in the same active-area geometry.

**VERIFY** — all of:

- [ ] Unique high-motion / high-NCC matches
- [ ] Reverse round-trip agreement
- [ ] Independent scene-cut check
- [ ] Report median/IQR, per-act medians, and drift

**If picture shows a constant offset `d`:** remux only — adjust LD audio, retimed 5.1, and SRT **together**; do **not** re-encode video. Record build/effective skips in the segment manifest.

**TRAP** — the E03 regression found +72 ms and corrected it with a stream-copy video remux and +72 ms shift to both audio tracks and all SRT times. **Measure `d` independently for every episode; never copy that value.**

### ACTION 7.4 — retime construction (E04-proven)

**Cut construction — edge-anchored, zero DVD audio content dropped:**

- Cut exactly at DVD audio silence boundaries: segment before a break ends at `silence_start`, next begins at `silence_end`. The dropped span is silence by construction; both cuts must satisfy the recorded detector threshold (no clicks).
- Place each cut in output time:

```
dst = X - off_edge/FPS - DELTA
```

  where `off_edge` = DVD-master offset at that break edge, and `DELTA` = master time of output frame 0.
  (E04: `DELTA = 1485/FPS = 49.5495 s`; AudioSkip `(1485-4)/FPS` gives the identical content timeline for the LD stereo.)

- Inserted gap:

```
gap = silence_duration - step/FPS
```

  Print it **both ways** as an identity check.

  **TRAP** — if a step is longer than the silence, the gap goes **negative**. **Do not force it.** Re-adjudicate the edge offsets first. (On E04 the negative gap vanished once the fade-biased dp edge was replaced.)

**Luna fallback - trigger 8 (section 0.5):** only if the gap remains negative after verifying signs and silence boundaries.
**STOP** - if the gap remains negative after the source boundaries/signs have been checked and the applicable fallback has been used once.

- Segment `atempo` = `src_span / dst_span` of the two anchored ends.

  **TRAP** — do **NOT** reuse one plateau's rate for all segments.

- A break with **no** DVD audio silence (E04 break 2: music plays through the black, step ~0) = **NO cut**: merge the regions on both sides into a single segment; the DVD black passes through the plateau affine untouched.
- Head segment starts at `DELTA` mapped back to DVD time (lands inside the DVD head silence).
- Tail segment ends at `(TOTAL + DELTA) + TAIL_OFF/FPS` using a content-verified constant tail offset.
- `apad=whole_dur=TOTAL` with `TOTAL = frames*1001/24000` **exactly**.

  **TRAP** — E04: 65,347 → `2725.514458 s`. Use this **exact** value for `-t` and `apad`, **never** a rounded 2725.55.

#### Adjudicate every break-edge offset three ways before trusting it

| # | Method | Reference result (E04) |
|---|---|---|
| 1 | **Long-window content match**: 40–50 s of master content just outside each black (>=2 s clear of fades) vs DVD descriptors over +/-40 offsets, mean cosine per offset; drift-correct the window average to the edge using the plateau slope | corr 0.88–0.96, n~1400; windows span ~50 s so ~1.5 frames of drift |
| 2 | **Plateau LS fit** of the npy path | rms <= 1.4 f on A0/A2–A5 |
| 3 | **DP/npy median** near the edge + DVD `blackdetect` black **STARTS** as a fourth opinion | agree within 0–3 frames = LD fade lag; black **DURATIONS** stay threshold-biased |

**When fit, dp, and content disagree, CONTENT WINDOWS WIN.**

**Two E04 disagreements that mattered:**

- (a) dp at break 8 read -1466 vs true -1451 (fade bias) — trusting dp forced a cut into audio content and a negative gap.
- (b) the npy path trended +0.224 f/s through the end credits (+7484 ppm) while four content sub-windows read a constant -1424 — the trend was a **Viterbi credits artifact** and would have pushed the tail cut past the end of the audio.

**Luna fallback - trigger 9 (section 0.5):** only if independent tail windows still disagree after remeasurement.

#### Probe DVD stream reality before converting indices to seconds

**TRAP** — E04's DVD MKV video is ~24 fps (65,675 frames) while the cached descriptor array is 29.97-regridded (81,747 rows = duration x 29.97). Descriptor index/FPS = content time, and **that** is what the npy offsets use.

**ACTION** — count frames (`ffprobe -count_frames` or a null decode). **Do not** trust `r_frame_rate` or a quick PTS sample.

### ACTION 7.5 - generate the retimed DVD 5.1 and English SRT

Before running the helper, the manifest must contain the measured DVD/LD picture map,
adjudicated `retime.breaks`, `tail_off`, `head_sil_end`, `tail_sil_start`, and the
verified `files.srt_source`. Keep the original DVD AC-3 as the source/reference.

1. Run `python scripts\retime_dvd_assets.py --episode <ep> --dry-run`.
2. **VERIFY** the selected DVD 6-channel stream, each source/output segment, each gap,
   each tempo (0.99..1.01), and input/output SRT cue counts.
3. If and only if the dry run is sensible, rerun without `--dry-run`. The helper writes
   the 24-bit FLAC, retimed SRT, segment map, and filtergraph; it refuses to overwrite.
4. Do not drop cues, move cuts outside measured silence, or use a global offset.
   VobSub is a backup unless its packet timestamps receive the same piecewise retime.

**TRAP** — preserve the detector parameters used to measure source DVD-silence boundaries.
When validating the retimed FLAC, use the same `silencedetect` threshold/duration; E06's
-45 dB measurements checked at -40 dB appeared hundreds of milliseconds early. If changing
thresholds, remeasure the source boundaries instead of shifting the retime schedule.

**TRAP** - FFmpeg 9 does not accept legacy `-filter_complex_script`; the bundled helper
passes its reviewed graph through `-filter_complex` inline. Do not hand-edit/rebuild a
per-episode `retime-dvd-assets-s01e05.py` or `*-e04.py` script.

### ACTION 7.6 — retime validation gates (all must pass)

- [ ] With the same recorded detector parameters used for the source boundaries,
      `silencedetect` on the retimed FLAC reproduces planned gap positions within threshold
      timing (~70 ms) and durations **exactly**.
- [ ] SRT keeps every cue, monotonic, inside `[0, TOTAL]`.
- [ ] Envelope cross-correlation of retimed 5.1 vs LD stereo at 5–7 points (`scripts/sync_check.py --episode <code>`, 5 ms hops, parabolic peak) gives a **near-constant** offset.

**Reference (E04):** 5.1 leads LD by 76 +/- 13 ms with slope +12.8 +/- 7.3 ppm; subtracting the LD stereo's own validated +0.05..+0.10 s fade lag vs video puts the 5.1 within 0..-35 ms of video across the episode (inside ITU +/-45 ms) — accept rather than re-chase envelope noise.

**TRAP** — a constant offset **between** the two audio tracks is expected (each has its own fade relationship to video). **Only drift vs video is a defect.**

**STOP** — if gaps do not reproduce, SRT is out of range, or the offset drifts.

### ACTION 7.7 — mux

Input 0 = LD master, input 1 = retimed 5.1, input 2 = retimed SRT:

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

**VERIFY** — LD 2.0 is default; DVD 5.1 and SRT are selectable and non-default.

**TRAP** — if Topaz drops streams, remux tracks from this pre-upscale MKV afterward **without** applying the map again.

---

## 8. One-part vs two-part output

**Emissary (S01E01) only — two-part:**

1. Encode Part I and Part II **separately**.
2. Concatenate **those two episode parts** by stream-copy video.
3. Join LD audio at sample/PCM level into **one FLAC** before mux; verify frame counts and the join.
4. Then map DVD audio/subtitles across the combined timeline.

**Every other episode — one-part:**

1. Process one complete part.
2. Produce one MKV.

**TRAP — never:** concatenate capture files, concatenate separate episodes, or perform an automatic Topaz handoff.

---

## 9. Objective DVD quality comparison

**RULE** — only compare matched frames **after** cadence/time alignment and **identical** active-area geometry. PSNR/SSIM on mismatched frames is meaningless.

Compare **separately**, not as one score:

- [ ] luma spectrum by spatial-frequency band
- [ ] edge / sharpness
- [ ] grain / noise
- [ ] MPEG blocking
- [ ] combing
- [ ] levels

**Reference:** E01 showed similar low-frequency power but more LD high-frequency detail/grain. **That is not a reason to denoise.**

**TRAP** — there is **no** evidence for a seamless episode-wide DVD/LD blend.

The useful hybrid is **selective**: DVD can supply a **verified exact frame** at an LD defect/cut PAD, after 23.976 cadence normalization, active-area crop/scale, and a restrained match to the last good LD frame (see §6).

**TRAP** — do not paste DVD frames globally, and do not trust nearest-time matches without picture confirmation.

---

## 10. Final QC checklist

### Video

- [ ] Main 10 HEVC, `yuv420p10le`
- [ ] 24000/1001 progressive
- [ ] native raster
- [ ] 4:3 DAR
- [ ] SMPTE170M / limited range
- [ ] expected frame count and duration

**Inspect visually:** head, several evenly spaced points, every act break, seam (if two-part), and tail.

**Black scan:**

```
blackdetect=d=0:pix_th=0.10:pic_th=0.98     keep black_duration < 0.2
```

Every hit must be **either**:

- a genuine content fade that exists on the DVD, **or**
- a confirmed PAD blip already repaired per §6.

**Luna fallback - trigger 10 (section 0.5):** only if the PAD-map/full-raster/DVD checks leave a hit unclassified.
**STOP** — if any confirmed `PAD_1` black blink remains. An unexplained one-frame near-black frame is a defect.

### Audio / subtitles

- [ ] LD 2ch 44.1/16 FLAC is **stream 0 and default**
- [ ] DVD 6ch 5.1 is selectable and **non-default**
- [ ] English SDH SRT is selectable and **non-default**
- [ ] subtitle timestamps sorted and within duration
- [ ] audio duration compared with video (pad a few ms at the end if needed — **never** by altering the episode timing map)

**Sync checks** at head, dialogue points, after each timing step, and end.

**Acceptance criterion:** fade-in offsets **CONSTANT** across all act breaks and ~0, within +/-0.1 s on clean breaks.

**TRAP** — a **changing** offset = residual rate error → go back to the audio fit (§5, Gate 4).

**TRAP** — one noisy break (silence flickering on fading music) is **not** a failure.

**Objective cross-checks:**

- [ ] `silencedetect` on the retimed 5.1 reproduces planned act-break gap positions/durations
- [ ] envelope cross-correlation of the two audio tracks (`scripts/sync_check.py --episode <code>`) is drift-free vs video (constant inter-track offset is expected)

### Before reporting, include

1. final path / size
2. stream list and dispositions
3. encode command / settings
4. measured trims / rate map
5. frame count / duration
6. audio-sync results at multiple points
7. subtitle count / range
8. any unresolved issue

**TRAP** — do not clean intermediates unless asked. If asked, keep the audio-fixed master, the enriched pre-upscale master, and designated references.

---

## 11. Reference data — **DO NOT COPY**

> Everything in this section is **historical record for past episodes**. It exists so you can recognise whether a result is plausible.
> **Never** paste any value here into a new episode. Every number in §1–§10 marked **MEASURE** must be obtained fresh.

### Emissary (S01E01) regression

- Output `Trim(1167,70718)`; 69,552 frames / 2900.90 s; head trim 48.631917 s (Part I).
- Part II: `.Trim(20,0)` alignment, 61,214 output frames / 2553.134 s after its measured tail cut.
- DVD frame-offset plateaus: -1418/-1438/-1482/-1502/-1530; steps 20/44/20/28 frames.
- Part II DVD/LD time ratio ~0.999; offset steps 44/34/43/37 frames; short final tail measured 1:1; starts near DVD 2898.930 s.
- Part II local LD black ends used: 676.801, 1271.520, 1642.141, 2501.290 s. Schedule: `Z:\work\probe\dvd-retime-map.tsv`.
- `Z:\work\probe\retime-dvd-assets.py` is **hard-coded for E01** — do not run unchanged for another episode.
- E01 Part II: 77 L1 map pads; 76 rendered near-black blips, all repaired by RIFE. E01 Part I has no periodic PAD blips.
- Final: `Z:\work\out\S01E01-Emissary-23.976-padfixed.mkv` — 7,237,820,859 bytes; 130,766 frames / 5454.034 s; Main 10, 768x484, SAR 79:94; LD audio packet MD5 `1453a146c418b0b562f8c8eb3008576f`; video packet MD5 `b10fec0a94db5c520528416772252110`; **0/76** PAD-aligned hits remain.

### A Man Alone (S01E04) regression

- DVD: `roots.dvd_root/Deep Space 9 - S01E04 - A Man Alone.mkv` (historical E04 regression)
- Download: `Z:\DS9 S01E04 LaserDisc\` (16 verified files, 453.69 GiB)
- Stack master: `Z:\work\prores\S01E04-stack6-master.mov` (83,170 frames, L2-referenced, alignment `1+0,2+3,3+1,4+176,5+32,6+24`)
- Cadence: 93.17 % period-5, 0 unmatched, 5 inserts, repair set 3.07 %, VERDICT uniform 3:2
- Rate-corrected audio: `Z:\work\audio\S01E04-L1-44056.wav` (fit -992.8 +/- 53 ppm)
- DVD alignment: `Z:\work\probe\dvd-align-s01e04.npy` (state DVD-minus-master is negative, base about -1426; DP content-edge steps +2/0/+27/+43/+43/+27/-85/+37; use `dvd-retime-map-s01e04.tsv`)
- Head trim: output `.Trim(1186, 0)` (fade-up = output 1189), AudioSkip 49.416033 s, 65,347 frames / 2725.514458 s
- DVD retime: schedule `Z:\work\probe\dvd-retime-map-s01e04.tsv`, graph `Z:\work\audio\dvd-51-retimed-s01e04.filter`, retimed 5.1 `Z:\work\audio\dvd-51-retimed-s01e04.flac`, retimed SRT `Z:\work\subs\dvd-eng-retimed-s01e04.srt` (867/867 cues), edge data `Z:\work\probe\edges-longwin-s01e04.npy`
- Pre-upscale: `Z:\work\out\S01E04-23.976-pre-upscale.mkv` — 4,663,902,131 bytes; container 2725.554 s; envelope sync 5.1 vs LD 76 +/- 13 ms
- PAD-fixed pre-upscale: `Z:\work\out\S01E04-23.976-pre-upscale-padfixed.mkv` — 4,694,688,433 bytes; 65,347 frames / 2725.514458 s; 80 non-cut PADs by RIFE 4.26 CPU TTA; cut PAD frame 26160 by matched DVD progressive frame 26234 at 1094.191 s; **0/81** PAD-aligned hits remain. Audio MD5s: LD `4aab3478a270854433adf303efc712c4`, DVD 5.1 `ac7f8c4db605397ebf032bf65d58ae8c`, SRT `03192babaddd52b4db340cf9571ef77d`; video `22606cdbe6d42b755110e197072998a8`.
- E04 dark-content hits at output 1415.372 s (max 57/255) and 2721.052 s (max 64/255) are **non-PAD content**, left untouched.

### Standing rule

**E01's completed audio tracks were user-confirmed correctly timed. For every new episode: remeasure the absolute A/V offset and rate. Never infer another episode's correction from E01/E03/E04.**

---


## Quick reference - fallback vs STOP

For an ambiguity-only case, consult the single trigger list in section 0.5. Qwen3.5 4B
must finish local checks first; use one `/deep` request to `opencode-go/gpt-6-luna`
only for a listed unresolved case, and apply the response only if VERIFY passes.
Never escalate a command, path lookup, checksum, arithmetic operation, or explicit
numeric gate.

**STOP and ask the user** for factual failures that reasoning cannot fix:

1. Source file/hash/size mismatch or missing required raw EFM.
2. Command/tool failure after the documented recovery.
3. Frame-count mismatch, invalid stack inputs, or unrecoverable Orc samples.
4. Preflight, free-space, or output-preservation gate failure.
5. E01's two-part workflow is required; generic one-part stages reject it.
6. GPT-6 is unavailable, returns an unusable answer, or fails VERIFY.
7. A QC hard gate fails; no model may waive it.

**Stop report:** section/step; commands and inputs; actual vs expected result; artifact
and log paths; known facts and unresolved question. Do not claim delivery.
