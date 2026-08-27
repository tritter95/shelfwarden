"""The Plex adapter -- the only module in the project that imports plexapi.

plexapi objects, plexapi exceptions, and requests exceptions all stop here.
Nothing downstream can tell which provider it is talking to, which is what lets
the agent run unchanged against SnapshotLibrary.

Two plexapi behaviours are neutralized at construction rather than worked around
later, because both fail silently:

* Auto-reload refetches over the network whenever a partial object's attribute is
  None or []. Its config switch accepts only lowercase "false"/"0" and swallows
  anything else into a permissive default, so "False" leaves it on.
* Timestamps parse to *naive local time* by default, making an export depend on
  the timezone of the machine that produced it.
"""

import os
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from functools import wraps
from typing import Any
from xml.etree import ElementTree

import plexapi
import plexapi.utils
from plexapi.exceptions import (
    BadRequest,
    NotFound,
    PlexApiException,
    TwoFactorRequired,
    Unauthorized,
    UnknownType,
    Unsupported,
)
from plexapi.server import PlexServer
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import RequestException, Timeout

from shelfwarden.library.audiobook import AudiobookVerdict, TrackSample, classify_section
from shelfwarden.library.base import (
    LibraryAuthError,
    LibraryError,
    LibraryItemNotFound,
    LibraryProtocolError,
    LibraryRateLimited,
    LibraryRequestError,
    LibraryUnavailable,
    LibraryUnsupported,
)
from shelfwarden.library.session import StatusRecorder, build_session, status_from_message
from shelfwarden.models.ids import ItemId, parse_guids
from shelfwarden.models.item import (
    AudiobookItem,
    AudiobookPartItem,
    AuthorItem,
    EpisodeItem,
    FetchProfile,
    FilePart,
    ItemStub,
    MediaKind,
    MovieItem,
    NormalizedItem,
    Page,
    SeasonItem,
    SectionRef,
    ShowItem,
)

PROVIDER = "plex"

# How many tracks to sample when deciding whether a Music section is audiobooks.
AUDIOBOOK_SAMPLE_SIZE = 40

# Plex's own section vocabulary. There is no audiobook type -- see library/audiobook.py.
SECTION_TYPE_TO_KIND: dict[str, MediaKind] = {
    "movie": MediaKind.MOVIE,
    "show": MediaKind.SHOW,
    "artist": MediaKind.AUTHOR,
}

PLEX_TYPE_TO_KIND: dict[str, MediaKind] = {
    "movie": MediaKind.MOVIE,
    "show": MediaKind.SHOW,
    "season": MediaKind.SEASON,
    "episode": MediaKind.EPISODE,
    "artist": MediaKind.AUTHOR,
    "album": MediaKind.AUDIOBOOK,
    "track": MediaKind.AUDIOBOOK_PART,
}

KIND_TO_LIBTYPE: dict[MediaKind, str] = {
    MediaKind.MOVIE: "movie",
    MediaKind.SHOW: "show",
    MediaKind.SEASON: "season",
    MediaKind.EPISODE: "episode",
    MediaKind.AUTHOR: "artist",
    MediaKind.AUDIOBOOK: "album",
    MediaKind.AUDIOBOOK_PART: "track",
}

# The include set per profile, declared as data so the export manifest can record
# exactly what produced a record. plexapi applies every _INCLUDES key by default,
# several of which are expensive, so this is mostly about turning things off.
#
# `excludeElements` is never passed: it would drop the Guid and Media elements the
# whole model is built on.
RELOAD_INCLUDES: dict[FetchProfile, dict[str, bool]] = {
    FetchProfile.CORE: {
        "checkFiles": False,
        "includeBandwidths": False,
        "includeChapters": False,
        "includeGeolocation": False,
        "includeLoudnessRamps": False,
        "includeMarkers": False,
        "includeRelated": False,
        "includeReviews": False,
    },
    FetchProfile.FULL: {
        "checkFiles": True,
        "includeBandwidths": False,
        "includeChapters": False,
        "includeGeolocation": False,
        "includeLoudnessRamps": False,
        "includeMarkers": False,
        "includeRelated": False,
        "includeReviews": False,
    },
}


def configure_plexapi() -> None:
    """Neutralize the two silent-failure defaults. Idempotent.

    Auto-reload is set through the environment rather than documented in a config
    file because the environment is consulted *first*: a developer with
    PLEXAPI_PLEXAPI_AUTORELOAD="False" already exported would override a correct
    config.ini and land back in the fails-open case. Owning the value is the only
    way to win.

    The timezone is set programmatically because plexapi binds it at import time,
    which is already past by the time this module is imported.
    """
    os.environ["PLEXAPI_PLEXAPI_AUTORELOAD"] = "false"
    plexapi.utils.setDatetimeTimezone("utc")


def autoreload_is_off() -> bool:
    return plexapi.CONFIG.get("plexapi.autoreload", True, bool) is False


def _translate(exc: Exception, status: int | None) -> LibraryError:
    """Map a plexapi or requests failure onto the project's taxonomy.

    plexapi collapses everything that is not 401 or 404 into BadRequest, so the
    status code -- recovered from the session hook, or failing that from the
    message -- is what separates a retryable 503 from a terminal 400.
    """
    if isinstance(exc, TwoFactorRequired | Unauthorized):
        return LibraryAuthError(str(exc), status=status or 401)
    if isinstance(exc, NotFound):
        return LibraryItemNotFound(str(exc), status=status or 404)
    if isinstance(exc, Timeout | RequestsConnectionError):
        return LibraryUnavailable(f"cannot reach the Plex server: {exc}")

    code = status if status is not None else status_from_message(str(exc))
    if code == 429:
        return LibraryRateLimited(str(exc), status=code)
    if code is not None and 500 <= code < 600:
        return LibraryUnavailable(str(exc), status=code)
    if code == 401:
        return LibraryAuthError(str(exc), status=code)
    if code == 404:
        return LibraryItemNotFound(str(exc), status=code)

    if isinstance(exc, UnknownType):
        return LibraryProtocolError(str(exc), status=code)
    if isinstance(exc, BadRequest | Unsupported):
        return LibraryRequestError(str(exc), status=code)
    if isinstance(exc, RequestException):
        return LibraryUnavailable(str(exc), status=code)
    return LibraryProtocolError(str(exc), status=code)


# The foreign exception families this boundary exists to absorb. Deliberately not
# `Exception`: a TypeError or AttributeError raised by our own code is a bug, and
# laundering it into a LibraryError hides it behind a taxonomy that says "the
# server did something", which is untrue and costs a debugging session.
FOREIGN_EXCEPTIONS = (PlexApiException, RequestException, ElementTree.ParseError)


def _translates_errors[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Wrap a public method so no foreign exception type leaks past the adapter."""

    @wraps(method)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        provider: PlexLibrary = args[0]  # type: ignore[assignment]
        provider._recorder.clear()
        try:
            return method(*args, **kwargs)
        except LibraryError:
            raise
        except FOREIGN_EXCEPTIONS as exc:
            raise _translate(exc, provider._recorder.last_status) from exc

    return wrapper


def _text(value: Any) -> str | None:
    """Plex returns '' for absent strings; the model distinguishes '' from None."""
    if value is None:
        return None
    text = str(value)
    return text or None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        # Only reachable if configure_plexapi() has not run. Rather than guessing
        # a timezone -- which is what makes plexapi's default non-deterministic --
        # say so.
        raise LibraryProtocolError(
            "plexapi returned a naive datetime; configure_plexapi() must run "
            "before any object is built or timestamps depend on the local timezone"
        )
    return value.astimezone(UTC)


def _locked_fields(obj: Any) -> tuple[str, ...]:
    return tuple(field.name for field in getattr(obj, "fields", ()) or () if field.locked)


def _guids(obj: Any):
    children = [guid.id for guid in getattr(obj, "guids", ()) or () if guid.id]
    return parse_guids(getattr(obj, "guid", None), children)


def _parts(obj: Any) -> tuple[FilePart, ...]:
    parts: list[FilePart] = []
    for medium in getattr(obj, "media", ()) or ():
        for part in getattr(medium, "parts", ()) or ():
            parts.append(
                FilePart(
                    path=part.file or "",
                    container=_text(part.container) or _text(medium.container),
                    video_resolution=_text(getattr(medium, "videoResolution", None)),
                    size_bytes=part.size,
                    duration_ms=part.duration or medium.duration,
                )
            )
    return tuple(parts)


class PlexLibrary:
    """Read-only access to a live Plex server. Implements `LibraryProvider`."""

    def __init__(
        self,
        baseurl: str | None = None,
        token: str | None = None,
        *,
        timeout: int = 30,
        server: Any = None,
        recorder: StatusRecorder | None = None,
    ) -> None:
        configure_plexapi()
        if not autoreload_is_off():
            raise LibraryProtocolError(
                "could not disable plexapi auto-reload; every partial object would "
                "silently refetch over the network, making exports non-deterministic "
                "and cost unbounded"
            )

        if server is not None:
            # Injected for fixture-driven tests. The recorder is still required so
            # error translation behaves identically on both paths.
            self._server = server
            self._recorder = recorder or StatusRecorder()
        else:
            if not baseurl or not token:
                raise LibraryProtocolError("baseurl and token are required")
            session, self._recorder = build_session()
            self._server = PlexServer(baseurl, token, session=session, timeout=timeout)

        self._verdicts: dict[str, AudiobookVerdict] = {}

    # -- sections ---------------------------------------------------------

    @_translates_errors
    def sections(self) -> tuple[SectionRef, ...]:
        return tuple(
            SectionRef(
                section_id=str(section.key),
                title=section.title,
                section_type=section.type,
                agent=section.agent or "",
            )
            for section in self._server.library.sections()
        )

    def _section(self, section_id: str) -> Any:
        for section in self._server.library.sections():
            if str(section.key) == str(section_id):
                return section
        raise LibraryItemNotFound(f"no section with id {section_id!r}")

    @_translates_errors
    def audiobook_verdict(self, section_id: str) -> AudiobookVerdict:
        """Whether an `artist` section holds audiobooks. Cached per section."""
        if section_id in self._verdicts:
            return self._verdicts[section_id]

        section = self._section(section_id)
        if section.type != "artist":
            raise LibraryUnsupported(
                f"section {section_id!r} is a {section.type} section; audiobook "
                "detection applies only to artist sections"
            )

        tracks = section.search(
            libtype="track",
            container_start=0,
            maxresults=AUDIOBOOK_SAMPLE_SIZE,
        )
        samples = [
            TrackSample(
                album_id=str(getattr(track, "parentRatingKey", "") or ""),
                path=part.path,
                container=part.container,
                duration_ms=part.duration_ms,
            )
            for track in tracks
            for part in _parts(track)
        ]
        population = _total_of(tracks, section)
        verdict = classify_section(section.agent or "", samples, population)
        self._verdicts[section_id] = verdict
        return verdict

    def _require_supported(self, section: Any) -> None:
        if section.type not in SECTION_TYPE_TO_KIND:
            raise LibraryUnsupported(
                f"{section.type!r} sections are not modelled; ShelfWarden handles "
                "movie, show, and audiobook (artist) sections"
            )
        if section.type == "artist" and not self.audiobook_verdict(str(section.key)).is_audiobook:
            raise LibraryUnsupported(
                f"section {section.key!r} looks like a music library, not audiobooks: "
                f"{self.audiobook_verdict(str(section.key)).explain()}. The normalized "
                "model has no music kinds, so mapping it would mislabel albums as books."
            )

    # -- listing ----------------------------------------------------------

    @_translates_errors
    def list_items(
        self,
        section_id: str,
        offset: int,
        limit: int,
        media_kind: MediaKind | None = None,
    ) -> Page[ItemStub]:
        """One page of a section.

        `limit` is required, and deliberately has no default: passing
        `container_start` without `maxresults` does not fetch a page, it walks the
        entire remaining result set, and nothing in the result says that happened.
        """
        section = self._section(section_id)
        self._require_supported(section)
        kind = media_kind or SECTION_TYPE_TO_KIND[section.type]

        results = section.search(
            libtype=KIND_TO_LIBTYPE[kind],
            container_start=offset,
            maxresults=limit,
        )
        stubs = tuple(self._stub(obj, section_id) for obj in results)
        return Page[ItemStub](
            items=stubs,
            total=_total_of(results, section),
            offset=offset,
            returned=len(stubs),
        )

    @_translates_errors
    def get_children(self, item_id: ItemId, offset: int, limit: int) -> Page[ItemStub]:
        """Seasons of a show, episodes of a season, books of an author.

        Both paging arguments again: a show with 300 episodes is not a special
        case, it is Tuesday.
        """
        parent = self._fetch(item_id)
        results = parent.fetchItems(
            f"/library/metadata/{item_id.rating_key}/children",
            container_start=offset,
            maxresults=limit,
        )
        stubs = tuple(self._stub(obj, item_id.section_id) for obj in results)
        return Page[ItemStub](
            items=stubs,
            total=_total_of(results, None),
            offset=offset,
            returned=len(stubs),
        )

    @_translates_errors
    def find_similar(self, section_id: str, title: str, limit: int) -> tuple[ItemStub, ...]:
        section = self._section(section_id)
        self._require_supported(section)
        results = section.search(title=title, container_start=0, maxresults=limit)
        return tuple(self._stub(obj, section_id) for obj in results)

    # -- items ------------------------------------------------------------

    def _fetch(self, item_id: ItemId) -> Any:
        if item_id.provider != PROVIDER:
            raise LibraryItemNotFound(
                f"{item_id} belongs to provider {item_id.provider!r}, not {PROVIDER!r}"
            )
        return self._server.fetchItem(int(item_id.rating_key))

    @_translates_errors
    def get_item(
        self,
        item_id: ItemId,
        profile: FetchProfile = FetchProfile.CORE,
    ) -> NormalizedItem:
        obj = self._fetch(item_id)
        obj.reload(**RELOAD_INCLUDES[profile])
        _assert_object_autoreload_off(obj)
        return self._normalize(obj, item_id.section_id, profile)

    @_translates_errors
    def get_files(self, item_id: ItemId) -> tuple[FilePart, ...]:
        obj = self._fetch(item_id)
        obj.reload(**RELOAD_INCLUDES[FetchProfile.FULL])
        return _parts(obj)

    # -- mapping ----------------------------------------------------------

    def _stub(self, obj: Any, section_id: str) -> ItemStub:
        return ItemStub(
            item_id=ItemId(PROVIDER, section_id, str(obj.ratingKey)),
            media_kind=_kind_of(obj),
            title=obj.title,
            year=getattr(obj, "year", None),
        )

    def _normalize(self, obj: Any, section_id: str, profile: FetchProfile) -> NormalizedItem:
        return normalize_item(obj, section_id, profile)


def _assert_object_autoreload_off(obj: Any) -> None:
    """Verify on a real object, not just on the config.

    Reaching for a private attribute is deliberate: the alternative is trusting a
    configuration path that fails open, which is the exact failure this guards.
    """
    if getattr(obj, "_autoReload", True) is not False:
        raise LibraryProtocolError(
            "plexapi auto-reload is live on a fetched object; exports would be "
            "non-deterministic and cost unbounded"
        )


def _total_of(results: Any, section: Any) -> int:
    """The total from the response, never inferred from what we fetched.

    fetchItems returns a MediaContainer whose totalSize is propagated from each
    page by extend(). A total derived from len(results) would silently report a
    truncated page as the whole section.
    """
    total = getattr(results, "totalSize", None)
    if total is not None:
        return int(total)
    if section is not None:
        section_total = getattr(section, "totalSize", None)
        if section_total is not None:
            return int(section_total)
    return len(results)


def _kind_of(obj: Any) -> MediaKind:
    kind = PLEX_TYPE_TO_KIND.get(getattr(obj, "type", None) or "")
    if kind is None:
        raise LibraryProtocolError(f"unmappable Plex item type {getattr(obj, 'type', None)!r}")
    return kind


def _common(obj: Any, section_id: str, profile: FetchProfile) -> dict[str, Any]:
    return {
        "item_id": ItemId(PROVIDER, section_id, str(obj.ratingKey)),
        "fetched": profile,
        "title": obj.title or "",
        "title_sort": _text(getattr(obj, "titleSort", None)),
        "summary": _text(getattr(obj, "summary", None)),
        "guids": _guids(obj),
        "locked_fields": _locked_fields(obj),
        "has_thumb": bool(getattr(obj, "thumb", None)),
        "has_art": bool(getattr(obj, "art", None)),
        "added_at": _utc(getattr(obj, "addedAt", None)),
        "updated_at": _utc(getattr(obj, "updatedAt", None)),
    }


def _parent_id(obj: Any, attr: str, section_id: str) -> ItemId | None:
    key = getattr(obj, attr, None)
    return ItemId(PROVIDER, section_id, str(key)) if key else None


def _date_of(obj: Any) -> Any:
    value = getattr(obj, "originallyAvailableAt", None)
    return value.date() if isinstance(value, datetime) else value


def normalize_item(obj: Any, section_id: str, profile: FetchProfile) -> NormalizedItem:
    """Map one plexapi object onto the normalized model.

    Module-level rather than a method so fixture tests can exercise the mapping
    without constructing a provider.
    """
    kind = _kind_of(obj)
    common = _common(obj, section_id, profile)

    if kind is MediaKind.MOVIE:
        return MovieItem(
            **common,
            year=obj.year,
            edition_title=_text(getattr(obj, "editionTitle", None)),
            original_title=_text(getattr(obj, "originalTitle", None)),
            content_rating=_text(getattr(obj, "contentRating", None)),
            studio=_text(getattr(obj, "studio", None)),
            tagline=_text(getattr(obj, "tagline", None)),
            rating=getattr(obj, "rating", None),
            audience_rating=getattr(obj, "audienceRating", None),
            originally_available_at=_date_of(obj),
            duration_ms=getattr(obj, "duration", None),
            parts=_parts(obj),
        )
    if kind is MediaKind.SHOW:
        return ShowItem(
            **common,
            year=obj.year,
            content_rating=_text(getattr(obj, "contentRating", None)),
            studio=_text(getattr(obj, "studio", None)),
            network=_text(getattr(obj, "network", None)),
            show_ordering=_text(getattr(obj, "showOrdering", None)),
            child_count=getattr(obj, "childCount", None),
            leaf_count=getattr(obj, "leafCount", None),
        )
    if kind is MediaKind.SEASON:
        return SeasonItem(
            **common,
            parent=_parent_id(obj, "parentRatingKey", section_id),
            parent_title=_text(getattr(obj, "parentTitle", None)),
            index=getattr(obj, "index", None),
            year=getattr(obj, "year", None),
        )
    if kind is MediaKind.EPISODE:
        return EpisodeItem(
            **common,
            parent=_parent_id(obj, "parentRatingKey", section_id),
            grandparent=_parent_id(obj, "grandparentRatingKey", section_id),
            parent_title=_text(getattr(obj, "parentTitle", None)),
            grandparent_title=_text(getattr(obj, "grandparentTitle", None)),
            index=getattr(obj, "index", None),
            parent_index=getattr(obj, "parentIndex", None),
            year=getattr(obj, "year", None),
            originally_available_at=_date_of(obj),
            duration_ms=getattr(obj, "duration", None),
            parts=_parts(obj),
        )
    if kind is MediaKind.AUTHOR:
        return AuthorItem(**common, album_count=getattr(obj, "childCount", None))
    if kind is MediaKind.AUDIOBOOK:
        return AudiobookItem(
            **common,
            parent=_parent_id(obj, "parentRatingKey", section_id),
            parent_title=_text(getattr(obj, "parentTitle", None)),
            index=getattr(obj, "index", None),
            year=getattr(obj, "year", None),
            studio=_text(getattr(obj, "studio", None)),
            part_count=getattr(obj, "leafCount", None),
        )
    return AudiobookPartItem(
        **common,
        parent=_parent_id(obj, "parentRatingKey", section_id),
        grandparent=_parent_id(obj, "grandparentRatingKey", section_id),
        index=getattr(obj, "index", None),
        duration_ms=getattr(obj, "duration", None),
        parts=_parts(obj),
    )


def build_samples(tracks: Iterable[Any]) -> Sequence[TrackSample]:
    """Reduce plexapi tracks to the plain samples detection consumes."""
    return [
        TrackSample(
            album_id=str(getattr(track, "parentRatingKey", "") or ""),
            path=part.path,
            container=part.container,
            duration_ms=part.duration_ms,
        )
        for track in tracks
        for part in _parts(track)
    ]
