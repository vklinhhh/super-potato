import cv2
import numpy as np

from .constants import C_LOC, C_OTHER, C_PLATE, C_RIVAL, C_WIN, GEO_WEIGHTS
from .geometry import px


def _text(image, text, origin, color, scale=0.5, thickness=1):
    cv2.putText(image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thickness + 3, cv2.LINE_AA)
    cv2.putText(image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def visualise(main_img, ir_crop, result, target_text=""):
    image, height, width = main_img.copy(), main_img.shape[0], main_img.shape[1]
    winner, scale = result["vehicle_id"], max(0.4, min(0.7, width / 1400.0))
    ranked = {item["vehicle"]: item for item in result["ranked"]}
    for vehicle in result["vehicles"]:
        if vehicle["box"] is None:
            continue
        index = vehicle["index"]
        x1, y1, x2, y2 = px(vehicle["box"], width, height)
        color = C_WIN if index == winner else (C_RIVAL if index in ranked else C_OTHER)
        cv2.rectangle(image, (x1, y1), (x2, y2), color, 3 if index == winner else 2)
        item = ranked.get(index)
        label = f"#{index} {vehicle['label']}" + (f" | geo {item['geo']:.2f} -> {item['score']:.2f}" if item else "")
        _text(image, ("MATCH  " if index == winner else "") + label, (x1 + 4, max(16, y1 - 7 - (index % 3) * 17)), color, scale)
    for plate in result["plates"]:
        if plate["box"] is None:
            continue
        x1, y1, x2, y2 = px(plate["box"], width, height)
        cv2.rectangle(image, (x1, y1), (x2, y2), C_PLATE, 2)
        _text(image, f"{plate['text'] or 'unreadable'} v{plate['vehicle_id']}", (x1, min(height - 6, y2 + 15)), C_PLATE, scale * .85)
    band = np.full((64, width, 3), 28, dtype=np.uint8)
    label = next((vehicle["label"] for vehicle in result["vehicles"] if vehicle["index"] == winner), "")
    _text(band, f"{f'vehicle #{winner}' if winner is not None else 'NO MATCH'}  {label}", (12, 26), C_WIN if winner is not None else C_RIVAL, .7, 2)
    _text(band, f"phase: {result['phase']}   confidence: {result['confidence']:.3f}   query: {target_text or '(none)'}", (12, 50), (200, 200, 200), .5)
    return np.vstack([band, image])


def print_report(result, target_text=""):
    print("=" * 74)
    print(f"QUERY      : {target_text or '(no text -- visual/geometric path)'}")
    print(f"RESULT     : vehicle #{result['vehicle_id']}   phase={result['phase']}   confidence={result['confidence']:.3f}")
    print("=" * 74)
    for vehicle in result["vehicles"]:
        box = " ".join(f"{value:.3f}" for value in vehicle["box"]) if vehicle["box"] else "invalid"
        print(f"  #{vehicle['index']}  {str(vehicle['label'] or '?'):<26} {str(vehicle['color']):<20} [{box}]")
