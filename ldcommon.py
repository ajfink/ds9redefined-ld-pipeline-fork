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

Manifests live at <bundle>/episodes/<code>.json.
"""
from __future__ import annotations

import json
import os
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
    p = BUNDLE / "episodes" / f"{code}.json"
    if not p.exists():
        raise PipelineError(f"no manifest for {code}: {p} - create it from "
                            f"episodes/_template.json after measuring the episode")
    return Episode(code, _load_json(p), ld)


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
