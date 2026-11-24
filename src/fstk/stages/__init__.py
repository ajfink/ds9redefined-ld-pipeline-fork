"""Stage implementations. Importing this package registers every built-in stage."""
from . import (  # noqa: F401
    alignment,
    audioquality,
    avcorrection,
    cadence,
    dvd,
    dvdaudio,
    dvdborrow,  # helpers only: no stage
    dvdfree,
    efm,
    encode,
    epreport,
    framefix,
    intake,
    qc,
    quality,
    remux,
    retime,
    stack,
    stretchpreview,
    trim,
)
from .base import REGISTRY, Stage, get_stage, register  # noqa: F401
