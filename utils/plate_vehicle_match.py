#!/usr/bin/env python3
"""
plate_vehicle_match.py -- standalone plate -> vehicle matcher with visualisation.

Takes three inputs and tells you which vehicle in the frame owns the plate:
    1. the full context image      (--image)
    2. the MCAP analysis JSON      (--json)
    3. the IR / LPR plate crop     (--plate)

Usage
-----
    python plate_vehicle_match.py \
        --image frame.jpg \
        --json  frame_analysis.json \
        --plate plate_crop.jpg \
        --text  HXX7033 \
        --out   result.png

    # no OCR text available -- forces the visual + geometric path
    python plate_vehicle_match.py -i frame.jpg -j a.json -p crop.jpg

    # batch over a folder of matching triplets
    python plate_vehicle_match.py --batch ./frames --out-dir ./results

Options
-------
    --text          known plate text; omit to test the fallback path
    --force-phase   ocr | visual | geometric -- skip earlier phases to test one
    --out           output PNG (default: <image stem>_match.png)
    --show          open a window instead of / as well as writing the PNG
    --json-out      dump the full score breakdown as JSON
    --quiet         suppress the stdout table

Dependencies: opencv-python, numpy.  scipy is optional (better assignment).
"""

import argparse
import json
import os
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

import cv2
import numpy as np

# =============================================================================
# TUNABLES
# =============================================================================
OCR_STRONG_MATCH = 0.80        # text similarity that alone decides the match
PLATE_VISUAL_ACCEPT = 0.45     # min NCC to trust a detected-plate visual match
PLATE_HINT_ACCEPT = 0.25       # min weak-evidence score for detector-guided fallback
GLOBAL_VISUAL_ACCEPT = 0.35    # min NCC to trust full-image localisation
EXPECTED_PLATE_WIDTH_RATIO = (0.06, 0.30)
EDGE_MARGIN_FLOOR = 0.15
MIN_CONTAINMENT = 0.50

GEO_WEIGHTS = {
    "containment": 0.22,
    "horizontal": 0.28,
    "vertical": 0.08,
    "edge_margin": 0.25,
    "scale": 0.12,
    "depth": 0.05,
}

HUE_RANGES = {
    "red": [(0, 10), (168, 180)], "orange": [(8, 22)], "yellow": [(20, 35)],
    "green": [(35, 85)], "blue": [(90, 135)], "purple": [(130, 165)],
    "brown": [(5, 25)], "maroon": [(165, 180), (0, 8)],
}
ACHROMATIC = {"black", "white", "gray", "grey", "silver"}
COLOR_SAT_MIN = 50
COLOR_BLEND = 0.40             # weight of the colour cue in the final score

# BGR palette
C_WIN = (80, 220, 80)
C_RIVAL = (70, 70, 235)
C_OTHER = (150, 150, 150)
C_PLATE = (40, 220, 240)
C_LOC = (240, 160, 40)


# =============================================================================
# TEXT
# =============================================================================
def normalize_text(text):
    if not text:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


def text_similarity(a, b):
    s1, s2 = normalize_text(a), normalize_text(b)
    if not s1 or not s2:
        return 0.0
    return SequenceMatcher(None, s1, s2).ratio()


# =============================================================================
# GEOMETRY  (boxes are normalised tuples (x1, y1, x2, y2) in [0, 1])
# =============================================================================
def to_box(d):
    if not d:
        return None
    b = (float(d.get("x1", 0)), float(d.get("y1", 0)),
         float(d.get("x2", 0)), float(d.get("y2", 0)))
    return b if (b[2] > b[0] and b[3] > b[1]) else None


def area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def inter_area(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    return (x2 - x1) * (y2 - y1) if (x2 > x1 and y2 > y1) else 0.0


def containment(inner, outer):
    a = area(inner)
    return inter_area(inner, outer) / a if a > 0 else 0.0


def px(box, w, h):
    return int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)


def crop_norm(img, box, pad=0.0):
    if img is None or box is None:
        return None
    h, w = img.shape[:2]
    bw, bh = box[2] - box[0], box[3] - box[1]
    x1 = int(max(0, (box[0] - bw * pad) * w))
    y1 = int(max(0, (box[1] - bh * pad) * h))
    x2 = int(min(w, (box[2] + bw * pad) * w))
    y2 = int(min(h, (box[3] + bh * pad) * h))
    return img[y1:y2, x1:x2] if (x2 > x1 and y2 > y1) else None


def gray(img):
    if img is None:
        return None
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


# =============================================================================
# VISUAL
# =============================================================================
def plate_crop_similarity(ir_crop, cand_crop):
    """Same-object test between the IR crop and a candidate plate crop."""
    a, b = gray(ir_crop), gray(cand_crop)
    if a is None or b is None or a.size == 0 or b.size == 0:
        return 0.0
    size = (96, 48)
    a = cv2.equalizeHist(cv2.resize(a, size))
    b = cv2.equalizeHist(cv2.resize(b, size))
    return float(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED).max())


def locate_plate_candidates(main_img, ir_crop, vehicle_boxes=None, top_k=8):
    """
    Multi-scale template match over the WHOLE image, once, returning the top-K
    peaks rather than only the argmax.

    A single argmax is fragile: an IR crop correlated against an RGB frame
    produces plenty of spurious high peaks on trim, tyres and shadow. Returning
    several candidates lets geometry and colour arbitrate instead of NCC alone.
    The scale sweep is bounded by the plate-size prior, so patches far too small
    or large to be a plate on any detected vehicle are never proposed.
    """
    main, ir = gray(main_img), gray(ir_crop)
    if main is None or ir is None or ir.size == 0:
        return []
    H, W = main.shape

    lo_ratio, hi_ratio = EXPECTED_PLATE_WIDTH_RATIO
    if vehicle_boxes:
        widths = [b[2] - b[0] for b in vehicle_boxes if b]
        if widths:
            med = float(np.median(widths))
            min_w_px, max_w_px = lo_ratio * med * W, hi_ratio * med * W
        else:
            min_w_px, max_w_px = 20, W * 0.25
    else:
        min_w_px, max_w_px = 20, W * 0.25

    scales = np.linspace(max(0.15, min_w_px / ir.shape[1]),
                         min(3.0, max_w_px / ir.shape[1]), 16)

    main_eq = cv2.equalizeHist(main)
    out = []
    for s in scales:
        tw, th = int(ir.shape[1] * s), int(ir.shape[0] * s)
        if tw < 12 or th < 6 or tw >= W or th >= H:
            continue
        tpl = cv2.equalizeHist(cv2.resize(ir, (tw, th)))
        res = cv2.matchTemplate(main_eq, tpl, cv2.TM_CCOEFF_NORMED)
        work = res.copy()
        for _ in range(3):                       # a few peaks per scale
            _, mx, _, loc = cv2.minMaxLoc(work)
            if mx <= 0:
                break
            x, y = loc
            out.append({"box": (x / W, y / H, (x + tw) / W, (y + th) / H),
                        "ncc": float(mx), "scale": float(s)})
            x0, y0 = max(0, x - tw // 2), max(0, y - th // 2)
            work[y0:y + th // 2, x0:x + tw // 2] = -1   # suppress around the peak
    out.sort(key=lambda c: c["ncc"], reverse=True)
    return out[:top_k]


def locate_plate_in_full_image(main_img, ir_crop, vehicle_boxes=None):
    """Backwards-compatible single-best wrapper."""
    c = locate_plate_candidates(main_img, ir_crop, vehicle_boxes, top_k=1)
    return (c[0]["box"], c[0]["ncc"]) if c else (None, -1.0)


# =============================================================================
# SCORING
# =============================================================================
def geometric_score(plate_box, vehicle_box, all_vehicle_boxes=None):
    """Returns (score, feature_dict). Containment alone is deliberately not decisive."""
    if plate_box is None or vehicle_box is None:
        return 0.0, {}
    vw = vehicle_box[2] - vehicle_box[0]
    vh = vehicle_box[3] - vehicle_box[1]
    if vw <= 0 or vh <= 0:
        return 0.0, {}

    cx = (plate_box[0] + plate_box[2]) / 2.0
    cy = (plate_box[1] + plate_box[3]) / 2.0
    rel_cx = (cx - vehicle_box[0]) / vw
    rel_cy = (cy - vehicle_box[1]) / vh

    f = {"containment": containment(plate_box, vehicle_box),
         "horizontal": max(0.0, 1.0 - 2.0 * abs(rel_cx - 0.5)),
         "vertical": max(0.0, 1.0 - 2.0 * abs(rel_cy - 0.55))}

    margins = [(plate_box[0] - vehicle_box[0]) / vw,
               (vehicle_box[2] - plate_box[2]) / vw,
               (plate_box[1] - vehicle_box[1]) / vh,
               (vehicle_box[3] - plate_box[3]) / vh]
    m = min(margins)
    f["edge_margin"] = float(np.clip(m / EDGE_MARGIN_FLOOR, 0.0, 1.0)) if m > 0 else 0.0

    ratio = (plate_box[2] - plate_box[0]) / vw
    lo, hi = EXPECTED_PLATE_WIDTH_RATIO
    if lo <= ratio <= hi:
        f["scale"] = 1.0
    else:
        d = (lo - ratio) if ratio < lo else (ratio - hi)
        f["scale"] = float(np.clip(1.0 - d / lo, 0.0, 1.0))

    f["depth"] = 0.5
    if all_vehicle_boxes:
        rivals = [b for b in all_vehicle_boxes
                  if b is not vehicle_box and inter_area(b, vehicle_box) > 0
                  and containment(plate_box, b) > 0.5]
        if rivals:
            nearer = sum(1 for b in rivals
                         if vehicle_box[3] > b[3]
                         or (abs(vehicle_box[3] - b[3]) < 0.01 and area(vehicle_box) > area(b)))
            f["depth"] = nearer / len(rivals)

    return sum(GEO_WEIGHTS[k] * f[k] for k in GEO_WEIGHTS), f


def body_ring_hues(main_img, plate_box, pad=0.9):
    """Hue values of the saturated bodywork ring surrounding a plate box."""
    if main_img is None or plate_box is None or main_img.ndim != 3:
        return None
    h, w = main_img.shape[:2]
    x1, y1, x2, y2 = px(plate_box, w, h)
    pw, ph = int((x2 - x1) * pad), int((y2 - y1) * pad)
    X1, Y1 = max(0, x1 - pw), max(0, y1 - ph)
    X2, Y2 = min(w, x2 + pw), min(h, y2 + ph)
    reg = main_img[Y1:Y2, X1:X2]
    if reg.size == 0:
        return None
    mask = np.ones(reg.shape[:2], bool)
    mask[max(0, y1 - Y1):max(0, y2 - Y1), max(0, x1 - X1):max(0, x2 - X1)] = False
    pix = reg[mask]
    if len(pix) < 50:
        return None
    hsv = cv2.cvtColor(pix.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
    return hsv[hsv[:, 1] > COLOR_SAT_MIN][:, 0]


def color_consistency(main_img, plate_box, vehicle):
    """Fraction of ring hue that matches the vehicle's declared colours, or None."""
    colors = [c.lower() for c in (vehicle.get("attributes", {}).get("color") or [])]
    chromatic = [c for c in colors if c in HUE_RANGES and c not in ACHROMATIC]
    if not chromatic:
        return None
    hues = body_ring_hues(main_img, plate_box)
    if hues is None or len(hues) < 100:
        return None
    hit = np.zeros(len(hues), bool)
    for c in chromatic:
        for lo, hi in HUE_RANGES[c]:
            hit |= (hues >= lo) & (hues <= hi)
    return float(hit.mean())


def score_vehicles_for_plate(main_img, plate_box, vehicles, vehicle_boxes, claimed=()):
    """Rank every plausible vehicle for a plate location. Used by the fallback path."""
    ranked = []
    for i, vbox in enumerate(vehicle_boxes):
        if vbox is None:
            continue
        cont = containment(plate_box, vbox)
        if cont < MIN_CONTAINMENT:
            continue
        geo, feats = geometric_score(plate_box, vbox, vehicle_boxes)
        col = color_consistency(main_img, plate_box, vehicles[i])
        final = (1 - COLOR_BLEND) * geo + COLOR_BLEND * col if col is not None else geo
        if i in claimed:
            final *= 0.5
        ranked.append({"vehicle": i, "geo": geo, "color": col, "score": final,
                       "features": feats, "claimed": i in claimed,
                       "label": vehicles[i].get("attributes", {}).get("makeModel", "?")})
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked


def assign_plates_to_vehicles(plate_boxes, vehicle_boxes):
    """Global one-plate-per-vehicle assignment. A cross-check, not a source of truth."""
    if not plate_boxes or not vehicle_boxes:
        return {}
    cost = np.ones((len(plate_boxes), len(vehicle_boxes)))
    for i, p in enumerate(plate_boxes):
        for j, v in enumerate(vehicle_boxes):
            if p is not None and v is not None:
                cost[i, j] = 1.0 - geometric_score(p, v, vehicle_boxes)[0]
    try:
        from scipy.optimize import linear_sum_assignment
        r, c = linear_sum_assignment(cost)
        return {int(i): int(j) for i, j in zip(r, c)}
    except ImportError:
        out, used = {}, set()
        pairs = sorted(((i, j) for i in range(cost.shape[0]) for j in range(cost.shape[1])),
                       key=lambda t: cost[t])
        for i, j in pairs:
            if i not in out and j not in used:
                out[i] = j
                used.add(j)
        return out


# =============================================================================
# MAIN MATCHER
# =============================================================================
def match(main_img, ir_crop, json_data, target_text="", force_phase=None,
          allow_freeform=False):
    """Returns a result dict with the winning vehicleId and the full breakdown."""
    res = {"vehicle_id": None, "phase": None, "confidence": 0.0,
           "plates": [], "vehicles": [], "ranked": [], "localised": None,
           "assignment": {}, "warnings": []}

    try:
        analysis = (json_data["data"]["attributes"]["analytics"]["vehicleAnalysis"][0])
    except (KeyError, TypeError, IndexError):
        res["warnings"].append("No vehicleAnalysis in JSON")
        return res

    vehicles = analysis.get("vehicles") or []
    plates = analysis.get("plates") or []
    if not vehicles:
        res["warnings"].append("No vehicles detected")
        return res

    vehicle_boxes = [to_box(v.get("boundingBox")) for v in vehicles]
    res["vehicles"] = [{"index": i, "box": b,
                        "label": v.get("attributes", {}).get("makeModel", "?"),
                        "color": v.get("attributes", {}).get("color", [])}
                       for i, (v, b) in enumerate(zip(vehicles, vehicle_boxes))]

    # ---- score every detected plate against the query -----------------------
    norm_target = normalize_text(target_text)
    for i, p in enumerate(plates):
        pbox = to_box(p.get("boundingBox"))
        attrs = p.get("attributes", {})
        entry = {"index": i, "box": pbox, "text": attrs.get("text", ""),
                 "quality": attrs.get("quality"), "vehicle_id": p.get("vehicleId"),
                 "text_sim": text_similarity(norm_target, attrs.get("text", "")) if norm_target else 0.0,
                 "visual": None}
        if pbox is not None and ir_crop is not None and main_img is not None:
            entry["visual"] = plate_crop_similarity(ir_crop, crop_norm(main_img, pbox, pad=0.15))
        # Cross-modal NCC can be slightly negative even for the right detected plate.
        # Treat negative NCC as "no visual support" rather than hard evidence against.
        visual_support = max(0.0, entry["visual"] or 0.0)
        entry["combined"] = (0.55 * visual_support
                             + 0.30 * entry["text_sim"]
                             + 0.15 * (1.0 if entry["vehicle_id"] is not None else 0.0))
        res["plates"].append(entry)

    res["assignment"] = {str(k): v for k, v in
                         assign_plates_to_vehicles([p["box"] for p in res["plates"]],
                                                   vehicle_boxes).items()}
    for p in res["plates"]:
        a = res["assignment"].get(str(p["index"]))
        if a is not None and p["vehicle_id"] is not None and a != p["vehicle_id"]:
            res["warnings"].append(
                f"plate {p['index']} ({p['text'] or 'unreadable'}): detector says vehicle "
                f"{p['vehicle_id']}, geometry says {a} -- overlapping boxes, review")

    order = ["ocr", "visual", "geometric"]
    allowed = order[order.index(force_phase):] if force_phase in order else order

    # ---- PHASE 1: OCR text --------------------------------------------------
    if "ocr" in allowed and norm_target and res["plates"]:
        best = max(res["plates"], key=lambda p: p["text_sim"])
        if best["text_sim"] >= OCR_STRONG_MATCH and best["vehicle_id"] is not None:
            res.update(vehicle_id=best["vehicle_id"], phase="ocr",
                       confidence=best["text_sim"], winner_plate=best["index"])
            return res

    # ---- PHASE 2: visual match against DETECTED PLATE boxes -----------------
    if "visual" in allowed:
        graded = [p for p in res["plates"] if p["visual"] is not None and p["vehicle_id"] is not None]
        if graded:
            best = max(graded, key=lambda p: p["combined"])
            if best["visual"] >= PLATE_VISUAL_ACCEPT:
                res.update(vehicle_id=best["vehicle_id"], phase="plate_visual",
                           confidence=best["visual"], winner_plate=best["index"])
                return res
            # OCR on detected plate text can be weak/unreadable even when the plate box
            # and vehicle linkage are correct; accept a conservative combined hint.
            if norm_target and best["combined"] >= PLATE_HINT_ACCEPT:
                res.update(vehicle_id=best["vehicle_id"], phase="plate_hint",
                           confidence=best["combined"], winner_plate=best["index"])
                res["warnings"].append(
                    f"Weak visual evidence on best detected plate (NCC {best['visual']:.2f}); "
                    "accepted detector/text hint, review advised")
                return res

    # ---- PHASE 3: full-image localisation + geometry + colour ---------------
    cands = locate_plate_candidates(main_img, ir_crop, vehicle_boxes)
    if not cands:
        res["warnings"].append("Full-image localisation produced no candidates")
        return res

    # A plate detector is far better at finding plates than cross-modal NCC is.
    # So if the frame HAS plate detections, a free-form peak that lands nowhere
    # near any of them is almost certainly spurious -- abstain instead of guessing.
    detected = [p["box"] for p in res["plates"] if p["box"] is not None]
    if detected and not allow_freeform:
        cands = [c for c in cands
                 if any(containment(c["box"], d) > 0.3 or containment(d, c["box"]) > 0.3
                        for d in detected)]
        if not cands:
            res["warnings"].append(
                f"{len(detected)} plate(s) detected in frame, none matched the query crop "
                "-- plate is probably not in this frame (use --allow-freeform to override)")
            return res

    claimed_base = {p["vehicle_id"] for p in res["plates"]
                    if p["vehicle_id"] is not None and p["text_sim"] < OCR_STRONG_MATCH
                    and p["box"] is not None}

    best_pair = None
    for c in cands:
        claimed = {v for v in claimed_base
                   if not any(containment(p["box"], c["box"]) > 0.3
                              for p in res["plates"]
                              if p["box"] is not None and p["vehicle_id"] == v)}
        ranking = score_vehicles_for_plate(main_img, c["box"], vehicles,
                                           vehicle_boxes, claimed)
        if not ranking:
            continue
        top = ranking[0]
        # NCC alone is not trusted; a peak only wins if it also sits somewhere a
        # plate could plausibly be, on bodywork of the right colour.
        joint = 0.45 * c["ncc"] + 0.55 * top["score"]
        if best_pair is None or joint > best_pair[0]:
            best_pair = (joint, c, ranking)

    if best_pair is None:
        res["warnings"].append("No localisation candidate fell inside a vehicle box")
        return res

    joint, c, ranking = best_pair
    res["localised"] = {"box": c["box"], "score": c["ncc"], "joint": joint,
                        "n_candidates": len(cands)}
    res["ranked"] = ranking
    top = ranking[0]
    margin = top["score"] - (ranking[1]["score"] if len(ranking) > 1 else 0.0)
    res.update(vehicle_id=top["vehicle"], phase="geometric", confidence=joint)
    if c["ncc"] < GLOBAL_VISUAL_ACCEPT:
        res["warnings"].append(f"Weak localisation NCC {c['ncc']:.2f} -- low confidence")
    if margin < 0.10:
        res["warnings"].append(f"Top-2 margin only {margin:.3f} -- ambiguous, flag for review")
    return res


# =============================================================================
# VISUALISATION
# =============================================================================
def _text(img, s, org, color, scale=0.5, thick=1):
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick + 3, cv2.LINE_AA)
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)


def visualise(main_img, ir_crop, res, target_text=""):
    vis = main_img.copy()
    h, w = vis.shape[:2]
    win = res["vehicle_id"]
    scale = max(0.4, min(0.7, w / 1400.0))

    ranked_by_idx = {r["vehicle"]: r for r in res["ranked"]}

    for v in res["vehicles"]:
        if v["box"] is None:
            continue
        i = v["index"]
        x1, y1, x2, y2 = px(v["box"], w, h)
        is_win = (i == win)
        in_play = i in ranked_by_idx
        color = C_WIN if is_win else (C_RIVAL if in_play else C_OTHER)
        cv2.rectangle(vis, (x1, y1), (x2, y2), color, 3 if is_win else 2)

        label = f"#{i} {v['label']}"
        r = ranked_by_idx.get(i)
        if r:
            label += f" | geo {r['geo']:.2f}"
            label += f" col {r['color']:.2f}" if r["color"] is not None else " col n/a"
            label += f" -> {r['score']:.2f}"
        if is_win:
            label = "MATCH  " + label
        # stagger labels so adjacent overlapping boxes stay readable
        ly = max(16, y1 - 7 - (i % 3) * 17)
        _text(vis, label, (x1 + 4, ly), color, scale)

    for p in res["plates"]:
        if p["box"] is None:
            continue
        x1, y1, x2, y2 = px(p["box"], w, h)
        cv2.rectangle(vis, (x1, y1), (x2, y2), C_PLATE, 2)
        t = p["text"] or "unreadable"
        extra = f" v{p['vehicle_id']}"
        if p["visual"] is not None:
            extra += f" ncc {p['visual']:.2f}"
        _text(vis, t + extra, (x1, min(h - 6, y2 + 15)), C_PLATE, scale * 0.85)

    loc = res.get("localised") or {}
    if loc.get("box"):
        x1, y1, x2, y2 = px(loc["box"], w, h)
        cv2.rectangle(vis, (x1, y1), (x2, y2), C_LOC, 2)
        _text(vis, f"localised {loc['score']:.2f}", (x1, max(12, y1 - 6)), C_LOC, scale * 0.85)

    # header band
    band = np.zeros((64, w, 3), np.uint8)
    band[:] = (28, 28, 28)
    verdict = f"vehicle #{win}" if win is not None else "NO MATCH"
    vlabel = next((v["label"] for v in res["vehicles"] if v["index"] == win), "")
    _text(band, f"{verdict}  {vlabel}", (12, 26), C_WIN if win is not None else C_RIVAL, 0.7, 2)
    _text(band, f"phase: {res['phase']}   confidence: {res['confidence']:.3f}"
                f"   query: {target_text or '(none)'}", (12, 50), (200, 200, 200), 0.5)
    vis = np.vstack([band, vis])

    # IR crop inset, bottom-right
    if ir_crop is not None and ir_crop.size:
        ih = max(48, h // 12)
        iw = int(ir_crop.shape[1] * ih / ir_crop.shape[0])
        iw = min(iw, w // 4)
        thumb = cv2.resize(ir_crop, (iw, ih))
        if thumb.ndim == 2:
            thumb = cv2.cvtColor(thumb, cv2.COLOR_GRAY2BGR)
        oy, ox = vis.shape[0] - ih - 12, vis.shape[1] - iw - 12
        vis[oy:oy + ih, ox:ox + iw] = thumb
        cv2.rectangle(vis, (ox - 2, oy - 2), (ox + iw + 2, oy + ih + 2), C_LOC, 2)
        _text(vis, "query crop", (ox, oy - 8), C_LOC, 0.45)

    # warnings, bottom-left
    for k, msg in enumerate(res["warnings"][:3]):
        _text(vis, "! " + msg, (12, vis.shape[0] - 12 - k * 20), (60, 200, 255), 0.45)
    return vis


def print_report(res, target_text=""):
    print("=" * 74)
    print(f"QUERY      : {target_text or '(no text -- visual/geometric path)'}")
    print(f"RESULT     : vehicle #{res['vehicle_id']}"
          f"   phase={res['phase']}   confidence={res['confidence']:.3f}")
    print("=" * 74)

    print("\nVEHICLES")
    for v in res["vehicles"]:
        mark = " <== MATCH" if v["index"] == res["vehicle_id"] else ""
        box = " ".join(f"{c:.3f}" for c in v["box"]) if v["box"] else "invalid"
        label = str(v["label"]) if v["label"] is not None else "?"
        print(f"  #{v['index']}  {label:<26} {str(v['color']):<20} [{box}]{mark}")

    print("\nDETECTED PLATES")
    for p in res["plates"]:
        vis = f"{p['visual']:.3f}" if p["visual"] is not None else "  n/a"
        print(f"  #{p['index']}  {(p['text'] or 'unreadable'):<12} vehicleId={p['vehicle_id']}"
              f"  quality={str(p['quality']):<11} text_sim={p['text_sim']:.3f}  ncc={vis}")

    if res["ranked"]:
        print("\nVEHICLE RANKING (fallback path)")
        keys = list(GEO_WEIGHTS)
        print("       " + "  ".join(f"{k[:5]:>5}" for k in keys) + "    geo  color  FINAL")
        for r in res["ranked"]:
            feats = "  ".join(f"{r['features'].get(k, 0):5.2f}" for k in keys)
            col = f"{r['color']:.2f}" if r["color"] is not None else " n/a"
            flag = " (claimed)" if r["claimed"] else ""
            print(f"  #{r['vehicle']}   {feats}   {r['geo']:.3f}  {col}  {r['score']:.3f}{flag}")

    if res["assignment"]:
        print("\nGLOBAL ASSIGNMENT CROSS-CHECK (plate -> vehicle)")
        print("  " + ", ".join(f"p{k}->v{v}" for k, v in sorted(res["assignment"].items())))

    if res["warnings"]:
        print("\nWARNINGS")
        for wmsg in res["warnings"]:
            print("  ! " + wmsg)
    print()


# =============================================================================
# CLI
# =============================================================================
def run_one(image_path, json_path, plate_path, target_text, out_path,
            force_phase=None, show=False, json_out=None, quiet=False,
            allow_freeform=False):
    main_img = cv2.imread(str(image_path))
    ir_crop = cv2.imread(str(plate_path))
    if main_img is None:
        sys.exit(f"Could not read image: {image_path}")
    if ir_crop is None:
        sys.exit(f"Could not read plate crop: {plate_path}")
    with open(json_path) as fh:
        data = json.load(fh)

    res = match(main_img, ir_crop, data, target_text, force_phase, allow_freeform)

    if not quiet:
        print_report(res, target_text)

    vis = visualise(main_img, ir_crop, res, target_text)
    cv2.imwrite(str(out_path), vis)
    if not quiet:
        print(f"Wrote {out_path}")

    if json_out:
        with open(json_out, "w") as fh:
            json.dump(res, fh, indent=2, default=float)
        if not quiet:
            print(f"Wrote {json_out}")

    if show:
        cv2.imshow("plate -> vehicle", vis)
        cv2.waitKey(0)
        cv2.destroyAllWindows()
    return res


def main():
    ap = argparse.ArgumentParser(description="Match an LPR plate crop to its vehicle in a frame.")
    ap.add_argument("-i", "--image", help="full context image")
    ap.add_argument("-j", "--json", help="MCAP analysis JSON for that image")
    ap.add_argument("-p", "--plate", help="IR / LPR plate crop")
    ap.add_argument("-t", "--text", default="", help="known plate text (optional)")
    ap.add_argument("-o", "--out", help="output PNG path")
    ap.add_argument("--json-out", help="dump full score breakdown to JSON")
    ap.add_argument("--force-phase", choices=["ocr", "visual", "geometric"],
                    help="skip earlier phases to exercise a specific one")
    ap.add_argument("--allow-freeform", action="store_true",
                    help="let the fallback accept a template peak that matches no detected plate")
    ap.add_argument("--show", action="store_true", help="display in a window")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--batch", help="folder of triplets: <stem>.jpg/.json/<stem>_plate.jpg")
    ap.add_argument("--out-dir", default="./results", help="output folder for --batch")
    args = ap.parse_args()

    if args.batch:
        src, dst = Path(args.batch), Path(args.out_dir)
        dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for jf in sorted(src.glob("*.json")):
            stem = jf.stem
            img = next((src / f"{stem}{e}" for e in (".jpg", ".jpeg", ".png")
                        if (src / f"{stem}{e}").exists()), None)
            crop = next((src / f"{stem}_plate{e}" for e in (".jpg", ".jpeg", ".png")
                         if (src / f"{stem}_plate{e}").exists()), None)
            if not img or not crop:
                print(f"skip {stem}: missing image or _plate crop")
                continue
            run_one(img, jf, crop, args.text, dst / f"{stem}_match.png",
                    args.force_phase, False, None, quiet=True,
                    allow_freeform=args.allow_freeform)
            n += 1
        print(f"Processed {n} frame(s) -> {dst}")
        return

    if not (args.image and args.json and args.plate):
        ap.error("--image, --json and --plate are all required (or use --batch)")

    out = args.out or f"{Path(args.image).stem}_match.png"
    run_one(args.image, args.json, args.plate, args.text, out,
            args.force_phase, args.show, args.json_out, args.quiet,
            args.allow_freeform)


if __name__ == "__main__":
    main()