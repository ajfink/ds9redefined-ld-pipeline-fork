# Installing and starting FromStunToKill

The app folder is **unzipped once** (or sits on a shared folder that several computers use). On each computer you
start the launcher for that system; it checks what is there, asks before installing anything, and starts the app.
The folder itself is never written to, so one copy can serve Windows, Mac and Linux at once.

| System | Start this (double-click, or run from a terminal) |
|---|---|
| Windows 10/11 | `FromStunToKill.cmd` |
| Apple Silicon Mac (macOS 15 or newer) | `FromStunToKill.command` |
| Linux | `./FromStunToKill.sh` |

Extra words after the launcher name: `setup` (run the setup wizard again), `check` (read-only readiness report),
`cli ...` (an `fstk` command line), `env` (where this computer keeps its files), `reset-env` (delete this
computer's Python environment; it is rebuilt on the next start).

## What happens on a new computer

1. **Python.** The launcher looks for Python 3.12–3.15. If there is none it offers to download a private copy
   (python-build-standalone, sha256-verified, ~40 MB) into this computer's own folder. No administrator rights.
2. **Environment.** It creates a per-computer Python environment (one time, a few minutes) with the window toolkit,
   VapourSynth and the plugin set. Versions are pinned in `vendor/vapoursynth-plugins/LOCK.json`; on an Apple
   Silicon Mac the verified wheels in `vendor/` are installed first.
3. **Setup wizard** (only when the computer has no profile yet, or on `setup`). It finds FFmpeg, FFprobe, VapourSynth,
   Decode-Orc, RIFE, 7-Zip and curl; offers to install FFmpeg and 7-Zip through the system package manager
   (winget / Homebrew / apt / pacman) and shows the exact command first; asks for the data and media folders
   (checking free space), and optionally the downloads, DVD and shared folders. It then runs the readiness check
   and lists anything still failing.
4. **The app starts.**

Where things live on each computer (never inside the app folder):

| | Windows | macOS | Linux |
|---|---|---|---|
| Environment and private Python | `%LOCALAPPDATA%\fstk` | `~/Library/Application Support/FromStunToKill` | `~/.local/share/fstk` |
| Machine profile | `%APPDATA%\FromStunToKill\machine.json` | `~/Library/Application Support/FromStunToKill/machine.json` | `~/.config/FromStunToKill/machine.json` |

### What setup can install, and what it cannot

| Tool | Windows | macOS | Linux |
|---|---|---|---|
| FFmpeg + FFprobe (needs libx265) | `winget install Gyan.FFmpeg` | `brew install ffmpeg` | apt / pacman (Fedora: RPM Fusion, instructions only) |
| 7-Zip (unpacks the .7z EFM archives) | `winget install 7zip.7zip` | `brew install sevenzip` (the program is `7zz`) | p7zip |
| VapourSynth + plugins | in fstk's own environment (pinned wheels) | same | same |
| Decode-Orc, RIFE | never automatic — see below | never automatic | never automatic |

Setup offers each install and shows the exact command first; nothing is installed without a yes. It looks for
programs on PATH **and** in the places a double-clicked launcher cannot see — on macOS `/opt/homebrew/bin`,
`/usr/local/bin`, `/opt/local/bin`, `~/.local/bin` — because a process started from Finder or the Dock inherits
the system PATH, not your shell's. Homebrew is found the same way, so the install offer appears even when `brew`
is not on the launcher's PATH. If a tool is still missing, run `FromStunToKill.command setup` again after
installing it by hand; `check` re-reports without changing anything.

If you keep tools in the shared folder (Settings → Folders → shared folder), setup also looks in
`<shared folder>/tools`, so one copy of the RIFE engine and model can serve every machine. A file there is used
even without the executable bit, which a Windows share often drops.

## What is never installed for you

* **Decode-Orc** and **RIFE** change the output when their version changes, so the wizard only tells you where to
  get the pinned release (Orc 2.11.1 on the owner's machines) and then asks for the path. Windows: MSI. macOS: DMG,
  self-signed, after copying `orc-gui.app` to Applications run `xattr -dr com.apple.quarantine /Applications/orc-gui.app`
  (or right-click → Open once). Linux: the Flatpak `io.github.simoninns.decode-orc`; the wizard can create an
  `orc-cli` wrapper for it.
* Fedora/RHEL: FFmpeg with x265 needs RPM Fusion, so the wizard gives instructions instead of installing.
* **RIFE on macOS and Linux.** Only needed to repair single-frame PAD blanks. Unpack the release to
  `~/tools/rife-ncnn-vulkan` (the macOS build runs on Metal through MoltenVK; Linux needs a Vulkan driver) and put
  **the same model folder the other machines use** beside it — `rife-v4.26` on the owner's Windows machines — or in
  the shared folder's `tools` directory. A different model repairs frames differently, which is why setup never
  picks one for you.

## Using one folder from several computers

Put the app folder (e.g. `W:\FromStunToKill`) where every computer can read it. Each computer keeps its own
environment and profile (table above), so nothing collides. Work folders for an episode can also be shared:
see `docs/adr/0002-*` and Settings → Folders (shared folder and volumes). A second computer must see captures and
the work folders at paths its own profile knows; the wizard asks.

Notes:

* macOS needs **15 or newer**: the plugin wheels (BestSource, TIVTC, MVTools, znedi3) are tagged `macosx_15_0`.
* If Finder will not open `FromStunToKill.command` (a share can drop the executable flag), run
  `sh FromStunToKill.command` in Terminal. The first time macOS may ask you to right-click → Open.
* Linux needs the Qt runtime libraries; if the window cannot start the launcher prints the missing package
  (Debian/Ubuntu: `sudo apt install libxcb-cursor0 libegl1 libgl1 libxkbcommon-x11-0 libdbus-1-3 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0`)
  and `python3-venv` if the `venv` module is split out.
* Windows may show a SmartScreen prompt for a launcher run from a network location: choose "Run anyway".
* Retire `tools\windows\setup-fstk.ps1` / `tools\windows\FromStunToKill.cmd` (the old Windows-only scripts): this
  launcher does the same and more.
