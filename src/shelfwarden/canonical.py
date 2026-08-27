"""The canonical serializer.

Determinism for byte-identical exports, content-addressed evidence, and semantic
`case_id` hashes all reduce to this function producing the same bytes for the same
value. Anything hashed, compared, or written to a dataset goes through it; a bare
`json.dumps` elsewhere skips the guarantees below and is a bug.
"""

import json
import unicodedata


def canonical_json(obj: object) -> bytes:
    """Deterministic JSON bytes.

    `allow_nan=False` is load-bearing rather than defensive: `json.dumps` will
    otherwise happily emit bare `NaN` and `Infinity`, which are not JSON. Python
    reads them back, so the dataset looks fine here and is rejected by every other
    parser that touches it. Failing at write time turns a silent corruption into a
    loud one.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_text(value: str) -> str:
    """NFC-normalize text crossing into the model.

    "Amélie" composed and decomposed are the same string to a reader and different
    bytes to a hash. macOS filesystems hand out NFD, so both forms genuinely reach
    us. Note the deliberate exception: file paths are *not* normalized -- see
    `models.item.FilePart`.
    """
    return unicodedata.normalize("NFC", value)
