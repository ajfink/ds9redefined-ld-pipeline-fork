"""Declared manifest fields.

Every value the pipeline records about an episode is declared here once, with its type,
unit, kind, and v38 location. Nothing else may be written into a manifest: v38's rule
"do not add ad hoc manifest keys" is enforced by the store, not by prose.

Kinds:
  measurement – produced by a stage from evidence; carries provenance.
  choice      – an operator decision (e.g. no-DVD mode, selected capture); carries the actor.
  setting     – configuration of the deliverable (tracks, deliverable key); carries the actor.
  record      – a structured decision/approval document (selection, review); schema-tagged.

An unset value is *absent*. ``None`` is never stored, and zero is never a placeholder.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fstk.core.errors import ManifestError

KINDS = ("measurement", "choice", "setting", "record")


def _is_int(v: Any) -> bool:
    return type(v) is int


def _is_num(v: Any) -> bool:
    return (type(v) in (int, float)) and v == v and v not in (float("inf"), float("-inf"))


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def _is_bool(v: Any) -> bool:
    return type(v) is bool


def _int_list(v: Any) -> bool:
    return isinstance(v, list) and all(type(x) is int for x in v)


def _list(v: Any) -> bool:
    return isinstance(v, list)


def _dict(v: Any) -> bool:
    return isinstance(v, dict)


def _str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "int": _is_int, "number": _is_num, "str": _is_str, "bool": _is_bool,
    "int_list": _int_list, "str_list": _str_list, "list": _list, "dict": _dict,
}


@dataclass(frozen=True)
class FieldDef:
    key: str
    kind: str
    type: str
    description: str
    unit: str = ""
    v38: str | None = None
    minimum: float | None = None
    choices: tuple[Any, ...] = ()
    #: For v38 export: value written when unset (v38 templates use ``[]``/``""`` for some keys).
    v38_unset: Any = None
    extra_check: Callable[[Any], str | None] | None = field(default=None, compare=False)

    def validate(self, value: Any) -> None:
        if value is None:
            raise ManifestError(f"{self.key}: None is not a value; unset the field instead")
        if not TYPE_CHECKS[self.type](value):
            raise ManifestError(f"{self.key}: expected {self.type}, got {type(value).__name__} {value!r}")
        if self.type == "str" and not value.strip():
            raise ManifestError(f"{self.key}: empty string is not a value; unset the field instead")
        if self.minimum is not None and _is_num(value) and value < self.minimum:
            raise ManifestError(f"{self.key}: {value!r} is below the minimum {self.minimum}")
        if self.minimum is not None and self.type == "int_list" and any(x < self.minimum for x in value):
            raise ManifestError(f"{self.key}: list contains values below {self.minimum}")
        if self.choices and value not in self.choices:
            raise ManifestError(f"{self.key}: {value!r} is not one of {self.choices}")
        if self.extra_check:
            problem = self.extra_check(value)
            if problem:
                raise ManifestError(f"{self.key}: {problem}")


def _unique(values: list) -> str | None:
    return None if len(set(values)) == len(values) else "values must be unique"


def _tracks(values: list) -> str | None:
    bad = [v for v in values if v not in ("ld", "51", "srt")]
    if bad:
        return f"unknown track(s) {bad}; allowed ld, 51, srt"
    if values and values[0] != "ld":
        return "LD stereo ('ld') must be present and first (it is the default track)"
    return _unique(values)


_F = FieldDef
FIELDS: tuple[FieldDef, ...] = (
    # ---------------------------------------------------------------- mode / choices
    _F("mode.dvd_reference_available", "choice", "bool",
       "A matching DVD is available as an independent reference", v38="dvd_reference_available"),
    _F("mode.no_dvd_proceed_approved", "choice", "bool",
       "Operator explicitly approved the LD-only (no-DVD) route", v38="no_dvd_proceed_approved"),
    _F("mode.cut_pad_images_choice", "choice", "str",
       "Use of curated cut-PAD stills in no-DVD mode", v38="cut_pad_images_choice",
       choices=("use", "skip", "not-applicable", "not-available")),   # S02E02 (v38) uses not-available
    _F("mode.chroma_output", "setting", "str",
       "Chroma of the delivered video: 422 keeps the master's chroma (default, quality first), "
       "420 is the v38/compatible output", choices=("422", "420")),
    _F("mode.format", "setting", "str", "Episode format", v38="format", choices=("one_part", "two_part")),
    _F("mode.record_status", "setting", "str", "Manifest lifecycle status", v38="record_status"),
    _F("mode.pad_report_label", "setting", "str", "Key used in shared PAD reports", v38="pad_report_label"),
    # ---------------------------------------------------------------- stack
    _F("stack.reference", "measurement", "str", "Capture label Orc recommended as stack reference",
       v38="reference"),
    _F("stack.alignment_map", "measurement", "str",
       "Orc source_align map 'index+offset, ...' in Orc input order", v38="alignment_map"),
    _F("stack.frames", "measurement", "int", "Rendered stack frame count", "frames", "stack.frames", 1),
    _F("stack.duration_s", "measurement", "number", "Rendered stack duration", "s", "stack.duration_s", 0),
    _F("stack.pressing_groups", "measurement", "list", "Capture labels grouped by pressing",
       v38="stack.pressing_groups", v38_unset=[]),
    _F("stack.fit_trusted", "measurement", "bool", "Orc stack fit flagged trustworthy", v38="stack.fit_trusted"),
    _F("stack.fell_back_to_mean", "measurement", "bool", "Stacker fell back to mean", v38="stack.fell_back_to_mean"),
    _F("stack.stack_sigma", "measurement", "number", "Stack noise estimate", v38="stack.stack_sigma", minimum=0),
    _F("stack.best_single_sigma", "measurement", "number", "Best single-capture noise estimate",
       v38="stack.best_single_sigma", minimum=0),
    # ---------------------------------------------------------------- audio
    _F("audio.capture", "choice", "str", "Capture whose EFM audio is used", v38="audio.capture"),
    _F("audio.efm_provenance_required", "setting", "bool", "Selected audio must carry verified EFM provenance",
       v38="audio.efm_provenance_required"),
    _F("audio.pad_frames", "measurement", "int",
       "Selected capture's leading PAD frames from the verified Orc alignment map", "frames",
       "audio.pad_frames", 0),
    _F("audio.skip_s", "measurement", "number", "AudioSkip applied to native LD audio", "s", "audio.skip_s"),
    _F("audio.rate_fit_ppm", "measurement", "number", "LD audio rate error fit", "ppm", "audio.rate_fit_ppm"),
    _F("audio.rate_fit_stderr_ppm", "measurement", "number", "Rate fit standard error", "ppm",
       "audio.rate_fit_stderr_ppm", 0),
    _F("audio.asetrate", "measurement", "number", "asetrate used for rate correction", "Hz", "audio.asetrate", 1),
    _F("audio.corrected_duration_s", "measurement", "number", "Duration after rate correction", "s",
       "audio.corrected_duration_s", 0),
    _F("audio.origin_verified_by_duration", "measurement", "bool", "EFM origin proven by duration identity",
       v38="audio.origin_verified_by_duration"),
    _F("audio.rate_method", "choice", "str", "Accepted rate method", v38="audio.rate_method"),
    _F("audio.duration_candidate_rate_hz", "measurement", "number", "Duration/PAD candidate rate", "Hz",
       "audio.duration_candidate_rate_hz", 1),
    _F("audio.intermediate_asetrate_hz", "measurement", "number", "Intermediate asetrate", "Hz",
       "audio.intermediate_asetrate_hz", 1),
    _F("audio.resampler", "setting", "str", "Resampler recipe", v38="audio.resampler"),
    _F("audio.skip_method", "choice", "str", "How skip_s was established", v38="audio.skip_method"),
    _F("audio.wav0_master_frame", "measurement", "number", "Master frame aligned with WAV sample 0", "frames",
       "audio.wav0_master_frame"),
    _F("audio.endpoint_residual_frames", "measurement", "number", "Endpoint residual", "frames",
       "audio.endpoint_residual_frames"),
    _F("audio.endpoint_tolerance_frames", "setting", "number", "Endpoint tolerance", "frames",
       "audio.endpoint_tolerance_frames", 0),
    _F("audio.absolute_av_reference_available", "measurement", "bool",
       "An external absolute A/V reference (DVD) was available", v38="audio.absolute_av_reference_available"),
    _F("audio.dvd_free_approved", "choice", "bool", "No-DVD audio timing promoted by the operator",
       v38="audio.dvd_free_approved"),
    # ---------------------------------------------------------------- video / cadence
    _F("video.frames", "measurement", "int", "Output (23.976) frame count", "frames", "video.frames", 1),
    _F("video.duration_s", "measurement", "number", "Output duration", "s", "video.duration_s", 0),
    _F("video.head_trim", "measurement", "int", "Output frames trimmed at head", "frames", "video.head_trim", 0),
    _F("video.tail_trim", "measurement", "int", "Output frames trimmed at tail", "frames", "video.tail_trim", 0),
    _F("video.fade_up_output", "measurement", "int", "Output frame of the opening fade-up", "frames",
       "video.fade_up_output", 0),
    _F("video.delta_s", "measurement", "number", "Video DELTA", "s", "video.delta_s"),
    _F("video.tfm_period5_pct", "measurement", "number", "Share of period-5 cadence", "%",
       "video.tfm_period5_pct", 0),
    _F("video.tfm_unmatched", "measurement", "int", "TFM unmatched fields", "count", "video.tfm_unmatched", 0),
    _F("video.tfm_combed_count", "measurement", "int", "TFM combed frames", "count", "video.tfm_combed_count", 0),
    _F("video.tfm_combed_spans", "measurement", "list", "Combed spans [start, end]", "frames",
       "video.tfm_combed_spans", v38_unset=[]),
    _F("video.inserts", "measurement", "int", "Hybrid/interlaced inserts", "count", "video.inserts", 0),
    _F("video.repair_frames", "measurement", "int", "Frames repaired", "count", "video.repair_frames", 0),
    _F("video.repair_pct", "measurement", "number", "Repaired share", "%", "video.repair_pct", 0),
    _F("video.hybrid_seconds", "measurement", "number", "Video-origin stretches converted at 23.976p", "s"),
    _F("video.manual_fixes", "setting", "list",
       "Operator frame fixes from the final review [{first, last, action, rect?, src?, note}] (fstk.media.manual_fix)"),
    _F("video.hybrid_choices", "setting", "dict",
       "Per-stretch treatment of video-origin stretches {first_cycle: nearest|interpolate} (default nearest)"),
    # ---------------------------------------------------------------- picture quality (video-quality stage)
    _F("quality.crop", "measurement", "dict", "Locked active-area crop {left,top,right,bottom}", "px"),
    _F("quality.sar", "measurement", "str", "Locked sample aspect ratio (num:den)"),
    _F("quality.dar", "measurement", "str", "Locked display aspect ratio (num:den)"),
    _F("quality.dropouts_found", "measurement", "int", "Dropouts detected after stacking", "count", minimum=0),
    _F("quality.verdict", "measurement", "str", "Per-episode quality report verdict (CLEAN / REVIEW / FAIL)"),
    _F("quality.dropouts_fixed", "measurement", "int", "Dropouts concealed from LD neighbours", "count", minimum=0),
    _F("quality.chroma_denoise", "measurement", "str", "Chroma-only denoise applied (off | strength/limit)"),
    _F("quality.audio_events_found", "measurement", "int", "Clicks and digital gaps found in the LD stereo", "count", minimum=0),
    _F("quality.audio_repaired", "measurement", "int", "Short clicks/gaps filled by linear prediction", "count", minimum=0),
    _F("quality.ld_lufs", "measurement", "number", "LD stereo integrated loudness", "LUFS"),
    _F("quality.ld_lra", "measurement", "number", "LD stereo loudness range", "LU"),
    _F("quality.ld_true_peak", "measurement", "number", "LD stereo true peak", "dBFS"),
    _F("quality.fiveone_lufs", "measurement", "number", "DVD 5.1 integrated loudness", "LUFS"),
    _F("quality.subtitle_offset_s", "measurement", "number", "Best global subtitle shift vs speech", "s"),
    # ---------------------------------------------------------------- PADs
    _F("pads.map_pads", "measurement", "int", "PADs in the full map", "count", "pads.map_pads", 0),
    _F("pads.rendered_isolated", "measurement", "int", "Isolated rendered PADs", "count",
       "pads.rendered_isolated", 0),
    _F("pads.repaired", "measurement", "int", "PADs repaired", "count", "pads.repaired", 0),
    _F("pads.trimmed_away", "measurement", "int", "PADs trimmed away", "count", "pads.trimmed_away", 0),
    _F("pads.inside_break_black", "measurement", "int", "PADs inside break black", "count",
       "pads.inside_break_black", 0),
    _F("pads.never_rendered", "measurement", "int", "PADs never rendered", "count", "pads.never_rendered", 0),
    _F("pads.cut_pads", "measurement", "int", "PADs classified as on a shot cut", "count", "pads.cut_pads", 0),
    _F("pads.cut_pad_frames", "measurement", "int_list", "Output frames of every cut PAD", "frames",
       "pads.cut_pad_frames", 0, v38_unset=[], extra_check=_unique),
    _F("pads.content_blacks_left_alone", "measurement", "list", "Genuine dark content not repaired",
       v38="pads.content_blacks_left_alone", v38_unset=[]),
    # ---------------------------------------------------------------- retime
    _F("retime.breaks", "measurement", "list", "Adjudicated commercial-break edges", v38="retime.breaks",
       v38_unset=[]),
    _F("retime.tail_off", "measurement", "number", "Tail offset", "s", "retime.tail_off"),
    _F("retime.merged_music_through", "measurement", "list", "Breaks with music bridging the edit",
       v38="retime.merged_music_through", v38_unset=[]),
    _F("retime.end_black_ld", "measurement", "list", "Final fade-to-black [start, end] in master time", "s",
       "retime.end_black_ld"),
    _F("retime.srt_cues", "measurement", "int", "SRT cue count", "count", "retime.srt_cues", 0),
    _F("retime.max_gap_vs_black_s", "measurement", "number", "Largest retime gap vs black (negative: gap inside black)",
       "s", "retime.max_gap_vs_black_s"),   # S01E18 (v38): −0.719
    _F("retime.head_sil_end", "measurement", "number", "End of head silence", "s", "retime.head_sil_end"),
    _F("retime.tail_sil_start", "measurement", "number", "Start of tail silence", "s", "retime.tail_sil_start"),
    # ---------------------------------------------------------------- QC
    _F("qc.video_md5", "measurement", "str", "Deliverable video MD5", v38="qc.video_md5", v38_unset=""),
    _F("qc.ld_audio_md5", "measurement", "str", "LD audio MD5", v38="qc.ld_audio_md5", v38_unset=""),
    _F("qc.fiveone_md5", "measurement", "str", "DVD 5.1 MD5", v38="qc.fiveone_md5", v38_unset=""),
    _F("qc.srt_md5", "measurement", "str", "SRT MD5", v38="qc.srt_md5", v38_unset=""),
    _F("qc.av_constant_ms", "measurement", "number", "A/V constant offset", "ms", "qc.av_constant_ms"),
    _F("qc.av_slope_ppm", "measurement", "number", "A/V drift", "ppm", "qc.av_slope_ppm"),
    _F("qc.av_rms_ms", "measurement", "number", "A/V RMS residual", "ms", "qc.av_rms_ms", 0),
    _F("qc.track_offset_ms", "measurement", "number", "Track offset", "ms", "qc.track_offset_ms"),
    _F("qc.pad_aligned_black_hits", "measurement", "int", "PAD-aligned black detections", "count",
       "qc.pad_aligned_black_hits", 0),
    _F("qc.identity_own_ncc_min", "measurement", "number", "Min own-identity NCC", v38="qc.identity_own_ncc_min"),
    _F("qc.identity_other_ncc_max", "measurement", "number", "Max other-identity NCC",
       v38="qc.identity_other_ncc_max"),
    _F("qc.full_decode_errors", "measurement", "int", "Full-decode errors", "count", "qc.full_decode_errors", 0),
    _F("qc.dvd_audio_map", "setting", "str", "DVD audio stream used by the absolute A/V gate", v38="qc.dvd_audio_map"),
    _F("qc.delivered_bytes", "measurement", "int", "Deliverable size checked by QC", "bytes", "qc.delivered_bytes", 1),
    # ---------------------------------------------------------------- deliverable settings
    _F("deliverable.key", "setting", "str", "Artifact key QC checks as the deliverable", v38="deliverable"),
    _F("deliverable.tracks", "setting", "str_list", "Streams in the deliverable", v38="deliverable_tracks",
       extra_check=_tracks),
    _F("deliverable.mux_tracks", "setting", "str_list", "Streams the remux maps", v38="mux_tracks",
       extra_check=_tracks),
    _F("deliverable.ld_audio_source", "setting", "str", "Artifact key of the LD audio for remux",
       v38="ld_audio_source"),
    # ---------------------------------------------------------------- records
    _F("audio.capture_selection", "record", "dict", "EFM capture selection decision",
       v38="audio.capture_selection"),
    _F("audio.capture_selection_history", "record", "list", "Superseded capture selections",
       v38="audio.capture_selection_history", v38_unset=[]),
    _F("audio.efm_provenance", "record", "dict", "Per-capture verified EFM decode provenance",
       v38="audio.efm_provenance", v38_unset={}),
    _F("video.trim_review", "record", "dict", "Visual head/tail trim review", v38="video.trim_review"),
    _F("video.cadence_review", "record", "dict", "Cadence evidence review", v38="video.cadence_review"),
    _F("audio.efm_decode_failures", "record", "dict",
       "EFM decodes that failed, per capture (run, reason, source size) — excluded from the automatic choice"),
    _F("audio.dvd_clock", "measurement", "dict",
       "DVD-referenced LD audio clock fit: rate, origin, precision, window evidence summary"),
    _F("audio.av_correction", "record", "dict",
       "Reviewer-driven A/V timing corrections per track (offset/drift, every review round)"),
    _F("video.dvd_alignment_review", "record", "dict", "v38 visual DVD/LD alignment approval",
       v38="video.dvd_alignment_review"),
    _F("video.dvd_alignment", "measurement", "dict",
       "DVD↔LD picture alignment: segments, origin, checks and the automatic decision"),
    _F("video.dvd_origin_master_frame", "measurement", "int",
       "First stack-master frame matched to DVD picture (start of the DVD programme)", "frames", minimum=0),
)

BY_KEY: dict[str, FieldDef] = {f.key: f for f in FIELDS}
BY_V38: dict[str, FieldDef] = {f.v38: f for f in FIELDS if f.v38}
assert len(BY_KEY) == len(FIELDS), "duplicate field key"
assert all(f.kind in KINDS for f in FIELDS)


def field_def(key: str) -> FieldDef:
    try:
        return BY_KEY[key]
    except KeyError:
        raise ManifestError(f"{key!r} is not a declared manifest field (see fstk.manifest.registry)") from None
