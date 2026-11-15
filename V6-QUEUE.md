# Version 6 release plan and deferred follow-ups

**Authoritative marker:** Version 6 (`WORKFLOW_VERSION`). Items marked integrated are
included in this release; all other entries remain deferred proposals and do not change
the V6 procedure or authorize untested behavior.

## Authority and distribution roles

- `Z:\ld-pipeline` remains the home base and authoring authority for this project.
- `W:\ld-pipeline` is the network distribution share from which LENOVOFLEX, M2, and M5
  should source a published workflow release. W is a mirror/distribution point, not a
  second authoring authority; workflow changes originate at Z and are then published to W.
- Device-local `machine.json`, episode manifests, work outputs, and detailed run logs stay
  local unless explicitly selected for publication. Devices record their own current
  version and the source release/commit they consumed.

## V6 integrated work and deferred follow-ups

### 1. Multi-device release and run-log sharing

Use the same shared parent for the authoritative workflow folder and one uniquely named
log/evidence folder per physical machine. Proposed layout:

```text
\\<share>\<project-parent>\
  ld-pipeline\                         # shared workflow/release folder
    current.json
    releases\v5\                      # immutable, read-only release snapshot
    releases\v6\
    Cut Pad Images\                    # curated release set, grouped by episode
      S01E01\
      S01E02\
  <sanitized-machine-name>-<XXXXXXXX>\  # sibling of ld-pipeline, not inside it
    S01E01\
      logs\                            # unique, immutable run packages
      cut-pad-images\                  # PNGs of confirmed cut PADs only
    S01E02\
      logs\
      cut-pad-images\
```

`XXXXXXXX` is a cryptographically random 8-character alphanumeric suffix, generated once
per machine and persisted locally so the same host reuses its folder. Sanitize the machine
name for the target filesystem, check for collisions, and never derive the suffix from a
username or other personal data. Each log filename also needs a unique UTC/run ID so
multiple OpenCode instances on the same machine cannot overwrite one another.

Requirements:

- Pin each run to both `WORKFLOW_VERSION` and the source Git commit/release hash. Keep
  working files and `roots.logs` as local staging; publish complete reviewed log packages
  atomically into that machine's sibling folder. Never append to a shared log or modify a
  published log/release in place.
- Include stage/gate outcomes, tool versions, commands, measured values with units and
  conventions, input/output hashes, retries, approvals, and unresolved issues. Redact
  secrets and omit machine-local absolute paths when logical roots/hash evidence suffice.
  Do not put raw media, caches, DVDs, or non-cut reference frames in these folders.
- Reserve each machine's folder for that machine's writes; make the shared `ld-pipeline`
  release tree read-only to consumers. Test access controls, atomic upload/rename behavior,
  interrupted uploads, and recovery. Define a same-episode single-writer/claim policy so
  instances do not concurrently edit one episode manifest; separate logs remain safe to
  publish concurrently.
- In each machine/episode `cut-pad-images` folder, store **only** high-quality, lossless PNGs
  of independently confirmed cut PAD replacements sourced from the corresponding DVD.
  Name each PNG with its verified DVD timeline timestamp, using the portable pattern
  `DVD-HH-MM-SS.mmm-fNNNNNNNN.png` (for example, `DVD-00-32-14.581-f00048232.png`); the
  frame-index suffix disambiguates timestamps that round to the same millisecond. Derive the
  timestamp from the sequentially decoded DVD frame and validated timeline/alignment, not an
  unverified or non-monotonic container PTS and never an unsafe seek. Record the exact
  timestamp/timebase, decoded DVD frame index, DVD identity/hash, picture-map evidence,
  crop/scale/color transform, and destination PAD index in the episode log. Never include
  non-cut PADs, RIFE-generated frames, full DVD framesets, or other episode imagery; keep the
  PNG lossless and do not store the DVD. Preserve this timestamp-based filename when images
  are curated into the authoritative release folder.
- On every accepted release publication, curate the reviewed available images into
  `Z:\ld-pipeline\Cut Pad Images\<episode>\` (the authoritative copy, grouped by
  episode) and sync that same approved set into W's release. Deduplicate identical images;
  if candidates differ, preserve both in staging and require technical review rather than
  overwriting. Per the project owner's direction, do not add a separate rights-review gate
  for the individual cut-pad stills. Keep the publication limited to verified single-frame
  cut-pad PNGs; never include DVDs, full framesets, or video sequences.
- For the V6 release, retroactively inspect completed episode logs/artifacts and extract
  images only where the exact DVD source is available and the cut-PAD identity/timing can
  be re-verified. If evidence or source media is unavailable, leave that episode empty and
  report it; never fabricate or infer a DVD replacement image from an encode.
- **V6 retroactive set assembled:** S01E04 and S01E07 each have one confirmed cut-PAD PNG
  under `Cut Pad Images/<episode>/`, named by the verified DVD timestamp/frame; source DVD
  and PNG SHA-256 provenance is in `Cut Pad Images/README.md`. No other episode image was
  added without a similarly verified cut replacement.
- Keep V5's Windows/AviSynth versus M5 VapourSynth support status explicit; a shared folder
  does not prove every platform is V5-ready.

Before implementation, confirm the actual share parent/UNC path, machine-name source,
permissions (read-only release tree; per-machine write area), retention/backup policy, and
same-episode concurrency policy.

**V6 status:** the curated `Cut Pad Images/` release folder and E04/E07 retroactive stills
are integrated. The per-machine sibling folder creation, ACLs, and concurrent atomic log
publication are still deferred; do not claim multi-instance shared logging is implemented.

### 2. Optional DVD-free audio path — integrated in V6

**Evidence complete and accepted for V6 by the user.** In
`Z:\work\logs\S01E07-v5-rerun.md`, the candidate was generated from native EFM audio and
LD-side stack/PAD/timing evidence without opening a DVD or using the DVD-derived rate fit.
The DVD was only an independent holdout afterward. E07 reconciled 84 full-map PAD entries
to 83 post-head-trim entries; its fractional duration/PAD residual is +0.32 frames and is
retained (not rounded away). The saved-report robust fit was 44056.90 Hz; the duration
candidate was 44056.129 Hz (2x integer path effective 44056.00). Against the same picture
reference and seven windows, both routes passed and differed by at most 1 microsecond; the
final-format no-DVD test deliverable had identical video packets and passed A/V gates when
the DVD was used only as an after-the-fact evaluation reference. **The existing E07
manifest/deliverable remains DVD-backed and unchanged.**

Implementation is in the V6 candidate: initialization supports an explicit `--no-dvd`
choice, `audio_timing_diagnostic.py --promote-dvd-free` creates corrected/synced audio and
records the recipe with a backup, and `ld_only_sync_check.py` validates the origin method
and exact output sample budget without claiming external absolute A/V correlation.

V6 behavior:

- At job start, ask for the matching reference DVD/version. If supplied, verify it and use
  the normal DVD-reference workflow. If not supplied, explicitly ask whether the user wants
  to proceed with LD-only timing. No-DVD mode means LD stereo only; do not invent DVD 5.1,
  DVD subtitles, picture-map offsets, or DVD retime data.
- In the approved no-DVD path, select the LD audio capture from EFMSink reports, use the
  measured Orc capture-pad offset and LD-derived output-frame-zero time, run the duration/PAD
  model, and reconcile full-map/projected PAD counts, EFM structural trim, sample count,
  skip, and both endpoints. The bounded V6 promotion helper writes a full-length corrected
  FLAC and exact-sample `ld_synced`, then records method, effective/intermediate rate,
  resampler, offsets, endpoints, hashes, and report in the episode manifest. It refuses
  overwrites and refuses promotion when the absolute endpoint residual exceeds 1 frame or
  required evidence is missing. The accepted E07 test fixture (+0.32 frames) and E06's
  distinct 0-ppm-intrinsic-rate / separate-PAD-stretch case guard the implementation.
- With no DVD, QC validates this explicit origin/capture-pad method and the exact audio
  sample budget against the final measured video frame count. It must **not claim** an
  external absolute A/V cross-correlation: QC reports that limitation, and the user must
  have chosen no-DVD mode. Existing full DVD-based A/V gates remain unchanged when a DVD is
  supplied. A/V endpoint/PAD disagreement is STOP, not a rate guess.
- If no DVD was supplied and shared cut-pad PNGs exist for the episode, ask whether the user
  wants to use them. Explain that each is one verified, timestamp-named DVD frame prepared
  to replace a confirmed one-frame black PAD at a scene cut; it is not a full DVD or a
  source for audio, picture mapping, 5.1, or subtitles. Use one only after its episode,
  timestamp/scene, and output PAD mapping are verified. If declined, unavailable, or
  ambiguous, never RIFE across the cut: hold the previous native frame or STOP under the
  existing cut-PAD rules. Non-cut PADs keep the existing source-derived RIFE path.

Keep this path optional and explicit. A DVD, when available, continues to enable the
existing independent picture-map, A/V holdout, 5.1, and subtitle workflow; do not silently
substitute DVD-free evidence for those measurements.

### 3. Root `changelog.txt` — integrated in V6

Maintain a plain-text `changelog.txt` in the root of `ld-pipeline` with concise,
bulleted descriptions for each accepted workflow version. As part of V6, reconstruct
entries for Versions 1–5 from `WORKFLOW_VERSIONING.md` and the authoring/device logs; mark
any retrospective or uncertain detail rather than presenting it as a contemporaneous note.
For each future accepted version, add its date, `FROM Version N -> Version N+1`, and
user-visible/core changes. Keep machine profiles, episode measurements, personal paths,
and run-specific artifacts out of this workflow changelog. The detailed logs remain the
forensic record; `changelog.txt` is the short release-history index. Publish the same file
with the read-only release to W.

### 4. Conditional post-download free-space reserve

Evaluate a **35 GiB projected-free floor after download completion** only when the
download destination and `roots.work` are on the same underlying volume. For subsequent
write stages on that same volume, the projected-free check may retain that 35 GiB floor;
the stage's measured output/temp estimate is subtracted separately. When downloads and
work are on different volumes, keep the existing 80 GiB reserve on each applicable volume.
This is a proposed V6 policy, not a V5 setting or permission to edit a machine profile now.

Requirements before adopting it:

- Distinguish filesystem/volume identity from drive-letter or mount-path strings; aliases
  to the same volume count as one volume.
- Apply the conditional floor only to a same-physical-volume download/work plan and only
  once the downloader's manifest verifies all selected files. Do not apply it to split-volume
  jobs or use it as a reason to bypass the initial download projection.
- Keep every write-stage gate: projected free = current free minus the measured peak
  output/temp estimate, and require the applicable 35 or 80 GiB post-stage floor. Stage
  output estimates and the reserve are separate quantities.
- Back-test against E06/E07 and other recorded download/render/encode peaks, including
  failure cleanup and rerun space. Adopt only if every target machine passes without
  deleting user artifacts or relying on a lower reserve than the measured stage requires.
- Do not silently rewrite W's preserved `machine.json` (`min_free_gb=25`) or Z's 80 GiB
  profile as part of this queue item. Reconcile the LenovoFlex 25 GiB value separately
  before write-heavy processing.

### 5. Linux porting support ? target 1: x86-64; research-only, untested

No Linux machine is available for implementation or validation. This item is a research-based
recommendation only; Linux support must remain **untested / not supported by V5** until a
maintainer runs the test plan below. Do not treat a successful tool installation or shared
workflow folder as evidence of a working port. V5 remains authoritative and Windows-only for
production until a separately approved release passes the gates.

Recommended direction:

- Target a current supported x86-64 Ubuntu LTS first (Ubuntu 26.04 LTS was released
  2026-04-23); Debian stable may be evaluated as a second target. Record distro, kernel,
  architecture, GPU/driver, filesystem, locale, and package sources in every test report.
- For RF-to-TBC, upstream `ld-decode` documents Linux builds (including a Nix development
  environment and release artifacts). For downstream TBC processing, evaluate Decode-Orc's
  official Linux Flatpak (GUI and `orc-cli`); its CLI is run with
  `flatpak run --command=orc-cli io.github.decode_orc.decode-orc --help`. The legacy
  `ld-decode-tools` repository explicitly marks itself deprecated in favor of Decode-Orc;
  do not treat it as the default new Linux stack. Grant and test access to mounted source,
  work, and output paths explicitly: Flatpak grants home, `/run/media`, `/media`, and `/mnt`
  by default, with explicit overrides for other mounts; GUI file access does not prove all
  headless commands see the same files. Confirm each required stage/output (including EFM,
  audio, PAD, and stacking) against current Orc docs/releases before committing to it.
- Prefer native Linux VapourSynth for the filter/frameserver layer instead of assuming the
  Windows AviSynth+ plugin set will transfer. Official installation guidance supports Linux
  wheels and Python 3.12+ (`pip install vapoursynth`, then `vapoursynth config`); R82 was the
  latest release checked on 2026-11-14. Python 3.15.2 is current, but choose the newest Python
  version for which VapourSynth and every required plugin are verified together; 3.14.x is a
  conservative candidate, not a tested Linux pin. Use an isolated environment, and use
  `vspipe`/Y4M to a software encoder where appropriate.
- Treat source filters and script compatibility as a major porting gate. BestSource R23 was
  the latest upstream release checked; its upstream README requires FFmpeg 9.0+ for CPU use.
  Its optional GPU path requires Vulkan 1.4 headers and an FFmpeg master build with specific
  Vulkan support, so keep GPU decoding out of the baseline until separately tested. The current
  VapourSynth TIVTC upstream documents Linux/macOS source builds producing `tivtc.so` (Meson,
  C++20, built against the same installed VapourSynth Python environment); still build and test
  it rather than assuming a wheel or behavior. Inventory every V5 AviSynth filter (IVTC, frame
  replacement, source, resize/color, custom functions), then test a Linux equivalent for each.
  VapourSynth `std.FrameEval`/Python frame selection or a maintained RemapFrames port may cover
  some replacement cases, but prove equivalence against the episode scripts.
- Use FFmpeg with a verified `libx265` encoder for the production HEVC path. FFmpeg 9.0.2 was
  the latest stable release found in the 2026-11-14 research snapshot; confirm the current
  stable release and distribution build at installation time. Verify with `ffmpeg -version`,
  `ffmpeg -buildconf`, and `ffmpeg -encoders` that `libx265` is present. Preserve the V5
  video-only final x265 encode and audio stream-copy remux design; do not substitute a GPU
  encoder or change codec/metadata settings without a separately reviewed quality test.
- RIFE ncnn Vulkan has upstream Linux binaries/build instructions and can avoid a CUDA/PyTorch
  runtime, but requires working Vulkan loader/ICD and compatible GPU drivers. The upstream
  release history found was old; treat interpolation as optional/experimental, check current
  maintenance and model provenance, and do not make it a Linux baseline dependency.
- Use Python for cross-platform orchestration where practical. PowerShell 7 is available on
  Linux, but porting existing `.ps1` scripts still requires replacing Windows drive-letter,
  path, process, and filesystem assumptions. Prefer `pathlib`/logical configured roots in
  Python and `Join-Path`/platform-aware .NET APIs in PowerShell; never hard-code `C:\Users\...`
  paths. `micromamba`/conda-forge is an alternative for isolating Python/VapourSynth packages,
  not a guarantee that every native plugin is packaged.

Version currency and supply-chain checks (required at implementation time):

- Re-check official release/install pages immediately before setup for Ubuntu/Debian, Python,
  VapourSynth, Decode-Orc, FFmpeg, x265, PowerShell (if used), and each native plugin. The
  versions above are a dated research snapshot, not a lockfile or minimum-version promise.
- Official-source snapshot checked 2026-11-14: Ubuntu 26.04 LTS; Decode-Orc v2.11.1; Python
  3.15.2; VapourSynth R82 (official docs require Python 3.12+); FFmpeg 9.0.2 latest stable;
  BestSource R23; TIVTC v4/API4; PowerShell 7.7.0 latest stable and 7.6.10 LTS; upstream
  RIFE ncnn Vulkan latest release remains 20221029. Linux support for this workflow and these combined dependencies is
  still **untested**. These are dated observations, not a compatible lockfile. Re-check official
  releases and distro package metadata at implementation time; choose versions by verified
  compatibility, not newest-version number alone.
- Prefer distro packages or upstream signed/checksummed release artifacts; record exact
  versions, URLs, checksums/signatures, build configuration, and licenses. Avoid unverified
  third-party static builds for the production encoder.
- Keep dependencies isolated; verify binaries resolve the intended libraries, and test
  Unicode/space-containing paths, mounted/network paths, interrupted jobs, cleanup, and
  free-space accounting on the target filesystem.

Required port and acceptance work before Linux can be called supported:

- Create a Linux-specific setup guide and machine-profile example without modifying any
  device-local profile or changing V5's 80 GiB reserve policy.
- Audit every workflow script for Windows-only commands, drive letters, path separators,
  executable names, quoting, shell semantics, and assumptions about atomic rename and file
  locks. Keep logical roots configurable and preserve per-device profile isolation.
- Build a capability matrix for every V5 stage and plugin, with explicit alternatives and
  unsupported stages. Test a small known TBC through decode, filter/IVTC, intermediate render,
  video-only x265 encode, audio remux, metadata/QC, hashes, logs, and failure cleanup. Compare
  frame counts/timing, audio sync, stream metadata, and representative image/audio checks to a
  known-good V5 Windows result using existing gates and tolerances.
- Run the existing Python tests on Linux, add path/platform tests, run PowerShell parser/tests
  under supported `pwsh` if scripts remain, and document any test exclusions. Do not lower
  existing QC, synchronization, or reserve gates to obtain a pass.
- Mark Linux support as **untested** until those checks pass on real Linux hardware and the
  results are reviewed. Any Linux-specific workflow behavior belongs in a new accepted
  version, not in V5 or in machine-specific local edits.

Research links: [Decode-Orc Linux Flatpak](https://decode-orc.github.io/decode-orc/installation/linux-flatpak/),
[Decode-Orc releases](https://github.com/decode-orc/decode-orc/releases),
[Decode-Orc Nix source build (Linux and Mac)](https://decode-orc.github.io/decode-orc/installation/source/nix-source/),
[ld-decode](https://github.com/happycube/ld-decode),
[deprecated ld-decode-tools notice](https://github.com/simoninns/ld-decode-tools),
[VapourSynth installation](https://vapoursynth.com/doc/installation.html),
[VapourSynth releases](https://github.com/vapoursynth/vapoursynth/releases),
[BestSource releases](https://github.com/vapoursynth/bestsource/releases),
[BestSource R23 build requirements](https://github.com/vapoursynth/bestsource/blob/R23/README.md),
[TIVTC Linux build instructions](https://github.com/dubhater/vapoursynth-tivtc),
[TIVTC releases](https://github.com/dubhater/vapoursynth-tivtc/releases),
[Python downloads](https://www.python.org/downloads/),
[PowerShell releases](https://github.com/PowerShell/PowerShell/releases),
[FFmpeg downloads](https://ffmpeg.org/download.html),
[FFmpeg libx265 requirements](https://ffmpeg.org/ffmpeg-codecs.html#libx265),
[RIFE ncnn Vulkan](https://github.com/nihui/rife-ncnn-vulkan),
[Ubuntu 26.04 release notes](https://documentation.ubuntu.com/release-notes/26.04/).

### 6. README intro: design for free and low-cost models — integrated in V6

Add a short, prominent goal statement near the top of `README.md`: the operator pack is
intended to make the workflow clear and reliable enough for capable free or low-cost models
to complete successfully, including examples such as Qwen3.5 4B and Big Pickle. Present
these as **design targets/examples, not a guarantee or a claim of completed testing**.

V6 implementation should emphasize concise and prioritized instructions, explicit
ACTION/VERIFY and STOP gates, small bounded tasks, deterministic parameterized helpers,
a machine-readable episode state, actionable error messages, resumability, and minimal
reliance on long-context recall. Avoid requiring an expensive model for routine steps; keep
uncertain measurements and approval decisions as explicit human/escalation gates rather
than hiding them in prompts. Include a repeatable evaluation plan using representative
free/low-cost models (record exact model/provider/version, context and tool limits, task,
inputs, outcomes, retries, and unsafe/incorrect gate behavior). Do not weaken QC or safety
requirements to improve a model's pass rate. Retain the existing versioning rule: when this
README/core workflow change is accepted, bump the workflow version and record it in the
authoring log.

### 7. Remove `motion-probe.ps1` hard-coded FFmpeg path — integrated in V6

In V6, remove the author-machine WinGet path from `scripts/motion-probe.ps1` and resolve
FFmpeg using the existing per-machine configuration (`machine.json` `tools.ffmpeg`, with
the documented profile selection such as `LDP_PROFILE`). Follow the shared profile-driven
tool-resolution pattern; do not add a replacement username, drive letter, or install-path
guess. Preserve the current command arguments, filtergraph, output behavior, and failure
semantics. Keep `machine.json` device-local and do not publish the author's profile.

Tests must cover a configured executable path containing spaces, missing/invalid profile
and FFmpeg paths with actionable errors, invocation through the normal pipeline path, and
an independent temporary profile (no reliance on the author's local machine). Update
`GITHUB-EXPORT.md` to remove its known-limitation warning once the fix is implemented and
verified; search the source/docs for any remaining machine-specific FFmpeg paths. This is a
core workflow change: accept only with regression tests, incremented `WORKFLOW_VERSION`,
updated version history, and authoring-device log.

**V6 implementation:** the script now uses the shared `pipeline.ps1` profile resolver and
`tools.ffmpeg`; `GITHUB-EXPORT.md` no longer advertises the author-machine path.

### 8. Hardware-aware `heavy_jobs` policy during initial readiness

Add a read-only hardware/resource check to the initial tasks, before the first episode's
write-heavy stages. Record total and currently available RAM, physical CPU cores (and
logical threads separately), CPU class/power limits where available, GPU/model/VRAM and
usable driver when a GPU stage is selected, and the work-volume type/free space. Do not
run an expensive stress test or change a machine profile just to classify the host.

Recommended provisional classification (validate before adopting):

- Treat **16 GiB RAM plus 4 physical CPU cores** as a reasonable *single-heavy-job*
  baseline, not as proof that two concurrent heavy jobs will improve throughput. The
  existing LenovoFlex example is 4c/8t, 16 GiB, and records ~204-minute encodes and ~4?5-hour
  stack renders while recommending one heavy job at a time.
- For automatically removing the user-facing `heavy_jobs` choice, start conservatively at
  **32 GiB RAM plus 8 physical CPU cores**, with adequate available RAM, work-volume space
  and I/O, and any GPU/VRAM headroom needed by the planned stages. This is a proposed
  threshold, not a measured guarantee; verify it with the benchmark below. Physical cores
  matter more than SMT/logical-thread count for this classification.
- If hardware is below that threshold, incomplete, or resource headroom is poor, ask the
  user at startup whether to keep the conservative single-heavy-job limit (`heavy_jobs=1`,
  recommended) or disable that limit for **at most two independent heavy jobs**. Explain
  that disabling it may increase contention and total runtime; never interpret a missing
  setting as unlimited parallelism. If approved, record the choice for that run without
  silently persisting it to the machine profile.
- For hosts above the validated capable threshold, remove the per-run choice/flag from the
  normal startup flow and apply the tested bounded policy automatically. Do not let either
  policy parallelize dependent stages, two downloaders for one item, writes to the same
  outputs, or tasks that exceed free-space/resource gates.

Before adoption, test representative pairs of independent stack/render/encode/prep work
on each hardware tier, comparing one-at-a-time versus two-at-once wall-clock throughput,
per-job slowdown, peak RAM/VRAM, CPU/GPU utilization, disk/network contention, thermal
throttling, failures, and output QC. Report end-to-end episode savings separately from the
speed of the overlapped pair. If two jobs do not measurably improve safe end-to-end time,
keep serialization. `limits.heavy_jobs` is currently profile guidance rather than an
execution-enforced scheduler; V6 must implement and test the chosen policy before claiming
the setting controls concurrency. Preserve all existing dependency, QC, and free-space
gates, and document that V5 remains one-heavy-job-at-a-time until V6 is accepted.

### 9. Linux ARM64 tooling/dependency target ? research-only, untested

Evaluate Linux aarch64 as a separate target after the x86-64 baseline. This means a capable
ARM64 workstation/server, not an assumption that a small SBC can handle multi-capture TBC,
large intermediate files, and production x265 workloads.

Candidate stack and dependencies:

- Supported ARM64 Ubuntu LTS or Debian stable; record architecture, kernel, RAM, storage,
  GPU/Vulkan driver, and package sources. Keep the existing work-volume and reserve gates.
- `ld-decode` has a Nix-based reproducible build; verify the current flake and each required
  dependency actually evaluate/build for `aarch64-linux`. For Decode-Orc, the current
  2026-11-14 release checked was v2.11.1; its Linux Flatpak asset does not advertise an
  ARM64 variant, and its separately published Linux plugin SDK is named `linux-x86_64`.
  Verify Flatpak architecture and source-build/CLI/GUI support on ARM64; do not assume either
  artifact runs natively. Nix source build is a candidate, not proof of ARM64 support.
- VapourSynth/Python: current VapourSynth docs state Python 3.12+ and Linux wheels, while its
  plugin-packaging guidance lists Linux aarch64 as an important platform. Check actual wheel
  tags for the selected Python/ABI/architecture; build any missing plugins natively. Candidate
  dependencies include Python, VapourSynth, `vspipe`, C/C++ compiler, Meson, Ninja, pkg-config,
  Cython as needed, zimg, and the workflow's Python modules.
- BestSource R23 requires FFmpeg 9.0+ for CPU use; use ARM64 FFmpeg and libx265 packages/builds
  with the required headers/libraries. BestSource GPU support has additional Vulkan 1.4 and
  FFmpeg requirements and is out of the baseline. Current VapourSynth TIVTC source specifies
  C++20/Meson and emits a native `.so`; build using the exact Python/VapourSynth environment.
- RIFE ncnn Vulkan is optional: build for ARM64 and verify a working Vulkan loader/ICD, driver,
  GPU, model files, and output consistency; upstream release recency is limited. CPU fallback
  may be slow and must not be mistaken for GPU validation.

Before calling this target supported, verify architecture for every binary/library (no x86
emulation assumptions), run unit and native-plugin tests, then pass the same end-to-end TBC,
PAD/EFM/audio, encode/remux, metadata, QC, hash, cleanup, and space-gate comparisons required
for Linux x86-64. Keep the target **untested** until run on actual ARM64 hardware.

### 10. Windows ARM64 tooling/dependency target ? research-only, untested

Evaluate Windows 11 on ARM64 as a distinct target from Windows x64. Preserve the V5 Windows
workflow contract unless all dependencies are proven native or a fully emulated x64 toolchain
passes the tests. Windows 11 24H2 can emulate user-mode x86/x64 apps, but kernel drivers must
be ARM64-native; emulation is not proof of performance, compatibility, or GPU support.

Candidate tooling and known gaps (refresh release pages before implementation):

- PowerShell has a native Windows ARM64 release artifact; the official release checked
  2026-11-14 was 7.7.0. Verify the current stable/LTS release and use a native ARM64 `pwsh`
  for script/parser tests where available.
- FFmpeg can be built natively for Windows ARM64 with the MSYS2 CLANGARM64 environment. The
  production build must expose the exact required `libx265` and AviSynth demuxer capabilities.
  FFmpeg explicitly does not intend to support ARM64EC, so do not use ARM64EC builds as a
  presumed shortcut. Record architecture/build flags and verify `ffmpeg -buildconf`, codecs,
  filter/demuxer availability, and actual AviSynth input.
- AviSynth+ upstream lists ARM as a supported CPU family, but not every OS/CPU combination is
  available or tested. The current V5 TFM/TDecimate, L-SMASHSource, FFMS2, RemapFrames, QTGMC,
  and other plugin DLLs may lack native ARM64 builds. Build/obtain a complete matching ARM64
  chain or keep the complete x64 process and its plugins together under Windows x64 emulation;
  never load an x64 DLL into an ARM64 process or mix architectures in-process.
- Decode-Orc v2.11.1 (latest release checked 2026-11-14) publishes a Windows x64 MSI, not a
  Windows ARM64 installer. Determine whether the complete x64 Orc app and plugins work under
  emulation, or build a native ARM64 package from source with its supported toolchain. Do not
  claim native support from successful installation alone.
- Check native ARM64 availability for Python and workflow dependencies (`numpy`, OpenCV,
  Pillow), 7-Zip, RIFE engine/model and Vulkan driver. Keep RIFE optional. Ensure any GPU
  compute/capture dependencies have actual ARM64 drivers; user-mode x64 emulation cannot supply
  missing kernel drivers.

Before accepting this target, test the exact native and/or fully emulated architecture matrix
on real Windows ARM64 hardware: preflight, Orc stages/plugins, AviSynth source and filters,
FFmpeg/libx265, RIFE if selected, Python tests, all existing quality/sync/PAD/delivery gates,
space checks, and representative end-to-end output comparison against Windows x64. Record
which components are native versus emulated and the resulting performance. This target remains
**untested** until the full matrix passes; do not silently change V5's x64 support claim.

### 11. Fresh macOS setup and included VapourSynth toolchain — integrated in V6; end-to-end validation deferred

The M5 update log and `m5-S01E10-audio-results.md` were reviewed and their V5-relevant
macOS ports are included in this V6 candidate: profile example, `preflight_mac.py` fixes,
`build_motion_trace.py`, `build_map_vpy.py` with the measured QTGMC mapping, the
`vspipe_util.py` transport, source-derived `pad_audit.py`/`fullres_check.py`/RIFE neighbor
ports, `build_padfix_vpy.py`, `encode_episode_mac.py`, `remux_padfixed_mac.py`, and the
padfix tests. The tool inventory/setup steps are in `PORTING.md` and
`machine-macos.example.json`; `init_episode.py` emits `.vpy` paths for a `darwin` profile.
The M5 log establishes multiple component/stage passes, but not a completed fresh-Mac V6
final delivery/QC. Run the full test suite, preflight, ListOnly, and an end-to-end episode
on a fresh Apple-silicon machine before claiming that target fully supported.

## Queue state

V6 integrated changes are release content. Remaining space-reserve, shared multi-instance
logging, hardware-concurrency, Linux, and ARM64 items are deferred and are not V6 behavior.
Mac end-to-end delivery and QC on a fresh machine remain an explicit follow-up validation,
not an implied support claim.
