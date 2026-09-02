"""Families with shapes the shared fixture library does not have.

`tests/evals/conftest.py` is the export's fixture and is deliberately left alone:
0.4 and 0.45 assert byte-identity and census counts against it, and widening it to
suit step 0.5 would move numbers in tests that are about something else.

So the shapes only 0.5 needs -- an edition marker in a folder, a three-book series
whose folders carry positions, a book whose files live under a series directory --
are built here instead.
"""

import pytest

from shelfwarden.evals.corrupt.context import stub_of
from shelfwarden.models.ids import ItemId, parse_guids
from shelfwarden.models.item import (
    AudiobookItem,
    AudiobookPartItem,
    AuthorItem,
    FetchProfile,
    FilePart,
    MovieItem,
    NormalizedItem,
)

PROVIDER = "fake"
MOVIES = "1"
BOOKS = "3"


def survey_inputs(items):
    """`(items, roots)` for `run_corruptions`, with roots derived rather than typed."""
    roots = tuple(stub_of(item) for item in items if getattr(item, "parent", None) is None)
    return tuple(items), roots


def _movie_with_edition(rating_key: str, title: str, year: int, edition: str) -> MovieItem:
    folder = f"{title} ({year}) [{edition}]"
    return MovieItem(
        item_id=ItemId(PROVIDER, MOVIES, rating_key),
        fetched=FetchProfile.CORE,
        title=title,
        year=year,
        summary=f"{title} is a film.",
        edition_title=edition,
        guids=parse_guids(f"plex://movie/{rating_key}", [f"tmdb://{rating_key}"]),
        parts=(
            FilePart(
                media_id=f"9{rating_key}",
                part_id=f"1{rating_key}",
                path=f"/media/Movies/{folder}/{folder}.mkv",
                container="mkv",
                video_resolution="1080",
            ),
        ),
    )


@pytest.fixture
def edition_family() -> tuple[NormalizedItem, ...]:
    """Two cuts of one film, each named in its own folder."""
    return (
        _movie_with_edition("501", "Blade Runner", 1982, "Final Cut"),
        _movie_with_edition("502", "Blade Runner", 1982, "Theatrical"),
    )


def _series_author() -> tuple[NormalizedItem, ...]:
    author = AuthorItem(
        item_id=ItemId(PROVIDER, BOOKS, "601"),
        fetched=FetchProfile.CORE,
        title="Brandon Sanderson",
        album_count=3,
    )
    series = "The Stormlight Archive"
    books: list[NormalizedItem] = []
    parts: list[NormalizedItem] = []
    for ordinal, name in enumerate(("The Way of Kings", "Words of Radiance", "Oathbringer"), 1):
        key = f"61{ordinal}"
        folder = f"/media/Books/Sanderson/{series}/Book {ordinal} - {name}"
        books.append(
            AudiobookItem(
                item_id=ItemId(PROVIDER, BOOKS, key),
                fetched=FetchProfile.CORE,
                title=f"Book {ordinal} - {name}",
                parent=author.item_id,
                parent_title=author.title,
                index=ordinal,
                series=series,
                series_position=str(ordinal),
                part_count=2 if ordinal == 1 else 1,
                guids=parse_guids(f"com.plexapp.agents.audnexus://B00{ordinal}ZWFO7E"),
            )
        )
        for disc in (1, 2) if ordinal == 1 else (1,):
            parts.append(
                AudiobookPartItem(
                    item_id=ItemId(PROVIDER, BOOKS, f"{key}{disc}"),
                    fetched=FetchProfile.CORE,
                    title=f"Part {disc}",
                    parent=books[-1].item_id,
                    grandparent=author.item_id,
                    index=disc,
                    duration_ms=20_000_000,
                    parts=(FilePart(part_id=f"7{key}{disc}", path=f"{folder}/CD{disc}.m4b"),),
                )
            )
    return (author, *books, *parts)


@pytest.fixture
def series_family() -> tuple[NormalizedItem, ...]:
    """One author, three positioned books under a series folder, one of them split."""
    return _series_author()
