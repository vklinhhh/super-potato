"""Unit tests for vbio.enrichment.aliases.MakeModelAliases and its JSON loader."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vbio.clients.make_model_mapping import load_make_model_aliases
from vbio.enrichment.aliases import MakeModelAliases

MAPPING_PATH = Path(__file__).resolve().parents[2] / "assets" / "make_model_mapping.json"


def test_canonicalize_resolves_make_and_model_variants():
    aliases = MakeModelAliases(
        make_lookup={"ford": "Ford", "forc": "Ford"},
        model_lookup={("ford", "f150"): "F-150", ("ford", "f-150"): "F-150"},
    )

    assert aliases.canonicalize("FORD", "f150") == ("Ford", "F-150")
    assert aliases.canonicalize("Forc", "F-150") == ("Ford", "F-150")


def test_canonicalize_passes_through_unknown_values_unchanged():
    aliases = MakeModelAliases(make_lookup={}, model_lookup={})

    assert aliases.canonicalize("Tesla", "Cybertruck") == ("Tesla", "Cybertruck")
    assert aliases.canonicalize(None, "X") == (None, "X")
    assert aliases.canonicalize("Tesla", None) == ("Tesla", None)


def test_loader_resolves_a_real_word_level_model_variant_from_the_mapping_file():
    """"Tlx" and "Tl" differ by more than case/punctuation/whitespace -- so
    keys.normalize_model_key()'s mechanical stripping alone can never merge them.
    make_model_mapping.json's curators grouped both under canonical "Tl" for Acura,
    so the loader must resolve "Tlx" to "Tl" too.
    """
    aliases = load_make_model_aliases(str(MAPPING_PATH))

    assert aliases.canonicalize("Acura", "Tlx") == ("Acura", "Tl")
    assert aliases.canonicalize("ACURA", "tl") == ("Acura", "Tl")


def test_loader_resolves_supra_and_gr_supra_as_the_same_model():
    """"Supra" (MCAP's spelling) and "GR Supra" (VBIo/gt spelling, Toyota's official
    marketing name) were merged into one canonical group by a human curation decision
    in make_model_mapping.json -- previously they were two deliberately separate
    groups (see ENGINE_LOGIC_FLOW.md / PHASE2_REVIEW.md history for that earlier
    analysis), but repeated review of real MCAP output confirmed they refer to the
    same vehicle, so the mapping file was updated to merge them. This loader must
    resolve both spellings to the single canonical "GR Supra"."""
    aliases = load_make_model_aliases(str(MAPPING_PATH))

    assert aliases.canonicalize("Toyota", "Supra") == ("Toyota", "GR Supra")
    assert aliases.canonicalize("TOYOTA", "GR Supra") == ("Toyota", "GR Supra")


def test_loader_resolves_model_make_key_through_make_typo_variant():
    """The mapping file groups models under the "Hyundai" spelling, but the make's own
    canonical name is the typo "Huyndai" ("Hyundai" is only a listed variant of it).
    The loader must still resolve models correctly for whichever spelling a caller
    passes in as make."""
    aliases = load_make_model_aliases(str(MAPPING_PATH))

    assert aliases.canonicalize("Hyundai", "Elantra") == ("Huyndai", "Elantra")
    assert aliases.canonicalize("Huyndai", "Elantra") == ("Huyndai", "Elantra")
