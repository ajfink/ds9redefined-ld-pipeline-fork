#!/usr/bin/env python3
"""Run the repository's unittest suite with normalized temporary paths."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def configure_test_environment() -> Path:
    """Resolve the temp root once so symlinked POSIX /var roots compare consistently."""
    tests_root = ROOT / "tests"
    if str(tests_root) not in sys.path:
        sys.path.insert(0, str(tests_root))
    from support import normalize_temp_root

    temp_root = normalize_temp_root()
    for key in ("TMPDIR", "TMP", "TEMP"):
        os.environ[key] = str(temp_root)
    for value in (ROOT, ROOT / "scripts", ROOT / "tests"):
        raw = str(value)
        if raw not in sys.path:
            sys.path.insert(0, raw)
    return temp_root


def main(argv: list[str] | None = None) -> int:
    configure_test_environment()
    verbosity = 2
    if argv:
        if argv not in ([], ["-q"], ["--quiet"]):
            print("usage: python scripts/run_tests.py [-q|--quiet]", file=sys.stderr)
            return 2
        if argv:
            verbosity = 1
    suite = unittest.TestLoader().discover(
        str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
