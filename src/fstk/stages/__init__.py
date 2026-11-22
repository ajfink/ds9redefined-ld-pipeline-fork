"""Stage implementations. Importing this package registers every built-in stage."""
from . import (  # noqa: F401
    alignment,
    avcorrection,
    cadence,
    dvd,
    dvdaudio,
    dvdfree,
    efm,
    encode,
    intake,
    qc,
    remux,
    retime,
    stack,
)
from .base import REGISTRY, Stage, get_stage, register  # noqa: F401
