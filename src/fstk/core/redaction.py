"""Redaction of device-specific paths from text written to a shared tree.

The shared release tree is an SMB share that other machines (the Mac) also write. Anything
published there must not leak this device's absolute paths, user name, or host name.
Replacements are longest-first so nested roots redact to their most specific token.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import PurePath


def _variants(path: str) -> set[str]:
    raw = str(path)
    forms = {raw, raw.replace("\\", "/"), raw.replace("/", "\\")}
    if raw.endswith(("/", "\\")):
        forms |= {f.rstrip("/\\") for f in forms}
    return {f for f in forms if len(f) >= 3}


class Redactor:
    def __init__(self, replacements: Mapping[str, str] | Iterable[tuple[str, str]] = ()):
        pairs = replacements.items() if isinstance(replacements, Mapping) else replacements
        expanded: list[tuple[str, str]] = []
        for needle, token in pairs:
            if not needle:
                continue
            for form in _variants(str(needle)):
                expanded.append((form, token))
        expanded.sort(key=lambda item: len(item[0]), reverse=True)
        self._pairs = expanded
        self._pattern = (re.compile("|".join(re.escape(n) for n, _ in expanded),
                                    re.IGNORECASE if os.name == "nt" else 0)
                         if expanded else None)
        self._lookup = {n.lower() if os.name == "nt" else n: t for n, t in expanded}

    @classmethod
    def for_roots(cls, roots: Mapping[str, str | PurePath], *, extra: Mapping[str, str] | None = None,
                  include_home: bool = True) -> Redactor:
        pairs = {str(path): f"<{name}>" for name, path in roots.items() if path}
        if include_home:
            pairs.setdefault(os.path.expanduser("~"), "<home>")
        if extra:
            pairs.update(extra)
        return cls(pairs)

    def __call__(self, text: str) -> str:
        if not self._pattern:
            return text

        def swap(match: re.Match) -> str:
            key = match.group(0).lower() if os.name == "nt" else match.group(0)
            return self._lookup.get(key, "<redacted>")

        return self._pattern.sub(swap, text)
