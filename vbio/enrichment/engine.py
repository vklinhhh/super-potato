"""
engine.py

EnrichmentEngine: the single-detection version of simulation_hmm.html's
computeHMMSequence() loop body -- i.e. one "tick" of the TRUE INCREMENTAL ENGINE
described there. Given one MCAP detection for a plate, it:
  1. Loads the plate's current Redis association records (or admits them fresh from
     VBIo/VBIo if Redis has nothing for this plate yet -- cold start).
  2. Decays every candidate in place, using its own elapsed time since last decayed.
  3. Runs the Bayes/HMM posterior update across all live candidates.
  4. Applies the observation itself (High-confidence direct update, or Low-confidence
     partial credit).
  5. Decides the final tier (OBSERVED/HIGH/MEDIUM/LOW/NONE) and packs the result.
  6. Writes the updated candidate records back to Redis.

This mirrors the VRDB Plan doc's "Look Up (Enrichment Logic)" flow: MMC detected ->
Update; MMC not detected -> Look Up -> MMC Enriched -> Return.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from .aliases import MakeModelAliases
from .cleanup import gc_and_sync, maybe_promote
from .config import DEFAULT_CONFIG, EnrichmentConfig
from .keys import build_redis_key, normalize_model_key
from .posterior import (
    compute_likelihoods,
    compute_pi_score,
    compute_posteriors,
    compute_t_same_dynamic,
    compute_transition_prior,
)
from .scoring import apply_high_confidence_update, apply_low_confidence_update, decay_all
from .tiering import TierResult, decide_tier

# Matches vbio_schema.STATUS_PENDING's value -- kept as a local constant rather than
# importing across the schema/enrichment module boundary for one string.
STATUS_PENDING = "PENDING"


@dataclass
class Detection:
    """One MCAP detection for a plate, as input to the engine."""
    plate_number: str
    plate_state: str
    make: Optional[str] = None          # MCAP's own top-1 make, or None if no MMC detected
    model: Optional[str] = None         # MCAP's own top-1 model, or None if no MMC detected
    confidence: Optional[float] = None  # MCAP's own top-1 confidence, or None
    timestamp: Optional[datetime] = None
    top_k_confidences: Dict[str, float] = field(default_factory=dict)
    # Optional per-candidate confidences from a top-5 (or top-k) MCAP prediction list,
    # keyed by the BARE "make_model" string (see enrichment.keys.normalize_model_key) --
    # not the plate-scoped Redis key. The engine translates these internally. Candidates
    # with no entry here just get the epsilon floor.


@dataclass
class EnrichmentResult:
    plate_number: str
    plate_state: str
    tier: str                      # OBSERVED | HIGH | MEDIUM | LOW | NONE
    make: Optional[str]
    model: Optional[str]
    confidence: Optional[float]    # MCAP confidence (OBSERVED) or Bayes posterior (other tiers)
    candidates: List[dict]         # full updated candidate records, for persistence/inspection


# A vbio_seed_fn takes (plate_number, plate_state) and returns a list of VBIo-shaped
# dicts (Make, Model, FirstSeen, LastSeen, ObservationCount, HmmPosterior, ...) -- see
# vbio.clients.query_vbio.VBIoClient.lookup for a ready-made implementation.
VBIoSeedFn = Callable[[str, str], List[dict]]

# A vbio_sync_fn is handed the list of VBIo-shaped dicts cleanup.gc_and_sync() produced
# this tick (see cleanup.py) -- i.e. every OBSERVED+ACTIVE candidate that is about to
# be evicted from the Redis cache for staleness and therefore needs its facts written
# somewhere permanent first. Expected to append/upsert these into VBIo (NEVER into the
# same read path vbio_seed_fn queries from mid-run -- in production this is a real
# write to the permanent store; in tests, e.g. a brand-new parquet file, never the
# original vbio.parquet). Optional: if omitted, synced rows are simply discarded along
# with the cache entry (acceptable for dry runs/previews, not for production use).
VBIoSyncFn = Callable[[List[dict]], None]


class EnrichmentEngine:
    def __init__(
        self,
        redis_store,
        config: EnrichmentConfig = DEFAULT_CONFIG,
        aliases: Optional[MakeModelAliases] = None,
    ):
        """
        :param redis_store: a RedisAssociationStore (see clients/redis_store.py) --
                             handles reading/writing the plate's candidate records.
        :param aliases: optional MakeModelAliases (see vbio.clients.make_model_mapping
                         for the loader) -- resolves every make/model this engine sees
                         (both VBIo seed rows and MCAP detections) to its canonical
                         spelling BEFORE building a candidate key, so e.g. a VBIo seed
                         spelled "GR Supra" and a MCAP detection spelled "Supra" land on
                         the SAME candidate instead of splitting evidence across two.
                         If omitted, make/model pass through unchanged (old behavior).
        """
        self.store = redis_store
        self.config = config
        self.aliases = aliases

    def _canonicalize(self, make: Optional[str], model: Optional[str]):
        if self.aliases is None:
            return make, model
        return self.aliases.canonicalize(make, model)

    def process_detection(self, detection: Detection, vbio_seed_fn: Optional[VBIoSeedFn] = None,
                           vbio_sync_fn: Optional[VBIoSyncFn] = None,
                           dry_run: bool = False) -> EnrichmentResult:
        """
        :param dry_run: if True, computes and returns the full tiered result WITHOUT
                         writing anything back to the store (no save_candidates, no
                         set_last_update, no cache eviction) -- useful for previewing
                         what the engine WOULD do before committing to a live Redis
                         instance.
        :param vbio_sync_fn: see VBIoSyncFn above -- receives any OBSERVED+ACTIVE rows
                              evicted by this tick's TTL cleanup, for persisting
                              somewhere permanent before they're gone from the cache.
        """
        cfg = self.config
        now = detection.timestamp or datetime.now(timezone.utc)

        # Canonicalize once, up front -- every subsequent use of "the detection's
        # make/model" in this method uses det_make/det_model, never
        # detection.make/detection.model directly, so seed rows (VBIo spelling) and
        # this detection (MCAP spelling) are compared/keyed in the same vocabulary.
        det_make, det_model = self._canonicalize(detection.make, detection.model)

        # gapDays uses the PREVIOUS plate-level detection time, read before anything else
        # is touched -- matches computeHMMSequence() reading `lastUpdate` at the very top
        # of each loop iteration, before decay/admission/posterior for that tick.
        last_update = self.store.get_last_update(detection.plate_number, detection.plate_state) or now
        gap_days = max(0.0, (now - last_update).total_seconds() / 86400.0)

        records = self.store.get_candidates(detection.plate_number, detection.plate_state)

        # --- Redis TTL + PENDING -> ACTIVE lifecycle cleanup (cleanup.py) -- run
        # lazily, right before this tick needs `records`, equivalent to a periodic
        # sync/cleanup job that happens to always run right before the cache is read.
        # Never touches source=VBIO records (see cleanup.gc_and_sync()'s docstring).
        if records:
            keys_before_gc = {r["key"] for r in records}
            records, synced_rows = gc_and_sync(records, now, cfg.gc_days)
            if synced_rows and vbio_sync_fn is not None:
                vbio_sync_fn(synced_rows)
            if not dry_run:
                evicted_keys = keys_before_gc - {r["key"] for r in records}
                if evicted_keys:
                    self.store.delete_candidates(list(evicted_keys))

        # Cold start: nothing in Redis yet for this plate -- admit VBIo/VBIo seed rows.
        if not records and vbio_seed_fn is not None:
            for seed in vbio_seed_fn(detection.plate_number, detection.plate_state):
                seed_make, seed_model = self._canonicalize(seed["Make"], seed["Model"])
                key = build_redis_key(detection.plate_number, detection.plate_state, seed_make, seed_model)
                records.append({
                    "key": key,
                    "plateNumber": detection.plate_number,
                    "plateState": detection.plate_state,
                    "make": seed_make,
                    "model": seed_model,
                    "firstSeen": seed.get("FirstSeen") or now,
                    "lastSeen": seed.get("LastSeen") or now,
                    "lastDecayedAt": now,
                    "lastTouchedAt": seed.get("LastSeen") or now,  # unused for source=VBIO (never GC'd), kept for field consistency
                    "observationCount": seed.get("ObservationCount", 0),
                    "score": cfg.seed_base * (seed.get("HmmPosterior") or 1.0),
                    "posterior": cfg.epsilon,
                    "hmmPosterior": seed.get("HmmPosterior"),  # kept for traceability/debugging only
                    "status": seed.get("Status"),              # kept for traceability/debugging only
                    "source": "VBIO",
                })

        top_key = None
        # A plate with no VBIo (DMV) backing at all has nothing to fall back on while
        # waiting for a read to clear the full Th1 -- use the lower, no-DMV-backing
        # threshold instead. Free to check: `records` (seed candidates included) is
        # already loaded this tick, no extra storage/Redis round-trip.
        has_vbio_candidate = any(r.get("source") == "VBIO" for r in records)
        effective_th1 = cfg.th1 if has_vbio_candidate else cfg.th1_no_vbio
        is_mmc_detected = detection.confidence is not None and detection.confidence >= effective_th1
        if det_make and det_model:
            # NOTE: this must be the same full, plate-scoped key format used for every
            # record's "key" field (build_redis_key), NOT the bare normalize_model_key --
            # otherwise it can never match an existing candidate's "key".
            canonical_top_key = build_redis_key(
                detection.plate_number, detection.plate_state, det_make, det_model
            )
            # Existing Redis hashes may have been written using an older key
            # normalizer. Match their record fields canonically before admitting a
            # duplicate candidate under the new canonical key.
            top_key = next(
                (
                    record["key"] for record in records
                    if normalize_model_key(record["make"], record["model"])
                    == normalize_model_key(det_make, det_model)
                ),
                canonical_top_key,
            )

        # "Late-arriving VBIo" rescue: if this detection's (make, model) isn't a
        # candidate yet, check whether VBIo has since documented this exact
        # association for the plate -- even if it wasn't visible yet at this plate's
        # very first-ever cold-start (that only runs once, using whatever `now` was at
        # the time). A plate transfer whose NEW vehicle's VBIo registration postdates
        # this plate's first-ever processed detection would otherwise never be seeded
        # at all, forcing it to wait for MCAP's own confidence to independently clear
        # Th1 -- which can take months (see ENGINE_LOGIC_FLOW.md, HMU4444_NY case
        # study). Only queries vbio_seed_fn when a genuinely unmatched (make, model)
        # is seen; once admitted, this branch never re-fires for it.
        if det_make and det_model and top_key and vbio_seed_fn is not None and not any(r["key"] == top_key for r in records):
            for seed in vbio_seed_fn(detection.plate_number, detection.plate_state):
                seed_make, seed_model = self._canonicalize(seed["Make"], seed["Model"])
                if normalize_model_key(seed_make, seed_model) == normalize_model_key(det_make, det_model):
                    records.append({
                        "key": top_key,
                        "plateNumber": detection.plate_number,
                        "plateState": detection.plate_state,
                        "make": seed_make,
                        "model": seed_model,
                        "firstSeen": seed.get("FirstSeen") or now,
                        "lastSeen": seed.get("LastSeen") or now,
                        "lastDecayedAt": now,
                        "lastTouchedAt": seed.get("LastSeen") or now,
                        "observationCount": seed.get("ObservationCount", 0),
                        "score": cfg.seed_base * (seed.get("HmmPosterior") or 1.0),
                        "posterior": cfg.epsilon,
                        "hmmPosterior": seed.get("HmmPosterior"),
                        "status": seed.get("Status"),
                        "source": "VBIO",
                    })
                    break

        # Admit a brand-new candidate from a Th1-passing read that isn't in Redis yet.
        if is_mmc_detected and top_key and not any(r["key"] == top_key for r in records):
            records.append({
                "key": top_key,
                "plateNumber": detection.plate_number,
                "plateState": detection.plate_state,
                "make": det_make,
                "model": det_model,
                "firstSeen": now,
                "lastSeen": now,
                "lastDecayedAt": now,
                "lastTouchedAt": now,
                "observationCount": 0,
                "score": 0.0,
                "posterior": cfg.epsilon,
                "hmmPosterior": None,  # no VBIo backing for a brand-new sighting
                # PENDING, not ACTIVE: per the VRDB Plan's admission flow, one high-confidence
                # read doesn't immediately promote a never-seen-before association -- it still
                # needs to accumulate observations first. See cleanup.maybe_promote() (called
                # below, after this tick's update) for the PENDING -> ACTIVE promotion rule.
                "status": STATUS_PENDING,
                "source": "OBSERVED",
            })

        if not records:
            # Nothing known about this plate at all, and this read didn't clear Th1 either.
            if not dry_run:
                self.store.set_last_update(detection.plate_number, detection.plate_state, now)
            return EnrichmentResult(
                plate_number=detection.plate_number, plate_state=detection.plate_state,
                tier="NONE", make=None, model=None, confidence=None, candidates=[],
            )

        # --- 1. decay in place, per candidate ---
        decay_all(records, now, cfg.lambda_base)

        candidates = [r["key"] for r in records]
        scores = {r["key"]: r["score"] for r in records}
        prev_posteriors = {r["key"]: r["posterior"] for r in records}
        # Tie-break for every argmax below: two candidates landing on the exact same
        # score/posterior most commonly means neither has ever been reinforced by a
        # real detection (e.g. two VBIo seeds that just decay in lockstep forever) --
        # prefer whichever was actually seen more recently over whichever happened to
        # load first.
        recency = {r["key"]: r.get("lastSeen") for r in records}

        # --- 2. prior / likelihood / posterior ---
        t_same_dynamic = compute_t_same_dynamic(len(candidates), gap_days, cfg.t_same_base, cfg.lambda_base)
        pi_score = compute_pi_score(scores, cfg.eps_t)
        trans_p = compute_transition_prior(candidates, prev_posteriors, pi_score, t_same_dynamic)
        transition_total = sum(trans_p.values())
        trans_p = (
            {key: value / transition_total for key, value in trans_p.items()}
            if transition_total > 0
            else pi_score
        )

        # detection.top_k_confidences is naturally keyed by the bare "make_model" string
        # (e.g. from a top-5 prediction list, which has no notion of plate-scoped keys);
        # translate it into the full plate-scoped key space used by `records`/`candidates`.
        full_key_by_simple = {normalize_model_key(r["make"], r["model"]): r["key"] for r in records}
        top_k_confidences = {
            full_key_by_simple[simple_key]: conf
            for simple_key, conf in detection.top_k_confidences.items()
            if simple_key in full_key_by_simple
        }
        if top_key and detection.confidence is not None:
            top_k_confidences[top_key] = detection.confidence
        if det_make and detection.confidence is not None:
            # This frame's MAKE is still real evidence even when MCAP's exact model
            # text doesn't match any live candidate (e.g. "Toyota Supra" detected,
            # only "Toyota GR Supra" lives) -- give every OTHER live same-make
            # candidate a fraction of this confidence as its likelihood instead of
            # leaving it at the epsilon floor, so the Bayes posterior below can weigh
            # this make hint against the accumulated-score prior instead of the
            # make hint being invisible to it entirely. Never overwrites an existing
            # entry (an exact match, or real top-k data, is stronger evidence).
            for r in records:
                key = r["key"]
                if key in top_k_confidences:
                    continue
                if r["make"] and r["make"].strip().casefold() == det_make.strip().casefold():
                    top_k_confidences[key] = detection.confidence * cfg.same_make_likelihood_weight
        # Always fuse this tick's evidence with the transition prior through the same
        # Bayes update, regardless of how many entries top_k_confidences ended up with.
        # compute_likelihoods() floors every candidate ABSENT from top_k_confidences at
        # epsilon -- when top_k_confidences is empty (no detection at all this tick,
        # e.g. MCAP returned Unknown), that floor is UNIFORM across every candidate, so
        # the likelihood term is a no-op and the posterior below reduces to exactly
        # trans_p (renormalized): "no new evidence this tick, carry the prior forward."
        # That's the correct behavior for a no-read tick -- and it also correctly fuses
        # the common in-between case (exactly one piece of real evidence, no same-make
        # rival to pad out a 2nd top_k_confidences entry) through the SAME formula,
        # instead of a special-cased fallback that discarded trans_p's memory in favor
        # of comparing only each candidate's current decayed `score`. That fallback let
        # a single strong misread's score "lock in" as the apparent winner on every
        # later no-detection tick, with the plate's long-standing true candidate never
        # getting a chance to be favored again purely because trans_p (which DOES carry
        # that memory forward) was never consulted.
        likelihoods = compute_likelihoods(candidates, top_k_confidences, cfg.epsilon)
        posteriors, best_candidate, max_posterior = compute_posteriors(
            candidates, likelihoods, trans_p, recency=recency
        )

        # --- 3. apply the observation itself ---
        if is_mmc_detected and top_key:
            apply_high_confidence_update(records, top_key, detection.confidence, now, cfg.malus_rate, cfg.alpha)
        elif detection.confidence is not None and top_key and any(r["key"] == top_key for r in records):
            winner = next(r for r in records if r["key"] == top_key)
            apply_low_confidence_update(winner, detection.confidence, now, cfg.alpha)

        # PENDING -> ACTIVE promotion (cleanup.py) -- checked for whichever candidate
        # this tick actually touched, right after its observationCount could have
        # changed above. No-op for anything other than an OBSERVED candidate still
        # PENDING with >= cfg.n_activate real confirmations.
        if top_key and any(r["key"] == top_key for r in records):
            maybe_promote(next(r for r in records if r["key"] == top_key), cfg.n_activate)

        scores_after = {r["key"]: r["score"] for r in records}
        for r in records:
            r["posterior"] = posteriors[r["key"]]

        # --- 4. pack the result ---
        tier_result: TierResult = decide_tier(
            is_mmc_detected=is_mmc_detected,
            top_key=top_key,
            best_candidate=best_candidate,
            max_posterior=max_posterior,
            scores=scores_after,
            th3=cfg.th3,
            recency=recency,
        )

        # --- 5. persist (unless dry_run) ---
        if not dry_run:
            self.store.save_candidates(records)
            self.store.set_last_update(detection.plate_number, detection.plate_state, now)

        result_make = result_model = result_confidence = None
        if tier_result.result_key:
            winner_record = next(r for r in records if r["key"] == tier_result.result_key)
            result_make, result_model = winner_record["make"], winner_record["model"]
            result_confidence = detection.confidence if tier_result.tier == "OBSERVED" else max_posterior

        return EnrichmentResult(
            plate_number=detection.plate_number, plate_state=detection.plate_state,
            tier=tier_result.tier, make=result_make, model=result_model,
            confidence=result_confidence, candidates=records,
        )
