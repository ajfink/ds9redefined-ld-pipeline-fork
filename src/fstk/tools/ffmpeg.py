"""FFmpeg / FFprobe adapter.

Rules carried from v38 (PORTING §5), enforced here rather than in prose:

* **Never seek a reference DVD with non-monotonic PTS.** ``scan_packets`` measures packet
  timestamp order; ``input_args(start=...)`` refuses to seek a source whose scan found
  non-monotonic video PTS. Such sources must be decoded sequentially and indexed by frame.
* **Frame counts come from decoding,** not container metadata (``count_frames`` decodes;
  ``null_decode`` decodes everything and also counts decoder errors).
* **select= expressions stay ≤ 32 terms** per call (``frame_selection_chunks``); longer chains
  fail to initialise or silently mis-select.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from fstk.core.errors import ArtifactError, ToolError
from fstk.tools import process

SELECT_TERMS_PER_CALL = 32


# ---------------------------------------------------------------------------------- probe
@dataclass(frozen=True)
class StreamInfo:
    index: int
    codec_type: str
    codec_name: str | None
    raw: dict[str, Any]

    @property
    def frame_rate(self) -> Fraction | None:
        value = self.raw.get("r_frame_rate") or ""
        try:
            f = Fraction(value)
        except (ValueError, ZeroDivisionError):
            return None
        return f if f > 0 else None

    @property
    def avg_frame_rate(self) -> Fraction | None:
        value = self.raw.get("avg_frame_rate") or ""
        try:
            f = Fraction(value)
        except (ValueError, ZeroDivisionError):
            return None
        return f if f > 0 else None

    @property
    def duration_s(self) -> float | None:
        d = self.raw.get("duration")
        return float(d) if d not in (None, "N/A") else None


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    format: dict[str, Any]
    streams: tuple[StreamInfo, ...]

    def of_type(self, codec_type: str) -> list[StreamInfo]:
        return [s for s in self.streams if s.codec_type == codec_type]

    @property
    def duration_s(self) -> float | None:
        d = self.format.get("duration")
        return float(d) if d not in (None, "N/A") else None

    def only(self, codec_type: str) -> StreamInfo:
        found = self.of_type(codec_type)
        if len(found) != 1:
            raise ArtifactError(f"{self.path.name}: expected exactly one {codec_type} stream, found {len(found)}")
        return found[0]


@dataclass(frozen=True)
class PacketScan:
    stream: str
    packets: int
    monotonic: bool
    first_violation_index: int | None
    violations: int

    @property
    def seekable(self) -> bool:
        return self.monotonic


@dataclass
class DecodeResult:
    frames: int | None
    errors: list[str] = field(default_factory=list)
    returncode: int | None = None

    @property
    def clean(self) -> bool:
        return self.returncode == 0 and not self.errors and self.frames is not None


@dataclass
class BlackScan:
    frames: int | None
    events: list[tuple[float, float, float]]  # (start_s, end_s, duration_s)
    errors: list[str]
    returncode: int | None


def progress_seconds(output: str) -> float | None:
    """FFmpeg's final machine-readable ``out_time``, preferring integer microseconds (v38)."""
    micro = re.findall(r"(?m)^out_time_us=(\d+)\s*$", output)
    if micro:
        return int(micro[-1]) / 1_000_000.0
    times = re.findall(r"(?m)^out_time=(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)\s*$", output)
    if times:
        h, m, s = times[-1]
        return int(h) * 3600 + int(m) * 60 + float(s)
    return None


class FFmpeg:
    def __init__(self, ffmpeg: str | Path, ffprobe: str | Path, *, log_dir: Path | None = None):
        self.ffmpeg = str(ffmpeg)
        self.ffprobe = str(ffprobe)
        self.log_dir = log_dir
        self._scans: dict[Path, PacketScan] = {}

    @classmethod
    def from_profile(cls, profile, *, log_dir: Path | None = None) -> FFmpeg:
        return cls(profile.tool("ffmpeg"), profile.tool("ffprobe"), log_dir=log_dir)

    def _log(self, name: str) -> Path | None:
        return self.log_dir / f"{name}.log" if self.log_dir else None

    def _probe_json(self, args: list[str], path: Path, *, what: str) -> dict[str, Any]:
        res = process.run([self.ffprobe, "-v", "error", *args, "-of", "json", str(path)],
                          capture_stdout=True, log_path=self._log("ffprobe"))
        if not res.exited_cleanly:
            raise ToolError(f"ffprobe {what} failed for {path}: {res.describe()}")
        try:
            return json.loads(res.stdout or b"{}")
        except json.JSONDecodeError as exc:
            raise ToolError(f"ffprobe {what} returned invalid JSON for {path}: {exc}") from None

    def probe(self, path: str | Path) -> MediaInfo:
        p = Path(path)
        if not p.is_file():
            raise ArtifactError(f"not a file: {p}")
        data = self._probe_json(["-show_format", "-show_streams"], p, what="probe")
        streams = tuple(StreamInfo(int(s.get("index", i)), str(s.get("codec_type")), s.get("codec_name"), s)
                        for i, s in enumerate(data.get("streams") or []))
        return MediaInfo(p, data.get("format") or {}, streams)

    def count_frames(self, path: str | Path, stream: str = "v:0") -> int:
        """Decode-count frames of one stream (``nb_read_frames``)."""
        p = Path(path)
        data = self._probe_json(["-count_frames", "-select_streams", stream,
                                 "-show_entries", "stream=nb_read_frames"], p, what="frame count")
        streams = data.get("streams") or []
        if len(streams) != 1 or not str(streams[0].get("nb_read_frames", "")).isdigit():
            raise ArtifactError(f"{p.name}: ffprobe did not report a decoded frame count for {stream}")
        return int(streams[0]["nb_read_frames"])

    def scan_packets(self, path: str | Path, stream: str = "v:0") -> PacketScan:
        """Check packet PTS order in *decode order*. Non-monotonic → never seek this source.

        B-frames legitimately reorder PTS in decode order, so the check is on presentation
        order consistency: sorted PTS must not repeat and the stream must not jump backwards
        beyond the reorder window. v38 found MakeMKV DVD rips whose PTS go backwards across
        interleave boundaries; those are flagged here.
        """
        p = Path(path).resolve()
        data = self._probe_json(["-select_streams", stream, "-show_entries", "packet=pts,dts,flags"],
                                p, what="packet scan")
        packets = data.get("packets") or []
        dts = [int(pk["dts"]) for pk in packets if str(pk.get("dts", "N/A")).lstrip("-").isdigit()]
        pts = [int(pk["pts"]) for pk in packets if str(pk.get("pts", "N/A")).lstrip("-").isdigit()]
        violations, first = 0, None
        for i in range(1, len(dts)):
            if dts[i] <= dts[i - 1]:
                violations += 1
                first = i if first is None else first
        if len(set(pts)) != len(pts):
            violations += len(pts) - len(set(pts))
            first = first if first is not None else 0
        if len(dts) < len(packets) or len(pts) < len(packets):
            # missing timestamps cannot be proven monotonic: fail closed
            violations += len(packets) - min(len(dts), len(pts))
            first = first if first is not None else 0
        scan = PacketScan(stream, len(packets), violations == 0, first, violations)
        self._scans[p] = scan
        return scan

    def input_args(self, path: str | Path, *, start_s: float | None = None,
                   duration_s: float | None = None) -> list[str]:
        """Input arguments; seeking is refused for sources not proven monotonic."""
        p = Path(path).resolve()
        args: list[str] = []
        if start_s is not None or duration_s is not None:
            scan = self._scans.get(p)
            if scan is None:
                raise ToolError(f"refusing to seek {p.name} before scan_packets() proved its PTS monotonic")
            if not scan.seekable:
                raise ToolError(f"refusing to seek {p.name}: non-monotonic PTS ({scan.violations} violations). "
                                "Decode sequentially and select frames by index.")
            if start_s is not None:
                args += ["-ss", f"{start_s:.6f}"]
            if duration_s is not None:
                args += ["-t", f"{duration_s:.6f}"]
        return [*args, "-i", str(p)]

    def null_decode(self, path: str | Path, *, map_spec: str = "0:v:0", cancel=None) -> DecodeResult:
        """Decode a whole stream to null, counting frames and decoder error lines."""
        p = Path(path)
        errors: list[str] = []
        frames: list[int] = []

        def on_line(stream: str, line: str) -> None:
            m = re.fullmatch(r"frame=(\d+)\s*", line)
            if stream == "stdout" and m:
                frames.append(int(m.group(1)))
            elif stream != "stdout" and line.strip():
                errors.append(line.strip())

        res = process.run([self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                           "-progress", "pipe:1", "-stats_period", "5", "-i", str(p),
                           "-map", map_spec, "-f", "null", "-"],
                          on_line=on_line, log_path=self._log("null-decode"), cancel=cancel)
        return DecodeResult(frames[-1] if frames else None, errors, res.returncode)

    def blackdetect_decode(self, path: str | Path, *, cancel=None,
                           vf: str = "blackdetect=d=0:pix_th=0.10:pic_th=0.98") -> BlackScan:
        """One full sequential decode of v:0 with blackdetect (v38 ``scan_padfixed_black``).

        Frame count comes from ``-progress`` (final ``frame=``); decoder problems are lines that
        FFmpeg itself tags ``[error]``/``[fatal]`` (``-loglevel level+info``).
        """
        p = Path(path)
        frames: list[int] = []
        events: list[tuple[float, float, float]] = []
        errors: list[str] = []
        pat = re.compile(r"black_start:\s*([0-9.]+)\s+black_end:\s*([0-9.]+)\s+black_duration:\s*([0-9.]+)")

        def on_line(stream: str, line: str) -> None:
            if stream == "stdout":
                m = re.fullmatch(r"frame=(\d+)\s*", line)
                if m:
                    frames.append(int(m.group(1)))
                return
            m = pat.search(line)
            if m:
                events.append((float(m.group(1)), float(m.group(2)), float(m.group(3))))
            elif "[error]" in line or "[fatal]" in line:
                errors.append(line.strip())

        res = process.run([self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "level+info",
                           "-progress", "pipe:1", "-stats_period", "5", "-i", str(p), "-map", "0:v:0",
                           "-vf", vf, "-an", "-f", "null", "-"],
                          on_line=on_line, log_path=self._log("blackdetect"), cancel=cancel)
        return BlackScan(frames[-1] if frames else None, events, errors, res.returncode)

    def audio_window_seconds(self, path: str | Path, stream_number: int, at_s: float, window_s: float,
                             *, input_seek: bool) -> float:
        """Decode one audio window of a *deliverable* after an input (index) or output seek.

        This deliberately exercises container seeking (v38 QC seekability gate); it is only used
        on our own Matroska output, never on DVD sources (whose PTS may be non-monotonic).
        """
        stamp = f"{at_s:.6f}"
        seek = ["-ss", stamp, "-i", str(path)] if input_seek else ["-i", str(path), "-ss", stamp]
        res = process.run([self.ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", *seek,
                           "-map", f"0:a:{stream_number}", "-vn", "-t", f"{window_s:.3f}", "-af", "volumedetect",
                           "-f", "null", "-", "-progress", "pipe:1"], capture_stdout=True,
                          log_path=self._log("seek-window"))
        mode = "input" if input_seek else "output"
        if res.returncode != 0:
            raise ArtifactError(f"FFmpeg {mode}-seek audio decode failed at {at_s:.3f}s for a:{stream_number}: "
                                f"{res.describe()}")
        seconds = progress_seconds((res.stdout or b"").decode("utf-8", "replace"))
        if seconds is None:
            raise ArtifactError(f"FFmpeg returned no -progress out_time for a:{stream_number} at {at_s:.3f}s")
        return seconds

    def gray_thumbnails(self, path: str | Path, width: int = 48, height: int = 36, *, cancel=None):
        """Decode every frame of v:0 sequentially to ``width``×``height`` grey (area scaling).

        Returns a uint8 array (frames, width*height). Frames are indexed exactly; nothing seeks.
        """
        import numpy as np

        res = process.run([self.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0",
                           "-vf", f"scale={width}:{height}:flags=area,format=gray", "-f", "rawvideo",
                           "-pix_fmt", "gray", "-"], capture_stdout=True, log_path=self._log("thumbnails"),
                          cancel=cancel)
        data = res.stdout or b""
        size = width * height
        if res.returncode != 0 or len(data) % size:
            raise ArtifactError(f"thumbnail decode of {path} failed or was truncated: {res.describe()}")
        return np.frombuffer(data, np.uint8).reshape(-1, size)

    def pcm(self, path: str | Path, selector: str, sample_rate: int, *, start_s: float | None = None,
            duration_s: float | None = None, cancel=None):
        """Mono float32 PCM of one audio stream.

        Without ``start_s`` the stream is decoded sequentially from the start (safe for DVDs).
        ``start_s`` uses an input seek and is only for fstk's own deliverables (seek-tested by QC).
        """
        import numpy as np

        seek = (["-ss", f"{start_s:.3f}"] if start_s is not None else []) + \
               (["-t", f"{duration_s:.3f}"] if duration_s is not None else [])
        res = process.run([self.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", *seek, "-i", str(path),
                           "-map", selector, "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-"],
                          capture_stdout=True, log_path=self._log("pcm"), cancel=cancel)
        if res.returncode != 0:
            raise ArtifactError(f"audio decode failed {path} {selector}: {res.describe()}")
        return np.frombuffer(res.stdout or b"", np.int16).astype(np.float32)

    def supports_soxr(self) -> bool:
        """Whether this FFmpeg build has libsoxr (v38 records the swr fallback rather than failing late)."""
        res = process.run([self.ffmpeg, "-hide_banner", "-buildconf"], capture_stdout=True,
                          log_path=self._log("buildconf"))
        text = (res.stdout or b"").decode("utf-8", "replace") + "\n".join(res.tail)
        return res.returncode == 0 and "--enable-libsoxr" in text

    def packet_md5(self, path: str | Path, selector: str) -> str:
        """MD5 of one stream's packets via stream copy (v38 stream-copy identity evidence)."""
        res = process.run([self.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-map", selector,
                           "-c", "copy", "-f", "hash", "-hash", "md5", "-"], capture_stdout=True,
                          log_path=self._log("packet-md5"))
        hits = re.findall(r"(?m)^MD5=([0-9a-fA-F]{32})\s*$", (res.stdout or b"").decode("ascii", "replace"))
        if res.returncode != 0 or len(hits) != 1:
            raise ArtifactError(f"could not read a unique packet MD5 for {path} {selector}: {res.describe()}")
        return hits[0].lower()

    def run(self, args: list[str], *, name: str = "ffmpeg", **kw) -> process.ProcessResult:
        return process.run([self.ffmpeg, "-hide_banner", "-nostdin", *args], log_path=self._log(name), **kw)


# ------------------------------------------------------------------------- select chunks
def frame_selection_chunks(frames: list[int], limit: int = SELECT_TERMS_PER_CALL) -> list[tuple[list[int], str]]:
    """Split frame indices into bounded ``select=`` expressions (ported verbatim from v38).

    Returns ``(frames, expression)`` pairs that concatenate back to the sorted input and whose
    expressions select exactly those frames. Constant-step runs collapse into one term.
    """
    if type(limit) is not int or not 1 <= limit <= SELECT_TERMS_PER_CALL:
        raise ToolError(f"select expression limit must be 1..{SELECT_TERMS_PER_CALL}")
    unique = sorted(set(frames))
    if len(unique) != len(frames):
        raise ToolError("select frame indices must be unique")
    if any(type(frame) is not int or frame < 0 for frame in unique):
        raise ToolError("select frame indices must be non-negative integers")
    runs: list[tuple[list[int], str]] = []
    begin = 0
    while begin < len(unique):
        end = begin
        step = None
        while end + 1 < len(unique):
            delta = unique[end + 1] - unique[end]
            if step is None:
                step = delta
            elif delta != step:
                break
            end += 1
        start_frame, end_frame = unique[begin], unique[end]
        if start_frame == end_frame:
            term = f"eq(n,{start_frame})"
        elif step == 1:
            term = f"between(n,{start_frame},{end_frame})"
        else:
            term = f"between(n,{start_frame},{end_frame})*not(mod(n-{start_frame},{step}))"
        runs.append((unique[begin:end + 1], term))
        begin = end + 1
    return [([f for run_frames, _ in group for f in run_frames], "+".join(t for _, t in group))
            for group in (runs[i:i + limit] for i in range(0, len(runs), limit))]
