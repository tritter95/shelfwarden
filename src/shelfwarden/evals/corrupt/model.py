"""What a corruption did, recorded precisely enough to undo.

`implementation-plan.md` §3 gives `CorruptionResult` a single mutated `item` and
a list of `FieldChange`. Five of the fifteen problem classes cannot be expressed
that way: `duplicate_quality` **adds** an item, `author_name_variant` and
`multi_file_split` add items *and* re-parent existing ones,
`absolute_vs_seasonal` renumbers every episode of a show and empties a season,
`anthology_omnibus` splits one item into several. "A new item appeared" is not a
field change on an old one.

So the unit is a **family** -- a root and everything beneath it, which is already
step 0.4's unit -- and the record is an **item-set delta**. Two rules make that
delta trustworthy, and both are verified rather than assumed:

1. **`before` and `after` are read back from the dumped item, never taken from
   the caller's intent.** Validation is not the identity function: a title
   written as NFD is stored as NFC, guids are re-sorted, `locked_fields` is
   deduplicated and sorted. A change recording what it *meant* describes a
   mutation that did not happen, and its reverse writes bytes the ground truth
   never had. `reverse.diff_items` is the only constructor a corruption uses.
2. **A no-op is decided on canonical bytes, not Python equality.**
   `canonical_json(True)` is `b'true'` and `canonical_json(1)` is `b'1'` while
   `True == 1`, so a change between them would pass an equality check and then
   fail the byte-identity gate it was supposed to protect.
"""

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator

from shelfwarden.canonical import canonical_json
from shelfwarden.models.finding import ProblemClass
from shelfwarden.models.item import MediaKind
from shelfwarden.pointer import JSONValue, PointerError, has_wildcard, parse

# Pointers whose value is always recorded whole rather than decomposed. Two
# different reasons, and both matter:
#
# * **Derived order.** `guids` is re-sorted by `sort_external_ids` on every
#   validation and `locked_fields` is sorted and deduplicated, so `/guids/0/value`
#   names a slot the next validation may fill with something else.
# * **Composite identity.** `item_id`, `parent`, and `grandparent` are `ItemId`s:
#   an address, not a record of independently meaningful fields. Re-parenting an
#   episode is one change, and recording it as `/parent/rating_key` would let a
#   reverse write half an address -- an id whose section no longer matches its key.
#
# `parts` is deliberately absent -- step 0.2 records that part order carries
# meaning (disc order, and the split `multi_file_split` operates on), so
# `/parts/0/path` is a stable address and is exactly what `filename_unmatchable`
# must name.
ATOMIC_PATHS: frozenset[str] = frozenset(
    {"/guids", "/locked_fields", "/item_id", "/parent", "/grandparent"}
)


class CorruptionError(Exception):
    """A corruption function produced something that must not ship."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChangeKind(StrEnum):
    ADD = "add"
    REMOVE = "remove"
    MODIFY = "modify"


class FieldChange(_Frozen):
    """One field of one item, before and after.

    `path` is an RFC 6901 pointer (`/title`, `/parts/0/path`) and may not contain
    a wildcard: a change addresses exactly one location, because its reverse has
    exactly one value to write back. A constraint may address many -- that is
    what `must_not_change` selectors are for.
    """

    path: str
    before: JSONValue = None
    after: JSONValue = None

    @model_validator(mode="after")
    def _addressable_and_not_a_noop(self) -> Self:
        try:
            parse(self.path)
        except PointerError as exc:
            raise CorruptionError(f"unusable change path: {exc}") from exc
        if has_wildcard(self.path):
            raise CorruptionError(
                f"{self.path!r} contains a wildcard; a change addresses one location "
                "because its reverse writes one value"
            )
        # Also the serializability gate. Every value here must have come from
        # `dump_item`, and `canonical_json` is where a `date` or a `set` that came
        # from somewhere else stops -- at the change that recorded it, naming the
        # path, rather than at write time with a stack trace and no subject.
        try:
            unchanged = canonical_json(self.before) == canonical_json(self.after)
        except (TypeError, ValueError) as exc:
            raise CorruptionError(
                f"change at {self.path!r} holds a value that is not JSON ({exc}); "
                "record `before` and `after` from `dump_item`, not from the model"
            ) from exc
        if unchanged:
            raise CorruptionError(
                f"no-op change at {self.path!r}: before and after serialize identically. "
                "A corruption that changed nothing is a bug in the corruption, not a "
                "property of the item -- it would ship as an unsolvable case."
            )
        return self


class ItemChange(_Frozen):
    """One item added, removed, or modified.

    `record` carries the whole item for an ADD (what to create) and for a REMOVE
    (what to put back). A MODIFY carries field changes instead: recording the
    whole item there would make every reverse a wholesale overwrite and hide
    which field the repair is actually about.
    """

    kind: ChangeKind
    item_id: str
    fields: tuple[FieldChange, ...] = ()
    record: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> Self:
        if self.kind is ChangeKind.MODIFY:
            if not self.fields:
                raise CorruptionError(f"MODIFY {self.item_id} carries no field changes")
            if self.record is not None:
                raise CorruptionError(f"MODIFY {self.item_id} must not carry a whole record")
            paths = [change.path for change in self.fields]
            if len(paths) != len(set(paths)):
                duplicates = sorted({path for path in paths if paths.count(path) > 1})
                raise CorruptionError(
                    f"MODIFY {self.item_id} changes {duplicates} more than once; the "
                    "reverse would depend on which one it applied last"
                )
        else:
            if self.record is None:
                raise CorruptionError(f"{self.kind} {self.item_id} carries no record")
            if self.fields:
                raise CorruptionError(f"{self.kind} {self.item_id} must not carry field changes")
        return self


class Rejection(_Frozen):
    """Why a case was not emitted.

    Rejections are the step's other output, not its failure log: "TMDB has no
    alternate title for 40% of this library" is a coverage fact that must reach
    the deficit report rather than a mysteriously low score three steps later.

    `applicable` separates the two populations. `False` means the family was
    never a candidate -- a supply fact about the library. `True` means a
    corruption was attempted and failed an acceptance check -- a fact about this
    machinery. Collapsing them turns "your library has no remake pairs" into
    "the generator is broken", and only one of those is actionable.
    """

    problem_class: ProblemClass
    media_kind: MediaKind
    root_id: str
    reason: str
    detail: str | None = None
    applicable: bool = True


__all__ = [
    "ATOMIC_PATHS",
    "ChangeKind",
    "CorruptionError",
    "FieldChange",
    "ItemChange",
    "Rejection",
]
