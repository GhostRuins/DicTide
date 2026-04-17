"""
GUI entry for development and for PyInstaller builds.
"""
from __future__ import annotations

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    _root = Path(__file__).resolve().parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from src.main import main

if __name__ == "__main__":
    main()
