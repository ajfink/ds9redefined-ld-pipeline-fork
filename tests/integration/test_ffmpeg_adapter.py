from __future__ import annotations

import shutil
import subprocess

import pytest

from fstk.core.errors import ArtifactError, ToolError
from fstk.tools.ffmpeg import FFmpeg

pytestmark = pytest.mark.media
HAVE = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not HAVE, reason="ffmpeg/ffprobe not on PATH")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    if not HAVE:
        pytest.skip("ffmpeg missing")
    out = tmp_path_factory.mktemp("media") / "clip.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=24000/1001",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-frames:v", "48", "-t", "2.1", "-c:v", "ffv1", "-c:a", "flac", "-shortest", str(out)], check=True)
    return out


@pytest.fixture
def ff(tmp_path):
    return FFmpeg("ffmpeg", "ffprobe", log_dir=tmp_path / "logs")


@needs_ffmpeg
def test_probe_and_count(ff, clip):
    info = ff.probe(clip)
    v = info.only("video")
    assert v.frame_rate.numerator == 24000 and v.frame_rate.denominator == 1001
    assert len(info.of_type("audio")) == 1
    assert ff.count_frames(clip) == 48


@needs_ffmpeg
def test_null_decode_counts_frames(ff, clip):
    res = ff.null_decode(clip)
    assert res.clean and res.frames == 48


@needs_ffmpeg
def test_null_decode_reports_corruption(ff, clip, tmp_path):
    bad = tmp_path / "bad.mkv"
    data = bytearray(clip.read_bytes())
    for off in range(len(data) // 3, len(data) // 3 + 4000, 7):
        data[off] ^= 0x5A
    bad.write_bytes(bytes(data))
    res = ff.null_decode(bad)
    assert not res.clean


@needs_ffmpeg
def test_seek_requires_monotonic_scan(ff, clip):
    with pytest.raises(ToolError, match="before scan_packets"):
        ff.input_args(clip, start_s=1.0)
    scan = ff.scan_packets(clip)
    assert scan.monotonic and scan.packets == 48
    assert ff.input_args(clip, start_s=1.0)[:2] == ["-ss", "1.000000"]
    assert ff.input_args(clip) == ["-i", str(clip.resolve())]


def test_non_monotonic_source_refuses_seek(monkeypatch, tmp_path):
    ff = FFmpeg("ffmpeg", "ffprobe")
    f = tmp_path / "dvd.mkv"
    f.write_bytes(b"x")
    packets = [{"pts": p, "dts": d} for p, d in [(0, 0), (1001, 1001), (2002, 2002), (1501, 1501), (3003, 3003)]]
    monkeypatch.setattr(ff, "_probe_json", lambda *a, **k: {"packets": packets})
    scan = ff.scan_packets(f)
    assert not scan.monotonic and scan.first_violation_index == 3
    with pytest.raises(ToolError, match="non-monotonic"):
        ff.input_args(f, start_s=10)


def test_missing_timestamps_fail_closed(monkeypatch, tmp_path):
    ff = FFmpeg("ffmpeg", "ffprobe")
    f = tmp_path / "x.mkv"
    f.write_bytes(b"x")
    monkeypatch.setattr(ff, "_probe_json", lambda *a, **k: {"packets": [{"pts": 0, "dts": 0},
                                                                         {"pts": "N/A", "dts": "N/A"}]})
    assert not ff.scan_packets(f).monotonic


@needs_ffmpeg
def test_probe_missing_file(ff, tmp_path):
    with pytest.raises(ArtifactError):
        ff.probe(tmp_path / "nope.mkv")
