"""Small shared test helpers for path-stable fixtures."""
from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def normalize_temp_root() -> Path:
    """Pin tempfile's cached root to its resolved spelling (notably macOS /private/var)."""
    root = Path(tempfile.gettempdir()).resolve(strict=False)
    tempfile.tempdir = str(root)
    return root


@contextmanager
def temporary_directory() -> Iterator[Path]:
    """Yield a resolved temp root so symlinked system temp paths compare consistently."""
    with tempfile.TemporaryDirectory() as temporary:
        yield Path(temporary).resolve(strict=False)
