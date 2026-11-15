#!/usr/bin/env python3
"""Read-only macOS capability check for the Windows/AviSynth pipeline port.

This checks the profile-resolved VapourSynth/FFmpeg/Orc path, required workflow
scripts, and local roots. It does not install software, download inputs, create
pipeline outputs, or claim that the end-to-end Mac workflow has been validated.

  python scripts/preflight_mac.py
  python scripts/preflight_mac.py --profile /path/to/machine.json
  python scripts/preflight_mac.py --episode S01E10
  python scripts/preflight_mac.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class Results:
    def __init__(self, quiet: bool = False) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.quiet = quiet

    def add(self, name: str, status: str, detail: str) -> None:
        self.rows.append((name, status, detail))
        if not self.quiet:
            print(f"{status:4s} {name}: {detail}")

    @property
    def failures(self) -> int:
        return sum(status == "FAIL" for _, status, _ in self.rows)

    @property
    def warnings(self) -> int:
        return sum(status == "WARN" for _, status, _ in self.rows)

    @property
    def passes(self) -> int:
        return sum(status == "PASS" for _, status, _ in self.rows)


def resolve_path(value: object) -> Path | None:
    if value is None or not str(value).strip():
        return None
    raw = os.path.expandvars(os.path.expanduser(str(value)))
    if os.path.sep not in raw and not Path(raw).is_absolute():
        found = shutil.which(raw)
        if found:
            raw = found
    return Path(raw)


def run_text(command: list[str], timeout: int = 30,
             env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=timeout, env=env)
        return result.returncode, result.stdout + result.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def check_tool(results: Results, profile: dict, key: str,
               required: bool = True) -> Path | None:
    tools = profile.get("tools") or {}
    path = resolve_path(tools.get(key))
    if path is None:
        results.add(f"tool:{key}", "FAIL" if required else "WARN",
                    "not configured in machine.json")
        return None
    if not path.is_file():
        results.add(f"tool:{key}", "FAIL" if required else "WARN",
                    f"not found: {path}")
        return None
    if not os.access(path, os.X_OK):
        results.add(f"tool:{key}", "FAIL" if required else "WARN",
                    f"not executable: {path}")
        return None
    results.add(f"tool:{key}", "PASS", str(path))
    return path


def run_python_probe(python: Path, required_modules: list[str]) -> tuple[dict | None, str]:
    code = r'''
import importlib.util, json, sys, warnings
warnings.simplefilter("ignore")   # keep stderr clean; the caller parses stdout JSON
import importlib
mods = json.loads(sys.argv[1])
missing, import_errors, versions = [], {}, {}
for m in mods:
    try:
        if importlib.util.find_spec(m) is None:
            missing.append(m)
        else:
            module = importlib.import_module(m)
            versions[m] = str(getattr(module, "__version__", "import ok"))
    except (ImportError, ModuleNotFoundError, ValueError):
        missing.append(m)
    except Exception as e:
        import_errors[m] = str(e)
data = {"python": sys.version.split()[0], "missing_modules": missing,
        "import_errors": import_errors, "module_versions": versions, "plugins": {}}
try:
    import vapoursynth as vs
    core = vs.core
    data["vapoursynth"] = getattr(vs, "__version__", "loaded")
    for namespace, name in (("tivtc", "TFM"), ("tivtc", "TDecimate"), ("bs", "VideoSource")):
        try:
            plugin = getattr(core, namespace)
            data["plugins"][namespace + "." + name] = callable(getattr(plugin, name))
        except Exception:
            data["plugins"][namespace + "." + name] = False
except Exception as e:
    data["vapoursynth_error"] = str(e)
print(json.dumps(data))
'''
    code_rc, output = run_text([str(python), "-c", code, json.dumps(required_modules)])
    if code_rc != 0:
        return None, output.strip()
    # Do not assume the JSON is the last line: warnings (e.g. VapourSynth R82's
    # `__version__` DeprecationWarning) are merged from stderr and can be printed
    # *after* the JSON. Whether they appear depends on whether that warning has
    # already fired in this process, so taking the last line is order-dependent
    # and passes the main run while failing a fresh test process.
    for line in reversed([ln for ln in output.splitlines() if ln.strip()]):
        if line.lstrip().startswith("{"):
            try:
                return json.loads(line), ""
            except json.JSONDecodeError:
                continue
    return None, "Python probe returned no valid JSON: " + output[-1000:]


def count_y4m_frames(data: bytes) -> int:
    """Count packed Y4M frames by walking frame-header/payload boundaries.

    Frame markers are not line-delimited after the first frame: the next
    ``FRAME`` header follows binary pixel data directly. Searching for the text
    marker therefore undercounts or can match arbitrary bytes in the image.
    """
    header_end = data.find(b"\n")
    if header_end < 0 or not data.startswith(b"YUV4MPEG2"):
        raise ValueError("invalid Y4M stream header")
    header = data[:header_end].decode("ascii", "strict")
    width_match = re.search(r"(?:^|\s)W(\d+)(?:\s|$)", header)
    height_match = re.search(r"(?:^|\s)H(\d+)(?:\s|$)", header)
    chroma_match = re.search(r"(?:^|\s)C([^\s]+)", header)
    if not width_match or not height_match:
        raise ValueError("Y4M stream header has no width/height")
    width, height = int(width_match.group(1)), int(height_match.group(1))
    chroma = chroma_match.group(1).casefold() if chroma_match else "420jpeg"
    bit_match = re.search(r"p(\d+)", chroma)
    bytes_per_sample = 1 if not bit_match or int(bit_match.group(1)) <= 8 else 2
    luma_bytes = width * height * bytes_per_sample
    if chroma.startswith("420"):
        chroma_bytes = ((width + 1) // 2) * ((height + 1) // 2) * bytes_per_sample * 2
    elif chroma.startswith("422"):
        chroma_bytes = ((width + 1) // 2) * height * bytes_per_sample * 2
    elif chroma.startswith("444"):
        chroma_bytes = width * height * bytes_per_sample * 2
    elif chroma.startswith("440"):
        chroma_bytes = width * ((height + 1) // 2) * bytes_per_sample * 2
    elif chroma.startswith("411"):
        chroma_bytes = ((width + 3) // 4) * height * bytes_per_sample * 2
    elif chroma.startswith("mono"):
        chroma_bytes = 0
    else:
        raise ValueError(f"unsupported Y4M chroma format: {chroma}")
    frame_bytes = luma_bytes + chroma_bytes

    cursor = header_end + 1
    frames = 0
    while cursor < len(data):
        if not data.startswith(b"FRAME", cursor):
            raise ValueError(f"expected FRAME header at byte {cursor}")
        frame_header_end = data.find(b"\n", cursor)
        if frame_header_end < 0:
            raise ValueError("truncated Y4M frame header")
        payload_start = frame_header_end + 1
        payload_end = payload_start + frame_bytes
        if payload_end > len(data):
            raise ValueError("truncated Y4M frame payload")
        frames += 1
        cursor = payload_end
    return frames


def vspipe_smoke(vspipe: Path, python: Path) -> tuple[bool, str]:
    """Exercise the actual TIVTC -> vspipe Y4M path with a tiny synthetic clip."""
    prefix = python.parent.parent if python.parent.name == "bin" else None
    env = os.environ.copy()
    if prefix and (prefix / "lib").is_dir():
        lib_dir = str(prefix / "lib")
        previous = env.get("DYLD_LIBRARY_PATH", "")
        env["DYLD_LIBRARY_PATH"] = lib_dir + (os.pathsep + previous if previous else "")
    config = Path.home() / ".config" / "vapoursynth" / "vapoursynth.toml"
    if not config.is_file():
        return False, f"missing VapourSynth runtime config {config}; run `vapoursynth config`"

    try:
        with tempfile.TemporaryDirectory(prefix="ld-preflight-vs-") as temp:
            root = Path(temp)
            script = root / "preflight.vpy"
            tfm_log = root / "preflight-tfm.log"
            script.write_text(
                "import vapoursynth as vs\n"
                "core = vs.core\n"
                "clip = core.std.BlankClip(width=64, height=48, length=30, "
                "format=vs.YUV420P8)\n"
                "clip = core.std.SetFrameProps(clip, _FieldBased=2)\n"
                f"clip = core.tivtc.TFM(clip, order=-1, PP=0, chroma=1, slow=2, output={str(tfm_log)!r})\n"
                "clip.set_output()\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [str(vspipe), "-c", "y4m", str(script), "-"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=90,
            )
            if result.returncode != 0:
                err = result.stderr.decode("utf-8", "replace")[-2000:]
                return False, f"vspipe smoke exit={result.returncode}: {err}"
            if not result.stdout.startswith(b"YUV4MPEG2"):
                return False, "vspipe output did not begin with a YUV4MPEG2 header"
            try:
                frame_count = count_y4m_frames(result.stdout)
            except (UnicodeDecodeError, ValueError) as exc:
                return False, f"Y4M output could not be parsed: {exc}"
            if frame_count != 30:
                return False, f"Y4M smoke produced {frame_count} frames, expected 30"
            if not tfm_log.is_file():
                return False, "TIVTC smoke did not create its TFM log"
            decisions = [line for line in tfm_log.read_text(encoding="utf-8", errors="replace").splitlines()
                         if re.match(r"^\d+\s+[a-z]\s+\[\d+\]$", line)]
            if len(decisions) != 30:
                return False, f"TIVTC smoke logged {len(decisions)} decisions, expected 30"
            return True, f"TIVTC/vspipe/Y4M passed on 30 synthetic frames (DYLD path={'set' if 'DYLD_LIBRARY_PATH' in env else 'unchanged'})"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"vspipe smoke could not complete: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="machine.json path; defaults to this bundle's profile")
    ap.add_argument("--episode", help="also sanity-check an episode manifest")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    if sys.platform != "darwin":
        print("FAIL platform: preflight_mac.py must run on macOS")
        return 2

    profile_path = resolve_path(args.profile) if args.profile else Path(__file__).resolve().parent.parent / "machine.json"
    if profile_path is None or not profile_path.is_file():
        print(f"FAIL profile: not found: {profile_path}")
        return 2
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL profile: {exc}")
        return 2

    results = Results(quiet=args.json)
    results.add("platform", "PASS" if profile.get("os") == "darwin" else "FAIL",
                f"{platform.mac_ver()[0] or platform.platform()}; profile os={profile.get('os')!r}")
    results.add("profile", "PASS", str(profile_path))
    version_path = Path(__file__).resolve().parent.parent / "WORKFLOW_VERSION"
    try:
        workflow_version = version_path.read_text(encoding="ascii").strip()
        version_ok = re.fullmatch(r"[1-9]\d*", workflow_version) is not None
        results.add("workflow:version", "PASS" if version_ok else "FAIL",
                    f"Version {workflow_version}" if version_ok else
                    f"invalid WORKFLOW_VERSION value: {workflow_version!r}")
    except OSError as exc:
        results.add("workflow:version", "FAIL", f"cannot read {version_path}: {exc}")
    versioning_doc = version_path.with_name("WORKFLOW_VERSIONING.md")
    change_template = version_path.parent / "templates" / "device-workflow-change.template.md"
    for check_name, path in (("workflow:versioning-doc", versioning_doc),
                             ("workflow:device-log-template", change_template)):
        results.add(check_name, "PASS" if path.is_file() else "FAIL",
                    str(path) if path.is_file() else f"missing: {path}")

    ffmpeg = check_tool(results, profile, "ffmpeg")
    ffprobe = check_tool(results, profile, "ffprobe")
    orc = check_tool(results, profile, "orc")
    python_value = (profile.get("python") or {}).get("interpreter")
    python = resolve_path(python_value)
    if python is None or not python.is_file() or not os.access(python, os.X_OK):
        results.add("tool:python.interpreter", "FAIL",
                    f"not configured, found, and executable: {python_value!r}")
        python = None
    else:
        results.add("tool:python.interpreter", "PASS", str(python))
    check_tool(results, profile, "pwsh", required=True)
    if python is not None:
        vspipe_value = (profile.get("tools") or {}).get("vspipe")
        vspipe = resolve_path(vspipe_value) if vspipe_value else None
        if vspipe is None:
            sibling = python.parent / "vspipe"
            vspipe = sibling if sibling.is_file() else (Path(shutil.which("vspipe")) if shutil.which("vspipe") else None)
        if vspipe is None or not vspipe.is_file() or not os.access(vspipe, os.X_OK):
            results.add("tool:vspipe", "FAIL", "not found/executable; set tools.vspipe or place it beside the selected Python")
            vspipe = None
        else:
            results.add("tool:vspipe", "PASS", str(vspipe))
    else:
        vspipe = None

    roots = profile.get("roots") or {}
    work = resolve_path(roots.get("work"))
    if work is None or not work.is_dir():
        results.add("root:work", "FAIL", f"missing/non-directory: {work}")
    else:
        results.add("root:work", "PASS", str(work))
        limits = profile.get("limits") or {}
        min_free = float(limits.get("min_free_gb", 80))
        free_gib = shutil.disk_usage(work).free / (1024 ** 3)
        status = "PASS" if free_gib >= min_free else "FAIL"
        results.add("space:work", status,
                    f"{free_gib:.1f} GiB free; configured minimum {min_free:.1f} GiB")
    for root_name in ("probe", "avs", "audio", "subs", "out",
                      "prores", "stack", "logs"):
        value = roots.get(root_name)
        path = resolve_path(value)
        if path is None or not path.exists():
            results.add(f"root:{root_name}", "FAIL", f"missing/unset: {value!r}")
        elif not path.is_dir():
            results.add(f"root:{root_name}", "FAIL", f"not a directory: {path}")
        else:
            results.add(f"root:{root_name}", "PASS", str(path))
    dvd_root_value = roots.get("dvd_root")
    dvd_root = resolve_path(dvd_root_value)
    if dvd_root is None:
        results.add("root:dvd_root", "WARN", "unset; required only for DVD-dependent stages")
    elif not dvd_root.is_dir():
        results.add("root:dvd_root", "FAIL", f"not a directory: {dvd_root}")
    else:
        results.add("root:dvd_root", "PASS", str(dvd_root))
    downloads = resolve_path(roots.get("downloads_root"))
    if downloads is None or not downloads.is_dir():
        results.add("root:downloads_root", "WARN",
                    f"missing/unset: {roots.get('downloads_root')!r}; an episode may override this root")
    else:
        results.add("root:downloads_root", "PASS", str(downloads))
        try:
            free_gib = shutil.disk_usage(downloads).free / (1024 ** 3)
            results.add("space:downloads", "INFO", f"{free_gib:.1f} GiB free; compare with the selected download size")
        except OSError as exc:
            results.add("space:downloads", "WARN", f"could not query free space: {exc}")

    if ffmpeg is not None:
        code, version = run_text([str(ffmpeg), "-version"])
        results.add("ffmpeg:version", "PASS" if code == 0 else "FAIL",
                    version.splitlines()[0] if version else "no version output")
        code, encoders = run_text([str(ffmpeg), "-hide_banner", "-encoders"])
        results.add("ffmpeg:libx265", "PASS" if code == 0 and "libx265" in encoders else "FAIL",
                    "libx265 encoder available" if "libx265" in encoders else "libx265 encoder missing")
        code, demuxers = run_text([str(ffmpeg), "-hide_banner", "-demuxers"])
        results.add("ffmpeg:yuv4mpegpipe", "PASS" if code == 0 and "yuv4mpegpipe" in demuxers else "FAIL",
                    "YUV4MPEG input demuxer available" if "yuv4mpegpipe" in demuxers else "yuv4mpegpipe missing")
    if ffprobe is not None:
        code, version = run_text([str(ffprobe), "-version"])
        results.add("ffprobe:version", "PASS" if code == 0 else "FAIL",
                    version.splitlines()[0] if version else "no version output")

    if orc is not None:
        code, stages = run_text([str(orc), "stages", "list", "--core"])
        required_stages = ("stacker", "source_align", "tbc_source", "EFMSink",
                           "RawEFMSink", "efm_audio_decode", "audio_align")
        normalized_output = re.sub(r"[^a-z0-9]", "", stages.casefold())
        missing = [name for name in required_stages
                   if re.sub(r"[^a-z0-9]", "", name.casefold()) not in normalized_output]
        if code == 0 and not missing:
            results.add("orc:core-stages", "PASS", "all required core stack/EFM stages listed")
        else:
            results.add("orc:core-stages", "FAIL",
                        f"stages list --core exit={code}; missing/unconfirmed: {', '.join(missing)}")
        app = next((parent for parent in orc.parents if parent.suffix == ".app"), None)
        plist = app / "Contents" / "Info.plist" if app else None
        if plist is not None and plist.is_file():
            version_rc, version_text = run_text([
                "/usr/bin/plutil", "-extract", "CFBundleShortVersionString",
                "raw", str(plist),
            ])
            version = version_text.strip()
            if version_rc == 0 and version == "2.11.1":
                results.add("orc:version", "PASS", "2.11.1 (current stacker parity pin)")
            elif version_rc == 0 and version:
                results.add("orc:version", "WARN",
                            f"{version}; verify the current stacker-parity pin before processing")
            else:
                results.add("orc:version", "WARN", "could not read app-bundle version")
        else:
            results.add("orc:version", "WARN", "orc-cli app bundle not found; verify its version manually")

    python_data = None
    if python is not None:
        required = list((profile.get("python") or {}).get("require") or ["numpy", "cv2", "PIL"])
        for module in ("vapoursynth",):
            if module not in required:
                required.append(module)
        python_data, error = run_python_probe(python, required)
        if python_data is None:
            results.add("python:modules", "FAIL", error)
        else:
            missing = python_data.get("missing_modules", [])
            import_errors = python_data.get("import_errors", {})
            module_status = "PASS" if not missing and not import_errors else "FAIL"
            results.add("python:modules", module_status,
                        f"Python {python_data.get('python')}; versions={python_data.get('module_versions', {})}; "
                        f"missing={missing or 'none'}; import_errors={import_errors or 'none'}")
            if "vapoursynth_error" in python_data:
                results.add("vapoursynth:import", "FAIL", python_data["vapoursynth_error"])
                results.add("vapoursynth:plugins", "FAIL", "cannot inspect plugins because VapourSynth did not import")
            else:
                plugins = python_data.get("plugins", {})
                absent = [name for name, ok in plugins.items() if not ok]
                results.add("vapoursynth:plugins", "PASS" if not absent else "FAIL",
                            "TFM/TDecimate and bestsource available" if not absent else "missing: " + ", ".join(absent))

    if vspipe is not None and python is not None and python_data is not None:
        ok, detail = vspipe_smoke(vspipe, python)
        results.add("vspipe:tfm-y4m-smoke", "PASS" if ok else "FAIL", detail)

    scripts_dir = Path(__file__).resolve().parent
    required_scripts = (
        "build_map_vpy.py", "build_map.ps1", "build_stack_project.py",
        "select_stack_reference.py", "extract_video_descriptors.py",
        "measure_dvd_alignment.py", "measure_break_edges.py",
        "measure_audio_rate.py", "audio_timing_diagnostic.py", "build_ld_synced.py",
        "retime_dvd_assets.py", "av_check_output.py", "ld_only_sync_check.py",
        "pad_audit.py", "fullres_check.py", "rife_padframes.py",
        "build_padfix_vpy.py", "encode_episode_mac.py", "remux_padfixed_mac.py",
        "vspipe_util.py", "build_motion_trace.py", "qc_report.py",
        "verify_pad_identities.py", "check_space_budget.py", "doctor.py",
        "bootstrap_deps.py", "dependency_lock.py", "run_stage.py",
        "generate_runbooks.py",
    )
    missing_scripts = [name for name in required_scripts if not (scripts_dir / name).is_file()]
    results.add("bundle:workflow-scripts", "PASS" if not missing_scripts else "FAIL",
                f"{len(required_scripts) - len(missing_scripts)}/{len(required_scripts)} present"
                if not missing_scripts else "missing: " + ", ".join(missing_scripts))
    deps_dir = Path(__file__).resolve().parent.parent / "dependencies"
    dependency_files = ("requirements-windows-x86_64.lock.txt",
                        "requirements-macos-arm64.lock.txt",
                        "requirements-linux-x86_64.lock.txt",
                        "environment-macos-arm64.yml",
                        "environment-linux-x86_64.yml")
    missing_dependencies = [name for name in dependency_files if not (deps_dir / name).is_file()]
    results.add("bundle:dependency-locks", "PASS" if not missing_dependencies else "FAIL",
                f"{len(dependency_files) - len(missing_dependencies)}/{len(dependency_files)} present"
                if not missing_dependencies else "missing: " + ", ".join(missing_dependencies))

    if args.episode:
        episode_path = profile_path.parent / "episodes" / f"{args.episode}.json"
        if not episode_path.is_file():
            results.add("episode:manifest", "FAIL", f"missing manifest: {episode_path}")
        else:
            try:
                episode = json.loads(episode_path.read_text(encoding="utf-8"))
                captures = episode.get("captures") or []
                if captures:
                    results.add("episode:manifest", "PASS",
                                f"{args.episode}: {episode.get('title', '')!r}, {len(captures)} captures")
                else:
                    results.add("episode:manifest", "FAIL", "manifest has no captures")
                reference = episode.get("reference")
                results.add("episode:reference", "PASS" if reference else "WARN",
                            str(reference) if reference else "UNMEASURED; set after Orc refresh")
            except (OSError, json.JSONDecodeError) as exc:
                results.add("episode:manifest", "FAIL", str(exc))

    # RIFE is optional until the current episode's PAD policy requires it.
    rife_engine = check_tool(results, profile, "rife_engine", required=False)
    if rife_engine is not None:
        if not os.access(rife_engine, os.X_OK):
            results.add("rife:executable", "FAIL", f"not executable: {rife_engine}")
        else:
            code, output = run_text([str(rife_engine), "-h"], timeout=30)
            loader_error = any(marker in output.casefold()
                               for marker in ("dyld", "cannot open", "not found"))
            results.add("rife:starts", "PASS" if code != 127 and not loader_error else "FAIL",
                        "engine responds to -h" if code != 127 and not loader_error else output[-1000:])
    model_dir = resolve_path((profile.get("tools") or {}).get("rife_model_dir"))
    if model_dir is None or not model_dir.is_dir():
        results.add("tool:rife_model_dir", "WARN", "not configured/found; required only if a Mac PAD stage uses RIFE")
    else:
        bins = list(model_dir.rglob("*.bin"))
        params = list(model_dir.rglob("*.param"))
        results.add("tool:rife_model_dir", "PASS" if bins and params else "FAIL",
                    f"{len(bins)} .bin / {len(params)} .param under {model_dir}")
    check_tool(results, profile, "sevenzip", required=False)

    failures = results.failures
    if args.json:
        print(json.dumps({
            "pass": results.passes,
            "warn": results.warnings,
            "fail": failures,
            "ok": failures == 0,
            "results": [{"check": name, "status": status, "detail": detail}
                        for name, status, detail in results.rows],
        }, indent=2))
        return 1 if failures else 0
    if failures:
        print(f"PREFLIGHT MAC FAIL: {failures} fail, {results.warnings} warn")
        return 1
    print(f"PREFLIGHT MAC OK: {results.passes} pass, {results.warnings} warn, 0 fail")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
