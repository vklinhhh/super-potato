"""
vbio_redis_loader.py

Loads VBIo parquet rows into Redis as the Plate<->Vehicle association records that
simulation_hmm.html's `insert()` function builds at runtime, so the seeded Redis state
is shaped exactly like what the live HMM/decay logic expects.

Field mapping, VBIo row -> Redis hash (mirrors insert() in simulation_hmm.html):

    key              = f"vbio:{PlateNumber}:{PlateState}:{normalizeModelKey(Make, Model)}"
                        normalizeModelKey is a like-for-like Python port of the JS helper.
                        plateNumber/plateState are prefixed because a real Redis store is
                        global across all plates -- the JS demo's `redis` Map is scoped to
                        ONE plate at a time (rebuilt per plate inside computeHMMSequence),
                        which doesn't hold once this is a shared Redis instance.
    plateNumber      <- VBIo PlateNumber
    plateState       <- VBIo PlateState
    make             <- VBIo Make
    model            <- VBIo Model
    firstSeen        <- VBIo FirstSeen
    lastSeen         <- VBIo LastSeen
    lastDecayedAt    = now(), set fresh at WRITE time -- never read from VBIo or any
                        persisted field. Per insert()'s decayRefTime comment: it's always
                        "now", so a seed score is never retroactively crushed by decaying
                        it since some old date.
    observationCount <- VBIo ObservationCount
    score            <- VBIo CredibilityScore (this IS the record's "score" field --
                        simulation_hmm.html's own UI table renders r.score as
                        "CredibilityScore", so the names line up 1:1)
    posterior        = epsilon (small uniform seed prior, matching insert()'s hardcoded
                        `posterior: epsilon`; deliberately NOT derived from HmmPosterior)
    hmmPosterior     <- VBIo HmmPosterior (kept for reference/traceability; the JS demo
                        doesn't persist this on the record itself, but VBIo does)
    status           <- VBIo Status
    source           = 'VBIo' (matches the literal source tag insert() uses when seeding
                        from vrdbEntries)

Usage:
    import redis
    from vbio.loaders.vbio_redis_loader import VBIoRedisLoader

    client = redis.Redis.from_url("redis://localhost:6379/0", decode_responses=True)
    loader = VBIoRedisLoader(client)
    result = loader.load_parquet("./data/vbio.parquet")
    print(result)  # {"written": n, "skipped": n}

Or from the command line:
    python -m vbio.loaders.vbio_redis_loader --parquet ./data/vbio.parquet --redis-url redis://localhost:6379/0
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import duckdb
import pyarrow.parquet as pq

# Makes `from vbio.enrichment... import ...` resolve even when this file is run
# directly (e.g. `python vbio/loaders/vbio_redis_loader.py ...`) rather than via `-m`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from vbio.enrichment.keys import build_redis_key, normalize_model_key  # noqa: F401,E402 (re-exported below)
from vbio.clients.redis_connection import build_redis_client, build_redis_client_for_env  # noqa: E402

# Matches simulation_hmm.html's default epsilonInput value.
EPSILON_DEFAULT = 0.010


def _iso(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _to_redis_value(value):
    """
    redis-py can only encode bytes/str/int/float. Arrow batches can hand back other
    Python types depending on the source column's inferred type (e.g. decimal.Decimal
    for a DECIMAL column) -- cast anything unexpected down to something safe.
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, str, int, float)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    try:
        import decimal
        if isinstance(value, decimal.Decimal):
            return float(value)
    except ImportError:
        pass
    return str(value)


class VBIoRedisLoader:
    """Loads VBIo parquet rows into Redis as Plate<->Vehicle association hashes."""

    def __init__(self, redis_client, epsilon: float = EPSILON_DEFAULT):
        """
        :param redis_client: any redis-py-compatible client (real redis.Redis, or a
                              fake/mock client exposing .exists/.hset for testing).
        :param epsilon: seed value for the record's `posterior` field, matching
                         simulation_hmm.html's epsilonInput default.
        """
        self.redis = redis_client
        self.epsilon = epsilon

    def build_record(self, row: dict) -> dict:
        """Builds the Redis hash fields for one VBIo row, per the mapping documented above."""
        return {
            "key": build_redis_key(row["PlateNumber"], row["PlateState"], row["Make"], row["Model"]),
            "plateNumber": row["PlateNumber"],
            "plateState": row["PlateState"],
            "make": row["Make"],
            "model": row["Model"],
            "firstSeen": _iso(row.get("FirstSeen")),
            "lastSeen": _iso(row.get("LastSeen")),
            "lastDecayedAt": datetime.now(timezone.utc).isoformat(),  # always "now" at write time
            "observationCount": row.get("ObservationCount", 0),
            "score": row.get("CredibilityScore", 0.0),
            "posterior": self.epsilon,
            "hmmPosterior": row.get("HmmPosterior"),
            "status": row.get("Status"),
            "source": "VBIO",
        }

    def load_row(self, row: dict, overwrite: bool = False) -> Optional[str]:
        """
        Writes a single VBIo row into Redis as a hash.

        :return: the Redis key written, or None if skipped (key already exists and
                 overwrite=False) -- mirroring insert()'s admission guard `!redis.has(k)`.
        """
        record = self.build_record(row)
        key = record["key"]

        if not overwrite and self.redis.exists(key):
            return None

        mapping = {k: _to_redis_value(v) for k, v in record.items()}
        self.redis.hset(key, mapping=mapping)
        return key

    # If a source file has more rows than this, load_parquet() refuses to run unless
    # force=True is passed. This exists specifically because a full VBIo file can be
    # hundreds of millions of rows -- bulk-loading that into Redis is almost always the
    # wrong move (a single dev/staging cluster can't hold it, and even prod-sized
    # clusters shouldn't hold cold historical data that isn't being actively queried).
    # The intended pattern is cold-start seeding via EnrichmentEngine's vbio_seed_fn,
    # which only pulls in the handful of rows for a plate that's actually been detected.
    LARGE_FILE_ROW_THRESHOLD = 5_000_000

    def load_parquet(
        self,
        vbio_parquet_path: str,
        overwrite: bool = False,
        batch_size: int = 50_000,
        force: bool = False,
        show_progress: bool = True,
    ) -> dict:
        """
        Loads a VBIo parquet file into Redis, streaming in Arrow batches so memory use
        stays bounded regardless of file size (never materializes the whole file as a
        DataFrame or a Python list of dicts).

        :param force: required if the source file has more than LARGE_FILE_ROW_THRESHOLD
                       rows -- see the class-level docstring note on why bulk-loading a
                       full VBIo file is almost always the wrong approach.
        :return: {"written": n, "skipped": n}
        """
        total_rows = pq.ParquetFile(vbio_parquet_path).metadata.num_rows

        if total_rows > self.LARGE_FILE_ROW_THRESHOLD and not force:
            raise ValueError(
                f"{vbio_parquet_path} has {total_rows:,} rows, which is far more than a "
                f"typical Redis cluster should hold as a bulk load (threshold: "
                f"{self.LARGE_FILE_ROW_THRESHOLD:,}). This usually means you want "
                f"cold-start seeding via EnrichmentEngine's vbio_seed_fn instead of "
                f"bulk-loading the whole file. If you really intend to bulk-load all "
                f"{total_rows:,} rows, re-run with force=True (or --force on the CLI) -- "
                f"but make sure your Redis cluster actually has enough memory first."
            )

        con = duckdb.connect()
        result = con.execute(f"SELECT * FROM '{vbio_parquet_path}'")
        batch_reader = result.to_arrow_reader(batch_size=batch_size)

        written = 0
        skipped = 0
        rows_seen = 0
        start = time.time()

        for batch in batch_reader:
            for row in batch.to_pylist():
                key = self.load_row(row, overwrite=overwrite)
                if key is None:
                    skipped += 1
                else:
                    written += 1
            rows_seen += batch.num_rows

            if show_progress:
                elapsed = time.time() - start
                rate = rows_seen / elapsed if elapsed > 0 else 0
                print(f"\r{rows_seen:,}/{total_rows:,} rows ({rate:,.0f} rows/sec)", end="", flush=True)

        con.close()
        if show_progress:
            print()

        return {"written": written, "skipped": skipped}


def _main():
    parser = argparse.ArgumentParser(description="Load a VBIo parquet file into Redis.")
    parser.add_argument("--parquet", required=True, help="Path to the VBIo parquet file.")
    parser.add_argument(
        "--env", default=None,
        help="Environment name from default.json's 'redis' block (e.g. dev, staging, prod). "
             "If given, this overrides --redis-url/--cluster/--tls-ca-cert/--iam-auth below.",
    )
    parser.add_argument("--config", default="default.json", help="Path to config json (used with --env).")
    parser.add_argument("--redis-url", default="redis://localhost:6379/0", help="Redis connection URL.")
    parser.add_argument(
        "--cluster", action="store_true",
        help="Use a cluster-aware client (required for GCP Memorystore for Redis Cluster; "
             "leave off for a plain standalone Redis, e.g. local dev).",
    )
    parser.add_argument(
        "--tls-ca-cert", default=None,
        help="Path to a CA certificate file, if the target has in-transit encryption (TLS) enabled.",
    )
    parser.add_argument(
        "--iam-auth", action="store_true",
        help="Authenticate using GCP IAM (Application Default Credentials) instead of a static "
             "password. Requires --tls-ca-cert (IAM tokens must not travel over plaintext) and the "
             "running identity must have roles/redis.dbConnectionUser on the cluster.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing keys instead of skipping them.")
    parser.add_argument("--epsilon", type=float, default=EPSILON_DEFAULT, help="Seed value for the posterior field.")
    parser.add_argument("--batch-size", type=int, default=50_000, help="Rows read per streaming batch.")
    parser.add_argument(
        "--force", action="store_true",
        help=f"Required if the source file has more than "
             f"{VBIoRedisLoader.LARGE_FILE_ROW_THRESHOLD:,} rows. Bulk-loading a full VBIo "
             f"file is almost always the wrong approach -- see load_parquet()'s docstring.",
    )
    args = parser.parse_args()

    if args.env:
        client = build_redis_client_for_env(env=args.env, config_path=args.config)
    else:
        if args.iam_auth and not args.tls_ca_cert:
            parser.error("--iam-auth requires --tls-ca-cert (IAM tokens must not be sent over plaintext)")
        client = build_redis_client(
            args.redis_url, use_cluster=args.cluster, tls_ca_cert=args.tls_ca_cert, iam_auth=args.iam_auth,
        )

    loader = VBIoRedisLoader(client, epsilon=args.epsilon)
    try:
        result = loader.load_parquet(
            args.parquet, overwrite=args.overwrite, batch_size=args.batch_size, force=args.force,
        )
    except ValueError as exc:
        parser.error(str(exc))
        return

    print(f"Written: {result['written']}, Skipped (already existed): {result['skipped']}")


if __name__ == "__main__":
    _main()
