"""The HTTP session PlexLibrary hands to plexapi.

plexapi has no retry, no backoff, and no rate limiting anywhere, and its default
timeout is 30s with any non-2xx raising immediately. It does accept a `session`
argument, which is the seam this module uses to supply all three.

It also solves a second problem. `PlexServer.query` maps 401 to Unauthorized, 404
to NotFound, and *everything else* -- 429, 500, 502, 503 alike -- to BadRequest, so
retryable and terminal conditions are indistinguishable by exception type. The
status survives only inside the message string. A response hook records it
instead, so the translation layer classifies on a number.
"""

import re
import threading

from requests import Session
from requests.adapters import HTTPAdapter, Retry

# Statuses worth retrying: rate limiting and the transient 5xx family. 500 is
# included because Plex returns it for conditions that clear on their own.
RETRY_STATUSES: tuple[int, ...] = (429, 500, 502, 503, 504)
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 0.5

# PlexServer.query formats every failure as "(NNN) codename; url body".
_STATUS_IN_MESSAGE = re.compile(r"^\((\d{3})\)")


class StatusRecorder:
    """Remembers the status of the most recent response on a session.

    plexapi raises before the caller sees the response, so this is the only place
    the code is available as an integer rather than as prose.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_status: int | None = None

    def __call__(self, response, *args, **kwargs):  # requests response hook
        with self._lock:
            self._last_status = response.status_code
        return response

    @property
    def last_status(self) -> int | None:
        with self._lock:
            return self._last_status

    def clear(self) -> None:
        with self._lock:
            self._last_status = None


def status_from_message(message: str) -> int | None:
    """Recover the status code from a plexapi exception message.

    A fallback for when the hook did not fire -- a message format is a string
    contract with a library that never promised one, so it is not the primary
    path.
    """
    match = _STATUS_IN_MESSAGE.match(message)
    return int(match.group(1)) if match else None


def build_session(
    *,
    retries: int = DEFAULT_RETRIES,
    backoff_factor: float = DEFAULT_BACKOFF,
) -> tuple[Session, StatusRecorder]:
    """A session with retry, backoff, and status capture.

    Retries are confined to GET: every read in this project is a GET, and a blanket
    retry policy is how a mutating request gets replayed by accident in Phase 3.
    `respect_retry_after_header` means a server that tells us how long to wait is
    obeyed rather than guessed at.
    """
    session = Session()
    recorder = StatusRecorder()
    session.hooks["response"].append(recorder)

    retry = Retry(
        total=retries,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset({"GET"}),
        backoff_factor=backoff_factor,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session, recorder
