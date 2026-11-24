# VapourSynth plugin set (pinned) and macOS arm64 wheels

FromStunToKill runs VapourSynth through `vspipe` subprocesses. The plugins it uses are published on PyPI as wheels by
their own upstream projects; this folder pins the exact tested set and carries the **macOS Apple Silicon (arm64)**
wheels so a Mac installs the same bits that were verified, from a checkout of this repository, with their hashes
checked first.

**Nothing here was compiled, patched or repackaged by FromStunToKill.** Each `.whl` is the unmodified file
downloaded from PyPI on 2026-11-23; `LOCK.json` records its sha256 (equal to the digest PyPI publishes), its
upstream repository and its licence. Searching GitHub first (2026-11-23) showed the work already exists upstream:
the community macOS build repositories are archived in favour of these PyPI wheels, and the plugin authors publish
macOS arm64 builds from their own GitHub Actions. A source build here would be less canonical and could differ from
the Windows/Linux builds (TIVTC in particular decides field matching, so it must be the same code everywhere).

| Package | Version | Used for | Upstream | Licence |
|---|---|---|---|---|
| vapoursynth | 82 | the engine (`vspipe`, API 4) | https://github.com/vapoursynth/vapoursynth | LGPL-2.1+ |
| vapoursynth-bestsource | 23 | frame-exact source filter (`bs`) | https://github.com/vapoursynth/bestsource | MIT |
| vapoursynth-tivtc | 4 | TFM / TDecimate field matching + IVTC (`tivtc`) | https://github.com/dubhatervapoursynth/vapoursynth-tivtc | GPL-2.0 |
| vapoursynth-mvtools | 29 | motion analysis (`mv`) | https://github.com/dubhatervapoursynth/vapoursynth-mvtools | GPL-2.0 |
| vapoursynth-mvutensils | 9 | motion analysis for QTempGaussMC in vsjetpack 2.2 (`mvu`) | https://github.com/myrsloik/mvutensils | GPL-2.0 |
| vapoursynth-zsmooth | 0.20.0 | median/repair filters QTempGaussMC calls through vsrgtools (`zsmooth`) | https://github.com/adworacz/zsmooth | MIT |
| vapoursynth-znedi3 | 3.3 | NNEDI3 interpolation | https://github.com/sekrit-twc/znedi3 | GPL-2.0 |
| vapoursynth-eedi3 | 10.0 | EEDI3 interpolation | https://github.com/HomeOfVapourSynthEvolution/VapourSynth-EEDI3 | GPL-2.0+ |
| vapoursynth-resize2 | 0.5.1 | resizer | https://github.com/Jaded-Encoding-Thaumaturgy/vapoursynth-resize2 | LGPL-2.1 |
| vapoursynth-akarin | 1.5.0 | expression engine | https://github.com/Jaded-Encoding-Thaumaturgy/akarin-vapoursynth-plugin | LGPL-3.0 |
| vapoursynth-vszip | 24.0.0 | image metrics / filters | https://github.com/dnjulek/vapoursynth-zip | MIT |
| vsjetpack | 2.2.7 | QTempGaussMC and helpers (pure Python) | https://github.com/Jaded-Encoding-Thaumaturgy/vs-jetpack | MIT |

## How it is used

* `pyproject.toml` (`media` extra) names the packages; the launcher (`tools/fstk_launch.py`) pins each to the version
  in `LOCK.json` on **every** platform, so Windows, Linux and macOS run the same versions.
* Windows and Linux install the pinned versions from PyPI.
* macOS arm64 first verifies each vendored wheel against `LOCK.json`, installs them from this folder, then installs
  the remaining (pure-Python) dependencies from PyPI. A missing or altered wheel is refused and PyPI is used instead.
* The macOS wheels need **macOS 15 or newer** (BestSource, TIVTC, MVTools and znedi3 are tagged `macosx_15_0`).

## Updating the set

1. Choose new versions, check that every package has the wheels for Windows x86-64, Linux x86-64 and macOS arm64.
2. Download the macOS arm64 wheels from PyPI into `macos-arm64/`, replace the old ones, update `LOCK.json`
   (version, file, sha256 from the PyPI JSON `digests`, size, upstream).
3. Run the unit tests (`tests/unit/test_install.py` re-checks every hash) and a full episode on each platform before
   adopting the new set: a plugin version change can change output.
