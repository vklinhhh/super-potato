#!/usr/bin/env python3
"""Convenience wrapper: `python scripts/load_redis.py ...` == `python -m vbio.loaders.vbio_redis_loader ...`"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vbio.loaders.vbio_redis_loader import _main  # noqa: E402

if __name__ == "__main__":
    _main()
