"""The truth file: what a case expects, derived from its delta and its witness.

Step 0.6.3's gate is `TestTheGate`: for every class that carries one, a derived
postcondition holds on the ground truth and fails on the corrupted world, and the
three relation classes carry a resolution built from their witness. Everything
else defends a way an expectation could be satisfied without doing the work:

* **Defect 1** -- `unexpected: fail` is a default on the model, so no slice can
  be generated blind to fabricated findings.
* **Defect 2** -- an escalate case that silence could satisfy is refused when it
  is parsed, rather than left for the scorer to notice.
* **Finding 5, one layer up** -- a class that cannot describe an item is never a
  known problem on it. Otherwise the false positive the screen's third bucket
  exists to catch would be excused here instead.

`truth.py` has no postcondition evaluator; scoring is 0.8's. `_holds` below is a
reference reading of the five predicates in the step plan's §4.4, and 0.8 should
replace it with the scorer's own so that the two cannot disagree.
"""

import functools
import inspect

import pytest
from pydantic import TypeAdapter, ValidationError

from shelfwarden.canonical import canonical_json
from shelfwarden.compare import fold_text
from shelfwarden.evals.corrupt.context import CorruptionContext, group_families, subject_key
from shelfwarden.evals.corrupt.registry import CORRUPTION_TABLE, attempt
from shelfwarden.evals.corrupt.reverse import apply_changes
from shelfwarden.evals.corrupt.run import run_corruptions
from shelfwarden.evals.corrupt.witness import MIN_AMBIGUITY_CANDIDATES, WitnessKind
from shelfwarden.evals.screen import (
    GUARD_TABLE,
    CheckStatus,
    NullAuthority,
    Predicate,
    ScreenContext,
    Verdict,
    screen_item,
)
from shelfwarden.evals.screen import (
    SCHEMA_VERSION as SCREEN_SCHEMA_VERSION,
)
from shelfwarden.evals.truth import (
    GUIDS_POINTER,
    HARD_FIELDS,
    MUST_NOT_CHANGE_FLOOR,
    SOFT_FIELDS,
    Case,
    EscalateExpectation,
    Expect,
    Expectation,
    NoActionExpectation,
    Op,
    Provenance,
    RepairExpectation,
    ScreenRef,
    Slice,
    SourceExport,
    SubjectKeyRecord,
    Tier,
    TruthError,
    TruthFile,
    Unexpected,
    already_failing_classes,
    case_id,
    corruption_fingerprint,
    external_id_label,
    field_tier,
    load_truth,
    no_action_expectation,
    render_truth,
    repair_expectation,
    required_finding,
    run_group,
)
from shelfwarden.models.finding import ProblemClass, describes
from shelfwarden.models.ids import parse_guids
from shelfwarden.models.item import FilePart, MediaKind, dump_item, with_changes
from shelfwarden.pointer import matches, resolve

from .conftest import _movie
from .corrupt.conftest import survey_inputs, world_for

IMPLEMENTED = sorted(CORRUPTION_TABLE, key=str)
RELATION_CLASSES = sorted(
    (pc for pc in CORRUPTION_TABLE if CORRUPTION_TABLE[pc].witness_kind is WitnessKind.RELATION),
    key=str,
)


# -- fixture cases ----------------------------------------------------------


@functools.cache
def _cases(problem_class):
    """Every fixture case of this class: `(result, ground_truth, corrupted, finding)`."""
    items = world_for(problem_class)
    payload, roots = survey_inputs(items)
    run = run_corruptions(
        export_id="test-export", items=payload, roots=roots, seed=1518, classes=[problem_class]
    )
    assert run.results, f"the fixture world supplies no {problem_class} case"
    families = {str(family.root.item_id): family for family in group_families(payload)}
    cases = []
    for result in run.results:
        ground_truth = families[result.root_id].records
        finding = required_finding(
            problem_class=problem_class,
            witness=result.witness,
            changes=result.changes,
            ground_truth=ground_truth,
        )
        cases.append((result, ground_truth, apply_changes(ground_truth, result.changes), finding))
    return tuple(cases)


@functools.cache
def _clean_screens(problem_class):
    """Every item of this class's fixture world, screened clean. Keyed by id."""
    payload, roots = survey_inputs(world_for(problem_class))
    ctx = ScreenContext.build(
        export_id="test-export", items=payload, roots=roots, authority=NullAuthority()
    )
    return {str(item.item_id): screen_item(ctx, item) for item in payload}


# -- a reference reading of the postcondition vocabulary --------------------


def _world(items):
    return {str(item.item_id): dump_item(item) for item in items}


def _holds(expect, document, pointer):
    """The five predicates of the step plan's §4.4, read literally.

    `document` is `None` when the item is not in the world at all, which no
    predicate accepts: every one of them is a statement about an item that exists.
    """
    if document is None:
        return False
    value = resolve(document, pointer)
    match expect.op:
        case Op.EQUALS:
            return canonical_json(value) == canonical_json(expect.value)
        case Op.NORMALIZED_EQUALS:
            return isinstance(value, str) and fold_text(value) == fold_text(expect.value)
        case Op.CONTAINS:
            labels = {external_id_label(guid) for guid in value}
            return set(expect.value) <= labels and not labels & set(expect.excludes)
        case Op.ABSENT:
            return value is None
        case Op.NON_EMPTY:
            return value not in (None, "", [], {})


def _entries(postcondition):
    return sorted(
        (item_id, pointer, expect)
        for item_id, fields in postcondition.items()
        for pointer, expect in fields.items()
    )


def _violations(postcondition, world):
    return [
        (item_id, pointer)
        for item_id, pointer, expect in _entries(postcondition)
        if not _holds(expect, world.get(item_id), pointer)
    ]


def _guid(namespace, value):
    """An `ExternalId` as `dump_item` writes it."""
    return {"namespace": namespace, "value": value, "raw": "", "season": None, "episode": None}


def test_the_reference_evaluator_is_not_vacuous():
    """Each predicate says no to something, or the gate tests below prove nothing."""
    document = {"title": "Amélie", "year": 2001, "series": None, "guids": [], "tags": ["x"]}
    assert _holds(Expect(op=Op.NORMALIZED_EQUALS, value="AMÉLIE"), document, "/title")
    assert not _holds(Expect(op=Op.NORMALIZED_EQUALS, value="Amelia"), document, "/title")
    assert _holds(Expect(op=Op.EQUALS, value=2001), document, "/year")
    assert not _holds(Expect(op=Op.EQUALS, value=2001.0 + 1), document, "/year")
    assert _holds(Expect(op=Op.ABSENT), document, "/series")
    assert not _holds(Expect(op=Op.ABSENT), document, "/title")
    assert not _holds(Expect(op=Op.CONTAINS, value=["tmdb://1"]), document, "/guids")
    matched = {"guids": [_guid("tmdb", "1"), _guid("imdb", "tt9")]}
    assert _holds(Expect(op=Op.CONTAINS, value=["tmdb://1"]), matched, "/guids")
    assert not _holds(
        Expect(op=Op.CONTAINS, value=["tmdb://1"], excludes=["imdb://tt9"]), matched, "/guids"
    )
    assert _holds(Expect(op=Op.NON_EMPTY), document, "/tags")
    assert not _holds(Expect(op=Op.NON_EMPTY), document, "/guids")
    assert not _holds(Expect(op=Op.NON_EMPTY), None, "")


# -- the gate ---------------------------------------------------------------


class TestTheGate:
    def test_ten_classes_carry_a_postcondition_and_nine_gate_on_one(self):
        """Measured, not assumed. `duplicate_quality`'s delta is a pure ADD, so it
        carries none. `author_name_variant` carries one, but every field its delta
        rewrites on a surviving item is derived by Plex after the merge
        (`/album_count`, `/parent`, `/parent_title`), so it is all soft and the
        class is gated by its resolution alone."""
        carrying, gating = set(), set()
        for problem_class in IMPLEMENTED:
            for *_, finding in _cases(problem_class):
                if finding.postcondition or finding.soft_postcondition:
                    carrying.add(problem_class)
                if finding.postcondition:
                    gating.add(problem_class)
        assert carrying == set(IMPLEMENTED) - {ProblemClass.DUPLICATE_QUALITY}
        assert gating == carrying - {ProblemClass.AUTHOR_NAME_VARIANT}

    @pytest.mark.parametrize(
        "problem_class",
        [pc for pc in IMPLEMENTED if pc is not ProblemClass.DUPLICATE_QUALITY],
    )
    def test_a_derived_postcondition_holds_on_truth_and_fails_on_the_corruption(
        self, problem_class
    ):
        """Per predicate, both tiers. Holding on the truth means a correct repair can
        pass; failing on the corruption means no predicate is a gate an agent passes
        by doing nothing -- the leniency `normalized_equals` would hide if a
        corruption ever changed only case."""
        for result, ground_truth, corrupted, finding in _cases(problem_class):
            for postcondition in (finding.postcondition, finding.soft_postcondition):
                everything = [(item_id, pointer) for item_id, pointer, _ in _entries(postcondition)]
                assert _violations(postcondition, _world(ground_truth)) == [], result.root_id
                assert _violations(postcondition, _world(corrupted)) == everything, result.root_id

    @pytest.mark.parametrize("problem_class", RELATION_CLASSES)
    def test_a_relation_case_carries_a_resolution(self, problem_class):
        for result, _, _, finding in _cases(problem_class):
            resolution = finding.resolution
            assert resolution is not None
            assert resolution.relation == result.witness.relation
            assert set(resolution.item_ids) == set(result.witness.subjects)
            assert len(set(resolution.item_ids)) == len(resolution.item_ids)
            assert len(resolution.item_ids) >= MIN_AMBIGUITY_CANDIDATES
            assert finding.item_ids == resolution.item_ids

    def test_there_are_exactly_three_relation_classes(self):
        assert (
            sorted(
                [
                    ProblemClass.DUPLICATE_QUALITY,
                    ProblemClass.AUTHOR_NAME_VARIANT,
                    ProblemClass.MULTI_FILE_SPLIT,
                ],
                key=str,
            )
            == RELATION_CLASSES
        )

    @pytest.mark.parametrize(
        "problem_class", [pc for pc in IMPLEMENTED if pc not in RELATION_CLASSES]
    )
    def test_a_value_case_carries_no_resolution(self, problem_class):
        for *_, finding in _cases(problem_class):
            assert finding.resolution is None

    def test_duplicate_quality_carries_a_resolution_and_no_postcondition(self):
        """Its delta is a pure ADD: no surviving item's field changed, and inventing a
        postcondition would describe the item the repair makes disappear."""
        for *_, finding in _cases(ProblemClass.DUPLICATE_QUALITY):
            assert finding.resolution is not None
            assert finding.postcondition == {}
            assert finding.soft_postcondition == {}


class TestGuids:
    """`/guids` requires the ground truth's ids **and forbids the ones the
    corruption injected**.

    Found in step 0.6 by the reference evaluator. `contains` alone asked only that
    the right ids be present. On `fake:1:106`, whose ground truth has no ids, that
    was `contains []` -- true of every world, the corrupted one included. And on any
    `wrong_match` case, a steward that added the right id beside the donor's
    passed while still carrying the wrong match, which Plex re-derives metadata
    from on the next refresh.
    """

    def test_it_requires_the_truth_s_ids_and_forbids_exactly_the_injected_ones(self):
        checked = 0
        for problem_class in IMPLEMENTED:
            for result, _, _, finding in _cases(problem_class):
                for change in result.changes:
                    for field in change.fields:
                        if field.path != GUIDS_POINTER:
                            continue
                        before = {external_id_label(guid) for guid in field.before}
                        after = {external_id_label(guid) for guid in field.after}
                        expect = finding.postcondition[change.item_id][GUIDS_POINTER]
                        assert set(expect.value) == before
                        assert set(expect.excludes) == after - before
                        checked += 1
        assert checked

    def test_a_ground_truth_with_no_ids_still_gates(self):
        """The vacuous case: `contains []` alone passed the corrupted world."""
        _, _, corrupted, finding = next(
            case for case in _cases(ProblemClass.WRONG_MATCH) if case[0].root_id == "fake:1:106"
        )
        expect = finding.postcondition["fake:1:106"][GUIDS_POINTER]
        assert expect.value == []
        assert expect.excludes
        assert ("fake:1:106", GUIDS_POINTER) in _violations(
            finding.postcondition, _world(corrupted)
        )

    def test_a_repair_that_keeps_the_donor_id_fails(self):
        """The non-vacuous case: the right id added back, the donor's left beside it."""
        checked = 0
        for _, ground_truth, corrupted, finding in _cases(ProblemClass.WRONG_MATCH):
            (item_id,) = finding.item_ids
            truth = next(item for item in ground_truth if str(item.item_id) == item_id)
            wrong = next(item for item in corrupted if str(item.item_id) == item_id)
            if not truth.guids:
                continue
            both = with_changes(truth, {"guids": (*truth.guids, *wrong.guids)})
            world = _world([both if str(i.item_id) == item_id else i for i in ground_truth])
            assert (item_id, GUIDS_POINTER) in _violations(finding.postcondition, world)
            checked += 1
        assert checked

    def test_a_rematch_that_adds_a_correct_id_still_passes(self):
        """What `contains` was chosen over equality for. A modern-agent rematch of a
        legacy item gains a `plex://` id and often a `tmdb://` one; neither is the
        wrong match, and neither is a defect."""
        for _, ground_truth, _, finding in _cases(ProblemClass.WRONG_MATCH):
            (item_id,) = finding.item_ids
            truth = next(item for item in ground_truth if str(item.item_id) == item_id)
            extra = parse_guids(f"plex://movie/rematched-{truth.item_id.rating_key}", [])
            richer = with_changes(truth, {"guids": (*truth.guids, *extra)})
            world = _world([richer if str(i.item_id) == item_id else i for i in ground_truth])
            assert (item_id, GUIDS_POINTER) not in _violations(finding.postcondition, world)

    @pytest.mark.parametrize("op", [op for op in Op if op is not Op.CONTAINS])
    def test_excludes_is_refused_on_any_other_predicate(self, op):
        with pytest.raises(ValidationError, match="only `contains`"):
            Expect(op=op, value="x", excludes=["tmdb://1"])

    def test_an_id_both_required_and_excluded_is_refused(self):
        """No world satisfies it, so it could only ever score a correct repair wrong."""
        with pytest.raises(ValidationError, match="both required and excluded"):
            Expect(op=Op.CONTAINS, value=["tmdb://1"], excludes=["tmdb://1"])


class TestRelations:
    @pytest.mark.parametrize(
        "problem_class", [ProblemClass.AUTHOR_NAME_VARIANT, ProblemClass.MULTI_FILE_SPLIT]
    )
    def test_the_keeper_is_the_member_that_existed_before_the_corruption(self, problem_class):
        """One name is canonical and one file set is one book: merging into the
        minted item is the wrong answer, and the ground truth says which that is."""
        for _, ground_truth, _, finding in _cases(problem_class):
            existed = {str(item.item_id) for item in ground_truth}
            assert finding.resolution.keeper in existed
            assert set(finding.resolution.item_ids) - existed, "the other member was minted"

    def test_the_keeper_is_null_where_the_ground_truth_does_not_settle_it(self):
        """Pinned on the `resolution` variant specifically: it mints the clone at
        2160p against a 1080 original, so "keep the entry that already existed" would
        score a steward that keeps the better copy as wrong. The rule looks safe
        until you read the variant table."""
        items = world_for(ProblemClass.DUPLICATE_QUALITY)
        payload, roots = survey_inputs(items)
        family = next(f for f in group_families(payload) if str(f.root.item_id) == "fake:1:101")
        ctx = CorruptionContext.build(
            export_id="test-export",
            seed=1518,
            problem_class=ProblemClass.DUPLICATE_QUALITY,
            variant="resolution",
            root=family.root,
            subject=subject_key(family.records[0]),
            items={str(item.item_id): item for item in payload},
            roots=roots,
        )
        result = attempt(CORRUPTION_TABLE[ProblemClass.DUPLICATE_QUALITY], family, ctx)
        corrupted = apply_changes(family.records, result.changes)
        resolutions = {
            str(item.item_id): item.parts[0].video_resolution
            for item in corrupted
            if item.media_kind is MediaKind.MOVIE
        }
        assert sorted(resolutions.values()) == ["1080", "2160"], "the clone is the better copy"

        finding = required_finding(
            problem_class=ProblemClass.DUPLICATE_QUALITY,
            witness=result.witness,
            changes=result.changes,
            ground_truth=family.records,
        )
        assert finding.resolution.keeper is None
        assert set(finding.resolution.item_ids) == set(resolutions)

    def test_merging_a_split_book_does_not_excuse_the_mangled_title(self):
        """Step 0.6, Finding 3. A steward that merges the two entries and restores
        everything else, but leaves the pre-existing book titled `... CD1`, would pass
        on a resolution alone. The hard postcondition is what fails it."""
        for _, ground_truth, corrupted, finding in _cases(ProblemClass.MULTI_FILE_SPLIT):
            keeper = finding.resolution.keeper
            mangled = next(item for item in corrupted if str(item.item_id) == keeper).title
            merged = [
                with_changes(item, {"title": mangled}) if str(item.item_id) == keeper else item
                for item in ground_truth
            ]
            assert _violations(finding.postcondition, _world(merged)) == [(keeper, "/title")]


# -- the field tiers --------------------------------------------------------


class TestTiers:
    def test_every_delta_path_has_exactly_one_tier(self):
        """`field_tier` checks HARD first, so a path both tables matched would be
        silently hard. Measured over every change every corruption makes."""
        paths = {
            field.path
            for problem_class in IMPLEMENTED
            for result, *_ in _cases(problem_class)
            for change in result.changes
            for field in change.fields
        }
        assert paths
        for path in paths:
            hard = any(matches(selector, path) for selector in HARD_FIELDS)
            soft = any(matches(selector, path) for selector in SOFT_FIELDS)
            assert hard != soft, path
            assert field_tier(path) is (Tier.HARD if hard else Tier.SOFT)

    def test_a_path_in_neither_table_raises_rather_than_defaulting_to_hard(self):
        with pytest.raises(TruthError, match="neither HARD_FIELDS nor SOFT_FIELDS"):
            field_tier("/rating")

    def test_a_wildcard_is_not_a_change_path(self):
        with pytest.raises(TruthError, match="contains a wildcard"):
            field_tier("/parts/*/path")

    def test_parent_is_soft_while_parent_index_is_hard(self):
        """An agent repairs a misfiled episode by setting its season *number*; Plex
        re-derives the parent link on rescan. Gating the link would fail a correct
        repair for a reason the agent does not control."""
        assert field_tier("/parent") is Tier.SOFT
        assert field_tier("/parent_index") is Tier.HARD

    def test_a_file_path_is_soft_even_on_the_class_that_renames(self):
        """The truth holds a *suggested* filename, and a rename is Phase 3's."""
        assert field_tier("/parts/0/path") is Tier.SOFT


class TestMustNotChange:
    @pytest.mark.parametrize("problem_class", IMPLEMENTED)
    def test_must_not_change_never_forbids_the_repair(self, problem_class):
        """A repair writes back every path the delta touched, and satisfies every
        postcondition. A constraint on any of those would forbid the answer."""
        for result, _, _, finding in _cases(problem_class):
            required = {field.path for change in result.changes for field in change.fields}
            required |= {
                pointer
                for postcondition in (finding.postcondition, finding.soft_postcondition)
                for _, pointer, _ in _entries(postcondition)
            }
            for selector in finding.must_not_change:
                assert not any(matches(selector, path) for path in required), (selector, required)

    @pytest.mark.parametrize("problem_class", IMPLEMENTED)
    def test_the_file_floor_holds_wherever_the_delta_does_not_rename(self, problem_class):
        """Invariant 11, never delete a file. The floor drops only where the delta
        itself wrote a path -- `filename_unmatchable`, whose repair is the rename."""
        for result, _, _, finding in _cases(problem_class):
            renames = any(
                matches(floor, field.path)
                for floor in MUST_NOT_CHANGE_FLOOR
                for change in result.changes
                for field in change.fields
            )
            assert ("/parts/*/path" in finding.must_not_change) is not renames
            assert renames is (problem_class is ProblemClass.FILENAME_UNMATCHABLE)

    @pytest.mark.parametrize("problem_class", IMPLEMENTED)
    def test_no_location_is_constrained_twice(self, problem_class):
        """`/parts/0/path` beside `/parts/*/path` would have the scorer check one
        location twice and report one violation as two."""
        for *_, finding in _cases(problem_class):
            for selector in finding.must_not_change:
                for other in finding.must_not_change:
                    if other != selector and "*" not in other.split("/"):
                        assert not matches(selector, other), (selector, other)


# -- defects 1 and 2 --------------------------------------------------------


def _minimal(kind):
    """Each expectation model, built with nothing but what it requires."""
    if kind is RepairExpectation:
        return {"kind": "repair", "required_findings": []}
    if kind is NoActionExpectation:
        return {
            "kind": "no_action",
            "guarded_classes": [],
            "trivially_guarded_classes": [],
            "unguarded_classes": [],
            "verification": {},
        }
    return {"kind": "escalate"}


class TestUnexpectedIsAFailureByDefault:
    @pytest.mark.parametrize("model", [RepairExpectation, NoActionExpectation, EscalateExpectation])
    def test_unexpected_fail_is_the_default_on_every_expectation(self, model):
        """Defect 1: a default in code cannot be forgotten for one slice, which is how
        85% of the first draft's dataset came to be blind to fabricated findings.
        Every slice maps to one of these three -- synthetic and real to `repair`,
        should-not-touch to `no_action`, ambiguous to `escalate`."""
        parsed = TypeAdapter(Expectation).validate_python(_minimal(model))
        assert isinstance(parsed, model)
        assert parsed.unexpected is Unexpected.FAIL

    def test_the_builders_never_pass_it(self):
        result, ground_truth, _, _ = _cases(ProblemClass.WRONG_MATCH)[0]
        repair = repair_expectation(
            problem_class=ProblemClass.WRONG_MATCH,
            witness=result.witness,
            changes=result.changes,
            ground_truth=ground_truth,
        )
        assert repair.unexpected is Unexpected.FAIL
        screen = _clean_screens(ProblemClass.WRONG_MATCH)["fake:1:101"]
        assert no_action_expectation(screen).unexpected is Unexpected.FAIL


class TestSilenceIsNotEscalation:
    def test_an_escalate_case_demands_a_finding_by_default(self):
        expectation = EscalateExpectation()
        assert expectation.require_finding is True
        assert expectation.require_needs_human is True
        assert expectation.min_candidates == MIN_AMBIGUITY_CANDIDATES

    @pytest.mark.parametrize(
        "override",
        [
            {"require_finding": False},
            {"require_needs_human": False},
            {"min_candidates": MIN_AMBIGUITY_CANDIDATES - 1},
        ],
        ids=["no-finding", "no-needs-human", "one-candidate"],
    )
    def test_silence_does_not_satisfy_an_escalate_case(self, override):
        """Defect 2, invariant 10. A curated file is hand-written TOML, so an escalate
        case that an agent finding nothing -- or naming one candidate, which is not
        an ambiguity -- could pass is refused when it is read, not when it is
        scored."""
        with pytest.raises(ValidationError):
            EscalateExpectation(**override)


# -- known other problems ---------------------------------------------------


class TestKnownOtherProblems:
    def test_a_class_that_cannot_describe_the_item_is_never_a_known_problem_on_it(self):
        """Finding 5, one layer up. A film with a scene-release file fails
        `filename_matches_metadata`, which guards `filename_unmatchable` -- and also
        `absolute_vs_seasonal` and `episode_wrong_season`. Listing those as known
        problems on a film would excuse a TV-class false positive on it."""
        movie = _movie(
            "101",
            "Amélie",
            2001,
            parts=(FilePart(part_id="1", path="/media/Movies/xvid-abc123.avi", container="avi"),),
        )
        payload, roots = survey_inputs((movie,))
        ctx = ScreenContext.build(
            export_id="test-export", items=payload, roots=roots, authority=NullAuthority()
        )
        screen = screen_item(ctx, movie)
        assert Predicate.FILENAME_MATCHES_METADATA in screen.failing_predicates

        known = already_failing_classes([screen])
        assert ProblemClass.FILENAME_UNMATCHABLE in known
        assert ProblemClass.ABSOLUTE_VS_SEASONAL not in known
        assert ProblemClass.EPISODE_WRONG_SEASON not in known

    @pytest.mark.parametrize("problem_class", IMPLEMENTED)
    def test_every_known_problem_is_induced_or_demonstrated_on_the_clean_family(
        self, problem_class
    ):
        """The two mechanical sources and no third: the corruption's own `induced`,
        or a guard predicate that fails on a clean family item the class can
        describe. And the case's own class is required, never excused."""
        screens = _clean_screens(problem_class)
        for result, ground_truth, _, _ in _cases(problem_class):
            family = [screens[str(item.item_id)] for item in ground_truth]
            expectation = repair_expectation(
                problem_class=problem_class,
                witness=result.witness,
                changes=result.changes,
                ground_truth=ground_truth,
                induced=result.induced,
                clean_screens=family,
            )
            assert problem_class not in expectation.known_other_problems
            for known in expectation.known_other_problems:
                demonstrated = any(
                    describes(known, screen.media_kind)
                    and GUARD_TABLE[known] & set(screen.failing_predicates)
                    for screen in family
                )
                assert known in result.induced or demonstrated, (result.root_id, known)


# -- should-not-touch -------------------------------------------------------


class TestNoAction:
    def test_it_is_the_screen_s_own_three_buckets_with_its_citations(self):
        screen = _clean_screens(ProblemClass.WRONG_MATCH)["fake:1:101"]
        assert screen.verdict is Verdict.GUARDED
        expectation = no_action_expectation(screen)
        assert expectation.guarded_classes == screen.guarded_classes
        assert expectation.trivially_guarded_classes == screen.trivially_guarded_classes
        assert expectation.unguarded_classes == screen.unguarded_classes

        decided = {
            (check.predicate, check.evidence_id, check.status)
            for check in screen.checks
            if check.status in (CheckStatus.PASS, CheckStatus.FAIL)
        }
        cited = {
            (check.predicate, check.evidence_id, check.result)
            for check in expectation.verification.checks
        }
        assert cited == decided
        assert all(check.evidence_id for check in expectation.verification.checks)


# -- identity ---------------------------------------------------------------


class TestIdentity:
    def test_the_case_id_digest_is_pinned(self):
        """Changing this recipe resets every case's history in the CI baseline. If
        this test fails, the change has to be deliberate."""
        assert (
            case_id(
                slice_=Slice.SYNTHETIC,
                problem_class=ProblemClass.WRONG_MATCH,
                media_kind=MediaKind.MOVIE,
                subject_key="external_id:imdb://tt0111161",
                corruption_variant="donor_same_section",
            )
            == "case-68dd357c8f49"
        )

    def test_a_should_not_touch_case_has_no_class_and_still_has_a_unique_id(self):
        """`problem_class` and `corruption_variant` enter the digest as `null` rather
        than being dropped, so one hash covers every slice and a should-not-touch
        case can never collide with a synthetic case on the same subject."""
        subject = "external_id:imdb://tt0111161"
        untouched = case_id(
            slice_=Slice.SHOULD_NOT_TOUCH,
            problem_class=None,
            media_kind=MediaKind.MOVIE,
            subject_key=subject,
            corruption_variant=None,
        )
        repaired = case_id(
            slice_=Slice.SYNTHETIC,
            problem_class=ProblemClass.WRONG_MATCH,
            media_kind=MediaKind.MOVIE,
            subject_key=subject,
            corruption_variant="donor_same_section",
        )
        assert untouched.startswith("case-") and len(untouched) == len("case-") + 12
        assert untouched != repaired

    def test_generator_version_moves_the_fingerprint_and_not_the_case_id(self):
        """In `case_id` it would reset the baseline on every version bump;
        `corruption_fingerprint` carries the signal into the diff's `changed` bucket."""
        assert "generator_version" not in inspect.signature(case_id).parameters
        result, *_ = _cases(ProblemClass.WRONG_MATCH)[0]
        assert corruption_fingerprint(result.changes, "0.1.0") != corruption_fingerprint(
            result.changes, "0.2.0"
        )

    def test_the_run_group_is_keyed_on_the_subject(self):
        """Not on the root's rating key, which moves on rescan."""
        assert run_group(1518, "external_id:tmdb://1") == run_group(1518, "external_id:tmdb://1")
        assert run_group(1518, "external_id:tmdb://1") != run_group(1518, "external_id:tmdb://2")


# -- the file ---------------------------------------------------------------


def _repair_case(problem_class):
    result, ground_truth, _, _ = _cases(problem_class)[0]
    kind, _, value = result.subject_key.partition(":")
    return Case(
        case_id=case_id(
            slice_=Slice.SYNTHETIC,
            problem_class=problem_class,
            media_kind=ground_truth[0].media_kind,
            subject_key=result.subject_key,
            corruption_variant=result.variant,
        ),
        slice=Slice.SYNTHETIC,
        run_group=run_group(1518, result.subject_key),
        problem_class=problem_class,
        media_kind=ground_truth[0].media_kind,
        subject_key=SubjectKeyRecord(kind=kind, value=value),
        corruption_variant=result.variant,
        corruption_fingerprint=corruption_fingerprint(result.changes, "0.1.0"),
        item_ids=tuple(sorted({change.item_id for change in result.changes})),
        expectation=repair_expectation(
            problem_class=problem_class,
            witness=result.witness,
            changes=result.changes,
            ground_truth=ground_truth,
            induced=result.induced,
        ),
        witness=result.witness,
        ground_truth=ground_truth,
        provenance=Provenance(method="synthetic"),
        collateral=result.collateral,
    )


def test_the_truth_file_round_trips_and_keeps_its_nulls():
    """`keeper: null` is a statement -- the ground truth does not settle which
    duplicate survives -- so rendering keeps nulls, and the discriminated union
    brings each expectation back as the model it was written as."""
    screen = _clean_screens(ProblemClass.WRONG_MATCH)["fake:1:101"]
    untouched = Case(
        case_id=case_id(
            slice_=Slice.SHOULD_NOT_TOUCH,
            problem_class=None,
            media_kind=MediaKind.MOVIE,
            subject_key="external_id:tmdb://101",
            corruption_variant=None,
        ),
        slice=Slice.SHOULD_NOT_TOUCH,
        run_group=run_group(1518, "external_id:tmdb://101"),
        problem_class=None,
        media_kind=MediaKind.MOVIE,
        subject_key=SubjectKeyRecord(kind="external_id", value="tmdb://101"),
        corruption_variant=None,
        corruption_fingerprint=None,
        item_ids=("fake:1:101",),
        expectation=no_action_expectation(screen),
        witness=None,
        ground_truth=(),
        provenance=Provenance(method="mechanical"),
    )
    truth = TruthFile(
        dataset_id="sw-test",
        lineage_id="lin-test",
        seed=1518,
        generator_version="0.1.0",
        source_export=SourceExport(export_id="test-export", items_sha256="0" * 64),
        screen=ScreenRef(
            items_sha256="0" * 64,
            schema_version=SCREEN_SCHEMA_VERSION,
            authority="none",
            min_applicable_checks=3,
        ),
        cases=(
            _repair_case(ProblemClass.DUPLICATE_QUALITY),
            _repair_case(ProblemClass.WRONG_MATCH),
            untouched,
        ),
    )
    payload = render_truth(truth)
    assert render_truth(load_truth(payload)) == payload
    assert b'"keeper":null' in payload

    reloaded = load_truth(payload)
    assert [type(case.expectation) for case in reloaded.cases] == [
        RepairExpectation,
        RepairExpectation,
        NoActionExpectation,
    ]
