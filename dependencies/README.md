# V7 dependency version locks

Choose the lock that matches the actual machine architecture and the Python
interpreter configured by its local `machine.json`:

| Target | Reference Python | Lock / environment | Evidence status |
|---|---:|---|---|
| Windows x86_64 | 3.10.14 | `requirements-windows-x86_64.lock.txt` | Direct Python package versions verified in the V7 authoring environment. |
| macOS arm64 | 3.15.2 | `requirements-macos-arm64.lock.txt`, `environment-macos-arm64.yml` | M5 completed S01E10 end to end through final remux/seek repair; fresh-device installation is not independently certified. |
| Linux x86_64 | 3.10.14 | `requirements-linux-x86_64.lock.txt`, `environment-linux-x86_64.yml` | Candidate only; Linux x86_64 is not an end-to-end supported platform. |

These are **reference direct-package pins**, not hash-complete locks of every
transitive package or conda build string. Doctor/preflight checks that the
selected Python runtime and required modules are installed and importable; a
different Python or package version is reported for information and is not a
readiness blocker. Missing or broken required modules remain blockers.
`bootstrap_deps.py --install` can still install the direct packages listed in
the platform reference file. The Mac/Linux environment YAMLs pin direct conda
dependencies but still require a recorded `conda-lock` resolution before they
can be treated as bit-for-bit environment locks.

Installation is always explicit. The desktop app's **Dependency update**
dialog checks the listed Python packages against PyPI and supported managed
native tools against WinGet/Homebrew. It shows local/newest versions and only
installs individually selected updates. `python scripts/bootstrap_deps.py
--install` remains available for the platform reference pins; its default is
read-only and `--print` only prints the command.

Native tools and plugins remain separate because their OS/architecture,
licensing, GPU/driver, and installation requirements differ. The GUI updater
only offers native updates when it can match the configured executable to the
supported package manager; Decode-Orc, AviSynth+/DLLs, VapourSynth plugins,
and RIFE engine/models remain vendor-managed and are never guessed or replaced.
