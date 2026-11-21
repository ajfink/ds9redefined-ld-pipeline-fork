"""Standard plan templates. Plans are data; these only assemble the usual step order."""
from __future__ import annotations

from typing import Any

from fstk.core.identity import EpisodeId
from fstk.core.timeutil import utc_stamp

from .engine import PLAN_SCHEMA, validate_plan


def expects_efm(manifest) -> bool:
    """EFM steps belong in the plan unless we already know the episode has no raw EFM."""
    caps = manifest.captures
    if caps:
        return any((c.get("files") or {}).get("efm") for c in caps)
    count = (manifest.doc["sources"].get("catalog") or {}).get("raw_efm_count")
    return count != 0


def intake_plan(episode: str | EpisodeId, *, download: bool, job: str | None = None,
                include_stack_reference: bool = True, include_efm: bool = True) -> dict[str, Any]:
    """download → inventory → (stack-reference ∥ efm-decode) → efm-select.

    EFM decoding only needs the inventory, so it runs beside the stack-reference refresh loop
    (different resources permitting). Selection needs both: ``audio.pad_frames`` comes from the
    measured alignment map. ``include_efm`` adds the EFM steps only when captures carry EFM.
    """
    ep = episode if isinstance(episode, EpisodeId) else EpisodeId.parse(episode)
    steps: list[dict[str, Any]] = []
    if download:
        steps.append({"id": "download", "stage": "archive-download", "params": {}, "after": []})
    steps.append({"id": "inventory", "stage": "capture-inventory", "params": {},
                  "after": ["download"] if download else []})
    if include_stack_reference:
        steps.append({"id": "stack-reference", "stage": "stack-reference", "params": {}, "after": ["inventory"]})
    if include_efm:
        steps.append({"id": "efm-decode", "stage": "efm-decode", "params": {}, "after": ["inventory"]})
        if include_stack_reference:
            steps.append({"id": "efm-select", "stage": "efm-select", "params": {},
                          "after": ["stack-reference", "efm-decode"]})
    plan = {"schema": PLAN_SCHEMA, "episode": ep.canonical,
            "job": job or f"intake-{ep.slug}-{utc_stamp()[:15].lower()}", "steps": steps}
    validate_plan(plan)
    return plan
