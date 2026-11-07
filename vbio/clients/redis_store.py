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
from datetime import datetime
from typing import List, Optional

_NUMERIC_FLOAT_FIELDS = ("score", "posterior", "hmmPosterior", "credibilityScore")
_NUMERIC_INT_FIELDS = ("observationCount",)
_DATETIME_FIELDS = ("firstSeen", "lastSeen", "lastDecayedAt", "lastTouchedAt")


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

    def get_candidates(self, plate_number: str, plate_state: str) -> List[dict]:
        """Returns every candidate association record currently known for this plate+state."""
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

    def delete_candidates(self, keys: List[str]) -> None:
        """Evicts the given candidate hash keys entirely -- used by the enrichment
        engine's TTL cleanup (see vbio.enrichment.cleanup.gc_and_sync) to reclaim
        OBSERVED candidates that were never confirmed/promoted within the TTL window.
        No-op if `keys` is empty."""
        if keys:
            self.redis.delete(*keys)

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

    def get_candidates(self, plate_number: str, plate_state: str) -> List[dict]:
        prefix = f"vbio:{plate_number.upper()}:{plate_state.upper()}:"
        # Return copies so callers mutating the result don't corrupt the store by accident.
        return [dict(r) for k, r in self._records.items() if k.startswith(prefix)]

    def save_candidates(self, records: List[dict]) -> None:
        for record in records:
            self._records[record["key"]] = dict(record)

    def delete_candidates(self, keys: List[str]) -> None:
        for key in keys:
            self._records.pop(key, None)

    def get_last_update(self, plate_number: str, plate_state: str) -> Optional[datetime]:
        return self._last_update.get((plate_number.upper(), plate_state.upper()))

    def set_last_update(self, plate_number: str, plate_state: str, ts: datetime) -> None:
        self._last_update[(plate_number.upper(), plate_state.upper())] = ts
