"""Corruptions whose subject is an author, and whose evidence is a directory.

Audiobooks in Plex live in an `artist` section: an author is an Artist, a book is
an Album, and a book's files are Tracks. Nothing in that structure records a
series, a narrator, or which of three spellings of a name is the real one -- which
is why these four classes exist, and why three of the four take their witness
from the **path** rather than from a field.

That is what `parse_release_name` could not see: it reads the basename, and on
`.../The Stormlight Archive/The Way of Kings/CD1.m4b` the basename parses to
`CD1` while the directories carry the series and the book. `find_in_path` and
`path_segments` (step 0.5.2) are here for these classes.

`narrator_as_author` is not here. It needs a real narrator's name as an
ingredient, and taking another author's name from the export produces
`author_name_variant` wearing a different label; see
`registry.UNSYNTHESIZABLE_REASON`.
"""

import re
from collections.abc import Sequence

from shelfwarden.compare import (
    Policy,
    compare_person_name,
    compare_series_position,
    compare_title,
    find_in_path,
    fold_text,
    path_segments,
)
from shelfwarden.evals import export as export_module
from shelfwarden.evals.corrupt.context import (
    CorruptionContext,
    PartRef,
    parts_in,
    pick_by_rank,
    subject_key,
)
from shelfwarden.evals.corrupt.model import Rejection
from shelfwarden.evals.corrupt.registry import Applicability, Mutation, corruption
from shelfwarden.evals.corrupt.witness import LocalWitness, WitnessKind, WitnessTier
from shelfwarden.models.finding import ProblemClass
from shelfwarden.models.item import MediaKind, NormalizedItem, dump_item, load_item, with_changes

# How a scrambled book announces its position. One variant each, because the
# whole point of the class is that a library uses these markers inconsistently.
SERIES_MARKERS: dict[str, str] = {
    "book_marker": "Book {position}",
    "part_marker": "Part {position}",
    "hash_marker": "#{position}",
    "no_marker": "",
}

# How one author's name gets split into several. `inverted` is the important one:
# `compare_person_name` returns ALIAS for it via `token_set`, which is a
# structural equivalence rather than a similarity score -- the class's guard is
# therefore not threshold-dependent, which is the one shape spec §3 forbids.
NAME_VARIANTS: dict[str, tuple[str, ...]] = {
    "inverted": ("{last}, {first}",),
    "double_space": ("{first}  {last}",),
    "inverted_and_spaced": ("{last}, {first}", "{first}  {last}"),
}


_DISC_SUFFIX = re.compile(
    r"[\s._-]*(?:cd|disc|disk|part|pt|vol(?:ume)?)[\s._-]*\d+$", re.IGNORECASE
)


def _without_disc_marker(title: str) -> str:
    """`"The Way of Kings CD2"` -> `"The Way of Kings"`."""
    return _DISC_SUFFIX.sub("", title).strip()


def _books(family: export_module.Family) -> list[NormalizedItem]:
    return [record for record in family.records if record.media_kind is MediaKind.AUDIOBOOK]


def _book_parts(family: export_module.Family, book: NormalizedItem) -> list[NormalizedItem]:
    return [
        record
        for record in family.records
        if record.media_kind is MediaKind.AUDIOBOOK_PART
        and getattr(record, "parent", None) == book.item_id
    ]


def _replace(
    family: export_module.Family, updates: dict[str, dict[str, object]]
) -> list[NormalizedItem]:
    return [
        with_changes(record, updates[str(record.item_id)])
        if str(record.item_id) in updates
        else record
        for record in family.records
    ]


def _first_part_ref(family: export_module.Family, item: NormalizedItem) -> PartRef | None:
    for ref in parts_in(family):
        if ref.item_id == str(item.item_id):
            return ref
    return None


def _split_name(name: str) -> tuple[str, str] | None:
    words = name.split()
    if len(words) < 2:
        return None
    return " ".join(words[:-1]), words[-1]


# -- series_order_broken --------------------------------------------------


def _positioned_books(family: export_module.Family) -> list[NormalizedItem]:
    return [book for book in _books(family) if book.series and book.series_position]


def _position_marker(position: str) -> re.Pattern[str]:
    """A path segment that names this position, as a token rather than a substring.

    `"1" in "CD1"` is true and means nothing -- it would let a disc marker stand in
    as a series position and produce a case whose witness proves the wrong thing.
    The number has to appear as its own token, optionally behind a `book`/`part`/
    `vol`/`#` word.
    """
    escaped = re.escape(position)
    return re.compile(
        rf"(?:^|[^0-9a-z])(?:(?:book|part|vol(?:ume)?|#)[\s._-]*)?0*{escaped}(?![0-9])",
        re.IGNORECASE,
    )


def _path_refs(family: export_module.Family, book: NormalizedItem) -> list[PartRef]:
    """Every file belonging to a book, its own and its parts'."""
    owned = {str(book.item_id)} | {str(part.item_id) for part in _book_parts(family, book)}
    return [ref for ref in parts_in(family) if ref.item_id in owned]


def _position_ref(
    family: export_module.Family, book: NormalizedItem, position: str
) -> tuple[PartRef, str] | None:
    """The file whose path names this position, and the segment that does it."""
    marker = _position_marker(position)
    for ref in _path_refs(family, book):
        for segment in path_segments(ref.path):
            if marker.search(segment):
                return ref, segment
    return None


def _series_order_applicable(family: export_module.Family, ctx: CorruptionContext) -> Applicability:
    books = _positioned_books(family)
    if len(books) < 3:
        return Applicability.no(
            "too_few_in_series",
            "a scrambled position is only detectable against intact siblings; "
            "fewer than three positioned books in this author",
        )
    if not any(_position_ref(family, book, book.series_position) for book in books):
        return Applicability.no(
            "no_position_in_path",
            "no book's files name its position, so nothing outside the metadata "
            "records the running order",
        )
    return Applicability.yes()


@corruption(
    ProblemClass.SERIES_ORDER_BROKEN,
    applies_to={MediaKind.AUTHOR},
    variants=tuple(SERIES_MARKERS),
    witness=WitnessKind.VALUE,
    tier=WitnessTier.LOCAL,
    applicable=_series_order_applicable,
)
def series_order_broken(
    family: export_module.Family, ctx: CorruptionContext
) -> Mutation | Rejection:
    """Scramble one book's place in its series and rewrite its marker inconsistently."""
    candidates = [
        book
        for book in _positioned_books(family)
        if _position_ref(family, book, book.series_position)
    ]
    book = pick_by_rank(ctx.seed, [(subject_key(item), item) for item in candidates])
    if book is None:  # pragma: no cover -- applicability already checked
        return ctx.reject("no_position_in_path")

    taken = {other.series_position for other in _books(family) if other.series_position}
    wrong = next(
        (str(n) for n in range(1, 99) if str(n) not in taken and str(n) != book.series_position),
        None,
    )
    if wrong is None:  # pragma: no cover -- 99 books in one series
        return ctx.reject("no_free_position")

    marker = SERIES_MARKERS[ctx.variant].format(position=wrong)
    stripped = book.title.split(" - ")[-1].strip()
    retitled = f"{marker} - {stripped}" if marker else stripped

    mutated = _replace(
        family,
        {
            str(book.item_id): {
                "series_position": wrong,
                "title": retitled,
                "title_sort": retitled,
                "index": int(wrong),
            }
        },
    )

    located = _position_ref(family, book, book.series_position)
    if located is None:  # pragma: no cover -- candidate list already filtered
        return ctx.reject("no_position_in_path")
    ref, resolved = located
    witness = LocalWitness.over(ctx.export_id, mutated).value(
        subject_id=ref.item_id,
        pointer=ref.pointer,
        comparator="compare_series_position",
        resolved=resolved,
        against_truth=compare_series_position(book.series_position, book.series_position),
        against_corrupted=compare_series_position(book.series_position, wrong),
        policy=ctx.policy,
    )
    return Mutation(items=tuple(mutated), witness=witness)


# -- author_name_variant --------------------------------------------------


def _name_variant_applicable(family: export_module.Family, ctx: CorruptionContext) -> Applicability:
    author = family.records[0]
    if _split_name(author.title) is None:
        return Applicability.no(
            "single_word_name", f"{author.title!r} has no first and last name to invert"
        )
    if len(_books(family)) < 2:
        return Applicability.no(
            "too_few_books", "splitting an author needs at least two books to split between"
        )
    return Applicability.yes()


@corruption(
    ProblemClass.AUTHOR_NAME_VARIANT,
    applies_to={MediaKind.AUTHOR},
    variants=tuple(NAME_VARIANTS),
    witness=WitnessKind.RELATION,
    tier=WitnessTier.LOCAL,
    applicable=_name_variant_applicable,
)
def author_name_variant(
    family: export_module.Family, ctx: CorruptionContext
) -> Mutation | Rejection:
    """Split one author into two or three spellings and share the books between them."""
    author = family.records[0]
    split = _split_name(author.title)
    if split is None:  # pragma: no cover -- applicability already checked
        return ctx.reject("single_word_name")
    first, last = split

    books = sorted(_books(family), key=lambda book: str(book.item_id))
    spellings = [template.format(first=first, last=last) for template in NAME_VARIANTS[ctx.variant]]

    added: list[NormalizedItem] = []
    updates: dict[str, dict[str, object]] = {}
    for ordinal, spelling in enumerate(spellings):
        document = dump_item(author)
        document["item_id"] = {
            "provider": author.item_id.provider,
            "section_id": author.item_id.section_id,
            "rating_key": ctx.mint(ordinal),
        }
        document["title"] = spelling
        document["title_sort"] = spelling
        # Locks belong to the record Plex actually has; a minted variant has none.
        document["locked_fields"] = []
        variant = load_item(document)
        added.append(variant)

        moved = books[ordinal + 1 :: len(spellings) + 1]
        for book in moved:
            updates[str(book.item_id)] = {
                "parent": document["item_id"],
                "parent_title": spelling,
            }
        document["album_count"] = len(moved)
        added[-1] = load_item(document)

    if not updates:
        return ctx.reject(
            "no_books_moved", "every book stayed with the original author; nothing was split"
        )

    remaining = len(books) - len(updates)
    updates[str(author.item_id)] = {"album_count": remaining}
    mutated = tuple([*_replace(family, updates), *added])

    supports = [compare_person_name(variant.title, author.title) for variant in added]
    witness = LocalWitness.over(ctx.export_id, mutated).relation(
        subject_ids=(str(author.item_id), *(str(variant.item_id) for variant in added)),
        relation="same_author",
        pointers=("/title", *("/title" for _ in added)),
        comparator="compare_person_name",
        supports=supports,
        policy=ctx.policy,
    )
    return Mutation(items=mutated, witness=witness)


# -- multi_file_split -----------------------------------------------------


def _shared_directory(paths: Sequence[str]) -> str | None:
    """The parent directory every file shares, if they share one."""
    parents = {tuple(path_segments(path)[:-1]) for path in paths}
    if len(parents) != 1:
        return None
    (only,) = parents
    return only[-1] if only else None


def _splittable_books(family: export_module.Family) -> list[NormalizedItem]:
    found = []
    for book in _books(family):
        parts = _book_parts(family, book)
        if len(parts) < 2:
            continue
        owned = {str(part.item_id) for part in parts}
        paths = [ref.path for ref in parts_in(family) if ref.item_id in owned]
        if len(paths) < 2 or _shared_directory(paths) is None:
            continue
        found.append(book)
    return found


def _multi_file_applicable(family: export_module.Family, ctx: CorruptionContext) -> Applicability:
    if not _splittable_books(family):
        return Applicability.no(
            "no_multi_part_book",
            "no book has two or more files sharing a directory, so nothing would "
            "prove the pieces belong together",
        )
    return Applicability.yes()


@corruption(
    ProblemClass.MULTI_FILE_SPLIT,
    applies_to={MediaKind.AUTHOR},
    variants=("cd_markers",),
    witness=WitnessKind.RELATION,
    tier=WitnessTier.LOCAL,
    applicable=_multi_file_applicable,
)
def multi_file_split(family: export_module.Family, ctx: CorruptionContext) -> Mutation | Rejection:
    """Split one book's files into separate albums, `… CD1` and `… CD2`."""
    book = pick_by_rank(ctx.seed, [(subject_key(item), item) for item in _splittable_books(family)])
    if book is None:  # pragma: no cover -- applicability already checked
        return ctx.reject("no_multi_part_book")

    parts = sorted(_book_parts(family, book), key=lambda part: (part.index or 0, str(part.item_id)))
    added: list[NormalizedItem] = []
    updates: dict[str, dict[str, object]] = {
        str(book.item_id): {"title": f"{book.title} CD1", "part_count": 1}
    }

    for ordinal, part in enumerate(parts[1:], start=2):
        document = dump_item(book)
        document["item_id"] = {
            "provider": book.item_id.provider,
            "section_id": book.item_id.section_id,
            "rating_key": ctx.mint(ordinal),
        }
        document["title"] = f"{book.title} CD{ordinal}"
        document["title_sort"] = document["title"]
        document["part_count"] = 1
        document["locked_fields"] = []
        piece = load_item(document)
        added.append(piece)
        updates[str(part.item_id)] = {"parent": document["item_id"], "index": 1}

    if not added:
        return ctx.reject("nothing_to_split", "the book has only one file after all")

    mutated = tuple([*_replace(family, updates), *added])
    owned = {str(part.item_id) for part in parts}
    directory = _shared_directory([ref.path for ref in parts_in(family) if ref.item_id in owned])

    # Not `compare_title(piece.title, book.title)`: after the split the two titles
    # are `… CD1` and `… CD2`, which land at FUZZY and prove nothing. The relation
    # is that the disc markers strip to one title *and* that one folder still holds
    # every file -- the second half being what a disc marker alone cannot show.
    supports = [
        compare_title(_without_disc_marker(piece.title), _without_disc_marker(book.title))
        for piece in added
    ]
    if directory is not None:
        supports.append(compare_title(directory, _without_disc_marker(book.title)))

    witness = LocalWitness.over(ctx.export_id, mutated).relation(
        subject_ids=(str(book.item_id), *(str(piece.item_id) for piece in added)),
        relation="same_book",
        pointers=("/title", *("/title" for _ in added)),
        comparator="compare_title",
        supports=supports,
        policy=ctx.policy,
    )
    return Mutation(items=mutated, witness=witness)


# -- missing_series -------------------------------------------------------


def _series_books(
    family: export_module.Family, policy: Policy
) -> list[tuple[NormalizedItem, PartRef]]:
    """Books whose series name genuinely appears in a path segment.

    The test is the **policy**, not a non-NONE support. `find_in_path` applies no
    threshold of its own -- a path always offers some fuzzy noise, and `Sanderson`
    against `The Stormlight Archive` scores well above zero -- so a caller that
    treats any support as a hit has skipped the policy and will build a witness
    that cannot discriminate.
    """
    found: list[tuple[NormalizedItem, PartRef]] = []
    for book in _books(family):
        if not book.series:
            continue
        for ref in _path_refs(family, book):
            if policy.satisfied_by(find_in_path(ref.path, book.series)):
                found.append((book, ref))
                break
    return found


def _missing_series_applicable(
    family: export_module.Family, ctx: CorruptionContext
) -> Applicability:
    if not _series_books(family, ctx.policy):
        return Applicability.no(
            "no_series_in_path",
            "no book records a series that its files also name, so stripping it "
            "would leave nothing to recover it from",
        )
    return Applicability.yes()


@corruption(
    ProblemClass.MISSING_SERIES,
    applies_to={MediaKind.AUTHOR},
    variants=("strip_series",),
    witness=WitnessKind.VALUE,
    tier=WitnessTier.LOCAL,
    applicable=_missing_series_applicable,
)
def missing_series(family: export_module.Family, ctx: CorruptionContext) -> Mutation | Rejection:
    """Strip a book's series membership from its metadata. The folder still knows."""
    candidates = _series_books(family, ctx.policy)
    chosen = pick_by_rank(ctx.seed, [(subject_key(book), (book, ref)) for book, ref in candidates])
    if chosen is None:  # pragma: no cover -- applicability already checked
        return ctx.reject("no_series_in_path")
    book, ref = chosen

    series = book.series
    folded = fold_text(series)
    title = book.title
    for separator in (" - ", ": "):
        head, found, tail = title.partition(separator)
        if found and fold_text(head).startswith(folded):
            title = tail.strip()
            break

    updates: dict[str, object] = {"series": None, "series_position": None}
    if title != book.title:
        updates["title"] = title
        updates["title_sort"] = title
    mutated = _replace(family, {str(book.item_id): updates})

    support = find_in_path(ref.path, series)
    witness = LocalWitness.over(ctx.export_id, mutated).value(
        subject_id=ref.item_id,
        pointer=ref.pointer,
        comparator="find_in_path",
        resolved=support.matched,
        against_truth=support,
        # Nothing is left in the metadata for the evidence to agree with, which is
        # precisely the corruption: an absent series cannot be supported.
        against_corrupted=compare_title(support.matched, None),
        policy=ctx.policy,
    )
    return Mutation(items=tuple(mutated), witness=witness)


__all__ = [
    "NAME_VARIANTS",
    "SERIES_MARKERS",
    "author_name_variant",
    "missing_series",
    "multi_file_split",
    "series_order_broken",
]
