import re
from difflib import SequenceMatcher

from .constants import GLOBAL_VISUAL_ACCEPT, OCR_STRONG_MATCH, PLATE_HINT_ACCEPT, PLATE_VISUAL_ACCEPT
from .geometry import containment, crop_norm, to_box
from .visual import (assign_plates_to_vehicles, locate_plate_candidates,
                     plate_crop_similarity, score_vehicles_for_plate)


def normalize_text(text):
    return re.sub(r"[^A-Z0-9]", "", str(text).upper()) if text else ""


def text_similarity(left, right):
    left, right = normalize_text(left), normalize_text(right)
    return SequenceMatcher(None, left, right).ratio() if left and right else 0.0


def _new_result():
    return {"vehicle_id": None, "phase": None, "confidence": 0.0, "plates": [], "vehicles": [],
            "ranked": [], "localised": None, "assignment": {}, "warnings": []}


def _analysis(json_data):
    try:
        value = json_data["data"]["attributes"]["analytics"]["vehicleAnalysis"][0]
    except (KeyError, TypeError, IndexError):
        return None
    return value if isinstance(value, dict) else None


def _score_plates(main_img, ir_crop, plates, target):
    result = []
    for index, plate in enumerate(plates):
        attrs, box = plate.get("attributes") or {}, to_box(plate.get("boundingBox"))
        visual = plate_crop_similarity(ir_crop, crop_norm(main_img, box, 0.15)) if box is not None and ir_crop is not None and main_img is not None else None
        text_score = text_similarity(target, attrs.get("text", "")) if target else 0.0
        result.append({"index": index, "box": box, "text": attrs.get("text", ""), "quality": attrs.get("quality"),
                       "vehicle_id": plate.get("vehicleId"), "text_sim": text_score, "visual": visual,
                       "combined": 0.55 * max(0.0, visual or 0.0) + 0.30 * text_score + 0.15 * (plate.get("vehicleId") is not None)})
    return result


def _record_assignments(result, vehicle_boxes):
    result["assignment"] = {str(key): value for key, value in assign_plates_to_vehicles([plate["box"] for plate in result["plates"]], vehicle_boxes).items()}
    for plate in result["plates"]:
        assigned = result["assignment"].get(str(plate["index"]))
        if assigned is not None and plate["vehicle_id"] is not None and assigned != plate["vehicle_id"]:
            result["warnings"].append(f"plate {plate['index']} ({plate['text'] or 'unreadable'}): detector says vehicle {plate['vehicle_id']}, geometry says {assigned} -- overlapping boxes, review")


def _match_ocr(result, target):
    best = max(result["plates"], key=lambda plate: plate["text_sim"], default=None)
    if best and best["text_sim"] >= OCR_STRONG_MATCH and best["vehicle_id"] is not None:
        result.update(vehicle_id=best["vehicle_id"], phase="ocr", confidence=best["text_sim"], winner_plate=best["index"])
        return True
    return False


def _match_detected(result, target):
    candidates = [plate for plate in result["plates"] if plate["visual"] is not None and plate["vehicle_id"] is not None]
    best = max(candidates, key=lambda plate: plate["combined"], default=None)
    if best is None:
        return False
    if best["visual"] >= PLATE_VISUAL_ACCEPT:
        result.update(vehicle_id=best["vehicle_id"], phase="plate_visual", confidence=best["visual"], winner_plate=best["index"])
        return True
    if target and best["combined"] >= PLATE_HINT_ACCEPT:
        result.update(vehicle_id=best["vehicle_id"], phase="plate_hint", confidence=best["combined"], winner_plate=best["index"])
        result["warnings"].append(f"Weak visual evidence on best detected plate (NCC {best['visual']:.2f}); accepted detector/text hint, review advised")
        return True
    return False


def match(main_img, ir_crop, json_data, target_text="", force_phase=None, allow_freeform=False,
          whole_image_fallback=False):
    result, analysis = _new_result(), _analysis(json_data)
    if analysis is None:
        result["warnings"].append("No vehicleAnalysis in JSON")
        return result
    vehicles, plates = analysis.get("vehicles") or [], analysis.get("plates") or []
    if not vehicles:
        if whole_image_fallback:
            result["vehicles"] = [{
                "index": 0,
                "box": (0.0, 0.0, 1.0, 1.0),
                "label": "Whole image (MCAP vehicle unavailable)",
                "color": [],
            }]
            result.update(vehicle_id=0, phase="whole_image_fallback", confidence=0.0)
            result["warnings"].append(
                "MCAP detected no vehicles; using the full overview image as the vehicle box."
            )
            return result
        result["warnings"].append("No vehicles detected")
        return result
    vehicle_boxes = [to_box(vehicle.get("boundingBox")) for vehicle in vehicles]
    result["vehicles"] = [{"index": index, "box": box, "label": ((vehicle.get("attributes") or {}).get("makeModel") or "?"),
                           "color": ((vehicle.get("attributes") or {}).get("color") or [])}
                          for index, (vehicle, box) in enumerate(zip(vehicles, vehicle_boxes))]
    target = normalize_text(target_text)
    result["plates"] = _score_plates(main_img, ir_crop, plates, target)
    _record_assignments(result, vehicle_boxes)
    phases = ["ocr", "visual", "geometric"]
    allowed = phases[phases.index(force_phase):] if force_phase in phases else phases
    if "ocr" in allowed and target and _match_ocr(result, target):
        return result
    if "visual" in allowed and _match_detected(result, target):
        return result
    candidates = locate_plate_candidates(main_img, ir_crop, vehicle_boxes)
    detected = [plate["box"] for plate in result["plates"] if plate["box"] is not None]
    if detected and not allow_freeform:
        candidates = [candidate for candidate in candidates if any(containment(candidate["box"], box) > 0.3 or containment(box, candidate["box"]) > 0.3 for box in detected)]
    if not candidates:
        result["warnings"].append("Full-image localisation produced no candidates" if not detected else f"{len(detected)} plate(s) detected in frame, none matched the query crop -- plate is probably not in this frame (use --allow-freeform to override)")
        return result
    best_pair = None
    claimed_base = {plate["vehicle_id"] for plate in result["plates"] if plate["vehicle_id"] is not None and plate["text_sim"] < OCR_STRONG_MATCH and plate["box"] is not None}
    for candidate in candidates:
        claimed = {vehicle for vehicle in claimed_base if not any(containment(plate["box"], candidate["box"]) > 0.3 for plate in result["plates"] if plate["box"] is not None and plate["vehicle_id"] == vehicle)}
        ranking = score_vehicles_for_plate(main_img, candidate["box"], vehicles, vehicle_boxes, claimed)
        if ranking:
            joint = 0.45 * candidate["ncc"] + 0.55 * ranking[0]["score"]
            if best_pair is None or joint > best_pair[0]:
                best_pair = joint, candidate, ranking
    if best_pair is None:
        result["warnings"].append("No localisation candidate fell inside a vehicle box")
        return result
    joint, candidate, ranking = best_pair
    top, margin = ranking[0], ranking[0]["score"] - (ranking[1]["score"] if len(ranking) > 1 else 0.0)
    result.update(vehicle_id=top["vehicle"], phase="geometric", confidence=joint, ranked=ranking,
                  localised={"box": candidate["box"], "score": candidate["ncc"], "joint": joint, "n_candidates": len(candidates)})
    if candidate["ncc"] < GLOBAL_VISUAL_ACCEPT:
        result["warnings"].append(f"Weak localisation NCC {candidate['ncc']:.2f} -- low confidence")
    if margin < 0.10:
        result["warnings"].append(f"Top-2 margin only {margin:.3f} -- ambiguous, flag for review")
    return result
