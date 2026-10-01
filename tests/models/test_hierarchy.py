"""The hierarchy table, checked against the model it describes.

`CHILD_KIND` is hand-written, and the item classes declare `parent` and
`grandparent` fields by hand too. These tests make the two agree, so a kind added
to one and forgotten in the other fails here instead of in a walk that silently
stops a level early.
"""

from typing import get_args

import pytest

from shelfwarden.models.hierarchy import (
    CHILD_KIND,
    DERIVED_COPIES,
    PARENT_KIND,
    Rule,
    Violation,
    derived_violations,
    lineage,
    newly_violated,
    propagate,
)
from shelfwarden.models.ids import ItemId
from shelfwarden.models.item import (
    EpisodeItem,
    FetchProfile,
    MediaKind,
    NormalizedItem,
    SeasonItem,
    ShowItem,
    with_changes,
)

# Read off the discriminated union rather than listed, so a new item class cannot
# be missed by this file.
ITEM_CLASSES = {
    cls.model_fields["media_kind"].default: cls for cls in get_args(get_args(NormalizedItem)[0])
}


def test_every_kind_has_an_item_class():
    assert set(ITEM_CLASSES) == set(MediaKind)


def test_no_two_kinds_share_a_child():
    """`PARENT_KIND` inverts `CHILD_KIND`. Inverting a mapping that is not
    one-to-one drops an entry without a word."""
    assert len(PARENT_KIND) == len(CHILD_KIND)
    assert all(PARENT_KIND[child] is parent for parent, child in CHILD_KIND.items())


@pytest.mark.parametrize("kind", list(MediaKind))
def test_an_item_has_a_parent_field_exactly_when_its_kind_has_a_parent(kind):
    assert ("parent" in ITEM_CLASSES[kind].model_fields) == (kind in PARENT_KIND)


@pytest.mark.parametrize("kind", list(MediaKind))
def test_an_item_has_a_grandparent_field_exactly_when_it_is_two_levels_deep(kind):
    two_deep = kind in PARENT_KIND and PARENT_KIND[kind] in PARENT_KIND
    assert ("grandparent" in ITEM_CLASSES[kind].model_fields) == two_deep


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (MediaKind.MOVIE, (MediaKind.MOVIE,)),
        (MediaKind.SHOW, (MediaKind.SHOW, MediaKind.SEASON, MediaKind.EPISODE)),
        (MediaKind.SEASON, (MediaKind.SEASON, MediaKind.EPISODE)),
        (
            MediaKind.AUTHOR,
            (MediaKind.AUTHOR, MediaKind.AUDIOBOOK, MediaKind.AUDIOBOOK_PART),
        ),
        (MediaKind.AUDIOBOOK_PART, (MediaKind.AUDIOBOOK_PART,)),
    ],
)
def test_lineage_is_a_kind_and_everything_beneath_it_top_down(kind, expected):
    assert lineage(kind) == expected


# -- derived copies ------------------------------------------------------------


@pytest.mark.parametrize("copy", DERIVED_COPIES, ids=lambda copy: copy.field)
def test_every_derived_copy_is_a_field_of_every_kind_it_names(copy):
    for kind in copy.kinds:
        assert copy.field in ITEM_CLASSES[kind].model_fields, (copy.field, kind)


def _show_tree(show_title: str = "Cowboy Bebop", season_parent_title: str = "Cowboy Bebop"):
    def _id(key):
        return ItemId("fake", "2", key)

    show = ShowItem(item_id=_id("1"), fetched=FetchProfile.CORE, title=show_title, child_count=1)
    season = SeasonItem(
        item_id=_id("2"),
        fetched=FetchProfile.CORE,
        title="Season 1",
        parent=show.item_id,
        parent_title=season_parent_title,
        index=1,
    )
    episode = EpisodeItem(
        item_id=_id("3"),
        fetched=FetchProfile.CORE,
        title="Asteroid Blues",
        parent=season.item_id,
        grandparent=show.item_id,
        parent_title="Season 1",
        grandparent_title=show_title,
        index=1,
        parent_index=1,
    )
    return show, season, episode


class TestDerivedViolations:
    def test_a_coherent_tree_has_none(self):
        assert derived_violations(_show_tree()) == ()

    def test_a_stale_copy_is_reported_with_its_field(self):
        show, season, episode = _show_tree()
        renamed = with_changes(show, {"title": "Pilot Only"})
        found = derived_violations((renamed, season, episode))
        assert {(v.subject, v.path) for v in found} == {
            ("fake:2:2", "/parent_title"),
            ("fake:2:3", "/grandparent_title"),
        }
        assert {v.rule for v in found} == {Rule.DERIVED_COPY}

    def test_an_unreported_copy_is_never_a_disagreement(self):
        show, season, episode = _show_tree()
        silent = with_changes(season, {"parent_title": None})
        assert derived_violations((show, silent, episode)) == ()

    def test_a_count_disagrees_when_the_children_do(self):
        show, season, episode = _show_tree()
        assert derived_violations((with_changes(show, {"child_count": 5}), season, episode))


def test_newly_violated_compares_rule_subject_and_field_not_the_values():
    """A count that was wrong and is now differently wrong is inherited, not new."""
    before = [Violation(Rule.DERIVED_COPY, "a", "is 1, but 0", path="/part_count")]
    after = [
        Violation(Rule.DERIVED_COPY, "a", "is 1, but 2", path="/part_count"),
        Violation(Rule.DERIVED_COPY, "b", "is 1, but 0", path="/part_count"),
    ]
    assert [v.subject for v in newly_violated(after, before)] == ["b"]


class TestPropagate:
    def test_a_copy_the_change_made_stale_is_brought_back_in_line(self):
        before = _show_tree()
        after = (with_changes(before[0], {"title": "Pilot Only"}), *before[1:])
        propagated, touched = propagate(before, after)
        assert derived_violations(propagated) == ()
        assert touched == (("fake:2:2", "/parent_title"), ("fake:2:3", "/grandparent_title"))

    def test_a_disagreement_the_source_already_had_is_left_alone(self):
        """A delta must describe the change, not tidy the source."""
        before = _show_tree(season_parent_title="Bebop (old)")
        after = (with_changes(before[0], {"summary": "Bounty hunters."}), *before[1:])
        propagated, touched = propagate(before, after)
        assert touched == ()
        assert propagated[1].parent_title == "Bebop (old)"

    def test_an_added_items_copies_are_computed(self):
        show, season, episode = _show_tree()
        new_id = {"provider": "fake", "section_id": "2", "rating_key": "4"}
        added = with_changes(episode, {"item_id": new_id, "parent_title": "S1"})
        propagated, touched = propagate((show, season, episode), (show, season, episode, added))
        assert touched == (("fake:2:4", "/parent_title"),)
        assert propagated[-1].parent_title == "Season 1"

    def test_one_pass_is_enough(self):
        """Every derivation reads primary fields, so a second pass finds nothing."""
        before = _show_tree()
        after = (with_changes(before[0], {"title": "Pilot Only"}), *before[1:])
        once, _ = propagate(before, after)
        _, again = propagate(before, once)
        assert again == ()
