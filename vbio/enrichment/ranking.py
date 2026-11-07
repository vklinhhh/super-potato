"""
ranking.py

pick_best_key(): argmax over a {key: score} dict with a deterministic tie-break.

Without this, every max(dict, key=...) in the engine ties-break on dict/list
insertion order -- which for VBIo-seeded candidates is just whatever order the
parquet query happened to return them in (e.g. ascending surrogate Id), NOT any
signal that one candidate is more likely correct than another. A plate seeded with
two VBIo candidates that never get reinforced by a real detection (decay identically
forever) would then always resolve to whichever one happened to load first --
regardless of which one was actually registered more recently.

Tie-break rule: highest score wins; if scores are equal (including the common "both
decayed identically, never reinforced" case), prefer the candidate with the more
recent `recency` timestamp (typically lastSeen).
"""
from datetime import datetime
from typing import Dict, Optional


def pick_best_key(scores: Dict[str, float], recency: Optional[Dict[str, datetime]] = None) -> str:
    if not recency:
        return max(scores, key=lambda k: scores[k])
    return max(scores, key=lambda k: (scores[k], recency.get(k) or datetime.min))
