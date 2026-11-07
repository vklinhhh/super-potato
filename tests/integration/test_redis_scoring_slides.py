"""
Regression test for the XA_7231 Redis-score timeline shown in the scoring slides.

NOTE: the expected values below use the EMA score-update formula (see scoring.py and
config.py's EnrichmentConfig.alpha/seed_base), NOT the original slide deck's literal
`score += confidence` arithmetic -- that additive formula has no ceiling and was
replaced after an accuracy sweep (scripts/experiment_score_normalization.py) showed EMA
matches or beats it while keeping `score` naturally bounded. The scenario (same days,
same make/model/confidence sequence) is unchanged; only the formula computing the
expected numbers is. See ENGINE_LOGIC_FLOW.md section 5.3/8.

ALSO NOTE: Day 10's tick has only ONE live candidate (CR-V -- Pilot's low-confidence
read that day doesn't admit it), so decay_all() skips decay entirely for that tick (see
scoring.py's decay_all() docstring / ENGINE_LOGIC_FLOW.md section 5.3/8) -- CR-V's score
carries over UNCHANGED from Day 0 into Day 25, instead of decaying over those 10 days.
Day 25 is the first tick with 2 live candidates (Pilot gets admitted that same tick,
before decay_all() runs), so decay resumes normally from Day 25 onward.
"""
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
    # seed_base (0.7) * HmmPosterior (0.9) gives the opening score of 0.63.
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
    """Reproduce the slide scenario's event sequence (decay, low credit, high admission,
    high malus) with the EMA score formula -- see module docstring."""
    store = RedisAssociationStore(fakeredis.FakeStrictRedis(decode_responses=True))
    engine = EnrichmentEngine(store, DEFAULT_CONFIG)
    decay = lambda days: math.exp(-DEFAULT_CONFIG.lambda_base * days)
    alpha = DEFAULT_CONFIG.alpha

    # Day 0: a weak read for the existing CR-V pulls its EMA toward 0.40.
    _process(engine, 0, "Honda", "CR-V", 0.40)
    records = _records_by_model(store)
    crv = alpha * 0.40 + (1 - alpha) * 0.63
    assert records["CR-V"]["score"] == pytest.approx(crv)
    assert records["CR-V"]["observationCount"] == 0
    assert round(crv, 2) == 0.56

    # Day 10: an unseen low-confidence Pilot is not admitted -- CR-V is still the
    # plate's ONLY live candidate, so decay_all() skips decay entirely this tick
    # (nothing competes with it yet); crv carries over unchanged from Day 0.
    _process(engine, 10, "Honda", "Pilot", 0.40)
    records = _records_by_model(store)
    assert records.keys() == {"CR-V"}
    assert records["CR-V"]["score"] == pytest.approx(crv)
    assert round(crv, 2) == 0.56

    # Day 25: a high-confidence Pilot is admitted at zero (now 2 live candidates,
    # so decay resumes -- the gap is Day25 - Day10 = 15 days, since Day 10 only
    # refreshed lastDecayedAt without decaying), EMA's toward 0.93, and applies the
    # 0.10 * 0.93 * CR-V score malus.
    _process(engine, 25, "Honda", "Pilot", 0.93)
    records = _records_by_model(store)
    crv *= decay(15)
    crv *= 1 - DEFAULT_CONFIG.malus_rate * 0.93
    pilot = alpha * 0.93 + (1 - alpha) * 0.0
    assert records["CR-V"]["score"] == pytest.approx(crv)
    assert records["Pilot"]["score"] == pytest.approx(pilot)
    assert records["Pilot"]["observationCount"] == 1
    assert round(crv, 2) == 0.48
    assert round(pilot, 2) == 0.28

    # Day 40: another high Pilot read EMA's toward 0.91, while the CR-V pays malus.
    _process(engine, 40, "Honda", "Pilot", 0.91)
    records = _records_by_model(store)
    crv *= decay(15)
    pilot *= decay(15)
    crv *= 1 - DEFAULT_CONFIG.malus_rate * 0.91
    pilot = alpha * 0.91 + (1 - alpha) * pilot
    assert records["CR-V"]["score"] == pytest.approx(crv)
    assert records["Pilot"]["score"] == pytest.approx(pilot)
    assert round(crv, 2) == 0.41
    assert round(pilot, 2) == 0.46

    # Day 70: a weak existing CR-V read EMA's toward 0.55 only, no Pilot malus.
    _process(engine, 70, "Honda", "CR-V", 0.55)
    records = _records_by_model(store)
    crv = alpha * 0.55 + (1 - alpha) * (crv * decay(30))
    pilot *= decay(30)
    assert records["CR-V"]["score"] == pytest.approx(crv)
    assert records["Pilot"]["score"] == pytest.approx(pilot)
    assert round(crv, 2) == 0.42
    assert round(pilot, 2) == 0.41

    # Day 100: high Pilot EMA update plus proportional CR-V malus.
    _process(engine, 100, "Honda", "Pilot", 0.96)
    records = _records_by_model(store)
    crv *= decay(30)
    pilot *= decay(30)
    crv *= 1 - DEFAULT_CONFIG.malus_rate * 0.96
    pilot = alpha * 0.96 + (1 - alpha) * pilot
    assert records["CR-V"]["score"] == pytest.approx(crv)
    assert records["Pilot"]["score"] == pytest.approx(pilot)
    assert round(crv, 2) == 0.34
    assert round(pilot, 2) == 0.54
