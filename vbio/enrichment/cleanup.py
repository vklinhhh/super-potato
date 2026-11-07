"""
cleanup.py

PENDING -> ACTIVE promotion rule for OBSERVED candidate records.

Garbage collection itself is NOT done here. An earlier version of this module ran a
manual scan-and-evict pass (gc_and_sync()) at the top of every process_detection()
tick, over that tick's own plate's `records`. That only ever reclaimed a plate's OWN
stale rivals WHEN that same plate got detected again -- a plate that stops being
detected entirely (the actual common case for "this was a one-off misread, never
reconfirmed") would never get cleaned up, since nothing ever calls process_detection()
for it again. Replaced with native Redis TTL instead (see engine.py's persist step and
clients/redis_store.py's set_ttl()/persist()): an OBSERVED+PENDING candidate's Redis
key gets its own TTL armed/refreshed every time it's the one touched by a detection,
so Redis reclaims it on its own schedule with no scan needed at all -- including for an
abandoned plate. source=VBIO and OBSERVED+ACTIVE candidates are `persist()`-ed (TTL
removed outright) since they have permanent backing (VBIo, or promotion) and must
never auto-expire. See ENGINE_LOGIC_FLOW.md section 9 for the full design history.
"""

STATUS_PENDING = "PENDING"
STATUS_ACTIVE = "ACTIVE"


def maybe_promote(record: dict, n_activate: int) -> None:
    """
    In-place. An OBSERVED candidate still PENDING flips to ACTIVE once it has
    accumulated >= n_activate real (high-confidence) confirmations
    (observationCount only increments on apply_high_confidence_update's winner
    branch -- see scoring.py).

    This does NOT change the candidate's weight in any score/posterior comparison --
    PENDING and ACTIVE candidates compete on equal footing throughout engine.py. The
    ONLY effect of ACTIVE is that engine.py's persist step stops renewing this
    candidate's Redis TTL and removes it outright -- a rare-but-real vehicle is
    trusted in full on sight 1, exactly as before; it only risks its cache entry
    expiring if it is NEVER reconfirmed again within gc_days. (A different, REJECTED
    idea from earlier discussion used PENDING/ACTIVE to dampen a candidate's weight in
    comparisons until promoted -- that reintroduces RFC-01 Phase 1's own documented
    "Low-Frequency Vehicle" starvation risk. This function only ever gates TTL
    retention, never trust.)
    """
    if (
        record.get("source") == "OBSERVED"
        and record.get("status") == STATUS_PENDING
        and record.get("observationCount", 0) >= n_activate
    ):
        record["status"] = STATUS_ACTIVE
