# PORTING — moving ld-pipeline between machines

The workflow is profile-driven. Resolve executables and storage roots from the local
`machine.json`; do not assume a host name, drive letter, user directory, or mount point,
and do not edit scripts to insert machine-specific paths.
The authoritative version is stored in `WORKFLOW_VERSION`; core-update and device-log
rules are in `WORKFLOW_VERSIONING.md`.

---

## 1. Profile paths

| Purpose | Profile fields | Porting rule |
|---|---|---|
| Work/render volume | `roots.work` and the probe/audio/subs/out/prores/stack/logs roots | Use a local, fast volume with enough free space; avoid SMB for large renders. |
| Archive download | `roots.downloads_root` + `roots.downloads_template` | Resolve one canonical destination, preview it, and resume only in that folder. |
| DVD reference | `roots.dvd_root` and `files.dvd` | Use the exact matching local DVD; do not substitute another episode. |
| Legacy E01 assets | `roots.upscale_root` | Configure only if those files are actually staged. |
| Bundle/runbooks | `roots.runbook_dir` and bundle-relative paths | Copy the whole bundle; keep reusable helpers in `scripts/`. |

`roots.downloads_template` allows per-episode folders to resolve differently on each
host without a script change. The profile, not a machine-specific path table, is the
source of truth.

**Platform boundary:** Windows uses the PowerShell/AviSynth+ workflow and
`preflight.ps1`. macOS uses POSIX profile paths and VapourSynth; it cannot use the
Windows AviSynth demuxer or Windows preflight. A bundled `preflight_mac.py` is now
available, but this copy has not been run on macOS and the end-to-end episode workflow
is still **not validated**. Do not treat component smoke tests as a full workflow pass.

---

## macOS porting — M5 Mac field notes (2026-11-14)

These are verified observations from the M5 Apple-silicon setup, not a claim that the
whole pipeline is portable. Keep the Mac work in a **local copy** of `ld-pipeline/`;
the SMB/share bundle is read-only. Preserve any imported Windows profile verbatim as a
separate `machine-<name>-imported.json`, then create a Mac `machine.json` with
`os: darwin` and POSIX paths. Point `roots.downloads_root`, `roots.dvd_root`, and
`roots.work` at local storage with enough real free space; do not route large source
downloads or renders through a small SMB volume or lower `limits.min_free_gb` to force
a pass. Keep the DVD source local and decode it sequentially: its packet PTS may be
non-monotonic, making `-ss` seeks unsafe.

The tested profile used user-space paths such as `~/tools/ffmpeg/ffmpeg`,
`~/tools/ffmpeg/ffprobe`, `~/Applications/orc-gui.app/Contents/MacOS/orc-cli`,
`~/tools/pwsh/pwsh`, and `~/tools/vsenv/bin/python`. Put the selected executable paths
in `machine.json`; use one Python interpreter/environment for the required modules and
VapourSynth bindings. `preflight.ps1` is Windows-only. The bundle's `preflight_mac.py`
provides a separate capability check, but this copy must be validated on the target Mac
and does not establish end-to-end readiness.

### Version policy — update first, then verify

**Always check for the latest available versions before starting a Mac setup or episode,**
then ask the user what to update. Use the cross-platform Tool-update gate below; do not
install updates before the user chooses specific components, **Update all**, or **Continue
anyway**. Record installed/latest versions, source, the user's choice, and verification in
`tool-version-check-YYYYMMDD.md` under the local logs root. The versions in this section
are the M5 snapshot from 2026-11-14, not permanent pins; re-check upstream before reuse.
After selected updates, rerun functional/capability checks. TFM is treated as compatible
under the current-software policy below; no TFM flag or parity-clip rerun is required.
Freeze the toolchain once an episode starts; do not silently continue using a stale package
or upgrade mid-episode.

If the workflow has an explicit parity requirement (for example, Decode-Orc/stacker
parity with the Windows master), verify that the latest available version satisfies
it. If not, stop before processing and document the exact compatible version and
reason; do not claim both “latest” and “parity” without testing. On the logged setup,
Decode-Orc 2.11.1 was both the current available version and the runbook parity version.
FFmpeg/FFprobe were matched to the Windows 9.0.2 build, including `libx265`; re-check
the Windows master version and the latest Mac build at each setup.

M5 snapshot of verified components (refresh versions before use):

| Component | Verified on M5 | Setup notes |
|---|---|---|
| Decode-Orc | 2.11.1 | `orc-cli` is inside `orc-gui.app/Contents/MacOS/`; check version parity with the Windows master. |
| FFmpeg / FFprobe | 9.0.2, arm64 | Static build with `libx265`; match/verify the encode capabilities required by the master recipe. |
| PowerShell | 7.7.0 | User-space install; runs bundled `.ps1` helpers unmodified where their dependencies allow. |
| VapourSynth | R82; Python 3.15.2 | Official macOS arm64 wheel in the tested conda-forge environment. |
| `vivtc` | 3.0 (master) | Source-built against R82; `VFM`/`VDecimate` load and run. |
| `vapoursynth-tivtc` | Installed 2026-11-14 | PyPI wheel is `cp312-abi3`; it was visible in the Python 3.15 env but not the earlier Python 3.9 query. Supplies native `TFM(output=...)` logging. |
| `bestsource` | R23 | Source-built with AviSynth disabled and GPU disabled; `bs.VideoSource` opened the DVD. |
| `vapoursynth-mvtools` | Source-built 2026-11-14 | `fftw` from conda-forge was needed for `fftw3f`; QTGMC use remains conditional and unvalidated. |
| RIFE | Engine 20221029 + `rife-v4.6` weights; functional M5 test | Native arm64/Vulkan path and the selected model were tested on that host; resolve and verify engine/model/backend separately on every machine. |
| micromamba | 2.10.0 | User-space environment manager used for the VapourSynth environment. |
| Python modules | latest at install | `numpy`, `pillow`, and headless OpenCV were installed/upgraded; `cv2` is required by the PAD identity gate. |

### Fresh workflow validation on M5 (2026-11-14)

The M5 log §9.3 records fixes found by executing the freshly adopted workflow on macOS:

- The Mac preflight's embedded Python probe had invalid indentation that `py_compile`
  cannot see inside a string; the current Z checker has a direct regression test for it.
- Orc stage discovery must use `orc-cli stages list --core`, not the third-party
  `plugins list` registry. Normalize names such as `source-align`/`source_align`.
- A Y4M frame marker cannot be counted with a line-start regex on binary output; the
  current Z checker walks each frame header and geometry-derived payload.
- PowerShell's `DriveInfo` must query the POSIX destination root `/`; a Windows-letter-only
  `Get-PSDrive` gate reports UNKNOWN and refuses Mac downloads. The current downloader
  tries `System.IO.DriveInfo` first and retains `Get-PSDrive` as the Windows fallback.

The corrected M5-local preflight reported **33 pass, 1 warn, 0 fail**; its sole warning
was optional 7-Zip for an item with no `.efm.7z`. A Mac ListOnly space-gate check projected
260.9 GiB free after E10's selected transfer while retaining the 80 GiB reserve. Those
results validate the fixes on M5; the adapted Z copies still need a Mac run. The latest M5
log still had the 457.3 GiB E10 download in progress at 163 GiB, so no manifest or
downstream stage was yet authorized by the verified-download rule. Its tool inventory
found the listed software current, saved the report, and froze that toolchain for E10.

Homebrew on that particular M5 was stale, unwritable without admin rights, and rejected
macOS 27.2.1. That is an environment-specific diagnosis, not a general Mac requirement:
check whether the installed package manager works and is current. If it cannot be
updated, use a current user-space distribution rather than relying on stale packages.

### Proven port pieces and mandatory gates

- **VapourSynth route:** conda-forge provided an Apple-silicon VapourSynth build;
  `vivtc` built against R82 without source patching. The tested R82 wheel auto-loads
  plugins from its `site-packages/vapoursynth/plugins` directory. The earlier R73
  environment did not, so do not copy its plugin-path workaround into the R82 setup.
- **Source reader:** `bestsource` R23 `bs.VideoSource` opened the supplied DVD and
  exposed frame properties. This is the tested Mac-side source-reader choice in place
  of the Windows LSMASH/AviSynth chain.
- **Native TFM log:** prefer the full `vapoursynth-tivtc` PyPI port over a custom
  `VFM` logger. In the Python 3.15 VapourSynth environment, `core.tivtc.TFM(...,
  output=...)` wrote the expected native log, and the **unmodified** `build_map.ps1`
  parsed all 1,200 test frames and printed the normal pp=0 verdict. In addition,
  the historical cross-machine test found exact decision and mic agreement on two
  1,600-frame clips (full record: `M5 Mac LaserDisc Project Updates Log.md`, §8).
  **Current workflow policy:** when both platforms use their latest supported software,
  treat TFM as compatible; no recurring parity clip run, flag, or evidence check is a
  precondition. This assumption does not measure the current episode's cadence.
- **Python wheel discovery trap:** `vapoursynth-tivtc` is a `cp312-abi3` wheel. A
  package search under Python 3.9 reported no distribution, while Python 3.15 could
  install it. Check package availability in the actual target interpreter before
  concluding a package is unavailable; then record and verify the installed version.
- **TFM field order:** for `order=-1`, set `_FieldBased=2` on the source clip before
  calling TFM. The real stack master's field-based property still needs verification;
  do not assume the ProRes source carries it correctly.
- **Mac encode transport:** FFmpeg's AviSynth demuxer is unavailable on macOS. The
  tested route is `vspipe -c y4m <script.vpy> - | ffmpeg -f yuv4mpegpipe -i - ...`.
  A TFM+TDecimate smoke test completed with both processes exiting 0 and 48→38 frames;
  this is not a production encode. The final recipe must retain `libx265`, Main 10,
  `yuv420p10le`, CRF 14, slow preset, grain tune, `setsar` from the stack master, and
  measured `-frames:v` / `-t` limits. `scripts/build_map_vpy.py` is now included from
  the byte-verified Mac reference snapshot; the M5 log reports a successful synthetic
  encode through this helper. Re-run its checks against the current bundle/profile on
  Mac; this is not a full-episode validation.
- **Standalone plugin runtime:** if plugins load conda libraries by `@rpath`, run
  `vspipe` with `DYLD_LIBRARY_PATH="$HOME/tools/vsenv/lib"`; in-process Python may
  hide a missing library path. Run `vapoursynth config` once, and always test plugin
  loading through `vspipe`, not only through Python.
- **Smoke-test recipes (not a substitute for full-episode validation):**

  ```python
  clip = core.std.SetFrameProps(src, _FieldBased=2)
  fm = core.tivtc.TFM(
      clip, order=-1, PP=0, chroma=1, slow=2, output="/path/tfm.log"
  )
  ```

  ```zsh
  setopt pipefail
  export DYLD_LIBRARY_PATH="$HOME/tools/vsenv/lib"
  ~/tools/vsenv/bin/vspipe -c y4m /path/encode.vpy - \
    | ~/tools/ffmpeg/ffmpeg -f yuv4mpegpipe -i - <Windows-equivalent x265 arguments>
  ```

  Initialize VapourSynth's runtime configuration once before using `vspipe`, then verify
  both pipeline exit statuses and frame count. TFM parity is assumed under the current
  software policy; no separate report flag or parity-clip run is required.
- **PAD replacement:** VapourSynth core `std.FrameEval` was tested as the equivalent
  of the used `ReplaceFramesSimple` operation. Preserve the source frame count and
  prove only the requested PAD indices change; keep the existing replacement-identity
  QC gate. No RemapFrames plugin is needed for this operation.
- **DVD measurements:** do not trust `r_frame_rate` by itself. Measure presentation
  timestamps/frame rate and cross-check frame count, audio-frame duration, and
  container duration. Decode sequentially when packet PTS are non-monotonic.
- **Track defaults:** `init_episode.py` initializes `deliverable_tracks` and
  `mux_tracks` to `['ld']` even when a DVD is supplied. Keep LD stereo only until DVD
  sources and retiming data are verified; add `51`/`srt` explicitly only after that.

### TFM compatibility policy

For workflow execution, treat TFM as compatible when Windows and Mac are kept on the
latest supported software versions. Do not require a TFM parity flag, report evidence,
or parity clips before cadence. A historical cross-machine PASS is documented in
`M5 Mac LaserDisc Project Updates Log.md`, §8, but it is background evidence rather than
a recurring gate. If an actual TFM error or disagreement is observed during processing,
stop and diagnose it; do not infer the episode's cadence from the historical clips.

### Still open — do not call the Mac workflow complete

The Mac reference snapshot also contains an `align_dvd.py` implementation, but this
bundle already has the current descriptor workflow (`extract_video_descriptors.py` +
`measure_dvd_alignment.py`) that produces `align_npy` for the A/V gate. Do not add a
competing alignment implementation; `av_check_output.py` now points to the current pair.
Mac machine profiles and episode-specific manifests from that snapshot remain local and
are not copied into the authoritative bundle.

1. Run the included `scripts/build_map_vpy.py` with the selected Mac profile and verify
   the emitted VapourSynth chain on the current stack master. The M5 synthetic stand-in
   test is historical evidence, not a current-episode pass. Windows `.dll` plugins are
   not macOS plugins.
2. Run the bundled `scripts/preflight_mac.py` on macOS and resolve any required failures.
   It is a capability check, not a substitute for Windows preflight or an end-to-end
   episode run.
3. The QTGMC path is conditional on measured interlaced inserts. `mvtools` is built,
   but the current QTGMC implementation is `vsdeinterlace`'s
   `QTempGaussMC().deinterlace()` API; it is not the old `QTGMC(clip, Preset=...)`
   call, and real `havsfunc` v34 no longer contains QTGMC. Map the Windows settings
   (`Preset="Slower", SourceMatch=3, Lossless=2, TR2=2, Sharpness=0.2,
   FPSDivisor=2`) to the current API and validate parity only if measured inserts make
   this stage necessary. The similarly named PyPI `havsfunc` decoy does not provide
   QTGMC.
4. Run the full manifest-driven workflow and end-to-end QC on the Mac; component and
   smoke tests alone do not validate an episode.

---

## Tool-update gate — Windows and macOS

Run this gate during machine setup and **before starting every new episode**, before
downloading or rendering. Use the paths in that machine's `machine.json`; a version
found elsewhere on `PATH` is not proof that the pipeline uses it.

1. Record the machine/OS, resolved executable path, installed version, latest upstream
   release/tag, check date, and source for each applicable tool below. Check actual
   release pages for manually installed plugins and models; package-manager output alone
   is not a complete inventory.
2. If an applicable component is missing or out of date, show its installed state (or
   **missing**) and latest version, then ask the user to select specific components to
   install/update, **Update all**, or **Continue anyway**. Do not choose for the user or
   start an update before they choose. If everything is current, report that and continue
   without asking.
3. Apply only the user's selection. **Update all** means install/update all listed,
   compatible key components—not every package on the machine. **Continue anyway** means
   make no changes; record skipped components and proceed only if required tools exist and
   preflight/capability checks pass. It does not waive functional gates. If an update
   requires unavailable elevation or fails, report the blocker and ask before proceeding.
   Do not run blanket upgrades such as `winget upgrade --all` or update dependencies
   mid-episode. On Windows, `winget upgrade --source winget` is an inventory check for
   packages known to WinGet; update confirmed packages intentionally. Do not invoke an
   unqualified WinGet search/upgrade that can prompt for Microsoft Store terms in a
   non-interactive run. On macOS, use `brew outdated` only if Homebrew is current and
   usable; check the exact Python/VapourSynth environment with its own interpreter and
   check upstream releases for source-built plugins. If a manager is unusable, stale, or
   cannot see a target package, use the official source or record the blocker—do not
   silently use an old copy or bypass OS permissions.
4. Prefer the latest stable release. If the latest tag is a prerelease, mark that fact
   and do not adopt it without user approval. If it breaks a required encode capability,
   stop and resolve that before processing. Do not proceed on an older compatibility pin
   without explicit user approval or claim an older build is current.
5. Save a copy of `templates/tool-version-check.template.md` as
   `tool-version-check-YYYYMMDD.md` under `roots.logs` (or the machine's equivalent local
   log folder). Include installed/latest versions, resolved paths, release-source URLs,
   the user's choice, skipped components, and preflight/capability results. TFM is
   assumed compatible under the current-software policy; no parity status field is
   required. Freeze the toolchain during an episode; defer new releases until the next
   one unless the user approves pausing and revalidating the current episode.

| Machine | Check these key tools and assets |
|---|---|
| Windows | FFmpeg/FFprobe (`libx265`, AviSynth demuxer); AviSynth+; TIVTC/TFM and TDecimate; L-SMASHSource, FFMS2 and RemapFrames; QTGMC dependencies when needed; Decode-Orc and its plugins; Python interpreter plus `numpy`, `opencv-python`, and Pillow; PowerShell; RIFE engine **and model** as separate versions; 7-Zip if used. |
| macOS | FFmpeg/FFprobe (`libx265`, `yuv4mpegpipe`); Decode-Orc and plugins; PowerShell; micromamba; the profile-selected Python and its `numpy`, OpenCV, Pillow and VapourSynth bindings; VapourSynth; `vapoursynth-tivtc`, `vivtc`, `bestsource`; `mvtools`/`vsdeinterlace` if measured inserts require them; RIFE engine/model or 7-Zip if adopted for Mac stages; build dependencies such as Meson, Ninja, Cython and FFTW. Check the exact environment used by `vspipe`, not a different system Python. |

For workflow purposes, current supported software on Windows and Mac is treated as
sufficient for TFM compatibility. Do not gate cadence on a parity flag, evidence lookup,
or parity-clip run. The historical test result remains in `M5 Mac LaserDisc Project
Updates Log.md`, §8, as background only.

## 2. Platform tool contracts

- **Windows:** FFmpeg/FFprobe must provide `libx265` and the AviSynth demuxer;
  `preflight.ps1` functionally smoke-tests the profile's AviSynth path with
  `LSMASHVideoSource`, TFM, and TDecimate. If functions are not in AviSynth's autoload
  path, set the optional `tools.avisynth_loader` to that machine's plugin-loader `.avsi`;
  `build_map.ps1` imports it into generated AVS scripts. Keep the loader path in the
  profile, not the shared script. Decode-Orc and its stacker/source plugins are required.
  QTGMC is conditional: validate it and dependencies only if measured inserts require
  targeted deinterlacing. The shared FFmpeg wrapper records exact executable arguments
  and workflow version in a unique `ffmpeg-command-*.json` sidecar beside its log.
  Version 4/5 PAD preparation and final video encoding are currently AviSynth-based: the PAD
  audit/replacement chain must read `avs_encode`, and `encode_episode.ps1 -Pass final`
  consumes the version-stamped `avs_padfix` exactly once, video-only. Version 5 builds the
  sample-accurate LD synced FLAC separately; audio corrections are stream-copy remuxes and
  must not trigger x265. The old second-pass encoder is retired.
- **macOS:** FFmpeg/FFprobe must provide `libx265` and `yuv4mpegpipe`; the selected
  VapourSynth Python environment, `vspipe`, `vapoursynth-tivtc`, `bestsource`, and
  plugin-linked libraries must work together. Resolve Decode-Orc from the app bundle
  through `machine.json`. Windows `.dll` plugins and the AviSynth demuxer do not apply.
  The Version 4 PAD-before-final-encode route has not yet been adapted or validated on the
  M5 VapourSynth path; do not claim V4 Mac readiness or run the retired second-pass flow
  there until an equivalent source-derived VapourSynth wrapper is implemented and tested.
  Version 5's `audio_timing_diagnostic.py` and `build_ld_synced.py` are Python/FFmpeg tools
  with no Windows-only audio filters, but have not been run on the M5 environment; verify
  its exact FFmpeg build and Orc-project access before adopting them. V5 also adds the
  DVD-picture diagnostic's robust fit summary and an experimental DVD-free timing candidate;
  no M5 audio result is validated by the Windows tests.
- **Both:** record RIFE engine and model separately if used; check Python and all
  profile-required packages; use 7-Zip only when `.efm.7z` inputs require it. The
  cross-platform Tool-update gate is the authoritative version inventory.

---

## 3. Python dependencies

Use the exact `python.interpreter` selected by `machine.json`; do not install into a
different system Python and assume the scripts will see the package. Common QC
dependencies include `numpy`, `opencv-python` (`cv2`, PAD identity checks), and Pillow
(`PIL`, contact sheets/thumbnails). Mac additionally requires VapourSynth bindings and
plugins in the same environment used by `vspipe`. Inventory missing/outdated packages
on each host and ask the user which updates to apply before installing.

---

## 4. New-machine setup

1. Copy the whole `ld-pipeline/` folder; keep its relative structure intact.
2. **Windows:** if the profile is foreign, run
   `powershell -NoProfile -ExecutionPolicy Bypass -File preflight.ps1 -Write` to
   discover/initialize a local profile; this is not the final readiness pass. **Mac:**
   do not run the Windows preflight; create a local POSIX `machine.json`, set `os: darwin`,
   and keep work in a local bundle copy. Then run `python scripts/preflight_mac.py` as a
   capability check; this new bundle copy has not been validated on macOS.
3. Set roots discovery cannot infer, especially `roots.work` and `roots.dvd_root`; set
   `upscale_root` only when legacy E01 assets are present.
4. Run the Tool-update gate with the local profile paths. Ask the user which missing or
   outdated components to install/update, whether to **Update all**, or **Continue anyway**;
   apply only that choice. Then run Windows preflight until `PREFLIGHT OK` (0 fail), or on
   Mac run `python scripts/preflight_mac.py` and resolve its required failures; do not
   claim full-workflow readiness from that capability check alone.
5. For a new job, use `scripts\init_episode.py` with the Archive URL, download folder,
   and verified DVD path; it creates a draft manifest only after the verified download.

Free-space gates are profile-driven. Check the actual download destination and the
`roots.work` volume against `limits.min_free_gb` (default 80); a failing gate means free
space must be made available or `roots.work` moved before starting, never lower the
threshold just to force a pass. The initial pass does not reserve space for later outputs:
before each write-heavy stage, run `scripts/check_space_budget.py` with a defensible estimate
for the expected output and scratch peak through the next check, then recheck after large
artifacts are written. If no reliable estimate is available or the projected free space is
below reserve, stop and ask rather than treating the earlier preflight as a stage budget.

---

## 5. Traps already hit (read before a first port)

**Measured, not assumed**
- **Exit codes lie.** Orc `tools refresh`, `blackdetect` and the Archive downloader have
  all returned non-zero while the job actually succeeded (`completed: true, saved: true`,
  a full `frame=` count, a 12/12 verified manifest). Always validate the *artifact*.
  Conversely, a zero exit with a truncated artifact is a failure.
- **Never `-ss` a reference DVD.** Its video PTS are non-monotonic (MakeMKV/interleave),
  so seeking lands wrong and `-t` silently covers fewer frames. Decode sequentially and
  index with `showinfo`; the bundled scripts do this already.
- **One-frame conventions.** S01E05 measured 87 ms late from the runbook's
  `(master_frame - (pad + 1))` AudioSkip term and one-frame-short `DELTA`. Prove the EFM
  origin with the duration identity (corrected WAV vs IVTC duration) and prove
  output-frame-0 with descriptor matching, then re-measure absolute A/V on the delivered
  file. Recorded in the runbook §4/§5 lesson.
- **RIFE model version.** Record the engine and model separately for each machine. Keep
  `method` accurate in the report — the identity gate compares the delivered frame
  against the PNG that was actually injected, so a model change must not be mislabelled.
- **Orc version lookup.** `orc-cli --version` is not supported by the tested CLI. Resolve
  the release from package/app metadata or the official release source and record that
  evidence; a file timestamp alone is not a version proof.
- **Cut pads.** Never RIFE across a shot cut: run the flank 1−NCC test and calibrate it
  against this episode's real cuts before generating frames (`--hold-cut` skips them).

**Mechanical**
- `select='eq(...)'` expressions: ≤32 terms or extraction silently mis-selects (chunk them).
- Never make A/B copies as hardlinks then overwrite with `Copy-Item -Force` (same inode).
- Respect the current profile's `limits.heavy_jobs`; never run two downloaders for one item.
- Flowframes' `FlowframesCmd.exe` can silently no-op without elevation/dev mode; verify
  behavior and prefer the upstream headless `rife-ncnn-vulkan` engine where appropriate.
- PowerShell 5.1 traps: `$args` is an automatic variable (do not use as an accumulator),
  `*>` redirection of native stderr can abort mid-encode, here-strings mangle embedded
  quotes when piped, `Get-FileHash` binds `-f` oddly — `pipeline.ps1`'s `Invoke-LdFfmpeg`
  exists specifically to capture native stderr safely.
- Do not pipe a running `download_archive.ps1 -ListOnly` process to an early-terminating
  consumer such as `Select-Object -First`; it can break the output pipe and corrupt the
  child exit code. Capture all output, save `$LASTEXITCODE`, then inspect/filter it.
- PowerShell `Tee-Object` output may be UTF-16LE. Detect a BOM before parsing logs; empty
  UTF-8 regex matches are not proof that the program emitted no diagnostics.
- A verification/assertion is not a pass if it skips unparseable inputs. Report expected
  versus checked counts; any parse error or omitted input must fail closed, not be filtered
  out before the assertion.
- If a volume is exFAT, network-mounted, or reports I/O warnings, treat failures as
  real and prefer a reliable local fast volume for large renders.
- For remote sessions, launch long jobs detached and monitor them by process identity and
  artifact/log progress.
- **OS Temp cleanup:** keep any cache or intermediate required after the current command
  under `roots.probe`, `roots.logs`, or its declared output root. Use OS Temp only for
  disposable scratch that can be recreated within the same stage; do not run broad Temp
  cleanup while a job is active. If a required artifact disappears, verify its source and
  regenerate it rather than continuing with a stale path.

---

## 6. Starting a new episode (the process contract)

1. Follow the Tool-update gate: check latest versions, present the missing/outdated
   component list, and ask the user to choose specific components, **Update all**, or
   **Continue anyway**. Apply only their choice and record it; then run platform
   preflight/capability checks (`preflight.ps1` on Windows) → `PREFLIGHT OK`.
2. Preview the Archive selection and free-space gate, then start one resumable verified
   download (TBC + JSON + raw EFM only; never `.pcm`/`.log`). Record sizes/MD5s in the
   final download manifest. While it runs, independently verify/probe the matching DVD,
   check its stream/timeline/subtitle facts, and save preliminary evidence under
   `roots.probe` / `roots.logs`. Keep that evidence separate from the not-yet-created
   episode manifest. Defer resource-heavy prep if it would compete with the transfer.
   DVD-only work cannot infer LD timing/cadence or the DVD↔LD map; do not build or render
   the stack until all captures verify.
3. After the complete download manifest verifies, run `scripts\init_episode.py` and
   validate its inputs. Do not create a placeholder manifest to get ahead of the download.
4. Run `python scripts\select_stack_reference.py --episode <code>`. It uses Orc's
   recommendation from a scratch refresh, repeats with the suggested capture first until
   stable (maximum three passes), records the selected reference, and saves the refreshed
   final project without rendering. Review its report; confirm the final recommendation
   is input 1 and all captures were analyzed. Record the measured alignment map and later
   stack metrics into `episodes\<code>.json` as you obtain them: capture set, reference,
   `alignment_map`, `stack.frames`, `audio.capture/pad_frames`, rate fit ± stderr,
   `video.frames/duration_s/head_trim/fade_up_output/delta_s`, cadence verdict,
   `pads.*`, `retime.breaks[]` (content-verified offsets, never raw `blackdetect`
   durations), `tail_off`, SRT cue count.
5. Run the stage scripts with `--episode`; each refuses to invent a missing value.
6. DVD picture-map and silence-edge adjudication remain episode-specific measurements. Once the manifest contains the measured schedule, use `scripts/retime_dvd_assets.py`; never run an old `*-s01e05.py`/`*-s01e04.py` generator for a new episode.
7. Finish with `qc_report.py --episode <code>` → exit 0 before anything is delivered.

**Flags to look out for at each gate**

| gate | green | investigate when |
|---|---|---|
| PAD audit | predicted pads == isolated single-frame blacks, `zero_max==0` | orphans (check against the DVD), predicted-but-absent pads, `zero_max>0` |
| full-raster | `rgb_max ≤ 2`, `rgb_mean ≤ 0.0005` | anything higher: chroma-only black, not a pad |
| cut test | flank 1−NCC below this episode's cut floor | score in the cut range → `--hold-cut` |
| black scan | `0/<n>` PAD-aligned hits | any hit remains |
| identity | own ≥ 0.99, separation ≥ 0.10 | layout drift — rebuild the AVS |
| absolute A/V | \|const\| ≤ 100 ms, \|slope\| ≤ 30 ppm, rms ≤ 40 ms | large constant → AudioSkip/DELTA; large slope → rate fit |
| between-track | rms ≤ 40 ms, \|slope\| ≤ 30 ppm | drift → the retime schedule, not the offset |
| SRT | count == manifest, monotonic, in range | skipped cues or out-of-range times |
