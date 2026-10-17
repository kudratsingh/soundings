"""Entry point for ``python -m soundings.audit``."""

from __future__ import annotations

import sys

from soundings.audit import main

if __name__ == "__main__":
    sys.exit(main())
