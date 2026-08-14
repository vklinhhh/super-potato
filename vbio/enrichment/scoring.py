"""
scoring.py

Score decay and score-update math, ported 1:1 from simulation_hmm.html's
computeHMMSequence() steps 1 and 4:
  - decay_all(): per-candidate exponential decay since its own lastDecayedAt (not a
    shared plate-wide clock -- a quiet candidate isn't decayed any faster just because
    another candidate on the same plate is noisy).
  - apply_high_confidence_update(): conf >= Th1 -- the winning candidate gains conf,
    and every OTHER live candidate on the plate pays a proportional, confidence-scaled
    malus.
  - apply_low_confidence_update(): conf < Th1 but the read's top candidate already
    exists in Redis -- small partial credit only, no malus, no admission.
"""
import math
from datetime import datetime
from typing import Iterable


def decay_score(score: float, last_decayed_at: datetime, now: datetime, lambda_base: float) -> float:
    """
    Exponential decay of a single candidate's score, using ITS OWN elapsed time since
    it was last decayed. Matches: r.score *= Math.exp(-lambdaBase * gapDaysForR)
    """
    gap_days = max(0.0, (now - last_decayed_at).total_seconds() / 86400.0)
    return score * math.exp(-lambda_base * gap_days)


def decay_all(records: Iterable[dict], now: datetime, lambda_base: float) -> None:
    """In-place decay of every candidate record's `score`; also refreshes `lastDecayedAt`."""
    for r in records:
        r["score"] = decay_score(r["score"], r["lastDecayedAt"], now, lambda_base)
        r["lastDecayedAt"] = now


def apply_high_confidence_update(records: Iterable[dict], winner_key: str, confidence: float,
                                  now: datetime, malus_rate: float) -> None:
    """
    conf >= Th1 branch:
        winner.score += confidence; winner.observationCount += 1; winner.lastSeen = now
        every OTHER candidate pays a proportional malus: score -= malus_rate * confidence * max(0, score)
    """
    for r in records:
        if r["key"] == winner_key:
            r["score"] += confidence
            r["observationCount"] = r.get("observationCount", 0) + 1
            r["lastSeen"] = now
        else:
            malus = malus_rate * confidence * max(0.0, r["score"])
            r["score"] -= malus


def apply_low_confidence_update(record: dict, confidence: float, now: datetime, malus_low_rate: float) -> None:
    """
    conf < Th1 branch, only applied when the read's top candidate already exists in Redis:
        winner.score += malus_low_rate * confidence; winner.lastSeen = now
        (no malus to others, no admission of new candidates, observationCount unchanged --
        a weak read is still a real sighting, but not a confirmed hit)
    """
    record["score"] += malus_low_rate * confidence
    record["lastSeen"] = now
