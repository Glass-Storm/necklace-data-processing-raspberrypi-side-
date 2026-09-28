"""``python -m ecosys_pi`` entry point: delegate to :func:`ecosys_pi.cli.main`."""

from __future__ import annotations

import sys

from ecosys_pi.cli import main

if __name__ == "__main__":
    sys.exit(main())
