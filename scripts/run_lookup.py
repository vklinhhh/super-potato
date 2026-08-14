#!/usr/bin/env python3
"""Convenience wrapper: `python scripts/run_lookup.py ...` == `python -m vbio.vehicle_lookup_service ...`"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vbio.vehicle_lookup_service import _main  # noqa: E402

if __name__ == "__main__":
    _main()
