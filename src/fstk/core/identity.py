"""Series/episode identity.

Canonical identity is ``SERIES:SxxExx`` (e.g. ``DS9:S01E20``). A bare ``SxxExx`` means DS9,
which keeps v38's historical behaviour. Series are a registry rather than a hard-coded pair
so new shows can be added without touching parsing code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import IdentityError

EPISODE_CODE_RE = re.compile(r"^S(\d{2})E(\d{2})$", re.IGNORECASE)
SERIES_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")

DEFAULT_SERIES = "DS9"

#: Known series. ``legacy_paths`` keeps v38's un-namespaced artifact paths for DS9.
SERIES: dict[str, dict] = {
    "DS9": {"title": "Star Trek: Deep Space Nine", "legacy_paths": True},
    "VOY": {"title": "Star Trek: Voyager", "legacy_paths": False},
    "TNG": {"title": "Star Trek: The Next Generation", "legacy_paths": False},
}


def register_series(code: str, title: str, *, legacy_paths: bool = False) -> None:
    code = code.upper()
    if not SERIES_CODE_RE.fullmatch(code):
        raise IdentityError(f"invalid series code {code!r}")
    SERIES[code] = {"title": title, "legacy_paths": legacy_paths}


@dataclass(frozen=True, order=True)
class EpisodeId:
    series: str
    code: str

    @property
    def season(self) -> int:
        return int(self.code[1:3])

    @property
    def number(self) -> int:
        return int(self.code[4:6])

    @property
    def canonical(self) -> str:
        return f"{self.series}:{self.code}"

    @property
    def artifact_component(self) -> str | None:
        """Path namespace for artifacts; ``None`` keeps v38 DS9 paths unchanged."""
        return None if SERIES[self.series]["legacy_paths"] else self.series

    @property
    def slug(self) -> str:
        """Filesystem-safe, lowercase identifier (``ds9-s01e20``)."""
        return f"{self.series}-{self.code}".lower()

    def __str__(self) -> str:
        return self.canonical

    @classmethod
    def parse(cls, value: str, series: str | None = None) -> EpisodeId:
        raw = str(value or "").strip()
        if ":" in raw:
            embedded, raw = raw.split(":", 1)
            if series and embedded.upper() != series.upper():
                raise IdentityError(f"episode identity {value!r} conflicts with series {series!r}")
            series = embedded
        chosen = (series or DEFAULT_SERIES).upper()
        if chosen not in SERIES:
            known = ", ".join(sorted(SERIES))
            raise IdentityError(f"unsupported series {chosen!r}; known series: {known}")
        if not EPISODE_CODE_RE.fullmatch(raw):
            raise IdentityError(f"episode must be SxxExx or SERIES:SxxExx, got {value!r}")
        return cls(chosen, raw.upper())
