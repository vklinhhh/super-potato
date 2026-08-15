"""Unit tests for every decision branch in assets/enrichment_flow.png."""
from datetime import datetime, timezone

from vbio.clients.redis_store import InMemoryAssociationStore
from vbio.enrichment.engine import Detection, EnrichmentEngine
from vbio.enrichment.keys import build_redis_key
from vbio.enrichment.tiering import decide_tier

NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)
PLATE = "ABC123"
STATE = "NY"


def _key(make, model):
    return build_redis_key(PLATE, STATE, make, model)


def test_no_candidates_returns_none():
    """N = 0 follows the diagram's discard/Unknown branch."""
    result = EnrichmentEngine(InMemoryAssociationStore()).process_detection(
        Detection(plate_number=PLATE, plate_state=STATE, timestamp=NOW),
    )

    assert result.tier == "NONE"
    assert result.make is None
    assert result.model is None
    assert result.candidates == []


def test_posterior_gate_returns_high_for_live_candidate():
    """A posterior at Th3 with a live best candidate returns HIGH."""
    top_key = _key("Toyota", "Camry")
    tier = decide_tier(
        is_mmc_detected=False,
        top_key=None,
        best_candidate=top_key,
        max_posterior=0.80,
        scores={top_key: 1.0},
        th3=0.80,
    )

    assert tier.tier == "HIGH"
    assert tier.result_key == top_key


def test_failed_posterior_gate_rescues_highest_scoring_same_make():
    """A failed output gate returns the top active candidate from the Bayes winner's make."""
    bayes_winner = _key("Toyota", "Camry")
    same_make_rescue = _key("Toyota", "Corolla")
    other_make = _key("Honda", "Accord")
    tier = decide_tier(
        is_mmc_detected=False,
        top_key=None,
        best_candidate=bayes_winner,
        max_posterior=0.79,
        scores={bayes_winner: 0.2, same_make_rescue: 0.9, other_make: 2.0},
        th3=0.80,
    )

    assert tier.tier == "MEDIUM"
    assert tier.result_key == same_make_rescue


def test_missing_same_make_candidate_rescues_global_top_score():
    """If the Bayes winner's make has no live candidate, return the global active top score."""
    inactive_bayes_winner = _key("Toyota", "Camry")
    global_rescue = _key("Honda", "Accord")
    tier = decide_tier(
        is_mmc_detected=False,
        top_key=None,
        best_candidate=inactive_bayes_winner,
        max_posterior=0.79,
        scores={inactive_bayes_winner: 0.0, global_rescue: 0.9},
        th3=0.80,
    )

    assert tier.tier == "LOW"
    assert tier.result_key == global_rescue


def test_no_live_candidates_returns_none():
    """If all stored candidates have zero or negative scores, return Unknown."""
    tier = decide_tier(
        is_mmc_detected=False,
        top_key=None,
        best_candidate=_key("Toyota", "Camry"),
        max_posterior=0.79,
        scores={_key("Toyota", "Camry"): 0.0, _key("Honda", "Accord"): -0.1},
        th3=0.80,
    )

    assert tier.tier == "NONE"
    assert tier.result_key is None
