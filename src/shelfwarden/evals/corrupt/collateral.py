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

**A guard moves in two directions, and step 0.5 recorded only one.** Found in
step 0.6, Finding 1, by re-screening the whole corrupted world per case and
diffing every item's verdict against the clean world. Four cases had a verdict
change outside `family | collateral`:

    wrong_match          fake:1:108   leaked: ['fake:1:107']
    filename_unmatchable fake:1:107   leaked: ['fake:1:108']
    ...

`fake:1:107` and `fake:1:108` are the two Blade Runner entries -- a genuine
title/year twin pair, both *failing* `no_title_year_twin` in the clean world.
Corrupting one gives it a different title, so the other stops having a twin and
its verdict *improves*, from `failed` to `guarded`. The original pass looked only
for population members that now share the corrupted item's key: it detected a
guard newly **broken** and missed a guard newly **granted**.

A newly granted guard is the more insidious of the two, because it is a claim
that is true only inside one case's world. So there is a second, symmetric pass
over the family's *pre-corruption* keys, taken from the clean population index.

The other half of the 0.5 finding lives in `context.stub_of`: with the items
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


def _stub_key(stub: ItemStub) -> tuple[str, str, str, str]:
    return _title_year_key(stub.item_id.section_id, stub.media_kind, stub.title, stub.year)


def _item_key(item: NormalizedItem) -> tuple[str, str, str, str]:
    return _title_year_key(
        item.item_id.section_id, item.media_kind, item.title, getattr(item, "year", None)
    )


def collateral_ids(
    roots: Sequence[ItemStub],
    family_ids: frozenset[str],
    corrupted: Sequence[NormalizedItem],
    policy: Policy = SCREEN_POLICY,
) -> tuple[str, ...]:
    """Population members outside this family whose guard the corruption moved.

    Two predicates and two directions, so four comparisons in all:

    * `no_title_year_twin` -- an outside root that **now** folds to the same
      `(section, kind, title, year)` as a corrupted item has newly *become* a twin;
      an outside root that shared the family's **pre-corruption** key may have
      newly *stopped* being one.
    * `no_author_name_twin` -- the same pair, under `compare_person_name`: an
      added or renamed author that compares equal to an existing one, and an
      existing one that compared equal to the family's clean author name.

    Both directions read the *clean* keys from `roots`, which is the population
    index of the world before this corruption. That is why no clean copy of the
    family is a parameter: `roots` already holds one stub per root, and only root
    kinds are population-scoped.

    The backward pass **over-reports**, deliberately. An outside item that shared
    the old key and still has another twin has no verdict change at all, and
    deciding that exactly would mean deriving the population index twice per case.
    The property this field is asserted against is a subset -- the set of items
    whose screen verdict moves is contained in `family | collateral` -- so an
    over-approximation satisfies it. What it costs is a little scorer leniency (a
    finding on an over-reported id in that case's world is excused rather than
    counted) and never the direction the project has forbidden.

    Returns ids sorted, so the field is a function of the data rather than of
    iteration order.
    """
    outside = [stub for stub in roots if str(stub.item_id) not in family_ids]
    inside = [stub for stub in roots if str(stub.item_id) in family_ids]

    by_title_year: dict[tuple[str, str, str, str], list[str]] = {}
    authors: list[ItemStub] = []
    for stub in outside:
        if stub.media_kind in TWIN_KINDS:
            by_title_year.setdefault(_stub_key(stub), []).append(str(stub.item_id))
        elif stub.media_kind is MediaKind.AUTHOR:
            authors.append(stub)

    found: set[str] = set()

    def _match(key: tuple[str, str, str, str]) -> None:
        found.update(by_title_year.get(key, ()))

    def _match_author(section_id: str, name: str) -> None:
        for stub in authors:
            if stub.item_id.section_id != section_id:
                continue
            if policy.satisfied_by(compare_person_name(stub.title, name)):
                found.add(str(stub.item_id))

    # Forward: a guard newly broken. The corrupted item's key is now shared.
    for item in corrupted:
        if item.media_kind in TWIN_KINDS:
            _match(_item_key(item))
        elif item.media_kind is MediaKind.AUTHOR:
            _match_author(item.item_id.section_id, item.title)

    # Backward: a guard newly granted. Someone shared the key this family used to
    # have, and may no longer share it with anything.
    for stub in inside:
        if stub.media_kind in TWIN_KINDS:
            _match(_stub_key(stub))
        elif stub.media_kind is MediaKind.AUTHOR:
            _match_author(stub.item_id.section_id, stub.title)

    return tuple(sorted(found))


__all__ = ["TWIN_KINDS", "collateral_ids"]
