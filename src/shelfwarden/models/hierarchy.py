"""The shape of a library: which kinds hang beneath which.

Show -> season -> episode and author -> audiobook -> audiobook part are the same
three-level tree -- step 0.2 added `author` for exactly that symmetry -- and a movie
is a tree of one. The export walks it, the Plex adapter maps sections onto it, the
snapshot provider serves it, and step 0.7's integrity rules check worlds against
it. One table here, rather than a copy in each.

A leaf beside `item.py`: it knows `MediaKind` and nothing else.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from shelfwarden.models.ids import ItemId
from shelfwarden.models.item import MediaKind, NormalizedItem, with_changes

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
    DERIVED_COPY = "derived_copy"


@dataclass(frozen=True, order=True, slots=True)
class Violation:
    """One broken rule. `subject` is the id it was found on, as a string; `path`
    is the field, for a rule about one field of an item."""

    rule: str
    subject: str
    detail: str
    path: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        """What makes two violations the same one, whatever values they report."""
        return (self.rule, self.subject, self.path)

    def __str__(self) -> str:
        return f"{self.rule}: {self.subject}{self.path}: {self.detail}"


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


# -- derived copies ------------------------------------------------------------


class Tree:
    """Records indexed by id and by parent: what a derivation reads."""

    def __init__(self, records: Iterable[NormalizedItem]) -> None:
        self.by_id: dict[str, NormalizedItem] = {}
        self.children: dict[str, list[NormalizedItem]] = {}
        for record in records:
            self.by_id[str(record.item_id)] = record
            parent = getattr(record, "parent", None)
            if parent is not None:
                self.children.setdefault(str(parent), []).append(record)

    def parent(self, record: NormalizedItem | None) -> NormalizedItem | None:
        parent = getattr(record, "parent", None) if record is not None else None
        return self.by_id.get(str(parent)) if parent is not None else None

    def children_of(self, record: NormalizedItem) -> list[NormalizedItem]:
        return self.children.get(str(record.item_id), [])


@dataclass(frozen=True, slots=True)
class DerivedCopy:
    """A field Plex computes from the hierarchy when it answers, never stores.

    `derive` returns the value the hierarchy gives, or `None` when it gives none --
    a parent that is not in the set. A `None` *copy* means "not reported", and is
    never a disagreement.
    """

    field: str
    kinds: frozenset[MediaKind]
    derive: Callable[[NormalizedItem, Tree], object]

    @property
    def path(self) -> str:
        return f"/{self.field}"


def _parent_title(record: NormalizedItem, tree: Tree) -> object:
    parent = tree.parent(record)
    return parent.title if parent is not None else None


def _grandparent(record: NormalizedItem, tree: Tree) -> object:
    parent = tree.parent(record)
    return getattr(parent, "parent", None) if parent is not None else None


def _grandparent_title(record: NormalizedItem, tree: Tree) -> object:
    grandparent = tree.parent(tree.parent(record))
    return grandparent.title if grandparent is not None else None


def _child_count(record: NormalizedItem, tree: Tree) -> object:
    return len(tree.children_of(record))


def _leaf_count(record: NormalizedItem, tree: Tree) -> object:
    return sum(len(tree.children_of(child)) for child in tree.children_of(record))


# The fields Plex derives from the hierarchy -- a show's `childCount`, an episode's
# `grandparentTitle` -- which therefore cannot disagree with it in anything a server
# returns. A recipe that edits one record and leaves these stale on its neighbours
# builds a library no server could serve, and two of 0.5's did exactly that in a
# way that leaked the answer (step 0.7, Finding 5).
#
# `/parent_index` is absent on purpose: `evals/truth.py` treats it as the primary,
# hard field from which Plex re-derives `/parent`, and a table claiming the reverse
# would contradict that. `/grandparent` is also a structural rule; here it is the
# copy propagation repairs.
DERIVED_COPIES: tuple[DerivedCopy, ...] = (
    DerivedCopy(
        "parent_title",
        frozenset({MediaKind.SEASON, MediaKind.EPISODE, MediaKind.AUDIOBOOK}),
        _parent_title,
    ),
    DerivedCopy(
        "grandparent", frozenset({MediaKind.EPISODE, MediaKind.AUDIOBOOK_PART}), _grandparent
    ),
    DerivedCopy("grandparent_title", frozenset({MediaKind.EPISODE}), _grandparent_title),
    DerivedCopy("child_count", frozenset({MediaKind.SHOW}), _child_count),
    DerivedCopy("leaf_count", frozenset({MediaKind.SHOW}), _leaf_count),
    DerivedCopy("album_count", frozenset({MediaKind.AUTHOR}), _child_count),
    DerivedCopy("part_count", frozenset({MediaKind.AUDIOBOOK}), _child_count),
)

DERIVED_PATHS: frozenset[str] = frozenset(copy.path for copy in DERIVED_COPIES)


def derived_violations(records: Sequence[NormalizedItem]) -> tuple[Violation, ...]:
    """Every derived copy that disagrees with the hierarchy it copies, sorted.

    Absolute, so it is rarely the question. A real export may carry a
    disagreement of its own, and a world must not be held to a coherence its
    source never had. The question is usually `newly_violated`: what a change
    *introduced*.
    """
    tree = Tree(records)
    found: list[Violation] = []
    for record in records:
        for copy in DERIVED_COPIES:
            if record.media_kind not in copy.kinds:
                continue
            held = getattr(record, copy.field)
            expected = copy.derive(record, tree)
            if held is None or expected is None or held == expected:
                continue
            found.append(
                Violation(
                    Rule.DERIVED_COPY,
                    str(record.item_id),
                    f"is {held!r}, but the hierarchy gives {expected!r}",
                    path=copy.path,
                )
            )
    return tuple(sorted(found))


def newly_violated(
    after: Iterable[Violation], before: Iterable[Violation]
) -> tuple[Violation, ...]:
    """The violations in `after` that `before` did not already have.

    Compared by `Violation.key` -- rule, subject, field -- not by detail. A count
    that was wrong before and is differently wrong now is the same inherited
    disagreement, not a new one.
    """
    known = {violation.key for violation in before}
    return tuple(sorted(v for v in after if v.key not in known))


def propagate(
    before: Sequence[NormalizedItem], after: Sequence[NormalizedItem]
) -> tuple[tuple[NormalizedItem, ...], tuple[tuple[str, str], ...]]:
    """Bring the derived copies a change made stale back in line with the hierarchy.

    Returns the records and the `(item_id, path)` pairs it set. A copy is set when:

    * its item existed `before`, and the copy agreed with the hierarchy there and
      disagrees with it now. The change made it stale, so the change's world must
      carry the fresh value, or it is a world no server could serve;
    * its item is new in `after`, and the copy is reported. Plex computes these
      for an item it has just added.

    Anything else is left alone. A disagreement the `before` set already had is a
    fact about the source, and a delta built from this must describe the change
    and nothing else.

    One pass suffices: every derivation reads primary fields only (`title`,
    `parent`, and which children exist), never another derived copy.
    """
    old_tree, new_tree = Tree(before), Tree(after)
    result: list[NormalizedItem] = []
    touched: list[tuple[str, str]] = []
    for record in after:
        key = str(record.item_id)
        original = old_tree.by_id.get(key)
        updates: dict[str, object] = {}
        for copy in DERIVED_COPIES:
            if record.media_kind not in copy.kinds:
                continue
            held = getattr(record, copy.field)
            fresh = copy.derive(record, new_tree)
            if held is None or fresh is None or held == fresh:
                continue
            if original is not None:
                was = getattr(original, copy.field)
                if was is None or was != copy.derive(original, old_tree):
                    continue
            updates[copy.field] = _as_json(fresh)
            touched.append((key, copy.path))
        result.append(with_changes(record, updates) if updates else record)
    return tuple(result), tuple(sorted(touched))


def _as_json(value: object) -> object:
    if isinstance(value, ItemId):
        return {
            "provider": value.provider,
            "section_id": value.section_id,
            "rating_key": value.rating_key,
        }
    return value


__all__ = [
    "CHILD_KIND",
    "DERIVED_COPIES",
    "DERIVED_PATHS",
    "PARENT_KIND",
    "DerivedCopy",
    "Rule",
    "Tree",
    "Violation",
    "derived_violations",
    "lineage",
    "newly_violated",
    "propagate",
    "structural_violations",
]
