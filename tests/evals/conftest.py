"""A library that exists entirely in memory.

The export's gate is byte-identity, and a gate that needs a Plex server is a gate
that does not run in CI. `FakeLibrary` implements `LibraryProvider` and nothing
else, which is also the point: if the export ever reaches for something only
`PlexLibrary` has, these tests stop compiling rather than quietly binding the
export to the adapter.

The records are hand-built rather than mapped from the XML fixtures. The mapping
has its own tests in `tests/library/`; what matters here is that the *shapes* the
export has to handle are all present and legible in one place -- a legacy guid, an
unknown guid, an item with no guid at all, locked fields, multiple parts, a show
with two seasons, and an author whose book is split across two files.
"""

from dataclasses import dataclass, field

import pytest

from shelfwarden.library.base import (
    LibraryItemNotFound,
    LibraryUnavailable,
    LibraryUnsupported,
    ProviderInfo,
)
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
    with_changes,
)

PROVIDER = "fake"

MOVIES = "1"
SHOWS = "2"
BOOKS = "3"
MUSIC = "4"
PHOTOS = "5"

SECTIONS = (
    SectionRef(
        section_id=MOVIES, title="Movies", section_type="movie", agent="tv.plex.agents.movie"
    ),
    SectionRef(section_id=SHOWS, title="TV", section_type="show", agent="tv.plex.agents.series"),
    SectionRef(
        section_id=BOOKS,
        title="Audiobooks",
        section_type="artist",
        agent="com.plexapp.agents.audnexus",
    ),
    SectionRef(
        section_id=MUSIC, title="Music", section_type="artist", agent="tv.plex.agents.music"
    ),
    SectionRef(
        section_id=PHOTOS, title="Photos", section_type="photo", agent="com.plexapp.agents.none"
    ),
)

# Music is an artist section that is not audiobooks -- the case where the provider
# raises LibraryUnsupported from the listing rather than from `sections()`.
UNSUPPORTED_ARTIST_SECTIONS = frozenset({MUSIC})


def _id(section_id: str, rating_key: str) -> ItemId:
    return ItemId(PROVIDER, section_id, rating_key)


def _movie(rating_key: str, title: str, year: int, **overrides) -> MovieItem:
    data: dict = {
        "item_id": _id(MOVIES, rating_key),
        "fetched": FetchProfile.CORE,
        "title": title,
        "year": year,
        "summary": f"{title} is a film.",
        "guids": parse_guids(f"plex://movie/{rating_key}", [f"tmdb://{rating_key}"]),
        "has_thumb": True,
        "has_art": False,
        "parts": (
            FilePart(
                media_id=f"9{rating_key}",
                part_id=f"1{rating_key}",
                path=f"/media/Movies/{title} ({year})/{title} ({year}).mkv",
                container="mkv",
                video_resolution="1080",
                size_bytes=8_000_000_000,
                duration_ms=7_740_000,
            ),
        ),
    }
    data.update(overrides)
    return MovieItem(**data)


def _library_records() -> tuple[NormalizedItem, ...]:
    movies: list[NormalizedItem] = [
        _movie("101", "Amélie", 2001, locked_fields=("title",)),
        # A remake pair: same title, different year. Feeds the readiness heuristic.
        _movie("102", "Solaris", 1972),
        _movie("103", "Solaris", 2002),
        # A legacy-agent match: one guid, no plex:// primary.
        _movie(
            "104",
            "The Shawshank Redemption",
            1994,
            guids=parse_guids("com.plexapp.agents.imdb://tt0111161?lang=en"),
        ),
        # An unrecognised agent. This is the case the census exists to surface --
        # step 0.2 wrote the legacy parsers against no legacy library.
        _movie(
            "105",
            "Home Video",
            2015,
            guids=parse_guids("com.plexapp.agents.plexmovie://12345?lang=en"),
        ),
        # Nothing matched at all, and no summary to null out.
        _movie("106", "Unmatched Rip", 1999, guids=(), summary=None, has_thumb=False),
        # Two versions of one film -- the duplicate_quality candidate.
        _movie("107", "Blade Runner", 1982, edition_title="Final Cut"),
        _movie(
            "108",
            "Blade Runner",
            1982,
            parts=(
                FilePart(
                    media_id="908",
                    part_id="108",
                    path="/media/Movies/Blade Runner (1982)/Blade Runner (1982) 4K.mkv",
                    container="mkv",
                    video_resolution="4k",
                    size_bytes=40_000_000_000,
                ),
            ),
        ),
    ]

    show = ShowItem(
        item_id=_id(SHOWS, "201"),
        fetched=FetchProfile.CORE,
        title="Cowboy Bebop",
        year=1998,
        summary="A bounty hunter crew.",
        guids=parse_guids("plex://show/201", ["tvdb://76885"]),
        show_ordering="absolute",
        child_count=2,
        leaf_count=3,
    )
    seasons = [
        SeasonItem(
            item_id=_id(SHOWS, f"21{index}"),
            fetched=FetchProfile.CORE,
            title=f"Season {index}",
            parent=show.item_id,
            parent_title=show.title,
            index=index,
        )
        for index in (1, 2)
    ]
    episodes = [
        EpisodeItem(
            item_id=_id(SHOWS, f"22{season}{episode}"),
            fetched=FetchProfile.CORE,
            title=f"Session {season}-{episode}",
            parent=seasons[season - 1].item_id,
            grandparent=show.item_id,
            parent_title=f"Season {season}",
            grandparent_title=show.title,
            index=episode,
            parent_index=season,
            parts=(
                FilePart(
                    media_id=f"93{season}{episode}",
                    part_id=f"13{season}{episode}",
                    path=f"/media/TV/Cowboy Bebop/S0{season}E0{episode}.mkv",
                    container="mkv",
                    video_resolution="720",
                ),
            ),
        )
        for season, episode in ((1, 1), (1, 2), (2, 1))
    ]

    # A second show with no children at all -- the smallest possible family, and
    # the one that still fits when a big family is dropped for budget.
    lone_show = ShowItem(
        item_id=_id(SHOWS, "301"),
        fetched=FetchProfile.CORE,
        title="Pilot Only",
        year=2020,
        guids=parse_guids("plex://show/301", ["tvdb://999999"]),
    )

    author = AuthorItem(
        item_id=_id(BOOKS, "401"),
        fetched=FetchProfile.CORE,
        title="Brandon Sanderson",
        album_count=2,
        locked_fields=("title",),
    )
    books = [
        AudiobookItem(
            item_id=_id(BOOKS, "411"),
            fetched=FetchProfile.CORE,
            title="The Way of Kings",
            parent=author.item_id,
            parent_title=author.title,
            index=1,
            series="The Stormlight Archive",
            series_position="1",
            part_count=2,
            guids=parse_guids("com.plexapp.agents.audnexus://B003ZWFO7E"),
        ),
        AudiobookItem(
            item_id=_id(BOOKS, "412"),
            fetched=FetchProfile.CORE,
            title="Edgedancer",
            parent=author.item_id,
            parent_title=author.title,
            index=2,
            series="The Stormlight Archive",
            # A novella. The position is a string precisely because of this.
            series_position="2.5",
            part_count=1,
        ),
    ]
    parts = [
        AudiobookPartItem(
            item_id=_id(BOOKS, f"42{index}"),
            fetched=FetchProfile.CORE,
            title=f"Part {index}",
            parent=books[0].item_id,
            grandparent=author.item_id,
            index=index,
            duration_ms=20_000_000,
            parts=(
                FilePart(
                    media_id=f"94{index}",
                    part_id=f"14{index}",
                    path=f"/media/Books/Sanderson/The Way of Kings/CD{index}.m4b",
                    container="m4b",
                ),
            ),
        )
        for index in (1, 2)
    ]

    return (*movies, show, *seasons, *episodes, lone_show, author, *books, *parts)


@dataclass
class FakeLibrary:
    """An in-memory `LibraryProvider`. Nothing here touches a network."""

    records: dict[str, NormalizedItem] = field(default_factory=dict)
    sections_: tuple[SectionRef, ...] = SECTIONS
    unsupported: frozenset[str] = UNSUPPORTED_ARTIST_SECTIONS
    missing: frozenset[str] = frozenset()
    # Ids whose fetch raises LibraryUnavailable -- a server that went away after
    # the session's own retries were exhausted, which must abort the whole export
    # rather than drop a family.
    unavailable: frozenset[str] = frozenset()
    get_item_calls: list[str] = field(default_factory=list)

    @classmethod
    def build(cls, **kwargs) -> "FakeLibrary":
        records = {str(record.item_id): record for record in _library_records()}
        return cls(records=records, **kwargs)

    # -- protocol ---------------------------------------------------------

    def provider_info(self) -> ProviderInfo:
        return ProviderInfo(
            provider=PROVIDER,
            server_id="0123456789abcdef",
            server_version="1.41.0.0000",
            platform="Linux",
        )

    def sections(self) -> tuple[SectionRef, ...]:
        return self.sections_

    def list_items(
        self,
        section_id: str,
        offset: int,
        limit: int,
        media_kind: MediaKind | None = None,
    ) -> Page[ItemStub]:
        if section_id in self.unsupported:
            raise LibraryUnsupported(
                f"section {section_id!r} looks like a music library, not audiobooks: "
                "not audiobooks [sampled 3/3] agent_identifier=no"
            )
        matches = [
            record
            for record in self._ordered()
            if record.item_id.section_id == section_id
            and (media_kind is None or record.media_kind is media_kind)
        ]
        window = matches[offset : offset + limit]
        return Page[ItemStub](
            items=tuple(self._stub(record) for record in window),
            total=len(matches),
            offset=offset,
            returned=len(window),
        )

    def get_item(
        self, item_id: ItemId, profile: FetchProfile = FetchProfile.CORE
    ) -> NormalizedItem:
        key = str(item_id)
        self.get_item_calls.append(key)
        if key in self.unavailable:
            raise LibraryUnavailable(f"the server went away while fetching {key}")
        if key in self.missing or key not in self.records:
            raise LibraryItemNotFound(f"no item {key}")
        return with_changes(self.records[key], {"fetched": profile})

    def get_children(self, item_id: ItemId, offset: int, limit: int) -> Page[ItemStub]:
        matches = [
            record for record in self._ordered() if getattr(record, "parent", None) == item_id
        ]
        window = matches[offset : offset + limit]
        return Page[ItemStub](
            items=tuple(self._stub(record) for record in window),
            total=len(matches),
            offset=offset,
            returned=len(window),
        )

    def get_files(self, item_id: ItemId) -> tuple[FilePart, ...]:
        return tuple(getattr(self.records[str(item_id)], "parts", ()))

    def find_similar(self, section_id: str, title: str, limit: int) -> tuple[ItemStub, ...]:
        return tuple(
            self._stub(record)
            for record in self._ordered()
            if record.item_id.section_id == section_id and title.lower() in record.title.lower()
        )[:limit]

    # -- helpers ----------------------------------------------------------

    def _ordered(self) -> list[NormalizedItem]:
        """Insertion order, deliberately not sorted.

        The export must impose its own ordering. Serving records pre-sorted would
        let an unordered writer pass the byte-identity test by accident.
        """
        return list(self.records.values())

    def _stub(self, record: NormalizedItem) -> ItemStub:
        return ItemStub(
            item_id=record.item_id,
            media_kind=record.media_kind,
            title=record.title,
            year=getattr(record, "year", None),
        )


@pytest.fixture
def library() -> FakeLibrary:
    return FakeLibrary.build()
