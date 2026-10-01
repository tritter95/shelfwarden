"""The shape of a library: which kinds hang beneath which.

Show -> season -> episode and author -> audiobook -> audiobook part are the same
three-level tree -- step 0.2 added `author` for exactly that symmetry -- and a movie
is a tree of one. The export walks it, the Plex adapter maps sections onto it, the
snapshot provider serves it, and step 0.7's integrity rules check worlds against
it. One table here, rather than a copy in each.

A leaf beside `item.py`: it knows `MediaKind` and nothing else.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from shelfwarden.models.item import MediaKind, NormalizedItem

CHILD_KIND: dict[MediaKind, MediaKind] = {
    MediaKind.SHOW: MediaKind.SEASON,
    MediaKind.SEASON: MediaKind.EPISODE,
    MediaKind.AUTHOR: MediaKind.AUDIOBOOK,
    MediaKind.AUDIOBOOK: MediaKind.AUDIOBOOK_PART,
}

# Derived rather than declared: a second hand-written table is a second place to
# be wrong. Inverting is only sound while no two kinds share a child, which a test
# pins -- a comprehension would otherwise drop one silently.
PARENT_KIND: dict[MediaKind, MediaKind] = {child: parent for parent, child in CHILD_KIND.items()}


def lineage(kind: MediaKind) -> tuple[MediaKind, ...]:
    """A kind and every kind beneath it, top-down: `SHOW -> (SHOW, SEASON, EPISODE)`."""
    kinds = [kind]
    while kinds[-1] in CHILD_KIND:
        kinds.append(CHILD_KIND[kinds[-1]])
    return tuple(kinds)


# -- structure -----------------------------------------------------------


class Rule(StrEnum):
    """What a set of records must satisfy to be a library Plex could serve.

    Facts about the records alone, so they hold for a whole export and for one
    family of it. That second use is what puts them here rather than in the
    snapshot provider: step 0.7.5 checks a corrupted family against them before it
    can ship.
    """

    DUPLICATE_ID = "duplicate_id"
    MISSING_PARENT = "missing_parent"
    ORPHAN = "orphan"
    PARENT_KIND = "parent_kind"
    PARENT_SECTION = "parent_section"
    GRANDPARENT = "grandparent"
    SHARED_PART_ID = "shared_part_id"


@dataclass(frozen=True, order=True, slots=True)
class Violation:
    """One broken rule. `subject` is the id it was found on, as a string."""

    rule: str
    subject: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.subject}: {self.detail}"


def structural_violations(records: Sequence[NormalizedItem]) -> tuple[Violation, ...]:
    """Every way these records fail to be a tree Plex could serve, sorted.

    All of them rather than the first, because a corruption recipe that breaks the
    tree tends to break it in several places, and fixing one at a time is a loop.

    * an id appears once;
    * a kind that has a parent kind has a parent, and it is in the set, of that
      kind, in the same section;
    * a grandparent is the parent's parent -- Plex derives it from the parent,
      so the two cannot disagree in anything a server returns;
    * no `part_id` is on two items. Plex's part ids are server-global, and a part
      id is how a repair names which file it changed.
    """
    found: list[Violation] = []
    by_id: dict[str, NormalizedItem] = {}
    for record in records:
        key = str(record.item_id)
        if key in by_id:
            found.append(Violation(Rule.DUPLICATE_ID, key, "this id names two records"))
        by_id[key] = record

    for key, record in by_id.items():
        found.extend(_parent_violations(key, record, by_id))

    owners: dict[str, list[str]] = {}
    for key, record in by_id.items():
        for part in getattr(record, "parts", ()):
            if part.part_id is not None:
                owners.setdefault(part.part_id, []).append(key)
    for part_id, keys in owners.items():
        if len(keys) > 1:
            found.append(Violation(Rule.SHARED_PART_ID, part_id, f"on {', '.join(sorted(keys))}"))
    return tuple(sorted(found))


def _parent_violations(
    key: str, record: NormalizedItem, by_id: dict[str, NormalizedItem]
) -> Iterable[Violation]:
    expected = PARENT_KIND.get(record.media_kind)
    if expected is None:
        return
    parent_id = getattr(record, "parent", None)
    if parent_id is None:
        yield Violation(Rule.MISSING_PARENT, key, f"a {record.media_kind} needs a {expected}")
        return
    parent = by_id.get(str(parent_id))
    if parent is None:
        yield Violation(Rule.ORPHAN, key, f"parent {parent_id} is not in the set")
        return
    if parent.media_kind is not expected:
        yield Violation(
            Rule.PARENT_KIND, key, f"parent {parent_id} is a {parent.media_kind}, not a {expected}"
        )
    if parent_id.section_id != record.item_id.section_id:
        yield Violation(Rule.PARENT_SECTION, key, f"parent {parent_id} is in another section")
    if "grandparent" in type(record).model_fields:
        grandparent = getattr(record, "grandparent", None)
        derived = getattr(parent, "parent", None)
        if grandparent != derived:
            yield Violation(
                Rule.GRANDPARENT,
                key,
                f"grandparent {grandparent}, but its parent's parent is {derived}",
            )


__all__ = [
    "CHILD_KIND",
    "PARENT_KIND",
    "Rule",
    "Violation",
    "lineage",
    "structural_violations",
]
