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


class EnrichmentEngine:
    def __init__(self, redis_store, config: EnrichmentConfig = DEFAULT_CONFIG):
        """
        :param redis_store: a RedisAssociationStore (see clients/redis_store.py) --
                             handles reading/writing the plate's candidate records.
        """
        self.store = redis_store
        self.config = config

    def process_detection(self, detection: Detection, vbio_seed_fn: Optional[VBIoSeedFn] = None,
                           dry_run: bool = False) -> EnrichmentResult:
        """
        :param dry_run: if True, computes and returns the full tiered result WITHOUT
                         writing anything back to the store (no save_candidates, no
                         set_last_update) -- useful for previewing what the engine
                         WOULD do before committing to a live Redis instance.
        """
        cfg = self.config
        now = detection.timestamp or datetime.now(timezone.utc)

        # gapDays uses the PREVIOUS plate-level detection time, read before anything else
        # is touched -- matches computeHMMSequence() reading `lastUpdate` at the very top
        # of each loop iteration, before decay/admission/posterior for that tick.
        last_update = self.store.get_last_update(detection.plate_number, detection.plate_state) or now
        gap_days = max(0.0, (now - last_update).total_seconds() / 86400.0)

        records = self.store.get_candidates(detection.plate_number, detection.plate_state)

        # Cold start: nothing in Redis yet for this plate -- admit VBIo/VBIo seed rows.
        if not records and vbio_seed_fn is not None:
            for seed in vbio_seed_fn(detection.plate_number, detection.plate_state):
                key = build_redis_key(detection.plate_number, detection.plate_state, seed["Make"], seed["Model"])
                records.append({
                    "key": key,
                    "plateNumber": detection.plate_number,
                    "plateState": detection.plate_state,
                    "make": seed["Make"],
                    "model": seed["Model"],
                    "firstSeen": seed.get("FirstSeen") or now,
                    "lastSeen": seed.get("LastSeen") or now,
                    "lastDecayedAt": now,
                    "observationCount": seed.get("ObservationCount", 0),
                    "score": cfg.seed_base * (seed.get("HmmPosterior") or 1.0),
                    "posterior": cfg.epsilon,
                    "hmmPosterior": seed.get("HmmPosterior"),  # kept for traceability/debugging only
                    "status": seed.get("Status"),              # kept for traceability/debugging only
                    "source": "VBIO",
                })

        top_key = None
        is_mmc_detected = detection.confidence is not None and detection.confidence >= cfg.th1
        if detection.make and detection.model:
            # NOTE: this must be the same full, plate-scoped key format used for every
            # record's "key" field (build_redis_key), NOT the bare normalize_model_key --
            # otherwise it can never match an existing candidate's "key".
            top_key = build_redis_key(detection.plate_number, detection.plate_state, detection.make, detection.model)

        # Admit a brand-new candidate from a Th1-passing read that isn't in Redis yet.
        if is_mmc_detected and top_key and not any(r["key"] == top_key for r in records):
            records.append({
                "key": top_key,
                "plateNumber": detection.plate_number,
                "plateState": detection.plate_state,
                "make": detection.make,
                "model": detection.model,
                "firstSeen": now,
                "lastSeen": now,
                "lastDecayedAt": now,
                "observationCount": 0,
                "score": 0.0,
                "posterior": cfg.epsilon,
                "hmmPosterior": None,  # no VBIo backing for a brand-new sighting
                # PENDING, not ACTIVE: per the VRDB Plan's admission flow, one high-confidence
                # read doesn't immediately promote a never-seen-before association -- it still
                # needs to accumulate observations first. (Dynamic PENDING->ACTIVE promotion
                # based on a score threshold isn't implemented yet -- this just sets the
                # correct starting value.)
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

        # --- 2. prior / likelihood / posterior ---
        t_same_dynamic = compute_t_same_dynamic(len(candidates), gap_days, cfg.t_same_base, cfg.lambda_base)
        pi_score = compute_pi_score(scores, cfg.eps_t)
        trans_p = compute_transition_prior(candidates, prev_posteriors, pi_score, t_same_dynamic)

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
        likelihoods = compute_likelihoods(candidates, top_k_confidences, cfg.epsilon)

        posteriors, best_candidate, max_posterior = compute_posteriors(candidates, likelihoods, trans_p)
        for r in records:
            r["posterior"] = posteriors[r["key"]]

        # --- 3. apply the observation itself ---
        if is_mmc_detected and top_key:
            apply_high_confidence_update(records, top_key, detection.confidence, now, cfg.malus_rate)
        elif detection.confidence is not None and top_key and any(r["key"] == top_key for r in records):
            winner = next(r for r in records if r["key"] == top_key)
            apply_low_confidence_update(winner, detection.confidence, now, cfg.malus_low_rate)

        scores_after = {r["key"]: r["score"] for r in records}

        # --- 4. pack the result ---
        tier_result: TierResult = decide_tier(
            is_mmc_detected=is_mmc_detected,
            top_key=top_key,
            best_candidate=best_candidate,
            max_posterior=max_posterior,
            scores=scores_after,
            th3=cfg.th3,
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
