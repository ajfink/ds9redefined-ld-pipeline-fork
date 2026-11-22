"""Stage implementations. Importing this package registers every built-in stage."""
from . import (  # noqa: F401
    alignment,
    audioquality,
    avcorrection,
    cadence,
    dvd,
    dvdaudio,
    dvdfree,
    efm,
    encode,
    epreport,
    intake,
    pads,
    qc,
    quality,
    remux,
    retime,
    stack,
    trim,
)
from .base import REGISTRY, Stage, get_stage, register  # noqa: F401
