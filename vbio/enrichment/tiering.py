"""
tiering.py

Final tier decision, ported 1:1 from simulation_hmm.html's "PACK THE RESULT" step:
    OBSERVED — confidence >= Th1: MCAP's own read is trusted directly.
    HIGH     — Bayes posterior clears Th3 AND the winning candidate is alive (score > 0).
    MEDIUM   — Th3 not cleared / winner not alive: fall back to the highest-scoring
               candidate that shares the SAME MAKE as the Bayes winner.
    LOW      — no same-make candidate alive: fall back to the highest-scoring candidate
               on the whole plate, regardless of make.
    NONE     — nothing alive on the plate at all: Unknown.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

from .keys import make_from_key
from .ranking import pick_best_key


@dataclass
class TierResult:
    branch: str                  # 'direct' | 'hmm-bayes'
    tier: str                    # OBSERVED | HIGH | MEDIUM | LOW | NONE
    result_key: Optional[str]    # the winning candidate key (None only for NONE)
    rescued_by_registry_fallback: bool = False
    rescued_by_top_score: bool = False
    discarded_by_th3: bool = False


def decide_tier(
    is_mmc_detected: bool,
    top_key: Optional[str],
    best_candidate: Optional[str],
    max_posterior: float,
    scores: Dict[str, float],
    th3: float,
    recency: Optional[Dict[str, datetime]] = None,
) -> TierResult:
    """
    :param recency: optional {candidate_key: lastSeen} tie-break for the MEDIUM/LOW
        rescue picks below -- e.g. two VBIo-seeded candidates that never get
        reinforced by a real detection decay identically forever, so without this
        the rescue would silently pick whichever happened to load first instead of
        the one actually registered/seen more recently.
    """
    if is_mmc_detected:
        return TierResult(branch="direct", tier="OBSERVED", result_key=top_key)

    pass_th3 = max_posterior >= th3
    is_top1_active = best_candidate is not None and scores.get(best_candidate, 0.0) > 0

    if best_candidate is not None and pass_th3 and is_top1_active:
        return TierResult(branch="hmm-bayes", tier="HIGH", result_key=best_candidate)

    active_all = [k for k, s in scores.items() if s > 0]

    if best_candidate is not None:
        make = make_from_key(best_candidate)
        same_make = [k for k in active_all if make_from_key(k) == make]
        if same_make:
            rescued = pick_best_key({k: scores[k] for k in same_make}, recency)
            return TierResult(
                branch="hmm-bayes", tier="MEDIUM", result_key=rescued,
                rescued_by_registry_fallback=True,
            )

    if active_all:
        rescued = pick_best_key({k: scores[k] for k in active_all}, recency)
        return TierResult(
            branch="hmm-bayes", tier="LOW", result_key=rescued,
            rescued_by_registry_fallback=True, rescued_by_top_score=True,
        )

    return TierResult(branch="hmm-bayes", tier="NONE", result_key=None, discarded_by_th3=True)
