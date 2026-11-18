"""Small shared numeric helpers used by diagnostic measurement scripts."""
from __future__ import annotations


def normalize_rows(values):
    """Center and L2-normalize rows, leaving zero-norm rows as zero vectors."""
    import numpy as np

    rows = values.astype(np.float32, copy=True)
    rows -= rows.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return rows / norms
