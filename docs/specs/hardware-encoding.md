# Spec: hardware-accelerated encoding as an option (NVENC and others)

Status: **approved to build, not implemented** (owner request and decisions 2026-11-25; see §0). No open questions remain. Builds on the encoder registry in `src/fstk/config/output.py` (`VIDEO_ENCODERS`, `video_args`), the final encode stage (`stages/encode.py`, `media/encode.final_encode_args`, `check_encoded_stream`) and the Settings › Video page (`ui/settings.py`, `VideoPage`).

## 0. Owner decisions (2026-11-25)

- **Build every encoder in §3**, not a first batch. Which ones a machine offers is decided by detection (§4.2), not by the code.
- **No benchmark before building.** Build it out; test and benchmark on the real machines afterwards (§4.1 becomes a post-build check).
- **The lossless intermediate becomes a visible setting in Settings › General** (§7), with its extra storage stated plainly.
- **A hardware encode may be an episode's only delivered copy.** Whether it is, or an x265 copy is also made, is a setting, global and per episode (§8).
- **The grain-retention check warns; it never blocks delivery** (§4.5).

## 1. What this adds, and what it does not

- New choices in **Settings › Video › Video encoder**, defaults and per episode, for the **final encode only** (`encode-final`, and so re-encodes and the queue's "re-encode").
- Nothing upstream changes. The stack master, IVTC, finishing script, frame fixes and QC are the same.
- **Never the default.** x265 10-bit 4:2:2 CRF 14 slow/grain stays the recommended setting. A hardware encoder is a person's explicit choice, flagged everywhere it shows.

## 2. The warning (required, very visible)

Proposed text, used everywhere a hardware encoder is shown:

> ⚠ **Hardware encoders smooth away film grain.** NVENC, Quick Sync, AMF and VideoToolbox are built for speed. Their rate control and in-loop filtering treat LaserDisc's natural grain as noise and flatten it, at any quality setting. That grain is what makes these transfers look like film. Use a hardware encoder only for a quick preview or a viewing copy, never for the copy you keep. For the archival copy use x265 (Recommended), or FFV1 lossless.

Where it appears:

| Place | How |
|---|---|
| Settings › Video, encoder list | Hardware entries sit in their own group, "Hardware (fast, smooths grain)", below a separator. Each label ends in "— smooths grain". |
| Settings › Video, page body | While a hardware encoder is selected, a full-width red banner with the text above sits **above** the quality controls. It can't be closed and is never hidden behind "Advanced". |
| First selection | A confirmation dialog with the same text, buttons **Use it anyway** / **Keep x265** (default). It is asked again after the defaults change, and per episode. |
| "Result:" line (Settings) | Appends "· HARDWARE — grain is smoothed". |
| Episode header flag (⚑ own settings) | "⚑ Own output settings — hardware encoder (smooths grain)", in red. |
| Run / queue | When a delivery or re-encode is started with a hardware encoder, the banner repeats the warning with **Switch to x265** as a button. |
| Encode step | Its tooltip, plan summary and the encode report say "hardware encoder: grain smoothed". |
| Deliverable | Track title metadata `LaserDisc …, hardware encode (NVENC)`. The episode report and QC report carry the warning, so the file explains itself later. |

## 3. The suite of hardware options

"4:2:2" matters because the app's default chroma is 4:2:2, and most hardware can only do 4:2:0. All of these are built (owner, §0); the encoder list shows only those whose probe passed on this machine (§4.2). `nvenc-h264-8` and `vaapi-hevc-10` are listed last.

| Id (proposed) | FFmpeg encoder | Bit depth / chroma | Hardware | Quality control | Notes |
|---|---|---|---|---|---|
| `nvenc-hevc-10` | `hevc_nvenc` | 10-bit 4:2:0 on all RTX; **10-bit 4:2:2 on Blackwell (RTX 50) only** | NVIDIA Turing and newer (4:2:2: Blackwell) | `-rc vbr -cq N` (constant quality), `-preset p7`, `-tune hq` (or `uhq` on Ada/Blackwell), `-multipass fullres`, `-rc-lookahead 32`, `-b_ref_mode middle` | Best of the hardware options. Turn spatial/temporal AQ **off** by default (it moves bits away from flat grainy areas). 4:2:2 needs an FFmpeg with the Blackwell support (8.0+) and a matching driver. |
| `nvenc-av1-10` | `av1_nvenc` | 10-bit 4:2:0 | NVIDIA Ada (RTX 40) and Blackwell | `-rc vbr -cq N`, `-preset p7`, `-tune hq` | Small files; there is no film-grain synthesis on NVENC, so grain is simply lost. |
| `nvenc-h264-8` | `h264_nvenc` | 8-bit 4:2:0 (10-bit and 4:2:2 on Blackwell only) | NVIDIA | `-rc vbr -cq N` | Compatibility copies only. 8-bit bands in gradients. Listed last. |
| `qsv-hevc-10` | `hevc_qsv` | 10-bit 4:2:0; 10-bit 4:2:2 (Y210) on Ice Lake+ and Arc (Windows; 4K max on some) | Intel iGPU 10th gen+, Arc | `-global_quality N` (ICQ), `-preset veryslow`, `-look_ahead 1` | Needs `-init_hw_device qsv` and a `format=p010`/`y210` upload. |
| `qsv-av1-10` | `av1_qsv` | 10-bit 4:2:0 | Arc, Core Ultra | `-global_quality N`, `-preset veryslow` | |
| `amf-hevc-10` | `hevc_amf` | 10-bit 4:2:0 (no 4:2:2) | AMD RDNA (VCN) | `-rc cqp -qp_i N -qp_p N -qp_b N`, `-quality quality` | Known FFmpeg issue: 10-bit output tagged BT.2020 regardless of input (fixed upstream). The encode check must verify the colour tags (see §4.5). |
| `amf-av1-10` | `av1_amf` | 10-bit 4:2:0 | AMD RDNA3+ | as above | Same colour-tag check. |
| `vt-hevc-10` | `hevc_videotoolbox` | 10-bit 4:2:0; 10-bit 4:2:2 (`main42210`, `p210`) on Apple silicon with a 2025+ FFmpeg | Apple silicon (the Mac) | `-q:v N` (0–100, Apple silicon only); `-b:v` otherwise | No CRF. `-q:v` has its own scale; calibrate it (§4.3). |
| `vt-prores-hq` | `prores_videotoolbox` (profile HQ) | 10-bit 4:2:2 | Apple silicon | profile only (HQ ≈ 220 Mb/s at 1080p, far less at 480p) | **The one hardware option that keeps grain well**: intra-only, very high bitrate, built for editing. Large files. A good "fast mezzanine" on the Mac. The warning still applies, in a milder form ("much larger files; grain largely kept"). |
| `vaapi-hevc-10` | `hevc_vaapi` | 10-bit 4:2:0 (4:2:2 on Intel) | Linux Intel/AMD | `-qp N` / `-global_quality` | Listed last (the owner's machines are Windows + Mac). |

## 4. Considerations

### 4.1 Will it actually be faster? Measure after building
The S01E20 encode on PC-A ran at **11.48 fps for 95 minutes**, with vspipe and x265 in one pipe. In a pipe the slower side sets the pace, so we don't yet know whether x265 or the VapourSynth finishing chain (IVTC, QTGMC repairs, concealment, chroma denoise) is the bottleneck. video-quality, which also runs that chain, took about 4 hours on its own, which suggests the chain is heavy.

**After the build (owner: no benchmark first):** time `vspipe finish.vpy → null` over about 3,000 frames on each machine, against the full encode. If vspipe alone runs near 11–12 fps, a hardware encoder reading straight from vspipe saves almost nothing; it pays off together with the lossless intermediate (§7), which makes every encode, software or hardware, read a file. `fstk encode-bench` (§5) is the tool for this.

### 4.2 Which hardware this machine has
- `ffmpeg -encoders` only says what the build contains, not what the GPU can do. The Gyan "full" build on PC-A lists every one of these encoders whether or not the hardware exists.
- Detect by a **probe encode**: a 10-frame `testsrc2` at the episode raster in each needed pixel format (`yuv420p10le`, `yuv422p10le`), per encoder.
- Cache the result in the machine profile with the GPU name, driver version and FFmpeg version. Probe again when any of those change, or when the person presses **Detect hardware encoders** in Settings › Tools.
- The encoder list shows only what passed. A failed probe shows why (no NVIDIA GPU, driver too old, 4:2:2 needs Blackwell, and so on).

### 4.3 Quality settings don't translate
- There is no CRF: NVENC uses CQ (0–51), QSV uses ICQ (1–51), AMF uses QP, VideoToolbox uses `-q:v` (0–100). The same number means different things.
- The slider is relabelled per encoder ("CQ", "ICQ", "Quality 0–100"), with its own range and default.
- Defaults are calibrated once on a reference clip against x265 CRF 14: a grainy LaserDisc stretch, measured with the existing grain-retention metric (`grain_retention`) and VMAF. Until calibrated, defaults lean toward more bits.
- **To keep grain, hardware needs much lower CQ (more bits) than people expect.** The default should favour grain, and the size estimate (`bytes_per_frame`) should say so.

### 4.4 Chroma
- The app's default is 4:2:2, and the per-episode decision `mode.chroma_output` may say 4:2:2. Most hardware is 4:2:0 only.
- Never convert silently: if the chosen hardware encoder can't do the episode's chroma, Settings shows the chroma choice greyed out with why. The run refuses before encoding ("NVENC on this GPU encodes 4:2:0 only; this episode's output is 4:2:2").
- The person can change the episode's chroma or pick another encoder.

### 4.5 Checks that must keep passing
`check_encoded_stream` already checks codec, profile, pixel format, rate, frame count and a full decode. Each hardware encoder needs its own `probe` table (profile names differ, e.g. NVENC HEVC 4:2:2 reports `Rext`), and two additions:
- **Colour tags:** `color_primaries`, `color_transfer` and `color_space` must read `smpte170m`, and `color_range` `tv`. This catches the AMF BT.2020 issue and any driver that drops tags.
- **Grain retention (new QC check, WARN only, never blocks; owner decision):** grain of encoded frames against the master at the QC sample points, reusing `quality.grain`. Below a threshold it says "the encode removed N % of the grain". This applies to every encoder, not only hardware, so the warning is measured, not just stated.

### 4.6 Repeatability
- Hardware output changes with GPU model and driver version. A re-run is not bit-identical, unlike x265 with a fixed version.
- Record GPU, driver and FFmpeg version in `deliverable.video_encoding` and the encode report. The "keep later steps when the result is identical" logic (ADR 0005) then simply reruns remux and QC when the encode changed, which is correct.

### 4.7 No silent fallback
If the GPU isn't available at run time (laptop on battery with the dGPU off, driver updated, another app holding all NVENC sessions), the encode **fails closed** with the reason. It never falls back to software without saying so. The banner offers **Switch to x265** and **Retry**.

### 4.8 Scheduling
- The engine already has a `gpu` resource (`limits.resources.gpu = 1` on PC-A). A hardware `encode-final` should take the `gpu` slot instead of a `compute` slot, so a CPU-heavy step can run beside it.
- Consumer NVIDIA cards allow a limited number of simultaneous NVENC sessions; one encode at a time is well within that.

### 4.9 Laptops (PC-A and PC-B)
- On hybrid-graphics laptops NVENC needs the discrete GPU awake. The probe (§4.2) records which adapter it used.
- QSV on the iGPU and NVENC on the dGPU can both exist on one laptop; the list shows both.

### 4.10 Input path
- vspipe (or the lossless intermediate, §7) sends 10-bit 4:2:2 or 4:2:0. NVENC and VideoToolbox take `yuv420p10le` / `p010` with an automatic software conversion; that's cheap at 720×480.
- QSV and VA-API need an explicit `-init_hw_device … -filter_hw_device … -vf format=p010,hwupload`. Build this per encoder in `video_args`.
- Two-pass: hardware encoders do their own multipass internally (`-multipass fullres`), so the app's two-pass bitrate mode isn't offered for them.

### 4.11 Licensing and builds
No new dependency: the Gyan full build (PC-A, PC-B) includes NVENC, QSV and AMF, and Homebrew FFmpeg on the Mac includes VideoToolbox. Settings › Tools shows the FFmpeg version, since NVENC 4:2:2 and VideoToolbox 4:2:2 need a 2025+ build.

## 5. Implementation plan

1. **Lossless intermediate (§7).** Its own step, the Settings › General control, the storage estimate and space gate, and cleanup.
2. **Registry.** Add `VideoEncoder.hardware: str | None` (vendor), `quality_kind` (CQ/ICQ/QP/q:v), `grain_warning: str`, per-encoder `probe` tables, and the `video_args` branches for every encoder in §3, with their fixed grain-friendly flags (AQ off, high-quality preset).
3. **Detection.** Probe encodes, cache in the profile, and a Settings › Tools button.
4. **UI.** Encoder grouping, the warning banner, the confirmation dialog, relabelled slider, greyed chroma with reason, ⚑ flag in red, run banner with **Switch to x265**.
5. **Checks.** Colour-tag checks in `check_encoded_stream`; the grain-retention QC gate (all encoders).
6. **Scheduling.** `gpu` resource for hardware encodes.
7. **Tests.** Argument building per encoder (no GPU needed); probe parsing; warning visibility (GUI tests: banner shown and not closable, dialog default is "Keep x265"); chroma refusal; fail-closed when the probe says no; colour-tag gate on a crafted bad stream. Real-hardware smoke runs on PC-A, PC-B and the Mac, recorded in DEVLOG.
8. **Benchmark tool.** `fstk encode-bench EPISODE`: vspipe→null, vspipe→x265, intermediate→each available encoder, over N frames. Run after the build (§4.1).
9. **Hardware-only delivery setting (§8).**
10. **ADR 0008** "Hardware encoders are opt-in, flagged, measured", and **ADR 0009** "Finished chain rendered once to a lossless intermediate (optional)".

## 6. Open questions for the owner
1. ~~Which GPUs are in PC-A, PC-B and the Mac?~~ Resolved: build all encoders; detection decides per machine.
2. ~~May a hardware encode be the only deliverable?~~ Resolved: yes, as a global and per-episode setting (§8).
3. ~~Grain gate: warn or block?~~ Resolved: warn, never block.

## 7. Lossless intermediate: a setting in Settings › General

Owner: make the saved open decision (render the finished chain once to a lossless file) a visible setting in **Settings › General**, clear about the storage it costs.

### What it does
- A new step, `finish-render`, runs the finishing script (IVTC, repairs, concealment, frame fixes, padfix) once through vspipe into an **FFV1 10-bit** file at the episode's output chroma (4:2:2 or 4:2:0), in the episode's work folder.
- `video-quality`, `encode-final` and any re-encode then read that file instead of running the chain again. A re-encode with different settings (software or hardware) no longer repeats the slow chain.
- It is invalidated like any other step: when the finishing script, frame fixes or padfix change (their fingerprints are its inputs), it is rebuilt, and ADR 0005 keeps later steps whose results don't change.
- Frame count and a full decode are checked before later steps may use it (fail closed).

### The control
In Settings › General, its own group, **not** under Advanced:

> ☐ **Render the finished picture once to a lossless file (faster re-encodes)**
> The picture chain (IVTC, repairs, frame fixes) runs once, and every encode reads the saved result. Re-encoding with new settings, or trying a hardware encoder, then takes minutes instead of hours.
> **Needs about 33 GB extra per episode** while the episode is being worked on, on the drive holding the episode's work folder (*W: — N GB free*, shown live).
> After the final output is approved: ◉ Delete it (recommended)  ○ Keep it (for later re-encodes; the space stays used)

- The size is computed, not fixed text: frames × the FFV1 bytes-per-frame figure (520 KB/frame in `config/output.py` today, so about 33 GB for a 44-minute episode at 23.976 fps). It is replaced by a **measured** figure once one intermediate has been written on that machine. It is shown for the selected episode and, in the Queue, as a total for queued episodes.
- The free space shown is the real free space of the work folder's drive. If an episode can't fit, the existing space gate stops `finish-render` before it starts, with how much is needed; the run banner offers **Turn it off for this episode**.
- Per-episode override, like the output settings (⚑ flag).
- Default: **off** until it has been benchmarked (§4.1), so nothing changes for existing runs.
- "Delete it" reuses CP71's cleanup on approval; "Keep it" records the file in the manifest so cleanup leaves it alone, and the Details tab lists it with its size.
- Hardware encoders do not require it, but Settings › Video says next to the warning: "Hardware encoders are fastest with *Render the finished picture once* turned on (Settings › General)."

### Tests
Setting round-trip and per-episode override; size estimate arithmetic and the measured-figure replacement; space gate refusal; plan wiring (encode and video-quality read the intermediate when on, the chain when off); invalidation when frame fixes change; cleanup on approval honours Delete/Keep; GUI: the group is on the General page, visible without Advanced, and shows the storage line.

## 8. Hardware encode as the only copy: a setting

Owner: a hardware encode may be an episode's only delivered copy, chosen by a setting that exists globally and per episode.

- In **Settings › Video**, shown only while a hardware encoder is selected, directly under the warning banner:

  > When a hardware encoder is used:
  > ◉ **Also make an x265 copy afterwards** — the hardware encode is delivered first as a viewing copy; the x265 encode follows (queued, can run overnight) and becomes the archival deliverable.
  > ○ **Deliver the hardware encode only** — no x265 copy. The grain warning applies to the only copy you keep.

- Default: **Also make an x265 copy afterwards**, which follows the warning's advice. The person can change it globally or for one episode (⚑ flag, like other output settings).
- "Deliver the hardware encode only" asks once for confirmation with the warning text (buttons **Deliver hardware only** / **Also make x265**, default the latter), and the episode's ⚑ flag reads "hardware encode only (smooths grain)".
- With "Also make an x265 copy", the plan runs `encode-final` with the hardware encoder, remux and QC, then a second `encode-final` (x265, the episode's x265 settings), remux and QC. Both deliverables are recorded in the manifest; the x265 one is the final output once approved, and the hardware copy is kept as `… (viewing copy).mkv` unless the person removes it. The x265 encode takes a `compute` slot, so it can be queued behind other work.
- The grain-retention check (§4.5) runs on both and only warns. On a hardware-only delivery its warning is repeated in the approval dialog, without blocking approval.
- Tests: setting round-trip and per-episode override; the plan has one or two encodes accordingly; confirmation dialog default; manifest records both deliverables and which is final; approval is not blocked by a grain warning.
