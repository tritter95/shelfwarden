"""The delta, and the two ways a corruption could lie about what it did.

The gate for step 0.5 is here: `apply_reverse` on a corrupted family reproduces
the ground truth **byte-for-byte**. Everything else defends the recording:

* **A change records what was stored, not what was asked.** Validation rewrites
  values -- NFD titles become NFC, guids re-sort, `locked_fields` deduplicates --
  so a change built from the caller's intent describes a mutation that did not
  happen, and its reverse writes bytes the ground truth never had.
* **A no-op is decided on bytes.** `True` and `1` are equal in Python and
  different in JSON, so an equality check would bless a change that then breaks
  the byte-identity gate it exists to protect.
"""

import unicodedata
from datetime import date

import pytest

from shelfwarden.evals.corrupt.model import (
    ChangeKind,
    CorruptionError,
    FieldChange,
    ItemChange,
)
from shelfwarden.evals.corrupt.reverse import (
    apply_changes,
    apply_reverse,
    diff_items,
    render_family,
    reverses_cleanly,
)
from shelfwarden.models.ids import parse_guids
from shelfwarden.models.item import FilePart, dump_item, with_changes

from ..conftest import MOVIES, FakeLibrary, _id, _movie


@pytest.fixture
def family():
    """One movie, standing in for a family of one."""
    return (_movie("101", "Amélie", 2001),)


def _library_family():
    return tuple(FakeLibrary.build().records.values())


class TestDiffRecordsWhatWasStored:
    def test_a_title_change_is_one_field_change(self, family):
        after = (with_changes(family[0], {"title": "Solaris"}),)
        (change,) = diff_items(family, after)
        assert change.kind is ChangeKind.MODIFY
        assert [(f.path, f.before, f.after) for f in change.fields] == [
            ("/title", "Amélie", "Solaris")
        ]

    def test_an_nfd_title_is_recorded_as_the_nfc_the_model_stored(self, family):
        # Verified in step 0.5: `with_changes(item, {"title": NFD})` stores NFC, so
        # `after` taken from the caller's argument would name a value the item does
        # not have -- and reversing it would write NFD back over an NFC ground truth.
        nfd = unicodedata.normalize("NFD", "Amélie 2")
        after = (with_changes(family[0], {"title": nfd}),)
        (change,) = diff_items(family, after)
        (field,) = change.fields
        assert field.after == unicodedata.normalize("NFC", nfd)
        assert field.after != nfd

    def test_guids_are_recorded_whole_because_their_order_is_derived(self, family):
        after = (with_changes(family[0], {"guids": parse_guids(None, ["tvdb://9", "imdb://tt1"])}),)
        (change,) = diff_items(family, after)
        assert [f.path for f in change.fields] == ["/guids"]
        # Stored sorted, not in the order handed in.
        assert [g["namespace"] for g in change.fields[0].after] == ["imdb", "tvdb"]

    def test_locked_fields_are_recorded_whole_and_deduplicated(self, family):
        after = (with_changes(family[0], {"locked_fields": ("year", "title", "year")}),)
        (change,) = diff_items(family, after)
        assert [(f.path, f.after) for f in change.fields] == [("/locked_fields", ["title", "year"])]

    def test_a_part_is_addressed_positionally_because_its_order_carries_meaning(self, family):
        document = dump_item(family[0])
        document["parts"][0]["path"] = "/media/garbage.mkv"
        after = (type(family[0]).model_validate(document),)
        (change,) = diff_items(family, after)
        assert [f.path for f in change.fields] == ["/parts/0/path"]

    def test_a_changed_part_count_is_recorded_whole(self, family):
        extra = FilePart(media_id="9", part_id="2", path="/media/Amélie/cd2.mkv")
        parts = [*dump_item(family[0])["parts"], extra.model_dump(mode="json")]
        after = (with_changes(family[0], {"parts": parts}),)
        (change,) = diff_items(family, after)
        assert [f.path for f in change.fields] == ["/parts"]

    def test_an_unchanged_family_has_no_delta(self, family):
        assert diff_items(family, family) == ()

    def test_changing_media_kind_is_not_a_corruption(self, family):
        show = _movie("101", "Amélie", 2001)
        other = with_changes(show, {})
        renamed = other.model_copy(update={"media_kind": "show"})
        with pytest.raises(CorruptionError, match="changed media kind"):
            diff_items(family, (renamed,))


class TestStructuralChanges:
    def test_an_added_item_carries_its_whole_record(self, family):
        clone = _movie("999", "Amélie", 2001)
        (change,) = diff_items(family, (*family, clone))
        assert change.kind is ChangeKind.ADD
        assert change.record is not None and change.record["title"] == "Amélie"

    def test_a_removed_item_carries_what_to_put_back(self, family):
        clone = _movie("999", "Amélie", 2001)
        (change,) = diff_items((*family, clone), family)
        assert (change.kind, change.item_id) == (ChangeKind.REMOVE, f"fake:{MOVIES}:999")

    def test_reversing_an_add_removes_it_and_the_order_is_recovered(self, family):
        clone = _movie("099", "Amélie", 2001)
        corrupted = (*family, clone)
        delta = diff_items(family, corrupted)
        # `099` sorts before `101`, so a reverse that merely appended would not
        # reproduce the bytes. Order is a function of the ids, not a memory.
        assert render_family(apply_reverse(corrupted, delta)) == render_family(family)

    def test_a_delta_applies_forward_to_the_corrupted_family(self, family):
        clone = _movie("099", "Amélie", 2001)
        corrupted = (*family, clone)
        delta = diff_items(family, corrupted)
        assert render_family(apply_changes(family, delta)) == render_family(corrupted)


class TestTheGate:
    def test_apply_reverse_restores_ground_truth_for_every_media_kind(self):
        # Practices §8.2 names this test. It runs over one item of every kind, with
        # a mutation shaped like the ones the corruptions actually make.
        items = _library_family()
        mutations = {
            "movie": {"title": "Wrong", "year": 1900},
            "show": {"summary": None},
            "season": {"index": 9},
            "episode": {"parent_index": 7},
            "author": {"title": "Sanderson, Brandon"},
            "audiobook": {"series": None, "series_position": None},
            "audiobook_part": {"index": 5},
        }
        seen = set()
        corrupted = []
        for item in items:
            kind = str(item.media_kind)
            if kind in mutations and kind not in seen:
                seen.add(kind)
                corrupted.append(with_changes(item, mutations[kind]))
            else:
                corrupted.append(item)
        assert seen == set(mutations)
        delta = diff_items(items, tuple(corrupted))
        assert reverses_cleanly(items, tuple(corrupted), delta)

    def test_reversal_is_compared_as_bytes_not_as_objects(self, family):
        after = (with_changes(family[0], {"rating": 8.0}),)
        delta = diff_items(family, after)
        restored = apply_reverse(after, delta)
        assert render_family(restored) == render_family(family)


class TestFieldChangeRefusals:
    def test_a_noop_raises_rather_than_warning(self):
        with pytest.raises(CorruptionError, match="no-op change"):
            FieldChange(path="/title", before="Amélie", after="Amélie")

    def test_a_noop_is_decided_on_canonical_bytes_not_python_equality(self):
        # `True == 1` in Python and `b'true' != b'1'` in JSON, so this is a real
        # change and recording it is correct. An equality check would drop it, and
        # the reverse would then leave a `1` where the ground truth had `true`.
        change = FieldChange(path="/has_thumb", before=True, after=1)
        assert change.before is True

    def test_a_value_that_is_not_json_stops_at_the_change_that_recorded_it(self):
        # Every value must come from `dump_item`. A model object smuggled in from
        # somewhere else fails here, naming the path, rather than at write time.
        with pytest.raises(CorruptionError, match="not JSON"):
            FieldChange(path="/originally_available_at", before=date(2001, 4, 25), after=None)

    def test_a_wildcard_is_not_a_change_path(self):
        with pytest.raises(CorruptionError, match="addresses one location"):
            FieldChange(path="/parts/*/path", before="a", after="b")

    def test_a_path_without_a_leading_slash_is_rejected(self):
        with pytest.raises(CorruptionError, match="unusable change path"):
            FieldChange(path="title", before="a", after="b")


class TestItemChangeShape:
    def test_a_modify_with_no_fields_is_not_a_change(self):
        with pytest.raises(CorruptionError, match="carries no field changes"):
            ItemChange(kind=ChangeKind.MODIFY, item_id="fake:1:1")

    def test_a_modify_may_not_change_one_path_twice(self):
        with pytest.raises(CorruptionError, match="more than once"):
            ItemChange(
                kind=ChangeKind.MODIFY,
                item_id="fake:1:1",
                fields=(
                    FieldChange(path="/title", before="a", after="b"),
                    FieldChange(path="/title", before="b", after="c"),
                ),
            )

    def test_an_add_without_a_record_cannot_be_applied(self):
        with pytest.raises(CorruptionError, match="carries no record"):
            ItemChange(kind=ChangeKind.ADD, item_id="fake:1:1")

    def test_applying_a_change_to_a_missing_item_says_so(self, family):
        delta = (
            ItemChange(
                kind=ChangeKind.MODIFY,
                item_id=str(_id(MOVIES, "404")),
                fields=(FieldChange(path="/title", before="a", after="b"),),
            ),
        )
        with pytest.raises(CorruptionError, match="not in the set"):
            apply_changes(family, delta)
