"""Unit tests for InMemoryAssociationStore's TTL simulation (set_ttl/persist/get_candidates).

This is the test double's stand-in for Redis's own native EXPIRE/PERSIST -- see
vbio/clients/redis_store.py's module-level notes and vbio/enrichment/cleanup.py for why
the enrichment engine relies on real Redis TTL (not a manual scan-and-evict pass) to
reclaim stale OBSERVED candidates, including for a plate that stops being detected
entirely.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vbio.clients.redis_store import InMemoryAssociationStore

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
PLATE, STATE = "ABC123", "NY"


def _record(key):
    return {"key": key, "plateNumber": PLATE, "plateState": STATE, "make": "Honda", "model": "Civic"}


def test_get_candidates_without_now_ignores_ttl():
    store = InMemoryAssociationStore()
    key = f"vbio:{PLATE}:{STATE}:honda_civic"
    store.save_candidates([_record(key)])
    store.set_ttl(key, NOW, seconds=10)

    records = store.get_candidates(PLATE, STATE)  # no `now` -> never-expired
    assert [r["key"] for r in records] == [key]


def test_get_candidates_excludes_key_past_its_armed_expiry():
    store = InMemoryAssociationStore()
    key = f"vbio:{PLATE}:{STATE}:honda_civic"
    store.save_candidates([_record(key)])
    store.set_ttl(key, NOW, seconds=10)

    still_alive = store.get_candidates(PLATE, STATE, now=NOW + timedelta(seconds=5))
    assert [r["key"] for r in still_alive] == [key]

    expired = store.get_candidates(PLATE, STATE, now=NOW + timedelta(seconds=11))
    assert expired == []


def test_persist_removes_ttl_so_key_never_expires():
    store = InMemoryAssociationStore()
    key = f"vbio:{PLATE}:{STATE}:honda_civic"
    store.save_candidates([_record(key)])
    store.set_ttl(key, NOW, seconds=10)
    store.persist(key)

    records = store.get_candidates(PLATE, STATE, now=NOW + timedelta(days=365))
    assert [r["key"] for r in records] == [key]


def test_set_ttl_refreshes_an_existing_expiry():
    store = InMemoryAssociationStore()
    key = f"vbio:{PLATE}:{STATE}:honda_civic"
    store.save_candidates([_record(key)])
    store.set_ttl(key, NOW, seconds=10)
    store.set_ttl(key, NOW + timedelta(seconds=8), seconds=10)  # touched again before expiry

    records = store.get_candidates(PLATE, STATE, now=NOW + timedelta(seconds=15))
    assert [r["key"] for r in records] == [key]
