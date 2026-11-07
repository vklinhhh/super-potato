"""Unit tests for vbio.enrichment.cleanup (Redis TTL + PENDING -> ACTIVE lifecycle)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vbio.enrichment.cleanup import gc_and_sync, maybe_promote

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
GC_DAYS = 180.0


def _record(**overrides):
    base = {
        "key": "vbio:ABC123:NY:honda_civic",
        "plateNumber": "ABC123",
        "plateState": "NY",
        "make": "Honda",
        "model": "Civic",
        "firstSeen": NOW - timedelta(days=200),
        "lastSeen": NOW - timedelta(days=200),
        "lastTouchedAt": NOW - timedelta(days=200),
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


def test_gc_never_touches_vbio_records_even_if_stale_and_active():
    vbio_rival = _record(key="k1", status="ACTIVE", source="VBIO",
                          lastTouchedAt=NOW - timedelta(days=400))
    other = _record(key="k2", status="PENDING", lastTouchedAt=NOW)
    survivors, synced = gc_and_sync([vbio_rival, other], NOW, GC_DAYS)
    assert vbio_rival in survivors
    assert synced == []


def test_gc_evicts_stale_pending_observed_candidate_when_not_solo():
    rival = _record(key="k1", status="PENDING", lastTouchedAt=NOW)
    stale_junk = _record(key="k2", status="PENDING", lastTouchedAt=NOW - timedelta(days=200))
    survivors, synced = gc_and_sync([rival, stale_junk], NOW, GC_DAYS)
    assert [r["key"] for r in survivors] == ["k1"]
    assert synced == []


def test_gc_keeps_stale_pending_observed_candidate_when_solo():
    """Mirrors decay_all()'s decay-skip-when-solo exemption: a lone candidate has no
    rival to have lost ground to, so staleness alone shouldn't discard it."""
    solo_stale = _record(key="k1", status="PENDING", lastTouchedAt=NOW - timedelta(days=200))
    survivors, synced = gc_and_sync([solo_stale], NOW, GC_DAYS)
    assert survivors == [solo_stale]
    assert synced == []


def test_gc_syncs_then_evicts_stale_active_observed_candidate():
    rival = _record(key="k1", status="PENDING", lastTouchedAt=NOW)
    stale_active = _record(key="k2", status="ACTIVE", lastTouchedAt=NOW - timedelta(days=200),
                            observationCount=5)
    survivors, synced = gc_and_sync([rival, stale_active], NOW, GC_DAYS)
    assert [r["key"] for r in survivors] == ["k1"]
    assert len(synced) == 1
    assert synced[0]["Make"] == "Honda" and synced[0]["Model"] == "Civic"
    assert synced[0]["Status"] == "ACTIVE"
    assert synced[0]["ObservationCount"] == 5


def test_gc_syncs_solo_stale_active_candidate_too():
    """Unlike the PENDING solo exemption, an ACTIVE candidate has already earned
    permanence -- being solo doesn't change that it has no backing store yet, so it
    still needs to be synced (just not discarded) once stale."""
    solo_active = _record(key="k1", status="ACTIVE", lastTouchedAt=NOW - timedelta(days=200))
    survivors, synced = gc_and_sync([solo_active], NOW, GC_DAYS)
    assert survivors == []
    assert len(synced) == 1


def test_gc_keeps_fresh_candidates_regardless_of_status():
    fresh_pending = _record(key="k1", status="PENDING", lastTouchedAt=NOW)
    fresh_active = _record(key="k2", status="ACTIVE", lastTouchedAt=NOW)
    survivors, synced = gc_and_sync([fresh_pending, fresh_active], NOW, GC_DAYS)
    assert len(survivors) == 2
    assert synced == []
