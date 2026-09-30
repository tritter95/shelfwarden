"""The truth file: what a case expects, and where every expectation came from.

The contract between the generator (0.6) and the scorer (0.8) is that `truth.json`
is **readable without the generator**. Nothing here is inferred at scoring time and
nothing is asserted by hand: an expectation is derived from a 0.5 delta and its
witness, by rules that are tables in this module.

Three expectation kinds, per spec §3, and each closes a defect recorded in
`implementation-plan.md` §3:

* `repair` -- the synthetic and real slices. Carries `required_findings`, and
  `unexpected: fail` as a **default on the model** rather than a value the
  generator writes per case. A default in code cannot be forgotten for one slice,
  which is how 85% of the dataset came to be blind to fabricated findings.
* `no_action` -- should-not-touch. Narrowed to what the screen actually verified,
  and partitioned three ways (guarded / trivially guarded / unguarded) so that
  "an agent claiming absolute numbering on a movie is a false positive" is a
  statement with a reason behind it rather than an open question.
* `escalate` -- the ambiguous slice. Threshold-free and demanding positive
  behavior: silence is not escalation.

**Two outcome shapes, and they compose.** Verified in step 0.6, Finding 3, by
measuring the deltas 0.5 actually produces against each class's witness kind:

* every MODIFY on an item that exists in the ground truth yields a
  `postcondition` -- ten of the eleven implementable classes, including two of the
  three whose witness is a relation;
* a `kind=relation` witness additionally yields a `resolution` -- three classes;
* an ADD yields neither (it names the item the repair is supposed to make
  disappear), and a REMOVE is a soft `absent -> present` report.

Splitting on the change kind instead of the witness kind is wrong in both
directions, and both were measured. `absolute_vs_seasonal` has a REMOVE and no
relation at all -- its emptied season is a container Plex re-creates, while the
renumbered `/index` and `/parent_index` are the gate. And `multi_file_split`
renames the *pre-existing* book to `The Way of Kings CD1`, so scored on a
resolution alone an agent that merges the entries and leaves that title passes.

**A note on invariant 5.** A `resolution` is scored on the finding's own id set
and keeper rather than on a simulated end state. That is not the model's word for
whether it succeeded -- the finding is recorded state and the scorer decides
whether it is right. What the invariant forbids is asking the model how it did.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shelfwarden.canonical import canonical_json
from shelfwarden.evals import screen as screen_module
from shelfwarden.evals.corrupt.context import rank_key
from shelfwarden.evals.corrupt.model import ChangeKind, ItemChange
from shelfwarden.evals.corrupt.witness import MIN_AMBIGUITY_CANDIDATES, DetectabilityWitness
from shelfwarden.models.finding import ProblemClass, describes
from shelfwarden.models.item import MediaKind, NormalizedItem
from shelfwarden.pointer import JSONValue, has_wildcard, matches

SCHEMA_VERSION = 1

CASE_PREFIX = "case-"
RUN_GROUP_PREFIX = "grp-"
DIGEST_CHARS = 12


class TruthError(Exception):
    """A case could not be assembled. Every message names the path or class at fault."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# -- vocabulary -----------------------------------------------------------


class Slice(StrEnum):
    """The four slices of spec §3. `should_not_touch` carries no problem class."""

    SYNTHETIC = "synthetic"
    REAL = "real"
    SHOULD_NOT_TOUCH = "should_not_touch"
    AMBIGUOUS = "ambiguous"


class ExpectationKind(StrEnum):
    REPAIR = "repair"
    NO_ACTION = "no_action"
    ESCALATE = "escalate"


class Unexpected(StrEnum):
    """What a finding nobody asked for costs.

    `FAIL` is the default on every expectation model, for every slice. Invariant 8.
    """

    FAIL = "fail"
    WARN = "warn"
    IGNORE = "ignore"


class Op(StrEnum):
    """The postcondition vocabulary. Five predicates, all literal comparisons.

    Deliberately a field named `op` holding an enum, rather than the single-key
    mapping (`{"normalized_equals": "..."}`) the implementation plan illustrates. A
    single-key mapping cannot be validated as a closed set without a custom
    validator, so a typo'd key parses as *a different predicate name* and the
    scorer silently evaluates nothing -- which is the failure `ProblemClass`
    became an enum to prevent, one layer up.
    """

    EQUALS = "equals"
    NORMALIZED_EQUALS = "normalized_equals"
    CONTAINS = "contains"
    ABSENT = "absent"
    NON_EMPTY = "non_empty"


class RepairOp(StrEnum):
    """Operation names, for the advisory `repair_op` hint only.

    An enum because these strings are compared against what an agent proposes, and
    a typo here would read as "the agent used an unexpected operation" forever.
    """

    REMATCH = "rematch"
    SET_FIELD = "set_field"
    CLEAR_FIELD = "clear_field"
    MERGE_ITEMS = "merge_items"
    SPLIT_ITEM = "split_item"
    MOVE_ITEM = "move_item"
    RENUMBER = "renumber"
    RENAME_FILE = "rename_file"
    SET_SERIES = "set_series"
    SET_SERIES_POSITION = "set_series_position"


# Which operations a steward might plausibly use, per class. **Advisory, never a
# gate** -- component scoring reads it and the outcome does not (spec §3: different
# repair paths can produce identical correct results). Declared for all fifteen
# classes, including the four with no corruption function, for the reason
# `composition.toml` declares all fifteen: the table is a statement of intent and
# should not churn when step 1.1 lands.
REPAIR_OPS: dict[ProblemClass, tuple[RepairOp, ...]] = {
    ProblemClass.WRONG_MATCH: (RepairOp.REMATCH, RepairOp.SET_FIELD),
    ProblemClass.YEAR_COLLISION_REMAKE: (RepairOp.REMATCH, RepairOp.SET_FIELD),
    ProblemClass.FOREIGN_TITLE_VARIANT: (RepairOp.REMATCH, RepairOp.SET_FIELD),
    ProblemClass.ALTERNATE_CUT: (RepairOp.SET_FIELD,),
    ProblemClass.MISSING_METADATA: (RepairOp.SET_FIELD, RepairOp.REMATCH),
    ProblemClass.DUPLICATE_QUALITY: (RepairOp.MERGE_ITEMS,),
    ProblemClass.EPISODE_WRONG_SEASON: (RepairOp.MOVE_ITEM, RepairOp.SET_FIELD),
    ProblemClass.ABSOLUTE_VS_SEASONAL: (RepairOp.RENUMBER, RepairOp.SET_FIELD),
    ProblemClass.FILENAME_UNMATCHABLE: (RepairOp.REMATCH, RepairOp.RENAME_FILE),
    ProblemClass.SERIES_ORDER_BROKEN: (RepairOp.SET_SERIES_POSITION, RepairOp.SET_FIELD),
    ProblemClass.AUTHOR_NAME_VARIANT: (RepairOp.MERGE_ITEMS,),
    ProblemClass.NARRATOR_AS_AUTHOR: (RepairOp.SET_FIELD,),
    ProblemClass.MULTI_FILE_SPLIT: (RepairOp.MERGE_ITEMS,),
    ProblemClass.MISSING_SERIES: (RepairOp.SET_SERIES, RepairOp.SET_SERIES_POSITION),
    ProblemClass.ANTHOLOGY_OMNIBUS: (RepairOp.SPLIT_ITEM, RepairOp.SET_SERIES_POSITION),
}


# -- the hard/soft field tiers --------------------------------------------

# Which fields a repair sets **directly**, through a plexapi edit. Gated.
HARD_FIELDS: tuple[str, ...] = (
    "/title",
    "/title_sort",
    "/year",
    "/guids",
    "/index",
    "/parent_index",
    "/series",
    "/series_position",
    "/edition_title",
    "/content_rating",
    "/studio",
)

# Which fields Plex's own agent or the filesystem derives after a rescan. Reported,
# never gated.
#
# `/parent` is soft while `/parent_index` is hard, which looks inconsistent and is
# not: an agent repairs a misfiled episode by setting its season *number*, and Plex
# re-derives the parent link on the next scan. Gating the derived value would fail
# a correct repair for a reason the agent does not control.
#
# `/parts/*/path` is soft on every class, `filename_unmatchable` included -- whose
# truth record holds a *suggested* filename per the implementation plan's own word.
# A rename is a Phase 3 operation; gating it here would score a correct diagnosis
# as a failure.
SOFT_FIELDS: tuple[str, ...] = (
    "/summary",
    "/parent",
    "/parent_title",
    "/child_count",
    "/leaf_count",
    "/album_count",
    "/part_count",
    "/parts/*/path",
    "/has_thumb",
    "/has_art",
)

# Neither table is exhaustive over the model, on purpose. Between them they cover
# every path the eleven corruptions touch -- measured: sixteen distinct paths -- and
# nothing else. Widening them speculatively (`/rating`, `/locked_fields`) would put
# a tier on a field with no case behind it, and the first corruption to touch one
# would inherit a gate chosen by guesswork instead of stopping in `field_tier`.


class Tier(StrEnum):
    HARD = "hard"
    SOFT = "soft"


def field_tier(path: str) -> Tier:
    """Which tier a concrete change path falls in. Raises rather than defaulting.

    **Matched as a pattern, not looked up as a key.** A `FieldChange.path` is
    always concrete -- `/parts/0/path` -- and `corrupt.model` forbids a wildcard in
    one outright; the tables carry `/parts/*/path` because a tier is a statement
    about a field, not about a slot. `pointer.select` cannot answer this: it needs
    a document, and there is none in hand here.

    A path in **neither** table raises. Every path the eleven corruptions touch is
    covered today (measured: sixteen distinct paths), and a twelfth class that
    touches something new should stop here rather than silently acquire a gate
    nobody chose.
    """
    if has_wildcard(path):
        raise TruthError(
            f"{path!r} contains a wildcard. A tier is asked of a change path, which "
            "addresses one location; the tables hold the selectors."
        )
    for selector in HARD_FIELDS:
        if matches(selector, path):
            return Tier.HARD
    for selector in SOFT_FIELDS:
        if matches(selector, path):
            return Tier.SOFT
    raise TruthError(
        f"{path!r} is in neither HARD_FIELDS nor SOFT_FIELDS, so no tier is declared "
        "for it. Add it to one in truth.py with the reason -- defaulting to hard "
        "would gate a field nobody chose to gate."
    )


# Fields a repair may never destroy, whatever else it does. Invariant 11: never
# delete a file. With `/parts/*/path` soft in the tier table, this is the one place
# a file path is gated at all -- not gating the *value* while gating its
# *destruction* is exactly the intent.
#
# The plan for this step declared the floor per class, so that
# `filename_unmatchable` -- whose repair legitimately renames -- could be exempted
# by hand. It is one rule instead: a selector that matches a path **the delta
# itself touched** is dropped from `must_not_change`. That derives the exemption
# rather than naming it, and a twelfth renaming class cannot silently acquire a
# constraint forbidding its own repair.
MUST_NOT_CHANGE_FLOOR: tuple[str, ...] = ("/parts/*/path",)


# -- records --------------------------------------------------------------


class Expect(_Frozen):
    """One predicate against one location.

    `value` is absent for `absent` and `non_empty`, which compare nothing.

    `excludes` qualifies `contains` alone: ids that must be **absent**. `contains`
    asks only that the right ids be present, and nothing in it says the wrong one
    must be gone. Found in step 0.6. On an item whose ground truth has no external
    ids, `contains []` held in every world, the corrupted one included. And on any
    wrong-match case, a steward that added the right id beside the donor's passed
    while still carrying the match Plex re-derives metadata from on refresh. It is
    a qualifier rather than a sixth predicate so that a location still carries
    exactly one predicate, and the scorer reads one thing per pointer.
    """

    op: Op
    value: JSONValue = None
    excludes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _excludes_qualifies_contains(self) -> "Expect":
        if not self.excludes:
            return self
        if self.op is not Op.CONTAINS:
            raise ValueError(
                f"`excludes` qualifies only `contains`, and this predicate is `{self.op}`: "
                "there is no set of ids here to exclude from."
            )
        both = sorted(set(self.excludes) & set(self.value or ()))
        if both:
            raise ValueError(
                f"{both} are both required and excluded. No world satisfies that, so it "
                "could only ever score a correct repair as wrong."
            )
        return self


# item_id -> pointer -> predicate. Two levels, deliberately: the implementation
# plan illustrates a flat `{pointer: predicate}` map, which cannot express a delta
# that modifies more than one item -- and three of the eleven classes do
# (`absolute_vs_seasonal`, `author_name_variant`, `multi_file_split`, measured).
type Postcondition = dict[str, dict[str, Expect]]


class Resolution(_Frozen):
    """A set relation the repair must identify: these ids are one thing.

    `item_ids` is compared as a **set** and by **equality**, not overlap: merging
    two of three author variants leaves the library broken, so partial credit here
    would report a broken library as a partial success and cost the CI gate its
    boolean.

    `keeper` is `None` where the ground truth does not settle which entry should
    survive -- see `KEEPER_UNSETTLED`.
    """

    relation: str
    item_ids: tuple[str, ...]
    keeper: str | None = None


class RepairOpHint(_Frozen):
    any_of: tuple[RepairOp, ...]


class RequiredFinding(_Frozen):
    """What the agent must produce for this case to pass.

    `item_ids` are the items the finding must **name**. They are not the same set
    as the postcondition's keys: repairing a split book re-parents its part files,
    so the end state constrains an `audiobook_part` that no finding of
    `multi_file_split` should ever be *about*.
    """

    problem_class: ProblemClass
    item_ids: tuple[str, ...]
    resolution: Resolution | None = None
    postcondition: Postcondition = {}
    soft_postcondition: Postcondition = {}
    must_not_change: tuple[str, ...] = ()
    repair_op: RepairOpHint | None = None


class VerificationCheck(_Frozen):
    predicate: screen_module.Predicate
    evidence_id: str
    result: screen_module.CheckStatus


class Verification(_Frozen):
    """What was mechanically verified about a should-not-touch item.

    A citation rather than an assertion: `evidence_id` names the export record the
    predicate read (`implementation-plan.md` §6 -- a library read is evidence too).
    """

    method: str = "mechanical"
    checks: tuple[VerificationCheck, ...] = ()


class RepairExpectation(_Frozen):
    kind: Literal[ExpectationKind.REPAIR] = ExpectationKind.REPAIR
    required_findings: tuple[RequiredFinding, ...]
    unexpected: Unexpected = Unexpected.FAIL
    known_other_problems: tuple[ProblemClass, ...] = ()


class NoActionExpectation(_Frozen):
    """Should-not-touch, narrowed to what was verified.

    Every class is in exactly one of three buckets. A finding in `guarded_classes`
    or `trivially_guarded_classes` is a false positive; a finding in
    `unguarded_classes` is `unverified` -- counted and reported, never pass or fail.
    Without the middle bucket the project starts recording "we could not verify
    that this movie has no absolute-numbering problem".
    """

    kind: Literal[ExpectationKind.NO_ACTION] = ExpectationKind.NO_ACTION
    guarded_classes: tuple[ProblemClass, ...]
    trivially_guarded_classes: tuple[ProblemClass, ...]
    unguarded_classes: tuple[ProblemClass, ...]
    verification: Verification
    unexpected: Unexpected = Unexpected.FAIL


class EscalateExpectation(_Frozen):
    """Ambiguous, and threshold-free.

    `require_finding` is the fix for a pass criterion an agent satisfied by finding
    nothing at all. `min_candidates` reads `witness.MIN_AMBIGUITY_CANDIDATES`
    rather than repeating the literal: 0.5 declared that constant *because* 0.6
    puts the same floor on an escalate case, and two 2s that must agree are one
    constant and a copy of it.
    """

    kind: Literal[ExpectationKind.ESCALATE] = ExpectationKind.ESCALATE
    # `Literal[True]` rather than `bool`: invariant 10 is not a per-case setting. A
    # curated case is hand-written TOML, and `require_finding = false` would let
    # an agent that found nothing score as having escalated -- Defect 2, reopened
    # one file at a time. Refused at parse, not noticed at scoring.
    require_finding: Literal[True] = True
    require_needs_human: Literal[True] = True
    # An ambiguity has at least two candidates; one is a guess wearing a flag.
    min_candidates: int = Field(default=MIN_AMBIGUITY_CANDIDATES, ge=MIN_AMBIGUITY_CANDIDATES)
    acceptable_resolutions: tuple[Resolution, ...] = ()
    forbidden_findings: tuple[RepairOpHint, ...] = ()
    unexpected: Unexpected = Unexpected.FAIL


Expectation = Annotated[
    RepairExpectation | NoActionExpectation | EscalateExpectation,
    Field(discriminator="kind"),
]


class SubjectKeyRecord(_Frozen):
    """The subject ladder's answer, recorded as its two parts.

    `case_id` hashes the `f"{kind}:{value}"` string rather than this record, so the
    id does not move if `context.SubjectKey` ever gains a field.
    """

    kind: str
    value: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


class Provenance(_Frozen):
    """Where the case came from. Affects reporting only -- never the scorer.

    No `label_confidence`, per `implementation-plan.md` §3: a float the scorer
    weights by has exactly the gaming property that sank the confidence threshold.
    Uncertainty is expressed by slice reassignment instead.
    """

    method: str
    labeled_by: str | None = None
    screen_predicate: screen_module.Predicate | None = None
    evidence_ids: tuple[str, ...] = ()
    second_labeler: str | None = None
    agreement: bool | None = None
    notes: str | None = None


class Case(_Frozen):
    """One case: one atomic repair, one expectation, one identity that survives a
    re-export."""

    case_id: str
    slice: Slice
    run_group: str
    # `None` on a should-not-touch case: such a case is about an item, not about a
    # class, and there is no corruption and therefore no variant. Both still enter
    # the `case_id` digest as JSON `null`, so one hash covers every slice.
    problem_class: ProblemClass | None
    media_kind: MediaKind
    subject_key: SubjectKeyRecord
    corruption_variant: str | None
    corruption_fingerprint: str | None
    item_ids: tuple[str, ...]
    expectation: Expectation
    witness: DetectabilityWitness | None
    # The clean **family**, not one item: 0.5's unit is a family and four classes
    # change more than one record. The delta lives in `deltas.jsonl` keyed by
    # `case_id`, so 0.7 can stream one case's world without parsing every case's
    # ground truth.
    ground_truth: tuple[NormalizedItem, ...]
    provenance: Provenance
    collateral: tuple[str, ...] = ()


class SourceExport(_Frozen):
    export_id: str
    items_sha256: str
    roots_sha256: str | None = None


class ScreenRef(_Frozen):
    """Which screen the guard claims in this file came from."""

    items_sha256: str
    schema_version: int
    authority: str
    min_applicable_checks: int


class TruthFile(_Frozen):
    """Cases sorted by `case_id`, so the bytes do not depend on selection order."""

    schema_version: int = SCHEMA_VERSION
    dataset_id: str
    lineage_id: str
    seed: int
    generator_version: str
    source_export: SourceExport
    screen: ScreenRef
    cases: tuple[Case, ...]


def render_truth(truth: TruthFile) -> bytes:
    """Canonical JSON, **with** nulls.

    `screen.json` and `corruptions.json` drop null fields as a size decision, safe
    there because no reader distinguishes absent from null. A reader distinguishes
    them here: `resolution.keeper: null` is a *statement* -- the ground truth does
    not settle which duplicate should survive -- and dropping the key would turn
    that statement into a missing field.
    """
    return canonical_json(truth.model_dump(mode="json"))


def load_truth(payload: bytes) -> TruthFile:
    return TruthFile.model_validate_json(payload)


# -- identity -------------------------------------------------------------


def _digest(payload: object) -> str:
    return sha256(canonical_json(payload)).hexdigest()


def case_id(
    *,
    slice_: Slice,
    problem_class: ProblemClass | None,
    media_kind: MediaKind,
    subject_key: str,
    corruption_variant: str | None,
) -> str:
    """`case-<sha256(...)[:12]>` over the five fields, as one dict.

    `generator_version` is deliberately **out**: including it would nuke the CI
    baseline on every version bump. `corruption_fingerprint` carries that signal
    separately, and feeds the diff's `changed` bucket.

    `problem_class` and `corruption_variant` stay in the digest as `null` on a
    should-not-touch case rather than being dropped from it, so one hash covers
    every slice and a should-not-touch case can never collide with a synthetic case
    on the same subject.
    """
    return (
        CASE_PREFIX
        + _digest(
            {
                "slice": str(slice_),
                "problem_class": None if problem_class is None else str(problem_class),
                "media_kind": str(media_kind),
                "subject_key": subject_key,
                "corruption_variant": corruption_variant,
            }
        )[:DIGEST_CHARS]
    )


def run_group(seed: int, subject_key: str) -> str:
    """`grp-<sha256(seed | subject_key)[:12]>` -- everything one run must see together.

    By Decision 1 of this step's plan that is exactly one family. Keyed on the
    subject rather than on the family's root id because a rating key moves on
    rescan, and a file whose whole purpose is surviving a re-export should not carry
    one where a semantic key is free. It reuses `context.rank_key`, which hashes the
    same payload, rather than re-deriving it here -- two hashes that must agree are
    one function and a copy of it.
    """
    return RUN_GROUP_PREFIX + rank_key(seed, subject_key)[:DIGEST_CHARS]


def corruption_fingerprint(changes: Sequence[ItemChange], generator_version: str) -> str:
    """`sha256(delta | generator_version)`.

    Separate from `case_id` on purpose: this moves when the corruption moves, which
    is what the CI diff's `changed` bucket is for, and `case_id` does not.
    """
    return "sha256:" + _digest(
        {
            "changes": [change.model_dump(mode="json") for change in changes],
            "generator_version": generator_version,
        }
    )


# -- deriving an expectation from a delta ---------------------------------

# Classes whose keeper the ground truth does not settle. `duplicate_quality`'s
# `resolution` variant mints the clone at 2160p against a 1080 original, so "keep
# the entry that already existed" would score a steward that keeps the better copy
# as wrong. Nothing in a recorded pre-corruption state says which entry of a real
# duplicate pair should survive -- that is a keep *policy*, and Phase 3's repair
# stage owns it.
KEEPER_UNSETTLED: frozenset[ProblemClass] = frozenset({ProblemClass.DUPLICATE_QUALITY})

# The pointer whose value is a set of external ids. `contains` rather than
# equality: a repair that leaves an extra `plex://` id in place is not a defect.
# Leaving the *donor's* id in place is, and `Expect.excludes` is what says so.
GUIDS_POINTER = "/guids"


def external_id_label(dump: Mapping[str, JSONValue]) -> str:
    """An `ExternalId` dump as the canonical `namespace://value` string.

    Round-trips through `ids.parse_guid`, including the legacy season/episode path
    form, so a required id can be compared against whatever form the repaired item
    ends up carrying. `raw` is deliberately not used: it holds `?lang=en` and the
    agent prefix, neither of which a repair is obliged to reproduce.
    """
    label = f"{dump['namespace']}://{dump['value']}"
    if dump.get("season") is not None:
        label += f"/{dump['season']}"
        if dump.get("episode") is not None:
            label += f"/{dump['episode']}"
    return label


def expectation_for(path: str, truth_value: JSONValue, corrupted_value: JSONValue) -> Expect:
    """The predicate that says "this field is back to its ground-truth value".

    * `/guids` -> `contains` the ground-truth ids, `excludes` the ids the
      corruption injected. Not equality: a rematch legitimately gains ids -- a
      `plex://` one, often a `tmdb://` one -- that are not the wrong match.
    * text -> `normalized_equals`. Case and NFC form are not the repair.
    * a ground truth of `null` -> `absent`.
    * everything else -> `equals`, compared as canonical bytes.

    `corrupted_value` is required rather than defaulted: a caller that forgot it
    would get a `/guids` predicate with no exclusions, which is the gap this
    parameter closes, reopened without a sound.
    """
    if path == GUIDS_POINTER:
        required = [external_id_label(item) for item in truth_value]
        injected = {external_id_label(item) for item in corrupted_value} - set(required)
        return Expect(op=Op.CONTAINS, value=required, excludes=tuple(sorted(injected)))
    if truth_value is None:
        return Expect(op=Op.ABSENT)
    if isinstance(truth_value, str):
        return Expect(op=Op.NORMALIZED_EQUALS, value=truth_value)
    return Expect(op=Op.EQUALS, value=truth_value)


def derive_postconditions(
    changes: Sequence[ItemChange], ground_truth_ids: frozenset[str]
) -> tuple[Postcondition, Postcondition]:
    """`(hard, soft)`, derived from the delta's MODIFYs and REMOVEs.

    Runs over `ChangeKind.MODIFY` on items that exist in the ground truth, for
    **every** class including the three whose witness is a relation. An `ADD`
    yields nothing -- it names the item the repair is supposed to make disappear,
    and the resolution covers it. A `REMOVE` is recorded soft as "this item is
    present again", addressed by the empty pointer (RFC 6901's whole document),
    because re-creating an emptied container is Plex's job after a rescan.
    """
    hard: Postcondition = {}
    soft: Postcondition = {}
    for change in changes:
        if change.kind is ChangeKind.REMOVE:
            soft.setdefault(change.item_id, {})[""] = Expect(op=Op.NON_EMPTY)
            continue
        if change.kind is not ChangeKind.MODIFY or change.item_id not in ground_truth_ids:
            continue
        for field in change.fields:
            target = hard if field_tier(field.path) is Tier.HARD else soft
            target.setdefault(change.item_id, {})[field.path] = expectation_for(
                field.path, field.before, field.after
            )
    return hard, soft


def derive_must_not_change(
    witness: DetectabilityWitness, changes: Sequence[ItemChange]
) -> tuple[str, ...]:
    """The witness's own pointers, minus what the delta touched, plus the floor.

    *A repair must not destroy the evidence that made the case solvable* -- with
    one exception that makes the rule correct rather than merely plausible.
    Measured in step 0.6, Finding 4: six of eight value classes have no overlap
    between their witness pointers and their delta, and the two that do are the
    point. For `filename_unmatchable` the witness **is** the corrupted path -- the
    scene name the corruption wrote -- and renaming that file is precisely the
    repair. Making the witness untouchable would forbid the correct answer.

    So the same subtraction applies to the floor: a selector matching a path the
    delta touched is dropped.
    """
    touched = {field.path for change in changes for field in change.fields}
    floor = [
        selector
        for selector in MUST_NOT_CHANGE_FLOOR
        if not any(matches(selector, path) for path in touched)
    ]
    # A witness pointer a retained selector already covers is dropped rather than
    # listed beside it: `/parts/0/path` and `/parts/*/path` in one constraint set
    # would have the scorer check the same location twice and report one violation
    # as two.
    kept = [
        pointer
        for pointer in witness.pointers
        if pointer not in touched and not any(matches(selector, pointer) for selector in floor)
    ]
    return tuple(sorted(set(kept) | set(floor)))


def derive_resolution(
    problem_class: ProblemClass, witness: DetectabilityWitness, ground_truth_ids: frozenset[str]
) -> Resolution:
    """The set relation a `kind=relation` witness already carries.

    The keeper is *the member of `item_ids` that exists in the ground-truth
    family* -- for `author_name_variant` and `multi_file_split` that is exactly the
    question the case asks, since one name is canonical and one file set is one
    book, and merging into the minted item is the wrong answer. It is
    `None` for the classes in `KEEPER_UNSETTLED`.
    """
    if witness.relation is None:
        raise TruthError(
            f"{problem_class}: a relation witness with no relation is a set of ids with "
            "a similarity score attached"
        )
    item_ids = tuple(sorted(witness.subjects))
    if len(item_ids) < MIN_AMBIGUITY_CANDIDATES:
        raise TruthError(f"{problem_class}: a relation needs at least two ids, got {item_ids}")

    keeper: str | None = None
    if problem_class not in KEEPER_UNSETTLED:
        surviving = sorted(item for item in item_ids if item in ground_truth_ids)
        if len(surviving) != 1:
            raise TruthError(
                f"{problem_class}: {len(surviving)} of the relation's ids existed before the "
                f"corruption ({surviving}), so the ground truth does not name one keeper. "
                "Either the corruption re-parented an item it should not have, or this "
                "class belongs in KEEPER_UNSETTLED with the reason recorded."
            )
        keeper = surviving[0]
    return Resolution(relation=witness.relation, item_ids=item_ids, keeper=keeper)


def already_failing_classes(
    screens: Sequence[screen_module.ItemScreen],
) -> tuple[ProblemClass, ...]:
    """Classes the clean family already fails, per the screen.

    The second of `known_other_problems`' two mechanical sources. **Not** read off
    `CorruptionResult.cross_check`: that carries one verdict for the case's *own*
    class, so its `already_failing` answers "was this class guarded before I broke
    it" and says nothing about the other fourteen. `multi_file_split` is always in
    that state -- it targets a book with two files and its guard is `single_part` --
    which is what makes the distinction easy to miss.
    """
    found: set[ProblemClass] = set()
    for screen in screens:
        failing = set(screen.failing_predicates)
        for problem_class, guards in screen_module.GUARD_TABLE.items():
            # A failing guard is evidence only about a class that can describe the
            # item. `filename_matches_metadata` guards `absolute_vs_seasonal` too,
            # so without this a film with a scene-release file would list a TV
            # class as a known problem -- and excuse exactly the false positive the
            # screen's trivially-guarded bucket exists to catch (Finding 5).
            if guards & failing and describes(problem_class, screen.media_kind):
                found.add(problem_class)
    return tuple(sorted(found, key=lambda pc: list(ProblemClass).index(pc)))


def required_finding(
    *,
    problem_class: ProblemClass,
    witness: DetectabilityWitness,
    changes: Sequence[ItemChange],
    ground_truth: Sequence[NormalizedItem],
) -> RequiredFinding:
    """One required finding, derived entirely from the delta and its witness.

    The shape is keyed on `witness.kind` and the two halves are **additive**: a
    relation class carries a resolution *and* a postcondition on every surviving
    item its delta rewrote. `duplicate_quality` is the one class with a resolution
    and no postcondition, because its delta is a pure ADD.
    """
    from shelfwarden.evals.corrupt.witness import WitnessKind

    ground_truth_ids = frozenset(str(item.item_id) for item in ground_truth)
    hard, soft = derive_postconditions(changes, ground_truth_ids)

    resolution: Resolution | None = None
    if witness.kind is WitnessKind.RELATION:
        resolution = derive_resolution(problem_class, witness, ground_truth_ids)
        item_ids = resolution.item_ids
    else:
        # The items a finding of this class must name: the pre-existing items the
        # delta modified. Not the postcondition's key set, which reaches further --
        # repairing a split book re-parents its parts.
        item_ids = tuple(
            sorted(
                change.item_id
                for change in changes
                if change.kind is ChangeKind.MODIFY and change.item_id in ground_truth_ids
            )
        )
    if not item_ids:
        raise TruthError(
            f"{problem_class}: no item for the finding to be about. A case whose delta "
            "neither modifies a pre-existing item nor carries a relation cannot be "
            "scored on anything."
        )

    return RequiredFinding(
        problem_class=problem_class,
        item_ids=item_ids,
        resolution=resolution,
        postcondition=hard,
        soft_postcondition=soft,
        must_not_change=derive_must_not_change(witness, changes),
        repair_op=RepairOpHint(any_of=REPAIR_OPS[problem_class]),
    )


def repair_expectation(
    *,
    problem_class: ProblemClass,
    witness: DetectabilityWitness,
    changes: Sequence[ItemChange],
    ground_truth: Sequence[NormalizedItem],
    induced: Sequence[ProblemClass] = (),
    clean_screens: Sequence[screen_module.ItemScreen] = (),
) -> RepairExpectation:
    """A `repair` expectation. `unexpected` is never passed: the default is the rule.

    `known_other_problems` has exactly two mechanical sources and no hand-written
    third: the problems the corruption knowingly created (`induced`), and the
    classes the ground-truth family already fails per the clean screen. The case's
    own class is excluded -- it is required, not excused.
    """
    finding = required_finding(
        problem_class=problem_class,
        witness=witness,
        changes=changes,
        ground_truth=ground_truth,
    )
    known = set(induced) | set(already_failing_classes(clean_screens))
    known.discard(problem_class)
    return RepairExpectation(
        required_findings=(finding,),
        known_other_problems=tuple(
            sorted(known, key=lambda pc: list(ProblemClass).index(pc)),
        ),
    )


def no_action_expectation(screen: screen_module.ItemScreen) -> NoActionExpectation:
    """A `no_action` expectation, read straight off the item's own screen.

    The three buckets are the screen's, unmodified. Every applicable check is
    recorded as a citation, so the label carries evidence rather than an assertion.
    """
    checks = tuple(
        VerificationCheck(
            predicate=check.predicate,
            evidence_id=check.evidence_id or "",
            result=check.status,
        )
        for check in screen.checks
        if check.status in (screen_module.CheckStatus.PASS, screen_module.CheckStatus.FAIL)
    )
    return NoActionExpectation(
        guarded_classes=screen.guarded_classes,
        trivially_guarded_classes=screen.trivially_guarded_classes,
        unguarded_classes=screen.unguarded_classes,
        verification=Verification(checks=checks),
    )


__all__ = [
    "CASE_PREFIX",
    "HARD_FIELDS",
    "KEEPER_UNSETTLED",
    "MUST_NOT_CHANGE_FLOOR",
    "REPAIR_OPS",
    "RUN_GROUP_PREFIX",
    "SCHEMA_VERSION",
    "SOFT_FIELDS",
    "Case",
    "EscalateExpectation",
    "Expect",
    "Expectation",
    "ExpectationKind",
    "NoActionExpectation",
    "Op",
    "Postcondition",
    "Provenance",
    "RepairExpectation",
    "RepairOp",
    "RepairOpHint",
    "RequiredFinding",
    "Resolution",
    "ScreenRef",
    "Slice",
    "SourceExport",
    "SubjectKeyRecord",
    "Tier",
    "TruthError",
    "TruthFile",
    "Unexpected",
    "Verification",
    "VerificationCheck",
    "already_failing_classes",
    "case_id",
    "corruption_fingerprint",
    "derive_must_not_change",
    "derive_postconditions",
    "derive_resolution",
    "expectation_for",
    "external_id_label",
    "field_tier",
    "load_truth",
    "no_action_expectation",
    "render_truth",
    "repair_expectation",
    "required_finding",
    "run_group",
]
