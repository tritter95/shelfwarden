"""Turning a survey of possible corruptions into a dataset.

Step 0.5 produces a *survey*: every applicable class against every applicable
family, no slices, no balance, no identity that survives a re-export. A dataset is
the opposite of a survey -- a deliberate selection with a published composition,
whose cases keep their identity when the library beneath them is re-exported. If
they do not, the spec's relative CI gate ("no case that passed may now fail") is
decorative.

**The dataset is the clean export plus one delta per case.** A world is composed
per case rather than shared, which is what makes three problems disappear instead
of being managed: no case's donor is another case's subject, a case's collateral is
scoped to itself, and a should-not-touch case's guard claims are *exactly* the clean
screen's. It also keeps the dataset small -- deltas, not N copies of a library --
and hands 0.7 a narrow contract, `SnapshotLibrary.for_case(export, dataset,
case_id)`, over an `apply_changes` that already exists. The cost is that a case is
never seen against a realistically messy library; that is the right trade at Phase
0, where the job is measurement rather than simulation.

**Nothing here draws from an RNG.** Selection is by hash rank throughout, for the
reason step 0.5 recorded and verified: `random.sample` is not a prefix-stable
function of `k`, so raising a cell's target by one would re-pick different subjects
and reset every `case_id` in that cell. Ranking by `sha256(seed | subject_key)` is
additive by construction -- a larger target is a superset of a smaller one.

**The subject cap is drawn before any target is read**, which is subtler and was
wrong in the first draft of this step's plan. A first-come cap consumed in cell
order breaks exactly the stability everything else here protects: with the cap at 1
and cells walked in class order, cell A takes `s1` and cell C takes `s2`; raise the
count until A's target reaches 2 and A now takes `s1` *and* `s2`, so C's case on
`s2` disappears and reappears on `s3`. So each subject is issued its tickets by
hash rank over its own candidate classes, before any cell target exists. A cell then
selects only among subjects already holding a ticket for it, and no cell's choices
depend on another cell's target -- or on whether another cell exists at all, which
is what keeps a `composition.toml` edit from moving cases in cells it did not touch.
"""

import argparse
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from shelfwarden import __version__
from shelfwarden.canonical import canonical_json
from shelfwarden.evals import composition as composition_module
from shelfwarden.evals import curated as curated_module
from shelfwarden.evals import export as export_module
from shelfwarden.evals import screen as screen_module
from shelfwarden.evals import truth as truth_module
from shelfwarden.evals.corrupt.context import (
    CorruptionContext,
    group_families,
    rank_key,
    subject_key,
)
from shelfwarden.evals.corrupt.model import Rejection
from shelfwarden.evals.corrupt.registry import (
    CORRUPTION_TABLE,
    UNSYNTHESIZABLE_REASON,
    CorruptionResult,
    attempt,
)
from shelfwarden.evals.corrupt.report import render_rejected
from shelfwarden.evals.corrupt.run import read_export_with_population, variant_for
from shelfwarden.models.finding import ProblemClass
from shelfwarden.models.item import ItemStub, MediaKind, NormalizedItem

SCHEMA_VERSION = 1

DATASET_FILE = "dataset.json"
TRUTH_FILE = "truth.json"
DELTAS_FILE = "deltas.jsonl"
REJECTED_FILE = "rejected.jsonl"
REPORT_FILE = "report.md"

# `datasets/evals/<id>/` rather than the `datasets/<id>/` the implementation plan
# names: `exports/`, `screens/`, `corruptions/` and `curated/` already live one
# level down, and a bare dataset id beside them reads as a fifth artifact *type* --
# with `curated/` in particular reading as a dataset.
DEFAULT_DATASET_ROOT = Path("datasets/evals")

# How many cases one subject may host. Chosen against an 11-family fixture whose
# busiest family is a candidate for four classes, so the cap bites there and its
# effect is visible in the fixture's own deficit table rather than only on a real
# library. The number to set it by is the concentration in the first real export --
# `subjects_covered` against `cases` -- and it should be revisited there.
MAX_CASES_PER_SUBJECT = 3

# How far down a ranked candidate list a cell may walk looking for successes. A
# rejected attempt must not silently shrink a cell, and must not cause an unbounded
# walk either. The window grows monotonically with the target, so prefix stability
# survives: a larger target sees a superset of the candidates a smaller one saw.
OVERSAMPLE = 3
MIN_ATTEMPT_WINDOW = 8

# The pseudo-class a should-not-touch case holds a ticket for. It shares the ticket
# ranking with the real classes so that one subject cannot host a repair case in
# every class *and* a should-not-touch case for free.
NO_ACTION_TICKET = "no_action"

EXAMPLE_CAP = 5


class GenerateError(Exception):
    """The dataset could not be built. Every message names a concrete next action."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# -- what the dataset records about itself --------------------------------


class ExcludedSubject(_Frozen):
    """A subject key held by more than one family, and therefore not a subject.

    Verified in step 0.6, Finding 2: the subject ladder's first rung is the first
    resolvable guid, and two library entries that are duplicates of one work carry
    the same guid -- which is the very condition `duplicate_quality` exists to
    describe. Two colliding subjects selected for the same class and variant would
    produce **one `case_id` for two cases**, and the baseline would then track one
    of them arbitrarily.

    A collision is never repaired by appending an ordinal: a disambiguating suffix
    is positional identity smuggled back in, which invariant 9 forbids. So
    uniqueness is a *selection precondition*, and the exclusion is counted here
    rather than silently applied -- a library full of duplicates will see this, and
    seeing it is the point.
    """

    subject_key: str
    root_ids: tuple[str, ...]


class CellResult(_Frozen):
    """What one cell actually produced, and how hard it tried."""

    slice: truth_module.Slice
    media_kind: MediaKind
    problem_class: ProblemClass | None
    intended: int
    achievable: int
    candidates: int
    ticketed: int
    attempted: int
    window: int
    capped_away: int
    rejected: int
    not_applicable: int
    surplus_dropped: int = 0
    # Families this cell would have drawn on whose subject another family also
    # holds, so neither is a subject (Decision 5). Counted per cell, as the plan
    # requires, rather than only in the dataset-wide `excluded_subjects` list:
    # otherwise a cell short because its candidates were excluded reads as a
    # library that has none, which is a different fact with a different fix.
    excluded_away: int = 0


class DatasetCounts(_Frozen):
    """`subjects_covered` sits beside `cases` on purpose.

    A dataset of *N* cases does not imply *N* items examined: the same film can be
    the subject of a `wrong_match` case and a `filename_unmatchable` case, and an
    agent that mishandles that film fails twice. Pass rate then over-weights
    whichever items happen to be corruptible, so the concentration has to be visible
    rather than inferred.
    """

    cases: int
    subjects_covered: int
    by_slice: dict[str, int]
    by_media_kind: dict[str, int]
    by_problem_class: dict[str, int]


class Dataset(_Frozen):
    """`dataset.json` -- the composition, the gap, and the lineage.

    Carries **no timestamp**: a dataset is a pure function of the export, the seed,
    the composition, and the code that read them, and byte-identity is the cheapest
    proof of that. `screen.json` and `corruptions.json` omit one for the same reason.
    """

    schema_version: int = SCHEMA_VERSION
    shelfwarden_version: str
    generator_version: str
    dataset_id: str
    lineage_id: str
    composition_id: str
    seed: int
    count: int
    max_cases_per_subject: int
    source_export: truth_module.SourceExport
    screen: truth_module.ScreenRef
    counts: DatasetCounts
    cells: tuple[CellResult, ...]
    deficits: tuple[composition_module.CompositionDeficit, ...]
    excluded_subjects: tuple[ExcludedSubject, ...]


@dataclass(frozen=True, slots=True)
class GenerateResult:
    dataset: Dataset
    truth: truth_module.TruthFile
    deltas: tuple[tuple[str, tuple], ...]
    rejections: tuple[Rejection, ...]


# -- identity -------------------------------------------------------------


def _digest(payload: object) -> str:
    return sha256(canonical_json(payload)).hexdigest()


def dataset_id(seed: int, items_sha256: str, generator_version: str) -> str:
    """`sw-<digest[:12]>` over the seed, the export, and the generator.

    Deliberately not the `sw-20260901-a1b2c3` date form the implementation plan
    illustrates: a date is not a function of those three inputs, and this step's gate
    is that `--count N --seed S` is reproducible. The formula in the plan is
    normative; the illustration was not.
    """
    return (
        "sw-"
        + _digest(
            {"seed": seed, "items_sha256": items_sha256, "generator_version": generator_version}
        )[: truth_module.DIGEST_CHARS]
    )


def lineage_id(
    manifest: export_module.Manifest, generator_version: str, schema_version: int
) -> str:
    """What actually decides whether two datasets are comparable.

    `implementation-plan.md` keys this on `sha256(composition.toml + slice defs)` and
    makes it the baseline key, which resets the baseline on the one edit
    `composition.toml` exists to let a human make. Moving a share does not make an
    older result untrue: the four CI buckets already express a composition edit
    correctly -- cases the new shares dropped are absent, cases they added are `new`
    and gate nothing, and everything else keeps its `case_id` and its history.

    So the lineage is the library and the generator, and `composition_id` is recorded
    beside it as a diagnostic. A reader can then see that the composition moved while
    the baseline did not, which is the fact they want.
    """
    return (
        "lin-"
        + _digest(
            {
                "provider": manifest.provider.provider,
                "section_ids": sorted(section.section_id for section in manifest.sections),
                "generator_version_major": generator_version.split(".")[0],
                "schema_version": schema_version,
            }
        )[: truth_module.DIGEST_CHARS]
    )


def ticket_rank(seed: int, subject: str, label: str) -> str:
    """`sha256(seed | subject_key | label)` -- the order a subject spends its cap in.

    A function of the subject and the class alone, so it is decided before any cell
    target is read. Raising `--count` and editing `composition.toml` therefore cannot
    move an existing case. Shipping a *new corruption class* can: a subject that
    becomes a candidate for a twelfth class re-ranks its tickets, and a class holding
    one may lose it. There is no cap that avoids this -- capping at all means some
    class goes without -- so it is recorded rather than engineered around. It lands
    once, in the CI diff's `new` and absent buckets.
    """
    return _digest({"seed": seed, "subject": subject, "label": label})


# -- the subject index and its tickets ------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """One family, addressed by the subject it is about."""

    subject: str
    family: export_module.Family
    media_kind: MediaKind


def index_subjects(
    families: Sequence[export_module.Family],
) -> tuple[tuple[Candidate, ...], tuple[ExcludedSubject, ...]]:
    """One candidate per subject, and the collisions that were dropped.

    A subject held by more than one family is not a subject -- see
    `ExcludedSubject`. The families are keyed on the *root* record's subject, which
    is what `run.run_corruptions` does and what `CorruptionResult.subject_key`
    records.
    """
    grouped: dict[str, list[export_module.Family]] = {}
    for family in families:
        grouped.setdefault(str(subject_key(family.records[0])), []).append(family)

    candidates: list[Candidate] = []
    excluded: list[ExcludedSubject] = []
    for subject, members in sorted(grouped.items()):
        if len(members) > 1:
            excluded.append(
                ExcludedSubject(
                    subject_key=subject,
                    root_ids=tuple(sorted(str(member.root.item_id) for member in members)),
                )
            )
            continue
        (only,) = members
        candidates.append(Candidate(subject=subject, family=only, media_kind=only.root.media_kind))
    return tuple(candidates), tuple(excluded)


def candidate_labels(candidate: Candidate, guarded: frozenset[str]) -> tuple[str, ...]:
    """Every cell label this subject could be drawn into, before any target exists.

    Applicability is deliberately **not** consulted. `spec.applicable` is a pure
    function and could be, which would stop a subject burning a ticket on a class
    that will reject it -- but it also reads the whole export, so a change anywhere
    in the library would re-rank an unrelated subject's tickets. Between wasting some
    supply and making case identity depend on edits elsewhere, the project's rule is
    clear: report the gap. The waste is visible as `no_candidates` rows in the
    deficit table.
    """
    labels = [
        str(problem_class)
        for problem_class, spec in CORRUPTION_TABLE.items()
        if candidate.media_kind in spec.applies_to
    ]
    if str(candidate.family.root.item_id) in guarded:
        labels.append(NO_ACTION_TICKET)
    return tuple(sorted(labels))


def issue_tickets(
    seed: int, candidates: Sequence[Candidate], guarded: frozenset[str]
) -> dict[str, frozenset[str]]:
    """Each subject's cap, spent by hash rank over its own candidate labels."""
    tickets: dict[str, frozenset[str]] = {}
    for candidate in candidates:
        labels = candidate_labels(candidate, guarded)
        ranked = sorted(
            labels, key=lambda label: (ticket_rank(seed, candidate.subject, label), label)
        )
        tickets[candidate.subject] = frozenset(ranked[:MAX_CASES_PER_SUBJECT])
    return tickets


def _attempt_window(target: int) -> int:
    return max(math.ceil(target * OVERSAMPLE), MIN_ATTEMPT_WINDOW)


def _ranked(seed: int, candidates: Sequence[Candidate]) -> tuple[Candidate, ...]:
    """Candidates in hash-rank order, ties broken by the subject key itself.

    The tie-break is not decoration: two subjects whose digests collided would
    otherwise be ordered by list position, which is the export's record order, which
    moves when the library does.
    """
    return tuple(sorted(candidates, key=lambda row: (rank_key(seed, row.subject), row.subject)))


# -- assembling a case ----------------------------------------------------


def _case_from_corruption(
    *,
    result: CorruptionResult,
    slice_: truth_module.Slice,
    seed: int,
    family: export_module.Family,
    screens: Mapping[str, screen_module.ItemScreen],
    generator_version: str,
) -> truth_module.Case:
    subject = result.subject_key
    kind, _, value = subject.partition(":")
    expectation = truth_module.repair_expectation(
        problem_class=result.problem_class,
        witness=result.witness,
        changes=result.changes,
        ground_truth=family.records,
        induced=result.induced,
        clean_screens=[
            screens[str(record.item_id)]
            for record in family.records
            if str(record.item_id) in screens
        ],
    )
    item_ids = tuple(
        sorted({item for finding in expectation.required_findings for item in finding.item_ids})
    )
    return truth_module.Case(
        case_id=truth_module.case_id(
            slice_=slice_,
            problem_class=result.problem_class,
            media_kind=result.media_kind,
            subject_key=subject,
            corruption_variant=result.variant,
        ),
        slice=slice_,
        run_group=truth_module.run_group(seed, subject),
        problem_class=result.problem_class,
        media_kind=result.media_kind,
        subject_key=truth_module.SubjectKeyRecord(kind=kind, value=value),
        corruption_variant=result.variant,
        corruption_fingerprint=truth_module.corruption_fingerprint(
            result.changes, generator_version
        ),
        item_ids=item_ids,
        expectation=expectation,
        witness=result.witness,
        ground_truth=family.records,
        provenance=truth_module.Provenance(method="synthetic"),
        collateral=result.collateral,
    )


def _case_from_screen(
    *,
    candidate: Candidate,
    seed: int,
    screen: screen_module.ItemScreen,
) -> truth_module.Case:
    """A should-not-touch case: no class, no variant, and the clean export as its world.

    Its guard claims are *exactly* the clean screen's, with no collateral filtering
    at all -- which is what Decision 1 of this step's plan buys. Step 0.5's handoff
    ("should-not-touch selection must exclude every id named in `collateral`") is
    retired by that decision: with per-case worlds there is no shared world for a
    collateral id to be wrong in.
    """
    kind, _, value = candidate.subject.partition(":")
    return truth_module.Case(
        case_id=truth_module.case_id(
            slice_=truth_module.Slice.SHOULD_NOT_TOUCH,
            problem_class=None,
            media_kind=candidate.media_kind,
            subject_key=candidate.subject,
            corruption_variant=None,
        ),
        slice=truth_module.Slice.SHOULD_NOT_TOUCH,
        run_group=truth_module.run_group(seed, candidate.subject),
        problem_class=None,
        media_kind=candidate.media_kind,
        subject_key=truth_module.SubjectKeyRecord(kind=kind, value=value),
        corruption_variant=None,
        corruption_fingerprint=None,
        item_ids=(screen.item_id,),
        expectation=truth_module.no_action_expectation(screen),
        witness=None,
        ground_truth=candidate.family.records,
        provenance=truth_module.Provenance(method="screen"),
    )


def _case_from_curated(
    *,
    case: curated_module.CuratedCase,
    slice_: truth_module.Slice,
    seed: int,
    by_id: Mapping[str, NormalizedItem],
    family_of: Mapping[str, export_module.Family],
) -> truth_module.Case:
    """Bind a hand-labelled case to the export it describes.

    Everything mechanical is derived here rather than typed by a human, so a curated
    case cannot carry an identity that disagrees with the library.
    """
    missing = [item_id for item_id in case.item_ids if item_id not in by_id]
    if missing:
        raise GenerateError(
            f"curated {slice_} case names item(s) {missing} that this export does not hold. "
            "Rating keys move on rescan, so a curated case is addressed by live ids and has "
            "to be re-pointed after a re-export: re-run the 0.9 adjudication for it, or drop "
            "the case."
        )
    if case.problem_class is None:
        raise GenerateError(
            f"curated {slice_} case {list(case.item_ids)} declares no problem_class. The "
            f"{slice_} slice is split by class in composition.toml, so a case without one "
            "cannot be placed in a cell."
        )
    anchor = case.item_ids[0]
    family = family_of[anchor]
    subject = str(subject_key(family.records[0]))
    kind, _, value = subject.partition(":")
    return truth_module.Case(
        case_id=truth_module.case_id(
            slice_=slice_,
            problem_class=case.problem_class,
            media_kind=family.root.media_kind,
            subject_key=subject,
            corruption_variant=None,
        ),
        slice=slice_,
        run_group=truth_module.run_group(seed, subject),
        problem_class=case.problem_class,
        media_kind=family.root.media_kind,
        subject_key=truth_module.SubjectKeyRecord(kind=kind, value=value),
        corruption_variant=None,
        corruption_fingerprint=None,
        item_ids=tuple(case.item_ids),
        expectation=case.expectation,
        witness=None,
        ground_truth=family.records,
        provenance=case.provenance,
    )


# -- the run --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CellOutcome:
    cases: tuple[truth_module.Case, ...]
    deltas: tuple[tuple[str, tuple], ...]
    rejections: tuple[Rejection, ...]
    result: CellResult


def _synthetic_cell(
    *,
    cell: composition_module.Cell,
    seed: int,
    export_id: str,
    candidates: Sequence[Candidate],
    tickets: Mapping[str, frozenset[str]],
    by_id: Mapping[str, NormalizedItem],
    roots: Sequence[ItemStub],
    screens: Mapping[str, screen_module.ItemScreen],
    generator_version: str,
    excluded: Sequence[export_module.Family],
) -> _CellOutcome:
    problem_class = cell.problem_class
    assert problem_class is not None
    spec = CORRUPTION_TABLE.get(problem_class)
    excluded_away = sum(
        1
        for family in excluded
        if family.root.media_kind is cell.media_kind
        and spec is not None
        and family.root.media_kind in spec.applies_to
    )

    in_scope = [
        candidate
        for candidate in candidates
        if candidate.media_kind is cell.media_kind
        and spec is not None
        and candidate.media_kind in spec.applies_to
    ]
    ticketed = [
        candidate
        for candidate in in_scope
        if str(problem_class) in tickets.get(candidate.subject, frozenset())
    ]
    ranked = _ranked(seed, ticketed)
    window = _attempt_window(cell.intended)

    cases: list[truth_module.Case] = []
    deltas: list[tuple[str, tuple]] = []
    rejections: list[Rejection] = []
    attempted = 0
    not_applicable = 0
    if spec is not None and cell.intended > 0:
        for candidate in ranked[:window]:
            if len(cases) >= cell.intended:
                break
            family = candidate.family
            ctx = CorruptionContext.build(
                export_id=export_id,
                seed=seed,
                problem_class=problem_class,
                variant=variant_for(spec, seed, candidate.subject),
                root=family.root,
                subject=subject_key(family.records[0]),
                items=by_id,
                roots=roots,
            )
            outcome = attempt(spec, family, ctx)
            if isinstance(outcome, Rejection):
                rejections.append(outcome)
                if outcome.applicable:
                    attempted += 1
                else:
                    not_applicable += 1
                continue
            attempted += 1
            case = _case_from_corruption(
                result=outcome,
                slice_=cell.slice,
                seed=seed,
                family=family,
                screens=screens,
                generator_version=generator_version,
            )
            cases.append(case)
            deltas.append((case.case_id, outcome.changes))

    return _CellOutcome(
        cases=tuple(cases),
        deltas=tuple(deltas),
        rejections=tuple(rejections),
        result=CellResult(
            slice=cell.slice,
            media_kind=cell.media_kind,
            problem_class=problem_class,
            intended=cell.intended,
            achievable=len(cases),
            candidates=len(in_scope),
            ticketed=len(ticketed),
            attempted=attempted,
            window=window if cell.intended else 0,
            capped_away=len(in_scope) - len(ticketed),
            rejected=sum(1 for row in rejections if row.applicable),
            not_applicable=not_applicable,
            excluded_away=excluded_away,
        ),
    )


def _should_not_touch_cell(
    *,
    cell: composition_module.Cell,
    seed: int,
    candidates: Sequence[Candidate],
    tickets: Mapping[str, frozenset[str]],
    screens: Mapping[str, screen_module.ItemScreen],
    excluded: Sequence[export_module.Family],
) -> _CellOutcome:
    """The same loop with the corruption step skipped.

    Eligibility is the clean screen's own verdict, `GUARDED`, which already means
    every applicable predicate passed and at least `MIN_APPLICABLE_CHECKS` were
    applicable. An item the screen calls `failed` is a 0.9 curated candidate instead
    -- `screen.Candidate` is exactly that hand-off -- and an `insufficient` one is
    neither.
    """

    def eligible(family: export_module.Family) -> bool:
        return (
            family.root.media_kind is cell.media_kind
            and screens[str(family.root.item_id)].verdict is screen_module.Verdict.GUARDED
        )

    in_scope = [candidate for candidate in candidates if eligible(candidate.family)]
    ticketed = [
        candidate
        for candidate in in_scope
        if NO_ACTION_TICKET in tickets.get(candidate.subject, frozenset())
    ]
    ranked = _ranked(seed, ticketed)

    cases = tuple(
        _case_from_screen(
            candidate=candidate,
            seed=seed,
            screen=screens[str(candidate.family.root.item_id)],
        )
        for candidate in ranked[: cell.intended]
    )
    return _CellOutcome(
        cases=cases,
        # An empty delta rather than no entry: 0.7's `for_case` then finds a delta
        # for every `case_id` and needs no special case for a world that is simply
        # the clean export.
        deltas=tuple((case.case_id, ()) for case in cases),
        rejections=(),
        result=CellResult(
            slice=cell.slice,
            media_kind=cell.media_kind,
            problem_class=None,
            intended=cell.intended,
            achievable=len(cases),
            candidates=len(in_scope),
            ticketed=len(ticketed),
            attempted=len(cases),
            window=len(ticketed),
            capped_away=len(in_scope) - len(ticketed),
            rejected=0,
            not_applicable=0,
            excluded_away=sum(1 for family in excluded if eligible(family)),
        ),
    )


def _curated_cell(
    *, cell: composition_module.Cell, pool: Sequence[truth_module.Case]
) -> _CellOutcome:
    matching = sorted(
        (
            case
            for case in pool
            if case.slice is cell.slice
            and case.media_kind is cell.media_kind
            and case.problem_class == cell.problem_class
        ),
        key=lambda case: case.case_id,
    )
    kept = tuple(matching[: cell.intended])
    return _CellOutcome(
        cases=kept,
        deltas=tuple((case.case_id, ()) for case in kept),
        rejections=(),
        result=CellResult(
            slice=cell.slice,
            media_kind=cell.media_kind,
            problem_class=cell.problem_class,
            intended=cell.intended,
            achievable=len(kept),
            candidates=len(matching),
            ticketed=len(matching),
            attempted=len(matching),
            window=len(matching),
            capped_away=0,
            rejected=0,
            not_applicable=0,
            # House rule 12: if it drops, it says so.
            surplus_dropped=len(matching) - len(kept),
        ),
    )


def _deficit_reason(
    cell: composition_module.Cell, result: CellResult
) -> tuple[composition_module.DeficitReason, str]:
    """Which of five reasons accounts for the largest part of a cell's shortfall.

    Attributed rather than ordered by preference: a cell short by ten because the cap
    turned away nine candidates and one attempt was refused should not read as
    `rejected`. The whole breakdown travels in `detail`, so the row's single reason
    is a summary rather than the only fact.
    """
    reason = composition_module.DeficitReason
    # Curated first. A curated slice never uses a corruption function, so whether
    # one is registered says nothing about why `real.toml` holds no case for this
    # cell. Checked in the other order -- as first written -- nine curated cells in
    # the fixture dataset reported `not_implemented` and pointed at step 1.1, when
    # the work waiting on them is step 0.9's labelling.
    if cell.slice in composition_module.CURATED:
        return (
            reason.NOT_CURATED,
            f"the {cell.slice} slice is human-curated and holds "
            f"{result.candidates} case(s) for this cell; step 0.9 labels it",
        )
    if cell.problem_class is not None and cell.problem_class not in CORRUPTION_TABLE:
        return reason.NOT_IMPLEMENTED, UNSYNTHESIZABLE_REASON.get(
            cell.problem_class, "no corruption function is registered"
        )
    gap = cell.intended - result.achievable
    # `capped` covers both ways a subject is withheld from a cell: the per-subject
    # cap, and exclusion as non-unique (step plan §4.5, Decision 5).
    withheld = result.capped_away + result.excluded_away
    buckets: list[tuple[int, composition_module.DeficitReason]] = [
        (withheld, reason.CAPPED),
        (result.rejected, reason.REJECTED),
        (
            result.not_applicable + max(gap - withheld - result.rejected, 0),
            reason.NO_CANDIDATES,
        ),
    ]
    ordered = sorted(
        buckets, key=lambda pair: (-pair[0], list(composition_module.DeficitReason).index(pair[1]))
    )
    detail = (
        f"{result.candidates} famil(y/ies) in scope, {result.ticketed} holding a ticket "
        f"({result.capped_away} turned away by the cap of {MAX_CASES_PER_SUBJECT}), "
        f"{result.excluded_away} excluded as non-unique subjects, "
        f"{result.attempted} attempted within a window of {result.window}, "
        f"{result.rejected} rejected, {result.not_applicable} never candidates"
    )
    return ordered[0][1], detail


def generate(
    *,
    manifest: export_module.Manifest,
    items: Sequence[NormalizedItem],
    roots: Sequence[ItemStub],
    screen: screen_module.Screen,
    composition: composition_module.Composition,
    curated: Mapping[truth_module.Slice, curated_module.CuratedFile],
    count: int,
    seed: int,
    generator_version: str = __version__,
) -> GenerateResult:
    """Build a dataset. Pure: no I/O, no clock, no network."""
    cells = composition_module.resolve(composition, count)
    families = group_families(items)
    candidates, excluded = index_subjects(families)
    by_id = {str(item.item_id): item for item in items}
    screens = {row.item_id: row for row in screen.items}
    guarded = frozenset(
        row.item_id for row in screen.items if row.verdict is screen_module.Verdict.GUARDED
    )
    tickets = issue_tickets(seed, candidates, guarded)

    family_of: dict[str, export_module.Family] = {}
    for family in families:
        for record in family.records:
            family_of[str(record.item_id)] = family
    # Kept so each cell can count what the exclusion cost it, not only the dataset.
    excluded_families = tuple(family_of[root] for row in excluded for root in row.root_ids)

    curated_pool: list[truth_module.Case] = []
    for slice_, file in sorted(curated.items()):
        for case in file.cases:
            curated_pool.append(
                _case_from_curated(
                    case=case,
                    slice_=slice_,
                    seed=seed,
                    by_id=by_id,
                    family_of=family_of,
                )
            )
    # Asserted on the whole pool, before any cell takes its target. Checked only on
    # what the cells keep -- as first written -- two copies of one label in a cell
    # whose target is 1 lost the second copy as surplus, and the duplicate went
    # unnoticed.
    _assert_unique(curated_pool)

    outcomes: list[_CellOutcome] = []
    for cell in cells:
        if cell.slice is truth_module.Slice.SHOULD_NOT_TOUCH:
            outcomes.append(
                _should_not_touch_cell(
                    cell=cell,
                    seed=seed,
                    candidates=candidates,
                    tickets=tickets,
                    screens=screens,
                    excluded=excluded_families,
                )
            )
        elif cell.slice in composition_module.CURATED:
            outcomes.append(_curated_cell(cell=cell, pool=curated_pool))
        else:
            outcomes.append(
                _synthetic_cell(
                    cell=cell,
                    seed=seed,
                    export_id=manifest.export_id,
                    candidates=candidates,
                    tickets=tickets,
                    by_id=by_id,
                    roots=roots,
                    screens=screens,
                    generator_version=generator_version,
                    excluded=excluded_families,
                )
            )

    cases = [case for outcome in outcomes for case in outcome.cases]
    _assert_unique(cases)
    cases.sort(key=lambda case: case.case_id)

    achieved = {
        (row.result.slice, row.result.media_kind, row.result.problem_class): row.result.achievable
        for row in outcomes
    }
    reasons = {
        (row.result.slice, row.result.media_kind, row.result.problem_class): _deficit_reason(
            cell, row.result
        )
        for cell, row in zip(cells, outcomes, strict=True)
    }

    subjects = {str(case.subject_key) for case in cases}
    dataset = Dataset(
        shelfwarden_version=__version__,
        generator_version=generator_version,
        dataset_id=dataset_id(seed, manifest.items_sha256, generator_version),
        lineage_id=lineage_id(manifest, generator_version, truth_module.SCHEMA_VERSION),
        composition_id=composition.composition_id,
        seed=seed,
        count=count,
        max_cases_per_subject=MAX_CASES_PER_SUBJECT,
        source_export=truth_module.SourceExport(
            export_id=manifest.export_id,
            items_sha256=manifest.items_sha256,
            roots_sha256=manifest.roots_sha256,
        ),
        screen=truth_module.ScreenRef(
            items_sha256=screen.source.items_sha256,
            schema_version=screen.schema_version,
            authority=screen.authority,
            min_applicable_checks=screen.min_applicable_checks,
        ),
        counts=DatasetCounts(
            cases=len(cases),
            subjects_covered=len(subjects),
            by_slice=_tally(str(case.slice) for case in cases),
            by_media_kind=_tally(str(case.media_kind) for case in cases),
            by_problem_class=_tally(
                str(case.problem_class) for case in cases if case.problem_class is not None
            ),
        ),
        cells=tuple(row.result for row in outcomes),
        deficits=composition_module.deficits(cells, achieved, reasons),
        excluded_subjects=excluded,
    )
    truth = truth_module.TruthFile(
        dataset_id=dataset.dataset_id,
        lineage_id=dataset.lineage_id,
        seed=seed,
        generator_version=generator_version,
        source_export=dataset.source_export,
        screen=dataset.screen,
        cases=tuple(cases),
    )
    deltas = tuple(
        sorted(
            (pair for outcome in outcomes for pair in outcome.deltas),
            key=lambda pair: pair[0],
        )
    )
    return GenerateResult(
        dataset=dataset,
        truth=truth,
        deltas=deltas,
        rejections=tuple(row for outcome in outcomes for row in outcome.rejections),
    )


def _tally(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _assert_unique(cases: Sequence[truth_module.Case]) -> None:
    """A duplicate `case_id` raises and names both cases.

    Never repaired by appending an ordinal: a disambiguating suffix is positional
    identity smuggled back in. Subject uniqueness is a selection precondition
    (`index_subjects`) and this is the assertion that the precondition held -- a
    generator that silently disambiguates is a generator whose ids are positional
    again.
    """
    seen: dict[str, truth_module.Case] = {}
    for case in cases:
        other = seen.get(case.case_id)
        if other is not None:
            raise GenerateError(
                f"two cases share {case.case_id}: "
                f"({other.slice}, {other.problem_class}, {other.media_kind}, "
                f"{other.subject_key}, {other.corruption_variant}) and "
                f"({case.slice}, {case.problem_class}, {case.media_kind}, "
                f"{case.subject_key}, {case.corruption_variant}). Case identity is "
                "semantic; an ordinal suffix would make it positional again."
            )
        seen[case.case_id] = case


# -- writing --------------------------------------------------------------


def render_dataset(dataset: Dataset) -> bytes:
    return canonical_json(dataset.model_dump(mode="json", exclude_none=True))


def render_deltas(deltas: Sequence[tuple[str, tuple]]) -> bytes:
    """One case's delta per line, keyed by `case_id` and ordered by it.

    Kept out of `truth.json` so the truth file stays readable and 0.7 can stream one
    case's world without parsing every case's ground truth.
    """
    return b"".join(
        canonical_json(
            {
                "case_id": case_id,
                "changes": [change.model_dump(mode="json") for change in changes],
            }
        )
        + b"\n"
        for case_id, changes in deltas
    )


def run_generate(
    export_directory: Path,
    out: Path,
    *,
    count: int,
    seed: int,
    composition_path: Path = composition_module.COMPOSITION_FILE,
    curated_root: Path = curated_module.CURATED_ROOT,
    generator_version: str = __version__,
) -> GenerateResult:
    """Read an export, build a dataset, write the five artifacts. Atomic."""
    manifest, items, roots = read_export_with_population(export_directory)
    screen = screen_module.build_screen(manifest, items, roots)
    composition = composition_module.load_composition(composition_path)
    curated = {
        slice_: curated_module.load_curated(slice_, curated_root)
        for slice_ in sorted(curated_module.CURATED_FILES)
    }
    result = generate(
        manifest=manifest,
        items=items,
        roots=roots,
        screen=screen,
        composition=composition,
        curated=curated,
        count=count,
        seed=seed,
        generator_version=generator_version,
    )
    export_module.write_atomically(
        out,
        {
            DATASET_FILE: render_dataset(result.dataset),
            TRUTH_FILE: truth_module.render_truth(result.truth),
            DELTAS_FILE: render_deltas(result.deltas),
            REJECTED_FILE: render_rejected(result.rejections),
            REPORT_FILE: render_markdown(result).encode("utf-8"),
        },
    )
    return result


def default_directory(dataset: str, base: Path = DEFAULT_DATASET_ROOT) -> Path:
    return base / dataset


# -- the report a human reads ---------------------------------------------


def _table(headers: tuple[str, ...], rows) -> list[str]:
    materialized = list(rows)
    if not materialized:
        return ["_(none)_", ""]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(row) + " |" for row in materialized]
    lines.append("")
    return lines


def render_markdown(result: GenerateResult) -> str:
    dataset = result.dataset
    counts = dataset.counts
    intended = sum(cell.intended for cell in dataset.cells)
    lines = [
        "# Eval dataset",
        "",
        f"`{dataset.dataset_id}` · lineage `{dataset.lineage_id}` · "
        f"composition `{dataset.composition_id}` · seed `{dataset.seed}`",
        "",
        f"**{counts.cases} case(s)** of {intended} intended, over "
        f"{counts.subjects_covered} subject(s). Export `{dataset.source_export.export_id}`, "
        f"screen `{dataset.screen.items_sha256[:12]}` (authority "
        f"`{dataset.screen.authority}`).",
        "",
        "`subjects_covered` is the number to read beside `cases`: one film can be the",
        "subject of several cases, so a pass rate over cases over-weights whichever items",
        f"happen to be corruptible. A subject hosts at most {dataset.max_cases_per_subject} "
        "case(s).",
        "",
        "## Composition",
        "",
    ]
    lines += _table(
        ("slice", "media", "class", "intended", "achieved", "gap"),
        [
            (
                str(cell.slice),
                str(cell.media_kind),
                str(cell.problem_class) if cell.problem_class else "—",
                str(cell.intended),
                str(cell.achievable),
                str(cell.intended - cell.achievable) if cell.intended else "0",
            )
            for cell in dataset.cells
            if cell.intended or cell.achievable
        ],
    )

    lines += [
        "## Deficits",
        "",
        _WHY,
        "",
    ]
    lines += _table(
        ("slice", "media", "class", "short by", "reason", "detail"),
        [
            (
                str(row.slice),
                str(row.media_kind),
                str(row.problem_class) if row.problem_class else "—",
                str(row.intended - row.achievable),
                f"`{row.reason}`",
                row.detail or "",
            )
            for row in dataset.deficits
        ],
    )

    by_reason = _tally(str(row.reason) for row in dataset.deficits)
    lines += _table(
        ("reason", "cells"),
        [(f"`{reason}`", str(number)) for reason, number in by_reason.items()],
    )

    lines += ["## Cases per slice, media kind, and class", ""]
    lines += _table(
        ("slice", "cases"), [(name, str(number)) for name, number in counts.by_slice.items()]
    )
    lines += _table(
        ("media kind", "cases"),
        [(name, str(number)) for name, number in counts.by_media_kind.items()],
    )
    lines += _table(
        ("problem class", "cases"),
        [(name, str(number)) for name, number in counts.by_problem_class.items()],
    )

    if dataset.excluded_subjects:
        lines += [
            "## Subjects excluded as non-unique",
            "",
            "Two families sharing a subject key would produce one `case_id` for two cases,",
            "and a disambiguating suffix would make case identity positional again. The",
            "usual cause is a genuine duplicate pair: two entries for one work carry the",
            "same guid, which is the condition `duplicate_quality` exists to describe.",
            "",
        ]
        shown = dataset.excluded_subjects[:EXAMPLE_CAP]
        lines += _table(
            ("subject", "families"),
            [(f"`{row.subject_key}`", ", ".join(row.root_ids)) for row in shown],
        )
        if len(dataset.excluded_subjects) > len(shown):
            lines += [
                f"…and {len(dataset.excluded_subjects) - len(shown)} more in `{DATASET_FILE}`.",
                "",
            ]

    dropped = [cell for cell in dataset.cells if cell.surplus_dropped]
    if dropped:
        lines += [
            "## Curated cases beyond their cell's target",
            "",
            "Counted rather than silently discarded (house rule 12). Raise the cell's share",
            "in `composition.toml` or `--count` to take them in.",
            "",
        ]
        lines += _table(
            ("slice", "media", "class", "dropped"),
            [
                (
                    str(cell.slice),
                    str(cell.media_kind),
                    str(cell.problem_class) if cell.problem_class else "—",
                    str(cell.surplus_dropped),
                )
                for cell in dropped
            ],
        )
    return "\n".join(lines) + "\n"


_WHY = (
    "`not_implemented` waits on step 1.1 -- the class needs an external record as an "
    "ingredient or as a witness. `not_curated` waits on step 0.9: the real and "
    "ambiguous slices are labelled by hand, and an empty file is a fact about the work "
    "queue rather than about the library. `no_candidates` is a fact about the library "
    "(no remake pairs, no edition markers on disk). `rejected` is a fact about this "
    "harness -- a case was built and then refused, almost always because nothing could "
    "witness it -- and is the only reason here that is a bug. `capped` means a subject "
    "already held its share of cases; a dataset that quietly re-drew from a class with "
    "supply left would report coverage it does not have."
)


# -- `python -m shelfwarden.evals.generate` -------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """The entry point the Phase 0 gate names.

    The export directory is a positional argument, as it is on `shelfwarden
    corrupt`; the gate line in `roadmap.md` and `CLAUDE.md` omits it and means the
    same command.
    """
    parser = argparse.ArgumentParser(
        prog="python -m shelfwarden.evals.generate",
        description="Generate a labelled eval dataset from an export.",
    )
    parser.add_argument(
        "export", type=Path, help="Export directory written by `shelfwarden export`."
    )
    parser.add_argument("--count", type=int, default=200, help="Cases to aim for.")
    parser.add_argument(
        "--seed", type=int, default=export_module.DEFAULT_SEED, help="Seed for subject ranking."
    )
    parser.add_argument(
        "--composition",
        type=Path,
        default=composition_module.COMPOSITION_FILE,
        help="Composition file. Defaults to composition.toml at the repository root.",
    )
    parser.add_argument(
        "--curated",
        type=Path,
        default=curated_module.CURATED_ROOT,
        help="Directory holding real.toml and ambiguous.toml.",
    )
    parser.add_argument("--out", type=Path, default=None, help="Where to write the dataset.")
    args = parser.parse_args(argv)

    try:
        manifest = export_module.load_manifest(args.export)
    except FileNotFoundError:
        print(
            f"{args.export} is not an export directory: no {export_module.MANIFEST_FILE}. "
            "Point this at a directory written by `shelfwarden export`.",
            file=sys.stderr,
        )
        return 1

    out = args.out or default_directory(dataset_id(args.seed, manifest.items_sha256, __version__))
    try:
        result = run_generate(
            args.export,
            out,
            count=args.count,
            seed=args.seed,
            composition_path=args.composition,
            curated_root=args.curated,
        )
    except (
        GenerateError,
        composition_module.CompositionError,
        curated_module.CuratedError,
        screen_module.ScreenError,
        truth_module.TruthError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(describe(result, out))
    return 0


def describe(result: GenerateResult, out: Path) -> str:
    dataset = result.dataset
    intended = sum(cell.intended for cell in dataset.cells)
    short = [row for row in dataset.deficits]
    lines = [
        f"Wrote {out}",
        f"{dataset.counts.cases} case(s) of {intended} intended over "
        f"{dataset.counts.subjects_covered} subject(s); dataset {dataset.dataset_id}, "
        f"lineage {dataset.lineage_id}",
    ]
    if short:
        by_reason = _tally(str(row.reason) for row in short)
        lines.append(
            "  short in "
            + f"{len(short)} cell(s): "
            + ", ".join(f"{reason} x{number}" for reason, number in by_reason.items())
            + f" — see {out / REPORT_FILE}"
        )
    if dataset.excluded_subjects:
        lines.append(
            f"  {len(dataset.excluded_subjects)} subject(s) excluded as non-unique; a "
            "collision would give two cases one id"
        )
    return "\n".join(lines)


__all__ = [
    "DATASET_FILE",
    "DEFAULT_DATASET_ROOT",
    "DELTAS_FILE",
    "MAX_CASES_PER_SUBJECT",
    "MIN_ATTEMPT_WINDOW",
    "NO_ACTION_TICKET",
    "OVERSAMPLE",
    "REJECTED_FILE",
    "REPORT_FILE",
    "SCHEMA_VERSION",
    "TRUTH_FILE",
    "Candidate",
    "CellResult",
    "Dataset",
    "DatasetCounts",
    "ExcludedSubject",
    "GenerateError",
    "GenerateResult",
    "candidate_labels",
    "dataset_id",
    "default_directory",
    "describe",
    "generate",
    "index_subjects",
    "issue_tickets",
    "lineage_id",
    "main",
    "render_dataset",
    "render_deltas",
    "render_markdown",
    "run_generate",
    "ticket_rank",
]


if __name__ == "__main__":  # pragma: no cover -- exercised as a subprocess
    raise SystemExit(main())
