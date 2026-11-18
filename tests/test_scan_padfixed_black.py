from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from scan_padfixed_black import resolve_video_input  # noqa: E402


class ScanPadfixedInputTests(unittest.TestCase):
    def test_manifest_deliverable_pre_upscale_is_default_when_present(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            deliverable = root / "pre-upscale.mkv"
            padfixed = root / "padfixed.mkv"
            deliverable.write_bytes(b"final mux")
            padfixed.write_bytes(b"video only")
            ep = SimpleNamespace(data={"deliverable": "pre_upscale"},
                                 optional_path=lambda key: deliverable if key == "pre_upscale" else None,
                                 path=lambda key: padfixed)
            self.assertEqual(resolve_video_input(ep), deliverable)

    def test_intermediate_padfixed_is_fallback_if_deliverable_not_yet_present(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            padfixed = root / "padfixed.mkv"
            padfixed.write_bytes(b"video only")
            ep = SimpleNamespace(data={"deliverable": "pre_upscale"},
                                 optional_path=lambda key: root / "missing.mkv",
                                 path=lambda key: padfixed)
            self.assertEqual(resolve_video_input(ep), padfixed)

    def test_explicit_override_takes_precedence(self):
        ep = SimpleNamespace(data={"deliverable": "pre_upscale"},
                             optional_path=lambda key: Path("ignored.mkv"),
                             path=lambda key: Path("fallback.mkv"))
        self.assertEqual(resolve_video_input(ep, "reviewed.mkv"), Path("reviewed.mkv"))


if __name__ == "__main__":
    unittest.main()
