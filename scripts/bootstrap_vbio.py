#!/usr/bin/env python3
"""Convenience wrapper: `python scripts/bootstrap_vbio.py ...` == `python -m vbio.schema.vbio_schema ...`"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vbio.schema.vbio_schema import _main  # noqa: E402

if __name__ == "__main__":
    _main()
