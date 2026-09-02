"""Evidence: what was retrieved, and the content address that names it.

Step 0.45 creates this module with `Source` and `evidence_id()` and nothing
else. `EvidenceRecord`, `field_index`, `query_id`, and the absence-authority
table land in step 1.4 at this same path.

The reason a *screen* needs evidence at all is implementation-plan.md §6:
**a library read is evidence too**. `Source.LIBRARY` with `endpoint="export"`
is what lets a mechanical check cite the record it read, so an item's
should-not-touch label carries a citation rather than an assertion. Without it,
0.6's `verification.checks[]` array would have an `evidence_id` field that only
the authority tier could ever fill.
"""

from collections.abc import Mapping
from enum import StrEnum
from hashlib import sha256
from typing import Any

from shelfwarden.canonical import canonical_json

DIGEST_PREFIX = "sha256:"


class Source(StrEnum):
    """Where a piece of evidence came from.

    `LIBRARY` is the load-bearing member; the four external sources are listed
    now so that step 1.1 adds adapters rather than enum members, and so the
    screen's authority tier has something to name when it lands.
    """

    LIBRARY = "library"
    TMDB = "tmdb"
    TVDB = "tvdb"
    AUDNEXUS = "audnexus"
    OPENLIBRARY = "openlibrary"


def evidence_id(
    source: Source,
    endpoint: str,
    params: Mapping[str, Any],
    body: Any,
) -> str:
    """`sha256(source|endpoint|params|body)`, per implementation-plan.md §6.

    Each component is serialized with `canonical_json` *before* the join rather
    than concatenated raw. That matters: a raw pipe join is ambiguous the moment
    any component contains a `|`, so two different retrievals could hash to one
    id. JSON string quoting is self-delimiting, which makes the encoding
    injective -- `"a|b"|"c"` and `"a"|"b|c"` are different byte strings.

    Stable across processes because `canonical_json` sorts keys and this hashes
    bytes rather than a `repr`. Do not put credentials in `params`: practices
    §3.4 requires auth headers and keys stripped before hashing *and* before
    storing, and the evidence store is exactly where a leaked one would persist.
    """
    payload = b"|".join(
        (
            canonical_json(str(source)),
            canonical_json(endpoint),
            canonical_json(dict(params)),
            canonical_json(body),
        )
    )
    return DIGEST_PREFIX + sha256(payload).hexdigest()


__all__ = ["DIGEST_PREFIX", "Source", "evidence_id"]
