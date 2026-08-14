"""
vbio_schema.py

Defines the VBIo parquet schema and bootstraps it from the vin2plate export. VBIo is
seed data for the Redis Plate<->Vehicle association table used by simulation_hmm.html
(see the `insert()` function there) -- one row per unique Plate+PlateState+Make+Model.

Schema is based on:
  - "VRDB Plan 20260709.pdf" -> "VRDB Schema" section (PlateNumber, PlateState, Make,
    Model, FirstSeen, LastSeen, ObservationCount, CredibilityScore, Status)
  - simulation_hmm.html's Redis association-table model, which implements that same
    schema at runtime (see the `insert()` function and the rendered table header
    ['Make','Model','FirstSeen','LastSeen','ScoreAsOf','ObsCount','CredibilityScore',
    'HmmPosterior','Source'] around line ~1069). Note ScoreAsOf/lastDecayedAt is called
    out in that file as "Internal only — NOT part of the VBIo schema, not synced" and is
    therefore NOT included here. HmmPosterior/Source/posterior are runtime-only fields
    computed by the HMM step itself, not part of the persisted seed record, so they're
    not included either.

VehicleYear is NOT a stored column here (it's not part of the VBIo target schema, and
the source export has no true observation timestamps to persist). Instead it's used as
the only available time signal to derive FirstSeen/LastSeen and to collapse duplicate
rows: the vin2plate export has one row per (Plate, PlateState, Make, Model, VehicleYear)
-- e.g. the same plate/state/vehicle can appear multiple times differing only by
VehicleYear (successive registration-year snapshots). VBIo collapses those into a single
row per (PlateNumber, PlateState, Make, Model):
  - ObservationCount seeds from the number of VehicleYear rows collapsed into that group
    (a proxy for how many distinct DMV snapshots confirm this association).
  - FirstSeen / LastSeen both seed from the LATEST (max) VehicleYear in the group,
    represented as Jan 1 of that year, since that's the only date signal available.

Usage:
    from vbio.schema.vbio_schema import build_vbio_schema, bootstrap_vbio, describe_vbio

    bootstrap_vbio(
        source_parquet="./data/vin2plate_all_columns_export.parquet",
        output_parquet="./data/vbio.parquet",
    )
    describe_vbio("./data/vbio.parquet")

Or from the command line (run from the poc/ root so the `vbio` package resolves):
    python -m vbio.schema.vbio_schema bootstrap --source ./data/vin2plate_all_columns_export.parquet --out ./data/vbio.parquet
    python -m vbio.schema.vbio_schema describe --parquet ./data/vbio.parquet

Or via the thin wrapper, which works from any directory:
    python scripts/bootstrap_vbio.py bootstrap --source ./data/vin2plate_all_columns_export.parquet --out ./data/vbio.parquet
"""

import argparse
import time
from datetime import timedelta

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

# Status lifecycle, per the VRDB Plan doc:
# "any new Make-Model pair ... is not immediately recorded in the official history.
#  Instead, it must undergo screening: PENDING (low score) -> accumulating points over
#  multiple observations -> ACTIVE when exceeding a safety threshold (e.g., 10 points)."
STATUS_PENDING = "PENDING"
STATUS_ACTIVE = "ACTIVE"
VALID_STATUSES = (STATUS_PENDING, STATUS_ACTIVE)

# (type, description) per field, used both to build pyarrow field metadata (so the
# schema is self-documenting inside the parquet file) and for describe_vbio() output.
FIELD_DOCS = {
    "Id": (pa.int64(), "Surrogate key generated after deduplication (does not correspond to the "
                        "original vin2plate row id, since multiple source rows can collapse into one)."),
    "PlateNumber": (pa.string(), "License plate number. Anchor key together with PlateState."),
    "PlateState": (pa.string(), "Two-letter plate registration state. Anchor key together with PlateNumber."),
    "Make": (pa.string(), "Vehicle make, e.g. TOYOTA."),
    "Model": (pa.string(), "Vehicle model, e.g. CAMRY."),
    "FirstSeen": (pa.timestamp("us"), "Derived from the latest VehicleYear seen for this "
                                       "PlateNumber+PlateState+Make+Model group in the source export "
                                       "(represented as Jan 1 of that year), since the source has no "
                                       "true observation timestamps."),
    "LastSeen": (pa.timestamp("us"), "Same derivation as FirstSeen (latest VehicleYear in the group). "
                                      "Both start identical at seed time; the live pipeline should update "
                                      "LastSeen independently as real detections come in."),
    "ObservationCount": (pa.int32(), "Seeded from the number of VehicleYear rows collapsed into this "
                                      "PlateNumber+PlateState+Make+Model group in the source export."),
    "CredibilityScore": (pa.float64(), "Accumulated confidence score for this association (0.0-100.0)."),
    "HmmPosterior": (pa.float64(), "Seed posterior confidence for this association, in [0.0, 1.0]. Matches "
                                    "the HmmPosterior column simulation_hmm.html's VBIo loader reads to scale "
                                    "the initial Redis score on admission (score = SEED_BASE * HmmPosterior)."),
    "Status": (pa.string(), f"Association lifecycle status: {STATUS_PENDING} (accumulating confirmations) "
                             f"or {STATUS_ACTIVE} (passed the admission threshold)."),
}

# Column order matches the VRDB Plan doc's table order, with Id first (surrogate key).
VBIO_COLUMN_ORDER = [
    "Id", "PlateNumber", "PlateState", "Make", "Model",
    "FirstSeen", "LastSeen", "ObservationCount", "CredibilityScore", "HmmPosterior", "Status",
]


def build_vbio_schema() -> pa.Schema:
    """Builds the VBIo pyarrow schema, with human-readable metadata attached to each field."""
    fields = [
        pa.field(name, FIELD_DOCS[name][0], metadata={"description": FIELD_DOCS[name][1]})
        for name in VBIO_COLUMN_ORDER
    ]
    return pa.schema(fields, metadata={
        "dataset": "VBIo",
        "based_on": "VRDB Plan 20260709.pdf - VRDB Schema section; simulation_hmm.html association model",
        "grain": "one row per unique (PlateNumber, PlateState, Make, Model)",
    })


def _format_duration(seconds: float) -> str:
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN/inf guard
        return "unknown"
    return str(timedelta(seconds=round(seconds)))


def estimate_bootstrap_time(source_parquet: str, sample_rows: int = 2_000_000) -> dict:
    """
    Estimates how long bootstrap_vbio() will take on the full source file, WITHOUT
    scanning the whole thing:
      - total row count comes from the parquet file's row-group metadata (instant,
        no data scan needed even for hundreds of millions of rows).
      - throughput comes from timing the same GROUP BY pipeline on a small sample
        (first `sample_rows` rows), then extrapolating linearly to the full file.

    This is a rough estimate: real throughput can vary with key cardinality (how many
    distinct Plate+PlateState+Make+Model groups exist) and disk/IO conditions, so treat
    it as a ballpark, not a guarantee.

    :return: {"total_rows", "sample_rows", "sample_seconds", "rows_per_second",
              "estimated_total_seconds"}
    """
    total_rows = pq.ParquetFile(source_parquet).metadata.num_rows
    sample_rows = min(sample_rows, total_rows) if total_rows else sample_rows

    con = duckdb.connect()
    start = time.time()
    con.execute(f"""
        SELECT
            Plate, PlateState, VehicleMake, VehicleModel,
            MAX(TRY_CAST(VehicleYear AS INTEGER)), COUNT(*)
        FROM (SELECT * FROM '{source_parquet}' LIMIT {sample_rows})
        GROUP BY Plate, PlateState, VehicleMake, VehicleModel
    """)
    elapsed = time.time() - start
    con.close()

    rows_per_second = (sample_rows / elapsed) if elapsed > 0 else float("inf")
    estimated_total_seconds = (total_rows / rows_per_second) if rows_per_second > 0 else float("nan")

    return {
        "total_rows": total_rows,
        "sample_rows": sample_rows,
        "sample_seconds": elapsed,
        "rows_per_second": rows_per_second,
        "estimated_total_seconds": estimated_total_seconds,
    }


def print_bootstrap_estimate(source_parquet: str, sample_rows: int = 2_000_000) -> dict:
    """Runs estimate_bootstrap_time() and prints a human-readable summary."""
    est = estimate_bootstrap_time(source_parquet, sample_rows=sample_rows)
    print(
        f"Sampled {est['sample_rows']:,} of {est['total_rows']:,} rows in "
        f"{est['sample_seconds']:.2f}s ({est['rows_per_second']:,.0f} rows/sec)."
    )
    print(f"Estimated time for the full file: ~{_format_duration(est['estimated_total_seconds'])}")
    return est


def bootstrap_vbio(
    source_parquet: str,
    output_parquet: str,
    default_status: str = STATUS_ACTIVE,
    default_credibility_score: float = 2.0,
    default_hmm_posterior: float = 0.9,
    memory_limit: str = "4GB",
    batch_size: int = 100_000,
    show_progress: bool = True,
) -> int:
    """
    Reads the existing vin2plate parquet export, collapses it down to one row per unique
    (Plate, PlateState, Make, Model), and streams the output to a new VBIo parquet file
    in memory-safe chunks:
      - VehicleYear is dropped as a stored column.
      - ObservationCount seeds from the number of VehicleYear rows collapsed per group.
      - FirstSeen / LastSeen both seed from the group's latest (max) VehicleYear,
        represented as Jan 1 of that year.
      - CredibilityScore / HmmPosterior / Status seed from the given defaults
        (2.0 / 0.9 / ACTIVE) -- the live pipeline should update these as real
        detections accumulate.

    :param show_progress: if True, prints duckdb's live progress bar during the
        scan+aggregate step (the expensive part -- it fully consumes the source before
        any output rows exist, so this is where visibility matters most on a large
        file), then a final elapsed-time/throughput summary once writing finishes.
    :return: number of rows written (i.e. number of unique Plate+PlateState+Make+Model groups).
    """
    if default_status not in VALID_STATUSES:
        raise ValueError(f"default_status must be one of {VALID_STATUSES}, got {default_status!r}")

    con = duckdb.connect()

    if memory_limit:
        con.execute(f"SET memory_limit = '{memory_limit}';")
        con.execute("SET preserve_insertion_order = false;")

    if show_progress:
        con.execute("PRAGMA enable_progress_bar")
        con.execute("SET enable_progress_bar_print = true")
        con.execute("SET progress_bar_time = 500")  # show the bar after 0.5s, not duckdb's 2s default

    query = f"""
        SELECT
            ROW_NUMBER() OVER (ORDER BY PlateNumber, PlateState, Make, Model) AS Id,
            PlateNumber,
            PlateState,
            Make,
            Model,
            CAST(MAKE_DATE(CAST(LatestVehicleYear AS INTEGER), 1, 1) AS TIMESTAMP) AS FirstSeen,
            CAST(MAKE_DATE(CAST(LatestVehicleYear AS INTEGER), 1, 1) AS TIMESTAMP) AS LastSeen,
            CAST(YearRowCount AS INTEGER) AS ObservationCount,
            CAST(? AS DOUBLE) AS CredibilityScore,
            CAST(? AS DOUBLE) AS HmmPosterior,
            CAST(? AS VARCHAR) AS Status
        FROM (
            SELECT
                Plate AS PlateNumber,
                PlateState,
                VehicleMake AS Make,
                VehicleModel AS Model,
                MAX(TRY_CAST(VehicleYear AS INTEGER)) AS LatestVehicleYear,
                COUNT(*) AS YearRowCount
            FROM '{source_parquet}'
            GROUP BY Plate, PlateState, VehicleMake, VehicleModel
        )
    """

    overall_start = time.time()

    # This is the expensive step: duckdb must fully scan + hash-aggregate the source
    # before any output row exists, which is exactly what the progress bar tracks.
    result = con.execute(query, [
        default_credibility_score, default_hmm_posterior, default_status,
    ])
    query_elapsed = time.time() - overall_start

    schema = build_vbio_schema()
    # NOTE: duckdb's Arrow batch-streaming API has changed names across versions
    # (fetch_record_batch(rows_per_batch=...) is now deprecated in favor of
    # to_arrow_reader(batch_size=...)). If this breaks after a duckdb upgrade, check
    # `help(result.to_arrow_reader)` for the current signature.
    batch_reader = result.to_arrow_reader(batch_size=batch_size)

    total_rows = 0
    with pq.ParquetWriter(output_parquet, schema=schema) as writer:
        for batch in batch_reader:
            table_batch = pa.Table.from_batches([batch]).cast(schema)
            writer.write_table(table_batch)
            total_rows += table_batch.num_rows

    con.close()
    overall_elapsed = time.time() - overall_start

    if show_progress:
        print(
            f"\nDone: {total_rows:,} unique Plate+PlateState+Make+Model rows written to "
            f"{output_parquet} in {_format_duration(overall_elapsed)} "
            f"(scan+aggregate: {_format_duration(query_elapsed)}, "
            f"write: {_format_duration(overall_elapsed - query_elapsed)})."
        )

    return total_rows


def describe_vbio(parquet_path: str) -> None:
    """Prints the VBIo schema (field name, type, description) and row count for an existing parquet file."""
    schema = pq.read_schema(parquet_path)
    row_count = pq.ParquetFile(parquet_path).metadata.num_rows

    print(f"VBIo schema: {parquet_path}  ({row_count} rows)\n")
    for field in schema:
        description = (field.metadata or {}).get(b"description", b"").decode("utf-8")
        print(f"  {field.name:<18} {str(field.type):<20} {description}")


def _main():
    parser = argparse.ArgumentParser(description="VBIo parquet schema tools.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_estimate = sub.add_parser("estimate", help="Estimate how long a bootstrap run will take, without running it.")
    p_estimate.add_argument("--source", required=True, help="Path to the source vin2plate parquet file.")
    p_estimate.add_argument("--sample-rows", type=int, default=2_000_000, help="Rows to sample for the estimate.")

    p_bootstrap = sub.add_parser("bootstrap", help="Create a VBIo parquet file from a vin2plate export.")
    p_bootstrap.add_argument("--source", required=True, help="Path to the source vin2plate parquet file.")
    p_bootstrap.add_argument("--out", required=True, help="Path to write the new VBIo parquet file.")
    p_bootstrap.add_argument("--status", default=STATUS_ACTIVE, choices=VALID_STATUSES)
    p_bootstrap.add_argument("--score", type=float, default=2.0, help="Default CredibilityScore.")
    p_bootstrap.add_argument("--hmm-posterior", type=float, default=0.9, help="Default HmmPosterior.")
    p_bootstrap.add_argument("--memory-limit", default="4GB", help="DuckDB memory limit (e.g. 4GB, 8GB).")
    p_bootstrap.add_argument("--no-progress", action="store_true", help="Disable the live progress bar/timing summary.")
    p_bootstrap.add_argument(
        "--estimate-first", action="store_true",
        help="Print a rough ETA (sampled throughput extrapolated to the full file) before starting.",
    )
    p_bootstrap.add_argument("--sample-rows", type=int, default=2_000_000, help="Sample size for --estimate-first.")

    p_describe = sub.add_parser("describe", help="Print the schema of an existing VBIo parquet file.")
    p_describe.add_argument("--parquet", required=True, help="Path to a VBIo parquet file.")

    args = parser.parse_args()

    if args.command == "estimate":
        print_bootstrap_estimate(args.source, sample_rows=args.sample_rows)
    elif args.command == "bootstrap":
        if args.estimate_first:
            print_bootstrap_estimate(args.source, sample_rows=args.sample_rows)
            print()
        n = bootstrap_vbio(
            source_parquet=args.source,
            output_parquet=args.out,
            default_status=args.status,
            default_credibility_score=args.score,
            default_hmm_posterior=args.hmm_posterior,
            memory_limit=args.memory_limit,
            show_progress=not args.no_progress,
        )
        print(f"Wrote {n} rows to {args.out}")
    elif args.command == "describe":
        describe_vbio(args.parquet)


if __name__ == "__main__":
    _main()

