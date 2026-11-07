"""
posterior.py

Bayesian posterior update, ported 1:1 from simulation_hmm.html's step 3 ("PRIOR /
LIKELIHOOD / POSTERIOR"):
  1. pi(S): normalize each candidate's (non-negative score + epsT) into a probability
     distribution -- the "accumulated evidence" prior.
  2. transition prior: hybrid prior = T_same_dynamic * previous posterior +
     (1 - T_same_dynamic) * pi(S). T_same_dynamic decays from t_same_base toward 1/N as
     the gap since the last detection on this plate grows, so a long gap trusts
     accumulated evidence over short-term chain memory.
  3. likelihoods: MCAP's own per-candidate confidence for this detection (from a
     top-k prediction map), floored at epsilon.
  4. posteriors: likelihood * transition_prior, renormalized to sum to 1.
"""
import math
from datetime import datetime
from typing import Dict, Iterable, Optional, Tuple

from .ranking import pick_best_key


def compute_t_same_dynamic(n_candidates: int, gap_days: float, t_same_base: float, lambda_base: float) -> float:
    """T_same uses the SHARED gapDays + static lambda_base (plate-level frame-to-frame continuity)."""
    n = max(n_candidates, 1)
    t_min = 1.0 / n
    return t_min + (t_same_base - t_min) * math.exp(-lambda_base * gap_days)


def compute_pi_score(scores: Dict[str, float], eps_t: float) -> Dict[str, float]:
    """Normalizes max(0, score) + epsT per candidate into a probability distribution."""
    weights = {k: max(0.0, v) + eps_t for k, v in scores.items()}
    total = sum(weights.values())
    if total <= 0:
        n = len(weights) or 1
        return {k: 1.0 / n for k in weights}
    return {k: v / total for k, v in weights.items()}


def compute_transition_prior(candidates: Iterable[str], prev_posteriors: Dict[str, float],
                              pi_score: Dict[str, float], t_same_dynamic: float) -> Dict[str, float]:
    return {
        c: t_same_dynamic * prev_posteriors.get(c, 0.0) + (1 - t_same_dynamic) * pi_score[c]
        for c in candidates
    }


def compute_likelihoods(candidates: Iterable[str], top_k_confidences: Dict[str, float], epsilon: float) -> Dict[str, float]:
    """
    top_k_confidences maps candidate key -> MCAP's own confidence for that make/model on
    this detection (e.g. from a top-5 prediction list). Candidates absent from the map
    get the epsilon floor -- matches: `Math.max((rec.top_5_models||{})[c] ?? 0.0, epsilon)`.
    """
    return {c: max(top_k_confidences.get(c, 0.0), epsilon) for c in candidates}


def compute_posteriors(candidates: Iterable[str], likelihoods: Dict[str, float],
                        transition_prior: Dict[str, float],
                        recency: Optional[Dict[str, datetime]] = None,
                        ) -> Tuple[Dict[str, float], Optional[str], float]:
    """Returns (posteriors, best_candidate_key, max_posterior).

    :param recency: optional {candidate_key: lastSeen} tie-break -- when two
        candidates land on the exact same posterior (most commonly: tied scores that
        never got reinforced by a real detection), prefer the one seen more recently
        instead of silently picking whichever came first in `candidates`' order.
    """
    candidates = list(candidates)
    raw = {c: likelihoods[c] * transition_prior[c] for c in candidates}
    total = sum(raw.values())
    posteriors = {c: (v / total if total > 0 else transition_prior[c]) for c, v in raw.items()}

    if not candidates:
        return posteriors, None, -1.0
    best_candidate = pick_best_key(posteriors, recency)
    return posteriors, best_candidate, posteriors[best_candidate]
