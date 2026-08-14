"""
keys.py

Redis key helpers, ported 1:1 from simulation_hmm.html's normalizeModelKey(), plus a
plate-scoped key builder for a real (shared, global) Redis instance.

This is the single source of truth for key logic -- vbio_redis_loader.py imports from
here rather than defining its own copy.
"""
from typing import Optional


def normalize_model_key(make: Optional[str], model: Optional[str]) -> str:
    """
    Python port of simulation_hmm.html's normalizeModelKey(make, model):
        cleanMake  = lowercase, trimmed, internal whitespace runs -> '_'
        cleanModel = lowercase, trimmed, spaces AND hyphens stripped out entirely
    """
    clean_make = "_".join((make or "").lower().strip().split())
    clean_model = "".join(ch for ch in (model or "").lower().strip() if not ch.isspace() and ch != "-")
    return f"{clean_make}_{clean_model}"


def make_from_key(key: str) -> str:
    """Extracts the make portion of a normalized model key ('ford_flex' -> 'ford')."""
    return key.split("_", 1)[0]


def build_redis_key(plate_number: str, plate_state: str, make: str, model: str) -> str:
    """
    Real production Redis key. simulation_hmm.html's `redis` Map is local to ONE plate
    (rebuilt per plate inside computeHMMSequence), so it can key on make/model alone.
    A shared Redis instance is global across all plates, so plateNumber/plateState are
    prefixed to keep different plates' candidates from colliding.
    """
    model_key = normalize_model_key(make, model)
    return f"vbio:{(plate_number or '').upper()}:{(plate_state or '').upper()}:{model_key}"
