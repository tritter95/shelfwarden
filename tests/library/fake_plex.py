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
listing, of an item's children, of the sections, and what `title=` matches -- it
decides with the snapshot's named model functions (`library.snapshot`). That is
deliberate, and circular for those four questions by construction. The offline
suite can therefore show only that `PlexLibrary`'s client logic agrees with the
model. Whether Plex agrees with the model is the live suite's question. Each
element is mapped once, with `PlexLibrary`'s own `normalize_item`, so the model
sees exactly the record the adapter would produce.

Every request is recorded. A request the fake does not route is a test failure,
never a guess: an unrouted path means either a new plexapi behavior or a bug, and
guessing would hide both.
"""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET

from plexapi.exceptions import NotFound
from plexapi.server import PlexServer
from plexapi.utils import REVERSESEARCHTYPES

from shelfwarden.library.plex import configure_plexapi, normalize_item
from shelfwarden.library.snapshot import children_key, listing_key, section_key, title_matches
from shelfwarden.models.item import FetchProfile, NormalizedItem, SectionRef
from tests.library.conftest import StubServer, build

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


@dataclass(frozen=True)
class Entry:
    """One served element: a captured fixture, with every change to it listed.

    The changes are the only invented data in the fake, so they are kept as data
    rather than as edits to a copied file, where a reader could not tell captured
    from made-up. Three forms:

    * `"title": "X"` sets an attribute on the element itself;
    * `"Part@id": "X"` sets an attribute on every `Part` beneath it;
    * `"<Guid>": None` removes every `Guid` child. A copy keeps no external id,
      because two items claiming one id is a duplicate the copy did not mean.

    `None` as the value of an attribute form removes that attribute.
    """

    fixture: str
    section: str
    changes: Mapping[str, str | None] = field(default_factory=dict)

    def element(self) -> ET.Element:
        element = load_element(self.fixture)
        for target, value in self.changes.items():
            if target.startswith("<") and target.endswith(">"):
                tag = target[1:-1]
                for parent in list(element.iter()):
                    for child in [c for c in parent if c.tag == tag]:
                        parent.remove(child)
                continue
            tag, _, attribute = target.rpartition("@")
            for node in element.iter(tag) if tag else (element,):
                if value is None:
                    node.attrib.pop(attribute, None)
                else:
                    node.set(attribute, value)
        return element


# The library the fake serves: every captured fixture, plus copies wherever the
# paging and ordering properties need more than one item. Rating keys 8-13 and
# media/part ids from 1101 up are this table's own and collide with nothing
# captured. The captured counts describe the captured library, and this one is
# smaller, so the counts are restated wherever they would otherwise contradict
# the items served beneath them.
LIBRARY: tuple[Entry, ...] = (
    Entry("movie_new_agent", "1"),
    Entry("movie_legacy_agent", "1"),
    Entry("movie_no_guids", "1"),
    # Captured under 1702, which `movie_legacy_agent` holds. Under its own key it is
    # a second entry for the same film: a real duplicate pair, a title tie for the
    # ordering properties, and the one NFD path in the library.
    Entry(
        "movie_nfd_path",
        "1",
        {
            "ratingKey": "1704",
            "key": "/library/metadata/1704",
            "Media@id": "1101",
            "Part@id": "1102",
        },
    ),
    Entry("show", "2", {"childCount": "2", "leafCount": "3"}),
    Entry("season", "2"),
    Entry(
        "season",
        "2",
        {
            "ratingKey": "8",
            "key": "/library/metadata/8/children",
            "title": "Season 2",
            "index": "2",
            "year": "2003",
        },
    ),
    Entry("episode", "2"),
    Entry(
        "episode",
        "2",
        {
            "ratingKey": "9",
            "key": "/library/metadata/9",
            "title": "The Detail",
            "index": "2",
            "originallyAvailableAt": "2002-06-09",
            "<Guid>": None,
            "Media@id": "1103",
            "Part@id": "1104",
            "Part@file": "/media/TV/The Wire/S01/S01E02.mkv",
        },
    ),
    Entry(
        "episode",
        "2",
        {
            "ratingKey": "10",
            "key": "/library/metadata/10",
            "title": "Ebb Tide",
            "index": "1",
            "parentIndex": "2",
            "parentRatingKey": "8",
            "parentTitle": "Season 2",
            "year": "2003",
            "originallyAvailableAt": "2003-06-01",
            "<Guid>": None,
            "Media@id": "1105",
            "Part@id": "1106",
            "Part@file": "/media/TV/The Wire/S02/S02E01.mkv",
        },
    ),
    Entry("author", "3", {"childCount": "2"}),
    Entry(
        "audiobook",
        "3",
        {
            "ratingKey": "11",
            "key": "/library/metadata/11/children",
            "title": "The Way of Kings",
            "index": "1",
            "year": "2010",
            "leafCount": "1",
            "guid": "com.plexapp.agents.audnexus://B003ZWFO7E?lang=en",
        },
    ),
    Entry("audiobook", "3", {"leafCount": "2"}),
    Entry(
        "audiobook_part",
        "3",
        {
            "ratingKey": "12",
            "key": "/library/metadata/12",
            "parentRatingKey": "11",
            "parentTitle": "The Way of Kings",
            "Media@id": "1107",
            "Part@id": "1108",
            "Part@file": "/media/Audiobooks/Brandon Sanderson/The Way of Kings/Chapter 01.m4b",
        },
    ),
    Entry("audiobook_part", "3"),
    Entry(
        "audiobook_part",
        "3",
        {
            "ratingKey": "13",
            "key": "/library/metadata/13",
            "title": "Chapter 2",
            "index": "2",
            "Media@id": "1109",
            "Part@id": "1110",
            "Part@file": "/media/Audiobooks/Brandon Sanderson/Words of Radiance/Chapter 02.m4b",
        },
    ),
    Entry("music_track", "4"),
)

ROOT = ET.Element(
    "MediaContainer",
    machineIdentifier="fake-machine-identifier",
    version="1.41.0.0000",
    platform="Linux",
    friendlyName="fake",
)

LEAF_TYPES = frozenset({"movie", "episode", "track"})


def _section_order(section: Section) -> tuple[int, int, str]:
    return section_key(
        SectionRef(
            section_id=section.key,
            title=section.title,
            section_type=section.type,
            agent=section.agent,
        )
    )


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
        library: Sequence[Entry] = LIBRARY,
        *,
        omit_section_id: bool = False,
    ) -> None:
        self.queries: list[tuple[str, dict[str, str]]] = []
        self._sections = {section.key: section for section in sections}
        self._omit_section_id = omit_section_id
        # rating key -> (section key, element), and the record PlexLibrary maps each
        # element to, which is what the model functions order and match on.
        configure_plexapi()
        self._items: dict[str, tuple[str, ET.Element]] = {}
        self._records: dict[str, NormalizedItem] = {}
        for entry in library:
            element = entry.element()
            key = element.attrib["ratingKey"]
            if key in self._items:
                raise AssertionError(f"two entries serve rating key {key}; one server cannot")
            self._items[key] = (entry.section, element)
            self._records[key] = normalize_item(
                build(element, StubServer()), entry.section, FetchProfile.CORE
            )
        super().__init__("http://fake.invalid", "fake-token")

    def elements(self) -> list[tuple[str, ET.Element]]:
        """Every served element with its section, for tests that check the library
        itself rather than a provider's view of it."""
        return list(self._items.values())

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
        for section in sorted(self._sections.values(), key=_section_order):
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
        keys = [
            key
            for key, (section_key, element) in self._items.items()
            if section_key == section.key and element.attrib.get("type") == libtype
        ]
        if "title" in query:
            keys = [key for key in keys if title_matches(query["title"], self._records[key])]
        keys.sort(key=lambda key: listing_key(self._records[key]))
        return self._page([self._items[key][1] for key in keys], headers, section.key)

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
        keys = sorted(
            (key for key, (_, e) in self._items.items() if e.get("parentRatingKey") == rating_key),
            key=lambda key: children_key(self._records[key]),
        )
        return self._page([self._items[key][1] for key in keys], headers, section_key)

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

    def pages_since(self, mark: int) -> list[tuple[str, int, int]]:
        """`(path, start, size)` for every paged request after `mark`: what plexapi
        actually put in the container headers on PlexLibrary's behalf."""
        return [
            (
                urlsplit(key).path,
                int(headers["X-Plex-Container-Start"]),
                int(headers["X-Plex-Container-Size"]),
            )
            for key, headers in self.queries[mark:]
            if "X-Plex-Container-Start" in headers
        ]
