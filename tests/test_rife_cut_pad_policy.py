from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ldcommon import PipelineError  # noqa: E402
from rife_padframes import (is_device_instance_cut_pad_image, require_color_match_note,
                            validate_cut_replacement_policy)  # noqa: E402


class CutPadPolicyTests(unittest.TestCase):
    def test_dvd_backed_cut_requires_a_reviewed_image_and_forbids_hold(self):
        episode = {"dvd_reference_available": True,
                   "pads": {"cut_pads": 1, "cut_pad_frames": [100]}}
        self.assertEqual(validate_cut_replacement_policy(episode, {100, 200}, set(), {100}), {100})
        with self.assertRaisesRegex(PipelineError, "need reviewed per-device DVD stills"):
            validate_cut_replacement_policy(episode, {100, 200}, set(), set())
        with self.assertRaisesRegex(PipelineError, "only a no-DVD fallback"):
            validate_cut_replacement_policy(episode, {100, 200}, {100}, set())

    def test_no_dvd_still_or_hold_requires_explicit_user_approval(self):
        base = {"dvd_reference_available": False,
                "no_dvd_proceed_approved": True,
                "pads": {"cut_pads": 1, "cut_pad_frames": [100]}}
        image_choice = base | {"cut_pad_images_choice": "use"}
        self.assertEqual(validate_cut_replacement_policy(image_choice, {100, 200}, set(), {100}), {100})
        hold_choice = base | {"cut_pad_images_choice": "skip"}
        self.assertEqual(validate_cut_replacement_policy(hold_choice, {100, 200}, {100}, set()), {100})
        with self.assertRaisesRegex(PipelineError, "explicit no-DVD choice"):
            validate_cut_replacement_policy(base | {"no_dvd_proceed_approved": False},
                                            {100}, {100}, set())

    def test_cut_inventory_must_be_unique_confirmed_and_exact(self):
        episode = {"dvd_reference_available": True,
                   "pads": {"cut_pads": 2, "cut_pad_frames": [100, 100]}}
        with self.assertRaisesRegex(PipelineError, "exact, unique"):
            validate_cut_replacement_policy(episode, {100}, set(), {100})
        episode["pads"] = {"cut_pads": 1, "cut_pad_frames": [101]}
        with self.assertRaisesRegex(PipelineError, "not all confirmed candidates"):
            validate_cut_replacement_policy(episode, {100}, set(), {101})
        episode["pads"] = {"cut_pads": 1, "cut_pad_frames": [100]}
        with self.assertRaisesRegex(PipelineError, "only manifest-confirmed"):
            validate_cut_replacement_policy(episode, {100, 200}, set(), {200})
        episode["pads"] = {"cut_pads": 1.0, "cut_pad_frames": [100.5]}
        with self.assertRaisesRegex(PipelineError, "integer frame values"):
            validate_cut_replacement_policy(episode, {100}, set(), {100})

    def test_dvd_still_must_be_in_this_episode_inbox_and_have_color_note(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp) / "bundle"
            image = (bundle / "devices" / "MAC-ABCD1234" / "episodes" / "S01E11" /
                     "cut-pad-images" / "DVD-00-12-42.762-f00018288.png")
            image.parent.mkdir(parents=True)
            self.assertTrue(is_device_instance_cut_pad_image(image, bundle, "S01E11"))
            self.assertFalse(is_device_instance_cut_pad_image(image, bundle, "S01E12"))
            sidecar = image.with_suffix(".provenance.json")
            sidecar.write_text(json.dumps({"color_match_note": "stable-background match"}),
                               encoding="utf-8")
            self.assertEqual(require_color_match_note(image), "stable-background match")
            sidecar.write_text(json.dumps({"color_match_note": "  "}), encoding="utf-8")
            with self.assertRaisesRegex(PipelineError, "color_match_note"):
                require_color_match_note(image)


if __name__ == "__main__":
    unittest.main()
