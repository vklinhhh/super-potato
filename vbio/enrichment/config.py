"""
config.py

Tunable parameters for the enrichment engine, matching the UI-adjustable inputs in
simulation_hmm.html (decayLambdaInput, tSameInput, epsilonInput, detectionThresholdInput,
outputThresholdInput, epsTInput, malusRateInput, malusLowRateInput) plus the SEED_BASE
constant hardcoded inside computeHMMSequence().
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
    malus_rate: float = 0.10       # proportional malus rate, High-confidence branch
    malus_low_rate: float = 0.10   # partial-credit rate, Low-confidence branch
    seed_base: float = 2.0         # score granted once, when a VBIo/VBIo row is first admitted


DEFAULT_CONFIG = EnrichmentConfig()
