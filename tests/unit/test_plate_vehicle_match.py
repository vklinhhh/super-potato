import numpy as np

from vbio.plate_matching import matcher
from vbio.plate_matching.constants import PLATE_HINT_ACCEPT
from vbio.plate_matching.visual import geometric_score


def _box(x1, y1, x2, y2):
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}


def _mcap_payload(vehicle_attributes, plate_attributes, plate_text="ABC123"):
    return {
        "data": {
            "attributes": {
                "analytics": {
                    "vehicleAnalysis": [{
                        "vehicles": [{
                            "attributes": vehicle_attributes,
                            "boundingBox": _box(0.1, 0.1, 0.9, 0.9),
                        }],
                        "plates": [{
                            "vehicleId": 0,
                            "boundingBox": _box(0.4, 0.5, 0.6, 0.6),
                            "attributes": {"text": plate_text, **plate_attributes},
                        }],
                    }]
                }
            }
        }
    }


def test_ocr_phase_returns_linked_vehicle_and_normalizes_missing_label():
    result = matcher.match(
        main_img=None,
        ir_crop=None,
        json_data=_mcap_payload({"makeModel": None}, {}),
        target_text="abc-123",
    )

    assert result["vehicle_id"] == 0
    assert result["phase"] == "ocr"
    assert result["confidence"] == 1.0
    assert result["vehicles"][0]["label"] == "?"


def test_visual_phase_accepts_strong_detected_plate_match(monkeypatch):
    monkeypatch.setattr(matcher, "plate_crop_similarity", lambda *_: 0.70)
    image = np.zeros((100, 100, 3), dtype=np.uint8)

    result = matcher.match(
        main_img=image,
        ir_crop=image,
        json_data=_mcap_payload({"makeModel": "Toyota, Camry"}, {}),
        force_phase="visual",
    )

    assert result["vehicle_id"] == 0
    assert result["phase"] == "plate_visual"
    assert result["confidence"] == 0.70


def test_visual_phase_uses_detector_hint_when_negative_ncc_has_text_evidence(monkeypatch):
    monkeypatch.setattr(matcher, "plate_crop_similarity", lambda *_: -0.08)
    image = np.zeros((100, 100, 3), dtype=np.uint8)

    result = matcher.match(
        main_img=image,
        ir_crop=image,
        json_data=_mcap_payload({"makeModel": "Toyota, Camry"}, {}, plate_text="ABCDEF"),
        target_text="ABCDEX",
        force_phase="visual",
    )

    assert result["vehicle_id"] == 0
    assert result["phase"] == "plate_hint"
    assert result["confidence"] >= PLATE_HINT_ACCEPT
    assert "Weak visual evidence" in result["warnings"][-1]


def test_invalid_mcap_payload_returns_no_match_with_diagnostic():
    result = matcher.match(main_img=None, ir_crop=None, json_data={})

    assert result["vehicle_id"] is None
    assert result["phase"] is None
    assert result["warnings"] == ["No vehicleAnalysis in JSON"]


def test_whole_image_fallback_returns_virtual_vehicle_when_mcap_detects_none():
    payload = {
        "data": {
            "attributes": {
                "analytics": {
                    "vehicleAnalysis": [{"vehicles": [], "plates": []}]
                }
            }
        }
    }

    result = matcher.match(None, None, payload, whole_image_fallback=True)

    assert result["vehicle_id"] == 0
    assert result["phase"] == "whole_image_fallback"
    assert result["vehicles"][0]["box"] == (0.0, 0.0, 1.0, 1.0)


def test_geometric_score_favors_a_centered_plate_over_an_edge_plate():
    vehicle = (0.1, 0.1, 0.9, 0.9)
    centered_score, _ = geometric_score((0.4, 0.46, 0.6, 0.56), vehicle)
    edge_score, _ = geometric_score((0.1, 0.46, 0.3, 0.56), vehicle)

    assert centered_score > edge_score
    assert matcher.containment((0.4, 0.46, 0.6, 0.56), vehicle) == 1.0
