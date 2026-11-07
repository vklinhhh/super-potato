"""
vehicle_lookup_service.py

End-to-end pipeline:
    image(s) -> MCAP API (vehicle/plate detection) -> parse plate + plate state
             -> VBIo parquet lookup (raw candidates) -> enrichment engine (tiered result)

Wires together vbio.clients.mcap_client (MCAPClient, parse_vehicles),
vbio.clients.query_vbio (VBIoClient), and vbio.enrichment.engine (EnrichmentEngine).

By default, everything runs against a disposable InMemoryAssociationStore -- nothing
persists past one call, and the VBIo parquet file itself is never written to. Pass a
real EnrichmentEngine (backed by RedisAssociationStore) to persist state across calls
instead -- see build_redis_client_for_env() in vbio.clients.redis_connection.

Usage (preview mode, no Redis, nothing persisted):
    from vbio.clients.mcap_client import MCAPClient
    from vbio.clients.query_vbio import VBIoClient
    from vbio.vehicle_lookup_service import VehicleLookupService

    service = VehicleLookupService(
        mcap_client=MCAPClient(config_path="default.json", env="dev", token="YOUR_TOKEN"),
        vbio_client=VBIoClient("./data/vbio.parquet"),
    )
    results = service.lookup_from_image("test.jpg")
    print(results)

Usage (persistent, backed by a real Redis environment):
    from vbio.clients.redis_connection import build_redis_client_for_env
    from vbio.clients.redis_store import RedisAssociationStore
    from vbio.enrichment.engine import EnrichmentEngine

    engine = EnrichmentEngine(RedisAssociationStore(build_redis_client_for_env(env="dev")))
    service = VehicleLookupService(mcap_client=..., vbio_client=..., engine=engine)

Or from the command line (run from the poc/ root so the `vbio` package resolves):
    python -m vbio.vehicle_lookup_service test.jpg --env dev --parquet ./data/vbio.parquet
    python -m vbio.vehicle_lookup_service test.jpg --env dev --parquet ./data/vbio.parquet --use-redis

Or via the thin wrapper, which works from any directory:
    python scripts/run_lookup.py test.jpg --env dev --parquet ./data/vbio.parquet
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2

# Makes `from vbio.clients... import ...` resolve even when this file is run directly
# (e.g. `python vbio/vehicle_lookup_service.py ...`) rather than via `python -m`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vbio.clients.mcap_client import MCAPClient, MCAPRequestError, parse_vehicles
from vbio.clients.query_vbio import VBIoClient
from vbio.clients.redis_store import InMemoryAssociationStore
from vbio.enrichment.aliases import MakeModelAliases
from vbio.enrichment.engine import Detection, EnrichmentEngine, EnrichmentResult
from vbio.enrichment.keys import normalize_model_key
from vbio.plate_matching.matcher import match as match_plate_to_vehicle


class VehicleLookupService:
    """
    Chains an MCAP image analysis with a VBIo plate/state lookup and the enrichment engine:
      1. Send image(s) to MCAP -> get detected vehicles (make/model/color/plate/...).
      2. For each detected vehicle with a readable plate, query VBIo by plate (+ state)
         for the raw candidate list.
      3. Run that detection through EnrichmentEngine -- decay, Bayes posterior, tiering --
         seeded from the VBIo candidates, to get a single ranked/tiered answer instead of
         an unranked list.

    By default (engine=None), a fresh disposable EnrichmentEngine backed by an
    InMemoryAssociationStore is built for EVERY call -- nothing persists, and repeated
    calls for the same plate never build on each other. Pass a real `engine`
    (RedisAssociationStore-backed) to get actual persistence across calls.
    """

    def __init__(
        self,
        mcap_client: MCAPClient,
        vbio_client: VBIoClient,
        engine: Optional[EnrichmentEngine] = None,
        aliases: Optional[MakeModelAliases] = None,
    ):
        self.mcap_client = mcap_client
        self.vbio_client = vbio_client
        self.engine = engine  # None => build a disposable in-memory one per detection
        # Only used when building that disposable fallback engine, and to canonicalize
        # top-k keys below -- an injected `engine` already carries its own aliases.
        self.aliases = aliases

    def lookup_from_image(
        self,
        image_path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Run the full pipeline for a single image.

        :return: list of dicts, one per vehicle MCAP detected in the image:
            {
                "mcap": {...parsed vehicle fields from parse_vehicles...},
                "vbio_query": {"plate": ..., "plateState": ...} or None if no plate found,
                "vbio_matches": [...raw VBIo records, unranked...],
                "enrichment": {"tier": ..., "make": ..., "model": ..., "confidence": ..., "candidates": [...]}
            }
        """
        try:
            mcap_result = self.mcap_client.analyze_image(image_path, payload=payload)
        except MCAPRequestError as exc:
            return [{
                "mcap": None,
                "vbio_query": None,
                "vbio_matches": [],
                "enrichment": None,
                "error": f"MCAP request failed: {exc}",
            }]

        vehicles = parse_vehicles(mcap_result)
        if not vehicles:
            return [{
                "mcap": None,
                "vbio_query": None,
                "vbio_matches": [],
                "enrichment": None,
                "error": "No vehicles detected in image.",
            }]

        return [self._resolve_vehicle(v) for v in vehicles]

    def lookup_from_images(
        self,
        image_paths: List[str],
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Run the pipeline separately for each image path; keeps results grouped by image."""
        return {path: self.lookup_from_image(path, payload=payload) for path in image_paths}

    def lookup_from_components(
        self,
        plate_number: str,
        plate_state: str,
        plate_image_path: str,
        overview_image_path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Resolve one explicitly keyed plate observation from its crop and overview image.

        MCAP receives only ``overview_image_path``. The supplied plate number/state are
        authoritative for VBIo lookup and Redis scoring; MCAP plate OCR is evidence used
        solely to identify which detected vehicle owns the crop.
        """
        try:
            mcap_result = self.mcap_client.analyze_image(overview_image_path, payload=payload)
        except MCAPRequestError as exc:
            return self._component_error(plate_number, plate_state, f"MCAP request failed: {exc}")

        overview_image = cv2.imread(overview_image_path)
        plate_image = cv2.imread(plate_image_path)
        if overview_image is None:
            return self._component_error(plate_number, plate_state, f"Could not read overview image: {overview_image_path}")
        if plate_image is None:
            return self._component_error(plate_number, plate_state, f"Could not read plate image: {plate_image_path}")

        plate_match = match_plate_to_vehicle(
            overview_image,
            plate_image,
            mcap_result,
            target_text=plate_number,
            whole_image_fallback=True,
        )
        vehicle_id = plate_match["vehicle_id"]
        if vehicle_id is None:
            return {
                **self._component_error(plate_number, plate_state, "Plate could not be matched to a detected vehicle."),
                "mcap": None,
                "plate_match": plate_match,
            }

        vehicles = parse_vehicles(mcap_result)
        if plate_match["phase"] == "whole_image_fallback":
            vehicle = {
                "make": None,
                "model": None,
                "makeModel": "Whole image (MCAP vehicle unavailable)",
                "makeModelConfidence": None,
                "makeModelTopK": [],
                "boundingBox": {"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0},
            }
        elif vehicle_id >= len(vehicles):
            return {
                **self._component_error(plate_number, plate_state, f"Matched vehicle index {vehicle_id} is absent from parsed MCAP output."),
                "mcap": None,
                "plate_match": plate_match,
            }
        else:
            vehicle = vehicles[vehicle_id]
        matches = self.vbio_client.lookup(plate_number, plate_state)
        return {
            "key": {"plate": plate_number, "plateState": plate_state},
            "mcap": vehicle,
            "plate_match": plate_match,
            "vbio_query": {"plate": plate_number, "plateState": plate_state},
            "vbio_matches": matches,
            "enrichment": self._run_enrichment(vehicle, plate_number, plate_state),
            "error": None,
        }

    @staticmethod
    def _component_error(plate_number: str, plate_state: str, error: str) -> Dict[str, Any]:
        return {
            "key": {"plate": plate_number, "plateState": plate_state},
            "mcap": None,
            "plate_match": None,
            "vbio_query": None,
            "vbio_matches": [],
            "enrichment": None,
            "error": error,
        }

    def _resolve_vehicle(self, vehicle: Dict[str, Any]) -> Dict[str, Any]:
        plate = vehicle.get("plateText")
        state = vehicle.get("plateState")

        if not plate:
            return {"mcap": vehicle, "vbio_query": None, "vbio_matches": [], "enrichment": None}

        if state:
            matches = self.vbio_client.lookup(plate, state)
            vbio_query = {"plate": plate, "plateState": state}
        else:
            # No state read off the plate: fall back to a plate-only search across all states.
            matches = self.vbio_client.lookup_plate_only(plate)
            vbio_query = {"plate": plate, "plateState": None}

        enrichment = None
        if state:
            enrichment = self._run_enrichment(vehicle, plate, state)

        return {"mcap": vehicle, "vbio_query": vbio_query, "vbio_matches": matches, "enrichment": enrichment}

    def _run_enrichment(self, vehicle: Dict[str, Any], plate: str, state: str) -> Dict[str, Any]:
        """
        Runs this single detection through the enrichment engine, seeded from VBIo.
        Uses self.engine if one was injected (e.g. Redis-backed, persists across calls);
        otherwise builds a disposable in-memory one for just this call (nothing persists,
        and the VBIo parquet file itself is never written to either way).
        """
        engine = self.engine or EnrichmentEngine(InMemoryAssociationStore(), aliases=self.aliases)
        top_k_confidences = {}
        for row in vehicle.get("makeModelTopK") or []:
            if not isinstance(row, dict):
                continue
            conf = row.get("confidence")
            if conf is None:
                continue
            row_make, row_model = row.get("make"), row.get("model")
            if self.aliases is not None:
                row_make, row_model = self.aliases.canonicalize(row_make, row_model)
            key = normalize_model_key(row_make, row_model)
            if key == "_":
                continue
            try:
                conf_f = float(conf)
            except (TypeError, ValueError):
                continue
            top_k_confidences[key] = max(top_k_confidences.get(key, float("-inf")), conf_f)

        detection = Detection(
            plate_number=plate,
            plate_state=state,
            make=vehicle.get("make"),
            model=vehicle.get("model"),
            confidence=vehicle.get("makeModelConfidence"),
            top_k_confidences=top_k_confidences,
            timestamp=datetime.now(timezone.utc),
        )
        result: EnrichmentResult = engine.process_detection(detection, vbio_seed_fn=self.vbio_client.lookup)

        return {
            "tier": result.tier,
            "make": result.make,
            "model": result.model,
            "confidence": result.confidence,
            "candidates": [
                {
                    "make": c["make"],
                    "model": c["model"],
                    "score": round(c["score"], 4),
                    "posterior": round(c["posterior"], 4),
                    "observationCount": c["observationCount"],
                    "hmmPosterior": c.get("hmmPosterior"),
                    "status": c.get("status"),
                }
                for c in sorted(result.candidates, key=lambda r: -r["score"])
            ],
        }


def _main():
    parser = argparse.ArgumentParser(description="MCAP -> plate/state -> VBIo lookup -> enrichment pipeline.")
    parser.add_argument("images", nargs="+", help="Overview image path(s) to analyze")
    parser.add_argument("--plate-number", help="Authoritative plate number for component matching")
    parser.add_argument("--plate-state", help="Authoritative plate state for component matching")
    parser.add_argument("--plate-image", help="Plate crop image for component matching")
    parser.add_argument("--config", default="default.json", help="Path to MCAP/Redis config json")
    parser.add_argument("--env", default="dev", choices=["dev", "stage", "prod"], help="Environment (MCAP + Redis)")
    parser.add_argument("--token", default=None, help="MCAP bearer token (else MCAP_TOKEN env var or config)")
    parser.add_argument(
        "--parquet",
        default="./data/vbio.parquet",
        help="Path to the VBIo parquet file",
    )
    parser.add_argument(
        "--use-redis", action="store_true",
        help="Persist enrichment state to the Redis instance configured for --env, instead of "
             "the default disposable in-memory store (which forgets everything after this run).",
    )
    parser.add_argument(
        "--redis-only", action="store_true",
        help="Read Redis candidates first; seed a cache miss from the local VBIo parquet.",
    )
    parser.add_argument(
        "--mapping", default=None,
        help="Optional path to make_model_mapping.json -- resolves VBIo/MCAP spelling "
             "variants (e.g. 'Supra' vs 'GR Supra') to one canonical candidate before "
             "the enrichment engine keys them. See assets/make_model_mapping.json.",
    )
    parser.add_argument("--out", default=None, help="Optional path to save the JSON result")
    args = parser.parse_args()

    if args.redis_only and not args.use_redis:
        parser.error("--redis-only requires --use-redis")

    aliases = None
    if args.mapping:
        from vbio.clients.make_model_mapping import load_make_model_aliases
        aliases = load_make_model_aliases(args.mapping)

    engine = None
    vbio_client = VBIoClient(args.parquet)
    if args.use_redis:
        from vbio.clients.redis_connection import build_redis_client_for_env
        from vbio.clients.redis_store import RedisAssociationStore
        redis_client = build_redis_client_for_env(env=args.env, config_path=args.config)
        redis_store = RedisAssociationStore(redis_client)
        engine = EnrichmentEngine(redis_store, aliases=aliases)
        if args.redis_only:
            from vbio.clients.redis_vbio import RedisVBIoClient
            vbio_client = RedisVBIoClient(redis_store, cold_start_client=vbio_client)

    service = VehicleLookupService(
        mcap_client=MCAPClient(config_path=args.config, env=args.env, token=args.token),
        vbio_client=vbio_client,
        engine=engine,
        aliases=aliases,
    )

    component_args = (args.plate_number, args.plate_state, args.plate_image)
    if any(component_args):
        if not all(component_args):
            parser.error("--plate-number, --plate-state, and --plate-image must be supplied together")
        if len(args.images) != 1:
            parser.error("component matching accepts exactly one overview image")
        result = service.lookup_from_components(
            args.plate_number, args.plate_state, args.plate_image, args.images[0]
        )
    elif len(args.images) == 1:
        result = service.lookup_from_image(args.images[0])
    else:
        result = service.lookup_from_images(args.images)

    print(json.dumps(result, indent=2, default=str))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    _main()
