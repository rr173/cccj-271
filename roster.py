#!/usr/bin/env python3
"""Convenience launcher for the roster reconstruction CLI.

Explicitly puts this file's directory on ``sys.path`` so that ``python -I
roster.py ...`` works even though isolated mode does not add the script
directory automatically.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from roster_reconstruct.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
