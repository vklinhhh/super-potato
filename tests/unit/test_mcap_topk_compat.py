import sys
import types
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# vehicle_lookup_service imports query_vbio, which imports duckdb at module-load time.
# Stub it so this unit test can run in minimal environments.
if "duckdb" not in sys.modules:
    sys.modules["duckdb"] = types.ModuleType("duckdb")

from vbio.clients.mcap_client import parse_vehicles
from vbio.enrichment.engine import EnrichmentResult
from vbio import vehicle_lookup_service as lookup_module
from vbio.vehicle_lookup_service import VehicleLookupService


def _response_with_vehicle(attrs):
    return {
        "data": {
            "attributes": {
                "analytics": {
                    "vehicleAnalysis": [{
                        "vehicles": [{"attributes": attrs, "details": [], "boundingBox": None}],
                        "plates": [{"vehicleId": 0, "attributes": {"text": "ABC123", "state": "NY"}}],
                    }]
                }
            }
        }
    }


def test_parse_vehicles_keeps_legacy_top1_shape():
    parsed = parse_vehicles(_response_with_vehicle({
        "make": "Hyundai",
        "model": "Sonata",
        "makeModel": "Hyundai, Sonata",
        "rankings": {"makeModel": 0.93},
    }))
    assert len(parsed) == 1
    assert parsed[0]["makeModelConfidence"] == 0.93
    assert parsed[0]["makeModelTopK"] == []


def test_parse_vehicles_accepts_ranked_list_shape():
    parsed = parse_vehicles(_response_with_vehicle({
        "make": "Hyundai",
        "model": "Sonata",
        "makeModel": "Hyundai, Sonata",
        "rankings": {
            "makeModel": [
                {"make": "Hyundai", "model": "Sonata", "confidence": 0.82},
                {"make": "Kia", "model": "K5", "confidence": 0.12},
            ]
        },
    }))
    assert len(parsed) == 1
    assert parsed[0]["makeModelConfidence"] == 0.82
    assert [r["confidence"] for r in parsed[0]["makeModelTopK"]] == [0.82, 0.12]


class _NoopMCAPClient:
    def analyze_image(self, *_args, **_kwargs):
        raise NotImplementedError


class _NoopVBIoClient:
    def lookup(self, *_args, **_kwargs):
        return []


class _CaptureEngine:
    def __init__(self):
        self.last_detection = None

    def process_detection(self, detection, vbio_seed_fn=None):
        self.last_detection = detection
        return EnrichmentResult(
            plate_number=detection.plate_number,
            plate_state=detection.plate_state,
            tier="NONE",
            make=None,
            model=None,
            confidence=None,
            candidates=[],
        )


def test_vehicle_lookup_service_passes_topk_to_enrichment_detection():
    engine = _CaptureEngine()
    service = VehicleLookupService(_NoopMCAPClient(), _NoopVBIoClient(), engine=engine)

    service._run_enrichment(
        vehicle={
            "make": "Hyundai",
            "model": "Sonata",
            "makeModelConfidence": 0.80,
            "makeModelTopK": [
                {"make": "Hyundai", "model": "Sonata", "confidence": 0.80},
                {"make": "Mercedes-Benz", "model": "C Class", "confidence": 0.15},
            ],
        },
        plate="ABC123",
        state="NY",
    )

    assert engine.last_detection is not None
    assert engine.last_detection.top_k_confidences["hyundai_sonata"] == 0.80
    assert engine.last_detection.top_k_confidences["mercedesbenz_cclass"] == 0.15


def test_component_pipeline_matches_plate_to_vehicle_before_enrichment(monkeypatch):
    raw_mcap = {
        "data": {"attributes": {"analytics": {"vehicleAnalysis": [{
            "vehicles": [
                {"attributes": {"make": "Honda", "model": "Accord", "makeModel": "Honda, Accord",
                                "rankings": {"makeModel": 0.91}}, "details": [],
                 "boundingBox": {"x1": 0.05, "y1": 0.1, "x2": 0.45, "y2": 0.9}},
                {"attributes": {"make": "Toyota", "model": "Camry", "makeModel": "Toyota, Camry",
                                "rankings": {"makeModel": 0.95}}, "details": [],
                 "boundingBox": {"x1": 0.55, "y1": 0.1, "x2": 0.95, "y2": 0.9}},
            ],
            "plates": [
                {"vehicleId": 0, "boundingBox": {"x1": 0.15, "y1": 0.5, "x2": 0.3, "y2": 0.6},
                 "attributes": {"text": "OTHER1", "state": "NY"}},
                {"vehicleId": 1, "boundingBox": {"x1": 0.65, "y1": 0.5, "x2": 0.8, "y2": 0.6},
                 "attributes": {"text": "KEY123", "state": "NY"}},
            ],
        }]}}}
    }

    class MCAP:
        def analyze_image(self, image_path, payload=None):
            assert image_path == "overview.jpg"
            return raw_mcap

    class VBIo:
        def __init__(self):
            self.lookups = []

        def lookup(self, plate, state):
            self.lookups.append((plate, state))
            return []

    engine, vbio = _CaptureEngine(), VBIo()
    service = VehicleLookupService(MCAP(), vbio, engine=engine)
    monkeypatch.setattr(lookup_module.cv2, "imread", lambda _: np.zeros((100, 100, 3), dtype=np.uint8))

    result = service.lookup_from_components("KEY123", "CA", "plate.jpg", "overview.jpg")

    assert result["error"] is None
    assert result["plate_match"]["vehicle_id"] == 1
    assert result["mcap"]["makeModel"] == "Toyota, Camry"
    assert result["vbio_query"] == {"plate": "KEY123", "plateState": "CA"}
    assert vbio.lookups == [("KEY123", "CA")]
    assert engine.last_detection.plate_number == "KEY123"
    assert engine.last_detection.plate_state == "CA"
    assert engine.last_detection.make == "Toyota"


def test_component_pipeline_uses_whole_image_when_mcap_detects_no_vehicle(monkeypatch):
    raw_mcap = {"data": {"attributes": {"analytics": {"vehicleAnalysis": [{
        "vehicles": [], "plates": [],
    }]}}}}

    class MCAP:
        def analyze_image(self, *_args, **_kwargs):
            return raw_mcap

    engine = _CaptureEngine()
    service = VehicleLookupService(MCAP(), _NoopVBIoClient(), engine=engine)
    monkeypatch.setattr(lookup_module.cv2, "imread", lambda _: np.zeros((100, 100, 3), dtype=np.uint8))

    result = service.lookup_from_components("KEY123", "CA", "plate.jpg", "overview.jpg")

    assert result["error"] is None
    assert result["plate_match"]["phase"] == "whole_image_fallback"
    assert result["mcap"]["boundingBox"] == {"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}
    assert engine.last_detection.make is None


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
