"""
tune_gc_params.py

Sweeps EnrichmentConfig.gc_days (Redis TTL for stale OBSERVED candidates) and
.n_activate (confirmations needed to promote OBSERVED PENDING -> ACTIVE) over a grid,
re-running the full test_data accuracy harness for each combination via the REAL
EnrichmentEngine (vbio.enrichment.cleanup.gc_and_sync/maybe_promote run exactly as they
do in production -- no monkeypatching/duplicated engine logic). Use this to re-tune
these two values whenever test_data grows; see config.py's docstring and
ENGINE_LOGIC_FLOW.md section 9 for how they were last tuned (180.0 / 3, 97-profile set).

This does NOT modify any file. It only prints a ranked table -- if you want to adopt a
new best combo, update EnrichmentConfig.gc_days/n_activate in vbio/enrichment/config.py
by hand.

Usage:
    python scripts/tune_gc_params.py \
        --test-data "D:/VBIO/test_data" \
        --vbio-parquet "D:/VBIO/vbio/vbio.parquet" \
        --mapping assets/make_model_mapping.json \
        --gc-days 60,90,120,150,180,240,365 \
        --n-activate 1,2,3,4,5
"""
import argparse
import csv
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vbio.clients.make_model_mapping import load_make_model_aliases
from vbio.clients.redis_store import InMemoryAssociationStore
from vbio.enrichment.config import DEFAULT_CONFIG
from vbio.enrichment.engine import Detection, EnrichmentEngine


def parse_dt(value):
    return datetime.fromisoformat(value)


def clean_text(value):
    value = (value or "").strip()
    if not value or value.lower() == "unknown":
        return None
    return value


def parse_float_list(raw):
    return [float(x) for x in raw.split(",") if x.strip()]


def parse_int_list(raw):
    return [int(x) for x in raw.split(",") if x.strip()]


def load_profiles(test_data_dir):
    profiles = []
    for folder in sorted(Path(test_data_dir).iterdir()):
        if not folder.is_dir():
            continue
        csvs = list(folder.glob("*.csv"))
        if not csvs:
            continue
        with open(csvs[0], newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            continue
        rows.sort(key=lambda r: parse_dt(r["gmtDateTime"]))
        profiles.append((folder.name, rows))
    return profiles


def preload_vbio_seeds(parquet_path, plate_state_pairs):
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
    for records in seeds.values():
        records.sort(key=lambda r: r["LastSeen"] or datetime.min)
    return seeds


def make_seed_fn(all_seeds, as_of_holder):
    def seed_fn(plate_number, plate_state):
        key = (plate_number.upper(), plate_state.upper())
        as_of = as_of_holder["ts"]
        return [r for r in all_seeds.get(key, []) if (r["LastSeen"] or datetime.min) <= as_of]
    return seed_fn


def run_accuracy(config, profiles, all_seeds, aliases):
    """Full accuracy harness for one EnrichmentConfig, via the real EnrichmentEngine
    (GC/promotion run exactly as in production). Returns (accuracy_pct, correct, total)."""
    total = 0
    correct = 0

    for _name, rows in profiles:
        store = InMemoryAssociationStore()
        engine = EnrichmentEngine(store, config, aliases=aliases)
        as_of_holder = {"ts": None}
        seed_fn = make_seed_fn(all_seeds, as_of_holder)

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

            gt_make_n, gt_model_n = aliases.canonicalize(row.get("gtMake"), row.get("gtModel"))
            hmm_make_n, hmm_model_n = aliases.canonicalize(result.make, result.model)
            is_match = (
                result.make is not None
                and (hmm_make_n or "").casefold() == (gt_make_n or "").casefold()
                and (hmm_model_n or "").casefold() == (gt_model_n or "").casefold()
            )
            total += 1
            if is_match:
                correct += 1

    return (correct / total * 100 if total else 0.0), correct, total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-data", default="D:/VBIO/test_data")
    parser.add_argument("--vbio-parquet", default="D:/VBIO/vbio/vbio.parquet")
    parser.add_argument("--mapping", default="assets/make_model_mapping.json")
    parser.add_argument(
        "--gc-days", default="60,90,120,150,180,240,365",
        help="Comma-separated gc_days values to sweep.",
    )
    parser.add_argument(
        "--n-activate", default="1,2,3,4,5",
        help="Comma-separated n_activate values to sweep.",
    )
    parser.add_argument("--top", type=int, default=15, help="How many top results to print.")
    args = parser.parse_args()

    gc_days_values = parse_float_list(args.gc_days)
    n_activate_values = parse_int_list(args.n_activate)

    aliases = load_make_model_aliases(args.mapping)
    profiles = load_profiles(args.test_data)
    print(f"Loaded {len(profiles)} profiles.")

    pairs = {(rows[0]["plate"], rows[0]["stateName"]) for _, rows in profiles}
    print(f"Preloading VBIo seeds for {len(pairs)} plate/state pairs ...")
    all_seeds = preload_vbio_seeds(args.vbio_parquet, pairs)

    current_acc, current_correct, current_total = run_accuracy(DEFAULT_CONFIG, profiles, all_seeds, aliases)
    print(
        f"\nCurrent config (gc_days={DEFAULT_CONFIG.gc_days}, n_activate={DEFAULT_CONFIG.n_activate}): "
        f"{current_correct}/{current_total} = {current_acc:.2f}%\n"
    )

    results = []
    total_runs = len(gc_days_values) * len(n_activate_values)
    done = 0
    for gc_days in gc_days_values:
        for n_activate in n_activate_values:
            cfg = replace(DEFAULT_CONFIG, gc_days=gc_days, n_activate=n_activate)
            acc, correct, total = run_accuracy(cfg, profiles, all_seeds, aliases)
            results.append((acc, gc_days, n_activate, correct, total))
            done += 1
            print(f"\r[{done}/{total_runs}] gc_days={gc_days} n_activate={n_activate} -> {acc:.2f}%" + " " * 10, end="")

    print()
    results.sort(key=lambda r: -r[0])

    print(f"\n{'gc_days':>8} {'n_activate':>10} {'correct':>8} {'total':>7} {'accuracy':>10}")
    for acc, gc_days, n_activate, correct, total in results[: args.top]:
        marker = " <-- current" if (gc_days == DEFAULT_CONFIG.gc_days and n_activate == DEFAULT_CONFIG.n_activate) else ""
        print(f"{gc_days:>8} {n_activate:>10} {correct:>8} {total:>7} {acc:>9.2f}%{marker}")

    best_acc, best_gc_days, best_n_activate, _, _ = results[0]
    print(
        f"\nBest: gc_days={best_gc_days}, n_activate={best_n_activate} -> {best_acc:.2f}% "
        f"(current config: {current_acc:.2f}%)"
    )
    if best_acc > current_acc + 0.05:
        print(
            "To adopt: update EnrichmentConfig.gc_days/n_activate in vbio/enrichment/config.py, "
            "then re-run tests/ and update ENGINE_LOGIC_FLOW.md section 9 with the new numbers."
        )
    else:
        print("Current config is already at/near the best found in this sweep -- no change needed.")


if __name__ == "__main__":
    main()
