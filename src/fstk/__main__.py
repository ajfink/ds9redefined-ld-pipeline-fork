"""``python -m fstk …`` is the same as the ``fstk`` command (the launchers run fstk from its source folder)."""
from fstk.cli.main import main

raise SystemExit(main())
