from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402
from numeric import normalize_rows  # noqa: E402


class NormalizeRowsTests(unittest.TestCase):
    def test_centers_and_normalizes_rows_and_keeps_zero_rows_zero(self):
        values = np.array([[1, 2, 3], [4, 4, 4]], dtype=np.uint8)
        result = normalize_rows(values)
        expected = np.array([[-1, 0, 1], [0, 0, 0]], dtype=np.float32)
        expected[0] /= np.linalg.norm(expected[0])
        np.testing.assert_array_equal(result, expected)
        self.assertEqual(result.dtype, np.float32)
        self.assertEqual(values.dtype, np.uint8)


if __name__ == "__main__":
    unittest.main()
