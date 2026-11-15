#!/usr/bin/env python3
"""Shared helpers for the LaserDisc pipeline.

Everything machine-specific and episode-specific is resolved through here so
no pipeline script has to hard-code a drive letter, a user profile or an
episode's frame count.

  ld = Ld()                       # loads <bundle>/machine.json
  ld.tool('ffmpeg')               # -> absolute path, raises with a fix hint
  ld.root('out')                  # -> pathlib.Path
  ld.episode('S01E05')            # -> Episode object (manifest)
  ep.frame_count, ep.duration_s   # measured values, never guessed
  ep.path('pre_upscale')          # -> Path via {root, name} indirection

Profile resolution order:
  $LDP_PROFILE -> <bundle>/machine.json -> <bundle>/machine.example.json

Local manifests live at <bundle>/episodes/<code>.json. In shared-release mode,
the active episode manifest is redirected to the caller's unique
``LDP_INSTANCE_ROOT/manifest`` folder; no shared-root manifest writes are allowed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent
_REQUIRED_TOOLS = ("ffmpeg", "ffprobe", "orc", "rife_engine", "rife_model_dir",
                   "avisynth_plugin_dir")
_INSTALL_HINT = {
    "numpy": "pip install numpy",
    "cv2": "pip install opencv-python   # required by the replacement-identity gate",
    "PIL": "pip install pillow          # required by contact sheets/thumbnails",
}


class PipelineError(RuntimeError):
    """A missing tool/manifest/dependency, reported with an actionable message."""


def require_modules(*names):
    """Import dependencies up front with a message that names the pip command."""
    missing = []
    for n in names:
        try:
            __import__(n)
        except ImportError:
            missing.append(n)
    if missing:
        hints = "; ".join(_INSTALL_HINT.get(n, f"pip install {n}") for n in missing)
        raise PipelineError(f"missing Python package(s) {', '.join(missing)} - {hints}")


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PipelineError(f"file not found: {path}")
    except json.JSONDecodeError as e:
        raise PipelineError(f"{path} is not valid JSON: {e}")


class Ld:
    def __init__(self, profile_path: str | os.PathLike | None = None):
        p = profile_path or os.environ.get("LDP_PROFILE")
        shared_devices = BUNDLE / "devices"
        if not p and shared_devices.is_dir():
            raise PipelineError(
                f"{BUNDLE} is configured as a shared release tree; do not use a bundle-local profile. "
                "Set LDP_PROFILE to a local machine.json and initialize a unique workspace with "
                "scripts/shared_workspace.py.")
        if p and shared_devices.is_dir():
            candidate = Path(os.path.expandvars(os.path.expanduser(str(p)))).resolve()
            try:
                candidate.relative_to(BUNDLE.resolve())
                inside_release = True
            except ValueError:
                inside_release = False
            if inside_release:
                raise PipelineError(f"machine profile must be outside the shared release tree: {candidate}")
        self.profile_path = Path(p) if p else BUNDLE / "machine.json"
        if not self.profile_path.exists():
            alt = BUNDLE / "machine.example.json"
            if alt.exists():
                print(f"WARN: no {self.profile_path}; falling back to {alt} "
                      f"(run preflight.ps1 -Write to create a real profile)", file=sys.stderr)
                self.profile_path = alt
            else:
                raise PipelineError(f"no machine profile at {self.profile_path} - "
                                    f"run preflight.ps1 -Write")
        self.profile = _load_json(self.profile_path)
        shared = self.profile.get("shared") or {}
        if shared.get("enabled") is True:
            release_value = shared.get("release_root")
            release = Path(os.path.expandvars(os.path.expanduser(str(release_value)))) if release_value else None
            if release is None or release.resolve() != BUNDLE.resolve():
                raise PipelineError("shared.release_root must identify the bundle used by these scripts")
        elif shared_devices.is_dir():
            raise PipelineError("this README folder has per-device workspace data; use an external profile with shared.enabled=true")

    # ---- tools ----
    def tool(self, name: str) -> Path:
        v = (self.profile.get("tools") or {}).get(name)
        if not v:
            raise PipelineError(
                f"tool '{name}' is not set in {self.profile_path} - "
                f"run preflight.ps1 -Write to discover it")
        path = Path(v)
        if not path.exists():
            raise PipelineError(f"tool '{name}' points at a missing path: {path}")
        return path

    def ffmpeg(self) -> str:
        return str(self.tool("ffmpeg"))

    def ffprobe(self) -> str:
        return str(self.tool("ffprobe"))

    # ---- roots ----
    def root(self, name: str) -> Path:
        if name == "logs" and device_workspace_enabled(self.profile):
            return shared_instance_root(self.profile, self.profile_path,
                                        os.environ.get("LDP_EPISODE")) / "logs"
        v = (self.profile.get("roots") or {}).get(name)
        if not v:
            raise PipelineError(f"root '{name}' is not set in {self.profile_path}")
        return Path(v)

    def work(self) -> Path:
        return self.root("work")

    def downloads_dir(self, episode_code: str, override: str | None = None) -> Path:
        if override:
            return Path(override)
        roots = self.profile.get("roots") or {}
        template = roots.get("downloads_template") or "DS9 {episode} LaserDisc"
        base = roots.get("downloads_root")
        if not base:
            raise PipelineError("roots.downloads_root is not set in the profile")
        return Path(base) / template.format(episode=episode_code)

    # ---- reports ----
    def probe(self) -> Path:
        return self.root("probe")

    def merge_tsv(self, path: Path, rows: list[dict], key_fields: tuple[str, ...],
                  header: tuple[str, ...] | None = None) -> int:
        """Write rows into a TSV, replacing only rows with the same key values.

        A selective (one-episode) run must never clobber other episodes' records.
        """
        if header is None:
            header = tuple(rows[0].keys()) if rows else ()
        kept: list[list] = []
        if path.exists():
            with path.open(newline="", encoding="utf-8") as f:
                import csv as _csv
                for r in _csv.DictReader(f, delimiter="\t"):
                    if tuple(r.get(k, "") for k in key_fields) not in {
                            tuple(str(row.get(k, "")) for k in key_fields) for row in rows}:
                        kept.append([r.get(k, "") for k in header])
        import csv as _csv
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = _csv.writer(f, delimiter="\t")
            w.writerow(header)
            w.writerows(kept)
            for row in rows:
                w.writerow([row.get(k, "") for k in header])
        return len(kept)


class Episode:
    """A per-episode manifest: measured values live here, not in code."""

    def __init__(self, code: str, data: dict, ld: "Ld"):
        self.code = code
        self.data = data
        self.ld = ld

    def __repr__(self):
        return f"<Episode {self.code} {self.data.get('title', '')!r}>"

    # ---- identity ----
    @property
    def title(self) -> str:
        return self.data.get("title", self.code)

    def downloads(self) -> Path:
        return self.ld.downloads_dir(self.code, (self.data.get("roots") or {}).get("downloads"))

    # ---- measured values (fail loudly rather than silently defaulting) ----
    def _measure(self, section: str, key: str, cast=float):
        try:
            return cast(self.data[section][key])
        except KeyError:
            raise PipelineError(f"{self.code}: manifest is missing {section}.{key} - "
                                f"measure it and record it; never guess")
        except (TypeError, ValueError) as e:
            raise PipelineError(f"{self.code}: {section}.{key} has a bad value: {e}")

    @property
    def stack_frames(self) -> int:
        return self._measure("stack", "frames", int)

    @property
    def frame_count(self) -> int:
        return self._measure("video", "frames", int)

    @property
    def duration_s(self) -> float:
        return self._measure("video", "duration_s", float)

    @property
    def head_trim(self) -> int:
        return self._measure("video", "head_trim", int)

    @property
    def delta_s(self) -> float:
        return self._measure("video", "delta_s", float)

    @property
    def audio_skip_s(self) -> float:
        return self._measure("audio", "skip_s", float)

    @property
    def pad_trim(self) -> int:
        return self._measure("audio", "pad_frames", int)

    @property
    def capture_set(self) -> list[str]:
        caps = self.data.get("captures") or []
        if not caps:
            raise PipelineError(f"{self.code}: manifest has no captures")
        return [c["label"] for c in caps]

    @property
    def reference(self) -> str:
        ref = self.data.get("reference")
        if not ref:
            raise PipelineError(f"{self.code}: manifest has no stack reference")
        return ref

    # ---- files: {root, name} keeps machine paths out of the manifest ----
    def path(self, key: str) -> Path:
        spec = (self.data.get("files") or {}).get(key)
        if not spec:
            raise PipelineError(f"{self.code}: manifest has no file entry '{key}'")
        if isinstance(spec, str):
            return Path(spec)
        if "root" in spec:
            return self.ld.root(spec["root"]) / spec["name"]
        return Path(spec["path"])

    def optional_path(self, key: str) -> Path | None:
        try:
            return self.path(key)
        except PipelineError:
            return None


def load_profile(path=None) -> Ld:
    return Ld(path)


def load_episode(code: str, ld: Ld | None = None) -> Episode:
    ld = ld or Ld()
    p = episode_manifest_path(code, ld)
    if not p.exists():
        raise PipelineError(f"no manifest for {code}: {p} - create it from "
                            f"episodes/_template.json after verified downloads")
    return Episode(code, _load_json(p), ld)


def device_workspace_enabled(profile: dict | None) -> bool:
    if profile is None:
        return False
    return (profile.get("shared") or {}).get("enabled") is True


def workspace_release_root(profile: dict) -> Path:
    shared = profile.get("shared") or {}
    raw = shared.get("release_root") or BUNDLE
    return Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()


def shared_instance_root(profile: dict | None = None, profile_path: Path | None = None,
                         episode: str | None = None) -> Path:
    """Resolve and validate a README-relative device workspace or local fallback."""
    profile = profile or Ld().profile
    shared = profile.get("shared") or {}
    if not device_workspace_enabled(profile):
        raise PipelineError("device workspace is not enabled in the selected local profile")
    instance = os.environ.get("LDP_INSTANCE_ROOT")
    if not instance:
        episode = episode or os.environ.get("LDP_EPISODE")
        profile_path = profile_path or (Path(os.path.expandvars(os.path.expanduser(
            os.environ["LDP_PROFILE"]))).resolve() if os.environ.get("LDP_PROFILE") else None)
        if not episode or not profile_path:
            raise PipelineError("shared mode has no active workspace; episode helpers create one when an episode code is known")
        scripts_dir = BUNDLE / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        try:
            from shared_workspace import resolve_instance_workspace
            result, warning = resolve_instance_workspace(profile_path, profile, episode, create=True)
        except Exception as exc:
            raise PipelineError(f"could not prepare shared or local-fallback episode workspace: {exc}") from exc
        if not result:
            raise PipelineError(warning or "could not prepare an episode workspace")
        for key, value in result["shell_exports"].items():
            os.environ[key] = value
        if warning:
            print("WARN: " + warning, file=sys.stderr)
        instance = result["shell_exports"]["LDP_INSTANCE_ROOT"]
    root = Path(os.path.expandvars(os.path.expanduser(instance))).resolve()
    release = workspace_release_root(profile)
    if release != BUNDLE.resolve():
        raise PipelineError(f"shared.release_root must identify the bundle used by these scripts: {BUNDLE}")
    metadata_path = root / "workspace.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"instance metadata is missing/invalid at {metadata_path}: {exc}") from exc
    if metadata.get("storage_mode") == "local-fallback" or os.environ.get("LDP_INSTANCE_MODE") == "local-fallback":
        key = metadata.get("local_root_key")
        if key not in ("logs", "work", "probe"):
            raise PipelineError("local fallback workspace has an invalid local_root_key")
        raw = (profile.get("roots") or {}).get(key)
        if not raw:
            raise PipelineError(f"local fallback profile roots.{key} is missing")
        base = Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()
        device_name = str(shared.get("device_name") or profile.get("machine") or "device")
        safe_device = re.sub(r"[^A-Za-z0-9._-]+", "-", device_name).strip(".-_")[:64]
        expected = base / "ld-pipeline-local" / safe_device / "episodes" / root.name
        if (root != expected.resolve() or metadata.get("machine") != profile.get("machine") or
                str(metadata.get("episode", "")).casefold() != root.name.casefold()):
            raise PipelineError("device-local fallback workspace does not match the selected profile/episode")
        return root
    devices = release / "devices"
    if not devices.is_dir():
        raise PipelineError(f"README-folder devices directory is unavailable: {devices}")
    try:
        root.relative_to(devices.resolve())
    except ValueError as exc:
        raise PipelineError(f"LDP_INSTANCE_ROOT must be inside {devices}, not {root}") from exc
    if not root.is_dir():
        raise PipelineError(f"LDP_INSTANCE_ROOT is not an initialized directory: {root}")
    try:
        relative = root.relative_to(devices.resolve())
    except ValueError as exc:
        raise PipelineError(f"LDP_INSTANCE_ROOT is outside the shared devices tree: {root}") from exc
    if len(relative.parts) != 3 or relative.parts[1] != "episodes":
        raise PipelineError("LDP_INSTANCE_ROOT must have form devices/<device-instance>/episodes/<episode>")
    device_name = str(shared.get("device_name") or profile.get("machine") or "device")
    safe_device = re.sub(r"[^A-Za-z0-9._-]+", "-", device_name).strip(".-_")[:64]
    if not relative.parts[0].startswith(safe_device + "-"):
        raise PipelineError("LDP_INSTANCE_ROOT belongs to a different device label; create this device's workspace")
    expected_relative = root.relative_to(release).as_posix()
    if (metadata.get("release_root") != "." or
            metadata.get("instance_relative_path") != expected_relative or
            metadata.get("machine") != profile.get("machine") or
            str(metadata.get("episode", "")).casefold() != root.name.casefold() or
            relative.parts[0].rsplit("-", 1)[-1] != metadata.get("instance_id")):
        raise PipelineError("shared instance metadata does not match the selected release/workspace")
    return root


def shared_enabled(profile: dict | None = None) -> bool:
    if profile is None:
        raise PipelineError("shared_enabled requires a loaded profile")
    return (profile.get("shared") or {}).get("enabled") is True


def episode_manifest_path(code: str, ld: Ld | None = None) -> Path:
    """Return the current manifest location without ever writing to a shared release tree."""
    ld = ld or Ld()
    if device_workspace_enabled(ld.profile):
        os.environ["LDP_EPISODE"] = code
        try:
            instance = shared_instance_root(ld.profile, ld.profile_path, code)
        except PipelineError:
            scripts_dir = BUNDLE / "scripts"
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            try:
                from shared_workspace import resolve_instance_workspace
                result, warning = resolve_instance_workspace(
                    ld.profile_path, ld.profile, code,
                    os.environ.get("LDP_INSTANCE_ROOT"), create=True)
                if not result:
                    raise PipelineError(warning or "no usable workspace")
                for key, value in result["shell_exports"].items():
                    os.environ[key] = value
                instance = Path(result["instance_root"]).resolve()
                if warning:
                    print("WARN: " + warning, file=sys.stderr)
            except Exception as exc:
                raise PipelineError(f"could not prepare shared or local-fallback episode workspace: {exc}") from exc
        if instance.name.casefold() != code.casefold():
            raise PipelineError(f"active shared workspace is for {instance.name}, not requested episode {code}")
        manifest_dir = instance / "manifest"
        configured = os.environ.get("LDP_MANIFEST_DIR")
        if configured and Path(os.path.expandvars(os.path.expanduser(configured))).resolve() != manifest_dir.resolve():
            raise PipelineError("LDP_MANIFEST_DIR must equal <LDP_INSTANCE_ROOT>/manifest in shared mode")
        return manifest_dir / f"{code}.json"
    override = os.environ.get("LDP_MANIFEST_DIR")
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override))).resolve() / f"{code}.json"
    return BUNDLE / "episodes" / f"{code}.json"


def assert_writable_path(path: str | os.PathLike, ld: Ld | None = None) -> Path:
    """In shared mode, allow writes under the active instance only, never the release tree."""
    ld = ld or Ld()
    target = Path(path).expanduser().resolve()
    if not device_workspace_enabled(ld.profile):
        return target
    bundle = BUNDLE.resolve()
    try:
        target.relative_to(bundle)
    except ValueError:
        return target
    instance = shared_instance_root(ld.profile, ld.profile_path,
                                    os.environ.get("LDP_EPISODE"))
    allowed_roots = (instance / "manifest", instance / "logs", instance / "references",
                     instance / "cut-pad-images")
    if target == instance / "workspace.json" or any(
            target == allowed or target.is_relative_to(allowed) for allowed in allowed_roots):
        return target
    raise PipelineError(f"shared mode forbids writing to the release tree: {target}; "
                        f"use only this instance's manifest/logs/references/cut-pad-images folders "
                        f"under {instance}")


def redact_shared_text(text: str, ld: Ld) -> str:
    """Redact local machine paths before persisting output in a shared instance folder."""
    if not device_workspace_enabled(ld.profile):
        return text
    pairs: list[tuple[str, str]] = [(str(ld.profile_path.resolve()), "<device-local-profile>")]
    if str(Path.home()) not in ("", "."):
        pairs.append((str(Path.home().resolve()), "<user-home>"))
    shared_root = workspace_release_root(ld.profile)
    for section in ("tools", "roots", "python"):
        for name, value in ((ld.profile.get(section) or {}).items()):
            if not value or not isinstance(value, (str, os.PathLike)):
                continue
            candidate = Path(os.path.expandvars(os.path.expanduser(str(value))))
            if not candidate.is_absolute():
                continue
            try:
                candidate.resolve().relative_to(shared_root)
                continue
            except ValueError:
                pass
            pairs.append((str(candidate.resolve()), f"<{section}.{name}>"))
    pairs = sorted(set(pairs), key=lambda pair: len(pair[0]), reverse=True)
    for original, replacement in pairs:
        # JSON reports contain doubled backslashes. Redact that spelling first
        # so a drive-root path cannot leave an invalid JSON ``\D`` escape.
        escaped = original.replace("\\", "\\\\")
        if escaped != original:
            escaped_replacement = replacement + ("\\\\" if original.endswith("\\") else "")
            text = re.sub(re.escape(escaped), lambda _: escaped_replacement,
                          text, flags=re.IGNORECASE)
        forward = original.replace("\\", "/")
        if forward != original:
            forward_replacement = replacement + ("/" if original.endswith(("\\", "/")) else "")
            text = re.sub(re.escape(forward), lambda _: forward_replacement,
                          text, flags=re.IGNORECASE)
        raw_pattern = re.escape(original)
        raw_replacement = replacement
        if original.endswith("\\"):
            raw_pattern += r"(?!\\)"
        if original.endswith(("\\", "/")):
            raw_replacement += original[-1]
        text = re.sub(raw_pattern, lambda _: raw_replacement,
                      text, flags=re.IGNORECASE)
    return text


def main_smoke():
    ld = Ld()
    print(f"profile : {ld.profile_path}")
    print(f"machine : {ld.profile.get('machine')}")
    for t in _REQUIRED_TOOLS:
        try:
            print(f"  tool {t:22s} {ld.tool(t)}")
        except PipelineError as e:
            print(f"  tool {t:22s} FAIL: {e}")
    for r in ("work", "out", "probe", "downloads_root", "dvd_root", "upscale_root"):
        try:
            print(f"  root  {r:22s} {ld.root(r)}")
        except PipelineError as e:
            print(f"  root  {r:22s} ({e})")
    print(f"episodes: {sorted(p.stem for p in (BUNDLE / 'episodes').glob('*.json'))}"
          if (BUNDLE / "episodes").exists() else "episodes: none yet")


if __name__ == "__main__":
    main_smoke()
