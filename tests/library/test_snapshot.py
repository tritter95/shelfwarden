"""What only a snapshot has: the worlds it refuses, the profile it holds, and the
model of Plex it serves by.

What it shares with every provider -- paging, errors, ordering as a property --
is the conformance suite's (`test_conformance.py`).
"""

import pytest

from shelfwarden.library.base import (
    MUTATING_METHODS,
    LibraryItemNotFound,
    LibraryProvider,
    LibraryUnsupported,
    ProviderInfo,
    Retryability,
    protocol_methods,
)
from shelfwarden.library.snapshot import (
    SHOWN_VIOLATIONS,
    SnapshotLibrary,
    SnapshotRule,
    WorldIntegrityError,
    children_key,
    listing_key,
    section_key,
    title_matches,
)
from shelfwarden.models.hierarchy import Rule, Violation, structural_violations
from shelfwarden.models.ids import ItemId
from shelfwarden.models.item import (
    EpisodeItem,
    FetchProfile,
    FilePart,
    SeasonItem,
    SectionRef,
    ShowItem,
    with_changes,
)
from tests.library.snapshots import INFO, RECORDS, SNAPSHOT_SECTIONS, hand_built, relabel, restamp

CORE = FetchProfile.CORE


def _id(section_id: str, key: str) -> ItemId:
    return ItemId("snapshot", section_id, key)


def _record(key: str):
    (found,) = [record for record in RECORDS if record.item_id.rating_key == key]
    return found


def _without(*keys: str):
    return tuple(record for record in RECORDS if record.item_id.rating_key not in keys)


def _replaced(record):
    return tuple(record if r.item_id == record.item_id else r for r in RECORDS)


# -- a minimal tree, so each structural rule can be broken alone -------------


def _show(key: str = "1") -> ShowItem:
    return ShowItem(item_id=_id("2", key), fetched=CORE, title=f"Show {key}")


def _season(key: str, show: ShowItem, *, section_id: str = "2") -> SeasonItem:
    return SeasonItem(
        item_id=_id(section_id, key), fetched=CORE, title="Season 1", parent=show.item_id, index=1
    )


def _episode(key: str, season: SeasonItem, show: ShowItem, *, part_id: str | None = None):
    return EpisodeItem(
        item_id=_id("2", key),
        fetched=CORE,
        title=f"Episode {key}",
        parent=season.item_id,
        grandparent=show.item_id,
        index=1,
        parts=(FilePart(part_id=part_id, path=f"/tv/{key}.mkv"),) if part_id else (),
    )


# Two show sections, so a season can sit in the wrong one without also breaking a
# library rule; each test below then shows its rule alone, on both paths.
TREE_SECTIONS = (
    SectionRef(section_id="2", title="TV", section_type="show", agent="tv.plex.agents.series"),
    SectionRef(section_id="9", title="More TV", section_type="show", agent="tv.plex.agents.series"),
)


def _refused(records) -> set[str]:
    """The rules construction refuses `records` for."""
    with pytest.raises(WorldIntegrityError) as caught:
        SnapshotLibrary(records, TREE_SECTIONS, INFO, CORE)
    return _rules(caught.value.violations)


def _rules(violations) -> set[str]:
    return {violation.rule for violation in violations}


class TestStructuralRules:
    """Each rule broken alone, so a test shows the rule and not a pile-up -- and
    shows it twice: found by the function, and refused at construction."""

    def test_a_sound_tree_breaks_none(self):
        show = _show()
        season = _season("2", show)
        records = (show, season, _episode("3", season, show))
        assert structural_violations(records) == ()
        assert SnapshotLibrary(records, TREE_SECTIONS, INFO, CORE).sections() == TREE_SECTIONS

    def test_the_hand_built_library_breaks_none(self):
        assert structural_violations(RECORDS) == ()

    def test_an_id_twice(self):
        show = _show()
        records = (show, show)
        assert _rules(structural_violations(records)) == {Rule.DUPLICATE_ID} == _refused(records)

    def test_a_child_kind_with_no_parent(self):
        show = _show()
        season = _season("2", show)
        orphaned = with_changes(_episode("3", season, show), {"parent": None})
        records = (show, season, orphaned)
        assert _rules(structural_violations(records)) == {Rule.MISSING_PARENT} == _refused(records)

    def test_a_parent_not_in_the_set(self):
        show = _show()
        season = _season("2", show)
        records = (show, _episode("3", season, show))
        assert _rules(structural_violations(records)) == {Rule.ORPHAN} == _refused(records)

    def test_a_parent_of_the_wrong_kind(self):
        show = _show()
        misfiled = with_changes(
            _episode("3", _season("2", show), show),
            {
                "parent": _as_json(show.item_id),
                "grandparent": None,
            },
        )
        records = (show, misfiled)
        assert _rules(structural_violations(records)) == {Rule.PARENT_KIND} == _refused(records)

    def test_a_parent_in_another_section(self):
        show = _show()
        season = _season("2", show, section_id="9")
        records = (show, season)
        assert _rules(structural_violations(records)) == {Rule.PARENT_SECTION} == _refused(records)

    def test_a_grandparent_that_is_not_the_parents_parent(self):
        show, other = _show("1"), _show("4")
        season = _season("2", show)
        episode = with_changes(
            _episode("3", season, show), {"grandparent": _as_json(other.item_id)}
        )
        records = (show, other, season, episode)
        assert _rules(structural_violations(records)) == {Rule.GRANDPARENT} == _refused(records)

    def test_a_part_id_on_two_items(self):
        show = _show()
        season = _season("2", show)
        shared = (
            _episode("3", season, show, part_id="77"),
            _episode("4", season, show, part_id="77"),
        )
        records = (show, season, *shared)
        assert _rules(structural_violations(records)) == {Rule.SHARED_PART_ID} == _refused(records)


def _as_json(item_id: ItemId) -> dict:
    return {
        "provider": item_id.provider,
        "section_id": item_id.section_id,
        "rating_key": item_id.rating_key,
    }


class TestConstructionIsRefused:
    """A world no Plex server could serve is refused whole, listing every
    violation -- the tree rules above, and the library rules here."""

    def test_the_hand_built_library_is_accepted(self):
        assert isinstance(hand_built(), LibraryProvider)

    def test_a_broken_tree_is_refused(self):
        with pytest.raises(WorldIntegrityError) as caught:
            hand_built(records=_without("211"))  # season 1 of Cowboy Bebop
        assert _rules(caught.value.violations) == {Rule.ORPHAN}

    def test_a_live_provider_label(self):
        info = ProviderInfo(provider="plex", server_id="x")
        with pytest.raises(WorldIntegrityError) as caught:
            hand_built(records=relabel(RECORDS, "plex"), info=info)
        assert _rules(caught.value.violations) == {SnapshotRule.LIVE_PROVIDER}

    def test_an_id_under_another_label(self):
        stray = relabel([_record("106")], "fake")[0]
        records = tuple(stray if r.item_id.rating_key == "106" else r for r in RECORDS)
        with pytest.raises(WorldIntegrityError) as caught:
            hand_built(records=records)
        assert _rules(caught.value.violations) == {SnapshotRule.PROVIDER_LABEL}

    def test_a_section_listed_twice(self):
        with pytest.raises(WorldIntegrityError) as caught:
            hand_built(sections=(*SNAPSHOT_SECTIONS, SNAPSHOT_SECTIONS[0]))
        assert _rules(caught.value.violations) == {SnapshotRule.DUPLICATE_SECTION}

    def test_an_unmodelled_section(self):
        photos = SectionRef(section_id="5", title="Photos", section_type="photo", agent="none")
        with pytest.raises(WorldIntegrityError) as caught:
            hand_built(sections=(*SNAPSHOT_SECTIONS, photos))
        assert _rules(caught.value.violations) == {SnapshotRule.UNMODELLED_SECTION}

    def test_a_record_whose_section_is_missing(self):
        with pytest.raises(WorldIntegrityError) as caught:
            hand_built(sections=SNAPSHOT_SECTIONS[:2])
        assert _rules(caught.value.violations) == {SnapshotRule.UNKNOWN_SECTION}

    def test_a_kind_its_section_cannot_hold(self):
        movie = _record("106")
        moved = with_changes(movie, {"item_id": _as_json(_id("2", "106"))})
        with pytest.raises(WorldIntegrityError) as caught:
            hand_built(records=(*(r for r in RECORDS if r is not movie), moved))
        assert _rules(caught.value.violations) == {SnapshotRule.SECTION_KIND}

    def test_a_rating_key_in_two_sections(self):
        twin = ShowItem(item_id=_id("2", "101"), fetched=CORE, title="Amélie, the series")
        with pytest.raises(WorldIntegrityError) as caught:
            hand_built(records=(*RECORDS, twin))
        assert _rules(caught.value.violations) == {SnapshotRule.DUPLICATE_RATING_KEY}

    def test_a_record_fetched_at_another_profile(self):
        (stamped,) = restamp([_record("106")], FetchProfile.FULL)
        with pytest.raises(WorldIntegrityError) as caught:
            hand_built(records=_replaced(stamped))
        assert _rules(caught.value.violations) == {SnapshotRule.PROFILE}

    def test_a_stub_profile_cannot_be_held(self):
        with pytest.raises(ValueError, match="what a listing returns"):
            hand_built(profile=FetchProfile.STUB)

    def test_the_message_is_capped_and_says_so(self):
        violations = [
            Violation(Rule.ORPHAN, f"snapshot:2:{n}", "x") for n in range(SHOWN_VIOLATIONS + 2)
        ]
        error = WorldIntegrityError(violations)
        assert "...and 2 more, all on .violations" in str(error)
        assert len(error.violations) == SHOWN_VIOLATIONS + 2


class TestProfiles:
    def test_the_held_profile_is_answered(self):
        assert hand_built().get_item(_id("1", "101")).fetched is CORE

    def test_another_profile_is_refused_not_restamped(self):
        with pytest.raises(LibraryUnsupported, match="fetched at core") as caught:
            hand_built().get_item(_id("1", "101"), FetchProfile.FULL)
        assert caught.value.retryability is Retryability.TERMINAL

    def test_an_unknown_id_is_not_found_whatever_the_profile(self):
        with pytest.raises(LibraryItemNotFound):
            hand_built().get_item(_id("1", "99999"), FetchProfile.FULL)

    def test_a_full_snapshot_answers_full_and_refuses_core(self):
        snapshot = hand_built(
            records=restamp(RECORDS, FetchProfile.FULL), profile=FetchProfile.FULL
        )
        assert snapshot.get_item(_id("1", "101"), FetchProfile.FULL).fetched is FetchProfile.FULL
        with pytest.raises(LibraryUnsupported):
            snapshot.get_item(_id("1", "101"))


class TestSurface:
    def test_its_public_methods_are_exactly_the_protocols(self):
        public = {name for name in dir(SnapshotLibrary) if not name.startswith("_")}
        assert public == protocol_methods(LibraryProvider)
        assert public & MUTATING_METHODS == frozenset()

    def test_it_names_itself_by_the_info_it_was_given(self):
        assert hand_built().provider_info() == INFO

    def test_sections_come_back_in_section_order(self):
        assert [s.section_id for s in hand_built(sections=SNAPSHOT_SECTIONS[::-1]).sections()] == [
            "1",
            "2",
            "3",
        ]

    def test_a_wrong_section_is_answered_with_the_right_id(self):
        with pytest.raises(LibraryItemNotFound) as caught:
            hand_built().get_item(_id("2", "101"))
        assert caught.value.next_action == "use snapshot:1:101"

    def test_records_are_served_as_held(self):
        assert hand_built().get_item(_id("1", "101")) is _record("101")


class TestTheModelOfPlex:
    """The four functions that stand in for server behavior. Each test pins the
    model as it stands; the live suite is what decides whether Plex agrees."""

    def test_a_listing_sorts_by_sort_title_not_title(self):
        shawshank = with_changes(_record("104"), {"title_sort": "Shawshank Redemption"})
        solaris = _record("102")
        assert listing_key(shawshank) < listing_key(solaris)

    def test_a_listing_ignores_case(self):
        lower = with_changes(_record("106"), {"title": "a film", "title_sort": None})
        upper = with_changes(_record("105"), {"title": "B Film", "title_sort": None})
        assert listing_key(lower) < listing_key(upper)

    def test_a_title_tie_breaks_by_rating_key_numerically(self):
        first = with_changes(_record("102"), {"item_id": _as_json(_id("1", "9"))})
        second = with_changes(_record("103"), {"item_id": _as_json(_id("1", "10"))})
        assert listing_key(first) < listing_key(second)

    def test_children_sort_by_index_with_unnumbered_last(self):
        show = _show()
        numbered = [_season(str(k), show) for k in (5, 6)]
        second = with_changes(numbered[0], {"index": 2})
        unnumbered = with_changes(numbered[1], {"index": None})
        first = _season("7", show)
        assert sorted([unnumbered, second, first], key=children_key) == [first, second, unnumbered]

    def test_sections_sort_numerically(self):
        ids = ["10", "2", "1"]
        refs = [SectionRef(section_id=i, title=i, section_type="movie", agent="a") for i in ids]
        assert [s.section_id for s in sorted(refs, key=section_key)] == ["1", "2", "10"]

    @pytest.mark.parametrize("query", ["Amélie", "amélie", "AMÉLIE", "mél", "Amélie"])
    def test_a_title_search_is_a_case_and_form_insensitive_substring(self, query):
        assert title_matches(query, _record("101"))

    def test_a_title_search_does_not_fold_accents_yet(self):
        """The narrower claim. If Plex finds Amélie for "Amelie", the live probe
        reports it and this test is the one that changes."""
        assert not title_matches("Amelie", _record("101"))
