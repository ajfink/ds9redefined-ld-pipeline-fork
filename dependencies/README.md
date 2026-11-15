# V7 dependency version locks

Choose the lock that matches the actual machine architecture and the Python
interpreter configured by its local `machine.json`:

| Target | Python | Lock / environment | Evidence status |
|---|---:|---|---|
| Windows x86_64 | 3.10.14 | `requirements-windows-x86_64.lock.txt` | Direct Python package versions verified in the V7 authoring environment. |
| macOS arm64 | 3.15.2 | `requirements-macos-arm64.lock.txt`, `environment-macos-arm64.yml` | Package versions match M5 component evidence; fresh-Mac V7 delivery/QC is not complete. |
| Linux x86_64 | 3.10.14 | `requirements-linux-x86_64.lock.txt`, `environment-linux-x86_64.yml` | Candidate only; Linux x86_64 is not an end-to-end supported platform. |

These are **direct package version locks**, not hash-complete locks of every
transitive package or conda build string. `bootstrap_deps.py` checks the exact
Python and direct distribution versions and installs only the listed pip
packages with `--no-deps`; it never updates Python, native tools, VapourSynth,
or plugins. The Mac/Linux environment YAMLs pin direct conda dependencies but
still require a recorded `conda-lock` resolution before they can be treated as
bit-for-bit environment locks.

The operator must first perform the PORTING version/update gate. Installation
is an explicit action: `python scripts/bootstrap_deps.py --install` modifies
only the selected profile interpreter's listed pip packages. The default is a
read-only check; `--print` prints the selected command without running it.

Native tools and plugins remain separate because their OS/architecture,
licensing, GPU/driver, and installation requirements differ: Decode-Orc,
FFmpeg/libx265, PowerShell, AviSynth+/DLLs, VapourSynth plugins, RIFE engine
and model, and 7-Zip are **not** installed or silently upgraded by this helper.
