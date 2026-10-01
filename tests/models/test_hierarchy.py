"""The hierarchy table, checked against the model it describes.

`CHILD_KIND` is hand-written, and the item classes declare `parent` and
`grandparent` fields by hand too. These tests make the two agree, so a kind added
to one and forgotten in the other fails here instead of in a walk that silently
stops a level early.
"""

from typing import get_args

import pytest

from shelfwarden.models.hierarchy import CHILD_KIND, PARENT_KIND, lineage
from shelfwarden.models.item import MediaKind, NormalizedItem

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
