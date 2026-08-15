from datetime import datetime, timezone

import fakeredis

from vbio.clients.redis_store import RedisAssociationStore
from vbio.clients.redis_vbio import RedisVBIoClient


def test_redis_vbio_client_reads_only_plate_state_candidates():
    store = RedisAssociationStore(fakeredis.FakeStrictRedis(decode_responses=True))
    now = datetime.now(timezone.utc)
    store.save_candidates([
        {
            "key": "vbio:ABC123:CA:toyota_camry",
            "plateNumber": "ABC123",
            "plateState": "CA",
            "make": "Toyota",
            "model": "Camry",
            "score": 2.0,
            "posterior": 0.5,
            "observationCount": 1,
            "firstSeen": now,
            "lastSeen": now,
            "lastDecayedAt": now,
        },
    ])

    client = RedisVBIoClient(store)

    assert client.lookup("abc123", "ca")[0]["model"] == "Camry"
    assert client.lookup("ABC123", "NV") == []
    assert client.lookup_plate_only("ABC123") == []


def test_redis_vbio_client_uses_cold_start_source_only_on_cache_miss():
    store = RedisAssociationStore(fakeredis.FakeStrictRedis(decode_responses=True))

    class ColdStartClient:
        def __init__(self):
            self.calls = []

        def lookup(self, plate, state):
            self.calls.append((plate, state))
            return [{"Make": "CHEVROLET", "Model": "MALIBU"}]

    source = ColdStartClient()
    client = RedisVBIoClient(store, cold_start_client=source)

    assert client.lookup("LVGW18", "FL") == [{"Make": "CHEVROLET", "Model": "MALIBU"}]
    assert source.calls == [("LVGW18", "FL")]
