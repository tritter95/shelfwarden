"""What a corruption broke outside the family it was given.

Verified in step 0.5, on the committed fixture export. Corrupting `fake:1:101`
to carry the identity of `fake:1:103` flips **`fake:1:103`** -- an item nothing
touched -- from `guarded` to `failed`, and strips its `duplicate_quality` guard:

    fake:1:101  guarded -> failed   lost: duplicate_quality, filename_unmatchable
    fake:1:103  guarded -> failed   lost: duplicate_quality        <- untouched

The reason is that two of the screen's eleven predicates are **population**
scoped. If `fake:1:103` had been drawn into the should-not-touch slice, a correct
agent finding on it would score as a false positive -- the direction this project
has forbidden.

So every corruption declares what it moved. Only the two population-scoped
predicates can reach outside a family, so this is computed by recomputing two
keys over the changed items rather than by re-screening the export.

The other half of that finding lives in `context.stub_of`: with the items
corrupted and `roots.jsonl` stale, the twin relation goes *asymmetric* and the
screen reports a guard that is not true. The population index is derived from the
corrupted world, never carried over from the clean one.
"""

from collections.abc import Sequence

from shelfwarden.compare import SCREEN_POLICY, Policy, compare_person_name, fold_text
from shelfwarden.models.item import ItemStub, MediaKind, NormalizedItem

# The kinds `no_title_year_twin` is about, from `screen.PREDICATE_KINDS`. Kept as
# a named constant so the two can be asserted equal rather than assumed so.
TWIN_KINDS: frozenset[MediaKind] = frozenset({MediaKind.MOVIE, MediaKind.SHOW})


def _title_year_key(
    section_id: str, media_kind: MediaKind, title: str, year: int | None
) -> tuple[str, str, str, str]:
    """The key `screen.PopulationIndex` groups on, spelled out here so the two
    cannot drift apart silently."""
    return (section_id, str(media_kind), fold_text(title), "" if year is None else str(year))


def collateral_ids(
    roots: Sequence[ItemStub],
    family_ids: frozenset[str],
    corrupted: Sequence[NormalizedItem],
    policy: Policy = SCREEN_POLICY,
) -> tuple[str, ...]:
    """Population members outside this family whose guard the corruption moved.

    Two predicates, one pass each:

    * `no_title_year_twin` -- a corrupted item that now folds to the same
      `(section, kind, title, year)` as some other root has made that root a twin.
    * `no_author_name_twin` -- an added or renamed author that compares equal to
      an existing one under `compare_person_name` has made that author a twin.

    Returns ids sorted, so the field is a function of the data rather than of
    iteration order.
    """
    outside = [stub for stub in roots if str(stub.item_id) not in family_ids]

    by_title_year: dict[tuple[str, str, str, str], list[str]] = {}
    authors: list[ItemStub] = []
    for stub in outside:
        if stub.media_kind in TWIN_KINDS:
            key = _title_year_key(stub.item_id.section_id, stub.media_kind, stub.title, stub.year)
            by_title_year.setdefault(key, []).append(str(stub.item_id))
        elif stub.media_kind is MediaKind.AUTHOR:
            authors.append(stub)

    found: set[str] = set()
    for item in corrupted:
        if item.media_kind in TWIN_KINDS:
            key = _title_year_key(
                item.item_id.section_id,
                item.media_kind,
                item.title,
                getattr(item, "year", None),
            )
            found.update(by_title_year.get(key, ()))
        elif item.media_kind is MediaKind.AUTHOR:
            for stub in authors:
                if stub.item_id.section_id != item.item_id.section_id:
                    continue
                if policy.satisfied_by(compare_person_name(stub.title, item.title)):
                    found.add(str(stub.item_id))

    return tuple(sorted(found))


__all__ = ["TWIN_KINDS", "collateral_ids"]
