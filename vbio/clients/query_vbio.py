"""
query_vbio.py

Look up a vehicle in the VBIo parquet store by plate number and plate state.
(VBIo is the VBIo-shaped enrichment dataset -- see vbio.schema.vbio_schema -- built
from the raw vin2plate export; that raw export uses a "Plate" column, VBIo renames it
to "PlateNumber". This client targets the VBIo schema.)

Usage:
    from vbio.clients.query_vbio import VBIoClient

    client = VBIoClient("./data/vbio.parquet")
    results = client.lookup("098MNB", "OR")
    print(results)

Or from the command line:
    python -m vbio.clients.query_vbio 098MNB OR
    python -m vbio.clients.query_vbio 098MNB OR --parquet ./data/vbio.parquet
"""

import argparse
import json

import duckdb


class VBIoClient:
    """Queries the VBIo parquet store for vehicle records matching a plate + state."""

    def __init__(self, parquet_file: str = "./data/vbio.parquet"):
        self.parquet_file = parquet_file

    def lookup(self, plate: str, plate_state: str):
        """
        Look up all VBIo records matching a plate number and plate state (case-insensitive).

        :param plate: license plate text, e.g. "098MNB".
        :param plate_state: two-letter state code, e.g. "OR".
        :return: list of dict records (empty list if no match).
        """
        query = f"""
            SELECT *
            FROM '{self.parquet_file}'
            WHERE UPPER(PlateNumber) = UPPER(?)
              AND UPPER(PlateState) = UPPER(?)
        """
        df = duckdb.query(query, params=[plate, plate_state]).df()
        return df.to_dict(orient="records")

    def lookup_plate_only(self, plate: str):
        """Look up all VBIo records matching a plate number, across all states."""
        query = f"""
            SELECT *
            FROM '{self.parquet_file}'
            WHERE UPPER(PlateNumber) = UPPER(?)
        """
        df = duckdb.query(query, params=[plate]).df()
        return df.to_dict(orient="records")


def _main():
    parser = argparse.ArgumentParser(description="Query the VBIo parquet store by plate + state.")
    parser.add_argument("plate", help="License plate text, e.g. 098MNB")
    parser.add_argument("plate_state", nargs="?", default=None, help="Two-letter plate state, e.g. OR (omit to search all states)")
    parser.add_argument(
        "--parquet",
        default="./data/vbio.parquet",
        help="Path to the VBIo parquet file",
    )
    args = parser.parse_args()

    client = VBIoClient(args.parquet)

    if args.plate_state:
        results = client.lookup(args.plate, args.plate_state)
    else:
        results = client.lookup_plate_only(args.plate)

    print(json.dumps(results, indent=2, default=str))
    print(f"\n{len(results)} record(s) found.")


if __name__ == "__main__":
    _main()
