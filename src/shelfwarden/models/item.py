"""The normalized media model -- the vocabulary every other package speaks.

The export writes these records, the corruption functions mutate them, the truth
files embed them, the snapshot provider serves them, and the scorer compares them
byte-for-byte. A distinction this model cannot represent is a distinction the
harness cannot measure.

plexapi types never reach here: `library/plex.py` translates into these types at
the boundary, enforced by an import contract rather than by discipline.
"""

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from shelfwarden.canonical import canonical_text
from shelfwarden.models.ids import ExternalId, ItemId, sort_external_ids


def _to_nfc(value: str) -> str:
    return canonical_text(value)


def _require_utc(value: datetime) -> datetime:
    """Reject naive datetimes and coerce aware ones to UTC.

    Serialization renders a datetime by its *representation*, not its instant: the
    same moment as `+00:00` and as `-05:00` produces different bytes, and a naive
    value produces a third form with no offset at all. Byte-identical export would
    otherwise depend on the server's timezone configuration.
    """
    if value.tzinfo is None:
        raise ValueError(
            "timestamp must be timezone-aware; a naive datetime serializes "
            "without an offset and makes the export non-deterministic"
        )
    return value.astimezone(UTC)


Text = Annotated[str, AfterValidator(_to_nfc)]
UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


class MediaKind(StrEnum):
    """What an item is.

    `AUTHOR` maps a Plex Artist. It is not in the roadmap's original six, but two
    audiobook corruptions -- `author_name_variant` and `narrator_as_author` --
    operate on the Artist and record "the id set to merge", which is
    unrepresentable unless authors are addressable. Author/Audiobook/Part then
    mirrors Show/Season/Episode exactly. Recorded in implementation-plan.md.
    """

    MOVIE = "movie"
    SHOW = "show"
    SEASON = "season"
    EPISODE = "episode"
    AUTHOR = "author"
    AUDIOBOOK = "audiobook"
    AUDIOBOOK_PART = "audiobook_part"


class FetchProfile(StrEnum):
    """What was actually asked of the server.

    Absence is only meaningful relative to a profile. plexapi reloads a partial
    object when an attribute is `None` or `[]`, and with `autoreload=false` -- which
    this project requires -- it does not: an item fetched as part of a list returns
    `guids == []` whether it has no external ids or nobody asked for them. The same
    `NormalizedItem` type flows out of the export at FULL and out of
    `get_item_details` at CORE, so the marker travels with the record.
    """

    STUB = "stub"
    CORE = "core"
    FULL = "full"


class FilePart(BaseModel):
    """One file backing an item."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Deliberately NOT NFC-normalized. Every other string here is normalized so it
    # hashes consistently, but a path is an argument to a future filesystem
    # operation -- Phase 3 renames real files -- and macOS hands out NFD paths.
    # Normalizing one could produce a string that names no file on disk. Where a
    # path needs comparing rather than opening, normalize at the comparison site.
    path: str
    container: str | None = None
    video_resolution: str | None = None
    size_bytes: int | None = None
    duration_ms: int | None = None


class BaseItem(BaseModel):
    """Fields common to every media kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: ItemId
    fetched: FetchProfile
    title: Text
    title_sort: Text | None = None
    summary: Text | None = None
    guids: tuple[ExternalId, ...] = ()
    # Plex locks a field against future agent refreshes, and every edit helper
    # defaults to locked=True. Captured from day one because Phase 3's revert must
    # restore lock state, not just values.
    locked_fields: tuple[str, ...] = ()
    # Presence, not URL: a thumb key ends in a mutable timestamp, and the only
    # corruption that touches artwork cares whether artwork exists. None means
    # "not fetched at this profile".
    has_thumb: bool | None = None
    has_art: bool | None = None
    added_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None

    @field_validator("guids", mode="after")
    @classmethod
    def _canonical_guid_order(cls, value: tuple[ExternalId, ...]) -> tuple[ExternalId, ...]:
        return sort_external_ids(value)

    @field_validator("locked_fields", mode="after")
    @classmethod
    def _canonical_lock_order(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))


class MovieItem(BaseItem):
    media_kind: Literal[MediaKind.MOVIE] = MediaKind.MOVIE
    year: int | None = None
    edition_title: Text | None = None
    original_title: Text | None = None
    content_rating: str | None = None
    studio: Text | None = None
    tagline: Text | None = None
    rating: float | None = None
    audience_rating: float | None = None
    originally_available_at: date | None = None
    duration_ms: int | None = None
    parts: tuple[FilePart, ...] = ()


class ShowItem(BaseItem):
    media_kind: Literal[MediaKind.SHOW] = MediaKind.SHOW
    year: int | None = None
    content_rating: str | None = None
    studio: Text | None = None
    network: Text | None = None
    # Drives `absolute_vs_seasonal`: which ordering the section believes in.
    show_ordering: str | None = None
    child_count: int | None = None
    leaf_count: int | None = None


class SeasonItem(BaseItem):
    media_kind: Literal[MediaKind.SEASON] = MediaKind.SEASON
    parent: ItemId | None = None
    parent_title: Text | None = None
    index: int | None = None
    year: int | None = None


class EpisodeItem(BaseItem):
    media_kind: Literal[MediaKind.EPISODE] = MediaKind.EPISODE
    parent: ItemId | None = None
    grandparent: ItemId | None = None
    parent_title: Text | None = None
    grandparent_title: Text | None = None
    # `index` is the episode number and `parent_index` the season number -- the two
    # values `episode_wrong_season` and `absolute_vs_seasonal` move.
    index: int | None = None
    parent_index: int | None = None
    year: int | None = None
    originally_available_at: date | None = None
    duration_ms: int | None = None
    parts: tuple[FilePart, ...] = ()


class AuthorItem(BaseItem):
    media_kind: Literal[MediaKind.AUTHOR] = MediaKind.AUTHOR
    album_count: int | None = None


class AudiobookItem(BaseItem):
    media_kind: Literal[MediaKind.AUDIOBOOK] = MediaKind.AUDIOBOOK
    parent: ItemId | None = None
    parent_title: Text | None = None
    index: int | None = None
    year: int | None = None
    studio: Text | None = None
    series: Text | None = None
    # A string, not a number: Audnexus returns positions like "3.5" for novellas.
    series_position: str | None = None
    part_count: int | None = None


class AudiobookPartItem(BaseItem):
    media_kind: Literal[MediaKind.AUDIOBOOK_PART] = MediaKind.AUDIOBOOK_PART
    parent: ItemId | None = None
    grandparent: ItemId | None = None
    index: int | None = None
    duration_ms: int | None = None
    parts: tuple[FilePart, ...] = ()


NormalizedItem = Annotated[
    MovieItem
    | ShowItem
    | SeasonItem
    | EpisodeItem
    | AuthorItem
    | AudiobookItem
    | AudiobookPartItem,
    Field(discriminator="media_kind"),
]

ItemAdapter: TypeAdapter[NormalizedItem] = TypeAdapter(NormalizedItem)


class ItemStub(BaseModel):
    """The minimum useful payload for a listing. Tool results are resent every
    turn, so a listing carries identity and nothing else."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: ItemId
    media_kind: MediaKind
    title: Text
    year: int | None = None


class SectionRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    section_id: str
    title: Text
    # Plex's own vocabulary: movie | show | artist | photo. There is no audiobook
    # section type -- audiobooks live in an `artist` section, which is why
    # detection (step 0.3) keys off `agent` and structure instead.
    section_type: str
    agent: str


class Page[T](BaseModel):
    """A slice of a listing, with explicit counts.

    `total` and `returned` are separate on purpose: a caller must be able to tell a
    short page from the end of the results without inferring it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[T, ...]
    total: int
    offset: int
    returned: int


def with_changes(item: NormalizedItem, changes: Mapping[str, Any]) -> NormalizedItem:
    """Apply field changes and re-validate.

    The obvious alternative, `model_copy(update=...)`, does not validate even on a
    frozen model with `extra="forbid"`: it will put a `str` into an `int | None`
    field without complaint, and that value would flow into the truth file and the
    snapshot. Corruption functions (step 0.5) mutate through here so that a
    type-invalid item cannot be constructed at all.
    """
    return ItemAdapter.validate_python({**item.model_dump(mode="json"), **changes})


def dump_item(item: NormalizedItem) -> dict[str, Any]:
    """JSON-safe primitives, ready for `canonical_json`."""
    return item.model_dump(mode="json")


def load_item(data: Mapping[str, Any] | bytes | str) -> NormalizedItem:
    """Parse one item, dispatching on `media_kind`."""
    if isinstance(data, bytes | str):
        return ItemAdapter.validate_json(data)
    return ItemAdapter.validate_python(data)


def load_items(records: Iterable[Mapping[str, Any]]) -> tuple[NormalizedItem, ...]:
    return tuple(load_item(record) for record in records)
