"""Shared LD audio build for the no-DVD and DVD-referenced stages (exact-ratio, 24-bit, sample-exact)."""
from __future__ import annotations

from pathlib import Path

from fstk.core.errors import ArtifactError
from fstk.core.hashing import sha256_file
from fstk.media.resample import correct_and_sync
from fstk.qc.dvdfree import exact_audio_samples


def build_corrected_and_synced(ctx, source: Path, *, rate_hz: float, start_sample: int, target_samples: int,
                               corrected: Path, synced: Path, base: float = 0.05, span: float = 0.85) -> dict:
    """Resample ``source`` at the exact ``rate_hz``; write ``corrected`` (whole side) and ``synced``.

    Verifies both outputs with FFprobe (44.1 kHz stereo FLAC; synced has exactly ``target_samples``).
    """
    ff = ctx.tool("ffmpeg")
    try:
        info = correct_and_sync(source, rate_hz=rate_hz, start_sample=start_sample, target_samples=target_samples,
                                corrected=corrected, synced=synced, ffmpeg=str(ff.ffmpeg), log_dir=ctx.log_dir,
                                cancel=ctx.cancel, progress=lambda f: ctx.report(base + span * f, "rate correction"))
    except (OSError, ValueError, RuntimeError) as exc:
        raise ArtifactError(f"LD audio build failed: {exc}") from exc
    out = {"resample": info}
    for key, path in (("corrected", corrected), ("synced", synced)):
        stream = ff.probe(path).only("audio").raw
        if str(stream.get("sample_rate")) != "44100" or int(stream.get("channels") or 0) != 2 \
                or stream.get("codec_name") != "flac":
            raise ArtifactError(f"{path.name} has an unexpected stream format: {stream}")
        out[key] = {"path": str(path), "stream": stream, "output_sha256": sha256_file(path)}
    actual = exact_audio_samples(out["synced"]["stream"])
    if actual != target_samples:
        raise ArtifactError(f"synced audio has {actual} samples; the video needs exactly {target_samples}")
    return out
