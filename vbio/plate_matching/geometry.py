import cv2
import numpy as np


def to_box(data):
    if not data:
        return None
    box = tuple(float(data.get(key, 0)) for key in ("x1", "y1", "x2", "y2"))
    return box if box[2] > box[0] and box[3] > box[1] else None


def area(box):
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def inter_area(a, b):
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return (x2 - x1) * (y2 - y1) if x2 > x1 and y2 > y1 else 0.0


def containment(inner, outer):
    inner_area = area(inner)
    return inter_area(inner, outer) / inner_area if inner_area else 0.0


def px(box, width, height):
    return tuple(int(value) for value in (box[0] * width, box[1] * height, box[2] * width, box[3] * height))


def crop_norm(image, box, pad=0.0):
    if image is None or box is None:
        return None
    height, width = image.shape[:2]
    box_width, box_height = box[2] - box[0], box[3] - box[1]
    x1, y1 = int(max(0, (box[0] - box_width * pad) * width)), int(max(0, (box[1] - box_height * pad) * height))
    x2, y2 = int(min(width, (box[2] + box_width * pad) * width)), int(min(height, (box[3] + box_height * pad) * height))
    return image[y1:y2, x1:x2] if x2 > x1 and y2 > y1 else None


def gray(image):
    if image is None:
        return None
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
