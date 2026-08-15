"""Regression test for the XA_7231 Redis-score timeline shown in the scoring slides."""
from datetime import datetime, timedelta, timezone
import math

import fakeredis
import pytest

from vbio.clients.redis_store import RedisAssociationStore
from vbio.enrichment.config import DEFAULT_CONFIG
from vbio.enrichment.engine import Detection, EnrichmentEngine


DAY_0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
PLATE = "XA_7231"
STATE = "GA"


def _seed_crv(_plate_number, _plate_state):
    # seed_base (2.0) * HmmPosterior (0.9) gives the slide's 1.80 opening score.
    return [{"Make": "Honda", "Model": "CR-V", "HmmPosterior": 0.9, "ObservationCount": 0}]


def _records_by_model(store):
    return {record["model"]: record for record in store.get_candidates(PLATE, STATE)}


def _process(engine, day, make, model, confidence):
    return engine.process_detection(Detection(
        plate_number=PLATE,
        plate_state=STATE,
        make=make,
        model=model,
        confidence=confidence,
        timestamp=DAY_0 + timedelta(days=day),
    ), vbio_seed_fn=_seed_crv)


def test_xa_7231_slide_score_timeline():
    """Reproduce the slide equations: decay, low credit, high admission, and high malus."""
    store = RedisAssociationStore(fakeredis.FakeStrictRedis(decode_responses=True))
    engine = EnrichmentEngine(store, DEFAULT_CONFIG)
    decay = lambda days: math.exp(-DEFAULT_CONFIG.lambda_base * days)

    # Day 0: a weak read for the existing CR-V receives k2 * C = 0.04.
    _process(engine, 0, "Honda", "CR-V", 0.40)
    records = _records_by_model(store)
    crv = 1.80 + DEFAULT_CONFIG.malus_low_rate * 0.40
    assert records["CR-V"]["score"] == pytest.approx(crv)
    assert records["CR-V"]["observationCount"] == 0
    assert round(crv, 2) == 1.84

    # Day 10: an unseen low-confidence Pilot is not admitted.
    _process(engine, 10, "Honda", "Pilot", 0.40)
    records = _records_by_model(store)
    crv *= decay(10)
    assert records.keys() == {"CR-V"}
    assert records["CR-V"]["score"] == pytest.approx(crv)
    assert round(crv, 2) == 1.77

    # Day 25: a high-confidence Pilot is admitted at zero, gains +0.93, and
    # applies the 0.10 * 0.93 * CR-V score malus.
    _process(engine, 25, "Honda", "Pilot", 0.93)
    records = _records_by_model(store)
    crv *= decay(15)
    crv *= 1 - DEFAULT_CONFIG.malus_rate * 0.93
    pilot = 0.93
    assert records["CR-V"]["score"] == pytest.approx(crv)
    assert records["Pilot"]["score"] == pytest.approx(pilot)
    assert records["Pilot"]["observationCount"] == 1
    assert round(crv, 2) == 1.52
    assert round(pilot, 2) == 0.93

    # Day 40: another high Pilot read uses direct +C, while the CR-V pays malus.
    _process(engine, 40, "Honda", "Pilot", 0.91)
    records = _records_by_model(store)
    crv *= decay(15)
    pilot *= decay(15)
    crv *= 1 - DEFAULT_CONFIG.malus_rate * 0.91
    pilot += 0.91
    assert records["CR-V"]["score"] == pytest.approx(crv)
    assert records["Pilot"]["score"] == pytest.approx(pilot)
    assert round(crv, 2) == 1.30
    assert round(pilot, 2) == 1.79

    # Day 70: a weak existing CR-V read gets +0.10 * 0.55 only, no Pilot malus.
    _process(engine, 70, "Honda", "CR-V", 0.55)
    records = _records_by_model(store)
    crv = crv * decay(30) + DEFAULT_CONFIG.malus_low_rate * 0.55
    pilot *= decay(30)
    assert records["CR-V"]["score"] == pytest.approx(crv)
    assert records["Pilot"]["score"] == pytest.approx(pilot)
    assert round(crv, 2) == 1.21
    assert round(pilot, 2) == 1.59

    # Day 100: direct high Pilot score plus proportional CR-V malus.
    _process(engine, 100, "Honda", "Pilot", 0.96)
    records = _records_by_model(store)
    crv *= decay(30)
    pilot *= decay(30)
    crv *= 1 - DEFAULT_CONFIG.malus_rate * 0.96
    pilot += 0.96
    assert records["CR-V"]["score"] == pytest.approx(crv)
    assert records["Pilot"]["score"] == pytest.approx(pilot)
    assert round(crv, 2) == 0.98
    assert round(pilot, 2) == 2.38
