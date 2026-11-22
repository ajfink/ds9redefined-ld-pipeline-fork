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


#: Disk estimates from real data (owner's Legion): the S01E20 stack master is 287 KB/frame;
#: delivered v38 episodes are 4.1–4.7 GiB for ~65,200 frames (≈ 72 KB/frame). Margins included.
MASTER_BYTES_PER_STACK_FRAME = 360_000
ENCODE_BYTES_PER_OUTPUT_FRAME = 110_000


def delivery_plan(manifest, *, job: str | None = None) -> dict[str, Any]:
    """Everything after intake, in order, for the episode's DVD choice.

    DVD:    stack-render → dvd-probe → dvd-alignment → cadence-evidence → video-trim →
            dvd-ld-audio → dvd-retime → pad-audit → pad-repair → encode-final → remux → qc-report
    no DVD: stack-render → cadence-evidence → video-trim → dvd-free-audio → pad-audit → pad-repair →
            encode-final → remux → qc-report

    Every step's prerequisites are still gated by the engine; finished steps are reused on resume.
    The only human review left is the A/V check after ``qc-report``.
    """
    from fstk.core.errors import FstkError

    ep = manifest.episode
    dvd = manifest.get("mode.dvd_reference_available")
    if dvd is None:
        raise FstkError("record whether a DVD reference exists (mode.dvd_reference_available) first")
    stack_frames = manifest.get("stack.frames")
    if not stack_frames:
        raise FstkError("run intake first: stack.frames is not measured")
    master_gib = stack_frames * MASTER_BYTES_PER_STACK_FRAME / 2**30
    out_frames = manifest.get("video.frames") or int(stack_frames * 0.8)
    encode_gib = out_frames * ENCODE_BYTES_PER_OUTPUT_FRAME / 2**30
    steps: list[dict[str, Any]] = []

    def add(sid: str, stage: str, after: list[str], **params) -> None:
        steps.append({"id": sid, "stage": stage, "after": after, "params": params})

    add("stack-render", "stack-render", [], planned_gib=round(master_gib, 2),
        estimate_basis=f"{stack_frames} stack frames × {MASTER_BYTES_PER_STACK_FRAME} B (S01E20 master: 287 KB/frame)")
    add("cadence", "cadence-evidence", ["stack-render"])
    if dvd:
        add("dvd-probe", "dvd-probe", [])
        add("dvd-alignment", "dvd-alignment", ["stack-render", "dvd-probe"])
        add("video-trim", "video-trim", ["cadence", "dvd-alignment"])
        add("ld-audio", "dvd-ld-audio", ["video-trim"])
        add("retime", "dvd-retime", ["ld-audio"])
        audio_done = ["retime"]
        trim_done = ["video-trim"]
    else:
        add("video-trim", "video-trim", ["cadence"])
        add("ld-audio", "dvd-free-audio", ["video-trim"])
        audio_done = ["ld-audio"]
        trim_done = ["video-trim"]
    add("pad-audit", "pad-audit", trim_done)
    add("pad-repair", "pad-repair", ["pad-audit"] + (["dvd-alignment"] if dvd else []))
    add("encode", "encode-final", ["pad-repair"], planned_gib=round(encode_gib, 2),
        estimate_basis=f"{out_frames} output frames × {ENCODE_BYTES_PER_OUTPUT_FRAME} B (v38 deliveries ≈ 72 KB/frame)")
    add("remux", "remux", ["encode", *audio_done])
    add("qc", "qc-report", ["remux"])
    plan = {"schema": PLAN_SCHEMA, "episode": ep.canonical,
            "job": job or f"delivery-{ep.slug}-{utc_stamp()[:15].lower()}", "steps": steps}
    validate_plan(plan)
    return plan
