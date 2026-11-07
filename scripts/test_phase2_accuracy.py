"""
test_phase2_accuracy.py

Chạy lại EnrichmentEngine (Phase 2 - decay/malus/Bayes HMM, xem vbio/enrichment/)
trên toàn bộ profile trong test_data, ghi đè 4 cột hmmMake/hmmModel/hmmConfidence/
hmmTier trong từng CSV, rồi in ra accuracy tổng (so khớp Make+Model với gtMake/gtModel
sau khi normalize theo make_model_mapping.json).

Trục thời gian: mỗi profile (1 plate+state) được xử lý tuần tự theo gmtDateTime tăng
dần. Khi "cold start" (nạp candidate ban đầu từ vbio.parquet cho 1 plate+state chưa
từng thấy), CHỈ những dòng VBIo có LastSeen <= gmtDateTime của detection đầu tiên
trong CSV mới được dùng -- các đăng ký "trong tương lai" so với mốc thời gian test bị
loại bỏ, tránh rò rỉ thông tin tương lai vào thuật toán.

Usage (chạy từ gốc repo super-potato):
    python scripts/test_phase2_accuracy.py \
        --test-data "D:/VBIO/test_data" \
        --vbio-parquet "D:/VBIO/vbio/vbio.parquet" \
        --mapping assets/make_model_mapping.json

Thêm --dry-run để chỉ in accuracy, không ghi đè CSV.
"""
import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vbio.clients.make_model_mapping import load_make_model_aliases
from vbio.clients.redis_store import InMemoryAssociationStore
from vbio.enrichment.config import DEFAULT_CONFIG
from vbio.enrichment.engine import Detection, EnrichmentEngine

CSV_FIELDS = [
    "eventId", "imageCar", "imagePlate", "plate", "stateName", "vehicleMake",
    "vehicleModel", "gmtDateTime", "gtMake", "gtModel", "visual_complexity",
    "freshMcapMake", "freshMcapModel", "freshMcapConfidence", "freshMcapVehicleCount",
    "hmmMake", "hmmModel", "hmmConfidence", "hmmTier",
]


def parse_dt(value):
    return datetime.fromisoformat(value)


def clean_text(value):
    value = (value or "").strip()
    if not value or value.lower() == "unknown":
        return None
    return value


def load_profiles(test_data_dir):
    """One (name, csv_path, rows) per profile folder, rows sorted by gmtDateTime asc."""
    profiles = []
    for folder in sorted(Path(test_data_dir).iterdir()):
        if not folder.is_dir():
            continue
        csvs = list(folder.glob("*.csv"))
        if not csvs:
            continue
        csv_path = csvs[0]
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            continue
        rows.sort(key=lambda r: parse_dt(r["gmtDateTime"]))
        profiles.append((folder.name, csv_path, rows))
    return profiles


def preload_vbio_seeds(parquet_path, plate_state_pairs):
    """Single pass over the (large) VBIo parquet for every (plate, state) seen in
    test_data, instead of one query per profile. Returns dict:
    (PLATE_UPPER, STATE_UPPER) -> list of seed dicts sorted by LastSeen ascending."""
    con = duckdb.connect()
    con.execute("CREATE TEMP TABLE wanted(PlateNumber VARCHAR, PlateState VARCHAR)")
    con.executemany(
        "INSERT INTO wanted VALUES (?, ?)",
        [(p.upper(), s.upper()) for p, s in plate_state_pairs],
    )
    rows = con.execute(
        f"""
        SELECT v.PlateNumber, v.PlateState, v.Make, v.Model, v.FirstSeen, v.LastSeen,
               v.ObservationCount, v.HmmPosterior, v.Status
        FROM '{parquet_path}' v
        JOIN wanted w
          ON UPPER(v.PlateNumber) = w.PlateNumber AND UPPER(v.PlateState) = w.PlateState
        """
    ).fetchall()
    con.close()

    seeds = {}
    for plate, state, make, model, first_seen, last_seen, obs_count, hmm_post, status in rows:
        key = (plate.upper(), state.upper())
        seeds.setdefault(key, []).append({
            "Make": make, "Model": model, "FirstSeen": first_seen, "LastSeen": last_seen,
            "ObservationCount": obs_count, "HmmPosterior": hmm_post, "Status": status,
        })
    for key, records in seeds.items():
        records.sort(key=lambda r: r["LastSeen"] or datetime.min)
    return seeds


def make_seed_fn(all_seeds, as_of_holder):
    """vbio_seed_fn for EnrichmentEngine.process_detection(): only admits VBIo rows
    whose LastSeen is at or before the CURRENT detection's timestamp (as_of_holder is
    updated to that timestamp before every process_detection call) -- future DMV
    registrations relative to the test record being evaluated are never visible to
    the engine."""
    def seed_fn(plate_number, plate_state):
        key = (plate_number.upper(), plate_state.upper())
        as_of = as_of_holder["ts"]
        return [
            r for r in all_seeds.get(key, [])
            if (r["LastSeen"] or datetime.min) <= as_of
        ]
    return seed_fn


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-data", default="D:/VBIO/test_data")
    parser.add_argument("--vbio-parquet", default="D:/VBIO/vbio/vbio.parquet")
    parser.add_argument("--mapping", default="assets/make_model_mapping.json")
    parser.add_argument("--dry-run", action="store_true", help="Don't overwrite CSV files, just print accuracy.")
    args = parser.parse_args()

    aliases = load_make_model_aliases(args.mapping)
    profiles = load_profiles(args.test_data)
    print(f"Loaded {len(profiles)} profiles.")

    pairs = {(rows[0]["plate"], rows[0]["stateName"]) for _, _, rows in profiles}
    print(f"Preloading VBIo seeds for {len(pairs)} plate/state pairs from {args.vbio_parquet} ...")
    all_seeds = preload_vbio_seeds(args.vbio_parquet, pairs)

    total = 0
    correct = 0
    per_profile_stats = []

    for name, csv_path, rows in profiles:
        store = InMemoryAssociationStore()
        engine = EnrichmentEngine(store, DEFAULT_CONFIG, aliases=aliases)
        as_of_holder = {"ts": None}
        seed_fn = make_seed_fn(all_seeds, as_of_holder)

        profile_total = 0
        profile_correct = 0

        for row in rows:
            ts = parse_dt(row["gmtDateTime"])
            as_of_holder["ts"] = ts

            conf_raw = (row.get("freshMcapConfidence") or "").strip()
            detection = Detection(
                plate_number=row["plate"],
                plate_state=row["stateName"],
                make=clean_text(row.get("freshMcapMake")),
                model=clean_text(row.get("freshMcapModel")),
                confidence=float(conf_raw) if conf_raw else None,
                timestamp=ts,
            )
            result = engine.process_detection(detection, vbio_seed_fn=seed_fn)

            row["hmmMake"] = result.make or ""
            row["hmmModel"] = result.model or ""
            row["hmmConfidence"] = f"{result.confidence:.6f}" if result.confidence is not None else ""
            row["hmmTier"] = result.tier

            gt_make_n, gt_model_n = aliases.canonicalize(row.get("gtMake"), row.get("gtModel"))
            hmm_make_n, hmm_model_n = aliases.canonicalize(result.make, result.model)
            is_match = (
                result.make is not None
                and (hmm_make_n or "").casefold() == (gt_make_n or "").casefold()
                and (hmm_model_n or "").casefold() == (gt_model_n or "").casefold()
            )

            profile_total += 1
            total += 1
            if is_match:
                profile_correct += 1
                correct += 1

        per_profile_stats.append((name, profile_correct, profile_total))

        if not args.dry_run:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                writer.writeheader()
                writer.writerows(rows)

    print()
    print(f"{'Profile':<20} {'Correct':>8} {'Total':>8} {'Accuracy':>10}")
    for name, c, t in per_profile_stats:
        acc = (c / t * 100) if t else 0.0
        print(f"{name:<20} {c:>8} {t:>8} {acc:>9.2f}%")

    overall_acc = (correct / total * 100) if total else 0.0
    print()
    print(f"TOTAL: {correct}/{total} records matched ground truth -> accuracy = {overall_acc:.2f}%")


if __name__ == "__main__":
    run()
