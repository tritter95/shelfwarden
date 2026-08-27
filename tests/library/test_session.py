"""The HTTP session.

plexapi has no retry, backoff, or rate limiting, and collapses every status that
is not 401 or 404 into BadRequest. This module supplies the first three and
recovers the status for the fourth.
"""

import pytest

from shelfwarden.library.session import (
    RETRY_STATUSES,
    StatusRecorder,
    build_session,
    status_from_message,
)


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class TestStatusRecorder:
    def test_it_remembers_the_most_recent_status(self):
        recorder = StatusRecorder()
        recorder(FakeResponse(503))
        assert recorder.last_status == 503

    def test_a_later_response_replaces_an_earlier_one(self):
        recorder = StatusRecorder()
        recorder(FakeResponse(503))
        recorder(FakeResponse(200))
        assert recorder.last_status == 200

    def test_clear_resets_between_calls(self):
        """Each public method clears first, so a stale status cannot mislabel the
        next failure."""
        recorder = StatusRecorder()
        recorder(FakeResponse(429))
        recorder.clear()
        assert recorder.last_status is None

    def test_the_hook_returns_the_response_unchanged(self):
        response = FakeResponse(200)
        assert StatusRecorder()(response) is response


class TestStatusFromMessage:
    """The fallback: plexapi formats failures as '(NNN) codename; url body'."""

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("(503) service_unavailable; http://x/y server error", 503),
            ("(429) too_many_requests; http://x", 429),
            ("(404) not_found; http://x", 404),
            ("no status here", None),
            ("", None),
            ("(bad) not a number", None),
        ],
    )
    def test_it_recovers_the_status_or_admits_it_cannot(self, message, expected):
        assert status_from_message(message) == expected


class TestBuildSession:
    def test_it_retries_the_transient_statuses(self):
        _session, _recorder = build_session()
        assert 429 in RETRY_STATUSES
        assert 503 in RETRY_STATUSES
        assert 401 not in RETRY_STATUSES  # retrying a bad token never helps
        assert 404 not in RETRY_STATUSES

    def test_retries_are_confined_to_get(self):
        """A blanket retry policy is how a mutating request gets replayed by
        accident in Phase 3."""
        session, _ = build_session()
        adapter = session.get_adapter("https://example.invalid")
        assert adapter.max_retries.allowed_methods == frozenset({"GET"})

    def test_it_obeys_a_retry_after_header_rather_than_guessing(self):
        session, _ = build_session()
        assert session.get_adapter("https://example.invalid").max_retries.respect_retry_after_header

    def test_the_recorder_is_attached_as_a_response_hook(self):
        session, recorder = build_session()
        assert recorder in session.hooks["response"]

    def test_both_schemes_are_mounted(self):
        session, _ = build_session()
        for url in ("http://example.invalid", "https://example.invalid"):
            assert session.get_adapter(url).max_retries.total == 3
