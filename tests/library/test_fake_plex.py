"""The fake server checked as a library in its own right.

`FakePlexServer` stands in for Plex in the conformance suite, and its export
becomes a snapshot. If its library is not one Plex could serve -- a dangling
parent, a count that contradicts the items beneath it -- every later step
inherits the contradiction. Step 0.7.5's integrity rules would then reject the
snapshot for the fake's fault rather than the code's. So the library is checked
here, at the XML level, against the same relations those rules check.

The tripwires are tested too: a fake that quietly answered an unexpected request
would turn a bug into a passing test.
"""

from collections import Counter

import pytest

from tests.library.fake_plex import FIXTURES, LIBRARY, Entry, FakePlexServer

PARENT_TYPE = {"season": "show", "episode": "season", "album": "artist", "track": "album"}

# The music track's album and artist are not served: nothing walks an unmodelled
# section past audiobook detection, which samples tracks alone.
UNWALKED_SECTIONS = frozenset({"4"})


@pytest.fixture(scope="module")
def served():
    fake = FakePlexServer()
    return {element.attrib["ratingKey"]: (section, element) for section, element in fake.elements()}


def _children(served, rating_key):
    return [e for _, e in served.values() if e.attrib.get("parentRatingKey") == rating_key]


class TestTheLibraryIsOnePlexCouldServe:
    def test_every_parent_is_served_in_the_same_section_and_is_the_right_type(self, served):
        for section, element in served.values():
            parent_key = element.attrib.get("parentRatingKey")
            if parent_key is None or section in UNWALKED_SECTIONS:
                continue
            parent_section, parent = served[parent_key]
            assert parent_section == section
            assert parent.attrib["type"] == PARENT_TYPE[element.attrib["type"]]

    def test_a_grandparent_is_the_parents_parent(self, served):
        for section, element in served.values():
            grandparent = element.attrib.get("grandparentRatingKey")
            if grandparent is None or section in UNWALKED_SECTIONS:
                continue
            _, parent = served[element.attrib["parentRatingKey"]]
            assert grandparent == parent.attrib["parentRatingKey"]

    def test_denormalized_titles_and_indexes_agree_with_what_they_copy(self, served):
        for section, element in served.values():
            if "parentRatingKey" not in element.attrib or section in UNWALKED_SECTIONS:
                continue
            _, parent = served[element.attrib["parentRatingKey"]]
            assert element.attrib.get("parentTitle") == parent.attrib["title"]
            if "parentIndex" in element.attrib:
                assert element.attrib["parentIndex"] == parent.attrib["index"]
            if "grandparentTitle" in element.attrib:
                _, grandparent = served[element.attrib["grandparentRatingKey"]]
                assert element.attrib["grandparentTitle"] == grandparent.attrib["title"]

    def test_counts_agree_with_the_items_beneath_them(self, served):
        """The captured counts describe the captured library; the fake restates
        them for its own."""
        for key, (section, element) in served.items():
            if section in UNWALKED_SECTIONS:
                continue
            children = _children(served, key)
            if "childCount" in element.attrib:
                assert int(element.attrib["childCount"]) == len(children), key
            if "leafCount" in element.attrib:
                leaves = children
                if element.attrib["type"] == "show":
                    leaves = [g for c in children for g in _children(served, c.attrib["ratingKey"])]
                assert int(element.attrib["leafCount"]) == len(leaves), key

    @pytest.mark.parametrize("tag", ["Media", "Part"])
    def test_no_two_items_share_a_media_or_part_id(self, served, tag):
        ids = Counter(node.attrib["id"] for _, e in served.values() for node in e.iter(tag))
        assert [i for i, n in ids.items() if n > 1] == []

    def test_every_captured_fixture_is_served_under_its_captured_key(self):
        """Copies supplement the captures; they never replace one. The single
        exception moves only because its captured key is taken."""
        captured = {path.stem for path in FIXTURES.glob("*.xml")}
        in_place = {entry.fixture for entry in LIBRARY if "ratingKey" not in entry.changes}
        assert captured - in_place == {"movie_nfd_path"}


class TestEntry:
    def test_a_plain_attribute_is_set_on_the_element(self):
        element = Entry("show", "2", {"title": "Treme"}).element()
        assert element.attrib["title"] == "Treme"

    def test_a_tagged_attribute_is_set_on_every_matching_descendant(self):
        element = Entry("episode", "2", {"Part@id": "9001"}).element()
        assert [part.attrib["id"] for part in element.iter("Part")] == ["9001"]

    def test_none_removes_an_attribute(self):
        element = Entry("show", "2", {"titleSort": None}).element()
        assert "titleSort" not in element.attrib

    def test_an_angle_bracketed_tag_removes_those_children(self):
        assert list(load_guids("show", {})) != []
        assert list(load_guids("show", {"<Guid>": None})) == []


def load_guids(fixture, changes):
    return Entry(fixture, "2", changes).element().iter("Guid")


class TestTripwires:
    def test_an_unrouted_request_fails_rather_than_being_guessed(self):
        with pytest.raises(AssertionError, match="does not route"):
            FakePlexServer().query("/hubs/search?query=heat")

    def test_a_negative_paging_header_fails(self):
        with pytest.raises(AssertionError, match="negative paging header"):
            FakePlexServer().query(
                "/library/sections/1/all",
                headers={"X-Plex-Container-Start": "-1", "X-Plex-Container-Size": "10"},
            )

    def test_asking_for_a_leafs_children_fails(self):
        with pytest.raises(AssertionError, match="children of 4"):
            FakePlexServer().query("/library/metadata/4/children")

    def test_one_rating_key_cannot_be_served_twice(self):
        with pytest.raises(AssertionError, match="rating key 1702"):
            FakePlexServer(library=(Entry("movie_legacy_agent", "1"), Entry("movie_nfd_path", "1")))
