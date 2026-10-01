"""An offline Plex server, at plexapi's one network seam.

`PlexServer.query` is the only way plexapi reaches a server: construction,
`library`, `fetchItems`, `fetchItem` and `reload` all go through it.
`FakePlexServer` overrides that one method and nothing else. So everything above
it runs exactly as it does against a real server: the paging loop, the copy of
`librarySectionID` from container to item, element-to-class dispatch, and the
include keys `reload` builds. A fake built any higher -- a hand-made object with a
`fetchItem` method -- would test our code against our own idea of plexapi.

The library it serves is assembled from the committed fixtures, which were
captured from a real server (`scripts/capture_fixtures.py`) and then scrubbed. A
response container carries `librarySectionID`, as a real one does. The scrub
removed it from the elements themselves.

Where the fake must decide something only a server decides -- the order of a
listing, what `title=` matches -- the decision is marked. Step 0.7.4 replaces
those decisions with the snapshot's named model functions, so the offline suite
can show `PlexLibrary`'s client logic agrees with them. The live suite then shows
whether Plex does.

Every request is recorded. A request the fake does not route is a test failure,
never a guess: an unrouted path means either a new plexapi behavior or a bug, and
guessing would hide both.
"""

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET

from plexapi.exceptions import NotFound
from plexapi.server import PlexServer
from plexapi.utils import REVERSESEARCHTYPES

FIXTURES = Path(__file__).parent.parent / "fixtures" / "plex"


@dataclass(frozen=True)
class Section:
    key: str
    type: str
    title: str
    agent: str


SECTIONS: tuple[Section, ...] = (
    Section("1", "movie", "Movies", "tv.plex.agents.movie"),
    Section("2", "show", "TV Shows", "tv.plex.agents.series"),
    Section("3", "artist", "Audiobooks", "com.plexapp.agents.audnexus"),
    Section("4", "artist", "Music", "tv.plex.agents.music"),
    Section("5", "photo", "Photos", "com.plexapp.agents.none"),
)

# Fixture -> the section it is served from. `movie_nfd_path` is left out: it shares
# rating key 1702 with `movie_legacy_agent`, and one server cannot hold both.
LIBRARY: dict[str, str] = {
    "movie_new_agent": "1",
    "movie_legacy_agent": "1",
    "movie_no_guids": "1",
    "show": "2",
    "season": "2",
    "episode": "2",
    "author": "3",
    "audiobook": "3",
    "audiobook_part": "3",
    "music_track": "4",
}

ROOT = ET.Element(
    "MediaContainer",
    machineIdentifier="fake-machine-identifier",
    version="1.41.0.0000",
    platform="Linux",
    friendlyName="fake",
)

LEAF_TYPES = frozenset({"movie", "episode", "track"})


def load_element(name: str) -> ET.Element:
    return ET.fromstring((FIXTURES / f"{name}.xml").read_text(encoding="utf-8"))


class FakePlexServer(PlexServer):
    """A `PlexServer` whose `query` answers from fixtures.

    `omit_section_id` serves metadata containers without `librarySectionID`, the
    case `PlexLibrary` must refuse rather than stamp the caller's section onto.
    """

    def __init__(
        self,
        sections: tuple[Section, ...] = SECTIONS,
        library: dict[str, str] | None = None,
        *,
        omit_section_id: bool = False,
    ) -> None:
        self.queries: list[tuple[str, dict[str, str]]] = []
        self._sections = {section.key: section for section in sections}
        self._omit_section_id = omit_section_id
        # rating key -> (section key, element). Insertion order is the listing
        # order until step 0.7.4 -- a decision only a server makes.
        self._items: dict[str, tuple[str, ET.Element]] = {}
        for name, section_key in (LIBRARY if library is None else library).items():
            element = load_element(name)
            self._items[element.attrib["ratingKey"]] = (section_key, element)
        super().__init__("http://fake.invalid", "fake-token")

    # -- the seam ---------------------------------------------------------

    def query(self, key, method=None, headers=None, params=None, timeout=None, **kwargs):
        self.queries.append((key, dict(headers or {})))
        split = urlsplit(key)
        path = split.path.rstrip("/") or "/"
        query = {name: values[-1] for name, values in parse_qs(split.query).items()}
        query.update(params or {})
        segments = path.strip("/").split("/")

        if path == "/":
            return deepcopy(ROOT)
        if path == "/library":
            return ET.Element("MediaContainer", identifier="com.plexapp.plugins.library")
        if path == "/library/sections":
            return self._section_directory()
        if len(segments) == 4 and segments[:2] == ["library", "sections"] and segments[3] == "all":
            return self._all(self._section(segments[2]), query, headers or {})
        if len(segments) == 3 and segments[:2] == ["library", "metadata"]:
            return self._metadata(segments[2])
        is_children = len(segments) == 4 and segments[3] == "children"
        if is_children and segments[:2] == ["library", "metadata"]:
            return self._children(segments[2], headers or {})
        raise AssertionError(f"FakePlexServer does not route {key!r}; extend it or find the bug")

    # -- routes -----------------------------------------------------------

    def _section_directory(self) -> ET.Element:
        container = ET.Element("MediaContainer", size=str(len(self._sections)))
        for section in self._sections.values():
            ET.SubElement(
                container,
                "Directory",
                key=section.key,
                type=section.type,
                title=section.title,
                agent=section.agent,
            )
        return container

    def _section(self, key: str) -> Section:
        if key not in self._sections:
            raise NotFound(f"(404) not_found; http://fake.invalid/library/sections/{key}")
        return self._sections[key]

    def _all(self, section: Section, query: dict[str, str], headers: dict) -> ET.Element:
        # No `type` means the section's own type: what Plex does for an untyped
        # `/all`, by the plexapi docstring. A server decision, live-checked.
        libtype = REVERSESEARCHTYPES[int(query["type"])] if "type" in query else section.type
        matches = [
            element
            for section_key, element in self._items.values()
            if section_key == section.key and element.attrib.get("type") == libtype
        ]
        if "title" in query:
            # Case-insensitive substring: a server decision, replaced by
            # `snapshot.title_matches` in step 0.7.4 and live-checked after.
            wanted = query["title"].casefold()
            matches = [e for e in matches if wanted in e.attrib.get("title", "").casefold()]
        return self._page(matches, headers, section.key)

    def _metadata(self, rating_key: str) -> ET.Element:
        section_key, element = self._lookup(rating_key)
        attributes = {"size": "1"}
        if not self._omit_section_id:
            attributes["librarySectionID"] = section_key
        container = ET.Element("MediaContainer", attributes)
        container.append(deepcopy(element))
        return container

    def _children(self, rating_key: str, headers: dict) -> ET.Element:
        section_key, element = self._lookup(rating_key)
        if element.attrib.get("type") in LEAF_TYPES:
            raise AssertionError(
                f"asked for the children of {rating_key}, a {element.attrib['type']}. "
                "PlexLibrary answers a leaf without asking the server."
            )
        children = sorted(
            (e for _, e in self._items.values() if e.attrib.get("parentRatingKey") == rating_key),
            key=lambda e: int(e.attrib.get("index", "0")),
        )
        return self._page(children, headers, section_key)

    # -- helpers ----------------------------------------------------------

    def _lookup(self, rating_key: str) -> tuple[str, ET.Element]:
        if rating_key not in self._items:
            raise NotFound(f"(404) not_found; http://fake.invalid/library/metadata/{rating_key}")
        return self._items[rating_key]

    def _page(self, matches: list[ET.Element], headers: dict, section_key: str) -> ET.Element:
        start = int(headers.get("X-Plex-Container-Start", 0))
        size = int(headers.get("X-Plex-Container-Size", len(matches)))
        if start < 0 or size < 0:
            raise AssertionError(
                f"a negative paging header reached the server (start={start}, size={size}); "
                "check_page should have refused it before any request"
            )
        window = matches[start : start + size]
        container = ET.Element(
            "MediaContainer",
            size=str(len(window)),
            totalSize=str(len(matches)),
            offset=str(start),
            librarySectionID=section_key,
        )
        container.extend(deepcopy(element) for element in window)
        return container

    def requests_since(self, mark: int) -> list[str]:
        """The keys requested after `mark = len(server.queries)` was taken."""
        return [key for key, _ in self.queries[mark:]]
