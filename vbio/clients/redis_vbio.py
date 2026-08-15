"""VBIo lookup adapter backed exclusively by Redis association records."""


class RedisVBIoClient:
    """Reads Redis first and delegates cache misses to a VBIo source client."""

    def __init__(self, store, cold_start_client=None):
        self.store = store
        self.cold_start_client = cold_start_client

    def lookup(self, plate: str, plate_state: str):
        records = self.store.get_candidates(plate, plate_state)
        if records or self.cold_start_client is None:
            return records
        return self.cold_start_client.lookup(plate, plate_state)

    def lookup_plate_only(self, plate: str):
        if self.cold_start_client is None:
            # Redis keys are state-scoped, so a state-less lookup cannot safely scan all
            # candidate records without a dedicated secondary index.
            return []
        return self.cold_start_client.lookup_plate_only(plate)
