# PORTING — moving ld-pipeline between machines

The workflow is profile-driven. Resolve executables and storage roots from the local
`machine.json`; do not assume a host name, drive letter, user directory, or mount point,
and do not edit scripts to insert machine-specific paths.
The authoritative version is stored in `WORKFLOW_VERSION`; core-update and device-log
rules are in `Versioning/WORKFLOW_VERSIONING.md`.

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
`preflight.ps1`. macOS uses POSIX profile paths and the bundled VapourSynth companions;
it cannot use the Windows AviSynth demuxer or Windows preflight. The M5 Mac has completed
S01E10 end to end through final remux and seek-defect repair, validating the tested macOS
arm64 configuration. A separate fresh-Mac installation is not independently certified.
V7 includes a Linux x86_64 dependency/environment candidate and common
capability checks, but no full Linux episode run has passed. Linux x86_64 is not a production
support claim; Linux ARM64 and Windows ARM64 remain untested.

## V7 unified setup, dependency lock, and stage runner

The setup sequence is now: profile discovery/copy, downloader-specific profile/curl
(curl >=7.71.0), Archive metadata and space checks, then start the transfer. Do not wait for the full PORTING
tool inventory, `python scripts/bootstrap_deps.py --print --json`, or doctor before the
download; complete those while it runs. Do not install/update tools during the transfer.
All selected tool changes, dependency locks and preflight/doctor gates must pass before
processing. In shared mode, the README-folder instance workspace is created/reused
automatically by `run_stage`; an explicit `--instance-root` resumes a chosen instance.
If the shared folder is unavailable, processing uses a warned, isolated local fallback
rather than blocking on folder setup. A machine-only doctor report is diagnostic but not
episode-ready. The bootstrap command is read-only unless the operator
explicitly adds `--install`; it never changes Python, machine profiles, native tools,
plugins, or models. The locks pin the Python version and direct distributions. They are not
hash locks of every transitive wheel/build; the conda environment YAMLs pin direct packages
but need a recorded `conda-lock` resolution for bit-for-bit reproduction.

| Host target | Direct Python lock | Environment recipe | Support note |
|---|---|---|---|
| Windows x86_64 | `dependencies/requirements-windows-x86_64.lock.txt` | Use the profile-selected Python/venv. | V7 authoring environment lock: Python 3.10.14, NumPy 2.2.6, OpenCV 4.13.0.92, Pillow 12.2.0. |
| macOS arm64 | `dependencies/requirements-macos-arm64.lock.txt` | `dependencies/environment-macos-arm64.yml` | End-to-end S01E10 completed on the M5 Mac, including final remux and seekability repair; fresh-device installation remains unverified. VapourSynth/plugins/native tools remain distinct components. |
| Linux x86_64 | `dependencies/requirements-linux-x86_64.lock.txt` | `dependencies/environment-linux-x86_64.yml` | Candidate only. Python/QC package pins are copied from the Windows base where compatible; native plugin/toolchain and full delivery are unvalidated. |

After the tool-update decision and before setup, use `bootstrap_deps.py --install` only if
the user selected that install/update action. It uses `pip --no-deps` against the target's
direct-package lock to avoid silently resolving unrelated versions. For Mac/Linux conda
environments, inspect the YAML and current package/build plan before asking the user to
create the environment. Do not interpret a passing Linux dependency check as permission
to process a production episode.

`doctor.py` combines the Windows or Mac functional preflight with the same direct-lock
check. On Linux it checks profile roots, reserve, common FFmpeg capabilities, Orc core
stages, and Python pins, but always reports `ready_for_episode=false` while Linux support
is unvalidated. Save JSON with `--report-out <new-file>` when a durable diagnostic is
needed; reports are no-overwrite. In shared mode, report output must stay under the active
instance's `logs/` folder, never the release root.

`run_stage.py` is a bounded dispatcher, not an automatic end-to-end scheduler. It lists
allowlisted stages, prints a dry-run command plan by default, and requires `--execute` for
execution. The download stage runs its own profile/curl, Archive integrity, and space
checks without waiting for the full doctor; other write stages run doctor and their declared
manifest validators first. Logs and JSON reports go to the episode workspace logs folder
(or its local fallback). It does not download sources, render Orc
projects, choose measurements, bypass user decisions, run arbitrary shell text, or clean
files. Linux production stage dispatch is blocked. Each stage report records active elapsed
time and gate/action breakdowns; waits between invocations are excluded. Use
`scripts/stage_clock.py start/pause/resume/stop` around manual stages, pausing while waiting
for a person's gate decision.

The Qwen runbooks are generated from the canonical runbook plus two short model-policy
templates. Edit the canonical technical procedure or the template, then run
`python scripts/generate_runbooks.py`; use `--check` in validation/CI to detect drift.
The ASCII companion continues to be generated by `scripts/regen_plain_text.py`.

### Device workspaces beside the selected README (V7)

The directory containing the `README.md` selected for the episode is the workspace anchor;
it may be W:, another shared release, or a device-local `ld-pipeline` copy. Do not
hard-code a particular drive. Apply the same read-only-source/device-workspace policy to
any copy. Keep each machine profile outside this README folder and set `shared.enabled=true`
plus `shared.release_root` to this copy's bundle:

```json
"shared": {
  "enabled": true,
  "release_root": "<absolute path to this ld-pipeline folder>",
  "device_name": "<sanitized device label>"
}
```

A release/share administrator makes the README folder and all release files read/execute
only. `devices/` is the sole writable child (pre-provision it or allow creation of that
exact directory). A device may create its own instance folder and modify only that folder
and descendants, not the parent `devices/` contents or another device's folder. V7 does
**not** change ACLs.
Create a no-overwrite local profile with
`python scripts/shared_workspace.py configure-profile --source <local-profile> --output
<new-local-profile>` when needed; both profiles stay outside the README folder. The doctor
checks whether this process appears able to write the release root; this is not an ACL
audit. Shared-mode processing remains blocked if root writes are available.

At episode start, device-instance workspace creation is a **required setup instruction,
but not a workflow gate**. Prefer `ensure`:

```text
python scripts/shared_workspace.py ensure --profile <local-machine.json> --episode <code>
```

The expected path is `<README-folder>/devices/<device>-<instance-id>/episodes/<code>/`,
with separate `manifest/`, `logs/`, `references/`, and `cut-pad-images/` subfolders. `ensure`
reuses one unambiguous device/episode workspace or creates it under this README's folder.
`run_stage.py` invokes this setup automatically if `--instance-root` is omitted. If the
README-folder `devices/` path is missing/inaccessible, it warns and falls back to the
machine profile's local `roots.logs` or `roots.work`; this keeps manifests/logs isolated
without writing elsewhere in the README folder. Fallback data is not shared with peers.
Use the returned `LDP_INSTANCE_ROOT` to resume; use a different explicit root for a
separate concurrent instance.

In shared mode, manifests/backups go to this instance's `manifest/`, workflow logs to its
`logs/`, and its references/cut-PAD inbox stay within the same instance. Curated release
files and legacy `episodes/*.json` are read-only/reference-only. Work, render, cache,
download, and final-output roots stay on the machine's configured volumes, never beneath
the README folder. For reuse by another instance, publish only reviewed small reports/reference files with
`shared_workspace.py publish-reference`; cut-PAD stills use
`shared_workspace.py publish-cut-pad`, which requires the verified DVD hash and exact
output PAD mapping plus the explicit `--confirm-mapping` acknowledgment. Publications are
no-overwrite and carry per-file provenance.

Each workspace creates `logs/README.md` with the evidence contract. Keep unique stage logs
and JSON reports, exact commands, workflow version, timestamps, gates, full output/exit
codes, Orc JSON and stderr, FFmpeg sidecars, hashes, and tool-version/preflight reports.
Never replace full evidence with a filtered tail or overwrite/delete a prior run. Do not
put credentials/tokens in command lines; path redaction is not a general secret scrubber.

The V7 release sync copies committed source files only and never deletes untracked files.
Keep device profiles, manifests, references, plugins, and all `devices/` workspaces outside
the sync allowlist so one device cannot replace another device's data.

## Fresh macOS / Apple-silicon setup (V7)

Use a **local writable copy** of this bundle and local fast storage for work/render files.
Do not process large captures from SMB. Start from the portable Mac example, not an
imported Windows profile:

```zsh
cp machine-macos.example.json machine.json
# Edit machine.json: replace /Users/YOUR_ACCOUNT and /Volumes/YOUR_FAST_VOLUME,
# use absolute executable paths, and create the roots directories on local storage.
```

Before installing/updating, run the PORTING tool-version gate and let the user choose the
components. The following is the **tested M5 baseline snapshot from 2026-11-14**, not a
permanent version pin; re-check upstream releases and plugin wheels before every fresh
setup. Keep Decode-Orc at the Windows-master parity version for an episode (v2.11.1 was
the tested parity/current release in that snapshot); do not upgrade mid-episode.

The M5 first-run route was user-space micromamba rather than assuming Homebrew/admin access.
After the update gate and the user's install choice, the setup shape is:

```zsh
MM="$HOME/tools/mm/bin/micromamba"
"$MM" create -y -p "$HOME/tools/vsenv" -c conda-forge \
  python=3.15 ffmpeg xxhash zimg pkg-config fftw
PY="$HOME/tools/vsenv/bin/python"
"$PY" -m pip install vapoursynth vapoursynth-tivtc vapoursynth-bestsource \
  numpy opencv-python-headless pillow
"$PY" -m vapoursynth config
```

Treat this as a **setup template, not a blind install command**: inventory current
versions/wheel tags first, show the user the tool list, and apply only their choice. The
production `tools.ffmpeg`/`tools.ffprobe` paths may point to a separate arm64 static build
with `libx265`; the conda FFmpeg libraries support plugins. Set all executable paths in
`machine.json`, then verify the same selected Python/environment through `vspipe` and
`preflight_mac.py`. The V6 wrapper supplies the tested `DYLD_LIBRARY_PATH` for plugins
linked to the environment's libraries.

### Required baseline toolchain

| Component | Role / requirement |
|---|---|
| Decode-Orc GUI + `orc-cli` | Install the official macOS arm64 DMG. Resolve `orc-cli` inside the `.app` bundle and set `tools.orc`; verify its stage list and parity version. |
| FFmpeg + FFprobe | Set absolute paths in `tools.ffmpeg`/`tools.ffprobe`; must have `libx265` and `yuv4mpegpipe`. The tested static arm64 build was FFmpeg 9.0.2 with x265 4.2. If `libsoxr` is absent, V6 audio timing records and uses the tested FFmpeg default/SWR fallback. |
| PowerShell 7 (`pwsh`) | Needed for shared `.ps1` download/cadence helpers. The tested user-space build was 7.7.0; set `tools.pwsh`. |
| micromamba + one Python environment | The M5 used micromamba 2.10.0 and Python 3.15.2; set both `python.interpreter` and `tools.vspipe` to that same environment. |
| VapourSynth + `vspipe` | The tested version was R82. Run `vapoursynth config` once. Always smoke-test through `vspipe`; Python import alone does not prove plugin/library loading. |
| `vapoursynth-tivtc` v4 + `vapoursynth-bestsource` R23 | Native TFM log (`TFM(output=...)`), TDecimate, and the tested ProRes/DVD source reader. Prefer current compatible PyPI wheels in the selected arm64 Python environment over the old M5 source builds. |
| Python modules | `numpy`, Pillow (`PIL`), and headless OpenCV (`cv2`) are required for QC/PAD identity. Install in the profile-selected Python environment. |

RIFE is conditional on measured non-cut PADs: record the engine and weights separately.
The tested M5 combination was upstream engine 20221029 plus `rife-v4.6`, native arm64 /
Vulkan. 7-Zip (`7zz`) is optional and needed only if the selected Archive item has
`.efm.7z` files.

### Conditional VapourSynth deinterlace dependencies

Install these only if the episode's measured cadence reports interlaced inserts and the
user has approved the needed tool updates. The M5 validated `QTempGaussMC().deinterlace()`
path from `vsdeinterlace` with its mapped parameter set; required plugin packages at the
time included `vapoursynth-znedi3`, `vapoursynth-resize2`, `vapoursynth-mvutensils`,
`vapoursynth-dmetrics`, `vapoursynth-mvtools`, and `vapoursynth-zsmooth`. Confirm current
package names/versions and transitive dependencies in the actual environment. Do **not**
use the similarly named PyPI `havsfunc` decoy or install the whole `vsjetpack[deinterlace]`
extra without reviewing its extra dependencies. If the measured repair is not configured,
the Mac cadence builder must refuse rather than silently omit the deinterlace.

### First-run validation order

1. Run `python scripts/preflight_mac.py --json` and resolve required failures. This is a
   capability check, not a quality pass.
2. Run the Python unit suite with the selected environment and bundle on `PYTHONPATH`:
   `PYTHONPATH="$PWD" "$PY" -m unittest discover -s tests -p 'test_*.py'`.
   VapourSynth-only tests are explicitly skipped when that module is not installed.
3. Run the preflight's TIVTC → `vspipe` → Y4M smoke; confirm `build_map_vpy.py` output,
   frame count, field property, trims, and measured map. Use `encode_episode_mac.py --pass
   validate` (three-frame null decode; no x265) on the current source-derived chain.
4. Validate the PAD transport on real measured PADs: `pad_audit.py` and `fullres_check.py`
   must read the `.vpy` source; `rife_padframes.py` must preserve exact frame indices;
   `build_padfix_vpy.py` must pass both frame-count assertions; validate again before the
   one final encode. Keep DVD and no-DVD choices and QC limits the same as Windows.
5. The M5 S01E10 run has completed the full path through video-only final encode,
   `remux_padfixed_mac.py`, and final delivery on macOS arm64. New Mac installs still need
   their own preflight/tool/version checks; do not generalize this result to untested
   hardware or dependency combinations.

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

The M5 V5 adoption log records **36 pass, 1 warn, 0 fail** after the corrected preflight
and a successful POSIX ListOnly space gate. E10's 18/18-file download was later verified;
M5 then completed manifest/input/stack/cadence/audio stages and exercised the V5 Mac PAD
and validate ports. The log does not contain a final V6 delivery/QC result. Those are
machine-specific validations, not a fresh-Mac guarantee. Re-run the current checker and
ListOnly gate on the target Mac/profile, and finish a complete episode before claiming
end-to-end Mac support. The M5 tool inventory was current at its logged check; perform the
required upstream version check again before any new setup.

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
  `mux_tracks` to `['ld']`. With a DVD, add `51`/`srt` to both only after their sources
  and retiming data are verified. V6 remux maps `ld_synced`, `retimed_51`, and
  `retimed_srt` from separate files; a legacy `pre_upscale` aggregate is supported only
  for older manifests. No-DVD mode remains LD stereo only.

### TFM compatibility policy

For workflow execution, treat TFM as compatible when Windows and Mac are kept on the
latest supported software versions. Do not require a TFM parity flag, report evidence,
or parity clips before cadence. A historical cross-machine PASS is documented in
`M5 Mac LaserDisc Project Updates Log.md`, §8, but it is background evidence rather than
a recurring gate. If an actual TFM error or disagreement is observed during processing,
stop and diagnose it; do not infer the episode's cadence from the historical clips.

### macOS arm64 end-to-end validation — completed on M5

The Mac reference snapshot's `align_dvd.py` is not included: the authoritative
`extract_video_descriptors.py` + `measure_dvd_alignment.py` pair is the only supported
DVD-map implementation. Mac machine profiles and episode manifests remain local and are
not copied into the authoritative bundle.

The M5 project log §22 records S01E10 from verified downloads through final encode and
multi-track remux. All nine workflow stages were green (225 pass, 0 warn, 0 fail); the
deliverable was 4.077 GiB with LD stereo default, retimed DVD 5.1, and English SDH. Initial
QC exposed an indexed-seek audio dropout; after correcting the Matroska interleave option,
all six failing seek windows decoded fully, 0/1067 clusters lacked audio, and all four
packet-identity checks passed. This is end-to-end evidence for that M5/macOS arm64 setup.
It does not certify a fresh Mac, all Mac hardware, or every package/native-tool combination.

For a new Mac, follow the local-profile and approved-tool setup below, then run
`preflight_mac.py`, the test suite, and the episode workflow. Keep `build_map_vpy.py`
QTGMC dependency/frame-rate checks for episodes that require measured interlaced-insert
repair. Final `qc_report.py` now also checks indexed audio seeking on every track and points
to the proven `-max_interleave_delta 0` remux fix if needed.

---

## Tool-update gate — Windows, macOS, and Linux candidate

Run this gate during machine setup and for each new episode. For a new episode, it may run
after the source download has started, but **must pass before processing/rendering**. The
download itself uses the narrower downloader checks (Archive identity, curl/profile,
file-integrity metadata and destination space); do not wait for the full tool inventory.
Use the paths in that machine's `machine.json`; a version
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
| macOS | FFmpeg/FFprobe (`libx265`, `yuv4mpegpipe`); Decode-Orc and core stages; PowerShell; micromamba; profile-selected Python/VapourSynth/`vspipe`; `vapoursynth-tivtc` v4 and `vapoursynth-bestsource`; NumPy, headless OpenCV, Pillow. Conditional QTGMC plugins only if measured inserts require them. RIFE engine/model and 7-Zip only if the input needs them. Use the exact environment used by `vspipe`. |
| Linux x86_64 candidate | Python, NumPy, headless OpenCV, Pillow from the candidate lock; FFmpeg/FFprobe with `libx265` and `yuv4mpegpipe`; Decode-Orc, PowerShell, VapourSynth/`vspipe`, TIVTC/BestSource and native plugin builds must each be inventoried and capability-tested. All are candidate dependencies only; do not claim production support. |

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
  plugin-linked libraries must work together. Resolve Decode-Orc from the
  app bundle through `machine.json`. Windows `.dll` plugins and the AviSynth demuxer do not
  apply. V6 added `build_map_vpy.py`, `build_padfix_vpy.py`, `encode_episode_mac.py`,
  `remux_padfixed_mac.py`, `vspipe_util.py`, and `.vpy` transports for PAD audit, full-raster
  checks, RIFE neighbor extraction, and motion traces. The M5 log records unit/smoke passes
  and V7 adds the unified doctor, Python lock/bootstrap, stage runner, and runbook generator.
  The M5 log now records a completed S01E10 final delivery/remux and corrected seekability;
  macOS arm64 is end-to-end demonstrated on that tested setup, while fresh-device coverage
  remains open. The no-DVD timing path is available on
  Mac through the same Python/FFmpeg helper; check
  the exact FFmpeg build for `libsoxr` (the M5 static build lacked it and uses a logged SWR
  fallback). The no-DVD QC verifies capture-pad geometry and exact sample duration, but does
  not claim external absolute A/V correlation.
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

1. Copy the whole `ld-pipeline/` folder; keep its relative structure intact. The selected
   README's parent folder is the device-workspace anchor, wherever that copy lives.
2. Keep the device profile outside that folder. Seed a device-local profile from the
   platform example (or a preserved local profile), then use
   `shared_workspace.py configure-profile` so `shared.release_root` points to this exact
   README folder. **Windows:** run `preflight.ps1 -Profile <external-profile> -Write` to
   discover/initialize that external profile; this is not the final readiness pass. **Mac:**
   do not run the Windows preflight; create a POSIX profile with `os: darwin`, configure its
   README root, then run `python scripts/preflight_mac.py` as a capability check.
3. Set roots discovery cannot infer, especially `roots.work` and `roots.dvd_root`; set
   `upscale_root` only when legacy E01 assets are present.
4. Run the Tool-update gate with the local profile paths. For a new episode, the verified
   download may start after its narrower gates; perform the full version inventory while
   it transfers. Do not apply tool updates until the transfer completes. Ask the user which missing or
   outdated components to install/update, whether to **Update all**, or **Continue anyway**;
   apply only that choice. Then run Windows preflight until `PREFLIGHT OK` (0 fail), or on
   Mac run `python scripts/preflight_mac.py` and resolve its required failures; do not
   claim full-workflow readiness from that capability check alone.
5. For a new job, call `shared_workspace.py ensure --profile <external-profile> --episode <code>`
   (or let `run_stage.py` resolve it non-blockingly), then use `run_stage.py`'s download and
   init-episode stages. The manifest is created only after the verified download.

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

1. Confirm the Archive URL/identifier and intended download folder. Verify the local
   profile and PowerShell/curl executable (curl >=7.71.0), then run downloader `-ListOnly` to check Archive
   identity, required file metadata/sidecars, and projected destination free space. Start
   one resumable verified download immediately after this narrow gate (TBC + JSON + raw EFM
   only; never `.pcm`/`.log`). Record sizes/MD5s in the final download manifest.
2. While the transfer runs, perform the full Tool-update gate: check latest versions,
   present the missing/outdated component list, and ask the user to choose specific
   components, **Update all**, or **Continue anyway**. Apply only their choice after the
   transfer, never mid-transfer; then run platform preflight, dependency-lock and doctor
   checks before processing. During the download, independently verify/probe the matching DVD,
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
