"""Machine profile (device-local configuration).

The profile lives outside the code tree (``FSTK_PROFILE`` or the per-user config dir) and
never inside a release folder. It names:

* ``tools``    – absolute paths of external executables/model dirs;
* ``data_root``– small, durable per-episode state (manifest, journal, plans, logs, evidence);
* ``media_root`` – large per-episode media; individual artifact roots may be redirected to
  other volumes through ``roots`` (e.g. put ``stack`` on the fastest local disk);
* ``sources``  – read-only inputs (capture downloads, DVD rips);
* ``limits``   – space reserve and per-resource concurrency.

Root names keep v38's vocabulary (``stack``, ``prores``, ``probe``, ``audio``, ``subs``,
``out``, ``work``, ``logs``) so imported manifests resolve without rewriting; v38's ``avs``
root maps to ``scripts`` (VapourSynth scripts now).
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from fstk.core.errors import ConfigError
from fstk.core.fsio import atomic_write_json, read_json
from fstk.core.identity import EpisodeId

SCHEMA = "fstk-machine-profile-v1"

#: Every external tool the pipeline knows. ``kind`` is "exe" or "dir".
KNOWN_TOOLS: dict[str, dict[str, Any]] = {
    "ffmpeg": {"kind": "exe", "required": True, "path_names": ["ffmpeg"]},
    "ffprobe": {"kind": "exe", "required": True, "path_names": ["ffprobe"]},
    "orc": {"kind": "exe", "required": True, "path_names": ["orc-cli", "orc"]},
    "vspipe": {"kind": "exe", "required": True, "path_names": ["vspipe"]},
    "rife_engine": {"kind": "exe", "required": False, "path_names": ["rife-ncnn-vulkan"]},
    "rife_model_dir": {"kind": "dir", "required": False, "path_names": []},
    "sevenzip": {"kind": "exe", "required": False, "path_names": ["7z", "7zz", "7za"]},
    "curl": {"kind": "exe", "required": False, "path_names": ["curl"]},
}

#: Tools fstk once configured and no longer uses; dropped from a profile on load, with a note.
#: ``vs_plugin_dir``: the VapourSynth plugins come with the pip wheels and autoload, so the folder is never
#: consulted; what matters is the per-plugin probe in ``fstk.tools.requirements`` (``vs plugin <namespace>``).
RETIRED_TOOLS: dict[str, str] = {
    "vs_plugin_dir": "the plugin wheels in fstk's own environment autoload; the `vs plugin ...` probes check them",
    "avisynth_plugin_dir": "AviSynth is retired (VapourSynth on every platform)",
    "avisynth_loader": "AviSynth is retired (VapourSynth on every platform)",
}

def _without_retired(tools: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """A profile's tools without the ones fstk no longer uses, plus a note for each one dropped.

    An old profile keeps working: a retired entry is ignored rather than refused, and the note says why it went.
    """
    kept = {k: v for k, v in tools.items() if k not in RETIRED_TOOLS}
    notes = [f"[retired] tools.{k} dropped: {RETIRED_TOOLS[k]}" for k in tools if k in RETIRED_TOOLS]
    return kept, notes


#: Logical artifact roots and their default sub-folder of ``media_root/<episode-slug>``.
MEDIA_ROOTS: dict[str, str] = {
    "work": "work",
    "probe": "probe",
    "scripts": "scripts",
    "audio": "audio",
    "subs": "subs",
    "out": "out",
    "prores": "master",
    "stack": "stack",
    "upscale": "upscale",
}
#: Durable small-state roots inside ``data_root/episodes/<series>/<code>``.
STATE_ROOTS = ("manifest", "logs", "plans", "reports", "evidence", "approvals", "references")
#: Read-only source roots.
SOURCE_ROOTS = ("downloads_root", "dvd_root", "cut_pad_images")
ROOT_ALIASES = {"avs": "scripts"}


def default_app_home() -> Path:
    """Where fstk keeps its own state and working media unless the owner chooses other folders.

    The user's home on the local machine (``C:\\Users\\<you>\\FromStunToKill`` on Windows): never a network or
    USB share, and never next to the captures. ``FSTK_HOME`` overrides it.
    """
    env = os.environ.get("FSTK_HOME")
    return Path(env).expanduser() if env else Path.home() / "FromStunToKill"


def default_data_root() -> Path:
    return default_app_home() / "data"


def default_media_root() -> Path:
    return default_app_home() / "work"


def episodes_under(root: Path) -> list[Path]:
    """Episode folders already holding work under a data or media root (used before moving a root)."""
    if not root.is_dir():
        return []
    found = [p for p in (root / "episodes").glob("*/*") if p.is_dir()] if (root / "episodes").is_dir() else []
    found += [p for p in root.iterdir() if p.is_dir() and "-" in p.name and any(p.iterdir())]
    return sorted(set(found))


def change_folders(profile: Profile, *, data_root: str | os.PathLike | None = None,
                   media_root: str | os.PathLike | None = None, roots: Mapping[str, str | None] | None = None,
                   force: bool = False) -> list[tuple[str, str | None, str | None]]:
    """Point the profile at other state/working folders and save it. Returns (key, old, new) per change.

    ``roots`` sets a per-kind override (``{"out": "D:/Deliveries"}``) or clears one (``None``/``""``). Refuses
    (ConfigError) when work already exists under a folder being changed, unless ``force``: manifests record
    paths relative to these folders, so episodes left behind would no longer be found.
    """
    changes: list[tuple[str, Path | None, Path | None]] = []
    if data_root and Path(data_root).absolute() != profile.data_root:
        changes.append(("data_root", profile.data_root, Path(data_root).absolute()))
    if media_root and Path(media_root).absolute() != profile.media_root:
        changes.append(("media_root", profile.media_root, Path(media_root).absolute()))
    overrides = dict(profile.roots)
    for name, value in (roots or {}).items():
        if name not in MEDIA_ROOTS:
            raise ConfigError(f"unknown working-folder kind {name!r}; one of {', '.join(MEDIA_ROOTS)}")
        old = Path(overrides[name]) if overrides.get(name) else None
        new = Path(value).absolute() if value else None
        if old == new:
            continue
        if new is None:
            overrides.pop(name, None)
        else:
            overrides[name] = str(new)
        changes.append((f"roots.{name}", old, new))
    busy: list[Path] = []
    for key, old, _new in changes:
        if old is None:
            continue
        if key in ("data_root", "media_root"):
            busy += episodes_under(old)
        elif old.is_dir():
            busy += [x for x in old.iterdir() if x.is_dir()]
    if busy and not force:
        raise ConfigError("work already exists under a folder being changed (" + ", ".join(map(str, busy[:4]))
                          + "); those episodes would no longer be found. Move them to the new place yourself, "
                            "or change it anyway (force)")
    for key, _old, new in changes:
        if key == "data_root":
            profile.data_root = new
        elif key == "media_root":
            profile.media_root = new
        if new is not None:
            new.mkdir(parents=True, exist_ok=True)
    profile.roots = overrides
    if changes:
        profile.save()
    return [(k, str(o) if o else None, str(n) if n else None) for k, o, n in changes]


def _fstk_environment(path: Path) -> Path | None:
    """The environment folder ``path`` (``<env>/Scripts|bin/<exe>``) lives in, if it is one fstk made:
    ``…/venvs/<key>`` (the launcher's) or the older ``…/venv``."""
    env = path.parent.parent
    if (env / "pyvenv.cfg").is_file() and (env.parent.name == "venvs" or env.name == "venv"):
        return env
    return None


def _own_environment_vspipe(configured: Path) -> Path | None:
    """The vspipe of the environment this process runs in, when the profile names another fstk environment's.

    A profile written by ``setup`` records the vspipe of the environment that existed then. The launcher makes one
    environment per copy of the app (moving the app folder makes a new one), and a pip VapourSynth is configured
    per environment, so the old one's vspipe stops working (seen on the Windows machine: exit 1, "Python executable
    and library path couldn't be determined"). Follow the running environment instead of failing.
    """
    import sys

    if sys.prefix == sys.base_prefix:
        return None
    mine = Path(sys.prefix)
    env = configured.parent.parent
    # the configured one may already be deleted: recognise an fstk environment by where it sits
    theirs = env if (env.parent.name == "venvs" or env.name == "venv") and configured.parent.name in ("Scripts", "bin") \
        else None
    if theirs is None or _fstk_environment(mine / "Scripts" / "x") is None or theirs == mine:
        return None
    for sub, exe in (("Scripts", "vspipe.exe"), ("bin", "vspipe")):
        cand = mine / sub / exe
        if cand.is_file():
            return cand
    return None


def change_sources(profile: Profile, sources: Mapping[str, str | None]) -> list[tuple[str, str | None, str | None]]:
    """Set or clear the read-only source folders (``downloads_root``, ``dvd_root``) and save the profile.

    These hold the owner's captures and DVD rips: nothing is created or moved there, and a share that is not
    mounted right now is still accepted (the path is kept as typed). Returns (key, old, new) per change.
    """
    changes: list[tuple[str, str | None, str | None]] = []
    current = dict(profile.sources)
    for name, value in sources.items():
        if name not in ("downloads_root", "dvd_root"):
            raise ConfigError(f"unknown source folder {name!r}; one of downloads_root, dvd_root")
        new = (value or "").strip().strip('"').strip("'") or None
        old = current.get(name) or None
        if old == new:
            continue
        if new is None:
            current.pop(name, None)
        else:
            current[name] = new
        changes.append((f"sources.{name}", old, new))
    if changes:
        profile.sources = current
        profile.save()
    return changes


def change_shared_root(profile: Profile, value: str | os.PathLike | None) -> list[tuple[str, str | None, str | None]]:
    """Set or clear the shared workspace folder (hand-offs between machines) and save the profile.

    Nothing is created here: fstk makes its own ``devices`` folder inside it the first time the shared workspace is
    used. A share that is offline right now is accepted. ConfigError when it is not an absolute path or is the data
    folder (device state is never shared). Returns ``[(key, old, new)]`` or ``[]`` when nothing changed.
    """
    text = str(value or "").strip().strip('"').strip("'")
    old = profile.shared_root
    new = Path(text) if text else None
    if old == new:
        return []
    profile.shared_root = new
    try:
        profile.validate()
    except ConfigError:
        profile.shared_root = old
        raise
    profile.save()
    return [("shared_root", str(old) if old else None, str(new) if new else None)]


def default_profile_path() -> Path:
    env = os.environ.get("FSTK_PROFILE")
    if env:
        return Path(env).expanduser()
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "FromStunToKill" / "machine.json"


def current_os() -> str:
    return {"Windows": "windows", "Darwin": "macos"}.get(platform.system(), "linux")


@dataclass
class Limits:
    min_free_gb: float = 80.0
    #: Concurrent steps per resource class (network, prep, compute, gpu).
    resources: dict[str, int] = field(default_factory=lambda: {"network": 1, "prep": 1,
                                                                "compute": 1, "gpu": 1})

    def __post_init__(self) -> None:
        if not (self.min_free_gb > 0):
            raise ConfigError("limits.min_free_gb must be a positive reserve")
        for name, value in self.resources.items():
            if type(value) is not int or value < 1:
                raise ConfigError(f"limits.resources.{name} must be an integer >= 1")


@dataclass
class Profile:
    machine: str
    os: str
    data_root: Path
    media_root: Path
    tools: dict[str, str] = field(default_factory=dict)
    roots: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    downloads_template: str = "{series}-{episode}-LaserDisc-Copies"
    shared_root: Path | None = None
    instance_id: str = ""
    limits: Limits = field(default_factory=Limits)
    allow_newer_packages: bool = False
    notes: list[str] = field(default_factory=list)
    catalog_path: str = ""  # optional refreshed Archive catalog snapshot (default: bundled)
    #: Named volumes reachable from several machines (e.g. the capture disk shared from the Flex):
    #: the same name on every machine, each with its own mount path (ADR 0002 §3).
    volumes: dict[str, str] = field(default_factory=dict)
    #: Default output settings per group (``video``, ``audio``; see ``fstk.config.output``).
    output: dict[str, dict] = field(default_factory=dict)
    #: App preferences: operator name, notifications, catalog check interval, log level, process priority.
    ui: dict[str, object] = field(default_factory=dict)
    path: Path | None = None

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, path: Path | None = None) -> Profile:
        if data.get("schema") != SCHEMA:
            raise ConfigError(f"{path or 'profile'}: schema must be {SCHEMA!r} "
                              f"(v38 machine.json files are converted with `fstk profile import`)")
        tools, retired = _without_retired(data.get("tools") or {})
        try:
            limits_raw = dict(data.get("limits") or {})
            limits = Limits(min_free_gb=float(limits_raw.get("min_free_gb", 80)),
                            resources={**Limits().resources, **(limits_raw.get("resources") or {})})
            prof = cls(machine=str(data["machine"]), os=str(data.get("os") or current_os()),
                       data_root=Path(data["data_root"]), media_root=Path(data["media_root"]),
                       tools=tools, roots=dict(data.get("roots") or {}),
                       sources=dict(data.get("sources") or {}),
                       downloads_template=str(data.get("downloads_template")
                                              or cls.downloads_template),
                       shared_root=Path(data["shared_root"]) if data.get("shared_root") else None,
                       instance_id=str(data.get("instance_id") or ""), limits=limits,
                       allow_newer_packages=bool(data.get("allow_newer_packages", False)),
                       notes=[*(data.get("notes") or []), *retired],
                       catalog_path=str(data.get("catalog_path") or ""),
                       volumes=dict(data.get("volumes") or {}),
                       output={k: dict(v) for k, v in (data.get("output") or {}).items()},
                       ui=dict(data.get("ui") or {}), path=path)
        except KeyError as exc:
            raise ConfigError(f"{path or 'profile'}: missing required key {exc.args[0]!r}") from None
        prof.validate()
        return prof

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA, "machine": self.machine, "os": self.os,
            "instance_id": self.instance_id,
            "data_root": str(self.data_root), "media_root": str(self.media_root),
            "shared_root": str(self.shared_root) if self.shared_root else "",
            "tools": dict(sorted(self.tools.items())), "roots": dict(sorted(self.roots.items())),
            "sources": dict(sorted(self.sources.items())),
            "downloads_template": self.downloads_template,
            "limits": {"min_free_gb": self.limits.min_free_gb,
                       "resources": dict(sorted(self.limits.resources.items()))},
            "allow_newer_packages": self.allow_newer_packages, "notes": list(self.notes),
            **({"catalog_path": self.catalog_path} if self.catalog_path else {}),
            **({"volumes": dict(sorted(self.volumes.items()))} if self.volumes else {}),
            **({"output": {k: dict(v) for k, v in sorted(self.output.items())}} if self.output else {}),
            **({"ui": dict(sorted(self.ui.items()))} if self.ui else {}),
        }

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> Profile:
        p = Path(path) if path else default_profile_path()
        if not p.exists():
            raise ConfigError(f"no machine profile at {p}; create one with `fstk profile init` "
                              "or convert a v38 machine.json with `fstk profile import`")
        try:
            doc = read_json(p)
        except ValueError as exc:
            bak = p.with_name(p.name + ".bak")
            hint = f" The previous version is kept at {bak}; copy it over {p.name} to go back." if bak.is_file() else ""
            raise ConfigError(f"{exc}.{hint}") from None
        return cls.from_dict(doc, path=p)

    def save(self, path: str | os.PathLike | None = None) -> Path:
        p = Path(path or self.path or default_profile_path())
        atomic_write_json(p, self.to_dict(), backup=True)      # the previous version stays as <name>.bak
        self.path = p
        return p

    # ------------------------------------------------------------------ validation
    def validate(self) -> None:
        if self.os not in ("windows", "macos", "linux"):
            raise ConfigError(f"os must be windows, macos or linux, got {self.os!r}")
        for label, value in (("data_root", self.data_root), ("media_root", self.media_root)):
            if not _is_absolute(value, self.os):
                raise ConfigError(f"{label} must be an absolute path, got {value!s}")
        for name, value in self.roots.items():
            canonical = ROOT_ALIASES.get(name, name)
            if canonical not in MEDIA_ROOTS:
                raise ConfigError(f"roots.{name}: unknown media root (known: {', '.join(MEDIA_ROOTS)})")
            if value and not _is_absolute(Path(value), self.os):
                raise ConfigError(f"roots.{name} must be absolute, got {value!r}")
        for name in self.tools:
            if name not in KNOWN_TOOLS:
                raise ConfigError(f"tools.{name}: unknown tool (known: {', '.join(KNOWN_TOOLS)})")
        for name in self.sources:
            if name not in SOURCE_ROOTS:
                raise ConfigError(f"sources.{name}: unknown source root (known: {', '.join(SOURCE_ROOTS)})")
        if "{episode}" not in self.downloads_template and "{code}" not in self.downloads_template:
            raise ConfigError("downloads_template must contain {episode}")
        if self.shared_root and self.shared_root == self.data_root:
            raise ConfigError("shared_root must differ from data_root (device state is never shared)")
        if self.shared_root and not _is_absolute(self.shared_root, self.os):
            raise ConfigError(f"shared_root must be an absolute path, got {self.shared_root!s}")
        if self.instance_id and not re.fullmatch(r"[0-9A-F]{8}", self.instance_id):
            raise ConfigError("instance_id must be 8 upper-case hexadecimal characters (or empty)")
        for name, value in self.volumes.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name):
                raise ConfigError(f"volumes.{name}: names are lower-case letters, digits, '-' and '_'")
            if not _is_absolute(Path(value), self.os):
                raise ConfigError(f"volumes.{name} must be an absolute path, got {value!r}")

    # ------------------------------------------------------------------ resolution
    def tool(self, name: str, *, allow_path_lookup: bool = True) -> Path:
        """Resolve a tool path; profile entries win, PATH lookup is a fallback."""
        if name not in KNOWN_TOOLS:
            raise ConfigError(f"unknown tool {name!r}")
        spec = KNOWN_TOOLS[name]
        configured = self.tools.get(name) or ""
        if configured:
            p = Path(configured)
            if name == "vspipe" and (own := _own_environment_vspipe(p)) is not None:
                return own              # an older fstk environment's vspipe, broken or already deleted
            ok = p.is_dir() if spec["kind"] == "dir" else p.is_file()
            if not ok:
                elsewhere = next((w for c in spec["path_names"] if (w := shutil.which(c))), None)
                fix = (f"; {Path(elsewhere).name} is also on PATH at {elsewhere}, so setup can switch to it"
                       if elsewhere else "")
                raise ConfigError(f"tools.{name} points at a missing {spec['kind']}: {p} (the file was there when setup "
                                  f"ran; reinstall it or run `FromStunToKill setup` to find or install it again{fix})")
            return p
        if allow_path_lookup:
            for candidate in spec["path_names"]:
                found = shutil.which(candidate)
                if found:
                    return Path(found)
        where = self.path or default_profile_path()
        raise ConfigError(f"tool {name!r} is not configured in {where} and was not found on PATH")

    def episode_state_dir(self, episode: EpisodeId) -> Path:
        return self.data_root / "episodes" / episode.series / episode.code

    def episode_media_dir(self, episode: EpisodeId) -> Path:
        return self.media_root / episode.slug

    def root(self, name: str, episode: EpisodeId) -> Path:
        """Resolve a logical root for one episode."""
        name = ROOT_ALIASES.get(name, name)
        if name in STATE_ROOTS:
            return self.episode_state_dir(episode) / name
        if name in MEDIA_ROOTS:
            override = self.roots.get(name)
            if override:
                base = Path(override)
                component = episode.artifact_component
                return base / component / episode.code if component else base / episode.code
            return self.episode_media_dir(episode) / MEDIA_ROOTS[name]
        if name in SOURCE_ROOTS:
            value = self.sources.get(name)
            if not value:
                raise ConfigError(f"sources.{name} is not set in the machine profile")
            return Path(value)
        raise ConfigError(f"unknown root {name!r}")

    def downloads_dir(self, episode: EpisodeId) -> Path:
        base = self.sources.get("downloads_root")
        if not base:
            raise ConfigError("sources.downloads_root is not set in the machine profile: set it in Settings → Folders "
                              "to the folder that holds the LaserDisc capture folders (e.g. DS9-S01E20-LaserDisc-Copies), "
                              "or run `FromStunToKill setup`")
        name = self.downloads_template.format(series=episode.series, episode=episode.code,
                                              code=episode.code)
        return Path(base) / name


def _is_absolute(path: Path | str, os_name: str) -> bool:
    raw = str(path)
    if os_name == "windows":
        return (len(raw) >= 3 and raw[1] == ":" and raw[2] in "\\/") or raw.startswith("\\\\") or (
            len(raw) == 2 and raw[1] == ":")
    return raw.startswith("/") or (sys.platform == "win32" and Path(raw).is_absolute())


# ---------------------------------------------------------------------- v38 conversion
V38_TOOL_MAP = {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "orc": "orc", "rife_engine": "rife_engine",
                "rife_model_dir": "rife_model_dir", "sevenzip": "sevenzip", "curl": "curl",
                "vspipe": "vspipe"}
OS_ALIASES = {"windows": "windows", "win32": "windows", "darwin": "macos", "macos": "macos",
              "mac": "macos", "linux": "linux"}
V38_DROPPED_TOOLS = ("avisynth_plugin_dir", "avisynth_loader")


def convert_v38_profile(v38: Mapping[str, Any], *, data_root: str, media_root: str | None = None
                        ) -> tuple[dict[str, Any], list[str]]:
    """Convert a v38 ``machine.json`` dict. Returns (new profile dict, conversion notes).

    v38 had one folder per root type shared by all episodes; the new layout is per-episode.
    v38 roots are therefore kept as *overrides* only when they differ from the new default,
    and the caller is told what changed.
    """
    notes: list[str] = []
    os_name = OS_ALIASES.get(str(v38.get("os") or "").lower(), current_os())
    tools = {new: v38["tools"][old] for old, new in V38_TOOL_MAP.items()
             if (v38.get("tools") or {}).get(old)}
    for dropped in V38_DROPPED_TOOLS:
        if (v38.get("tools") or {}).get(dropped):
            notes.append(f"tools.{dropped} dropped: AviSynth is retired (VapourSynth on every platform)")
    if "vspipe" not in tools:
        notes.append("tools.vspipe is new and must be set (or be on PATH)")
    for unused in ("pwsh", "python"):
        if (v38.get("tools") or {}).get(unused):
            notes.append(f"tools.{unused} not carried over (PowerShell retired; fstk runs in its own interpreter)")
    roots_v38 = dict(v38.get("roots") or {})
    work = roots_v38.get("work") or ""
    pure = PureWindowsPath if os_name == "windows" else PurePosixPath
    media = media_root or (str(pure(work).parent) if work else "")
    if not media:
        raise ConfigError("cannot infer media_root from v38 roots.work; pass media_root explicitly")
    sources = {k: roots_v38[k] for k in ("downloads_root", "dvd_root") if roots_v38.get(k)}
    template = roots_v38.get("downloads_template") or ""
    if template and "{episode}" not in template:
        notes.append(f"v38 downloads_template {template!r} is episode-specific; replaced with the default")
        template = ""
    for retired in ("runbook_dir", "archive_share", "avs"):
        if roots_v38.get(retired):
            notes.append(f"roots.{retired} not carried over")
    limits_v38 = dict(v38.get("limits") or {})
    heavy = int(limits_v38.get("heavy_jobs") or 1)
    profile = {
        "schema": SCHEMA, "machine": v38.get("machine") or "UNNAMED", "os": os_name,
        "instance_id": (v38.get("shared") or {}).get("device_instance_id") or "",
        "data_root": data_root, "media_root": media, "shared_root": "",
        "tools": tools, "roots": {}, "sources": sources,
        "downloads_template": template or Profile.downloads_template,
        "limits": {"min_free_gb": float(limits_v38.get("min_free_gb") or 80),
                   "resources": {"network": 1, "prep": 1, "compute": heavy, "gpu": 1}},
        "allow_newer_packages": False,
        "notes": [*list(v38.get("notes") or []), *[f"[import] {n}" for n in notes]],
    }
    return profile, notes
