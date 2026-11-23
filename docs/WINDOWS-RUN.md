# First real Windows run — DS9:S01E20 (2026-11-22)

The first full fstk run on the Legion. The aim is a finished episode and the A/V review in the app.
Several parts have only run on Linux so far, so this run's second job is to show where Windows breaks.
**If a step fails, stop there, note what the table at the end asks for, and send it back.** Everything
resumes: running the same plan again reuses every finished step.

Inputs used (all read-only, never modified):

| What | Where |
|---|---|
| 12 captures (TBC + JSON + EFM), Archive-verified | `X:\DS9-S01E20-LaserDisc-Copies` |
| DVD rip | `Z:\Video\DS9S1D5\DS9 S01E20 - In The Hands of the Prophets.mkv` |
| v38 machine profile (tool paths) | `Z:\ld-pipeline\machine.json` |
| v38 ProRes master + project (smoke check only) | `Z:\ld-pipeline-device\prores`, `...\stack` |

**fstk only reads X: and Z:\Video.** Everything it writes goes under your user folder on the Legion's own
disk by default:

| Folder | Holds |
|---|---|
| `C:\Users\<you>\FromStunToKill\work` | working media: the lossless master (≈ 40 GB), audio, scripts, encodes, finished files |
| `C:\Users\<you>\FromStunToKill\data` | manifests, plans, reports, logs, approvals (small) |
| `C:\Users\<you>\FromStunToKill\smoke` | the smoke check's scratch copy |

Plan on about 60 GB free for one episode plus the 80 GB reserve the profile keeps. Setup prints the free
space; if the C: drive is short, choose another local folder (see "Changing the folders" below).

---

## 1. Setup (about 15 min, once)

Open **PowerShell** (not as administrator) and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File Z:\FromStunToKill\tools\windows\setup-fstk.ps1
```

It installs Python 3.12 if missing (winget), creates a venv at `%LOCALAPPDATA%\fstk\venv`, installs fstk with the GUI and VapourSynth plus every plugin as PyPI wheels (bestsource,
TIVTC, MVTools, znedi3, eedi3, resize2, akarin, vszip, vsjetpack), writes the profile to
`%APPDATA%\FromStunToKill\machine.json` from your v38 `machine.json` (tool paths only; the v38 `Z:\work`
folders are not used), prints the free space where fstk will write, and runs `fstk doctor`.

Then, for this session, put fstk on the path:

```powershell
$env:Path = "$env:LOCALAPPDATA\fstk\venv\Scripts;$env:Path"
```

**Doctor must show no FAIL.** WARNs are fine *except* `vs plugin resize2` (needed for QTGMC; it should
install from the wheel here even though it could not on the Linux test machine). Check in particular:
`probe:ffmpeg libx265 encoder PASS`, all `vs plugin …` PASS, `QTempGaussMC PASS`, tools `orc`,
`rife_engine`, `rife_model_dir` found.

## 2. Ten-minute smoke check (recommended before the long run)

Runs cadence → trim → PAD audit → PAD repair → encode on 100 s of the old v38 master, in a sandbox.
It proves VapourSynth, TIVTC, QTGMC, RIFE and x265 all work on Windows before committing hours to Orc.

```powershell
fstk smoke --episode DS9:S01E20 --master "Z:\ld-pipeline-device\prores\S01E20-stack-master.mov" `
  --project "Z:\ld-pipeline-device\stack\stack-full-s01e20.orcprj" --reference P6 --pad-frames 118 `
  --capture P2 --seconds 100
```

Expect five stages `passed` and an HEVC file under `…\FromStunToKill\smoke`. (No `--repair pp2` or
`--rife-standin` here: this is the real QTGMC and real RIFE.) A failure here is the most useful thing
to send back; you can still continue to step 3.

## 3. Create the episode (1 min)

```powershell
fstk episode create --local "X:\DS9-S01E20-LaserDisc-Copies"
fstk episode dvd DS9:S01E20 --find
```

The second command should print the `Z:\Video\DS9S1D5\…S01E20….mkv` file. (In the app:
**Episodes… → From local folder…**; the DVD step is command-line only for now.)

## 4. Intake (Orc reference loop + EFM decode of 12 captures; probably 1–3 h)

```powershell
fstk plan intake DS9:S01E20 --no-download
fstk run <the plan path it printed>
```

Steps: capture-inventory → stack-reference (Orc refresh loop) ∥ efm-decode (12 captures) → efm-select.
No review is expected: EFM choice is automatic (least audible damage). `--no-download` skips the Archive
step; the captures were verified when they were downloaded. (The app's **Run intake** includes the
download step, which now accepts the existing verified download manifest instead of re-hashing ~1 TB.)

## 5. Delivery (long: Orc render, then the final x265 `slow` encode; let it run overnight)

```powershell
fstk plan delivery DS9:S01E20
fstk run <the plan path it printed>
```

Order: stack-render (Orc → **lossless FFV1 master**, ~40 GB) → dvd-probe → dvd-alignment → cadence →
video-trim → DVD-referenced LD audio → 5.1/subtitle retime → stretch-preview → pad-audit → pad-repair →
frame-fixes (none yet) → video-quality → encode-final → audio-quality → remux → qc-report →
episode-report. It stops only for **the A/V check** (and for the cadence review if this episode's
cadence falls outside the v38-calibrated ranges).

You can also do steps 4–5 from the app (`fstk-gui`): **Run intake**, then **Run to delivery**.

## 6. Review (in the app)

```powershell
fstk-gui
```

Open the episode → **Delivery review…**:
- **A/V check**: play each 15 s chunk, mark in sync / audio early / audio late. All in sync → **Approve**.
- **Episode quality report** tab: the verdict and the contact sheet of the worst moments.
- **Video-origin stretches** tab (if any): left = nearest real frames (default), right = interpolated.
- **Frame fixes** tab: optional; double-click a worst moment, choose an action, **Preview selected**, then
  **Show latest preview** when the job finishes. Applying fixes or stretch choices re-encodes from the master
  (a new `-r2` file; the first encode is kept).

The finished file is in `…\FromStunToKill\work\ds9-s01e20\out\` (or your finished-files folder), with
`<name>.quality.md` beside it.

---

## If something fails

Check where a job stands: `fstk status DS9:S01E20 <job>` (the job name is the plan file name).
Send back:

1. the failing **stage** and its **issues** (from `fstk status`, or the app's job panel),
2. the stage's **log** from `…\FromStunToKill\data\episodes\DS9\S01E20\logs\` (newest files),
3. `fstk doctor` output if it is a tool or plugin problem.

Known risks and what each looks like:

| Risk | Symptom | What to do |
|---|---|---|
| Orc build does not accept `ffmpeg_format: mkv-ffv1` | stack-render: "Orc produced no stack master … rejects mkv-ffv1" | Send the Orc log. Do **not** fall back to ProRes. |
| TIVTC drops the `FstkN` frame tag on Windows | video-trim: "FstkN frame property lost" | Send the log; the trim probe needs another tagging method. |
| QTGMC / resize2 | video-trim or smoke: "No attribute … resize2" | Re-run setup; or plan the trim with `repair=pp2` (edit the plan step params) and tell me. |
| RIFE (Vulkan) | pad-repair: RIFE output missing/wrong size | Send the log; check `rife_engine` and `rife_model_dir` in the profile. |
| Disk space | a step "blocked" on space | Free space on that drive, move the working folder (below), or lower `limits.min_free_gb` (80 GB now). |
| Cadence outside v38 ranges | job stops at `cadence-review` | Approve in the app's review panel after looking at the evidence, then run the plan again. |

## Changing the folders

Before the first episode, or any time with nothing in progress:

```powershell
fstk profile folders                                   # show them
fstk profile folders --media-root D:\fstk-work         # move the working folder
fstk profile folders --root out=E:\Deliveries          # finished files somewhere else (out= alone resets)
fstk profile folders --data-root D:\fstk-data
```

or **Folders…** in the app. Setup takes the same choices: `setup-fstk.ps1 -MediaRoot D:\fstk-work -OutRoot E:\Deliveries`.
fstk refuses to move a folder that already holds episode work (those episodes would no longer be found) unless
you move the work yourself or confirm.

Nothing in this run modifies the captures, the DVD, the v38 tree, or the v38 master.
