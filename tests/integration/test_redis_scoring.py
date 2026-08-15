"""Redis-backed scoring tests for the decay/high/low branches in redis_scoring_system.png."""
import math
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

from vbio.clients.redis_store import RedisAssociationStore
from vbio.enrichment.config import DEFAULT_CONFIG
from vbio.enrichment.engine import Detection, EnrichmentEngine
from vbio.enrichment.keys import build_redis_key

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

    assert records["Camry"]["score"] == pytest.approx(decayed_winner + confidence)
    assert records["Camry"]["observationCount"] == 4
    assert records["Camry"]["lastSeen"] == NOW
    assert records["Accord"]["score"] == pytest.approx(
        decayed_rival * (1 - DEFAULT_CONFIG.malus_rate * confidence)
    )
    assert records["Accord"]["observationCount"] == 7


def test_low_confidence_existing_candidate_adds_credit_without_rival_malus():
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

    assert records["Camry"]["score"] == pytest.approx(2.0 + DEFAULT_CONFIG.malus_low_rate * confidence)
    assert records["Camry"]["observationCount"] == 3
    assert records["Camry"]["lastSeen"] == NOW
    assert records["Accord"]["score"] == pytest.approx(4.0)
    assert records["Accord"]["observationCount"] == 7
