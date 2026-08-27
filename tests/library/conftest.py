"""Offline plexapi fixtures.

plexapi objects build from XML with no server at all, so step 0.3 needs no live
Plex in CI. The stub server is not just a convenience: any `query()` is an
assertion failure, which makes accidental network access -- including the silent
auto-reload refetch this project exists to prevent -- a red test.
"""

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from shelfwarden.library import plex as plex_module

FIXTURES = Path(__file__).parent.parent / "fixtures" / "plex"


class StubServer:
    """Enough of a PlexServer to build objects. Every query is a test failure."""

    _baseurl = "http://stub"
    _timeout = 30

    def __init__(self) -> None:
        self.queries: list[tuple[str, dict]] = []

    def query(self, key, *args, **kwargs):
        raise AssertionError(
            f"network access during an offline test: query({key!r}). If this came "
            "from touching a None attribute, plexapi auto-reload is live."
        )


class RecordingServer(StubServer):
    """A stub that answers one canned response and records how it was asked.

    Used to assert that paging passes both container_start and maxresults, since
    the arguments are the whole point and the response is not.
    """

    def __init__(self, xml: str) -> None:
        super().__init__()
        self._xml = xml

    def query(self, key, *args, headers=None, params=None, **kwargs):
        self.queries.append((key, {"headers": headers or {}, "params": params or {}}))
        return ET.fromstring(self._xml)


@pytest.fixture(autouse=True)
def _configured():
    """Every test in this package runs with plexapi neutralized, exactly as
    PlexLibrary construction would."""
    plex_module.configure_plexapi()
    assert plex_module.autoreload_is_off()


@pytest.fixture
def server():
    return StubServer()


def load_fixture(name: str):
    """Build a plexapi object from a committed XML fixture."""
    element = ET.fromstring((FIXTURES / f"{name}.xml").read_text(encoding="utf-8"))
    return build(element)


def build(element, server=None):
    from plexapi.utils import PLEXOBJECTS

    etype = element.attrib.get("type")
    ehint = f"{element.tag}.{etype}" if etype else element.tag
    cls = PLEXOBJECTS.get(ehint) or PLEXOBJECTS.get(element.tag)
    if cls is None:
        raise AssertionError(f"no plexapi class for {ehint!r}")
    return cls(server or StubServer(), element, initpath="/library/sections/3/all")
