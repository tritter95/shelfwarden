"""The normalized item model.

The round-trip test is step 0.2's gate. Everything downstream -- byte-identical
exports, content-addressed evidence, the `apply_reverse` invariant, scorer
comparison -- rests on an item surviving a trip through canonical JSON unchanged.
"""

import unicodedata
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from shelfwarden.canonical import canonical_json
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
    Page,
    SeasonItem,
    SectionRef,
    ShowItem,
    dump_item,
    load_item,
    with_changes,
)


def _id(rating_key: str) -> ItemId:
    return ItemId("plex", "3", rating_key)


SAMPLES = {
    MediaKind.MOVIE: MovieItem(
        item_id=_id("1701"),
        fetched=FetchProfile.FULL,
        title="Amélie",
        year=2001,
        edition_title="Director's Cut",
        rating=8.3,
        originally_available_at=date(2001, 4, 25),
        guids=parse_guids("plex://movie/abc", ["tmdb://194", "imdb://tt0211915"]),
        locked_fields=("title",),
        parts=(
            FilePart(
                media_id="9",
                part_id="10",
                path="/media/Amélie (2001).mkv",
                container="mkv",
                size_bytes=1,
            ),
        ),
        added_at=datetime(2024, 1, 1, tzinfo=UTC),
    ),
    MediaKind.SHOW: ShowItem(
        item_id=_id("2"), fetched=FetchProfile.FULL, title="The Wire", year=2002, leaf_count=60
    ),
    MediaKind.SEASON: SeasonItem(
        item_id=_id("3"), fetched=FetchProfile.CORE, title="Season 1", parent=_id("2"), index=1
    ),
    MediaKind.EPISODE: EpisodeItem(
        item_id=_id("4"),
        fetched=FetchProfile.FULL,
        title="The Target",
        parent=_id("3"),
        grandparent=_id("2"),
        index=1,
        parent_index=1,
    ),
    MediaKind.AUTHOR: AuthorItem(
        item_id=_id("5"), fetched=FetchProfile.CORE, title="Brandon Sanderson", album_count=12
    ),
    MediaKind.AUDIOBOOK: AudiobookItem(
        item_id=_id("6"),
        fetched=FetchProfile.FULL,
        title="Words of Radiance",
        parent=_id("5"),
        series="The Stormlight Archive",
        series_position="2",
    ),
    MediaKind.AUDIOBOOK_PART: AudiobookPartItem(
        item_id=_id("7"), fetched=FetchProfile.STUB, title="Chapter 1", parent=_id("6"), index=1
    ),
}


class TestRoundTrip:
    """Step 0.2's gate."""

    @pytest.mark.parametrize("kind", list(MediaKind))
    def test_every_media_kind_round_trips(self, kind):
        item = SAMPLES[kind]
        assert load_item(canonical_json(dump_item(item))) == item

    @pytest.mark.parametrize("kind", list(MediaKind))
    def test_every_media_kind_is_byte_stable(self, kind):
        """Equality is not enough: the export is compared as bytes."""
        item = SAMPLES[kind]
        once = canonical_json(dump_item(item))
        twice = canonical_json(dump_item(load_item(once)))
        assert once == twice

    @pytest.mark.parametrize("kind", list(MediaKind))
    def test_the_discriminator_selects_the_right_subtype(self, kind):
        assert type(load_item(dump_item(SAMPLES[kind]))) is type(SAMPLES[kind])

    def test_a_sample_exists_for_every_kind(self):
        """So adding a media kind cannot quietly go untested."""
        assert set(SAMPLES) == set(MediaKind)


class TestCanonicalForm:
    def test_titles_are_nfc_normalized_on_the_way_in(self):
        decomposed = unicodedata.normalize("NFD", "Amélie")
        item = MovieItem(item_id=_id("1"), fetched=FetchProfile.FULL, title=decomposed)
        assert item.title == "Amélie"
        assert canonical_json(dump_item(item)) == canonical_json(
            dump_item(MovieItem(item_id=_id("1"), fetched=FetchProfile.FULL, title="Amélie"))
        )

    def test_file_paths_are_deliberately_not_normalized(self):
        """A path is an argument to a filesystem operation; normalizing it can
        produce a string that names no file on disk."""
        decomposed = unicodedata.normalize("NFD", "/media/Amélie.mkv")
        assert FilePart(path=decomposed).path == decomposed

    def test_guids_are_stored_sorted_regardless_of_input_order(self):
        forward = MovieItem(
            item_id=_id("1"),
            fetched=FetchProfile.FULL,
            title="x",
            guids=parse_guids(None, ["tvdb://9", "imdb://tt1", "tmdb://278"]),
        )
        backward = MovieItem(
            item_id=_id("1"),
            fetched=FetchProfile.FULL,
            title="x",
            guids=parse_guids(None, ["tmdb://278", "tvdb://9", "imdb://tt1"]),
        )
        assert canonical_json(dump_item(forward)) == canonical_json(dump_item(backward))

    def test_locked_fields_are_deduplicated_and_sorted(self):
        item = MovieItem(
            item_id=_id("1"),
            fetched=FetchProfile.FULL,
            title="x",
            locked_fields=("title", "art", "title"),
        )
        assert item.locked_fields == ("art", "title")

    def test_nulls_are_written_rather_than_omitted(self):
        """A fixed shape keeps diffs stable and keeps 'absent' visible."""
        assert b'"summary":null' in canonical_json(dump_item(SAMPLES[MediaKind.SHOW]))


class TestTimestamps:
    def test_a_naive_datetime_is_rejected(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            MovieItem(
                item_id=_id("1"),
                fetched=FetchProfile.FULL,
                title="x",
                added_at=datetime(2024, 1, 1),
            )

    def test_the_same_instant_in_any_offset_serializes_identically(self):
        """Serialization renders a datetime by representation, not by instant."""
        as_utc = MovieItem(
            item_id=_id("1"),
            fetched=FetchProfile.FULL,
            title="x",
            added_at=datetime(2024, 1, 1, 5, tzinfo=UTC),
        )
        as_offset = MovieItem(
            item_id=_id("1"),
            fetched=FetchProfile.FULL,
            title="x",
            added_at=datetime(2024, 1, 1, 0, tzinfo=timezone(timedelta(hours=-5))),
        )
        assert canonical_json(dump_item(as_utc)) == canonical_json(dump_item(as_offset))


class TestWithChanges:
    def test_it_validates_where_model_copy_does_not(self):
        """model_copy(update=...) skips validation even on a frozen model with
        extra='forbid'. Corruption functions mutate through with_changes so a
        type-invalid item cannot reach a truth file."""
        item = SAMPLES[MediaKind.MOVIE]

        assert item.model_copy(update={"year": "nineteen ninety five"}).year == (
            "nineteen ninety five"
        )
        with pytest.raises(ValidationError):
            with_changes(item, {"year": "nineteen ninety five"})

    def test_it_applies_a_valid_change(self):
        assert with_changes(SAMPLES[MediaKind.MOVIE], {"year": 2002}).year == 2002

    def test_it_leaves_the_original_untouched(self):
        item = SAMPLES[MediaKind.MOVIE]
        with_changes(item, {"year": 1900})
        assert item.year == 2001

    def test_it_rejects_an_unknown_field(self):
        with pytest.raises(ValidationError):
            with_changes(SAMPLES[MediaKind.MOVIE], {"not_a_field": 1})

    def test_it_preserves_the_media_kind(self):
        changed = with_changes(SAMPLES[MediaKind.EPISODE], {"parent_index": 2})
        assert isinstance(changed, EpisodeItem)
        assert changed.parent_index == 2


class TestModelDiscipline:
    def test_items_are_frozen(self):
        with pytest.raises(ValidationError):
            SAMPLES[MediaKind.MOVIE].title = "something else"

    def test_unknown_fields_are_rejected_at_parse_time(self):
        payload = dump_item(SAMPLES[MediaKind.MOVIE]) | {"bogus": 1}
        with pytest.raises(ValidationError):
            load_item(payload)

    def test_fetch_profile_travels_with_the_record(self):
        """'No external ids' and 'nobody asked' are different facts."""
        stub = load_item(dump_item(SAMPLES[MediaKind.AUDIOBOOK_PART]))
        assert stub.fetched is FetchProfile.STUB
        assert stub.guids == ()


class TestSupportingTypes:
    def test_page_reports_explicit_counts(self):
        page = Page[ItemStub](
            items=(ItemStub(item_id=_id("1"), media_kind=MediaKind.MOVIE, title="Heat"),),
            total=42,
            offset=0,
            returned=1,
        )
        assert (page.total, page.returned) == (42, 1)
        assert Page[ItemStub].model_validate(page.model_dump(mode="json")) == page

    def test_section_ref_round_trips(self):
        section = SectionRef(
            section_id="3", title="Movies", section_type="movie", agent="tv.plex.agents.movie"
        )
        assert SectionRef.model_validate(section.model_dump(mode="json")) == section

    def test_item_stub_carries_identity_and_little_else(self):
        """Tool results are resent every turn, so a listing stays minimal."""
        assert set(ItemStub.model_fields) == {"item_id", "media_kind", "title", "year"}


class TestPartIdentity:
    """Step 0.4, Decision 4. A part had no name at all before this.

    `filename_unmatchable` (0.5) rewrites one part's path and its truth record has
    to say which; Phase 3's rename has to find that same part again to revert it.
    `parts[2]` is a positional identifier, which is exactly what invariant 9
    rejects.
    """

    def test_the_ids_round_trip(self):
        item = SAMPLES[MediaKind.MOVIE]
        (part,) = load_item(canonical_json(dump_item(item))).parts
        assert (part.media_id, part.part_id) == ("9", "10")

    def test_they_are_optional_because_a_provider_need_not_have_them(self):
        """SnapshotLibrary (0.7) and any non-Plex provider are free to omit them."""
        part = FilePart(path="/media/x.mkv")
        assert part.media_id is None and part.part_id is None

    def test_duplicate_part_ids_within_an_item_are_rejected(self):
        """A duplicated Media/Part block would make `part_id` useless as an address
        for the one thing it exists to address, and would do so silently."""
        with pytest.raises(ValidationError, match="part_id"):
            MovieItem(
                item_id=_id("1"),
                fetched=FetchProfile.FULL,
                title="x",
                parts=(
                    FilePart(part_id="10", path="/media/a.mkv"),
                    FilePart(part_id="10", path="/media/b.mkv"),
                ),
            )

    def test_the_rejection_names_the_offending_id(self):
        with pytest.raises(ValidationError, match="'77'"):
            MovieItem(
                item_id=_id("1"),
                fetched=FetchProfile.FULL,
                title="x",
                parts=(
                    FilePart(part_id="77", path="/media/a.mkv"),
                    FilePart(part_id="88", path="/media/b.mkv"),
                    FilePart(part_id="77", path="/media/c.mkv"),
                ),
            )

    def test_several_parts_without_ids_are_fine(self):
        """Only ids that are present are checked -- `None` is absence, not a value."""
        item = MovieItem(
            item_id=_id("1"),
            fetched=FetchProfile.FULL,
            title="x",
            parts=(FilePart(path="/media/a.mkv"), FilePart(path="/media/b.mkv")),
        )
        assert len(item.parts) == 2

    def test_a_shared_media_id_across_parts_is_allowed(self):
        """One Media element legitimately holds several Parts -- a split file. It
        is the Part id that has to be unique."""
        item = MovieItem(
            item_id=_id("1"),
            fetched=FetchProfile.FULL,
            title="x",
            parts=(
                FilePart(media_id="9", part_id="10", path="/media/cd1.mkv"),
                FilePart(media_id="9", part_id="11", path="/media/cd2.mkv"),
            ),
        )
        assert {part.media_id for part in item.parts} == {"9"}

    @pytest.mark.parametrize(
        "model", [MovieItem, EpisodeItem, AudiobookPartItem], ids=lambda m: m.__name__
    )
    def test_every_kind_that_carries_parts_enforces_it(self, model):
        """So a kind added later cannot quietly opt out of the check."""
        with pytest.raises(ValidationError, match="part_id"):
            model(
                item_id=_id("1"),
                fetched=FetchProfile.FULL,
                title="x",
                parts=(
                    FilePart(part_id="1", path="/media/a.mkv"),
                    FilePart(part_id="1", path="/media/b.mkv"),
                ),
            )

    def test_part_order_is_preserved_not_sorted_by_id(self):
        """Order carries meaning -- disc order, and the split `multi_file_split`
        operates on. Sorting would destroy information to buy a stability
        guarantee no offline test can actually prove."""
        item = MovieItem(
            item_id=_id("1"),
            fetched=FetchProfile.FULL,
            title="x",
            parts=(
                FilePart(part_id="20", path="/media/cd1.mkv"),
                FilePart(part_id="3", path="/media/cd2.mkv"),
            ),
        )
        assert [part.part_id for part in item.parts] == ["20", "3"]
        reloaded = load_item(canonical_json(dump_item(item)))
        assert [part.part_id for part in reloaded.parts] == ["20", "3"]
