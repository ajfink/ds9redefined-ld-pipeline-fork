"""``av-correction``: turn the A/V check's per-chunk verdicts into the next audio timing correction.

Owner decision 2026-11-21: the only human review is the delivery A/V check over several 15 s
chunks. "Audio too early" / "audio too late" marks trigger a rerun in which the app corrects the
audio itself: this stage records the round and the next correction
(``fstk.media.av_correction``), and the audio stage, remux and QC that follow it in the rerun
plan apply it.
"""
from __future__ import annotations

from pathlib import Path

from fstk.core.errors import GateError
from fstk.core.fsio import atomic_write_json
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.media.av_correction import ContradictoryVerdicts, next_correction

from .base import Stage, StepOutput, StepPlan, register

#: Tracks whose timing a correction can change today. The DVD 5.1 joins once retime is ported.
CORRECTABLE = {"ld": "LD stereo (default track)"}


@register
class AvCorrection(Stage):
    id = "av-correction"
    title = "Apply the A/V check's verdicts"
    description = ("Records one A/V review round (per-chunk in sync / audio early / audio late) and solves the "
                   "next offset/drift correction; the audio, remux and QC steps after it apply the correction.")
    writes = ("audio.av_correction",)
    resource = "prep"

    def plan(self, ctx, params):
        track = params.get("track", "ld")
        if track not in CORRECTABLE:
            raise GateError(f"timing corrections for track {track!r} are not supported yet; correctable: "
                            f"{sorted(CORRECTABLE)}")
        verdicts = params.get("verdicts") or []
        if not verdicts and not params.get("reset"):
            raise GateError("no chunk verdicts given")
        report = ctx.root("reports") / f"av-correction-{utc_stamp()[:15].lower()}.json"
        state = (ctx.manifest.get("audio.av_correction") or {}).get(track)
        try:
            nxt, new_state = (None, None) if params.get("reset") else next_correction(state, verdicts)
        except ContradictoryVerdicts as exc:
            raise GateError(str(exc)) from None
        except ValueError as exc:
            raise GateError(f"invalid verdicts: {exc}") from None
        summary = ("reset to no correction" if nxt is None else
                   f"{track}: offset {nxt.offset_ms:+.1f} ms, drift {nxt.drift_ppm:+.1f} ppm")
        return StepPlan(self.id, params, outputs=[report], summary=summary,
                        data={"track": track, "state": new_state, "report": str(report),
                              "qc_report": params.get("qc_report")})

    def run(self, ctx, plan):
        d = plan.data
        record = dict(ctx.manifest.get("audio.av_correction") or {})
        if d["state"] is None:
            record.pop(d["track"], None)
        else:
            record[d["track"]] = {**d["state"], "qc_report": d["qc_report"], "updated_utc": utc_now_iso()}
        atomic_write_json(d["report"], {"schema": "fstk-av-correction-round-v1", "episode": str(ctx.episode),
                                        "track": d["track"], "qc_report": d["qc_report"], "result": record.get(d["track"]),
                                        "created_utc": utc_now_iso()})
        out = StepOutput(report=Path(d["report"]))
        cur = (record.get(d["track"]) or {}).get("current") or {}
        out.measure("audio.av_correction", record,
                    note=(f"{d['track']}: offset {cur.get('offset_ms', 0):+.1f} ms, drift {cur.get('drift_ppm', 0):+.1f} ppm"
                          if cur else f"{d['track']}: correction reset"))
        return out
