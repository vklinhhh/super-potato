#!/usr/bin/env python3
"""Backward-compatible executable wrapper for :mod:`vbio.plate_matching`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vbio.plate_matching.cli import main, run_one
from vbio.plate_matching.geometry import area, containment, crop_norm, gray, inter_area, px, to_box
from vbio.plate_matching.matcher import match, normalize_text, text_similarity
from vbio.plate_matching.presentation import print_report, visualise
from vbio.plate_matching.visual import (assign_plates_to_vehicles, color_consistency, geometric_score,
                                         locate_plate_candidates, locate_plate_in_full_image,
                                         plate_crop_similarity, score_vehicles_for_plate)

if __name__ == "__main__":
    main()
