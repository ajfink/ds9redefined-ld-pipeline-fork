"""UTC time helpers. All persisted timestamps are timezone-aware UTC."""
from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def utc_stamp(when: datetime | None = None) -> str:
    """Compact sortable stamp for file names: ``20261121T063000123456Z``."""
    return (when or utc_now()).astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp is not timezone-aware: {value!r}")
    return parsed.astimezone(UTC)
