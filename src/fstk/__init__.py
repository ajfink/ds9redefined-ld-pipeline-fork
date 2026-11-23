"""FromStunToKill (fstk): evidence-gated LaserDisc capture-to-delivery pipeline."""
__version__ = "1.0.0.dev0"

import logging as _logging

_logging.getLogger("fstk").addHandler(_logging.NullHandler())   # no stray output until fstk.core.applog sets up the file
