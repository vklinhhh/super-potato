"""Redis-backed scoring tests for the decay/high/low branches in redis_scoring_system.png."""
import math
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

from vbio.clients.redis_store import RedisAssociationStore
from vbio.enrichment.config import DEFAULT_CONFIG
from vbio.enrichment.engine import Detection, EnrichmentEngine
from vbio.enrichment.keys import build_redis_key
from vbio.enrichment.posterior import (
    compute_likelihoods,
    compute_pi_score,
    compute_posteriors,
    compute_t_same_dynamic,
    compute_transition_prior,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
PLATE = "ABC123"
STATE = "NY"


def _record(make, model, score, last_decayed_at, observation_count=3):
    return {
        "key": build_redis_key(PLATE, STATE, make, model),
        "plateNumber": PLATE,
        "plateState": STATE,
        "make": make,
        "model": model,
        "firstSeen": NOW - timedelta(days=30),
        "lastSeen": NOW - timedelta(days=1),
        "lastDecayedAt": last_decayed_at,
        "observationCount": observation_count,
        "score": score,
        "posterior": 0.5,
        "hmmPosterior": None,
        "status": "ACTIVE",
        "source": "VBIO",
    }


def _engine_with_records(records):
    store = RedisAssociationStore(fakeredis.FakeStrictRedis(decode_responses=True))
    store.save_candidates(records)
    store.set_last_update(PLATE, STATE, NOW - timedelta(days=1))
    return EnrichmentEngine(store, DEFAULT_CONFIG), store


def _by_model(store):
    return {record["model"]: record for record in store.get_candidates(PLATE, STATE)}


def test_decay_is_per_candidate_and_is_persisted_to_redis():
    engine, store = _engine_with_records([
        _record("Toyota", "Camry", 10.0, NOW - timedelta(days=2)),
        _record("Honda", "Accord", 10.0, NOW - timedelta(days=5)),
    ])

    engine.process_detection(Detection(plate_number=PLATE, plate_state=STATE, timestamp=NOW))
    records = _by_model(store)

    assert records["Camry"]["score"] == pytest.approx(10.0 * math.exp(-DEFAULT_CONFIG.lambda_base * 2))
    assert records["Accord"]["score"] == pytest.approx(10.0 * math.exp(-DEFAULT_CONFIG.lambda_base * 5))
    assert records["Camry"]["lastDecayedAt"] == NOW
    assert records["Accord"]["lastDecayedAt"] == NOW


def test_high_confidence_update_boosts_winner_penalizes_rival_and_persists():
    engine, store = _engine_with_records([
        _record("Toyota", "Camry", 2.0, NOW - timedelta(days=1), observation_count=3),
        _record("Honda", "Accord", 4.0, NOW - timedelta(days=1), observation_count=7),
    ])
    confidence = 0.95

    engine.process_detection(Detection(
        plate_number=PLATE, plate_state=STATE, make="Toyota", model="Camry",
        confidence=confidence, timestamp=NOW,
    ))
    records = _by_model(store)
    decayed_winner = 2.0 * math.exp(-DEFAULT_CONFIG.lambda_base)
    decayed_rival = 4.0 * math.exp(-DEFAULT_CONFIG.lambda_base)

    expected_winner = DEFAULT_CONFIG.alpha * confidence + (1 - DEFAULT_CONFIG.alpha) * decayed_winner
    expected_rival = decayed_rival * (1 - DEFAULT_CONFIG.malus_rate * confidence)
    assert records["Camry"]["score"] == pytest.approx(expected_winner)
    assert records["Camry"]["observationCount"] == 4
    assert records["Camry"]["lastSeen"] == NOW
    assert records["Accord"]["score"] == pytest.approx(
        expected_rival
    )
    assert records["Accord"]["observationCount"] == 7

    # Posterior is the full Bayes update: prior (PRE-update decayed scores blended
    # with the previous posterior via T_same) fused with this tick's likelihood (the
    # exact Camry match gets its confidence; Accord -- no same-make rival exists since
    # it's a different make -- floors at epsilon). Mirrors engine.py's step 2 exactly;
    # NOT the plain post-update score ratio, which would ignore the previous posterior
    # entirely (see the 9F84204_CA case study in ENGINE_LOGIC_FLOW.md for why that was
    # a real bug, not just an unused code path).
    keys = {model: record["key"] for model, record in records.items()}
    gap_days = 1.0  # NOW minus the plate's lastUpdate set in _engine_with_records
    t_same = compute_t_same_dynamic(2, gap_days, DEFAULT_CONFIG.t_same_base, DEFAULT_CONFIG.lambda_base)
    pi_score = compute_pi_score(
        {keys["Camry"]: decayed_winner, keys["Accord"]: decayed_rival}, DEFAULT_CONFIG.eps_t
    )
    prev_posteriors = {keys["Camry"]: 0.5, keys["Accord"]: 0.5}  # _record()'s default
    trans_p = compute_transition_prior([keys["Camry"], keys["Accord"]], prev_posteriors, pi_score, t_same)
    trans_total = sum(trans_p.values())
    trans_p = {k: v / trans_total for k, v in trans_p.items()}
    likelihoods = compute_likelihoods(
        [keys["Camry"], keys["Accord"]], {keys["Camry"]: confidence}, DEFAULT_CONFIG.epsilon
    )
    expected_posterior, _, _ = compute_posteriors([keys["Camry"], keys["Accord"]], likelihoods, trans_p)
    assert records["Camry"]["posterior"] == pytest.approx(expected_posterior[keys["Camry"]])


def test_low_confidence_existing_candidate_adds_credit_without_rival_malus():
    previous_last_seen = NOW - timedelta(days=1)
    engine, store = _engine_with_records([
        _record("Toyota", "Camry", 2.0, NOW, observation_count=3),
        _record("Honda", "Accord", 4.0, NOW, observation_count=7),
    ])
    confidence = 0.50

    engine.process_detection(Detection(
        plate_number=PLATE, plate_state=STATE, make="Toyota", model="Camry",
        confidence=confidence, timestamp=NOW,
    ))
    records = _by_model(store)

    assert records["Camry"]["score"] == pytest.approx(
        DEFAULT_CONFIG.alpha * confidence + (1 - DEFAULT_CONFIG.alpha) * 2.0
    )
    assert records["Camry"]["observationCount"] == 3
    assert records["Camry"]["lastSeen"] == previous_last_seen
    assert records["Camry"]["lastDecayedAt"] == NOW
    assert records["Accord"]["score"] == pytest.approx(4.0)
    assert records["Accord"]["observationCount"] == 7
    assert records["Accord"]["lastSeen"] == previous_last_seen
    assert records["Accord"]["lastDecayedAt"] == NOW


def test_lookup_only_refreshes_last_decayed_at_without_confirming_a_sighting():
    """Also covers decay_all()'s "skip decay for a lone, uncontested candidate" rule:
    this plate has exactly one candidate, so score does NOT decay over the 2-day gap
    (only lastDecayedAt advances) -- see scoring.py's decay_all() docstring."""
    previous_last_seen = NOW - timedelta(days=7)
    record = _record("Toyota", "Camry", 2.0, NOW - timedelta(days=2))
    record["lastSeen"] = previous_last_seen
    engine, store = _engine_with_records([record])

    engine.process_detection(Detection(
        plate_number=PLATE, plate_state=STATE, timestamp=NOW,
    ))
    record = _by_model(store)["Camry"]

    assert record["observationCount"] == 3
    assert record["lastSeen"] == previous_last_seen
    assert record["lastDecayedAt"] == NOW
    assert record["score"] == pytest.approx(2.0)
