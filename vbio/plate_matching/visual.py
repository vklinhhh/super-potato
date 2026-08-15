import cv2
import numpy as np

from .constants import (ACHROMATIC, COLOR_BLEND, COLOR_SAT_MIN, EDGE_MARGIN_FLOOR,
                        EXPECTED_PLATE_WIDTH_RATIO, GEO_WEIGHTS, HUE_RANGES, MIN_CONTAINMENT)
from .geometry import area, containment, crop_norm, gray, inter_area, px


def plate_crop_similarity(ir_crop, candidate_crop):
    a, b = gray(ir_crop), gray(candidate_crop)
    if a is None or b is None or not a.size or not b.size:
        return 0.0
    size = (96, 48)
    a, b = cv2.equalizeHist(cv2.resize(a, size)), cv2.equalizeHist(cv2.resize(b, size))
    return float(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED).max())


def locate_plate_candidates(main_img, ir_crop, vehicle_boxes=None, top_k=8):
    main, ir = gray(main_img), gray(ir_crop)
    if main is None or ir is None or not ir.size:
        return []
    height, width = main.shape
    widths = [box[2] - box[0] for box in vehicle_boxes or [] if box]
    if widths:
        low, high = EXPECTED_PLATE_WIDTH_RATIO
        median = float(np.median(widths))
        min_width, max_width = low * median * width, high * median * width
    else:
        min_width, max_width = 20, width * 0.25
    scales = np.linspace(max(0.15, min_width / ir.shape[1]), min(3.0, max_width / ir.shape[1]), 16)
    candidates = []
    for scale in scales:
        template_width, template_height = int(ir.shape[1] * scale), int(ir.shape[0] * scale)
        if template_width < 12 or template_height < 6 or template_width >= width or template_height >= height:
            continue
        template = cv2.equalizeHist(cv2.resize(ir, (template_width, template_height)))
        work = cv2.matchTemplate(cv2.equalizeHist(main), template, cv2.TM_CCOEFF_NORMED)
        for _ in range(3):
            _, score, _, (x, y) = cv2.minMaxLoc(work)
            if score <= 0:
                break
            candidates.append({"box": (x / width, y / height, (x + template_width) / width, (y + template_height) / height),
                               "ncc": float(score), "scale": float(scale)})
            work[max(0, y - template_height // 2):y + template_height // 2,
                 max(0, x - template_width // 2):x + template_width // 2] = -1
    return sorted(candidates, key=lambda candidate: candidate["ncc"], reverse=True)[:top_k]


def locate_plate_in_full_image(main_img, ir_crop, vehicle_boxes=None):
    candidates = locate_plate_candidates(main_img, ir_crop, vehicle_boxes, top_k=1)
    return (candidates[0]["box"], candidates[0]["ncc"]) if candidates else (None, -1.0)


def geometric_score(plate_box, vehicle_box, all_vehicle_boxes=None):
    if plate_box is None or vehicle_box is None:
        return 0.0, {}
    vehicle_width, vehicle_height = vehicle_box[2] - vehicle_box[0], vehicle_box[3] - vehicle_box[1]
    if vehicle_width <= 0 or vehicle_height <= 0:
        return 0.0, {}
    center_x = (plate_box[0] + plate_box[2]) / 2
    center_y = (plate_box[1] + plate_box[3]) / 2
    relative_x, relative_y = (center_x - vehicle_box[0]) / vehicle_width, (center_y - vehicle_box[1]) / vehicle_height
    features = {
        "containment": containment(plate_box, vehicle_box),
        "horizontal": max(0.0, 1.0 - 2.0 * abs(relative_x - 0.5)),
        "vertical": max(0.0, 1.0 - 2.0 * abs(relative_y - 0.55)),
    }
    margins = [(plate_box[0] - vehicle_box[0]) / vehicle_width, (vehicle_box[2] - plate_box[2]) / vehicle_width,
               (plate_box[1] - vehicle_box[1]) / vehicle_height, (vehicle_box[3] - plate_box[3]) / vehicle_height]
    features["edge_margin"] = float(np.clip(min(margins) / EDGE_MARGIN_FLOOR, 0.0, 1.0)) if min(margins) > 0 else 0.0
    ratio = (plate_box[2] - plate_box[0]) / vehicle_width
    low, high = EXPECTED_PLATE_WIDTH_RATIO
    features["scale"] = 1.0 if low <= ratio <= high else float(np.clip(1.0 - ((low - ratio) if ratio < low else (ratio - high)) / low, 0.0, 1.0))
    features["depth"] = 0.5
    rivals = [box for box in all_vehicle_boxes or [] if box is not vehicle_box and inter_area(box, vehicle_box) > 0 and containment(plate_box, box) > 0.5]
    if rivals:
        features["depth"] = sum(vehicle_box[3] > box[3] or (abs(vehicle_box[3] - box[3]) < 0.01 and area(vehicle_box) > area(box)) for box in rivals) / len(rivals)
    return sum(GEO_WEIGHTS[name] * features[name] for name in GEO_WEIGHTS), features


def _body_ring_hues(main_img, plate_box, pad=0.9):
    if main_img is None or plate_box is None or main_img.ndim != 3:
        return None
    height, width = main_img.shape[:2]
    x1, y1, x2, y2 = px(plate_box, width, height)
    pad_x, pad_y = int((x2 - x1) * pad), int((y2 - y1) * pad)
    left, top, right, bottom = max(0, x1 - pad_x), max(0, y1 - pad_y), min(width, x2 + pad_x), min(height, y2 + pad_y)
    region = main_img[top:bottom, left:right]
    if not region.size:
        return None
    mask = np.ones(region.shape[:2], bool)
    mask[max(0, y1 - top):max(0, y2 - top), max(0, x1 - left):max(0, x2 - left)] = False
    pixels = region[mask]
    if len(pixels) < 50:
        return None
    hsv = cv2.cvtColor(pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
    return hsv[hsv[:, 1] > COLOR_SAT_MIN][:, 0]


def color_consistency(main_img, plate_box, vehicle):
    colors = [color.lower() for color in (vehicle.get("attributes", {}).get("color") or [])]
    colors = [color for color in colors if color in HUE_RANGES and color not in ACHROMATIC]
    hues = _body_ring_hues(main_img, plate_box)
    if not colors or hues is None or len(hues) < 100:
        return None
    hit = np.zeros(len(hues), bool)
    for color in colors:
        for low, high in HUE_RANGES[color]:
            hit |= (hues >= low) & (hues <= high)
    return float(hit.mean())


def score_vehicles_for_plate(main_img, plate_box, vehicles, vehicle_boxes, claimed=()):
    ranked = []
    for index, vehicle_box in enumerate(vehicle_boxes):
        if vehicle_box is None or containment(plate_box, vehicle_box) < MIN_CONTAINMENT:
            continue
        geo, features = geometric_score(plate_box, vehicle_box, vehicle_boxes)
        color = color_consistency(main_img, plate_box, vehicles[index])
        score = (1 - COLOR_BLEND) * geo + COLOR_BLEND * color if color is not None else geo
        if index in claimed:
            score *= 0.5
        ranked.append({"vehicle": index, "geo": geo, "color": color, "score": score, "features": features,
                       "claimed": index in claimed, "label": (vehicles[index].get("attributes") or {}).get("makeModel") or "?"})
    return sorted(ranked, key=lambda result: result["score"], reverse=True)


def assign_plates_to_vehicles(plate_boxes, vehicle_boxes):
    if not plate_boxes or not vehicle_boxes:
        return {}
    cost = np.ones((len(plate_boxes), len(vehicle_boxes)))
    for plate_index, plate_box in enumerate(plate_boxes):
        for vehicle_index, vehicle_box in enumerate(vehicle_boxes):
            if plate_box is not None and vehicle_box is not None:
                cost[plate_index, vehicle_index] = 1.0 - geometric_score(plate_box, vehicle_box, vehicle_boxes)[0]
    try:
        from scipy.optimize import linear_sum_assignment
    except ImportError:
        pairs = sorted(((i, j) for i in range(cost.shape[0]) for j in range(cost.shape[1])), key=lambda pair: cost[pair])
        assigned, used = {}, set()
        for plate_index, vehicle_index in pairs:
            if plate_index not in assigned and vehicle_index not in used:
                assigned[plate_index], used = vehicle_index, used | {vehicle_index}
        return assigned
    rows, columns = linear_sum_assignment(cost)
    return {int(row): int(column) for row, column in zip(rows, columns)}
