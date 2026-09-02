"""The corruption registry, and the checks a case must survive to ship.

Fifteen problem classes are declared here. **Eleven are implemented**; three wait
on step 1.1 because they need an external record as an ingredient or as a
witness, and one is not synthesizable by design. That split is a table
(`CORRUPTION_TABLE` and `UNSYNTHESIZABLE_REASON`), computed and published rather
than asserted in prose -- the same shape step 0.45 used for `GUARD_TABLE`, and
for the same reason: a count typed into a docstring is a count that drifts.

Two rejection populations, kept apart for the reason 0.45 keeps `not_applicable`
and `unavailable` apart. **Not applicable** means the family was never a
candidate -- a supply fact about the library. **Rejected** means a corruption was
attempted and failed a check -- a fact about this machinery. Collapsing them
turns "your library has no remake pairs" into "the generator is broken", and only
one of those is actionable.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256

from pydantic import BaseModel, ConfigDict

from shelfwarden.evals import export as export_module
from shelfwarden.evals import screen as screen_module
from shelfwarden.evals.corrupt.collateral import collateral_ids
from shelfwarden.evals.corrupt.context import CorruptionContext, stub_of
from shelfwarden.evals.corrupt.model import ChangeKind, CorruptionError, ItemChange, Rejection
from shelfwarden.evals.corrupt.reverse import diff_items, render_family, reverses_cleanly
from shelfwarden.evals.corrupt.witness import DetectabilityWitness, WitnessKind, WitnessTier
from shelfwarden.models.finding import ProblemClass
from shelfwarden.models.item import ItemStub, MediaKind, NormalizedItem


class CrossCheck(BaseModel):
    """What the screen said about this class, before and after the corruption.

    Step 0.5 found `GUARD_TABLE[EPISODE_WRONG_SEASON]` claiming a guard it did not
    have. The realistic corruption re-parents an episode -- setting `parent`,
    `parent_index`, and `parent_title` together, the only form Plex can represent
    -- which leaves `season_membership_coherent` internally consistent and
    *passing* while the episode is plainly misfiled. An item could therefore be
    labelled *verified not to have* a problem it had, and a correct agent finding
    on it would have scored as a false positive.

    So every corruption is re-screened. The comparison is **before against
    after**, not after alone: with a `NullAuthority` most guards are unavailable
    anyway, so "not guarded afterwards" on its own would report every case as a
    success and prove nothing at all.

    Five verdicts:

    * `broken` -- guarded before, not after. The corruption did what it claims.
    * `intact` -- guarded before **and** after. Rejected: either the corruption
      is a lie or the guard is.
    * `already_failing` -- not guarded before, and the guards were answered. The
      ground-truth family already fails this class, and step 0.6 routes it to
      `known_other_problems` rather than requiring it.
    * `unavailable` -- not guarded before because no guard predicate could be
      answered: an authority-tier guard, with `sources/` still to come. Recorded,
      never counted as a pass.
    * `unguarded` -- the class has no guard predicate at all.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: str
    guard_predicates: tuple[str, ...] = ()
    detail: str | None = None


CROSS_BROKEN = "broken"
CROSS_INTACT = "intact"
CROSS_UNAVAILABLE = "unavailable"
CROSS_UNGUARDED = "unguarded"
CROSS_ALREADY_FAILING = "already_failing"


class CorruptionResult(BaseModel):
    """One emitted case: what was broken, how to undo it, and why it is solvable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    problem_class: ProblemClass
    media_kind: MediaKind
    root_id: str
    subject_key: str
    variant: str
    changes: tuple[ItemChange, ...]
    witness: DetectabilityWitness
    cross_check: CrossCheck
    # Problems this corruption knowingly creates *inside* its own family. Step 0.6
    # writes them into `known_other_problems`, where they are neither required nor
    # penalized -- without which `unexpected: fail` would score a correct finding
    # on a manufactured twin as a false positive.
    induced: tuple[ProblemClass, ...] = ()
    # Ids *outside* the family whose population-scope guard this corruption moved.
    collateral: tuple[str, ...] = ()
    added_roots: tuple[ItemStub, ...] = ()
    ground_truth_sha256: str
    corrupted_sha256: str


@dataclass(frozen=True, slots=True)
class Applicability:
    """Whether a family is a candidate, and why not when it is not."""

    ok: bool
    reason: str = ""
    detail: str | None = None

    @classmethod
    def yes(cls) -> "Applicability":
        return cls(True)

    @classmethod
    def no(cls, reason: str, detail: str | None = None) -> "Applicability":
        return cls(False, reason, detail)


@dataclass(frozen=True, slots=True)
class Mutation:
    """What a corruption function hands back.

    `items` is the **mutated family**, not a delta: the delta is read back from
    the dumps by `diff_items`, which is what stops a corruption from recording an
    intent the model declined to store (an NFD title stored as NFC, a guid list
    stored re-sorted).

    The witness is built by the corruption because only the corruption knows what
    it broke -- and it is built over the *mutated* items, through
    `LocalWitness.over`, so the anti-circularity rule is structural: a witness
    cannot cite the clean family because it never sees it.
    """

    items: tuple[NormalizedItem, ...]
    witness: DetectabilityWitness
    induced: tuple[ProblemClass, ...] = ()


Outcome = CorruptionResult | Rejection
CorruptFn = Callable[[export_module.Family, CorruptionContext], "Mutation | Rejection"]
ApplicableFn = Callable[[export_module.Family, CorruptionContext], Applicability]


@dataclass(frozen=True, slots=True)
class CorruptionSpec:
    """One registered corruption.

    `corrupt` returns the **mutated family** rather than a delta: the delta is
    read back from the dumps by `diff_items`, which is what stops a corruption
    from recording an intent the model declined to store.
    """

    problem_class: ProblemClass
    applies_to: frozenset[MediaKind]
    variants: tuple[str, ...]
    witness_kind: WitnessKind
    tier: WitnessTier
    induces: tuple[ProblemClass, ...]
    applicable: ApplicableFn
    corrupt: CorruptFn
    doc: str


CORRUPTION_TABLE: dict[ProblemClass, CorruptionSpec] = {}

# Why a class has no corruption function. Mirrors `screen.UNGUARDABLE_REASON`:
# the difference between "not built yet" and "not buildable" is the difference
# between a task and a design decision, and a reader should not have to guess.
UNSYNTHESIZABLE_REASON: dict[ProblemClass, str] = {
    ProblemClass.FOREIGN_TITLE_VARIANT: (
        "needs an authority record as an *ingredient*: the corruption substitutes a real "
        "TMDB `alternative_titles` entry, and inventing one produces a case about a film "
        "that does not exist. Lands with sources/ in step 1.1."
    ),
    ProblemClass.MISSING_METADATA: (
        "detecting the problem is local -- the summary is empty -- but resolving the "
        "ground-truth value is not, and the postcondition is the original text. Weakening "
        "it to 'non-empty and cited' would make the class shippable and the metric "
        "meaningless. Lands with sources/ in step 1.1."
    ),
    ProblemClass.NARRATOR_AS_AUTHOR: (
        "needs a real narrator name as an ingredient. Taking another author's name from "
        "the export produces author_name_variant wearing a different label. Lands with "
        "Audnexus in step 1.1."
    ),
    ProblemClass.ANTHOLOGY_OMNIBUS: (
        "not synthesizable by design: the expectation is `escalate`, so the case is "
        "solvable exactly when the ambiguity is evidenced, and it needs constituent "
        "titles from an authority. Curated by hand into the ambiguous slice (step 0.9)."
    ),
}


def corruption(
    problem_class: ProblemClass,
    *,
    applies_to: set[MediaKind] | frozenset[MediaKind],
    variants: Sequence[str],
    witness: WitnessKind,
    tier: WitnessTier,
    applicable: ApplicableFn,
    induces: Sequence[ProblemClass] = (),
) -> Callable[[CorruptFn], CorruptFn]:
    """Register a corruption function against its problem class."""

    def register(function: CorruptFn) -> CorruptFn:
        if problem_class in CORRUPTION_TABLE:
            raise CorruptionError(f"{problem_class} is already registered")
        if not variants:
            raise CorruptionError(f"{problem_class} declares no variants")
        CORRUPTION_TABLE[problem_class] = CorruptionSpec(
            problem_class=problem_class,
            applies_to=frozenset(applies_to),
            variants=tuple(variants),
            witness_kind=witness,
            tier=tier,
            induces=tuple(induces),
            applicable=applicable,
            corrupt=function,
            doc=(function.__doc__ or "").strip().splitlines()[0] if function.__doc__ else "",
        )
        return function

    return register


def implemented_classes() -> tuple[ProblemClass, ...]:
    """The classes with a corruption function, in declaration order."""
    return tuple(pc for pc in ProblemClass if pc in CORRUPTION_TABLE)


def deferred_classes() -> tuple[ProblemClass, ...]:
    return tuple(pc for pc in ProblemClass if pc not in CORRUPTION_TABLE)


def _digest(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _roots_after(
    roots: Sequence[ItemStub], family_ids: frozenset[str], corrupted: Sequence[NormalizedItem]
) -> tuple[ItemStub, ...]:
    """The population index as it stands *after* the corruption.

    Derived from the corrupted items rather than patched: a stale index makes the
    twin relation asymmetric, and the screen then reports a guard that is false.
    """
    kept = [stub for stub in roots if str(stub.item_id) not in family_ids]
    added = [stub_of(item) for item in corrupted if getattr(item, "parent", None) is None]
    return tuple(kept + added)


def _guard_status(
    problem_class: ProblemClass,
    ctx: CorruptionContext,
    items: Sequence[NormalizedItem],
    roots: Sequence[ItemStub],
    guards: frozenset[screen_module.Predicate],
    subjects: frozenset[str],
) -> tuple[bool, bool]:
    """`(guarded_on_some_subject, some_subject_had_every_guard_answered)`.

    Two scopings, both load-bearing.

    The screen **context** is built over the whole family and the whole
    population, because family- and population-scoped predicates need it. The
    **verdict** is read only for `subjects` -- the items this corruption actually
    changed. Reading it across the family instead would let an untouched sibling
    keep the guard alive: moving one episode leaves the show's other episodes
    correctly filed and correctly guarded, and the corruption would be reported as
    having changed nothing.

    The second value is per **item**, not per predicate. A class guarded by one
    local and one authority predicate has the local one answered on every item and
    is still unguardable until step 1.1; counting predicates would read that as
    "the ground truth already fails this class", which is a different and much
    more alarming claim.
    """
    screen_ctx = screen_module.ScreenContext.build(
        export_id=ctx.export_id,
        items=items,
        roots=roots,
        authority=screen_module.NullAuthority(),
        policy=ctx.policy,
    )
    decided = (screen_module.CheckStatus.PASS, screen_module.CheckStatus.FAIL)
    guarded = False
    answered = False
    for item in items:
        if str(item.item_id) not in subjects:
            continue
        result = screen_module.screen_item(screen_ctx, item)
        if problem_class in result.guarded_classes:
            guarded = True
        status = {check.predicate: check.status for check in result.checks}
        if all(status[predicate] in decided for predicate in guards):
            answered = True
    return guarded, answered


def cross_check(
    spec: CorruptionSpec,
    ctx: CorruptionContext,
    clean: Sequence[NormalizedItem],
    corrupted: Sequence[NormalizedItem],
    roots_before: Sequence[ItemStub],
    roots_after: Sequence[ItemStub],
    subjects: frozenset[str],
) -> CrossCheck:
    """Did this corruption actually break the guard the screen claims for its class?

    `subjects` is the set of items the delta touched. A corruption is corroborated
    when the guard held on those items before and does not hold on them after --
    not when it fails somewhere in the family, and not when it merely fails
    afterwards.
    """
    guards = screen_module.GUARD_TABLE[spec.problem_class]
    names = tuple(sorted(str(predicate) for predicate in guards))
    if not guards:
        return CrossCheck(
            verdict=CROSS_UNGUARDED,
            detail=screen_module.UNGUARDABLE_REASON.get(spec.problem_class, "no guard predicate"),
        )

    before, answered = _guard_status(spec.problem_class, ctx, clean, roots_before, guards, subjects)
    if not before:
        if not answered:
            return CrossCheck(
                verdict=CROSS_UNAVAILABLE,
                guard_predicates=names,
                detail=(
                    "no guard predicate could be answered on the clean family, so the "
                    "screen cannot corroborate this corruption"
                ),
            )
        return CrossCheck(
            verdict=CROSS_ALREADY_FAILING,
            guard_predicates=names,
            detail="the ground-truth family already fails this class's guard",
        )

    after, _ = _guard_status(spec.problem_class, ctx, corrupted, roots_after, guards, subjects)
    if after:
        return CrossCheck(
            verdict=CROSS_INTACT,
            guard_predicates=names,
            detail=(
                "the screen still guards this class after the corruption: either the "
                "corruption did not do what it claims, or the guard does not cover it"
            ),
        )
    return CrossCheck(verdict=CROSS_BROKEN, guard_predicates=names)


def attempt(spec: CorruptionSpec, family: export_module.Family, ctx: CorruptionContext) -> Outcome:
    """Run one corruption and every acceptance check, in order.

    The checks, and what each stops from shipping:

    1. **not applicable** -- the family was never a candidate. A supply fact.
    2. **empty_delta** -- the corruption returned the family unchanged.
    3. **noop_change** -- *raised* by `FieldChange`, not rejected: a corruption
       that emits a no-op is a bug in the corruption rather than a property of
       the family.
    4. **reverse_mismatch** -- undoing the delta does not reproduce the ground
       truth byte-for-byte.
    5. **witness_indiscriminate** -- the evidence does not tell the ground truth
       and the corrupted value apart, which is the whole point of the step. The
       witness's own anti-circularity check is inside it.
    6. **screen_intact** -- the screen still guards this class on the corrupted
       family. Either the corruption did not do what it claims or the guard is
       wrong; neither may reach a dataset.
    """
    applicability = spec.applicable(family, ctx)
    if not applicability.ok:
        return ctx.reject(applicability.reason, applicability.detail, applicable=False)

    produced = spec.corrupt(family, ctx)
    if isinstance(produced, Rejection):
        return produced

    ground_truth = family.records
    changes = diff_items(ground_truth, produced.items)
    if not changes:
        return ctx.reject("empty_delta", "the corruption returned the family unchanged")

    if not reverses_cleanly(ground_truth, produced.items, changes):
        return ctx.reject(
            "reverse_mismatch",
            "undoing the recorded delta does not reproduce the ground truth byte-for-byte",
        )

    witness = produced.witness
    if not witness.discriminates:
        return ctx.reject("witness_indiscriminate", witness.detail)

    family_ids = frozenset(str(item.item_id) for item in ground_truth) | {
        change.item_id for change in changes
    }
    roots_after = _roots_after(ctx.roots, family_ids, produced.items)
    # Scoped to what the delta touched, plus the family's roots. Two reasons, and
    # the second is easy to miss:
    #
    # * An untouched sibling that is still correctly filed is still correctly
    #   guarded, so reading the whole family would report a real corruption as
    #   having changed nothing.
    # * A corruption that only *adds* an item -- `duplicate_quality` clones a film
    #   and modifies nothing -- has no subject that exists on both sides. The item
    #   whose guard actually moves is the original, which is the root.
    touched = frozenset(
        change.item_id for change in changes if change.kind is not ChangeKind.REMOVE
    ) | frozenset(
        str(item.item_id) for item in ground_truth if getattr(item, "parent", None) is None
    )
    checked = cross_check(spec, ctx, ground_truth, produced.items, ctx.roots, roots_after, touched)
    if checked.verdict == CROSS_INTACT:
        return ctx.reject("screen_intact", checked.detail)

    added_roots = tuple(
        stub_of(item)
        for item in produced.items
        if getattr(item, "parent", None) is None
        and str(item.item_id) in {c.item_id for c in changes if c.kind is ChangeKind.ADD}
    )
    return CorruptionResult(
        problem_class=spec.problem_class,
        media_kind=family.root.media_kind,
        root_id=str(family.root.item_id),
        subject_key=str(ctx.subject),
        variant=ctx.variant,
        changes=changes,
        witness=witness,
        cross_check=checked,
        induced=tuple(sorted(set(spec.induces) | set(produced.induced))),
        collateral=collateral_ids(ctx.roots, family_ids, produced.items),
        added_roots=added_roots,
        ground_truth_sha256=_digest(render_family(ground_truth)),
        corrupted_sha256=_digest(render_family(produced.items)),
    )


__all__ = [
    "CORRUPTION_TABLE",
    "CROSS_ALREADY_FAILING",
    "CROSS_BROKEN",
    "CROSS_INTACT",
    "CROSS_UNAVAILABLE",
    "CROSS_UNGUARDED",
    "UNSYNTHESIZABLE_REASON",
    "Applicability",
    "CorruptionResult",
    "CorruptionSpec",
    "CrossCheck",
    "Mutation",
    "Outcome",
    "attempt",
    "corruption",
    "cross_check",
    "deferred_classes",
    "implemented_classes",
]
