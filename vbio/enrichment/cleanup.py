"""
cleanup.py

Redis-cache TTL + PENDING -> ACTIVE promotion lifecycle for OBSERVED candidate
records. Kept in its own module, separate from the scoring/posterior pipeline
(engine.py/posterior.py/scoring.py) -- the engine has 2 genuinely distinct jobs
("enrich / tinh diem": decide what a plate's vehicle is right now; "clean up / don
rac": decide what's still worth keeping in Redis) and this module owns only the 2nd.

Design (validated against the 97-profile test_data accuracy harness -- see
scripts/tune_gc_params.py to re-sweep gc_days/n_activate, and
ENGINE_LOGIC_FLOW.md's "Redis TTL + PENDING -> ACTIVE lifecycle" section for the full
writeup and sweep results):

  - source=VBIO (DMV-backed) records are NEVER touched here, regardless of status or
    staleness. VBIo is already the permanent store; evicting a dormant VBIo-sourced
    candidate from the cache loses nothing (a later cold start re-seeds it identically
    from vbio.parquet), so there's no reason to spend any cycles on it. (An earlier
    version of this mistakenly applied the ACTIVE-sync rule to VBIo rows too, because
    real VBIo seed data already carries status=ACTIVE -- that wrongly evicted hundreds
    of live VBIo candidates and cost ~0.8pp accuracy; fixed by gating the whole
    function on source == "OBSERVED" first.)
  - source=OBSERVED + status=ACTIVE + stale (now - lastTouchedAt > gc_days): this
    candidate has no permanent backing store of its own (it was never a VBIo seed), so
    before it can be evicted its facts must be handed off somewhere permanent --
    returned via `synced_rows` for the caller to persist (e.g. append to a VBIo sync
    job/new parquet row), never silently dropped.
  - source=OBSERVED + status=PENDING + stale + NOT solo (>=2 live records for this
    plate): evicted outright. Never promoted, never reconfirmed within the window --
    this IS the garbage the TTL exists to reclaim.
  - source=OBSERVED + status=PENDING + stale + solo: kept. Mirrors decay_all()'s own
    decay-skip-when-solo exemption -- a lone candidate has no rival to have lost
    ground to, so there's nothing for staleness to be evidence AGAINST; discarding it
    would just throw away the only information this plate has for no accuracy benefit.
"""
from datetime import datetime
from typing import List, Tuple

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
    ONLY effect of ACTIVE is immunity from gc_and_sync() below. That's what makes this
    safe: a rare-but-real vehicle is trusted in full on sight 1, exactly as before --
    it only risks being swept by the TTL if it is NEVER reconfirmed again within
    gc_days. (A different, REJECTED idea from earlier discussion used PENDING/ACTIVE to
    dampen a candidate's weight in comparisons until promoted -- that reintroduces
    RFC-01 Phase 1's own documented "Low-Frequency Vehicle" starvation risk. This
    function only ever gates cache retention, never trust.)
    """
    if (
        record.get("source") == "OBSERVED"
        and record.get("status") == STATUS_PENDING
        and record.get("observationCount", 0) >= n_activate
    ):
        record["status"] = STATUS_ACTIVE


def gc_and_sync(records: List[dict], now: datetime, gc_days: float) -> Tuple[List[dict], List[dict]]:
    """
    Pure function, no store access -- the caller (engine.process_detection) deletes
    the evicted keys from Redis and hands `synced_rows` to a VBIo sync callback.

    :return: (survivors, synced_rows) -- survivors is the subset of `records` that
             should remain in the live candidate set for this tick; synced_rows is a
             list of VBIo-row-shaped dicts (PlateNumber, PlateState, Make, Model,
             FirstSeen, LastSeen, ObservationCount, HmmPosterior, Status) for every
             ACTIVE candidate evicted this call.
    """
    solo = len(records) < 2
    survivors: List[dict] = []
    synced_rows: List[dict] = []
    for r in records:
        if r.get("source") != "OBSERVED":
            survivors.append(r)
            continue

        touched_at = r.get("lastTouchedAt") or r.get("firstSeen") or now
        stale = (now - touched_at).total_seconds() / 86400.0 > gc_days
        if not stale:
            survivors.append(r)
            continue

        if r.get("status") == STATUS_ACTIVE:
            synced_rows.append({
                "PlateNumber": r["plateNumber"],
                "PlateState": r["plateState"],
                "Make": r["make"],
                "Model": r["model"],
                "FirstSeen": r.get("firstSeen"),
                "LastSeen": r.get("lastSeen"),
                "ObservationCount": r.get("observationCount", 0),
                "HmmPosterior": r.get("posterior"),
                "Status": STATUS_ACTIVE,
            })
        elif r.get("status") == STATUS_PENDING and not solo:
            pass  # evicted, nothing worth syncing
        else:
            survivors.append(r)

    return survivors, synced_rows
