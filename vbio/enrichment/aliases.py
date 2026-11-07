"""
aliases.py

MakeModelAliases: resolves a raw (make, model) pair -- spelled however the SOURCE
happened to spell it (VBIo/DMV export, MCAP's own vocabulary, ...) -- to the single
canonical spelling curated in make_model_mapping.json.

Why this exists: vbio.enrichment.keys.normalize_model_key() only strips case,
whitespace, and punctuation -- it has no notion of semantic aliases (e.g. MCAP
reporting "Supra" for a car VBIo seeded as "GR Supra"). Without resolving that alias
BEFORE building a candidate's Redis key, the two spellings become two different
candidates that never reinforce each other, splitting the evidence for the same real
vehicle and letting an unrelated, consistently-misspelled-the-same-way wrong candidate
win on accumulated score alone.

Pure lookup logic only -- no file I/O (that lives in
vbio.clients.make_model_mapping.load_make_model_aliases), so this stays trivially
unit-testable like the rest of vbio/enrichment/.
"""
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class MakeModelAliases:
    # casefolded variant -> canonical make
    make_lookup: Dict[str, str]
    # (casefolded canonical make, casefolded variant) -> canonical model
    model_lookup: Dict[Tuple[str, str], str]

    def canonicalize(self, make: Optional[str], model: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """Resolves make first, then looks up model scoped under that canonical make.
        Unknown values pass through unchanged (stripped) rather than raising, so an
        un-reviewed/new spelling just behaves like it did before this existed."""
        if not make:
            return make, model

        canon_make = self.make_lookup.get(str(make).strip().casefold(), str(make).strip())
        if not model:
            return canon_make, model

        canon_model = self.model_lookup.get(
            (canon_make.strip().casefold(), str(model).strip().casefold()),
            str(model).strip(),
        )
        return canon_make, canon_model
