"""Tool/library requirements: a tested version *range* plus capability probes.

Policy (owner decision 2026-11-22):

* ``minimum``      — below it the check FAILS (a feature we rely on is missing).
* ``tested_up_to`` — above it the check WARNS (a newer release may change an API or the pixels;
  the QTGMC option rename that broke the S01E20 smoke run was a *newer* library). ``None`` = no known ceiling.
* ``probe``        — a capability test that does not trust version numbers (plugin loads, encoder and pixel
  format exist, API symbol present). A failed probe FAILS regardless of version.
* ``pinned``       — output-affecting tools (Orc, RIFE model, x265 build): never fail on version, but any
  difference from the version an episode was last built with is reported as drift (see ``compare_versions``).

Actual resolved versions are recorded per run (``record_versions``) so every output is traceable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from fstk.core.fsio import atomic_write_json, read_json
from fstk.core.timeutil import utc_now_iso
from fstk.tools import process


def parse_version(text: str) -> tuple[int, ...] | None:
    """First dotted number in ``text`` ("ffmpeg version n7.1-3-gabc" -> (7, 1)); None if there is none."""
    m = re.search(r"(\d+(?:\.\d+)*)", text or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


@dataclass(frozen=True)
class Requirement:
    name: str
    minimum: str | None = None
    tested_up_to: str | None = None       # compared on the components given: "2" means any 2.x is fine
    pinned: bool = False
    required: bool = True


def judge(req: Requirement, found: str | None) -> tuple[str, str]:
    """(status, why) for a version string against the range. Pure; no I/O."""
    if req.pinned:
        return "PASS", "pinned: reported for drift tracking, not range-checked"
    v = parse_version(found or "")
    if v is None:
        return ("FAIL" if req.required else "WARN"), f"version not readable from {found!r}"
    if req.minimum and _cmp(v, parse_version(req.minimum)) < 0:
        return "FAIL", f"{found} is below the minimum {req.minimum}"
    if req.tested_up_to:
        top = parse_version(req.tested_up_to)
        if _cmp(v[:len(top)], top) > 0:
            return "WARN", f"{found} is newer than the tested range (<= {req.tested_up_to}.x); re-check outputs"
    return "PASS", f"{found} (range {req.minimum or 'any'} .. {req.tested_up_to or 'open'})"


#: Python packages (distribution names). Ranges are what we have run; widen them only after a test.
PACKAGES = (
    Requirement("numpy", "1.26"),
    Requirement("opencv-python", "4.8", required=False),          # either the full or the headless wheel
    Requirement("opencv-python-headless", "4.8", required=False),
    Requirement("Pillow", "10.0"),
    Requirement("soxr", "0.3"),
    Requirement("vsjetpack", "2.2", "2", required=False),         # vsdeinterlace/vstools/vskernels bundle
)
#: Executables whose version we can read.
EXECUTABLES = (
    Requirement("ffmpeg", "4.4"),          # smoke-tested on 4.4.2; production Windows build: no ceiling known
    Requirement("ffprobe", "4.4"),
)
PINNED = (Requirement("orc", pinned=True), Requirement("rife_model", pinned=True),
          Requirement("x265", pinned=True))

VS_NAMESPACES = ("bs", "tivtc", "mv", "znedi3", "eedi3m", "resize2")   # resize2: default bobber of QTempGaussMC


def package_version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def exe_version(path: str | Path, args: list[str] | None = None) -> str | None:
    try:
        res = process.run([str(path), *(args or ["-version"])], capture_stdout=True, timeout=30)
    except Exception:
        return None
    text = (res.stdout or b"").decode("utf-8", errors="replace")
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), None)


# ------------------------------------------------------------------------------ capability probes
def probe_ffmpeg(path: str | Path) -> list[tuple[str, str, str]]:
    """Encoder + pixel format the 4:2:2 chain needs; read from ffmpeg itself."""
    out = []
    for label, args, needle in (("libx265 encoder", ["-hide_banner", "-encoders"], "libx265"),
                                ("yuv422p10le pixel format", ["-hide_banner", "-pix_fmts"], "yuv422p10le")):
        try:
            res = process.run([str(path), *args], capture_stdout=True, timeout=30)
            text = (res.stdout or b"").decode("utf-8", errors="replace")
        except Exception as exc:
            out.append((label, "FAIL", f"could not run ffmpeg ({exc.__class__.__name__})"))
            continue
        out.append((label, "PASS" if needle in text else "FAIL", "available" if needle in text else "missing"))
    return out


def probe_vapoursynth() -> list[tuple[str, str, str]]:
    """Plugins and the QTGMC API, tested by loading them (not by version)."""
    try:
        import vapoursynth as vs
    except Exception as exc:
        return [("vapoursynth", "WARN", f"python module not importable ({exc.__class__.__name__}); plugin probe skipped")]
    have = {p.namespace for p in vs.core.plugins()}
    out = []
    for ns in VS_NAMESPACES:
        # resize2 is only needed by the qtgmc repair; pp2 works without it, so a miss is a WARN.
        status = "PASS" if ns in have else ("WARN" if ns == "resize2" else "FAIL")
        note = "loaded" if ns in have else ("missing — `repair=qtgmc` will fail; `pp2` still works" if ns == "resize2"
                                            else "missing")
        out.append((f"vs plugin {ns}", status, note))
    try:
        from vsdeinterlace import QTempGaussMC
        api = ("SourceMatchMode API" if hasattr(QTempGaussMC, "SourceMatchMode") else "legacy kwargs API")
        out.append(("QTempGaussMC", "PASS", api + " (generated scripts handle both)"))
    except Exception as exc:
        out.append(("QTempGaussMC", "WARN", f"vsdeinterlace not importable ({exc.__class__.__name__}); qtgmc repair unavailable"))
    return out


# ------------------------------------------------------------------------------ the report
def check_requirements(profile=None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        rows.append({"name": name, "status": status, "detail": detail})

    cv_seen = False
    for req in PACKAGES:
        ver = package_version(req.name)
        if ver is None:
            if not req.name.startswith("opencv"):
                add(f"req:{req.name}", "FAIL" if req.required else "WARN", "not installed")
            continue
        cv_seen = cv_seen or req.name.startswith("opencv")
        add(f"req:{req.name}", *judge(req, ver))
    if not cv_seen:
        add("req:opencv", "FAIL", "neither opencv-python nor opencv-python-headless is installed")
    if profile is not None:
        for req in EXECUTABLES:
            try:
                path = profile.tool(req.name)
            except Exception:
                continue                      # the existing tool:* check already reports a missing tool
            add(f"req:{req.name}", *judge(req, exe_version(path)))
        try:
            for label, status, detail in probe_ffmpeg(profile.tool("ffmpeg")):
                add(f"probe:ffmpeg {label}", status, detail)
        except Exception:
            pass
    for label, status, detail in probe_vapoursynth():
        add(f"probe:{label}", status, detail)
    return rows


# ------------------------------------------------------------------------------ per-episode record
def resolve_versions(profile) -> dict[str, Any]:
    """Actual versions in use right now (best effort; unknowns are recorded as null)."""
    v: dict[str, Any] = {"packages": {r.name: package_version(r.name) for r in PACKAGES}}
    tools: dict[str, str | None] = {}
    for name, args in (("ffmpeg", ["-version"]), ("ffprobe", ["-version"]), ("vspipe", ["--version"])):
        try:
            tools[name] = exe_version(profile.tool(name), args)
        except Exception:
            tools[name] = None
    try:
        tools["orc"] = str(profile.tool("orc"))               # no --version; the path identifies the install
    except Exception:
        tools["orc"] = None
    try:
        tools["rife_model"] = Path(str(profile.tool("rife_model_dir"))).name
    except Exception:
        tools["rife_model"] = None
    v["tools"] = tools
    return v


def compare_versions(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    """Differences that can change output. Pinned tools always count; others only when the version moved."""
    if not previous:
        return []
    notes = []
    for group in ("tools", "packages"):
        for k, now in (current.get(group) or {}).items():
            before = (previous.get(group) or {}).get(k)
            if before is not None and now is not None and before != now:
                notes.append(f"{group[:-1]} {k}: {before!r} -> {now!r}")
    return notes


def record_versions(profile, reports_dir: Path) -> list[str]:
    """Write ``tool-versions.json`` (current + history) and return drift notes against the last record."""
    path = reports_dir / "tool-versions.json"
    doc = read_json(path) if path.is_file() else {"history": []}
    now = resolve_versions(profile)
    last = doc["history"][-1]["versions"] if doc.get("history") else None
    drift = compare_versions(last, now)
    if last != now:                                           # only changes extend the history
        doc["history"].append({"at": utc_now_iso(), "versions": now, "drift_from_previous": drift})
    doc["current"] = now
    atomic_write_json(path, doc)
    return drift
