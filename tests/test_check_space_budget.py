from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.check_space_budget import evaluate_budget  # noqa: E402


class SpaceBudgetTests(unittest.TestCase):
    def test_passes_when_projected_free_meets_reserve(self):
        passed, projected = evaluate_budget(120, 20, 100)
        self.assertTrue(passed)
        self.assertEqual(projected, 100)

    def test_fails_when_projection_dips_below_reserve(self):
        passed, projected = evaluate_budget(119, 20, 100)
        self.assertFalse(passed)
        self.assertEqual(projected, 99)


if __name__ == "__main__":
    unittest.main()
