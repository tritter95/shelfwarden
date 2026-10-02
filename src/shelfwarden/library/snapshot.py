"""A library that is a fixed set of records.

`SnapshotLibrary` implements `LibraryProvider` over records held in memory. In an
eval it is what the agent reads: step 0.7.6's world builder hands it one case's
corrupted world. It knows nothing of datasets, deltas, or truth -- `library/` may
not import `evals/`, by contract -- so the object the agent holds has no path to
the answer key.

Two properties need stating, because both are easy to lose.

**Where Plex decides, a named function decides.** The order of a listing, the
order of an item's children, the order of the sections, and what a title search
matches are all server behavior: plexapi has none of it, and no offline test can
check it. A snapshot still has to answer, so each answer is a pure function here
(`listing_key`, `children_key`, `section_key`, `title_matches`) rather than a sort
key written inline. That makes each one the *model* of Plex, which can be checked
twice: offline, the fake Plex server serves in these orders and the conformance
suite shows `PlexLibrary`'s client logic agrees; live, the same suite applies
them to what the real server returns. A disagreement there is a counterexample,
and the function changes.

**It refuses a world no Plex server could serve.** A record set with a dangling
parent, a grandparent that is not the parent's parent, or a rating key in two
sections is not a library, and serving one would measure the agent against
something it will never meet. Construction checks the structural rules
(`models.hierarchy.structural_violations`, and the library-level ones below) and
raises `WorldIntegrityError` listing every violation.

Edge behavior is the same as `PlexLibrary`'s, through the same checks in
`library.base`: §4.2 of `docs/plans/step-0.7-snapshot-provider.md` is the table.
The deliberate differences are declared there too. A snapshot lists only the
sections it was given, answers only the profile its records were fetched at, and
serves ids under its own provider label.
"""

from collections.abc import Sequence
from enum import StrEnum

from shelfwarden.compare import fold_text
from shelfwarden.library.base import (
    LIVE_PROVIDERS,
    SECTION_KINDS,
    SECTION_ROOT_KIND,
    LibraryItemNotFound,
    LibrarySectionNotFound,
    LibraryUnsupported,
    ProviderInfo,
    check_fetchable,
    check_page,
    check_search,
    resolve_kind,
)
from shelfwarden.models.hierarchy import CHILD_KIND, Violation, structural_violations
from shelfwarden.models.ids import ItemId, is_decimal, item_sort_key
from shelfwarden.models.item import (
    FetchProfile,
    FilePart,
    ItemStub,
    MediaKind,
    NormalizedItem,
    Page,
    SectionRef,
    stub_of,
)

PROVIDER = "snapshot"

# How many violations a `WorldIntegrityError` message spells out. The rest are
# counted in the message and all of them are on `.violations`: a capped list that
# did not say so would read as the whole story.
SHOWN_VIOLATIONS = 10


# -- the model of Plex ------------------------------------------------------


def listing_key(record: NormalizedItem) -> tuple[str, tuple[int, int, str]]:
    """The order of a listing: by sort title, then by rating key.

    Plex's default sort for a section's `/all` is its sort title; plexapi defaults
    `titleSort` to the title when the server sends none, so an export always
    carries one. Ties -- two entries for one film -- break by rating key, oldest
    first. Folded with `fold_text`, so case and Unicode form do not decide order.
    Both halves are assumptions until the live suite checks them.
    """
    return (fold_text(record.title_sort or record.title), item_sort_key(record.item_id))


def children_key(record: NormalizedItem) -> tuple[bool, int, tuple[int, int, str]]:
    """The order of an item's children: by index, unnumbered last, then by key."""
    index = getattr(record, "index", None)
    return (index is None, index if index is not None else 0, item_sort_key(record.item_id))


def section_key(section: SectionRef) -> tuple[int, int, str]:
    """The order of the sections: numerically by id, as Plex numbers them."""
    key = section.section_id
    return (0, int(key), "") if is_decimal(key) else (1, 0, key)


def title_matches(query: str, record: NormalizedItem) -> bool:
    """Whether a title search for `query` finds `record`.

    A case-insensitive substring of the title, compared under `fold_text`. This is
    the narrower claim. Whether Plex also folds accents, or matches `titleSort` or
    `originalTitle`, is what the live suite's probes are for -- and they would
    widen this, never the other way round.
    """
    return fold_text(query) in fold_text(record.title)


# -- the library-level rules ------------------------------------------------


class SnapshotRule(StrEnum):
    """What a record set must satisfy, beyond being a tree, to be served as one
    library. The tree rules are `models.hierarchy.Rule`."""

    LIVE_PROVIDER = "live_provider"
    PROVIDER_LABEL = "provider_label"
    DUPLICATE_SECTION = "duplicate_section"
    UNMODELLED_SECTION = "unmodelled_section"
    UNKNOWN_SECTION = "unknown_section"
    SECTION_KIND = "section_kind"
    DUPLICATE_RATING_KEY = "duplicate_rating_key"
    PROFILE = "profile"


class WorldIntegrityError(ValueError):
    """The records are not a library Plex could serve. Raised at construction.

    A `ValueError`, not a `LibraryError`: it is never raised to the agent, only to
    whatever built the world, and it means that code built it wrong. `note` is
    whatever the builder knows that the records do not: which case, and whether
    regenerating would help.
    """

    def __init__(self, violations: Sequence[Violation], note: str = "") -> None:
        self.violations = tuple(violations)
        shown = "\n".join(f"  {violation}" for violation in self.violations[:SHOWN_VIOLATIONS])
        hidden = len(self.violations) - SHOWN_VIOLATIONS
        tail = f"\n  ...and {hidden} more, all on .violations" if hidden > 0 else ""
        super().__init__(
            f"{len(self.violations)} way(s) these records are not a library Plex could "
            f"serve:\n{shown}{tail}" + (f"\n{note}" if note else "")
        )


def library_violations(
    records: Sequence[NormalizedItem],
    sections: Sequence[SectionRef],
    provider: str,
    profile: FetchProfile,
) -> tuple[Violation, ...]:
    """The rules about serving records as one library, sorted.

    * The provider label is not a live one. An export holds the user's real
      rating keys, so a snapshot under `plex` would make each of its addresses a
      live address too.
    * Every id, parent and grandparent carries the label, so nothing served
      points outside the snapshot.
    * Sections are unique and modelled, and each record's section exists and can
      hold its kind.
    * No rating key is in two sections. Plex's keys are server-global, which is
      also what lets a wrong-section id be answered with the right one.
    * Every record was fetched at the profile the snapshot claims to hold.
    """
    found: list[Violation] = []
    if provider in LIVE_PROVIDERS:
        found.append(
            Violation(
                SnapshotRule.LIVE_PROVIDER,
                provider,
                "a snapshot under a live label makes its addresses live ones",
            )
        )

    by_section: dict[str, SectionRef] = {}
    for section in sections:
        if section.section_id in by_section:
            found.append(
                Violation(SnapshotRule.DUPLICATE_SECTION, section.section_id, "listed twice")
            )
        by_section[section.section_id] = section
        if section.section_type not in SECTION_KINDS:
            found.append(
                Violation(
                    SnapshotRule.UNMODELLED_SECTION,
                    section.section_id,
                    f"{section.section_type!r} sections are not modelled",
                )
            )

    holders: dict[str, set[str]] = {}
    for record in records:
        subject = str(record.item_id)
        for ref in (
            record.item_id,
            getattr(record, "parent", None),
            getattr(record, "grandparent", None),
        ):
            if ref is not None and ref.provider != provider:
                found.append(
                    Violation(
                        SnapshotRule.PROVIDER_LABEL, subject, f"{ref} is not labelled {provider!r}"
                    )
                )
        section = by_section.get(record.item_id.section_id)
        if section is None:
            found.append(
                Violation(
                    SnapshotRule.UNKNOWN_SECTION, subject, "its section is not in the snapshot"
                )
            )
        elif record.media_kind not in SECTION_KINDS.get(section.section_type, ()):
            found.append(
                Violation(
                    SnapshotRule.SECTION_KIND,
                    subject,
                    f"a {section.section_type} section cannot hold a {record.media_kind}",
                )
            )
        if record.fetched is not profile:
            found.append(
                Violation(
                    SnapshotRule.PROFILE, subject, f"fetched at {record.fetched}, not {profile}"
                )
            )
        holders.setdefault(record.item_id.rating_key, set()).add(record.item_id.section_id)

    for rating_key, holding in holders.items():
        if len(holding) > 1:
            found.append(
                Violation(
                    SnapshotRule.DUPLICATE_RATING_KEY,
                    rating_key,
                    f"in sections {', '.join(sorted(holding))}",
                )
            )
    return tuple(sorted(found))


# -- the provider ----------------------------------------------------------


class SnapshotLibrary:
    """Read-only access to a fixed record set. Implements `LibraryProvider`.

    Its public methods are exactly the protocol's seven, which a test asserts. The
    records are frozen models, so they are returned as they are held -- no copy is
    needed, and none can be changed through what is returned.
    """

    def __init__(
        self,
        records: Sequence[NormalizedItem],
        sections: Sequence[SectionRef],
        info: ProviderInfo,
        profile: FetchProfile,
    ) -> None:
        check_fetchable(profile)
        violations = sorted(
            {
                *structural_violations(records),
                *library_violations(records, sections, info.provider, profile),
            }
        )
        if violations:
            raise WorldIntegrityError(violations)

        self._info = info
        self._profile = profile
        self._sections = {
            section.section_id: section for section in sorted(sections, key=section_key)
        }
        self._records = {str(record.item_id): record for record in records}
        self._by_rating_key = {record.item_id.rating_key: record for record in records}

        listings: dict[tuple[str, MediaKind], list[NormalizedItem]] = {}
        children: dict[str, list[NormalizedItem]] = {}
        for record in records:
            listings.setdefault((record.item_id.section_id, record.media_kind), []).append(record)
            parent = getattr(record, "parent", None)
            if parent is not None:
                children.setdefault(str(parent), []).append(record)
        self._listings = {
            key: tuple(sorted(found, key=listing_key)) for key, found in listings.items()
        }
        self._children = {
            key: tuple(sorted(found, key=children_key)) for key, found in children.items()
        }

    # -- identity and sections ----------------------------------------------

    def provider_info(self) -> ProviderInfo:
        return self._info

    def sections(self) -> tuple[SectionRef, ...]:
        """Only the sections the snapshot was given -- a declared difference from
        Plex, which also lists sections this project does not model."""
        return tuple(self._sections.values())

    # -- listing -------------------------------------------------------------

    def list_items(
        self,
        section_id: str,
        offset: int,
        limit: int,
        media_kind: MediaKind | None = None,
    ) -> Page[ItemStub]:
        check_page(offset, limit)
        section = self._section(section_id)
        kind = resolve_kind(section.section_type, media_kind)
        return _page(self._listings.get((section_id, kind), ()), offset, limit)

    def get_children(self, item_id: ItemId, offset: int, limit: int) -> Page[ItemStub]:
        check_page(offset, limit)
        record = self._record(item_id)
        if record.media_kind not in CHILD_KIND:
            return Page[ItemStub](items=(), total=0, offset=offset, returned=0)
        return _page(self._children.get(str(item_id), ()), offset, limit)

    def find_similar(self, section_id: str, title: str, limit: int) -> tuple[ItemStub, ...]:
        check_search(title, limit)
        section = self._section(section_id)
        roots = self._listings.get((section_id, SECTION_ROOT_KIND[section.section_type]), ())
        return tuple(stub_of(record) for record in roots if title_matches(title, record))[:limit]

    # -- items ---------------------------------------------------------------

    def get_item(
        self,
        item_id: ItemId,
        profile: FetchProfile = FetchProfile.CORE,
    ) -> NormalizedItem:
        """The record, at the one profile the snapshot holds.

        Another profile is refused rather than restamped. FULL maps no extra field
        today, but whether Plex omits a `Part` it cannot stat is unverified, so a
        record restamped in either direction could claim a fetch that would have
        returned something else.
        """
        check_fetchable(profile)
        record = self._record(item_id)
        if profile is not self._profile:
            raise LibraryUnsupported(
                f"this snapshot holds records fetched at {self._profile}; {profile} was "
                "never captured and cannot be made up. Retrying will not help."
            )
        return record

    def get_files(self, item_id: ItemId) -> tuple[FilePart, ...]:
        return tuple(getattr(self._record(item_id), "parts", ()))

    # -- lookups -------------------------------------------------------------

    def _section(self, section_id: str) -> SectionRef:
        section = self._sections.get(section_id)
        if section is None:
            raise LibrarySectionNotFound(f"no section with id {section_id!r}")
        return section

    def _record(self, item_id: ItemId) -> NormalizedItem:
        if item_id.provider != self._info.provider:
            raise LibraryItemNotFound(
                f"{item_id} belongs to provider {item_id.provider!r}, not {self._info.provider!r}"
            )
        record = self._records.get(str(item_id))
        if record is not None:
            return record
        located = self._by_rating_key.get(item_id.rating_key)
        if located is not None:
            raise LibraryItemNotFound(
                f"{item_id} names section {item_id.section_id!r}, but rating key "
                f"{item_id.rating_key} is in section {located.item_id.section_id!r}",
                next_action=f"use {located.item_id}",
            )
        raise LibraryItemNotFound(f"no item {item_id}")


def _page(records: Sequence[NormalizedItem], offset: int, limit: int) -> Page[ItemStub]:
    """One window of a pre-sorted listing. The arguments are already checked, so a
    slice here cannot be Python's negative-index answer to a negative offset."""
    window = records[offset : offset + limit]
    return Page[ItemStub](
        items=tuple(stub_of(record) for record in window),
        total=len(records),
        offset=offset,
        returned=len(window),
    )


__all__ = [
    "PROVIDER",
    "SHOWN_VIOLATIONS",
    "SnapshotLibrary",
    "SnapshotRule",
    "WorldIntegrityError",
    "children_key",
    "library_violations",
    "listing_key",
    "section_key",
    "title_matches",
]
