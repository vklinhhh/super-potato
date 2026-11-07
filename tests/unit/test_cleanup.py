"""Unit tests for vbio.enrichment.cleanup (PENDING -> ACTIVE promotion rule).

Garbage collection itself is handled via native Redis TTL at the storage layer (see
clients/redis_store.py's set_ttl()/persist() and engine.py's persist step), not by any
function in cleanup.py -- there's nothing to unit-test there beyond those stores'
own bookkeeping (InMemoryAssociationStore._ttls)."""
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vbio.enrichment.cleanup import maybe_promote

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _record(**overrides):
    base = {
        "key": "vbio:ABC123:NY:honda_civic",
        "plateNumber": "ABC123",
        "plateState": "NY",
        "make": "Honda",
        "model": "Civic",
        "firstSeen": NOW,
        "lastSeen": NOW,
        "observationCount": 1,
        "score": 0.3,
        "posterior": 0.5,
        "status": "PENDING",
        "source": "OBSERVED",
    }
    base.update(overrides)
    return base


def test_maybe_promote_flips_pending_to_active_once_threshold_reached():
    record = _record(observationCount=3, status="PENDING")
    maybe_promote(record, n_activate=3)
    assert record["status"] == "ACTIVE"


def test_maybe_promote_leaves_pending_below_threshold():
    record = _record(observationCount=2, status="PENDING")
    maybe_promote(record, n_activate=3)
    assert record["status"] == "PENDING"


def test_maybe_promote_never_touches_vbio_records():
    record = _record(observationCount=99, status="PENDING", source="VBIO")
    maybe_promote(record, n_activate=3)
    assert record["status"] == "PENDING"


def test_maybe_promote_is_a_noop_once_already_active():
    record = _record(observationCount=3, status="ACTIVE")
    maybe_promote(record, n_activate=3)
    assert record["status"] == "ACTIVE"
