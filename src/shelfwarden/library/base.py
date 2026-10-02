"""The read-only library contract and its error taxonomy.

`LibraryProvider` is where spec §3.2 stops being a promise and becomes a type.
plexapi has no read-only mode -- every method on it is an HTTP call the server
accepts based on token permissions -- so the guarantee cannot live in the client
library. It lives here, in what this protocol declines to offer.

Phase 3 adds a separate `MutableLibraryProvider`. It does not extend this one.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Protocol, runtime_checkable

from shelfwarden.models.hierarchy import lineage
from shelfwarden.models.ids import ItemId
from shelfwarden.models.item import (
    FetchProfile,
    FilePart,
    ItemStub,
    MediaKind,
    NormalizedItem,
    Page,
    SectionRef,
)

# Plex's section vocabulary, mapped onto ours. There is no audiobook section type
# -- an audiobook library is an `artist` section, see library/audiobook.py -- and
# a `photo` section maps to nothing at all. That absence is the point: it is how
# every provider, and the export, learns a section is not modelled.
SECTION_ROOT_KIND: dict[str, MediaKind] = {
    "movie": MediaKind.MOVIE,
    "show": MediaKind.SHOW,
    "artist": MediaKind.AUTHOR,
}

# Every kind a section can hold, top-down. Derived from the root and the
# hierarchy rather than listed, so it cannot disagree with either.
SECTION_KINDS: dict[str, tuple[MediaKind, ...]] = {
    section_type: lineage(root) for section_type, root in SECTION_ROOT_KIND.items()
}

# Provider labels that name a real server. An export holds the user's real rating
# keys, so a snapshot served under one of these labels would make every snapshot
# address a live address as well -- and in Phase 3, a plan recorded during an eval
# run would be one handoff away from editing a real item. `SnapshotLibrary`
# (step 0.7) refuses them. A test pins this set to `library.plex.PROVIDER`
# instead of importing the adapter here.
LIVE_PROVIDERS: frozenset[str] = frozenset({"plex"})

# plexapi method names that mutate server state. Named here rather than in the
# test so the list is documentation as well as an assertion: these are the
# operations that must not be reachable outside the `executing` phase.
MUTATING_METHODS: frozenset[str] = frozenset(
    {
        "addCollection",
        "addLabel",
        "analyze",
        "batchEdits",
        "batchMultiEdits",
        "delete",
        "edit",
        "editSortTitle",
        "editTitle",
        "fixMatch",
        "matches",
        "merge",
        "refresh",
        "removeCollection",
        "removeLabel",
        "saveEdits",
        "split",
        "unmatch",
        "unlockAllFields",
        "update",
        "uploadArt",
        "uploadPoster",
    }
)


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """Who answered, so a dataset can say where it came from.

    Step 0.4's export manifest needs this and the protocol had no way to ask for
    it; the alternative was reaching through `PlexLibrary._server`, which would
    have made `evals/export.py` depend on the Plex adapter and forfeited the
    offline byte-identity test.

    `server_id` is a **hash** of the server's machine identifier, never the
    identifier itself. The hash answers the only question the manifest actually
    asks -- are these two exports from the same server? -- and discards the part
    that identifies it. `scripts/capture_fixtures.py` already scrubs the raw value
    from committed fixtures; recording it verbatim here would undo that.

    `SnapshotLibrary` (step 0.7) returns `provider="snapshot"` with the **world
    id** as `server_id`, not the dataset id step 0.2 planned. One dataset is many
    libraries -- a world per case -- and `server_id` answers *is this the same
    library?* Two cases whose worlds are byte-identical share one. Its
    `server_version` and `platform` are `None`: nothing honest can be said about
    either.
    """

    provider: str
    server_id: str
    server_version: str | None = None
    platform: str | None = None


class Retryability(StrEnum):
    """How a caller should treat a failure.

    From CLAUDE.md: retryable errors are handled in code and never surfaced to the
    model; correctable errors are surfaced *with a concrete next action*; terminal
    errors are surfaced and say plainly that retrying will not help.
    """

    RETRYABLE = "retryable"
    CORRECTABLE = "correctable"
    TERMINAL = "terminal"


class LibraryError(Exception):
    """Base class. Every plexapi and requests failure is translated into one of
    these at the adapter boundary, so nothing downstream can tell which provider
    it is talking to."""

    retryability: ClassVar[Retryability] = Retryability.TERMINAL
    default_next_action: ClassVar[str | None] = None

    def __init__(
        self,
        message: str,
        *,
        next_action: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.next_action = next_action or self.default_next_action
        self.status = status
        # A correctable error that does not name a next action is a bug per
        # CLAUDE.md. Asserted here so no raise site can forget rather than
        # trusting each one to remember.
        if self.retryability is Retryability.CORRECTABLE and not self.next_action:
            raise ValueError(
                f"{type(self).__name__} is CORRECTABLE and must name a next action; "
                "a correctable error without one gives the model nothing to do."
            )

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} Next: {self.next_action}" if self.next_action else base


class LibraryAuthError(LibraryError):
    """The token is rejected, or two-factor is required. Retrying will not help."""

    retryability = Retryability.TERMINAL


class LibraryItemNotFound(LibraryError):
    """The rating key resolved to nothing.

    Correctable rather than terminal because it is usually recoverable: Plex
    rating keys move on rescan, so the identifier is stale rather than wrong.
    """

    retryability = Retryability.CORRECTABLE
    default_next_action = (
        "re-list the section and use the current item_id; Plex rating keys change on rescan"
    )


class LibrarySectionNotFound(LibraryItemNotFound):
    """The section id resolved to nothing.

    A subclass, so anything catching "that id does not exist" still catches it, but
    with its own next action: advice about re-listing a section to find an item id
    is the wrong fix for a section id.
    """

    default_next_action = "list the library's sections and use one of the ids it returns"


class LibraryRateLimited(LibraryError):
    retryability = Retryability.RETRYABLE


class LibraryUnavailable(LibraryError):
    """The server is unreachable, timed out, or returned 5xx."""

    retryability = Retryability.RETRYABLE


class LibraryRequestError(LibraryError):
    """A request the server refused on its merits -- a 4xx that is not 401 or 404."""

    retryability = Retryability.TERMINAL


class LibraryInvalidArgument(LibraryError):
    """An argument no library can answer: a negative offset, a kind the section
    cannot hold, a blank search.

    Raised before any request is made, which is what separates it from
    `LibraryRequestError`: that one is terminal and means the *server* refused.
    This one is correctable, because the caller -- in Phase 1, the model, through a
    tool argument -- can send something else.

    Deliberately without a default next action. The fix depends on the argument
    ("offset is >= 0" helps nobody who sent a bad media kind), so every raise site
    must name its own, and the base class's construction check makes forgetting
    one an error rather than a vague message.
    """

    retryability = Retryability.CORRECTABLE


class LibraryUnsupported(LibraryError):
    """A section this project deliberately does not model.

    A plain music library is the live case: the normalized model has no music
    kinds, so mapping one would mean labelling albums as audiobooks. Refusing is
    honest; guessing is not.
    """

    retryability = Retryability.TERMINAL


class LibraryProtocolError(LibraryError):
    """The server answered in a shape we cannot map, or a local invariant broke."""

    retryability = Retryability.TERMINAL


def check_page(offset: int, limit: int) -> None:
    """Refuse paging arguments that have no defined answer, before anything is fetched.

    Left unchecked, each provider invents one. plexapi sends both straight to the
    server: `container_start or 0` lets a negative offset through, and a negative
    `maxresults` becomes a negative container size. Python slicing is worse,
    because it answers: `items[-1:99]` is the last item, a believable page for a
    meaningless request.

    `limit == 0` is legal. It is Plex's count-only query -- an empty page carrying
    the true total -- and every provider answers it the same way.
    """
    if offset < 0:
        raise LibraryInvalidArgument(
            f"offset must be 0 or more; got {offset}.",
            next_action="pass offset=0 to start at the beginning, or the previous "
            "page's offset plus its `returned` count to continue",
        )
    if limit < 0:
        raise LibraryInvalidArgument(
            f"limit must be 0 or more; got {limit}.",
            next_action="pass a positive limit to get a page of results, or 0 to get "
            "only the total",
        )


def check_search(title: str, limit: int) -> None:
    """Refuse a title search that has no defined answer, before anything is fetched.

    A blank title matches every item in the section, which is a listing wearing a
    search's name -- and an unpaged one, since a search returns no total.
    """
    if not title.strip():
        raise LibraryInvalidArgument(
            "a title search needs a title; got a blank one.",
            next_action="pass the title to look for, or list the section instead of searching it",
        )
    if limit < 0:
        raise LibraryInvalidArgument(
            f"limit must be 0 or more; got {limit}.",
            next_action="pass a positive limit for up to that many matches",
        )


def check_fetchable(profile: FetchProfile) -> None:
    """`STUB` marks a record a listing produced; nobody can ask for one.

    A `ValueError`, not a `LibraryError`. The profile is chosen by our code, never by
    the model, so asking for `STUB` is a bug in the caller -- and a bug should not
    be dressed as a library failure.
    """
    if profile is FetchProfile.STUB:
        offered = ", ".join(str(p) for p in FetchProfile if p is not FetchProfile.STUB)
        raise ValueError(
            f"{profile} is what a listing returns, not something a caller can fetch. "
            f"Ask for one of: {offered}."
        )


def resolve_kind(section_type: str, media_kind: MediaKind | None) -> MediaKind:
    """The kind a listing returns: the section's root by default, otherwise any kind
    the section can hold.

    The section must already be known to be modelled. Asking a movie section for
    episodes has no defined answer from Plex, so it is refused here rather than
    sent.
    """
    kinds = SECTION_KINDS[section_type]
    if media_kind is None:
        return kinds[0]
    if media_kind not in kinds:
        held = ", ".join(str(kind) for kind in kinds)
        raise LibraryInvalidArgument(
            f"a {section_type} section holds {held}; it has no {media_kind} items.",
            next_action=f"pass media_kind as one of {held}, or leave it out to list "
            f"{kinds[0]} items",
        )
    return media_kind


@runtime_checkable
class LibraryProvider(Protocol):
    """Read-only access to a media library.

    Note what is absent: no `edit`, `merge`, `fixMatch`, `refresh`, or `delete`.
    That absence is the whole point -- see MUTATING_METHODS and the test that
    asserts the two are disjoint.

    `SnapshotLibrary` (step 0.7) implements this same protocol and raises this
    same taxonomy, which is what lets the agent run unchanged against both.
    """

    def provider_info(self) -> ProviderInfo:
        """Who is answering. Read-only, and recorded in every export manifest."""
        ...

    def sections(self) -> tuple[SectionRef, ...]:
        """Every library section the token can see."""
        ...

    def list_items(
        self,
        section_id: str,
        offset: int,
        limit: int,
        media_kind: MediaKind | None = None,
    ) -> Page[ItemStub]:
        """One page of a section. `limit` is required -- see PlexLibrary."""
        ...

    def get_item(
        self,
        item_id: ItemId,
        profile: FetchProfile = FetchProfile.CORE,
    ) -> NormalizedItem:
        """One item, fetched at the given profile."""
        ...

    def get_children(self, item_id: ItemId, offset: int, limit: int) -> Page[ItemStub]:
        """One page of an item's children: seasons of a show, books of an author."""
        ...

    def get_files(self, item_id: ItemId) -> tuple[FilePart, ...]:
        """The files backing an item."""
        ...

    def find_similar(self, section_id: str, title: str, limit: int) -> tuple[ItemStub, ...]:
        """Candidate matches by title within a section. Ranking belongs to the
        comparators in step 0.45, not here."""
        ...


def protocol_methods(protocol: type) -> frozenset[str]:
    """The public method names a Protocol declares."""
    return frozenset(
        name for name in getattr(protocol, "__protocol_attrs__", ()) if not name.startswith("_")
    )
