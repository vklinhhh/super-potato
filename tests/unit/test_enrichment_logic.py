import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vbio.enrichment.posterior import (
    compute_likelihoods,
    compute_pi_score,
    compute_posteriors,
    compute_t_same_dynamic,
    compute_transition_prior,
)
from vbio.enrichment.tiering import decide_tier


def test_compute_pi_score_normalizes_live_scores():
    scores = {"A": 2.0, "B": 1.0}
    pi = compute_pi_score(scores, eps_t=0.05)

    assert math.isclose(sum(pi.values()), 1.0, rel_tol=1e-9)
    assert pi["A"] > pi["B"]


def test_compute_posteriors_selects_best_candidate_after_likelihood_update():
    candidates = ["A", "B"]
    prior = {"A": 0.55, "B": 0.45}
    likelihoods = {"A": 0.90, "B": 0.20}
    posterior, best, max_posterior = compute_posteriors(candidates, likelihoods, prior)

    assert best == "A"
    assert posterior["A"] > posterior["B"]
    assert max_posterior > 0.5


def test_decide_tier_honors_high_priority_flow():
    tier = decide_tier(
        is_mmc_detected=False,
        top_key=None,
        best_candidate="A",
        max_posterior=0.93,
        scores={"A": 2.4, "B": 0.3},
        th3=0.80,
    )

    assert tier.tier == "HIGH"
    assert tier.result_key == "A"


def test_decide_tier_falls_back_to_same_make_medium_path():
    tier = decide_tier(
        is_mmc_detected=False,
        top_key=None,
        best_candidate="ford_flex",
        max_posterior=0.70,
        scores={"ford_focus": 1.4, "ford_flex": 2.2, "toyota_camry": 0.9},
        th3=0.80,
    )

    assert tier.tier == "MEDIUM"
    assert tier.result_key in {"ford_focus", "ford_flex"}


def test_decide_tier_uses_low_path_when_no_same_make_candidate_exists():
    tier = decide_tier(
        is_mmc_detected=False,
        top_key=None,
        best_candidate="ford_flex",
        max_posterior=0.50,
        scores={"toyota_camry": 1.7, "ford_flex": 0.0},
        th3=0.80,
    )

    assert tier.tier == "LOW"
    assert tier.result_key == "toyota_camry"


def test_decide_tier_returns_none_when_no_candidate_is_alive():
    tier = decide_tier(
        is_mmc_detected=False,
        top_key=None,
        best_candidate=None,
        max_posterior=0.10,
        scores={"A": -0.1, "B": 0.0},
        th3=0.80,
    )

    assert tier.tier == "NONE"
    assert tier.result_key is None


def test_flow_matches_the_enrichment_diagram_steps():
    scores = {"A": 1.0, "B": 0.5}
    pi = compute_pi_score(scores, eps_t=0.05)
    transition = compute_transition_prior(
        ["A", "B"],
        {"A": 0.8, "B": 0.2},
        pi,
        compute_t_same_dynamic(2, 0.0, 0.999, 0.00385),
    )
    likelihoods = compute_likelihoods(["A", "B"], {"A": 0.95, "B": 0.25}, 0.01)
    posterior, best, _ = compute_posteriors(["A", "B"], likelihoods, transition)

    assert best == "A"
    assert posterior["A"] > 0.5

    final = decide_tier(False, None, best, posterior[best], {"A": 3.0, "B": 0.2}, 0.80)
    assert final.tier == "HIGH"
