"""
redis_store.py

Thin Redis wrapper for the Plate<->Vehicle association records the enrichment engine
reads and writes. Handles:
  - listing/reading all candidate records for a plate+state
  - writing an updated set of candidate records back
  - the plate-level "last detection" clock used for T_same's gapDays. This is tracked
    SEPARATELY from any individual candidate's lastSeen: in simulation_hmm.html,
    `lastUpdate` advances on every detection tick regardless of whether that detection
    actually touched any specific candidate (see the note in EnrichmentEngine).

Field (de)serialization matches vbio_redis_loader.py's mapping: Redis hashes only
store strings, so numeric/datetime fields are cast on the way in and out.
"""
from datetime import datetime, timedelta
from typing import List, Optional

_NUMERIC_FLOAT_FIELDS = ("score", "posterior", "hmmPosterior", "credibilityScore")
_NUMERIC_INT_FIELDS = ("observationCount",)
_DATETIME_FIELDS = ("firstSeen", "lastSeen", "lastDecayedAt")


def _serialize(record: dict) -> dict:
    out = {}
    for k, v in record.items():
        if v is None:
            out[k] = ""
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _deserialize(raw: dict) -> dict:
    record = dict(raw)
    for f in _NUMERIC_FLOAT_FIELDS:
        if f in record and record[f] != "":
            record[f] = float(record[f])
    for f in _NUMERIC_INT_FIELDS:
        if f in record and record[f] != "":
            record[f] = int(float(record[f]))
    for f in _DATETIME_FIELDS:
        if f in record and record[f]:
            record[f] = datetime.fromisoformat(record[f])
    return record


class RedisAssociationStore:
    """Reads/writes the Plate<->Vehicle association hashes used by the enrichment engine."""

    def __init__(self, redis_client):
        """:param redis_client: a redis-py-compatible client, created with decode_responses=True."""
        self.redis = redis_client

    def get_candidates(self, plate_number: str, plate_state: str, now: Optional[datetime] = None) -> List[dict]:
        """Returns every candidate association record currently known for this
        plate+state. `now` is accepted for interface parity with
        InMemoryAssociationStore (see its own get_candidates()) but unused here --
        real Redis has already excluded any TTL-expired key from SCAN on its own."""
        pattern = f"vbio:{plate_number.upper()}:{plate_state.upper()}:*"
        records = []
        for key in self.redis.scan_iter(match=pattern):
            raw = self.redis.hgetall(key)
            if raw:
                records.append(_deserialize(raw))
        return records

    def save_candidates(self, records: List[dict]) -> None:
        """Writes each record back to its own hash key (record['key'] must already be set)."""
        for record in records:
            self.redis.hset(record["key"], mapping=_serialize(record))

    def set_ttl(self, key: str, now: datetime, seconds: float) -> None:
        """Arms/refreshes a native Redis TTL on this candidate's hash key -- used by
        the enrichment engine (see vbio.enrichment.cleanup's module docstring) for an
        OBSERVED candidate still PENDING. Redis reclaims the key on its own schedule if
        it's never touched again, with no scan/cleanup job needed -- including for a
        plate that stops being detected entirely, which a manual per-tick scan over
        only that plate's own records could never reach. `now` is accepted for
        interface parity with InMemoryAssociationStore but unused here -- real Redis
        tracks its own wall clock."""
        self.redis.expire(key, int(seconds))

    def persist(self, key: str) -> None:
        """Removes any TTL on this candidate's hash key -- called once a candidate is
        VBIo-backed or promoted to ACTIVE (see cleanup.maybe_promote()), both of which
        must never auto-expire. No-op if the key already has no TTL."""
        self.redis.persist(key)

    def _meta_key(self, plate_number: str, plate_state: str) -> str:
        return f"vbio:meta:{plate_number.upper()}:{plate_state.upper()}:lastUpdate"

    def get_last_update(self, plate_number: str, plate_state: str) -> Optional[datetime]:
        raw = self.redis.get(self._meta_key(plate_number, plate_state))
        return datetime.fromisoformat(raw) if raw else None

    def set_last_update(self, plate_number: str, plate_state: str, ts: datetime) -> None:
        self.redis.set(self._meta_key(plate_number, plate_state), ts.isoformat())


class InMemoryAssociationStore:
    """
    Same interface as RedisAssociationStore, backed by a plain in-process dict instead
    of a real Redis connection. Nothing here outlives the Python process -- useful for
    previewing what the enrichment engine WOULD do (tiers, scores) without requiring a
    live Redis instance or touching any persistent store at all.
    """

    def __init__(self):
        self._records: dict = {}       # key -> record dict
        self._last_update: dict = {}   # (plate, state) -> datetime
        self._expires_at: dict = {}    # key -> absolute datetime this key "expires" at

    def get_candidates(self, plate_number: str, plate_state: str, now: Optional[datetime] = None) -> List[dict]:
        """`now` is optional (defaults to never-expired, for any caller that doesn't
        care about TTL simulation) -- when given, a key whose armed expiry
        (see set_ttl()) is at or before `now` is excluded, simulating a real Redis
        instance having already reclaimed it. Return copies so callers mutating the
        result don't corrupt the store by accident."""
        prefix = f"vbio:{plate_number.upper()}:{plate_state.upper()}:"
        return [
            dict(r) for k, r in self._records.items()
            if k.startswith(prefix) and not (now is not None and k in self._expires_at and self._expires_at[k] <= now)
        ]

    def save_candidates(self, records: List[dict]) -> None:
        for record in records:
            self._records[record["key"]] = dict(record)

    def set_ttl(self, key: str, now: datetime, seconds: float) -> None:
        """Simulates a real Redis EXPIRE: records an absolute expiry (now + seconds)
        rather than a wall-clock-relative countdown, since test_data timestamps are
        historical and replayed under a caller-supplied `now`, not real time."""
        self._expires_at[key] = now + timedelta(seconds=seconds)

    def persist(self, key: str) -> None:
        self._expires_at.pop(key, None)

    def get_last_update(self, plate_number: str, plate_state: str) -> Optional[datetime]:
        return self._last_update.get((plate_number.upper(), plate_state.upper()))

    def set_last_update(self, plate_number: str, plate_state: str, ts: datetime) -> None:
        self._last_update[(plate_number.upper(), plate_state.upper())] = ts
