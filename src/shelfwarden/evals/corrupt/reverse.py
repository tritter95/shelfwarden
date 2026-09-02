"""Recording a delta, applying it, and undoing it byte-for-byte.

`diff_items` is the only way a corruption records what it did, which is what
enforces the read-back rule in `model.py`: a corruption function returns mutated
items and this module diffs their dumps, so a change can never describe an
intent the model declined to store.

The gate for the whole step lives here. `apply_reverse` on a corrupted family
must reproduce the ground-truth family **byte-for-byte** under the canonical
serializer -- not merely equal objects, because equality would accept `8` where
the truth had `8.0` and the dataset is compared as bytes.

Order is not recorded, because order is not information: the export's
`RECORD_ORDER` is a pure function of the ids, so a family's canonical order is
recoverable by sorting rather than by remembering. That is what lets a REMOVE be
undone without storing where the item used to sit.
"""

from collections.abc import Iterator, Sequence

from shelfwarden.canonical import canonical_json
from shelfwarden.evals import census as census_module
from shelfwarden.evals import export as export_module
from shelfwarden.evals.corrupt.model import (
    ATOMIC_PATHS,
    ChangeKind,
    CorruptionError,
    FieldChange,
    ItemChange,
)
from shelfwarden.models.item import NormalizedItem, dump_item, load_item
from shelfwarden.pointer import JSONValue, set_at


def family_sort_key(item: NormalizedItem) -> tuple[tuple[int, int, str], int, tuple[int, int, str]]:
    """A total order over the items of one family.

    The export orders records by `(section_id, root_key, kind_rank, item_key)`.
    Within a family the root is constant, so dropping it leaves the same order --
    and unlike the export's key this one needs no root, which matters because a
    corruption may *add* a second root (`duplicate_quality`) to the set it was
    handed.
    """
    return (
        census_module.section_sort_key(item.item_id.section_id),
        export_module.KIND_RANK[item.media_kind],
        export_module.item_sort_key(item.item_id),
    )


def render_family(items: Sequence[NormalizedItem]) -> bytes:
    """The family as canonical JSONL, in family order. The unit of comparison."""
    return export_module.render_items(sorted(items, key=family_sort_key))


def _diff_value(pointer: str, before: JSONValue, after: JSONValue) -> Iterator[FieldChange]:
    if canonical_json(before) == canonical_json(after):
        return
    if pointer in ATOMIC_PATHS:
        yield FieldChange(path=pointer, before=before, after=after)
        return
    if isinstance(before, dict) and isinstance(after, dict) and before.keys() == after.keys():
        for key in sorted(before):
            yield from _diff_value(f"{pointer}/{key}", before[key], after[key])
        return
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        # Positional correspondence is meaningful here: step 0.2 records that part
        # order carries meaning and is deliberately not sorted, so `/parts/0/path`
        # names the same file before and after. Lists whose order is *derived* are
        # in ATOMIC_PATHS and never reach this branch.
        for index, (left, right) in enumerate(zip(before, after, strict=True)):
            yield from _diff_value(f"{pointer}/{index}", left, right)
        return
    yield FieldChange(path=pointer, before=before, after=after)


def diff_items(
    before: Sequence[NormalizedItem], after: Sequence[NormalizedItem]
) -> tuple[ItemChange, ...]:
    """The delta between two item sets, read back from their dumps.

    Sorted by `(kind, item_id)` rather than by iteration order, so the same
    mutation records the same bytes whatever order the corruption built it in.
    """
    old = {str(item.item_id): item for item in before}
    new = {str(item.item_id): item for item in after}
    changes: list[ItemChange] = []

    for item_id in sorted(old.keys() - new.keys()):
        changes.append(
            ItemChange(kind=ChangeKind.REMOVE, item_id=item_id, record=dump_item(old[item_id]))
        )
    for item_id in sorted(new.keys() - old.keys()):
        changes.append(
            ItemChange(kind=ChangeKind.ADD, item_id=item_id, record=dump_item(new[item_id]))
        )
    for item_id in sorted(old.keys() & new.keys()):
        left, right = old[item_id], new[item_id]
        if left.media_kind is not right.media_kind:
            raise CorruptionError(
                f"{item_id} changed media kind from {left.media_kind} to {right.media_kind}; "
                "a corruption mutates an item, it does not replace it with another type"
            )
        fields = tuple(_diff_value("", dump_item(left), dump_item(right)))
        if fields:
            changes.append(ItemChange(kind=ChangeKind.MODIFY, item_id=item_id, fields=fields))

    return tuple(changes)


def _apply(
    items: Sequence[NormalizedItem], changes: Sequence[ItemChange], *, forward: bool
) -> tuple[NormalizedItem, ...]:
    current = {str(item.item_id): item for item in items}
    for change in changes:
        kind = change.kind
        if not forward:
            kind = {
                ChangeKind.ADD: ChangeKind.REMOVE,
                ChangeKind.REMOVE: ChangeKind.ADD,
                ChangeKind.MODIFY: ChangeKind.MODIFY,
            }[kind]
        if kind is ChangeKind.REMOVE:
            if change.item_id not in current:
                raise CorruptionError(f"cannot remove {change.item_id}: it is not in the set")
            del current[change.item_id]
            continue
        if kind is ChangeKind.ADD:
            if change.item_id in current:
                raise CorruptionError(f"cannot add {change.item_id}: it is already in the set")
            if change.record is None:  # pragma: no cover -- ItemChange guarantees it
                raise CorruptionError(f"ADD {change.item_id} carries no record")
            current[change.item_id] = load_item(change.record)
            continue
        if change.item_id not in current:
            raise CorruptionError(f"cannot modify {change.item_id}: it is not in the set")
        document = dump_item(current[change.item_id])
        for field in change.fields:
            set_at(document, field.path, field.after if forward else field.before)
        # Re-validated rather than trusted: a corruption that wrote a string into
        # an int field must fail here, not in the truth file. This is the same
        # reason `with_changes` exists rather than `model_copy(update=...)`.
        current[change.item_id] = load_item(document)
    return tuple(sorted(current.values(), key=family_sort_key))


def apply_changes(
    items: Sequence[NormalizedItem], changes: Sequence[ItemChange]
) -> tuple[NormalizedItem, ...]:
    """Apply a delta forward. Used to rebuild a corrupted family from the export."""
    return _apply(items, changes, forward=True)


def apply_reverse(
    items: Sequence[NormalizedItem], changes: Sequence[ItemChange]
) -> tuple[NormalizedItem, ...]:
    """Undo a delta. The property the gate rests on."""
    return _apply(items, changes, forward=False)


def reverses_cleanly(
    ground_truth: Sequence[NormalizedItem],
    corrupted: Sequence[NormalizedItem],
    changes: Sequence[ItemChange],
) -> bool:
    """Does undoing this delta reproduce the ground truth byte-for-byte?"""
    return render_family(apply_reverse(corrupted, changes)) == render_family(ground_truth)


__all__ = [
    "apply_changes",
    "apply_reverse",
    "diff_items",
    "family_sort_key",
    "render_family",
    "reverses_cleanly",
]
