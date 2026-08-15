"""
keys.py

Redis key helpers, ported 1:1 from simulation_hmm.html's normalizeModelKey(), plus a
plate-scoped key builder for a real (shared, global) Redis instance.

This is the single source of truth for key logic -- vbio_redis_loader.py imports from
here rather than defining its own copy.
"""
import re
import unicodedata
from typing import Optional


def _canonical_component(value: Optional[str]) -> str:
    """Case-fold and remove spacing/punctuation without applying semantic aliases."""
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def normalize_model_key(make: Optional[str], model: Optional[str]) -> str:
    """
    Canonical key for mechanical make/model spelling variants. Case, whitespace,
    punctuation, and diacritics are removed; semantic aliases are deliberately not.
    """
    clean_make = _canonical_component(make)
    clean_model = _canonical_component(model)
    return f"{clean_make}_{clean_model}"


def make_from_key(key: str) -> str:
    """Extracts the make from a normalized or plate-scoped Redis candidate key."""
    model_key = key.rsplit(":", 1)[-1]
    return model_key.split("_", 1)[0]


def build_redis_key(plate_number: str, plate_state: str, make: str, model: str) -> str:
    """
    Real production Redis key. simulation_hmm.html's `redis` Map is local to ONE plate
    (rebuilt per plate inside computeHMMSequence), so it can key on make/model alone.
    A shared Redis instance is global across all plates, so plateNumber/plateState are
    prefixed to keep different plates' candidates from colliding.
    """
    model_key = normalize_model_key(make, model)
    return f"vbio:{(plate_number or '').upper()}:{(plate_state or '').upper()}:{model_key}"
