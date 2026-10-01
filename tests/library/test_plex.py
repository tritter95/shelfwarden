"""The Plex adapter: the auto-reload guarantee, mapping, paging, and translation.

Every test here runs offline. The stub server raises on any query, so a passing
suite is also evidence that nothing reached for the network.
"""

import os
import unicodedata
from datetime import UTC
from xml.etree import ElementTree as ET

import plexapi
import pytest
from plexapi.exceptions import BadRequest, NotFound, TwoFactorRequired, Unauthorized, Unsupported
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout

from shelfwarden.canonical import canonical_json
from shelfwarden.compare import SupportStrength, compare_title, parse_release_name
from shelfwarden.library import plex as plex_module
from shelfwarden.library.base import (
    LibraryAuthError,
    LibraryInvalidArgument,
    LibraryItemNotFound,
    LibraryProtocolError,
    LibraryProvider,
    LibraryRateLimited,
    LibraryRequestError,
    LibrarySectionNotFound,
    LibraryUnavailable,
    LibraryUnsupported,
    Retryability,
)
from shelfwarden.library.plex import (
    PlexLibrary,
    configure_plexapi,
    effective_request_params,
    hash_server_id,
    normalize_item,
)
from shelfwarden.library.session import StatusRecorder
from shelfwarden.models.ids import IdNamespace, ItemId
from shelfwarden.models.item import FetchProfile, MediaKind, dump_item
from tests.library.conftest import RecordingServer, StubServer, build, load_fixture
from tests.library.fake_plex import FakePlexServer

# STUB is excluded deliberately: it describes what a listing returned, not
# something a caller can ask the server for. See effective_request_params.
REQUESTABLE_PROFILES = [FetchProfile.CORE, FetchProfile.FULL]

ALL_FIXTURES = [
    ("movie_new_agent", MediaKind.MOVIE),
    ("movie_legacy_agent", MediaKind.MOVIE),
    ("movie_no_guids", MediaKind.MOVIE),
    ("show", MediaKind.SHOW),
    ("season", MediaKind.SEASON),
    ("episode", MediaKind.EPISODE),
    ("author", MediaKind.AUTHOR),
    ("audiobook", MediaKind.AUDIOBOOK),
    ("audiobook_part", MediaKind.AUDIOBOOK_PART),
]


class TestAutoReload:
    """Finding: the config switch accepts only lowercase "false"/"0" and swallows
    anything else into a permissive default, so it fails *open*."""

    def test_capital_false_does_not_disable_autoreload(self, monkeypatch):
        monkeypatch.setenv("PLEXAPI_PLEXAPI_AUTORELOAD", "False")
        assert plexapi.CONFIG.get("plexapi.autoreload", True, bool) is True

    @pytest.mark.parametrize("value", ["false", "0"])
    def test_only_the_exact_lowercase_forms_work(self, monkeypatch, value):
        monkeypatch.setenv("PLEXAPI_PLEXAPI_AUTORELOAD", value)
        assert plexapi.CONFIG.get("plexapi.autoreload", True, bool) is False

    def test_configure_overrides_a_hostile_environment(self, monkeypatch):
        """Environment is consulted before the config file, so a developer with
        PLEXAPI_PLEXAPI_AUTORELOAD='False' exported would otherwise win."""
        monkeypatch.setenv("PLEXAPI_PLEXAPI_AUTORELOAD", "False")
        configure_plexapi()
        assert os.environ["PLEXAPI_PLEXAPI_AUTORELOAD"] == "false"
        assert plex_module.autoreload_is_off()

    def test_touching_a_none_attribute_does_not_hit_the_network(self):
        """The tripwire. On a partial object this is exactly what triggers the
        silent refetch; the stub server turns that into a test failure."""
        movie = load_fixture("movie_no_guids")
        assert movie.originalTitle is None  # would raise if auto-reload were live

    def test_the_tripwire_would_fire_if_autoreload_were_live(self, monkeypatch):
        """So the test above cannot pass for the wrong reason."""
        monkeypatch.setenv("PLEXAPI_PLEXAPI_AUTORELOAD", "true")
        movie = build(
            ET.fromstring('<Video ratingKey="1" key="/library/metadata/1" type="movie" title="X"/>')
        )
        with pytest.raises(AssertionError, match="network access"):
            _ = movie.originalTitle

    def test_construction_is_refused_when_autoreload_cannot_be_disabled(self, monkeypatch):
        monkeypatch.setattr(plex_module, "autoreload_is_off", lambda: False)
        with pytest.raises(LibraryProtocolError, match="auto-reload"):
            PlexLibrary(server=StubServer())


class TestTimestamps:
    def test_plexapi_returns_naive_local_time_without_configuration(self, monkeypatch):
        """Left alone, an export would depend on the timezone of the machine that
        produced it: epoch 1704067200 is 2024-01-01T00:00:00Z everywhere, but
        plexapi renders it in local time with no offset attached."""
        import plexapi.utils as utils

        monkeypatch.setattr(utils, "DATETIME_TIMEZONE", None)
        assert utils.toDatetime("1704067200").tzinfo is None

    def test_the_timezone_does_not_depend_on_a_system_tzdata_entry(self):
        """setDatetimeTimezone() resolves an IANA name and turns a lookup failure
        into tzinfo=None behind a log warning -- the same fails-open pattern as
        auto-reload. It cost a red CI run. The stdlib constant needs no tzdata."""
        import plexapi.utils as utils

        configure_plexapi()
        assert utils.DATETIME_TIMEZONE is UTC

    def test_configure_makes_timestamps_aware_and_correct(self):
        configure_plexapi()
        item = normalize_item(load_fixture("movie_new_agent"), "3", FetchProfile.FULL)
        assert item.added_at is not None
        assert item.added_at.isoformat() == "2024-01-01T00:00:00+00:00"

    def test_a_naive_timestamp_is_refused_rather_than_guessed(self, monkeypatch):
        import plexapi.utils as utils

        monkeypatch.setattr(utils, "DATETIME_TIMEZONE", None)
        movie = load_fixture("movie_new_agent")
        with pytest.raises(LibraryProtocolError, match="naive datetime"):
            normalize_item(movie, "3", FetchProfile.FULL)


class TestMapping:
    @pytest.mark.parametrize(("name", "kind"), ALL_FIXTURES)
    def test_every_fixture_maps_to_the_expected_kind(self, name, kind):
        item = normalize_item(load_fixture(name), "3", FetchProfile.FULL)
        assert item.media_kind is kind

    @pytest.mark.parametrize(("name", "_kind"), ALL_FIXTURES)
    def test_every_mapped_item_survives_canonical_json(self, name, _kind):
        item = normalize_item(load_fixture(name), "3", FetchProfile.FULL)
        assert canonical_json(dump_item(item))

    def test_a_new_agent_movie_maps_completely(self):
        item = normalize_item(load_fixture("movie_new_agent"), "3", FetchProfile.FULL)
        assert item.item_id == ItemId("plex", "3", "1701")
        assert item.title == "Amélie"
        assert item.year == 2001
        assert item.edition_title == "Director's Cut"
        assert item.rating == 8.3
        assert {g.namespace for g in item.guids} == {
            IdNamespace.PLEX,
            IdNamespace.IMDB,
            IdNamespace.TMDB,
        }
        assert item.has_thumb is True

    def test_only_locked_fields_are_captured(self):
        """Phase 3's revert must restore lock state, not just values -- and every
        plexapi edit helper defaults to locked=True, so this is easy to acquire
        by accident."""
        item = normalize_item(load_fixture("movie_new_agent"), "3", FetchProfile.FULL)
        assert item.locked_fields == ("title",)  # summary is locked="0"

    def test_a_legacy_agent_movie_yields_the_same_external_id(self):
        """The legacy single-guid form and the modern Guid children collapse to
        the same shape -- the reason a legacy library is analysable at all."""
        new = normalize_item(load_fixture("movie_new_agent"), "3", FetchProfile.FULL)
        legacy = normalize_item(load_fixture("movie_legacy_agent"), "3", FetchProfile.FULL)
        imdb = {(g.namespace, g.value) for g in legacy.guids}
        assert (IdNamespace.IMDB, "tt0211915") in imdb
        assert (IdNamespace.IMDB, "tt0211915") in {(g.namespace, g.value) for g in new.guids}

    def test_an_unmatched_movie_has_no_guids(self):
        item = normalize_item(load_fixture("movie_no_guids"), "3", FetchProfile.FULL)
        assert item.guids == ()
        assert item.fetched is FetchProfile.FULL  # so the emptiness is real

    def test_episode_numbering_is_mapped_to_the_fields_corruptions_move(self):
        item = normalize_item(load_fixture("episode"), "3", FetchProfile.FULL)
        assert (item.parent_index, item.index) == (1, 1)
        assert item.grandparent == ItemId("plex", "3", "2")

    def test_file_parts_are_mapped_with_their_path_unnormalized(self):
        item = normalize_item(load_fixture("movie_new_agent"), "3", FetchProfile.FULL)
        (part,) = item.parts
        assert part.path.endswith("Amélie (2001).mkv")
        assert part.container == "mkv"
        assert part.video_resolution == "1080"
        assert part.size_bytes == 8000000000

    def test_an_nfd_path_reaches_the_model_undecomposed_while_the_title_is_composed(self):
        """The trap in `compare.fold_text`, shown to be reachable from real data.

        Text crossing into the model is NFC-normalized; `FilePart.path` is
        deliberately exempt, because a path is an argument to a future filesystem
        operation and an NFC-normalized NFD path may name nothing on disk. That
        exemption is correct and it is also the *only* door decomposed text uses
        to reach the comparators -- so the fold, not the model, has to close it.
        """
        item = normalize_item(load_fixture("movie_nfd_path"), "3", FetchProfile.FULL)
        (part,) = item.parts
        assert unicodedata.is_normalized("NFC", item.title)
        assert not unicodedata.is_normalized("NFC", part.path)
        assert part.path != unicodedata.normalize("NFC", part.path)

        # And the comparison site closes it, which is what makes the exemption
        # affordable. `filename_unmatchable` is exactly this comparison.
        parsed = parse_release_name(part.path)
        assert compare_title(parsed.title, item.title).strength is SupportStrength.NORMALIZED

    def test_empty_strings_become_none(self):
        obj = build(
            ET.fromstring(
                '<Video ratingKey="1" key="/library/metadata/1" type="movie" title="X" summary=""/>'
            )
        )
        assert normalize_item(obj, "3", FetchProfile.CORE).summary is None

    def test_an_unmappable_type_is_a_protocol_error(self):
        obj = build(ET.fromstring('<Directory ratingKey="1" key="/k" type="photo" title="X"/>'))
        with pytest.raises(LibraryProtocolError, match="unmappable"):
            normalize_item(obj, "3", FetchProfile.CORE)


class TestConfinement:
    def test_no_plexapi_object_escapes_through_a_return_value(self):
        """lint-imports proves no module imports plexapi. It does not prove no
        plexapi *object* leaves through a return value; static and dynamic checks
        catch different mistakes."""
        item = normalize_item(load_fixture("movie_new_agent"), "3", FetchProfile.FULL)
        seen: list[object] = []

        def walk(value):
            module = type(value).__module__
            assert not module.startswith("plexapi"), f"plexapi type escaped: {type(value)}"
            if hasattr(value, "__dict__") or hasattr(value, "__slots__"):
                seen.append(value)
            if isinstance(value, tuple | list):
                for entry in value:
                    walk(entry)

        walk(item)
        for part in item.parts:
            walk(part)
        for guid in item.guids:
            walk(guid)
        assert seen  # the walk actually visited something


class TestErrorTranslation:
    @pytest.mark.parametrize(
        ("exc", "status", "expected"),
        [
            (Unauthorized("(401) unauthorized"), 401, LibraryAuthError),
            (TwoFactorRequired("2fa"), None, LibraryAuthError),
            (NotFound("(404) not_found"), 404, LibraryItemNotFound),
            (BadRequest("(429) too_many_requests"), 429, LibraryRateLimited),
            (BadRequest("(503) service_unavailable"), 503, LibraryUnavailable),
            (BadRequest("(500) internal_server_error"), 500, LibraryUnavailable),
            (BadRequest("(400) bad_request"), 400, LibraryRequestError),
            (Unsupported("nope"), None, LibraryRequestError),
            (Timeout("timed out"), None, LibraryUnavailable),
            (RequestsConnectionError("refused"), None, LibraryUnavailable),
        ],
    )
    def test_every_failure_maps_onto_the_taxonomy(self, exc, status, expected):
        assert isinstance(plex_module._translate(exc, status), expected)

    def test_the_status_is_recovered_from_the_message_when_the_hook_missed_it(self):
        """plexapi collapses 429, 500 and 503 into BadRequest; without the status
        they would all read as terminal."""
        assert isinstance(plex_module._translate(BadRequest("(503) x"), None), LibraryUnavailable)
        assert isinstance(plex_module._translate(BadRequest("(429) x"), None), LibraryRateLimited)

    def test_the_hook_status_wins_over_the_message(self):
        translated = plex_module._translate(BadRequest("(400) mislabelled"), 503)
        assert isinstance(translated, LibraryUnavailable)

    def test_a_plexapi_error_never_escapes_a_public_method(self):
        class Boom(StubServer):
            @property
            def library(self):
                raise BadRequest("(503) service_unavailable")

        provider = PlexLibrary(server=Boom(), recorder=StatusRecorder())
        with pytest.raises(LibraryUnavailable):
            provider.sections()


SECTION_XML = """
<MediaContainer size="2" totalSize="137" offset="0">
  <Video ratingKey="1701" key="/library/metadata/1701" type="movie" title="Amélie" year="2001"/>
  <Video ratingKey="1702" key="/library/metadata/1702" type="movie" title="Heat" year="1995"/>
</MediaContainer>
"""


class FakeSection:
    """A section that records exactly how search() was called."""

    key = "3"
    type = "movie"
    title = "Movies"
    agent = "tv.plex.agents.movie"
    totalSize = 137

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        server = RecordingServer(SECTION_XML)
        container = ET.fromstring(SECTION_XML)
        return [build(element, server) for element in container]


class SectionServer(StubServer):
    def __init__(self, section) -> None:
        super().__init__()
        self._section = section
        self.library = type("Lib", (), {"sections": lambda _self: [section]})()


class TestPaging:
    def _provider(self):
        section = FakeSection()
        return PlexLibrary(server=SectionServer(section)), section

    def test_list_items_passes_both_container_start_and_maxresults(self):
        """container_start alone does not fetch a page: fetchItems keeps looping
        until it has the entire remaining result set, and nothing in the result
        says that happened."""
        provider, section = self._provider()
        provider.list_items("3", offset=20, limit=5)
        (call,) = section.calls
        assert call["container_start"] == 20
        assert call["maxresults"] == 5

    def test_page_total_comes_from_the_response_not_from_len(self):
        provider, _ = self._provider()
        page = provider.list_items("3", offset=0, limit=5)
        assert page.total == 137
        assert page.returned == 2
        assert page.returned != page.total  # a page is not the population

    def test_list_items_requires_a_limit(self):
        """No default: a forgotten limit is a full library walk, not a slow path."""
        provider, _ = self._provider()
        with pytest.raises(TypeError):
            provider.list_items("3", 0)

    def test_stubs_carry_identity_and_little_else(self):
        provider, _ = self._provider()
        page = provider.list_items("3", offset=0, limit=5)
        assert page.items[0].item_id == ItemId("plex", "3", "1701")
        assert page.items[0].title == "Amélie"

    def test_an_unknown_section_is_correctable(self):
        provider, _ = self._provider()
        with pytest.raises(LibraryItemNotFound):
            provider.list_items("999", offset=0, limit=5)


class TestSections:
    def test_sections_are_mapped_with_their_agent(self):
        """The agent identifier is what audiobook detection keys off."""
        provider = PlexLibrary(server=SectionServer(FakeSection()))
        (section,) = provider.sections()
        assert section.section_id == "3"
        assert section.agent == "tv.plex.agents.movie"
        assert section.section_type == "movie"


def test_plex_library_satisfies_the_protocol():
    assert isinstance(PlexLibrary(server=StubServer()), LibraryProvider)


class TestProviderInfo:
    """Step 0.4 added this so the export manifest could name its source without
    `evals/export.py` reaching through `PlexLibrary._server`."""

    class IdentifiedServer(StubServer):
        machineIdentifier = "abc123-real-machine-identifier"
        version = "1.41.0.1234"
        platform = "Linux"

    def test_the_machine_identifier_is_hashed_never_recorded_raw(self):
        """It is durable and server-unique, which is exactly what makes it worth
        recording and exactly what makes recording it verbatim a leak.
        capture_fixtures.py already scrubs it; this keeps the two consistent."""
        info = PlexLibrary(server=self.IdentifiedServer()).provider_info()
        assert info.server_id == hash_server_id("abc123-real-machine-identifier")
        assert "abc123" not in info.server_id
        assert len(info.server_id) == 16

    def test_the_hash_is_stable_across_calls_and_processes(self):
        """A manifest's only question is "same server?"; a per-process salt would
        make two exports of one library look like two libraries."""
        assert hash_server_id("m") == hash_server_id("m")
        assert hash_server_id("m") != hash_server_id("n")

    def test_version_and_platform_travel_verbatim(self):
        info = PlexLibrary(server=self.IdentifiedServer()).provider_info()
        assert info.provider == "plex"
        assert info.server_version == "1.41.0.1234"
        assert info.platform == "Linux"

    def test_a_server_that_will_not_say_reads_as_unknown(self):
        """Absent provenance is recorded as absent rather than guessed at."""
        info = PlexLibrary(server=StubServer()).provider_info()
        assert info.server_id == "unknown"
        assert info.server_version is None

    def test_it_is_a_read_and_therefore_translates_its_errors(self):
        class Boom(StubServer):
            @property
            def machineIdentifier(self):
                raise BadRequest("(503) service_unavailable")

        with pytest.raises(LibraryUnavailable):
            PlexLibrary(server=Boom(), recorder=StatusRecorder()).provider_info()


class TestEffectiveRequestParams:
    """Finding 1 of step 0.4. `RELOAD_INCLUDES` is a set of *overrides*; recording
    it in a manifest would understate what produced a record while looking
    authoritative."""

    @pytest.mark.parametrize("profile", REQUESTABLE_PROFILES)
    def test_include_fields_survives_although_we_never_ask_for_it(self, profile):
        """The one non-falsy default among the eleven keys we do not override.
        `_buildDetailsKey` drops False/0/'0' and keeps everything else, and
        `includeFields` defaults to a *string*. Its name is a trap: it selects
        blur hashes and has nothing to do with the <Field> lock elements."""
        assert effective_request_params(profile)["includeFields"] == "thumbBlurHash,artBlurHash"

    def test_a_listing_profile_is_refused_by_name(self):
        """STUB marks a record that came from a listing, which plexapi builds with
        a different key builder entirely. A bare KeyError would name neither the
        cause nor the fix."""
        with pytest.raises(ValueError, match="core, full"):
            effective_request_params(FetchProfile.STUB)

    def test_the_two_profiles_differ_by_exactly_check_files(self):
        """Which is why CORE is the export default: checkFiles buys a server-side
        stat per part and maps to no field this model carries."""
        core = effective_request_params(FetchProfile.CORE)
        full = effective_request_params(FetchProfile.FULL)
        assert set(full) - set(core) == {"checkFiles"}
        assert set(core) - set(full) == set()
        assert full["checkFiles"] == "1"

    @pytest.mark.parametrize("profile", REQUESTABLE_PROFILES)
    def test_nothing_falsy_is_reported_as_requested(self, profile):
        """A parameter plexapi drops must not appear in the manifest: "asked for
        and got nothing" and "never asked" have to stay different facts."""
        params = effective_request_params(profile)
        assert all(value not in ("", "0", "False") for value in params.values())

    def test_it_matches_what_plexapi_actually_builds(self):
        """Pinned against plexapi's own key builder rather than against our
        reading of it, so a change in its defaults fails here."""
        from urllib.parse import parse_qs, urlparse

        movie = load_fixture("movie_new_agent")
        for profile in REQUESTABLE_PROFILES:
            key = movie._buildDetailsKey(**plex_module.RELOAD_INCLUDES[profile])
            built = {name: values[0] for name, values in parse_qs(urlparse(key).query).items()}
            assert built == effective_request_params(profile)

    def test_the_effective_set_is_uniform_across_every_media_kind(self):
        """`_INCLUDES` is defined once on PlexPartialObject and no subclass
        overrides it -- worth pinning, since one that did would silently make the
        manifest wrong for that kind alone."""
        from urllib.parse import parse_qs, urlparse

        expected = effective_request_params(FetchProfile.CORE)
        for name, _ in ALL_FIXTURES:
            key = load_fixture(name)._buildDetailsKey(
                **plex_module.RELOAD_INCLUDES[FetchProfile.CORE]
            )
            built = {n: values[0] for n, values in parse_qs(urlparse(key).query).items()}
            assert built == expected, name


# -- edge semantics (step 0.7.2) ------------------------------------------
#
# The rows of §4.2 in docs/plans/step-0.7-snapshot-provider.md, run against a real
# PlexServer whose only fake part is `query` (tests/library/fake_plex.py). Each row
# that changed failed against the 0.7.1 code first; the rest pin what already held.


@pytest.fixture
def fake():
    return FakePlexServer()


@pytest.fixture
def provider(fake):
    return PlexLibrary(server=fake)


def _plex(section_id: str, rating_key: str) -> ItemId:
    return ItemId("plex", section_id, rating_key)


def _call(provider, method: str, item_id: ItemId):
    if method == "get_children":
        return provider.get_children(item_id, 0, 10)
    return getattr(provider, method)(item_id)


ITEM_METHODS = ["get_item", "get_files", "get_children"]


class TestArgumentsAreRefusedBeforeAnyRequest:
    """Refused locally, because the server's answer to them is undefined. The fake
    server would also fail any request that carried a negative paging header."""

    @pytest.mark.parametrize(("offset", "limit"), [(-1, 10), (0, -1)])
    def test_list_items_paging(self, fake, provider, offset, limit):
        mark = len(fake.queries)
        with pytest.raises(LibraryInvalidArgument):
            provider.list_items("1", offset, limit)
        assert fake.requests_since(mark) == []

    @pytest.mark.parametrize(("offset", "limit"), [(-1, 10), (0, -1)])
    def test_get_children_paging(self, fake, provider, offset, limit):
        mark = len(fake.queries)
        with pytest.raises(LibraryInvalidArgument):
            provider.get_children(_plex("2", "2"), offset, limit)
        assert fake.requests_since(mark) == []

    @pytest.mark.parametrize(("title", "limit"), [("", 5), ("   ", 5), ("Heat", -1)])
    def test_find_similar(self, fake, provider, title, limit):
        mark = len(fake.queries)
        with pytest.raises(LibraryInvalidArgument) as caught:
            provider.find_similar("1", title, limit)
        assert caught.value.next_action
        assert fake.requests_since(mark) == []

    @pytest.mark.parametrize("method", ITEM_METHODS)
    @pytest.mark.parametrize("key", ["sw3f9a", "abc", "17O1", "١٢"])
    def test_a_rating_key_that_is_not_decimal(self, fake, provider, method, key):
        """It used to raise a bare `ValueError` from `int()`, outside the taxonomy.
        `١٢` is digits to `str.isdigit` and to `int()`, but no Plex key is written
        that way."""
        mark = len(fake.queries)
        with pytest.raises(LibraryItemNotFound):
            _call(provider, method, _plex("1", key))
        assert fake.requests_since(mark) == []

    def test_a_stub_profile_is_a_programming_error(self, fake, provider):
        """It used to raise a bare `KeyError` that named neither cause nor fix."""
        mark = len(fake.queries)
        with pytest.raises(ValueError, match="what a listing returns"):
            provider.get_item(_plex("1", "1701"), FetchProfile.STUB)
        assert fake.requests_since(mark) == []


class TestAnIdNamesWhereTheItemIs:
    def test_an_item_fetched_by_its_own_id_carries_that_id(self, provider):
        item = provider.get_item(_plex("1", "1701"))
        assert item.item_id == _plex("1", "1701")
        assert item.title == "Amélie"

    @pytest.mark.parametrize("method", ITEM_METHODS)
    def test_a_wrong_section_is_refused_and_the_right_id_named(self, provider, method):
        """It used to come back stamped `plex:2:1701` -- a well-formed id for a
        section the film is not in."""
        with pytest.raises(LibraryItemNotFound) as caught:
            _call(provider, method, _plex("2", "1701"))
        assert caught.value.next_action == "use plex:1:1701"

    def test_a_response_that_does_not_name_its_section_is_refused(self):
        provider = PlexLibrary(server=FakePlexServer(omit_section_id=True))
        with pytest.raises(LibraryProtocolError, match="did not say which section"):
            provider.get_item(_plex("1", "1701"))

    @pytest.mark.parametrize("method", ITEM_METHODS)
    def test_an_item_in_a_music_section_is_not_read_as_an_audiobook(self, provider, method):
        """A listing already refused the music section; a fetch by id read its
        track as an `AudiobookPartItem`. Found while building 0.7.2."""
        with pytest.raises(LibraryUnsupported, match="music library"):
            _call(provider, method, _plex("4", "900"))

    @pytest.mark.parametrize("method", ITEM_METHODS)
    def test_an_unknown_rating_key_is_correctable(self, provider, method):
        with pytest.raises(LibraryItemNotFound) as caught:
            _call(provider, method, _plex("1", "99999"))
        assert caught.value.retryability is Retryability.CORRECTABLE


class TestListing:
    def test_the_default_kind_is_the_sections_root(self, provider):
        page = provider.list_items("2", 0, 10)
        assert {stub.media_kind for stub in page.items} == {MediaKind.SHOW}

    def test_any_kind_the_section_holds_can_be_asked_for(self, provider):
        page = provider.list_items("2", 0, 10, MediaKind.EPISODE)
        assert [str(stub.item_id) for stub in page.items] == ["plex:2:4"]

    def test_a_kind_the_section_cannot_hold_is_refused_before_the_search(self, fake, provider):
        """It used to be sent, and the answer was whatever the server made of
        episodes in a movie section."""
        mark = len(fake.queries)
        with pytest.raises(LibraryInvalidArgument, match="movie section holds movie") as caught:
            provider.list_items("1", 0, 10, MediaKind.EPISODE)
        assert "movie" in caught.value.next_action
        assert not any("/all" in key for key in fake.requests_since(mark))

    def test_a_count_only_page_carries_the_true_total(self, provider):
        page = provider.list_items("1", 0, 0)
        assert (page.returned, page.total) == (0, 3)

    def test_an_offset_past_the_end_is_an_empty_page_not_an_error(self, provider):
        page = provider.list_items("1", 10, 5)
        assert (page.returned, page.total, page.offset) == (0, 3, 10)

    def test_an_unknown_section_says_to_list_the_sections(self, provider):
        """It used to repeat the advice for a stale *item* id."""
        with pytest.raises(LibrarySectionNotFound) as caught:
            provider.list_items("999", 0, 5)
        assert "sections" in caught.value.next_action
        assert isinstance(caught.value, LibraryItemNotFound)

    def test_an_unmodelled_section_is_unsupported(self, provider):
        with pytest.raises(LibraryUnsupported, match="photo"):
            provider.list_items("5", 0, 5)


class TestChildren:
    def test_a_show_lists_its_seasons(self, provider):
        page = provider.get_children(_plex("2", "2"), 0, 10)
        assert [str(stub.item_id) for stub in page.items] == ["plex:2:3"]

    @pytest.mark.parametrize(("section_id", "key"), [("1", "1701"), ("2", "4"), ("3", "7")])
    def test_a_leaf_has_no_children_and_the_server_is_not_asked(
        self, fake, provider, section_id, key
    ):
        mark = len(fake.queries)
        page = provider.get_children(_plex(section_id, key), 0, 10)
        assert (page.items, page.total, page.returned) == ((), 0, 0)
        assert not any(k.endswith("/children") for k in fake.requests_since(mark))

    def test_a_non_leaf_has_no_files(self, provider):
        assert provider.get_files(_plex("2", "2")) == ()


class TestFindSimilar:
    def test_the_root_kind_is_sent_rather_than_left_to_the_server(self, fake, provider):
        mark = len(fake.queries)
        provider.find_similar("1", "Amé", 5)
        (search,) = [key for key in fake.requests_since(mark) if "/all" in key]
        assert "type=1" in search

    def test_a_limit_of_zero_finds_nothing(self, provider):
        assert provider.find_similar("1", "Amé", 0) == ()

    def test_an_audiobook_section_finds_authors_not_books(self, provider):
        """Faithful to Plex, and a recorded limit: a book cannot be found by its
        own title through this method."""
        (author,) = provider.find_similar("3", "Sanderson", 5)
        assert author.media_kind is MediaKind.AUTHOR
        assert provider.find_similar("3", "Words of Radiance", 5) == ()
