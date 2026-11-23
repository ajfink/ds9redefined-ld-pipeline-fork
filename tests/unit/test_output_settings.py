"""Output settings: validation, defaults + per-episode merge, and real encodes for every encoder/codec."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from fstk.config.output import (
    AUDIO_CODECS,
    DEFAULTS,
    VIDEO_ENCODERS,
    OutputSettings,
    audio_tracks,
    default_group,
    effective,
    expected_video,
    parse_extra,
    validate_group,
)
from fstk.config.profile import SCHEMA, Profile
from fstk.core.errors import ConfigError
from fstk.media.encode import X265_PARAMS, check_encoded_stream, final_encode_args

HAVE_FF = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def profile(tmp_path, output=None):
    return Profile.from_dict({"schema": SCHEMA, "machine": "T", "os": "linux", "data_root": str(tmp_path / "d"),
                              "media_root": str(tmp_path / "m"), **({"output": output} if output else {})})


class FakeManifest(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def test_defaults_are_the_v38_recipe_and_valid():
    assert DEFAULTS["video"]["encoder"] == "x265-10" and DEFAULTS["video"]["quality"] == 14
    assert DEFAULTS["video"]["tune"] == "grain" and DEFAULTS["video"]["preset"] == "slow"
    assert DEFAULTS["audio"]["stereo_codec"] == "flac" and DEFAULTS["audio"]["compat"] is True
    assert DEFAULTS["audio"]["compat_codec"] == "aac" and DEFAULTS["audio"]["compat_bitrate"] == 256
    for g in DEFAULTS:
        assert validate_group(g, DEFAULTS[g]) == []
    for enc in VIDEO_ENCODERS:
        assert validate_group("video", default_group("video", enc)) == [], enc
    args = final_encode_args(aspect="4:3", frames=10, duration_s=1.0, capture="P1", out="o.mkv")
    assert args[args.index("-tune"):args.index("-tune") + 2] == ["-tune", "grain"]
    assert args[args.index("-crf") + 1] == "14" and args[args.index("-preset") + 1] == "slow"


def test_x265_grain_is_permanent_and_locked_options_are_refused():
    assert validate_group("video", {**DEFAULTS["video"], "tune": "none"})
    assert validate_group("video", {**DEFAULTS["video"], "encoder": "x265-12", "tune": "film"})
    with pytest.raises(ConfigError):
        parse_extra("tune=animation")
    with pytest.raises(ConfigError):
        parse_extra("range=full")
    assert parse_extra("aq-mode=3:psy-rd=2.0") == {"aq-mode": "3", "psy-rd": "2.0"}
    assert validate_group("video", {**default_group("video", "av1-10"), "chroma": "422"})   # SVT is 4:2:0 only
    assert validate_group("audio", {**DEFAULTS["audio"], "compat_codec": "flac"})          # compat must be lossy
    assert validate_group("audio", {**DEFAULTS["audio"], "compat_bitrate": 999})


def test_episode_group_replaces_the_default_group(tmp_path):
    prof = profile(tmp_path, {"video": {"quality": 16}})
    s = effective(prof, FakeManifest())
    assert s.video["quality"] == 16 and s.overridden == ()
    m = FakeManifest({"settings.output": {"audio": {"compat": False}}})
    s = effective(prof, m)
    assert s.video["quality"] == 16 and s.audio["compat"] is False and s.overridden == ("audio",)
    bad = FakeManifest({"settings.output": {"video": {"encoder": "nvenc"}}})
    with pytest.raises(ConfigError, match="this episode's video settings"):
        effective(prof, bad)


def test_audio_layout_keeps_ld_first_and_51_second():
    s = OutputSettings(dict(DEFAULTS["video"]), dict(DEFAULTS["audio"]))
    assert [t.role for t in audio_tracks(s, has_51=True)] == ["ld", "51", "compat"]
    assert [t.role for t in audio_tracks(s, has_51=False)] == ["ld", "compat"]
    assert [t.default for t in audio_tracks(s, has_51=True)] == [True, False, False]
    no51 = OutputSettings(dict(DEFAULTS["video"]), {**DEFAULTS["audio"], "include_51": False})
    assert [t.role for t in audio_tracks(no51, has_51=True)] == ["ld", "compat"]


@pytest.mark.skipif(not HAVE_FF, reason="ffmpeg not installed")
@pytest.mark.parametrize("enc", sorted(VIDEO_ENCODERS))
def test_every_encoder_produces_the_stream_qc_expects(tmp_path, enc):
    if enc not in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout \
            and VIDEO_ENCODERS[enc].codec not in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                                                capture_output=True, text=True).stdout:
        pytest.skip(f"{VIDEO_ENCODERS[enc].codec} not in this ffmpeg")
    group = default_group("video", enc)
    group["preset"] = {"av1-10": "12"}.get(enc, "ultrafast" if VIDEO_ENCODERS[enc].presets else "")
    for chroma in VIDEO_ENCODERS[enc].chromas:
        s = OutputSettings({**group, "chroma": chroma}, dict(DEFAULTS["audio"]))
        out = tmp_path / f"{enc}-{chroma}.mkv"
        pix = VIDEO_ENCODERS[enc].probe[chroma][1]
        src = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
               "testsrc2=size=768x484:rate=24000/1001", "-frames:v", "6", "-pix_fmt", pix, "-f", "yuv4mpegpipe",
               "-strict", "-1", "-"]
        args = final_encode_args(aspect="4:3", frames=6, duration_s=6 * 1001 / 24000, capture="P1", out=str(out),
                                 settings=s)
        y4m = subprocess.run(src, capture_output=True, check=True).stdout
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "yuv4mpegpipe", "-i", "-", *args],
                       input=y4m, check=True, capture_output=True)
        probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
                                          capture_output=True, text=True, check=True).stdout)["streams"][0]
        expected = expected_video(s)
        issues = check_encoded_stream(probe, probe_frames=6, decoded_frames=6, decode_errors=[],
                                      duration_s=6 * 1001 / 24000, expected_frames=6,
                                      expected_duration_s=6 * 1001 / 24000, expected_raster=(768, 484),
                                      expected=expected)
        issues = [i for i in issues if not i.startswith("encoded rate")]   # y4m test source, not the point here
        assert issues == [], (enc, chroma, probe.get("profile"), probe.get("pix_fmt"))


@pytest.mark.skipif(not HAVE_FF, reason="ffmpeg not installed")
def test_two_pass_bitrate_mode_runs_both_passes(tmp_path):
    s = OutputSettings({**DEFAULTS["video"], "mode": "bitrate", "bitrate_kbps": 2000, "preset": "ultrafast"},
                       dict(DEFAULTS["audio"]))
    stats = str(tmp_path / "x.passlog")
    out = tmp_path / "two.mkv"
    y4m = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=768x484:rate=24000/1001",
                          "-frames:v", "6", "-pix_fmt", "yuv422p10le", "-f", "yuv4mpegpipe", "-strict", "-1", "-"],
                         capture_output=True, check=True).stdout
    for n in (1, 2):
        args = final_encode_args(aspect="4:3", frames=6, duration_s=0.25, capture="P1", out=str(out), settings=s,
                                 pass_no=n, stats=stats)
        assert "-tune" in args and "grain" in args and f"pass={n}" in args[args.index("-x265-params") + 1]
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "yuv4mpegpipe", "-i", "-", *args], input=y4m,
                       check=True, capture_output=True)
    assert out.is_file() and out.stat().st_size > 0
    assert X265_PARAMS in args[args.index("-x265-params") + 1]


@pytest.mark.skipif(not HAVE_FF, reason="ffmpeg not installed")
@pytest.mark.parametrize("codec", sorted(AUDIO_CODECS))
def test_every_audio_codec_encodes_stereo_and_51(tmp_path, codec):
    from fstk.config.output import AudioTrack
    from fstk.media.remux import transcode_args

    c = AUDIO_CODECS[codec]
    for channels, rate, layout in ((2, 44100, "stereo"), (6, 48000, "5.1")):
        src = tmp_path / f"src{channels}.flac"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"sine=frequency=440:sample_rate={rate}", "-t", "1", "-af", f"aformat=channel_layouts={layout}",
                        "-sample_fmt", "s32", "-c:a", "flac", str(src)], check=True)
        track = AudioTrack("ld", c, channels, 0 if c.lossless else c.default_bitrate[channels], "t", True)
        out = tmp_path / f"{codec}-{channels}.mka"
        subprocess.run(["ffmpeg", *transcode_args(src, track, out)], check=True, capture_output=True)
        probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
                                          capture_output=True, text=True, check=True).stdout)["streams"][0]
        assert probe["codec_name"] == c.probe_name and int(probe["channels"]) == channels, probe
