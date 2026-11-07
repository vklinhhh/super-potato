"""
scoring.py

Score decay and score-update math.
  - decay_all(): per-candidate exponential decay since its own lastDecayedAt (not a
    shared plate-wide clock -- a quiet candidate isn't decayed any faster just because
    another candidate on the same plate is noisy). Ported 1:1 from simulation_hmm.html,
    EXCEPT: decay is skipped entirely when a plate has fewer than 2 candidates (see
    decay_all()'s own docstring) -- a deliberate departure from the 1:1 port, not an
    oversight.
  - apply_high_confidence_update() / apply_low_confidence_update(): the winner's score
    update is an EMA (exponential moving average) toward this detection's confidence --
    NOT simulation_hmm.html's literal `score += confidence` / `score += malus_low_rate *
    confidence`. The additive version has no ceiling: a plate detected often enough
    reaches a steady-state score of roughly (detections/day * avg confidence) / lambda_base,
    which can run into the hundreds or thousands for a busy plate -- blowing past both
    the VBIo schema's documented CredibilityScore range (0.0-100.0) and the 16-bit
    (0.00-655.35) fixed-point field RFC-01's Redis layout budgets for it. EMA keeps
    `score` bounded to roughly [0, 1] by construction (a weighted average of a past
    score and a confidence in [0, 1] can never exceed the larger of the two), with no
    cap/clamp needed and no change to decide_tier()'s comparisons (which only ever look
    at relative ordering between candidates on the same plate, never absolute magnitude).
    Malus (the penalty to every OTHER candidate) is untouched -- it was already
    multiplicative/self-limiting (`score *= (1 - malus_rate*confidence)`), so it never
    contributed to the runaway-growth problem in the first place.
    See ENGINE_LOGIC_FLOW.md section 5.3/8 and config.py's EnrichmentConfig.alpha for
    the full rationale and the accuracy sweep this was tuned against.
"""
import math
from datetime import datetime
from typing import List


def decay_score(score: float, last_decayed_at: datetime, now: datetime, lambda_base: float) -> float:
    """
    Exponential decay of a single candidate's score, using ITS OWN elapsed time since
    it was last decayed. Matches: r.score *= Math.exp(-lambdaBase * gapDaysForR)
    """
    gap_days = max(0.0, (now - last_decayed_at).total_seconds() / 86400.0)
    return score * math.exp(-lambda_base * gap_days)


def decay_all(records: List[dict], now: datetime, lambda_base: float) -> None:
    """
    In-place decay of every candidate record's `score`; also refreshes `lastDecayedAt`.

    Skips decay (but still refreshes lastDecayedAt) when the plate has fewer than 2
    candidates -- decay exists to let competing evidence out-age stale evidence in a
    COMPARISON; a lone candidate that has never been contradicted by anything has
    nothing to lose ground to, so there's no comparison for decay to serve. Without
    this, a make/model MCAP's vision model was never trained to recognize at all (so it
    can structurally never be reconfirmed) decays to below the eps_t floor and becomes
    functionally invisible to every posterior/tier comparison in ~1.8 years (seed_base
    0.7 * HmmPosterior 0.9, lambda_base 0.00385) regardless of how correct it still is
    -- see ENGINE_LOGIC_FLOW.md section 5.3/8 for the worked-out timeline and the
    97-profile accuracy sweep this was validated against (net +1, zero regressions:
    the moment a 2nd real candidate exists, decay resumes normally for everyone, so
    genuine multi-candidate competition is unaffected).
    """
    if len(records) < 2:
        for r in records:
            r["lastDecayedAt"] = now
        return
    for r in records:
        r["score"] = decay_score(r["score"], r["lastDecayedAt"], now, lambda_base)
        r["lastDecayedAt"] = now


def apply_high_confidence_update(records: List[dict], winner_key: str, confidence: float,
                                  now: datetime, malus_rate: float, alpha: float) -> None:
    """
    conf >= Th1 branch:
        winner.score = alpha * confidence + (1 - alpha) * winner.score  (EMA, bounded)
        winner.observationCount += 1; winner.lastSeen = now
        every OTHER candidate pays a proportional malus: score -= malus_rate * confidence * max(0, score)
    """
    for r in records:
        if r["key"] == winner_key:
            r["score"] = alpha * confidence + (1 - alpha) * r["score"]
            r["observationCount"] = r.get("observationCount", 0) + 1
            r["lastSeen"] = now
        else:
            malus = malus_rate * confidence * max(0.0, r["score"])
            r["score"] -= malus


def apply_low_confidence_update(record: dict, confidence: float, alpha: float) -> None:
    """
    conf < Th1 branch, only applied when the read's top candidate already exists in Redis:
        winner.score = alpha * confidence + (1 - alpha) * winner.score  (same EMA formula
        as the high-confidence branch -- a lower confidence already pulls the average
        less by construction, so no separate dampening factor is needed here)
        (no malus to others, no admission of new candidates, observationCount unchanged --
        a weak read is activity only, not a confirmed sighting, so lastSeen is unchanged.
        decay_all() has already refreshed lastDecayedAt for every candidate in this lookup.)
    """
    record["score"] = alpha * confidence + (1 - alpha) * record["score"]
