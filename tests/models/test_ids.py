"""Identity and guid parsing.

The guid table is the roadmap's explicit gate for step 0.2: both the current-agent
and the legacy `com.plexapp.agents.*` forms must parse. plexapi does none of this
itself -- `media.Guid` exposes `id` as a bare string -- so these cases are the
whole specification of what the project understands.
"""

import pytest

from shelfwarden.models.ids import (
    ExternalId,
    IdNamespace,
    ItemId,
    parse_guid,
    parse_guids,
    sort_external_ids,
)


class TestItemId:
    def test_str_and_parse_round_trip(self):
        item_id = ItemId("plex", "3", "1701")
        assert str(item_id) == "plex:3:1701"
        assert ItemId.parse(str(item_id)) == item_id

    def test_is_hashable_so_it_can_key_a_dict(self):
        assert len({ItemId("plex", "3", "1701"), ItemId("plex", "3", "1701")}) == 1

    def test_provider_keeps_live_and_snapshot_ids_apart(self):
        assert ItemId("plex", "3", "1701") != ItemId("snapshot", "3", "1701")

    @pytest.mark.parametrize("component", ["provider", "section_id", "rating_key"])
    def test_a_colon_in_any_component_is_rejected(self, component):
        parts = {"provider": "plex", "section_id": "3", "rating_key": "1701"}
        parts[component] = "a:b"
        with pytest.raises(ValueError, match="must not contain"):
            ItemId(**parts)

    @pytest.mark.parametrize("component", ["provider", "section_id", "rating_key"])
    def test_an_empty_component_is_rejected(self, component):
        parts = {"provider": "plex", "section_id": "3", "rating_key": "1701"}
        parts[component] = ""
        with pytest.raises(ValueError, match="must not be empty"):
            ItemId(**parts)

    @pytest.mark.parametrize("text", ["plex:3", "plex:3:1701:extra", "nonsense"])
    def test_parsing_a_malformed_string_is_rejected(self, text):
        with pytest.raises(ValueError, match="Expected"):
            ItemId.parse(text)


# (raw, namespace, value, season, episode)
CURRENT_AGENT_GUIDS = [
    ("plex://movie/5d776b9ad0a1a5001f4b5b1c", IdNamespace.PLEX, "movie/5d776b9ad0a1a5001f4b5b1c"),
    ("plex://show/5d9c086c46115600200aa2fe", IdNamespace.PLEX, "show/5d9c086c46115600200aa2fe"),
    ("imdb://tt0944947", IdNamespace.IMDB, "tt0944947"),
    ("tmdb://1399", IdNamespace.TMDB, "1399"),
    ("tvdb://121361", IdNamespace.TVDB, "121361"),
    (
        "mbid://8f6bd1e4-fbe1-4f50-aa9b-94c450ec0f11",
        IdNamespace.MBID,
        "8f6bd1e4-fbe1-4f50-aa9b-94c450ec0f11",
    ),
]

LEGACY_AGENT_GUIDS = [
    ("com.plexapp.agents.imdb://tt0111161?lang=en", IdNamespace.IMDB, "tt0111161", None, None),
    ("com.plexapp.agents.themoviedb://278?lang=en", IdNamespace.TMDB, "278", None, None),
    ("com.plexapp.agents.thetvdb://73739?lang=en", IdNamespace.TVDB, "73739", None, None),
    ("com.plexapp.agents.thetvdb://73739/1/1?lang=en", IdNamespace.TVDB, "73739", 1, 1),
    ("com.plexapp.agents.thetvdb://73739/2", IdNamespace.TVDB, "73739", 2, None),
    ("com.plexapp.agents.hama://tvdb-73739/1/1", IdNamespace.TVDB, "73739", 1, 1),
    (
        "com.plexapp.agents.audnexus://B08G9PRS1K?lang=en",
        IdNamespace.ASIN,
        "B08G9PRS1K",
        None,
        None,
    ),
    ("com.plexapp.agents.none://12345", IdNamespace.LOCAL, "12345", None, None),
]


class TestCurrentAgentGuids:
    @pytest.mark.parametrize(("raw", "namespace", "value"), CURRENT_AGENT_GUIDS)
    def test_parses(self, raw, namespace, value):
        parsed = parse_guid(raw)
        assert (parsed.namespace, parsed.value) == (namespace, value)
        assert parsed.raw == raw
        assert (parsed.season, parsed.episode) == (None, None)

    def test_a_plex_guid_keeps_its_type_component(self):
        """`movie/<hash>` is the identifier; splitting it would discard the type
        and make the value ambiguous against a show with the same hash shape."""
        assert parse_guid("plex://movie/abc123").value == "movie/abc123"


class TestLegacyAgentGuids:
    @pytest.mark.parametrize(("raw", "namespace", "value", "season", "episode"), LEGACY_AGENT_GUIDS)
    def test_parses(self, raw, namespace, value, season, episode):
        parsed = parse_guid(raw)
        assert (parsed.namespace, parsed.value) == (namespace, value)
        assert (parsed.season, parsed.episode) == (season, episode)

    def test_the_raw_string_including_its_query_is_retained(self):
        raw = "com.plexapp.agents.imdb://tt0111161?lang=en"
        assert parse_guid(raw).raw == raw

    def test_the_query_string_is_not_part_of_the_value(self):
        assert parse_guid("com.plexapp.agents.imdb://tt0111161?lang=en").value == "tt0111161"


class TestUnrecognisedGuids:
    """Nothing is ever dropped. An unparseable guid becomes a census number in
    step 0.4 rather than a silent omission -- the 'no silent caps' house rule."""

    @pytest.mark.parametrize(
        "raw",
        [
            "com.plexapp.agents.plexmovie://12345",
            "com.plexapp.agents.lastfm://artist/x",
            "something.else://value",
            "no-scheme-at-all",
            "",
        ],
    )
    def test_becomes_unknown_with_the_raw_string_intact(self, raw):
        parsed = parse_guid(raw)
        assert parsed.namespace is IdNamespace.UNKNOWN
        assert parsed.raw == raw

    def test_a_non_numeric_path_keeps_the_whole_remainder(self):
        """Rather than discarding a path component we did not anticipate."""
        parsed = parse_guid("com.plexapp.agents.thetvdb://73739/special/x")
        assert parsed.value == "73739/special/x"
        assert (parsed.season, parsed.episode) == (None, None)


class TestParseGuids:
    def test_combines_a_primary_guid_with_its_children(self):
        parsed = parse_guids("plex://movie/abc", ["tmdb://278", "imdb://tt0111161"])
        assert {g.namespace for g in parsed} == {
            IdNamespace.PLEX,
            IdNamespace.TMDB,
            IdNamespace.IMDB,
        }

    def test_a_legacy_library_has_only_a_primary_guid(self):
        parsed = parse_guids("com.plexapp.agents.imdb://tt0111161?lang=en", [])
        assert len(parsed) == 1
        assert parsed[0].namespace is IdNamespace.IMDB

    def test_input_order_does_not_change_the_result(self):
        forward = parse_guids(None, ["tmdb://278", "imdb://tt1", "tvdb://9"])
        backward = parse_guids(None, ["tvdb://9", "imdb://tt1", "tmdb://278"])
        assert forward == backward

    def test_duplicates_collapse(self):
        assert len(parse_guids(None, ["tmdb://278", "tmdb://278"])) == 1

    def test_a_missing_primary_is_not_an_error(self):
        assert parse_guids(None, ["tmdb://278"])[0].value == "278"

    def test_empty_strings_are_skipped(self):
        assert parse_guids("", ["", "tmdb://278"]) == parse_guids(None, ["tmdb://278"])


def test_sorting_places_a_series_level_id_before_its_episodes():
    series = ExternalId(IdNamespace.TVDB, "73739", "raw")
    episode = ExternalId(IdNamespace.TVDB, "73739", "raw", season=1, episode=1)
    assert sort_external_ids([episode, series]) == (series, episode)
