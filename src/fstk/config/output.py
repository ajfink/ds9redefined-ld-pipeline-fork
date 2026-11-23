"""Output (encode/audio) settings: machine defaults plus per-episode overrides.

Owner decisions (2026-11-23):
* six video encoders, all software: x265 10/12-bit, x264 10/8-bit, SVT-AV1 10-bit, FFV1 (lossless);
  hardware encoders and the older codecs are left out (quality first; CPU time is irrelevant);
* ``-tune grain`` is permanent whenever x265 is used; colour range is always limited (LaserDisc);
* every encode is one fresh pass from the lossless master, so a settings change re-runs only
  encode → remux → QC;
* audio: the LD stereo track (FLAC by default) is first and default; the retimed DVD 5.1 follows when
  the episode has a DVD; a lossy stereo *compatibility* track (AAC 256 kb/s by default) is added last;
* settings live in the machine profile (defaults) and, per episode, in the manifest as
  ``settings.output`` — one dict per group (``video``, ``audio``); a group present in the episode
  replaces the default group as a whole ("Use defaults" unticked).

Everything here is pure: the stages turn a validated :class:`OutputSettings` into FFmpeg arguments and
into the expected stream properties that the encode check and QC verify.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from fstk.core.errors import ConfigError

# ------------------------------------------------------------------------------------------ video
X26X_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow",
                "placebo")
SVT_PRESETS = tuple(str(n) for n in range(0, 14))          # 0 = slowest/best … 13 = fastest


@dataclass(frozen=True)
class VideoEncoder:
    id: str
    label: str
    codec: str                         # FFmpeg encoder name
    bits: int
    chromas: tuple[str, ...]           # allowed chroma choices
    #: ffprobe ``profile`` and ``pix_fmt`` per chroma (what the encode check and QC expect)
    probe: dict[str, tuple[str | None, str]]
    quality: tuple[float, float, float] | None   # (min, max, default) constant-quality value; None = lossless
    quality_label: str = "CRF"
    presets: tuple[str, ...] = ()
    default_preset: str = ""
    tunes: tuple[str, ...] = ()
    default_tune: str = ""
    tune_locked: bool = False
    bitrate_mode: bool = False         # average bitrate + two passes offered
    bytes_per_frame: int = 110_000     # space estimate per output frame at the default quality
    note: str = ""


VIDEO_ENCODERS: dict[str, VideoEncoder] = {e.id: e for e in (
    VideoEncoder("x265-10", "H.265 10-bit (x265)", "libx265", 10, ("422", "420"),
                 {"422": ("Rext", "yuv422p10le"), "420": ("Main 10", "yuv420p10le")},
                 (0, 51, 14), presets=X26X_PRESETS, default_preset="slow", tunes=("grain",), default_tune="grain",
                 tune_locked=True, bitrate_mode=True, note="Recommended. Tune is always grain."),
    VideoEncoder("x265-12", "H.265 12-bit (x265)", "libx265", 12, ("422", "420"),
                 {"422": ("Rext", "yuv422p12le"), "420": ("Rext", "yuv420p12le")},
                 (0, 51, 14), presets=X26X_PRESETS, default_preset="slow", tunes=("grain",), default_tune="grain",
                 tune_locked=True, bitrate_mode=True, bytes_per_frame=125_000,
                 note="Slightly smoother grain gradients; fewer players decode 12-bit HEVC. Tune is always grain."),
    VideoEncoder("x264-10", "H.264 10-bit (x264)", "libx264", 10, ("422", "420"),
                 {"422": ("High 4:2:2", "yuv422p10le"), "420": ("High 10", "yuv420p10le")},
                 (0, 51, 14), presets=X26X_PRESETS, default_preset="slow", tunes=("grain", "film", "none"),
                 default_tune="grain", bitrate_mode=True, bytes_per_frame=160_000,
                 note="For players without HEVC. Larger files at the same quality."),
    VideoEncoder("x264-8", "H.264 8-bit (x264)", "libx264", 8, ("420", "422"),
                 # 8-bit 4:2:0: x264 reports the lowest profile the fast presets fit (Constrained Baseline at
                 # ultrafast), so only the pixel format is checked there
                 {"420": (None, "yuv420p"), "422": ("High 4:2:2", "yuv422p")},
                 (0, 51, 16), presets=X26X_PRESETS, default_preset="slow", tunes=("grain", "film", "none"),
                 default_tune="grain", bitrate_mode=True, bytes_per_frame=160_000,
                 note="Maximum compatibility. 8-bit can band in smooth gradients."),
    VideoEncoder("av1-10", "AV1 10-bit (SVT)", "libsvtav1", 10, ("420",),
                 {"420": ("Main", "yuv420p10le")},
                 (0, 63, 20), presets=SVT_PRESETS, default_preset="4", tunes=("visual", "psnr"), default_tune="visual",
                 bytes_per_frame=80_000,
                 note="Smaller files; 4:2:0 only. Film-grain synthesis is off so the real grain is kept."),
    VideoEncoder("ffv1", "FFV1 (lossless)", "ffv1", 10, ("422", "420"),
                 {"422": (None, "yuv422p10le"), "420": (None, "yuv420p10le")},
                 None, quality_label="", bytes_per_frame=520_000,
                 note="Lossless archival copy. Very large files."),
)}
DEFAULT_ENCODER = "x265-10"


# ------------------------------------------------------------------------------------------ audio
@dataclass(frozen=True)
class AudioCodec:
    id: str
    label: str
    codec: str                         # FFmpeg encoder
    probe_name: str                    # ffprobe codec_name
    lossless: bool
    bitrates: dict[int, tuple[int, ...]] = None   # channels → choices (kb/s); empty for lossless
    default_bitrate: dict[int, int] = None


def _ab(stereo: tuple[int, ...], s_def: int, surround: tuple[int, ...], m_def: int) -> dict:
    return {"bitrates": {2: stereo, 6: surround}, "default_bitrate": {2: s_def, 6: m_def}}


AUDIO_CODECS: dict[str, AudioCodec] = {c.id: c for c in (
    AudioCodec("flac", "FLAC (lossless)", "flac", "flac", True, {}, {}),
    AudioCodec("alac", "ALAC (lossless)", "alac", "alac", True, {}, {}),
    AudioCodec("pcm", "PCM (uncompressed)", "pcm_s24le", "pcm_s24le", True, {}, {}),
    AudioCodec("opus", "Opus", "libopus", "opus", False,
               **_ab((96, 128, 160, 192, 256, 320), 192, (256, 320, 384, 448, 512), 384)),
    AudioCodec("aac", "AAC", "aac", "aac", False,
               **_ab((128, 160, 192, 224, 256, 320), 256, (384, 448, 512, 640), 512)),
    AudioCodec("ac3", "AC-3 (Dolby Digital)", "ac3", "ac3", False,
               **_ab((192, 224, 256, 320, 384, 448, 640), 448, (384, 448, 640), 640)),
    AudioCodec("eac3", "E-AC-3 (Dolby Digital Plus)", "eac3", "eac3", False,
               **_ab((128, 192, 256, 320, 384, 448, 640), 256, (384, 448, 640, 768, 1024), 768)),
)}
LOSSY = tuple(k for k, c in AUDIO_CODECS.items() if not c.lossless)

GROUPS = ("video", "audio")

DEFAULTS: dict[str, dict[str, Any]] = {
    "video": {"encoder": DEFAULT_ENCODER, "mode": "quality", "quality": 14, "bitrate_kbps": 6000,
              "preset": "slow", "tune": "grain", "chroma": "422", "extra": ""},
    "audio": {"stereo_codec": "flac", "stereo_bitrate": 0,
              "include_51": True, "fiveone_codec": "flac", "fiveone_bitrate": 0,
              "compat": True, "compat_codec": "aac", "compat_bitrate": 256},
}

#: x265/x264 option keys the free-text "extra" field may never set (colour signalling, tune, profile).
LOCKED_EXTRA_KEYS = ("colorprim", "transfer", "colormatrix", "range", "tune", "profile", "output-depth",
                     "film-grain", "input-depth", "pass", "stats")


def default_group(group: str, encoder: str | None = None) -> dict[str, Any]:
    out = copy.deepcopy(DEFAULTS[group])
    if group == "video" and encoder and encoder != out["encoder"]:
        enc = VIDEO_ENCODERS[encoder]
        out.update(encoder=encoder, quality=enc.quality[2] if enc.quality else 0,
                   preset=enc.default_preset, tune=enc.default_tune,
                   chroma=out["chroma"] if out["chroma"] in enc.chromas else enc.chromas[0],
                   mode="quality")
    return out


def parse_extra(text: str) -> dict[str, str]:
    """``key=value:key=value`` (x265 style; spaces and commas also separate). Locked keys are refused."""
    out: dict[str, str] = {}
    for part in text.replace(",", ":").replace(" ", ":").split(":"):
        part = part.strip()
        if not part:
            continue
        key, _, value = part.partition("=")
        key = key.strip().lstrip("-")
        if not key or not all(ch.isalnum() or ch in "-_" for ch in key):
            raise ConfigError(f"extra encoder option {part!r} is not key=value")
        if key in LOCKED_EXTRA_KEYS:
            raise ConfigError(f"extra encoder option {key!r} is fixed by the recipe and cannot be changed")
        out[key] = value.strip()
    return out


def validate_group(group: str, values: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if group not in GROUPS:
        return [f"unknown settings group {group!r}"]
    unknown = sorted(set(values) - set(DEFAULTS[group]))
    if unknown:
        problems.append(f"{group}: unknown setting(s) {unknown}")
    v = {**DEFAULTS[group], **values}
    if group == "video":
        enc = VIDEO_ENCODERS.get(v["encoder"])
        if enc is None:
            return [*problems, f"video: unknown encoder {v['encoder']!r}"]
        if v["chroma"] not in enc.chromas:
            problems.append(f"video: {enc.label} cannot encode {v['chroma']} chroma (allowed {list(enc.chromas)})")
        if v["mode"] not in ("quality", "bitrate"):
            problems.append(f"video: mode must be quality or bitrate, not {v['mode']!r}")
        if v["mode"] == "bitrate" and not enc.bitrate_mode:
            problems.append(f"video: {enc.label} does not offer an average-bitrate mode")
        if v["mode"] == "bitrate" and not (500 <= _num(v["bitrate_kbps"]) <= 100_000):
            problems.append("video: average bitrate must be 500–100000 kb/s")
        if enc.quality and v["mode"] == "quality":
            lo, hi, _ = enc.quality
            if not (lo <= _num(v["quality"]) <= hi):
                problems.append(f"video: {enc.quality_label} must be {lo:g}–{hi:g} for {enc.label}")
        if enc.presets and str(v["preset"]) not in enc.presets:
            problems.append(f"video: preset {v['preset']!r} is not one of {list(enc.presets)}")
        if enc.tunes and v["tune"] not in enc.tunes:
            problems.append(f"video: tune {v['tune']!r} is not one of {list(enc.tunes)}")
        if enc.tune_locked and v["tune"] != enc.default_tune:
            problems.append(f"video: {enc.label} always uses tune {enc.default_tune}")
        if v["extra"]:
            if enc.codec not in ("libx265", "libx264", "libsvtav1"):
                problems.append(f"video: {enc.label} takes no extra encoder options")
            else:
                try:
                    parse_extra(str(v["extra"]))
                except ConfigError as exc:
                    problems.append(f"video: {exc}")
    else:
        for track, channels in (("stereo", 2), ("fiveone", 6), ("compat", 2)):
            codec = AUDIO_CODECS.get(v[f"{track}_codec"])
            if codec is None:
                problems.append(f"audio: unknown {track} codec {v[f'{track}_codec']!r}")
                continue
            if track == "compat" and codec.lossless:
                problems.append("audio: the compatibility track must use a lossy codec (it exists to be small)")
            if not codec.lossless and int(v[f"{track}_bitrate"]) not in codec.bitrates[channels]:
                problems.append(f"audio: {track} bitrate {v[f'{track}_bitrate']} kb/s is not offered for "
                                f"{codec.label} (choices {list(codec.bitrates[channels])})")
        for flag in ("include_51", "compat"):
            if not isinstance(v[flag], bool):
                problems.append(f"audio: {flag} must be true or false")
    return problems


def _num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


@dataclass(frozen=True)
class OutputSettings:
    video: dict[str, Any]
    audio: dict[str, Any]
    #: which groups came from the episode rather than the machine defaults
    overridden: tuple[str, ...] = ()

    @property
    def encoder(self) -> VideoEncoder:
        return VIDEO_ENCODERS[self.video["encoder"]]

    def to_dict(self) -> dict[str, Any]:
        return {"video": dict(self.video), "audio": dict(self.audio), "overridden": list(self.overridden)}


def machine_defaults(profile) -> dict[str, dict[str, Any]]:
    stored = getattr(profile, "output", None) or {}
    return {g: {**DEFAULTS[g], **(stored.get(g) or {})} for g in GROUPS}


def effective(profile, manifest=None) -> OutputSettings:
    """Machine defaults, with each group the episode overrides replaced as a whole. Validated."""
    base = machine_defaults(profile)
    over = (manifest.get("settings.output") if manifest is not None else None) or {}
    groups, overridden = {}, []
    for g in GROUPS:
        if g in over:
            groups[g] = {**DEFAULTS[g], **over[g]}
            overridden.append(g)
        else:
            groups[g] = base[g]
        problems = validate_group(g, groups[g])
        if problems:
            where = "this episode's" if g in over else "the default"
            raise ConfigError(f"{where} {g} settings are invalid: " + "; ".join(problems))
    return OutputSettings(groups["video"], groups["audio"], tuple(overridden))


# ------------------------------------------------------------------------------------------ recipes
def video_args(settings: OutputSettings, *, base_params: str, pass_no: int | None = None,
               stats: str | None = None) -> list[str]:
    """Codec part of the FFmpeg output arguments (``-c:v`` … up to, not including, colour/frames/output)."""
    v, enc = settings.video, settings.encoder
    pix = enc.probe[v["chroma"]][1]
    args = ["-c:v", enc.codec, "-pix_fmt", pix]
    extra = parse_extra(str(v.get("extra") or ""))
    if enc.codec == "libx265":
        profile = {("422", 10): "main422-10", ("420", 10): "main10",
                   ("422", 12): "main422-12", ("420", 12): "main12"}[(v["chroma"], enc.bits)]
        params = base_params
        if v["mode"] == "bitrate" and pass_no:
            params += f":pass={pass_no}:stats={stats}"
        params += "".join(f":{k}={val}" if val else f":{k}" for k, val in extra.items())
        args += ["-profile:v", profile, "-preset", str(v["preset"]), "-tune", "grain", "-x265-params", params]
    elif enc.codec == "libx264":
        profile = {("422", 10): "high422", ("420", 10): "high10", ("422", 8): "high422",
                   ("420", 8): "high"}[(v["chroma"], enc.bits)]
        args += ["-profile:v", profile, "-preset", str(v["preset"])]
        if v["tune"] != "none":
            args += ["-tune", v["tune"]]
        x264 = "colorprim=smpte170m:transfer=smpte170m:colormatrix=smpte170m:fullrange=off"
        x264 += "".join(f":{k}={val}" if val else f":{k}" for k, val in extra.items())
        args += ["-x264-params", x264]
        if v["mode"] == "bitrate" and pass_no:
            args += ["-pass", str(pass_no), "-passlogfile", str(stats)]
    elif enc.codec == "libsvtav1":
        svt = f"tune={0 if v['tune'] == 'visual' else 1}:film-grain=0:enable-overlays=1"
        svt += "".join(f":{k}={val}" for k, val in extra.items())
        args += ["-preset", str(v["preset"]), "-svtav1-params", svt]
    elif enc.codec == "ffv1":
        args += ["-level", "3", "-g", "1", "-slices", "24", "-slicecrc", "1", "-context", "1"]
    if enc.quality is not None:
        if v["mode"] == "bitrate":
            args += ["-b:v", f"{int(v['bitrate_kbps'])}k"]
        else:
            q = v["quality"]
            args += ["-crf", f"{float(q):g}"]
    return args


def two_pass(settings: OutputSettings) -> bool:
    return settings.video["mode"] == "bitrate" and settings.encoder.bitrate_mode


def expected_video(settings: OutputSettings) -> dict[str, Any]:
    """What ffprobe must report for the encoded stream (the encode check and QC use this)."""
    enc, chroma = settings.encoder, settings.video["chroma"]
    profile, pix = enc.probe[chroma]
    return {"encoder": enc.id, "codec_name": {"libx265": "hevc", "libx264": "h264", "libsvtav1": "av1",
                                              "ffv1": "ffv1"}[enc.codec],
            "profile": profile, "pix_fmt": pix, "chroma": chroma}


def describe_video(settings: OutputSettings) -> str:
    v, enc = settings.video, settings.encoder
    bits = [enc.label, v["chroma"].replace("422", "4:2:2").replace("420", "4:2:0")]
    if enc.quality is not None:
        bits.append(f"{int(v['bitrate_kbps'])} kb/s 2-pass" if v["mode"] == "bitrate"
                    else f"{enc.quality_label} {float(v['quality']):g}")
    if enc.presets:
        bits.append(f"preset {v['preset']}")
    if enc.tunes and v["tune"] != "none":
        bits.append(f"tune {v['tune']}")
    return ", ".join(bits)


@dataclass(frozen=True)
class AudioTrack:
    role: str            # "ld" | "51" | "compat"
    codec: AudioCodec
    channels: int
    bitrate: int         # kb/s, 0 for lossless
    title: str
    default: bool

    @property
    def copy(self) -> bool:
        """The stream is delivered as the FLAC the pipeline built (no transcode)."""
        return self.codec.id == "flac"

    def args(self, out_index: int) -> list[str]:
        if self.copy:
            return [f"-c:a:{out_index}", "copy"]
        a = [f"-c:a:{out_index}", self.codec.codec]
        if not self.codec.lossless:
            a += [f"-b:a:{out_index}", f"{self.bitrate}k"]
        return a

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "codec": self.codec.id, "codec_name": self.codec.probe_name,
                "channels": self.channels, "bitrate_kbps": self.bitrate, "title": self.title,
                "default": self.default}


LD_TITLE = "LD 2.0 (original Dolby Surround)"
FIVEONE_TITLE = "DVD 5.1 (retimed)"


def audio_tracks(settings: OutputSettings, *, has_51: bool) -> list[AudioTrack]:
    """Delivered audio, in order: LD stereo (default) · DVD 5.1 (when the episode has it) · compatibility."""
    a = settings.audio
    out = [AudioTrack("ld", AUDIO_CODECS[a["stereo_codec"]], 2, int(a["stereo_bitrate"] or 0), LD_TITLE, True)]
    if has_51 and a["include_51"]:
        out.append(AudioTrack("51", AUDIO_CODECS[a["fiveone_codec"]], 6, int(a["fiveone_bitrate"] or 0),
                              FIVEONE_TITLE, False))
    if a["compat"]:
        c = AUDIO_CODECS[a["compat_codec"]]
        out.append(AudioTrack("compat", c, 2, int(a["compat_bitrate"]),
                              f"LD 2.0 compatibility ({c.label.split(' ')[0]} {a['compat_bitrate']} kb/s)", False))
    return out


def describe_audio(settings: OutputSettings, *, has_51: bool | None = None) -> str:
    a = settings.audio

    def one(codec_id: str, kbps: int) -> str:
        c = AUDIO_CODECS[codec_id]
        return c.label.split(" (")[0] + ("" if c.lossless else f" {kbps} kb/s")

    parts = [f"LD stereo {one(a['stereo_codec'], a['stereo_bitrate'])}"]
    if a["include_51"] and has_51 is not False:
        parts.append(f"DVD 5.1 {one(a['fiveone_codec'], a['fiveone_bitrate'])}"
                     + (" (when a DVD is available)" if has_51 is None else ""))
    if a["compat"]:
        parts.append(f"compatibility {one(a['compat_codec'], a['compat_bitrate'])}")
    return " · ".join(parts)
