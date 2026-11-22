"""Write this machine's fstk profile from the v38 machine.json, pointing vspipe at this venv.

Run with the venv's Python (setup-fstk.ps1 does):
    python tools\\windows\\make_profile.py --v38 Z:\\ld-pipeline\\machine.json --data-root Z:\\fstk-data ^
        --media-root X:\\fstk-media [--dvd-root Z:\\Video] [--downloads-root X:\\] [--force]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fstk.config.profile import Profile, convert_v38_profile, default_profile_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v38", required=True, help="v38 machine.json (read only)")
    ap.add_argument("--data-root", required=True, help="small durable state (manifests, reports, approvals)")
    ap.add_argument("--media-root", required=True, help="large per-episode media (master, audio, encodes)")
    ap.add_argument("--dvd-root", help="folder holding the DVD rips (searched by `fstk episode dvd --find`)")
    ap.add_argument("--downloads-root", help="folder holding capture folders")
    ap.add_argument("--out", default=str(default_profile_path()))
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    v38 = json.loads(Path(a.v38).read_bytes().decode("utf-8-sig"))
    data, notes = convert_v38_profile(v38, data_root=a.data_root, media_root=a.media_root)
    scripts = Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin")
    vspipe = scripts / ("vspipe.exe" if sys.platform == "win32" else "vspipe")
    if not vspipe.is_file():
        print(f"vspipe not found at {vspipe}: install the media extra first (pip install -e .[gui,media])")
        return 2
    data["tools"]["vspipe"] = str(vspipe)
    for key, val in (("dvd_root", a.dvd_root), ("downloads_root", a.downloads_root)):
        if val:
            data["sources"][key] = val
    notes = [n for n in notes if not n.startswith("tools.vspipe")]
    data["notes"] = [n for n in data["notes"] if "tools.vspipe" not in n]
    data["notes"].append("[setup] vspipe is the fstk venv's own (pip VapourSynth + plugin wheels)")
    out = Path(a.out)
    if out.exists() and not a.force:
        print(f"{out} exists; pass --force to replace it")
        return 1
    for root in (a.data_root, a.media_root):
        Path(root).mkdir(parents=True, exist_ok=True)
    Profile.from_dict(data).save(out)
    print(f"wrote {out}")
    for n in notes:
        print(f"  note: {n}")
    print(json.dumps({k: data[k] for k in ("tools", "sources", "data_root", "media_root")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
