"""Tests for the macOS padfix port (build_padfix_vpy.py).

End-to-end still cannot run in-process: the source-derived chain and the pad
audit/RIFE artefacts only exist once those stages have run. These tests
therefore cover the parts that carry the risk - the colour conversion (both
4:2:0 and this episode's 4:2:2), the audit/report parsing, and the
frame-substitution mechanics of the emitted script.
"""
from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ldcommon import PipelineError  # noqa: E402
from scripts.build_padfix_vpy import (  # noqa: E402
    load_payloads, rgb_to_yuv420p10, rgb_to_yuv_p10)

if importlib.util.find_spec("vapoursynth") is not None:
    import vapoursynth as vs  # noqa: E402
else:  # the source-only Windows test run does not install the Mac-only frameserver
    vs = None


@unittest.skipUnless(vs is not None, "requires the selected VapourSynth Python environment")
class ColorConversionTests(unittest.TestCase):
    def test_white_maps_to_limited_range_peak(self):
        import numpy as np
        rgb = np.full((4, 4, 3), 255, dtype=np.uint8)
        y, u, v = rgb_to_yuv420p10(rgb)
        self.assertEqual(y.dtype, np.uint16)
        self.assertEqual(y.shape, (4, 4))
        self.assertEqual(u.shape, (2, 2))          # 4:2:0 subsample
        # ConvertBits 8->10 scales by 1023/255 (not a floor): 235 -> 943
        self.assertAlmostEqual(int(y.min()), 235 * 1023 / 255, delta=1.0)
        self.assertEqual(int(u.max()), int(u.min()))         # neutral chroma
        self.assertLessEqual(int(y.max()), 1023)

    def test_black_maps_to_limited_range_floor(self):
        import numpy as np
        y, u, v = rgb_to_yuv420p10(np.zeros((4, 4, 3), dtype=np.uint8))
        self.assertAlmostEqual(int(y.max()), 16 * 1023 / 255, delta=1.0)
        # neutral chroma in 10-bit after the same 1023/255 scale: 128 -> 514
        self.assertAlmostEqual(float(u.mean()), 128 * 1023 / 255, delta=2.0)
        self.assertAlmostEqual(float(v.mean()), 128 * 1023 / 255, delta=2.0)

    def test_bt601_luma_weights(self):
        import numpy as np
        # pure green -> 0.587 of luma range above black
        rgb = np.zeros((4, 4, 3), dtype=np.uint8)
        rgb[:, :, 1] = 255
        y, _, _ = rgb_to_yuv420p10(rgb)
        expect = 16 + 0.587 * 219
        self.assertAlmostEqual(float(y.mean()), expect * 1023 / 255, delta=1.5)

    def test_odd_dimensions_rejected_by_shape(self):
        import numpy as np
        rgb = np.zeros((5, 7, 3), dtype=np.uint8)
        # 7x5 is not divisible by the 2x2 chroma block, so it must be rejected
        # before the reshape. The original code let numpy's reshape raise a
        # bare ValueError; the guard now reports the raster and the block.
        with self.assertRaises(PipelineError):
            rgb_to_yuv420p10(rgb)

    def test_422_subsamples_horizontally_only(self):
        """S01E10's source chain is YUV422P10 (ProRes 422 stack master).

        V5's build_padfix_avs.py accepts 4:2:0/4:2:2/4:4:4 and picks the
        matching ConvertToYUV42X, so chroma must follow the source's own
        subsampling rather than a hard 4:2:0.
        """
        import numpy as np
        rgb = np.zeros((8, 8, 3), dtype=np.uint8)
        y, u, v = rgb_to_yuv_p10(rgb, 1, 0)
        self.assertEqual(y.shape, (8, 8))         # luma stays full resolution
        self.assertEqual(u.shape, (8, 4))         # halved horizontally only
        self.assertEqual(v.shape, (8, 4))
        # 4:4:4 must not average at all
        _, u444, _ = rgb_to_yuv_p10(rgb, 0, 0)
        self.assertEqual(u444.shape, (8, 8))


@unittest.skipUnless(vs is not None, "requires the selected VapourSynth Python environment")
class PayloadLoadingTests(unittest.TestCase):
    """audit/report parsing + PNG loading + hold-prev, against a synthetic source."""

    def _fixture(self, tmp: Path):
        core = vs.core
        src = core.std.BlankClip(format=vs.YUV420P10, width=64, height=48,
                                 length=100, color=[800, 512, 512])
        # pad audit: two pads inside, one outside range (must be rejected)
        audit = tmp / "SELF-pad-audit.tsv"
        with audit.open("w", newline="") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(["frame", "zero_max", "note"])
            w.writerow([10, 0, "pad"])
            w.writerow([11, 1, "not a pad"])      # zero_max != 0 -> ignored
            w.writerow([20, 0, "pad"])
            # frame 50 is intentionally NOT a pad here: it appears in the report
            # under another episode, which must be ignored rather than adopted.
        from PIL import Image
        import numpy as np
        png = tmp / "pad_000010.png"
        Image.fromarray(np.full((48, 64, 3), 255, dtype=np.uint8)).save(png)
        report = tmp / "rife-pad-replacements-local.tsv"
        with report.open("w", newline="") as f:
            # rife_padframes.py writes this TSV tab-separated (both the AviSynth
            # version and this port read it with delimiter="\t")
            w = csv.DictWriter(f, fieldnames=["episode", "frame", "method", "png"],
                               delimiter="\t")
            w.writeheader()
            w.writerow({"episode": "SELF", "frame": 10, "method": "rife-v4.6",
                        "png": str(png)})
            w.writerow({"episode": "SELF", "frame": 20,
                        "method": "hold-prev-scene-cut", "png": ""})
            w.writerow({"episode": "OTHER", "frame": 50, "method": "rife-v4.6",
                        "png": str(png)})        # other episode -> ignored
        return audit, report

    def test_payloads_and_frame_mechanics(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            audit, report = self._fixture(tmp)
            # per-frame varying luma: a uniform source would make the
            # hold-prev-scene-cut replacement indistinguishable from the original
            base = vs.core.std.BlankClip(format=vs.YUV420P10, width=64, height=48,
                                         length=100)
            src = vs.core.std.FrameEval(
                base, lambda n, f=None: vs.core.std.BlankClip(
                    base, color=[700 + n, 512, 512]))
            payloads = load_payloads(audit, report, "SELF", src)

            # audit rows with zero_max==0 for THIS label only
            self.assertEqual(sorted(payloads), [10, 20])
            # PNG pad -> white decoded to limited-range peak (1023/255 scale)
            self.assertAlmostEqual(int(payloads[10][0].max()),
                                   235 * 1023 / 255, delta=1.0)
            # hold-prev pad -> copies native source planes (frame 19 -> 719)
            self.assertEqual(int(payloads[20][0][0, 0]), 719)
            self.assertEqual(int(payloads[20][1][0, 0]), 512)

            # replicate the emitted script's mechanics
            padset = frozenset(payloads)

            def _write(n, f):
                if n not in payloads:
                    return f
                nf = f.copy()
                np.copyto(np.asarray(nf[0]), payloads[n][0])
                np.copyto(np.asarray(nf[1]), payloads[n][1])
                np.copyto(np.asarray(nf[2]), payloads[n][2])
                return nf

            repl = vs.core.std.ModifyFrame(src, clip=src, selector=_write)
            self.assertEqual(repl.num_frames, 100)
            # std.FrameEval(clip, selector[, prop_src]) - clip must be first
            out = vs.core.std.FrameEval(
                src, lambda n, f=None: repl if n in padset else src)
            self.assertEqual(out.num_frames, 100)      # frame count preserved

            changed = [n for n in range(100)
                       if int(np.asarray(out.get_frame(n)[0])[0, 0]) !=
                       int(np.asarray(src.get_frame(n)[0])[0, 0])]
            self.assertEqual(sorted(changed), [10, 20],
                             "exactly the pad frames must differ")

    def test_missing_method_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            audit, _ = self._fixture(tmp)
            report = tmp / "empty.tsv"
            report.write_text("episode\tframe\tmethod\tpng\n", encoding="utf-8")
            src = vs.core.std.BlankClip(format=vs.YUV420P10, width=64, height=48,
                                        length=100)
            from ldcommon import PipelineError
            with self.assertRaises(PipelineError):
                load_payloads(audit, report, "SELF", src)


if __name__ == "__main__":
    unittest.main()
