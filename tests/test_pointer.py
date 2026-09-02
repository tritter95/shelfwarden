"""One pointer grammar, and the two ways it could quietly answer the wrong thing.

The gate for this module is that it addresses every leaf of a dumped item of
every media kind -- if it cannot, a corruption cannot record what it changed.
Everything else here defends a specific silent-wrong-answer:

* **A missing location raises.** Returning `None` would let a witness record a
  resolved value for a field its evidence body does not have, which is a claim
  with no referent wearing a citation.
* **The wildcard is illegal in a pointer and legal in a selector.** A change
  addresses one location; a constraint may address many. If a `FieldChange` could
  carry `/parts/*/path`, its reverse would have no single value to write back.
* **`~1` decodes before `~0`.** Otherwise `~01` decodes to `/` rather than to
  `~1`, and two different pointers name one location.
"""

from datetime import UTC, date, datetime

import pytest

from shelfwarden.models.ids import ItemId, parse_guids
from shelfwarden.models.item import (
    AudiobookItem,
    AudiobookPartItem,
    AuthorItem,
    EpisodeItem,
    FetchProfile,
    FilePart,
    MovieItem,
    NormalizedItem,
    SeasonItem,
    ShowItem,
    dump_item,
)
from shelfwarden.pointer import (
    WILDCARD,
    PointerError,
    build,
    escape,
    has_wildcard,
    parse,
    resolve,
    select,
    set_at,
    unescape,
)


def _id(rating_key: str) -> ItemId:
    return ItemId("test", "1", rating_key)


def _movie() -> MovieItem:
    return MovieItem(
        item_id=_id("101"),
        fetched=FetchProfile.CORE,
        title="Amélie",
        year=2001,
        summary="A film.",
        rating=8.3,
        originally_available_at=date(2001, 4, 25),
        added_at=datetime(2020, 1, 1, tzinfo=UTC),
        guids=parse_guids("plex://movie/101", ["tmdb://194"]),
        locked_fields=("title",),
        parts=(
            FilePart(
                media_id="9",
                part_id="1",
                path="/media/Movies/Amélie (2001)/Amélie (2001).mkv",
                container="mkv",
                video_resolution="1080",
            ),
            FilePart(media_id="9", part_id="2", path="/media/Movies/Amélie (2001)/part2.mkv"),
        ),
    )


def _every_kind() -> tuple[NormalizedItem, ...]:
    show = ShowItem(item_id=_id("201"), fetched=FetchProfile.CORE, title="Cowboy Bebop", year=1998)
    season = SeasonItem(
        item_id=_id("211"),
        fetched=FetchProfile.CORE,
        title="Season 1",
        parent=show.item_id,
        index=1,
    )
    author = AuthorItem(item_id=_id("401"), fetched=FetchProfile.CORE, title="Brandon Sanderson")
    book = AudiobookItem(
        item_id=_id("411"),
        fetched=FetchProfile.CORE,
        title="The Way of Kings",
        parent=author.item_id,
        series="The Stormlight Archive",
        series_position="1",
    )
    return (
        _movie(),
        show,
        season,
        EpisodeItem(
            item_id=_id("221"),
            fetched=FetchProfile.CORE,
            title="Asteroid Blues",
            parent=season.item_id,
            grandparent=show.item_id,
            index=1,
            parent_index=1,
            parts=(FilePart(part_id="13", path="/media/TV/Cowboy Bebop/S01E01.mkv"),),
        ),
        author,
        book,
        AudiobookPartItem(
            item_id=_id("421"),
            fetched=FetchProfile.CORE,
            title="Part 1",
            parent=book.item_id,
            grandparent=author.item_id,
            index=1,
            parts=(FilePart(part_id="14", path="/media/Books/CD1.m4b"),),
        ),
    )


def _leaves(document: object, prefix: str = "") -> list[str]:
    """Every pointer in a document, including the interior ones."""
    found = [prefix]
    if isinstance(document, dict):
        for key, value in document.items():
            found += _leaves(value, prefix + "/" + escape(key))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            found += _leaves(value, f"{prefix}/{index}")
    return found


class TestGrammar:
    def test_the_empty_pointer_is_the_whole_document(self):
        document = {"a": 1}
        assert parse("") == ()
        assert resolve(document, "") is document

    def test_a_pointer_without_a_leading_slash_names_its_own_fix(self):
        with pytest.raises(PointerError, match="'/parts/0'"):
            parse("parts/0")

    def test_escaping_round_trips_and_decodes_in_the_right_order(self):
        # `~01` must decode to `~1`, not to `/`. Decoding `~0` first would produce
        # `~1` and then decode *that* to `/`, so two pointers would name one place.
        assert unescape("~01") == "~1"
        assert unescape("~1") == "/"
        assert escape("a/b~c") == "a~1b~0c"
        for token in ("a/b", "a~b", "~01", "*", "plain"):
            assert parse(build([token])) == (token,)

    def test_a_slash_in_a_key_is_addressable(self):
        assert resolve({"a/b": 7}, "/a~1b") == 7


class TestResolve:
    def test_it_addresses_every_leaf_of_every_media_kind(self):
        # The gate: a corruption records what it changed as a pointer, so a field
        # this grammar cannot name is a field no corruption can touch.
        for item in _every_kind():
            document = dump_item(item)
            for pointer in _leaves(document):
                resolve(document, pointer)

    def test_a_missing_key_raises_rather_than_returning_none(self):
        with pytest.raises(PointerError, match="no key 'nope'"):
            resolve(dump_item(_movie()), "/nope")

    def test_descending_into_a_scalar_says_so(self):
        with pytest.raises(PointerError, match="is a int"):
            resolve({"year": 2001}, "/year/0")

    def test_a_wildcard_is_not_a_pointer(self):
        with pytest.raises(PointerError, match="use `select`"):
            resolve(dump_item(_movie()), "/parts/*/path")

    @pytest.mark.parametrize(
        ("pointer", "match"),
        [
            ("/parts/-", "names the position after the last element"),
            ("/parts/01", "not an array index"),
            ("/parts/x", "not an array index"),
            ("/parts/9", "out of range"),
        ],
    )
    def test_array_indices_are_strict(self, pointer, match):
        with pytest.raises(PointerError, match=match):
            resolve(dump_item(_movie()), pointer)


class TestSelect:
    def test_a_wildcard_matches_every_element_in_document_order(self):
        document = dump_item(_movie())
        assert select(document, "/parts/*/path") == (
            ("/parts/0/path", "/media/Movies/Amélie (2001)/Amélie (2001).mkv"),
            ("/parts/1/path", "/media/Movies/Amélie (2001)/part2.mkv"),
        )

    def test_a_plain_pointer_is_a_selector_matching_one_thing(self):
        assert select(dump_item(_movie()), "/year") == (("/year", 2001),)

    def test_a_selector_matching_nothing_is_empty_rather_than_an_error(self):
        # `must_not_change: ["/parts/*/path"]` on an item with no parts is
        # vacuously satisfied, not a broken constraint.
        show = ShowItem(item_id=_id("201"), fetched=FetchProfile.CORE, title="Cowboy Bebop")
        assert select(dump_item(show), "/parts/*/path") == ()
        assert select(dump_item(_movie()), "/nope/*") == ()

    def test_a_wildcard_over_a_mapping_walks_keys_in_sorted_order(self):
        document = {"m": {"b": 2, "a": 1, "c": 3}}
        assert select(document, "/m/*") == (("/m/a", 1), ("/m/b", 2), ("/m/c", 3))

    def test_a_literal_star_key_raises_rather_than_being_reinterpreted(self):
        with pytest.raises(PointerError, match="literal '\\*' key"):
            select({"a": {WILDCARD: 1}}, "/a/*")

    def test_has_wildcard_reads_segments_not_substrings(self):
        assert has_wildcard("/parts/*/path")
        assert not has_wildcard("/a*b")


class TestSetAt:
    def test_it_replaces_a_nested_value_in_place(self):
        document = dump_item(_movie())
        set_at(document, "/parts/1/path", "/media/other.mkv")
        assert document["parts"][1]["path"] == "/media/other.mkv"
        assert document["parts"][0]["path"].endswith("Amélie (2001).mkv")

    def test_it_cannot_create(self):
        # A dump carries every field, including the unset ones, so a path that
        # names nothing is a typo. Creating it would put a key into the record
        # that `extra="forbid"` rejects later, far from the cause.
        with pytest.raises(PointerError, match="no key 'nope' to replace"):
            set_at(dump_item(_movie()), "/nope", 1)

    def test_it_refuses_a_wildcard_and_the_whole_document(self):
        document = dump_item(_movie())
        with pytest.raises(PointerError, match="addresses one location"):
            set_at(document, "/parts/*/path", "x")
        with pytest.raises(PointerError, match="whole document"):
            set_at(document, "", {})
