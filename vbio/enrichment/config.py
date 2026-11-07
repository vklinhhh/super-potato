"""
config.py

Tunable parameters for the enrichment engine, matching the UI-adjustable inputs in
simulation_hmm.html (decayLambdaInput, tSameInput, epsilonInput, detectionThresholdInput,
outputThresholdInput, epsTInput, malusRateInput) plus the SEED_BASE constant hardcoded
inside computeHMMSequence() -- with `alpha`/`seed_base` retuned per the EMA score-update
rewrite (see scoring.py's module docstring and ENGINE_LOGIC_FLOW.md section 5.3/8 for the
full rationale: the original additive `score += confidence` has no ceiling and grows
without bound for a frequently-detected plate, which both breaks the 16-bit/0.00-655.35
fixed-point Redis field RFC-01 budgets for CredibilityScore, and contradicts the VBIo
schema's own documented 0.0-100.0 range).

`alpha`/`seed_base` were tuned by sweeping scripts/tune_score_params.py against the
97-profile test_data accuracy harness (see git history / report for the sweep results:
accuracy was flat-to-better across alpha in [0.2, 0.35] and seed_base in [0.5, 0.8],
peaking at ~89.5% around alpha=0.3, seed_base=0.7 vs. 88.70% for the old unbounded
formula) -- re-run that sweep and update these two values whenever the test set grows
meaningfully, rather than treating them as fixed forever.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class EnrichmentConfig:
    lambda_base: float = 0.00385   # decay rate; half-life ~180 days per the VRDB Plan doc
    t_same_base: float = 0.999     # frame-to-frame continuity prior as gapDays -> 0
    epsilon: float = 0.010         # floor for likelihoods, and seed posterior for new candidates
    th1: float = 0.90              # "isMMCDetected" / direct-observation confidence gate
    th3: float = 0.80              # output-tier gate (HIGH requires posterior >= th3)
    eps_t: float = 0.05            # floor added to score when computing the pi(S) prior weights
    malus_rate: float = 0.10       # proportional malus rate, applied to EVERY non-winner candidate
    # EMA weight for new evidence in the winner's score update (see scoring.py):
    #   score = alpha * confidence + (1 - alpha) * score_decayed
    # Replaces the old unbounded `score += confidence` (high-confidence) and
    # `score += malus_low_rate * confidence` (low-confidence) -- both branches now share
    # this single formula, which keeps `score` naturally bounded within roughly [0, 1]
    # (a weighted average of past score and a confidence in [0, 1] can never exceed the
    # larger of the two), regardless of detection frequency. Tuned via the sweep above.
    alpha: float = 0.3
    # score granted once, when a VBIo row is first admitted (score = seed_base *
    # HmmPosterior). Lowered from the pre-EMA default of 2.0: under EMA, a real detection
    # can never push score above ~1.0, so a seed_base of 2.0 would make every VBIo seed
    # permanently outscore any number of real confirmations. Tuned via the sweep above.
    seed_base: float = 0.7
    # Not from simulation_hmm.html -- added so a single top-1 MCAP reading (no true
    # top-k ranking) still gives the Bayes posterior a MAKE-level hint when the exact
    # (make, model) doesn't match any live candidate (e.g. "Toyota Supra" detected,
    # only "Toyota GR Supra" and "Subaru Brz" live). Other live candidates sharing
    # just the detected make get `confidence * same_make_likelihood_weight` as their
    # likelihood (instead of the epsilon floor); this only shifts this frame's
    # posterior/tier decision, it never touches persisted `score`.
    same_make_likelihood_weight: float = 0.5
    # Th1 used instead of `th1` for a plate that has NEVER had a VBIo-sourced (DMV)
    # candidate -- i.e. nothing for this plate came from a real registration, every
    # candidate so far is purely OBSERVED (camera-only). On such a plate, a repeated,
    # consistent-but-sub-Th1 read (e.g. CHC673_OK's correct "Nissan Sentra" at
    # 0.54-0.80, never once hitting 0.90) can otherwise never be admitted/trusted at
    # all, while a single one-off misread that happens to clear the FULL Th1 (e.g. a
    # platform-confusable model at 0.94) gets trusted immediately and then never
    # challenged again. Lowering the bar specifically for "no DMV backing exists"
    # plates costs nothing extra to check (`any(r["source"] == "VBIO" for r in
    # records)` over records already loaded this tick -- no new storage/Redis key).
    # Tuned via a sweep (0.50-0.85) against the 97-profile test_data accuracy harness:
    # flat at the best accuracy (92.74%, +69 records over the 0.90 no-op baseline's
    # 91.21%, zero regressions) across [0.55, 0.80] -- 0.80 picked as the end of that
    # plateau closest to the original th1, minimizing how far this diverges from the
    # literal RFC-01 Th1 for plates that DO have DMV backing. Re-sweep if the test set
    # grows meaningfully; see ENGINE_LOGIC_FLOW.md section 8.
    th1_no_vbio: float = 0.80
    # Redis-cache TTL for OBSERVED (camera-only, no DMV backing) candidates that are
    # never reconfirmed -- see cleanup.gc_and_sync(). A plate detected often enough is
    # unaffected (promotion below kicks in long before this); this only reclaims
    # candidates that really did turn out to be one-off misreads. 180 days matches the
    # VRDB Plan doc's own State Lifecycle & Periodical Sync slide. Validated against the
    # 97-profile harness: +0.73pp over the no-GC baseline (96.07% -> 96.80%), zero new
    # regressions -- see ENGINE_LOGIC_FLOW.md's "Redis TTL + PENDING -> ACTIVE lifecycle"
    # section. Re-sweep via scripts/tune_gc_params.py if the test set grows meaningfully.
    gc_days: float = 180.0
    # Number of real (high-confidence) confirmations an OBSERVED candidate needs
    # before cleanup.maybe_promote() flips it PENDING -> ACTIVE (permanent, immune to
    # gc_days above). Does NOT delay how much a candidate is trusted in score/posterior
    # comparisons -- those are unaffected by PENDING vs ACTIVE; this only decides
    # whether an unconfirmed candidate eventually ages out of the cache. 3 matches
    # RFC-01 Phase 1's own majority-voting N. Re-sweep via scripts/tune_gc_params.py
    # alongside gc_days if the test set grows meaningfully.
    n_activate: int = 3


DEFAULT_CONFIG = EnrichmentConfig()
