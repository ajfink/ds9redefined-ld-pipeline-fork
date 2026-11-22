"""Stage implementations. Importing this package registers every built-in stage."""
from . import alignment, cadence, dvd, dvdfree, efm, encode, intake, qc, remux, stack  # noqa: F401
from .base import REGISTRY, Stage, get_stage, register  # noqa: F401
