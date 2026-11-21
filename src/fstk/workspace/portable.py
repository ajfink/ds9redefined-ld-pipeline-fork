"""Machine-independent paths for state that travels between devices (ADR 0002 §3).

A device's absolute paths are replaced by tokens naming *what* the folder is, and expanded on
the receiving device with its own profile:

    {data_root}, {media_root}           the profile roots
    {root:<name>}                       a media-root override (``roots.stack`` …)
    {source:<name>}                     a source root (``downloads_root``, ``dvd_root`` …)
    {volume:<name>}                     a named volume in ``profile.volumes`` (e.g. the Flex's
                                        capture disk: ``\\\\LENOVOFLEX\\e hdd`` here, ``/Volumes/e hdd``
                                        on the Mac)

Only string values that *are* a path under one of those prefixes are tokenised (the tail is
stored with forward slashes). Prose that merely mentions a path is left alone; ``Redactor`` covers
free text one way. Windows paths compare case-insensitively and with either separator.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

TOKEN_RE = re.compile(r"^\{(data_root|media_root|root:[a-z_]+|source:[a-z_]+|volume:[a-z0-9_-]+)\}(?:/(.*))?$")


def _norm(path: str, windows: bool) -> str:
    text = path.replace("\\", "/") if windows else path
    text = text.rstrip("/") if len(text) > 1 else text
    return text.casefold() if windows else text


@dataclass
class PathMap:
    """Token ↔ absolute-path table for one machine profile."""

    windows: bool
    table: dict[str, str]                         # token -> absolute path (native spelling)
    unmapped: set[str] = field(default_factory=set)

    @classmethod
    def for_profile(cls, profile) -> PathMap:
        table: dict[str, str] = {"data_root": str(profile.data_root), "media_root": str(profile.media_root)}
        for name, value in profile.roots.items():
            if value:
                table[f"root:{name}"] = str(value)
        for name, value in profile.sources.items():
            if value:
                table[f"source:{name}"] = str(value)
        for name, value in profile.volumes.items():
            table[f"volume:{name}"] = str(value)
        return cls(profile.os == "windows", table)

    # ------------------------------------------------------------------ encode
    def encode_path(self, value: str) -> str | None:
        """``value`` as a token path, or None if it is not under any known root."""
        norm = _norm(value, self.windows)
        best = None
        for token, base in self.table.items():
            b = _norm(base, self.windows)
            if (norm == b or norm.startswith(b + "/")) and (best is None or len(b) > len(best[1])):
                best = (token, b)
        if best is None:
            return None
        token, b = best
        text = value.replace("\\", "/") if self.windows else value
        tail = text[len(b):].strip("/")
        return "{" + token + "}" + (f"/{tail}" if tail else "")

    # ------------------------------------------------------------------ decode
    def decode_path(self, value: str) -> str | None:
        m = TOKEN_RE.match(value)
        if not m:
            return None
        base = self.table.get(m.group(1))
        if base is None:
            self.unmapped.add(m.group(1))
            return None
        pure = PureWindowsPath if self.windows else PurePosixPath
        tail = [part for part in (m.group(2) or "").split("/") if part]
        return str(pure(base, *tail))


def _looks_absolute(value: str) -> bool:
    return value.startswith(("/", "\\\\")) or bool(re.match(r"^[A-Za-z]:[\\/]", value))


def walk(obj: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(obj, dict):
        return {k: walk(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [walk(v, fn) for v in obj]
    if isinstance(obj, str):
        return fn(obj)
    return obj


def encode(obj: Any, pmap: PathMap, *, leftovers: set[str] | None = None) -> Any:
    """Tokenise every whole-path string; absolute paths under no known root go to ``leftovers``."""
    def fn(s: str) -> str:
        if not _looks_absolute(s):
            return s
        enc = pmap.encode_path(s)
        if enc is None and leftovers is not None:
            leftovers.add(s)
        return enc or s
    return walk(obj, fn)


def decode(obj: Any, pmap: PathMap) -> Any:
    """Expand tokens with this machine's roots; tokens it lacks stay as text (see ``pmap.unmapped``)."""
    def fn(s: str) -> str:
        if not s.startswith("{"):
            return s
        dec = pmap.decode_path(s)
        return dec if dec is not None else s
    return walk(obj, fn)
