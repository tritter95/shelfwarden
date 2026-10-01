"""The read-only guarantee and the error taxonomy.

The first test here is step 0.3's gate, and the reason spec §3.2 is structural
rather than aspirational.
"""

import pytest

from shelfwarden.library import plex as plex_module
from shelfwarden.library.base import (
    LIVE_PROVIDERS,
    MUTATING_METHODS,
    SECTION_KINDS,
    SECTION_ROOT_KIND,
    LibraryAuthError,
    LibraryError,
    LibraryInvalidArgument,
    LibraryItemNotFound,
    LibraryProvider,
    LibraryRateLimited,
    LibrarySectionNotFound,
    LibraryUnavailable,
    Retryability,
    check_fetchable,
    check_page,
    check_search,
    protocol_methods,
    resolve_kind,
)
from shelfwarden.models.item import FetchProfile, MediaKind


def test_the_protocol_exposes_no_mutating_method():
    """Spec §3.2: mutating tools do not *exist* outside `executing`.

    plexapi has no read-only mode -- every method on it is an HTTP call the
    server accepts based on token permissions -- so this absence is the only
    place the guarantee can live.
    """
    assert protocol_methods(LibraryProvider) & MUTATING_METHODS == frozenset()


def test_the_protocol_still_declares_the_reads_it_should():
    """So the test above cannot pass by the protocol being empty."""
    assert protocol_methods(LibraryProvider) == {
        "provider_info",
        "sections",
        "list_items",
        "get_item",
        "get_children",
        "get_files",
        "find_similar",
    }


def test_mutating_methods_names_the_operations_that_matter():
    for name in ("edit", "merge", "fixMatch", "refresh", "delete", "saveEdits", "unmatch"):
        assert name in MUTATING_METHODS


class TestErrorTaxonomy:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (LibraryAuthError, Retryability.TERMINAL),
            (LibraryItemNotFound, Retryability.CORRECTABLE),
            (LibraryInvalidArgument, Retryability.CORRECTABLE),
            (LibraryRateLimited, Retryability.RETRYABLE),
            (LibraryUnavailable, Retryability.RETRYABLE),
        ],
    )
    def test_each_error_declares_how_it_should_be_treated(self, error, expected):
        assert error.retryability is expected

    def test_a_correctable_error_names_a_next_action(self):
        """CLAUDE.md: a correctable error that does not name a next action is a
        bug. Asserted at construction so no raise site can forget."""
        assert LibraryItemNotFound("gone").next_action

    def test_a_correctable_error_without_a_next_action_cannot_be_constructed(self):
        class Bad(LibraryError):
            retryability = Retryability.CORRECTABLE

        with pytest.raises(ValueError, match="must name a next action"):
            Bad("no guidance offered")

    def test_the_next_action_reaches_the_message(self):
        assert "re-list" in str(LibraryItemNotFound("gone"))

    def test_every_library_error_is_catchable_as_one_type(self):
        for error in (LibraryAuthError, LibraryItemNotFound, LibraryRateLimited):
            assert issubclass(error, LibraryError)

    def test_an_invalid_argument_has_no_default_next_action(self):
        """The fix depends on which argument was wrong, so every raise site must
        name its own. A default would be a vague message nobody had to write."""
        with pytest.raises(ValueError, match="must name a next action"):
            LibraryInvalidArgument("bad argument")


class TestCheckPage:
    @pytest.mark.parametrize(("offset", "limit"), [(0, 0), (0, 1), (5, 100), (10**9, 10**9)])
    def test_any_non_negative_pair_is_answerable(self, offset, limit):
        """Including `limit=0`, Plex's count-only query, and an offset past the end,
        which is an empty page rather than an error."""
        check_page(offset, limit)

    @pytest.mark.parametrize(
        ("offset", "limit", "named"), [(-1, 10, "offset"), (0, -1, "limit"), (-5, -5, "offset")]
    )
    def test_a_negative_argument_is_correctable_and_says_what_to_send(self, offset, limit, named):
        with pytest.raises(LibraryInvalidArgument, match=named) as caught:
            check_page(offset, limit)
        assert caught.value.retryability is Retryability.CORRECTABLE
        assert caught.value.next_action


class TestSectionVocabulary:
    def test_each_section_type_holds_its_root_and_everything_beneath_it(self):
        assert SECTION_KINDS == {
            "movie": (MediaKind.MOVIE,),
            "show": (MediaKind.SHOW, MediaKind.SEASON, MediaKind.EPISODE),
            "artist": (MediaKind.AUTHOR, MediaKind.AUDIOBOOK, MediaKind.AUDIOBOOK_PART),
        }

    def test_every_kind_belongs_to_exactly_one_section_type(self):
        """So "which section can hold this kind" always has one answer."""
        held = [kind for kinds in SECTION_KINDS.values() for kind in kinds]
        assert sorted(held) == sorted(MediaKind)

    def test_a_photo_section_is_not_modelled(self):
        assert "photo" not in SECTION_ROOT_KIND


def test_live_providers_name_exactly_the_live_adapters():
    """Pinned here rather than imported into base.py, which must not depend on the
    adapter. A second live adapter fails this test until it is listed."""
    assert {plex_module.PROVIDER} == LIVE_PROVIDERS


class TestSharedChecks:
    """The checks every provider calls, so their answers cannot drift apart."""

    def test_a_listing_defaults_to_the_sections_root_kind(self):
        assert resolve_kind("show", None) is MediaKind.SHOW

    def test_any_kind_the_section_holds_resolves_to_itself(self):
        assert resolve_kind("artist", MediaKind.AUDIOBOOK_PART) is MediaKind.AUDIOBOOK_PART

    def test_a_kind_the_section_cannot_hold_names_the_ones_it_can(self):
        with pytest.raises(LibraryInvalidArgument, match="show section holds") as caught:
            resolve_kind("show", MediaKind.MOVIE)
        assert "show, season, episode" in caught.value.next_action

    @pytest.mark.parametrize(("title", "limit"), [("", 1), ("  \t", 1), ("Heat", -1)])
    def test_a_search_with_no_defined_answer_is_correctable(self, title, limit):
        with pytest.raises(LibraryInvalidArgument) as caught:
            check_search(title, limit)
        assert caught.value.next_action

    def test_a_search_with_a_limit_of_zero_is_legal(self):
        check_search("Heat", 0)

    def test_a_stub_profile_cannot_be_fetched(self):
        with pytest.raises(ValueError, match="core, full"):
            check_fetchable(FetchProfile.STUB)

    @pytest.mark.parametrize("profile", [FetchProfile.CORE, FetchProfile.FULL])
    def test_a_requestable_profile_passes(self, profile):
        check_fetchable(profile)

    def test_an_unknown_section_is_still_an_unknown_id_with_its_own_advice(self):
        error = LibrarySectionNotFound("no section with id '9'")
        assert isinstance(error, LibraryItemNotFound)
        assert "sections" in error.next_action
        assert error.next_action != LibraryItemNotFound("x").next_action
