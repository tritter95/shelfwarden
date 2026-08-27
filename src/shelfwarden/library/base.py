"""The read-only library contract and its error taxonomy.

`LibraryProvider` is where spec §3.2 stops being a promise and becomes a type.
plexapi has no read-only mode -- every method on it is an HTTP call the server
accepts based on token permissions -- so the guarantee cannot live in the client
library. It lives here, in what this protocol declines to offer.

Phase 3 adds a separate `MutableLibraryProvider`. It does not extend this one.
"""

from enum import StrEnum
from typing import ClassVar, Protocol, runtime_checkable

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


class LibraryRateLimited(LibraryError):
    retryability = Retryability.RETRYABLE


class LibraryUnavailable(LibraryError):
    """The server is unreachable, timed out, or returned 5xx."""

    retryability = Retryability.RETRYABLE


class LibraryRequestError(LibraryError):
    """A request the server refused on its merits -- a 4xx that is not 401 or 404."""

    retryability = Retryability.TERMINAL


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


@runtime_checkable
class LibraryProvider(Protocol):
    """Read-only access to a media library.

    Note what is absent: no `edit`, `merge`, `fixMatch`, `refresh`, or `delete`.
    That absence is the whole point -- see MUTATING_METHODS and the test that
    asserts the two are disjoint.

    `SnapshotLibrary` (step 0.7) implements this same protocol and raises this
    same taxonomy, which is what lets the agent run unchanged against both.
    """

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
