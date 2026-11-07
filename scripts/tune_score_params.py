"""
tune_score_params.py

Sweeps EnrichmentConfig.alpha (EMA weight) and .seed_base (VBIo seed strength) over a
grid, re-running the full test_data accuracy harness for each combination, and reports
the best one. Use this to RE-tune these two values whenever test_data grows -- they
were last tuned against a 97-profile/4506-record set (see config.py's docstring and
ENGINE_LOGIC_FLOW.md section 5.3/8); a materially bigger or different test set may move
the optimum.

This does NOT modify any file. It only prints a ranked table -- if you want to adopt a
new best combo, update EnrichmentConfig.alpha/seed_base in vbio/enrichment/config.py by
hand (and re-run tests/integration/test_redis_scoring_slides.py's expected numbers,
which embed the current alpha/seed_base -- see that file's module docstring).

Usage:
    python scripts/tune_score_params.py \
        --test-data "D:/VBIO/test_data" \
        --vbio-parquet "D:/VBIO/vbio/vbio.parquet" \
        --mapping assets/make_model_mapping.json \
        --alpha 0.15,0.2,0.25,0.3,0.35,0.4 \
        --seed-base 0.4,0.5,0.6,0.7,0.8,0.9,1.0 \
        --top 10
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
    """Full accuracy harness for one EnrichmentConfig. Returns (accuracy_pct, correct, total)."""
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
        "--alpha", default="0.15,0.2,0.25,0.3,0.35,0.4",
        help="Comma-separated alpha values to sweep.",
    )
    parser.add_argument(
        "--seed-base", default="0.4,0.5,0.6,0.7,0.8,0.9,1.0",
        help="Comma-separated seed_base values to sweep.",
    )
    parser.add_argument("--top", type=int, default=15, help="How many top results to print.")
    args = parser.parse_args()

    alphas = parse_float_list(args.alpha)
    seed_bases = parse_float_list(args.seed_base)

    aliases = load_make_model_aliases(args.mapping)
    profiles = load_profiles(args.test_data)
    print(f"Loaded {len(profiles)} profiles.")

    pairs = {(rows[0]["plate"], rows[0]["stateName"]) for _, rows in profiles}
    print(f"Preloading VBIo seeds for {len(pairs)} plate/state pairs ...")
    all_seeds = preload_vbio_seeds(args.vbio_parquet, pairs)

    current_acc, current_correct, current_total = run_accuracy(DEFAULT_CONFIG, profiles, all_seeds, aliases)
    print(
        f"\nCurrent config (alpha={DEFAULT_CONFIG.alpha}, seed_base={DEFAULT_CONFIG.seed_base}): "
        f"{current_correct}/{current_total} = {current_acc:.2f}%\n"
    )

    results = []
    total_runs = len(alphas) * len(seed_bases)
    done = 0
    for alpha in alphas:
        for seed_base in seed_bases:
            cfg = replace(DEFAULT_CONFIG, alpha=alpha, seed_base=seed_base)
            acc, correct, total = run_accuracy(cfg, profiles, all_seeds, aliases)
            results.append((acc, alpha, seed_base, correct, total))
            done += 1
            print(f"\r[{done}/{total_runs}] alpha={alpha} seed_base={seed_base} -> {acc:.2f}%" + " " * 10, end="")

    print()
    results.sort(key=lambda r: -r[0])

    print(f"\n{'alpha':>6} {'seed_base':>10} {'correct':>8} {'total':>7} {'accuracy':>10}")
    for acc, alpha, seed_base, correct, total in results[: args.top]:
        marker = " <-- current" if (alpha == DEFAULT_CONFIG.alpha and seed_base == DEFAULT_CONFIG.seed_base) else ""
        print(f"{alpha:>6} {seed_base:>10} {correct:>8} {total:>7} {acc:>9.2f}%{marker}")

    best_acc, best_alpha, best_seed_base, _, _ = results[0]
    print(
        f"\nBest: alpha={best_alpha}, seed_base={best_seed_base} -> {best_acc:.2f}% "
        f"(current config: {current_acc:.2f}%)"
    )
    if best_acc > current_acc + 0.05:
        print(
            "To adopt: update EnrichmentConfig.alpha/seed_base in vbio/enrichment/config.py, "
            "then recompute tests/integration/test_redis_scoring_slides.py's expected numbers "
            "for the new values (see that file's module docstring)."
        )
    else:
        print("Current config is already at/near the best found in this sweep -- no change needed.")


if __name__ == "__main__":
    main()
