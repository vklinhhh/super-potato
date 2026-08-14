# VBIo PoC

A proof-of-concept pipeline for VBIo's "MMC Enriched" flow: MCAP image analysis ->
plate/state parsing -> VBIo/VBIo lookup -> HMM/decay-based enrichment logic, matching
the design in `VBIo_Plan_20260709.pdf` and prototyped in `simulation_hmm.html`.

## Layout

```
poc/
├── data/                         # parquet files (not included in this delivery --
│                                    point the tools below at your own copies)
│   ├── vbio.parquet
│   └── vin2plate_all_columns_export.parquet
│
├── vbio/                         # the package
│   ├── clients/                  # talks to the outside world -- no decision logic
│   │   ├── mcap_client.py        # MCAP perception-analyzer API client
│   │   ├── query_vbio.py         # VBIo parquet plate/state lookup (duckdb)
│   │   └── redis_store.py        # RedisAssociationStore + InMemoryAssociationStore
│   │                                (same interface; in-memory is the default -- no Redis needed)
│   │
│   ├── schema/
│   │   └── vbio_schema.py        # VBIo parquet schema + bootstrap from vin2plate export
│   │
│   ├── loaders/
│   │   └── vbio_redis_loader.py  # seeds Redis from a VBIo parquet file
│   │
│   ├── enrichment/                # pure decision logic -- no network/IO, unit-testable
│   │   ├── config.py             # Th1, Th3, lambda, T_same, epsilon, SEED_BASE, malus rates
│   │   ├── keys.py                # normalize_model_key / build_redis_key
│   │   ├── scoring.py             # decay + score update (High/Low branch, malus)
│   │   ├── posterior.py           # T_same transition, likelihood, Bayesian posterior
│   │   ├── tiering.py             # OBSERVED / HIGH / MEDIUM / LOW / NONE decision
│   │   └── engine.py              # EnrichmentEngine -- wires the above into one "tick"
│   │
│   └── vehicle_lookup_service.py  # orchestrator: MCAP -> plate/state -> VBIo lookup
│
├── scripts/                       # thin CLI wrappers -- work from any directory
│   ├── bootstrap_vbio.py
│   ├── load_redis.py
│   └── run_lookup.py
│
├── tests/
│   └── test_enrichment_engine.py  # regression tests for the enrichment engine
│
└── default.json                   # MCAP API config (dev/stage -> url, token)
```

**Why split it this way:** `clients/` only knows how to talk to MCAP, parquet, and
Redis -- no decision logic lives there. `enrichment/` is pure math ported from
`simulation_hmm.html`, with no I/O at all, so it's trivially unit-testable against
known scenarios. `vehicle_lookup_service.py` is the only place that wires clients and
enrichment together end-to-end.

## Setup

```bash
pip install duckdb pyarrow requests redis --break-system-packages
```

`fakeredis` is only needed to run the test suite without a live Redis server:

```bash
pip install fakeredis pytest --break-system-packages
```

All commands below assume you're running from the `poc/` root (needed so the `vbio`
package resolves via `python -m`). The `scripts/` wrappers work from anywhere.

## 1. Build VBIo from your vin2plate export

```bash
python -m vbio.schema.vbio_schema estimate --source ./data/vin2plate_all_columns_export.parquet
python -m vbio.schema.vbio_schema bootstrap --source ./data/vin2plate_all_columns_export.parquet --out ./data/vbio.parquet
python -m vbio.schema.vbio_schema describe --parquet ./data/vbio.parquet
```

## 2. Run an image through MCAP -> VBIo lookup -> enrichment

Fully local -- no Redis, no server. Each detection seeds a fresh in-memory candidate
set from VBIo, runs the enrichment engine's decay/Bayes/tiering logic, and prints the
result. Nothing is written back to the parquet file.

```bash
export MCAP_TOKEN="your-token-here"
python -m vbio.vehicle_lookup_service test.jpg --env dev --parquet ./data/vbio.parquet
```

Output now includes an `"enrichment"` block alongside the raw `"vbio_matches"`:

```json
{
  "vbio_matches": [
    {"Make": "HYUNDAI", "Model": "SONATA", "CredibilityScore": 2.0, "...": "..."},
    {"Make": "MERCEDES-BENZ", "Model": "C CLASS", "CredibilityScore": 2.0, "...": "..."}
  ],
  "enrichment": {
    "tier": "OBSERVED",
    "make": "HYUNDAI",
    "model": "SONATA",
    "confidence": 0.93,
    "candidates": [
      {"make": "HYUNDAI", "model": "SONATA", "score": 2.73, "observationCount": 3},
      {"make": "MERCEDES-BENZ", "model": "C CLASS", "score": 1.6326, "observationCount": 1}
    ]
  }
}
```

`vbio_matches` is the raw, unranked VBIo lookup (kept for visibility/debugging).
`enrichment` is the actual answer: a single tiered decision, with every candidate's
score/posterior after this detection was applied.

## 3. (Optional, for later) Seed Redis and run the engine against live state

The enrichment engine also works against a real Redis instance instead of the
in-memory store, once you're ready to persist state across calls rather than
recomputing it fresh from VBIo every time:

```bash
python -m vbio.loaders.vbio_redis_loader --parquet ./data/vbio.parquet --redis-url redis://localhost:6379/0
```

```python
import redis
from vbio.clients.redis_store import RedisAssociationStore
from vbio.enrichment.engine import Detection, EnrichmentEngine

client = redis.Redis.from_url("redis://localhost:6379/0", decode_responses=True)
engine = EnrichmentEngine(RedisAssociationStore(client))
# same Detection/process_detection(...) call as the in-memory version above
```

## 4. Run the tests

```bash
python -m pytest tests/test_enrichment_engine.py -v
# or, without pytest installed:
python tests/test_enrichment_engine.py
```
