OCR_STRONG_MATCH = 0.80
PLATE_VISUAL_ACCEPT = 0.45
PLATE_HINT_ACCEPT = 0.25
GLOBAL_VISUAL_ACCEPT = 0.35
EXPECTED_PLATE_WIDTH_RATIO = (0.06, 0.30)
EDGE_MARGIN_FLOOR = 0.15
MIN_CONTAINMENT = 0.50
COLOR_BLEND = 0.40
COLOR_SAT_MIN = 50

GEO_WEIGHTS = {
    "containment": 0.22, "horizontal": 0.28, "vertical": 0.08,
    "edge_margin": 0.25, "scale": 0.12, "depth": 0.05,
}
HUE_RANGES = {
    "red": [(0, 10), (168, 180)], "orange": [(8, 22)], "yellow": [(20, 35)],
    "green": [(35, 85)], "blue": [(90, 135)], "purple": [(130, 165)],
    "brown": [(5, 25)], "maroon": [(165, 180), (0, 8)],
}
ACHROMATIC = {"black", "white", "gray", "grey", "silver"}

C_WIN = (80, 220, 80)
C_RIVAL = (70, 70, 235)
C_OTHER = (150, 150, 150)
C_PLATE = (40, 220, 240)
C_LOC = (240, 160, 40)
