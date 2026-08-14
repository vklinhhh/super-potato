"""
test_enrichment_engine.py

Regression tests for the enrichment engine, checking it reproduces the tier semantics
documented in simulation_hmm.html ("Confidence tier of the returned result" comment):
    OBSERVED — clean read, engine trusted directly (conf >= Th1)
    HIGH     — Bayes clears Th3 and the winner is alive in Redis
    MEDIUM   — inferred from A*'s make: highest-scoring same-make record
    LOW      — registry only: highest-scoring record on the whole plate
    NONE     — no record alive, return Unknown

Run with: python -m pytest tests/test_enrichment_engine.py -v
(or just: python tests/test_enrichment_engine.py)
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fakeredis

from vbio.clients.redis_store import RedisAssociationStore
from vbio.enrichment.config import DEFAULT_CONFIG
from vbio.enrichment.engine import Detection, EnrichmentEngine

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


def make_engine():
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    store = RedisAssociationStore(client)
    return EnrichmentEngine(store, DEFAULT_CONFIG), store


def vbio_seed_two_candidates(plate_number, plate_state):
    """Mimics VBIoClient.lookup() returning two known vehicles for one plate+state."""
    return [
        {"Make": "HYUNDAI", "Model": "SONATA", "FirstSeen": NOW - timedelta(days=400),
         "LastSeen": NOW - timedelta(days=400), "ObservationCount": 2, "HmmPosterior": 0.9},
        {"Make": "MERCEDES-BENZ", "Model": "C CLASS", "FirstSeen": NOW - timedelta(days=3000),
         "LastSeen": NOW - timedelta(days=3000), "ObservationCount": 1, "HmmPosterior": 0.9},
    ]


def test_observed_tier_on_high_confidence_direct_read():
    """A single Th1-clearing MCAP read on a cold plate (no VBIo match) should be OBSERVED,
    trusted directly, with no Bayes fallback needed."""
    engine, _ = make_engine()
    detection = Detection(
        plate_number="DWD1915", plate_state="NY",
        make="Hyundai", model="Sonata", confidence=0.93, timestamp=NOW,
    )
    result = engine.process_detection(detection, vbio_seed_fn=None)

    assert result.tier == "OBSERVED"
    assert result.make == "Hyundai"
    assert result.model == "Sonata"
    assert result.confidence == 0.93
    assert len(result.candidates) == 1
    assert result.candidates[0]["observationCount"] == 1


def test_high_confidence_read_promotes_matching_vbio_candidate_and_penalizes_others():
    """A Th1-clearing read matching one of two VBIo-seeded candidates should boost that
    candidate's score and apply a malus to the other (non-matching) candidate."""
    engine, _ = make_engine()
    detection = Detection(
        plate_number="DWD1915", plate_state="NY",
        make="Hyundai", model="Sonata", confidence=0.93, timestamp=NOW,
    )
    result = engine.process_detection(detection, vbio_seed_fn=vbio_seed_two_candidates)

    assert result.tier == "OBSERVED"
    assert result.make == "HYUNDAI"

    by_model = {c["model"]: c for c in result.candidates}
    assert by_model["SONATA"]["score"] > DEFAULT_CONFIG.seed_base  # boosted above the seed base
    assert by_model["C CLASS"]["score"] < DEFAULT_CONFIG.seed_base * 0.9  # malus applied


def test_low_confidence_read_falls_back_to_bayes_tier():
    """A sub-Th1 read with no matching MCAP make/model at all (pure plate/state hit) should
    fall through to the Bayes branch and land on HIGH/MEDIUM/LOW depending on posterior --
    never OBSERVED, and never NONE when VBIo candidates exist."""
    engine, _ = make_engine()
    detection = Detection(
        plate_number="DWD1915", plate_state="NY",
        make=None, model=None, confidence=None, timestamp=NOW,
    )
    result = engine.process_detection(detection, vbio_seed_fn=vbio_seed_two_candidates)

    assert result.tier in ("HIGH", "MEDIUM", "LOW")
    assert result.make in ("HYUNDAI", "MERCEDES-BENZ")


def test_cold_plate_with_no_seed_and_no_detection_is_none():
    """No Redis state, no VBIo seed, and no MCAP make/model at all -> nothing to report."""
    engine, _ = make_engine()
    detection = Detection(
        plate_number="ZZZ0000", plate_state="XX",
        make=None, model=None, confidence=None, timestamp=NOW,
    )
    result = engine.process_detection(detection, vbio_seed_fn=None)

    assert result.tier == "NONE"
    assert result.make is None
    assert result.candidates == []


def test_second_detection_persists_and_builds_on_first():
    """Two detections in sequence on the same plate should accumulate state in Redis --
    the second call should see the first call's updated candidates, not a fresh seed."""
    engine, _ = make_engine()

    first = Detection(plate_number="DWD1915", plate_state="NY",
                       make="Hyundai", model="Sonata", confidence=0.93, timestamp=NOW)
    engine.process_detection(first, vbio_seed_fn=vbio_seed_two_candidates)

    second = Detection(plate_number="DWD1915", plate_state="NY",
                        make="Hyundai", model="Sonata", confidence=0.95,
                        timestamp=NOW + timedelta(hours=1))
    result = engine.process_detection(second, vbio_seed_fn=vbio_seed_two_candidates)

    assert result.tier == "OBSERVED"
    sonata = next(c for c in result.candidates if c["model"] == "SONATA")
    # Starts from the VBIo seed's ObservationCount=2, then +1 per detection (not reset
    # to 0 and not re-seeded on the second call).
    assert sonata["observationCount"] == 4
    assert len(result.candidates) == 2  # still only the two original candidates, not re-seeded


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
