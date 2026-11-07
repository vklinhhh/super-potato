"""
make_model_mapping.py

Loads assets/make_model_mapping.json (the human-reviewed make/model normalization
dataset) into a vbio.enrichment.aliases.MakeModelAliases -- the only place that
touches the filesystem for this; the lookup object itself is pure.

Usage:
    from vbio.clients.make_model_mapping import load_make_model_aliases
    aliases = load_make_model_aliases("assets/make_model_mapping.json")
    engine = EnrichmentEngine(redis_store, aliases=aliases)
"""
import json

from vbio.enrichment.aliases import MakeModelAliases


def load_make_model_aliases(path: str) -> MakeModelAliases:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    make_lookup = {}
    for group in data.get("makes", []):
        canonical = group["canonical"]
        make_lookup[canonical.strip().casefold()] = canonical
        for variant in group.get("variants", []):
            make_lookup[variant["value"].strip().casefold()] = canonical

    model_lookup = {}
    for group in data.get("models", []):
        # group["make"] is NOT guaranteed to equal a canonical make's own spelling
        # (e.g. models are grouped under "Hyundai", but the canonical make name is the
        # typo "Huyndai" -- "Hyundai" is only a listed variant of it). Resolve it
        # through make_lookup first so the key here matches what canonicalize() will
        # compute for make at lookup time.
        raw_make = group["make"].strip()
        make_key = make_lookup.get(raw_make.casefold(), raw_make).strip().casefold()

        canonical_model = group["canonical"]
        model_lookup[(make_key, canonical_model.strip().casefold())] = canonical_model
        for variant in group.get("variants", []):
            model_lookup[(make_key, variant["value"].strip().casefold())] = canonical_model

    return MakeModelAliases(make_lookup=make_lookup, model_lookup=model_lookup)
