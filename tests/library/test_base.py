"""The read-only guarantee and the error taxonomy.

The first test here is step 0.3's gate, and the reason spec §3.2 is structural
rather than aspirational.
"""

import pytest

from shelfwarden.library.base import (
    MUTATING_METHODS,
    LibraryAuthError,
    LibraryError,
    LibraryItemNotFound,
    LibraryProvider,
    LibraryRateLimited,
    LibraryUnavailable,
    Retryability,
    protocol_methods,
)


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
