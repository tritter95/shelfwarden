"""Corruption functions and their registry (step 0.5).

Fifteen problem classes are declared across `movies.py`, `tv.py`, and
`audiobooks.py`. Eleven are implemented; three wait on `sources/` (step 1.1)
because they need an external record as an ingredient or as a witness, and one is
not synthesizable by design. The split is `registry.CORRUPTION_TABLE` and
`registry.UNSYNTHESIZABLE_REASON` -- a table, so the counts are computed rather
than typed into prose.

Two invariants hold for every registered corruption: `apply_reverse(changes)`
reproduces the ground-truth family byte-for-byte, and no case ships without a
`DetectabilityWitness` that the scorer's own comparators accept.

Importing this package registers every corruption. The submodules are imported
for that side effect and are not otherwise re-exported, which is why the imports
below look unused.
"""

from shelfwarden.evals.corrupt import audiobooks, movies, tv  # noqa: F401
from shelfwarden.evals.corrupt.model import (
    ChangeKind,
    CorruptionError,
    FieldChange,
    ItemChange,
    Rejection,
)
from shelfwarden.evals.corrupt.registry import (
    CORRUPTION_TABLE,
    UNSYNTHESIZABLE_REASON,
    CorruptionResult,
    deferred_classes,
    implemented_classes,
)
from shelfwarden.evals.corrupt.run import CorruptionRun, run_corruptions
from shelfwarden.evals.corrupt.witness import DetectabilityWitness, WitnessKind, WitnessTier

__all__ = [
    "CORRUPTION_TABLE",
    "UNSYNTHESIZABLE_REASON",
    "ChangeKind",
    "CorruptionError",
    "CorruptionResult",
    "CorruptionRun",
    "DetectabilityWitness",
    "FieldChange",
    "ItemChange",
    "Rejection",
    "WitnessKind",
    "WitnessTier",
    "deferred_classes",
    "implemented_classes",
    "run_corruptions",
]
