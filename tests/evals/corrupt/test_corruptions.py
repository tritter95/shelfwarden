"""Every registered corruption, and the checks that decide whether one ships.

The step's gate is here: for each class, the mutation applies, the delta reverses
to the ground truth byte-for-byte, and the case is **provably detectable**. Those
three run as one parameterized sweep rather than fifteen near-identical tests,
because the interesting failures are the ones that are class-specific, and they
get their own tests below.

Three of those defend findings that cost real debugging:

* **The screen must notice what the generator did.** Step 0.5 found two rows of
  `GUARD_TABLE` claiming guards they did not have. The cross-check is what caught
  them, and `test_the_screen_corroborates_every_locally_guarded_class` is what
  keeps the next such row from shipping.
* **Selection is prefix-stable.** `random.sample` is not, and a re-picked subject
  resets `case_id` for cases that did not change.
* **A corruption declares its blast radius.** Corrupting one family can strip a
  population-scoped guard from an item in another one.
"""

from pathlib import Path

import pytest

from shelfwarden.evals.corrupt import registry
from shelfwarden.evals.corrupt.context import (
    MINTED_PREFIX,
    CorruptionContext,
    group_families,
    rank_key,
    subject_key,
)
from shelfwarden.evals.corrupt.model import ChangeKind, Rejection
from shelfwarden.evals.corrupt.registry import (
    CORRUPTION_TABLE,
    CROSS_BROKEN,
    CROSS_INTACT,
    UNSYNTHESIZABLE_REASON,
    attempt,
)
from shelfwarden.evals.corrupt.reverse import apply_reverse, render_family
from shelfwarden.evals.corrupt.run import run_corruptions, variant_for
from shelfwarden.evals.screen import GUARD_TABLE, PREDICATE_TIER, Tier
from shelfwarden.models.finding import ProblemClass
from shelfwarden.models.item import MediaKind, with_changes

from ..conftest import FakeLibrary
from .conftest import survey_inputs

SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src"


def _library_items():
    return tuple(FakeLibrary.build().records.values())


def survey(items, classes=None, limit=None, seed=1518):
    payload, roots = survey_inputs(items)
    return run_corruptions(
        export_id="test-export",
        items=payload,
        roots=roots,
        seed=seed,
        classes=classes,
        limit=limit,
    )


@pytest.fixture(scope="module")
def library_run():
    return survey(_library_items())


# -- the gate -------------------------------------------------------------


class TestEveryRegisteredCorruption:
    def test_every_problem_class_is_declared_exactly_once(self):
        """Fifteen classes, each either implemented or explained. Never neither, never both."""
        implemented = set(CORRUPTION_TABLE)
        deferred = set(UNSYNTHESIZABLE_REASON)
        assert implemented | deferred == set(ProblemClass)
        assert not implemented & deferred

    def test_the_counts_are_computed_from_the_table_not_typed(self):
        # The docstrings claim eleven implemented and four deferred. If that ever
        # stops being true, this is the test that says so rather than a stale
        # sentence in a plan.
        assert len(CORRUPTION_TABLE) == 11
        assert len(UNSYNTHESIZABLE_REASON) == 4

    def test_a_deferred_class_yields_no_cases_and_says_why(self, library_run):
        for deficit in library_run.deficits:
            if deficit.problem_class not in UNSYNTHESIZABLE_REASON:
                continue
            assert deficit.accepted == 0
            assert deficit.unsynthesizable_reason

    @pytest.mark.parametrize("problem_class", sorted(CORRUPTION_TABLE, key=str))
    def test_the_mutation_applies_the_truth_round_trips_and_the_case_is_detectable(
        self, problem_class, request
    ):
        """The step's gate, per class."""
        items = _library_items()
        if problem_class in {
            ProblemClass.SERIES_ORDER_BROKEN,
            ProblemClass.MISSING_SERIES,
            ProblemClass.MULTI_FILE_SPLIT,
        }:
            items = request.getfixturevalue("series_family")
        elif problem_class is ProblemClass.ALTERNATE_CUT:
            items = request.getfixturevalue("edition_family")

        run = survey(items, classes=[problem_class])
        assert run.results, (
            f"{problem_class} produced no case; rejections="
            f"{[(r.reason, r.detail) for r in run.rejections]}"
        )

        by_id = {str(item.item_id): item for item in items}
        for result in run.results:
            assert result.changes, "a case with no delta is not a corruption"
            family = next(
                fam for fam in group_families(items) if str(fam.root.item_id) == result.root_id
            )
            corrupted = _forward(family.records, result.changes)
            # 1. the mutation applied
            assert render_family(corrupted) != render_family(family.records)
            # 2. the truth round-trips, byte-for-byte
            assert render_family(apply_reverse(corrupted, result.changes)) == render_family(
                family.records
            )
            # 3. the case is provably detectable
            assert result.witness.discriminates
            assert result.witness.evidence_id
            # and the witness cites the corrupted world, never the clean one
            for subject in result.witness.subjects:
                assert subject in {str(i.item_id) for i in corrupted} or subject in by_id


def _forward(records, changes):
    from shelfwarden.evals.corrupt.reverse import apply_changes

    return apply_changes(records, changes)


# -- the screen cross-check ----------------------------------------------


class TestScreenCorroboration:
    def test_no_corruption_leaves_its_guard_intact(self, request):
        """`intact` is the regression signal, and it must never appear.

        This is the test step 0.5 would have wanted before it found two rows of
        `GUARD_TABLE` claiming guards they did not have: `episode_wrong_season`
        (whose predicate stays *passing* on a re-parented episode, because
        re-parenting is internally consistent) and `absolute_vs_seasonal` (whose
        contiguity check passes on exactly the numbering it is meant to detect,
        since S01E01..S01E52 is contiguous). Both produced `intact` before the
        table was corrected, and both would again.
        """
        for problem_class in self._locally_guarded():
            run = self._run_for(problem_class, request)
            assert run.results, f"{problem_class} produced no case to check"
            verdicts = {result.cross_check.verdict for result in run.results}
            assert CROSS_INTACT not in verdicts, (
                f"{problem_class}: the screen still guards this class after the "
                "corruption -- either the corruption is a lie or the guard is"
            )

    def test_the_screen_corroborates_the_classes_whose_guard_it_can_hold(self, request):
        """Where the guard *can* hold on a clean family, corruption must break it.

        Not every locally guarded class can be corroborated. `multi_file_split`
        targets a book with two files, and its guard is `single_part` -- so the
        ground truth already fails the guard, by construction, and the honest
        verdict is `already_failing` rather than a corroboration. That is a real
        limit of the screen, recorded rather than papered over.
        """
        corroborated = {
            ProblemClass.DUPLICATE_QUALITY,
            ProblemClass.EPISODE_WRONG_SEASON,
            ProblemClass.ABSOLUTE_VS_SEASONAL,
            ProblemClass.AUTHOR_NAME_VARIANT,
        }
        for problem_class in corroborated:
            run = self._run_for(problem_class, request)
            verdicts = {result.cross_check.verdict for result in run.results}
            assert CROSS_BROKEN in verdicts, f"{problem_class}: verdicts={verdicts}"

    @staticmethod
    def _locally_guarded():
        found = [
            problem_class
            for problem_class, guards in GUARD_TABLE.items()
            if guards
            and problem_class in CORRUPTION_TABLE
            and all(PREDICATE_TIER[predicate] is Tier.LOCAL for predicate in guards)
        ]
        assert found, "no locally guarded class is implemented; the check would be vacuous"
        return found

    @staticmethod
    def _run_for(problem_class, request):
        items = _library_items()
        if problem_class in {
            ProblemClass.MULTI_FILE_SPLIT,
            ProblemClass.MISSING_SERIES,
            ProblemClass.SERIES_ORDER_BROKEN,
            ProblemClass.AUTHOR_NAME_VARIANT,
        }:
            items = request.getfixturevalue("series_family")
        elif problem_class is ProblemClass.ALTERNATE_CUT:
            items = request.getfixturevalue("edition_family")
        return survey(items, classes=[problem_class])

    def test_a_corruption_the_screen_still_guards_is_rejected(self):
        """The rejection itself, forced by neutering a corruption into a near no-op."""
        items = _library_items()
        payload, roots = survey_inputs(items)
        spec = CORRUPTION_TABLE[ProblemClass.DUPLICATE_QUALITY]
        family = next(
            fam
            for fam in group_families(payload)
            if fam.root.media_kind is MediaKind.MOVIE and fam.records[0].parts
        )
        ctx = CorruptionContext.build(
            export_id="test-export",
            seed=1518,
            problem_class=ProblemClass.DUPLICATE_QUALITY,
            variant="resolution",
            root=family.root,
            subject=subject_key(family.records[0]),
            items={str(i.item_id): i for i in payload},
            roots=roots,
        )
        # A "corruption" that only edits a summary leaves the twin guard intact.
        neutered = registry.CorruptionSpec(
            problem_class=spec.problem_class,
            applies_to=spec.applies_to,
            variants=spec.variants,
            witness_kind=spec.witness_kind,
            tier=spec.tier,
            induces=spec.induces,
            applicable=spec.applicable,
            corrupt=lambda fam, c: registry.Mutation(
                items=(with_changes(fam.records[0], {"summary": "changed"}), *fam.records[1:]),
                witness=_always_discriminating_witness(c, fam),
            ),
            doc="",
        )
        outcome = attempt(neutered, family, ctx)
        assert isinstance(outcome, Rejection)
        assert outcome.reason == "screen_intact"


def _always_discriminating_witness(ctx, family):
    from shelfwarden.compare import Support, SupportStrength
    from shelfwarden.evals.corrupt.witness import LocalWitness

    return LocalWitness.over(ctx.export_id, family.records).value(
        subject_id=str(family.records[0].item_id),
        pointer="/title",
        comparator="compare_title",
        resolved=family.records[0].title,
        against_truth=Support(SupportStrength.EXACT, "identity"),
        against_corrupted=Support(SupportStrength.NONE, "no_match"),
        policy=ctx.policy,
    )


# -- determinism ----------------------------------------------------------


class TestDeterminism:
    def test_selection_is_prefix_stable_in_the_limit(self):
        """Raising a limit adds subjects; it never re-picks them.

        `random.sample` is not prefix-stable in `k` -- verified in step 0.5:
        `Random(1518).sample(range(24), 5)` selects element 23 and
        `sample(range(24), 6)` does not. Selection is by hash rank for this reason,
        and a regression here resets every `case_id` in a cell on a one-case edit.
        """
        items = _library_items()
        small = survey(items, classes=[ProblemClass.WRONG_MATCH], limit=3)
        large = survey(items, classes=[ProblemClass.WRONG_MATCH], limit=5)
        small_ids = [result.root_id for result in small.results]
        large_ids = [result.root_id for result in large.results]
        assert large_ids[: len(small_ids)] == small_ids

    def test_a_case_is_independent_of_the_run_it_was_generated_in(self):
        """One class alone produces the same bytes as that class inside the full set."""
        items = _library_items()
        alone = survey(items, classes=[ProblemClass.FILENAME_UNMATCHABLE])
        together = survey(items)
        picked = {
            r.root_id: r
            for r in together.results
            if r.problem_class is ProblemClass.FILENAME_UNMATCHABLE
        }
        assert picked
        for result in alone.results:
            other = picked[result.root_id]
            assert result.corrupted_sha256 == other.corrupted_sha256
            assert result.variant == other.variant

    def test_the_same_seed_produces_the_same_survey_twice(self):
        items = _library_items()
        first, second = survey(items), survey(items)
        assert [r.corrupted_sha256 for r in first.results] == [
            r.corrupted_sha256 for r in second.results
        ]

    def test_a_different_seed_produces_a_different_survey(self):
        items = _library_items()
        assert [r.corrupted_sha256 for r in survey(items, seed=1).results] != [
            r.corrupted_sha256 for r in survey(items, seed=2).results
        ]

    def test_the_variant_is_ranked_not_drawn(self):
        spec = CORRUPTION_TABLE[ProblemClass.DUPLICATE_QUALITY]
        subject = subject_key(_library_items()[0])
        assert variant_for(spec, 1518, subject) == variant_for(spec, 1518, subject)

    def test_the_subject_key_survives_a_rescan(self):
        """Rating keys move when Plex re-adds an item; a subject must not.

        Invariant 9. If the seed derived from a rating key, every case would be
        re-corrupted differently after any maintenance pass on the server.
        """
        from shelfwarden.models.ids import ItemId

        for item in _library_items():
            key = subject_key(item)
            assert key.kind in {"external_id", "title_year", "path"}
            moved = with_changes(
                item,
                {
                    "item_id": {
                        "provider": item.item_id.provider,
                        "section_id": item.item_id.section_id,
                        "rating_key": "999999",
                    }
                },
            )
            assert subject_key(moved) == key
            assert ItemId("p", "s", "k") != moved.item_id

    def test_the_rank_is_a_function_of_the_subject_alone(self):
        subject = subject_key(_library_items()[0])
        assert rank_key(1518, subject) == rank_key(1518, subject)
        assert rank_key(1518, subject) != rank_key(1519, subject)

    def test_the_package_never_uses_random_sample(self):
        """`sample` is banned outright rather than reviewed case by case.

        Its output is not a prefix-stable function of `k`, and the failure is
        invisible: the dataset still generates, the ids just all change.
        """
        # Parsed, not grepped: every module in the package *documents* why
        # `random.sample` is banned, so a text search finds the warning and calls
        # it the offence.
        import ast

        offenders = []
        for path in (SOURCE_ROOT / "shelfwarden" / "evals" / "corrupt").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                name = function.attr if isinstance(function, ast.Attribute) else None
                if name in {"sample", "choices"}:
                    offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == []


# -- blast radius ---------------------------------------------------------


class TestCollateralAndInduced:
    def test_a_manufactured_twin_is_declared(self, library_run):
        """`wrong_match` copies a real title, which makes its donor a twin.

        Verified in step 0.5: the donor -- an item nothing touched -- loses its
        `duplicate_quality` guard. Undeclared, a correct agent finding on it would
        score as a false positive, which is the forbidden direction.
        """
        matches = [r for r in library_run.results if r.problem_class is ProblemClass.WRONG_MATCH]
        assert matches
        for result in matches:
            assert ProblemClass.DUPLICATE_QUALITY in result.induced
            assert result.collateral, "the donor is outside the family and must be declared"
            assert result.root_id not in result.collateral

    def test_a_corruption_with_no_blast_radius_declares_none(self, library_run):
        duplicates = [
            r for r in library_run.results if r.problem_class is ProblemClass.DUPLICATE_QUALITY
        ]
        assert duplicates
        assert all(not result.collateral for result in duplicates)


class TestAddedItems:
    def test_a_minted_key_never_collides_with_the_export(self, library_run):
        existing = {str(item.item_id) for item in _library_items()}
        added = [
            change.item_id
            for result in library_run.results
            for change in result.changes
            if change.kind is ChangeKind.ADD
        ]
        assert added
        assert not set(added) & existing
        assert all(MINTED_PREFIX in item_id for item_id in added)

    def test_an_added_root_is_reported_so_the_population_index_can_be_rebuilt(self, library_run):
        duplicates = [
            r for r in library_run.results if r.problem_class is ProblemClass.DUPLICATE_QUALITY
        ]
        assert duplicates
        for result in duplicates:
            assert len(result.added_roots) == 1
            assert MINTED_PREFIX in result.added_roots[0].item_id.rating_key


# -- per-class specifics --------------------------------------------------


class TestClassBehaviour:
    def test_wrong_match_keeps_the_files_alone(self, library_run):
        for result in library_run.results:
            if result.problem_class is not ProblemClass.WRONG_MATCH:
                continue
            paths = [
                field.path
                for change in result.changes
                for field in change.fields
                if field.path.startswith("/parts")
            ]
            assert not paths, "the witness is the filename; rewriting it would erase the evidence"

    def test_a_remake_pair_is_not_treated_as_a_duplicate(self, library_run):
        """Solaris 1972 and Solaris 2002 share a title and are not twins.

        Matching on title alone would block `duplicate_quality` on every film that
        has ever been remade, and would let `year_collision_remake` fire on a pair
        that is already colliding.
        """
        remakes = [
            r for r in library_run.results if r.problem_class is ProblemClass.YEAR_COLLISION_REMAKE
        ]
        duplicates = [
            r.root_id
            for r in library_run.results
            if r.problem_class is ProblemClass.DUPLICATE_QUALITY
        ]
        assert remakes
        assert set(r.root_id for r in remakes) & set(duplicates)

    def test_episode_wrong_season_moves_the_parent_and_the_index_together(self, library_run):
        (result,) = [
            r for r in library_run.results if r.problem_class is ProblemClass.EPISODE_WRONG_SEASON
        ]
        (change,) = [c for c in result.changes if c.kind is ChangeKind.MODIFY]
        moved = {field.path for field in change.fields}
        # Plex derives an episode's season from its parent, so `parent_index`
        # alone is a state no real server can produce. `/parent` is recorded whole
        # rather than as `/parent/rating_key`: an ItemId is an address, and half of
        # one is an id whose section no longer matches its key.
        assert {"/parent", "/parent_index", "/parent_title"} <= moved

    def test_absolute_vs_seasonal_removes_the_seasons_it_empties(self, library_run):
        (result,) = [
            r for r in library_run.results if r.problem_class is ProblemClass.ABSOLUTE_VS_SEASONAL
        ]
        removed = [c for c in result.changes if c.kind is ChangeKind.REMOVE]
        assert removed, "a real server does not keep a season with nothing in it"

    def test_filename_unmatchable_writes_a_name_that_parses(self, library_run):
        """The scene name carries signal on purpose.

        A name with none would not be a hard case, it would be an unsolvable one,
        and a dataset of those measures nothing while looking rigorous.
        """
        from shelfwarden.compare import parse_release_name

        results = [
            r for r in library_run.results if r.problem_class is ProblemClass.FILENAME_UNMATCHABLE
        ]
        assert results
        for result in results:
            written = [
                field.after
                for change in result.changes
                for field in change.fields
                if field.path.endswith("/path")
            ]
            assert written
            assert all(parse_release_name(path).title for path in written)

    def test_author_name_variant_lands_at_alias_not_fuzzy(self, series_family):
        """An inversion is a structural equivalence, not a similarity score.

        Letting it land at FUZZY would make the class's own guard
        threshold-dependent, which is the one shape spec §3 forbids.
        """
        run = survey(series_family, classes=[ProblemClass.AUTHOR_NAME_VARIANT])
        (result,) = run.results
        assert result.witness.relation == "same_author"
        assert all(support.strength != "fuzzy" for support in result.witness.support)

    def test_multi_file_split_cites_the_shared_directory(self, series_family):
        run = survey(series_family, classes=[ProblemClass.MULTI_FILE_SPLIT])
        (result,) = run.results
        assert result.witness.relation == "same_book"
        assert len(result.witness.subjects) >= 2

    def test_missing_series_recovers_the_series_from_the_folder(self, series_family):
        run = survey(series_family, classes=[ProblemClass.MISSING_SERIES])
        assert run.results
        for result in run.results:
            assert result.witness.resolved
            assert "Stormlight" in str(result.witness.resolved[0])

    def test_series_order_broken_needs_intact_siblings(self, series_family):
        run = survey(series_family, classes=[ProblemClass.SERIES_ORDER_BROKEN])
        assert run.results
        thin = survey(series_family[:3], classes=[ProblemClass.SERIES_ORDER_BROKEN])
        assert not thin.results
        assert any(r.reason == "too_few_in_series" for r in thin.rejections)

    def test_alternate_cut_needs_the_marker_on_disk(self, edition_family):
        run = survey(edition_family, classes=[ProblemClass.ALTERNATE_CUT])
        assert run.results
        for result in run.results:
            assert result.witness.resolved


# -- rejection bookkeeping ------------------------------------------------


class TestDeficits:
    def test_not_applicable_and_rejected_are_counted_apart(self, library_run):
        """ "Your library has no remake pairs" and "the generator is broken" are
        different facts, and only one of them is actionable."""
        remakes = next(
            d for d in library_run.deficits if d.problem_class is ProblemClass.YEAR_COLLISION_REMAKE
        )
        assert dict(remakes.not_applicable_by_reason).get("no_remake_pair")
        assert remakes.candidates == remakes.attempted

    def test_a_deficit_reports_scope_candidates_and_acceptance(self, library_run):
        for deficit in library_run.deficits:
            if deficit.unsynthesizable_reason:
                continue
            assert deficit.accepted <= deficit.attempted <= deficit.candidates
            assert deficit.candidates <= deficit.families_in_scope

    def test_reasons_are_ordered_by_count_then_name(self, library_run):
        for deficit in library_run.deficits:
            for counted in (deficit.not_applicable_by_reason, deficit.rejected_by_reason):
                ordered = sorted(counted, key=lambda pair: (-pair[1], pair[0]))
                assert list(counted) == ordered
