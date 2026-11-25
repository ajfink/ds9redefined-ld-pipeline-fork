"""7-Zip adapter: list an archive and stream one validated member to a new file.

Ported from v38 ``efm_decode.extract_7z_member`` (used for ``.efm.7z`` sources). Extraction
never recreates archive paths (``e -so`` streams one member to stdout) and never overwrites.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from fstk.core.errors import ArtifactError, ToolError
from fstk.tools import process


class SevenZip:
    def __init__(self, exe: str | Path):
        self.exe = str(exe)

    def list_slt(self, archive: Path) -> str:
        res = subprocess.run([self.exe, "l", "-slt", str(archive)], capture_output=True, check=False,
                             **process.hidden())
        if res.returncode:
            raise ToolError(f"7-Zip could not list {archive}: {res.stderr.decode('utf-8', 'replace')[-800:]}")
        return res.stdout.decode("utf-8", "replace")

    def extract_member(self, archive: Path, member: str, expected_size: int, output: Path) -> None:
        if output.exists() or output.is_symlink():
            raise ArtifactError(f"refusing to overwrite extracted file: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(output.name + ".partial")
        if partial.exists():
            raise ArtifactError(f"an earlier extraction left {partial}; inspect and move it aside first")
        cmd = [self.exe, "e", "-so", "-y", "-bso0", "-bsp0", "-bse2", str(archive), member]
        with partial.open("xb") as stream:
            res = subprocess.run(cmd, stdout=stream, stderr=subprocess.PIPE, check=False, **process.hidden())
            stream.flush()
        if res.returncode:
            raise ToolError(f"7-Zip extraction failed ({res.returncode}); partial output kept at {partial}: "
                            f"{res.stderr.decode('utf-8', 'replace')[-800:]}")
        actual = partial.stat().st_size
        if actual != expected_size:
            raise ArtifactError(f"extracted size {actual} differs from archive listing {expected_size}; "
                                f"partial output kept at {partial}")
        partial.rename(output)
