"""``rife-ncnn-vulkan`` adapter (non-cut PAD fill).

The recipe is v38's validated one (``scripts/rife_padframes.py``): the midpoint (``-s 0.5``) of the
two real neighbours, CPU fp32 (``-g -1``; GPU fp16 is the precision risk) with spatial and
temporal TTA (``-x -z``). UHD mode and frame de-duplication are off. A tested CPU+TTA run was
numerically tied with GPU fp32+TTA; GPU without TTA differed substantially. The engine and model
are per-machine profile tools (``rife_engine``, ``rife_model_dir``); nothing is assumed from
another host.
"""
from __future__ import annotations

from pathlib import Path

from fstk.core.errors import ToolError
from fstk.tools import process


class Rife:
    def __init__(self, engine: str | Path, model_dir: str | Path, *, log_dir: Path | None = None):
        self.engine, self.model_dir, self.log_dir = str(engine), str(model_dir), log_dir

    @classmethod
    def from_profile(cls, profile, *, log_dir: Path | None = None) -> Rife:
        return cls(profile.tool("rife_engine"), profile.tool("rife_model_dir"), log_dir=log_dir)

    @property
    def method(self) -> str:
        """Provenance label recorded per frame, e.g. ``RIFE-4.26-CPU-TTA`` (from the model folder name)."""
        name = Path(self.model_dir).name
        version = name.removeprefix("rife-v").removeprefix("rife-")
        return f"RIFE-{version}-CPU-TTA"

    def command(self, prev: Path, nxt: Path, out: Path) -> list[str]:
        return [self.engine, "-0", str(prev), "-1", str(nxt), "-o", str(out), "-s", "0.5", "-g", "-1", "-x", "-z",
                "-m", self.model_dir, "-j", "1:4:1"]

    def interpolate(self, prev: Path, nxt: Path, out: Path, *, cancel=None) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        log = self.log_dir / f"rife-{out.stem}.log" if self.log_dir else None
        res = process.run(self.command(prev, nxt, out), log_path=log, cancel=cancel)
        if res.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
            raise ToolError(f"RIFE failed for {out.name}: {res.describe()}")
