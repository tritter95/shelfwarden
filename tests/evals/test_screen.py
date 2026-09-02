"""The mechanical screen: what has this item been *verified* not to have?

The gate is one assertion -- every exported item lands in exactly one verdict --
and everything else here defends a specific way the screen could be wrong while
still looking right. Three of them matter more than the rest:

* **Population scope.** A duplicate that was simply not sampled must not mark an
  item guarded. That failure would invert `fp_rate_snt` on the one class the
  local tier looks strongest at, in the direction the project has forbidden.
* **`unavailable` is not `pass`.** It is what lets the authority tier ship as a
  protocol, and if it ever counted toward a guard, nine classes would silently
  report themselves verified by nothing.
* **Byte-identity across hash seeds.** The screen builds more sets than the
  census does -- blocking buckets, guarded-class sets, twin groups -- so it is
  more exposed to hash-order leakage, not less.
"""

import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from shelfwarden.compare import SCREEN_POLICY
from shelfwarden.evals.export import ITEMS_FILE, ROOTS_FILE, run_export, select
from shelfwarden.evals.screen import (
    GUARD_TABLE,
    MIN_APPLICABLE_CHECKS,
    SCREEN_FILE,
    SCREEN_MARKDOWN_FILE,
    AuthorityRecord,
    CheckStatus,
    NullAuthority,
    Predicate,
    ScreenError,
    Tier,
    Verdict,
    build_screen,
    item_evidence_id,
    load_screen,
    read_export,
    render_markdown,
    render_screen,
    run_screen,
)
from shelfwarden.models.evidence import Source
from shelfwarden.models.finding import ProblemClass
from shelfwarden.models.ids import IdNamespace, ItemId, parse_guids
from shelfwarden.models.item import (
    AuthorItem,
    FetchProfile,
    FilePart,
    MediaKind,
    MovieItem,
)

from .conftest import BOOKS, MOVIES, SECTIONS, FakeLibrary, _id, _movie

TESTS_ROOT = str(Path(__file__).resolve().parent.parent)


@pytest.fixture
def screened(library, tmp_path):
    """The standard fixture library, exported and screened."""
    export = run_export(library, tmp_path / "e", count=200)
    return export, run_screen(export.directory, tmp_path / "s")


def custom_library(records, sections=SECTIONS[:1]) -> FakeLibrary:
    """A library holding exactly the records a test cares about.

    Purpose-built rather than bolted onto the shared fixture: the export and
    census suites assert exact counts against that one, and bending it to make a
    screen test convenient would be paying for this module's coverage out of
    theirs.
    """
    return FakeLibrary(
        records={str(record.item_id): record for record in records}, sections_=sections
    )


def author(rating_key: str, name: str) -> AuthorItem:
    return AuthorItem(
        item_id=_id(BOOKS, rating_key), fetched=FetchProfile.CORE, title=name, album_count=1
    )


def checks_of(screen, item_id: str) -> dict[Predicate, CheckStatus]:
    item = next(row for row in screen.items if row.item_id == item_id)
    return {check.predicate: check.status for check in item.checks}


def verdict_of(screen, item_id: str) -> Verdict:
    return next(row for row in screen.items if row.item_id == item_id).verdict


# -- the gate -------------------------------------------------------------


def test_screen_classifies_every_exported_item_into_exactly_one_verdict(screened):
    export, screen = screened
    assert len(screen.items) == len(export.items)
    assert [row.item_id for row in screen.items] == [str(item.item_id) for item in export.items]
    assert all(isinstance(row.verdict, Verdict) for row in screen.items)

    counts = screen.counts
    assert counts.items == len(export.items)
    assert counts.guarded + counts.failed + counts.insufficient == counts.items
    assert sum(row.items for row in counts.by_media_kind.values()) == counts.items


def test_a_screen_is_byte_identical_across_hash_seeds(tmp_path):
    """A same-process "screen it twice" comparison cannot see hash-order leakage
    at all, and this module builds more sets than the census does."""
    program = (
        "import sys, hashlib;"
        f"sys.path.insert(0, {TESTS_ROOT!r});"
        "from pathlib import Path;"
        "from evals.conftest import FakeLibrary;"
        "from shelfwarden.evals.export import run_export;"
        "from shelfwarden.evals.screen import run_screen, render_screen, render_markdown;"
        "out = Path(sys.argv[1]);"
        "run_export(FakeLibrary.build(), out / 'e', count=200);"
        "screen = run_screen(out / 'e', out / 's');"
        "payload = render_screen(screen) + render_markdown(screen).encode();"
        "print(hashlib.sha256(payload).hexdigest())"
    )
    digests = []
    for index, seed in enumerate(("0", "1")):
        result = subprocess.run(
            [sys.executable, "-c", program, str(tmp_path / f"run{index}")],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": seed},
            check=False,
        )
        assert result.returncode == 0, result.stderr
        digests.append(result.stdout.strip())
    assert digests[0] == digests[1]


# -- population scope -----------------------------------------------------


class TestPopulationScope:
    """Finding 4, and the single test that would have caught it."""

    def _library_with_twins(self):
        return custom_library(
            [
                _movie("101", "Solaris", 1972),
                _movie("102", "Stalker", 1979),
                _movie("103", "Blade Runner", 1982),
                _movie("104", "Blade Runner", 1982),
            ]
        )

    def _seed_selecting_one_twin(self, library) -> int:
        stubs = tuple(
            library.list_items(MOVIES, offset=0, limit=100, media_kind=MediaKind.MOVIE).items
        )
        for seed in range(200):
            chosen = {stub.item_id.rating_key for stub in select(stubs, 2, seed, MOVIES)}
            if len({"103", "104"} & chosen) == 1:
                return seed
        raise AssertionError("no seed samples exactly one of the twins")

    def test_twin_detection_uses_population_scope_not_the_slice(self, tmp_path):
        """A duplicate that was simply not sampled still disqualifies its twin.

        Under slice scope this item comes back `guarded` against
        `duplicate_quality`, the agent's correct finding on it is scored a false
        positive, and the project trains itself to suppress true detections.
        """
        library = self._library_with_twins()
        seed = self._seed_selecting_one_twin(library)
        export = run_export(library, tmp_path / "e", count=2, seed=seed, sections=(MOVIES,))

        exported = {str(item.item_id) for item in export.items}
        twins = {"fake:1:103", "fake:1:104"}
        (inside,) = twins & exported
        (outside,) = twins - exported
        assert outside not in exported, "the fixture must leave one twin unsampled"

        screen = run_screen(export.directory, tmp_path / "s")
        assert checks_of(screen, inside)[Predicate.NO_TITLE_YEAR_TWIN] is CheckStatus.FAIL
        assert verdict_of(screen, inside) is Verdict.FAILED

        check = next(
            c
            for c in next(r for r in screen.items if r.item_id == inside).checks
            if c.predicate is Predicate.NO_TITLE_YEAR_TWIN
        )
        assert check.scope == "population"
        assert check.population == 4, "the denominator is the library, not the slice"

    def test_uniqueness_is_unavailable_when_roots_jsonl_is_absent(self, library, tmp_path):
        """A version-1 export. Never a silent fallback to slice scope."""
        export = run_export(library, tmp_path / "e", count=200)
        (export.directory / ROOTS_FILE).unlink()
        screen = run_screen(export.directory, tmp_path / "s")

        statuses = checks_of(screen, "fake:1:101")
        assert statuses[Predicate.NO_TITLE_YEAR_TWIN] is CheckStatus.UNAVAILABLE
        assert screen.source.population_index is False
        reasons = {row.predicate: row.reasons for row in screen.predicates}
        assert "no_population_index" in reasons[Predicate.NO_TITLE_YEAR_TWIN]
        assert "no_population_index" in reasons[Predicate.NO_AUTHOR_NAME_TWIN]

    def test_an_absent_population_index_costs_a_guard_rather_than_producing_one(
        self, library, tmp_path
    ):
        export = run_export(library, tmp_path / "e", count=200)
        with_index = run_screen(export.directory, tmp_path / "s1")
        (export.directory / ROOTS_FILE).unlink()
        without = run_screen(export.directory, tmp_path / "s2")

        def guarded(screen):
            return next(
                row.guarded
                for row in screen.guard_coverage
                if row.problem_class is ProblemClass.DUPLICATE_QUALITY
            )

        assert guarded(with_index) > 0
        assert guarded(without) == 0

    def test_blocking_reports_its_scheme_and_what_it_did_not_compare(self, tmp_path):
        """House rule 12. Blocking means some pairs are never compared at all, so
        the gap is published rather than left invisible."""
        library = custom_library(
            [
                author("401", "Brandon Sanderson"),
                author("402", "Sanderson, Brandon"),
                author("403", "Ursula K. Le Guin"),
                author("404", "Terry Pratchett"),
            ],
            sections=(SECTIONS[2],),
        )
        export = run_export(library, tmp_path / "e", count=200, sections=(BOOKS,))
        screen = run_screen(export.directory, tmp_path / "s")

        (row,) = [b for b in screen.blocking if b.predicate is Predicate.NO_AUTHOR_NAME_TWIN]
        assert row.population == 4
        assert row.pairs_possible == 6
        assert 0 < row.pairs_resolved <= row.pairs_possible
        assert row.pairs_skipped == row.pairs_possible - row.pairs_resolved
        assert "token_set" in row.scheme
        assert row.note

        assert checks_of(screen, "fake:3:401")[Predicate.NO_AUTHOR_NAME_TWIN] is CheckStatus.FAIL
        assert checks_of(screen, "fake:3:402")[Predicate.NO_AUTHOR_NAME_TWIN] is CheckStatus.FAIL
        assert checks_of(screen, "fake:3:404")[Predicate.NO_AUTHOR_NAME_TWIN] is CheckStatus.PASS

    def test_the_author_threshold_is_published_as_a_sweep_not_chosen(self, tmp_path):
        """The one float in the step. The screen counts a twin at NORMALIZED or
        better; the sweep shows what a fuzzy floor would add."""
        library = custom_library(
            # Same last name, so the (initial, last_token) key puts them in one
            # bucket and they are actually compared -- at FUZZY, which the screen
            # does not accept and the sweep does.
            [author("401", "Brandon Sanderson"), author("402", "Brandan Sanderson")],
            sections=(SECTIONS[2],),
        )
        export = run_export(library, tmp_path / "e", count=200, sections=(BOOKS,))
        screen = run_screen(export.directory, tmp_path / "s")

        assert checks_of(screen, "fake:3:401")[Predicate.NO_AUTHOR_NAME_TWIN] is CheckStatus.PASS
        assert screen.author_fuzzy_sweep["0.90"] == 1
        assert screen.author_fuzzy_sweep["0.95"] == 0
        assert list(screen.author_fuzzy_sweep) == sorted(screen.author_fuzzy_sweep)

    def test_a_pair_sharing_no_blocking_key_is_never_compared(self, tmp_path):
        """The honest limitation, asserted rather than described.

        `Sandersen` and `Sanderson` share no token and no (initial, last) key, so
        the pair is never compared at all -- it does not even reach the sweep.
        Raising this to an all-pairs comparison is O(n^2) over the whole
        population and should be measured before being dismissed; until then the
        gap is a published number.
        """
        library = custom_library(
            [author("401", "Brandon Sanderson"), author("402", "Brandon Sandersen")],
            sections=(SECTIONS[2],),
        )
        export = run_export(library, tmp_path / "e", count=200, sections=(BOOKS,))
        screen = run_screen(export.directory, tmp_path / "s")

        (row,) = [b for b in screen.blocking if b.predicate is Predicate.NO_AUTHOR_NAME_TWIN]
        assert (row.pairs_possible, row.pairs_resolved, row.pairs_skipped) == (1, 0, 1)
        assert all(count == 0 for count in screen.author_fuzzy_sweep.values())


# -- the four statuses ----------------------------------------------------


class TestStatuses:
    def test_authority_predicates_are_unavailable_not_failed(self, screened):
        """Decision 3. `sources/` is step 1.1; until then nobody is answering, and
        "we did not ask" must never read as "we asked and it was wrong"."""
        _, screen = screened
        authority = [row for row in screen.predicates if row.tier is Tier.AUTHORITY]
        assert len(authority) == 3
        for row in authority:
            assert row.passed == 0
            assert row.failed == 0
            assert row.unavailable > 0
            assert set(row.reasons) == {"no_authority"}
        assert screen.authority == "none"

    def test_not_applicable_and_unavailable_are_distinguishable(self, screened):
        """Decision 4. Collapsing them makes "we have no TMDB record"
        indistinguishable from "movies have no season"."""
        _, screen = screened
        movie = checks_of(screen, "fake:1:101")
        assert movie[Predicate.SINGLE_PART] is CheckStatus.NOT_APPLICABLE
        assert movie[Predicate.TITLE_MATCHES_AUTHORITY] is CheckStatus.UNAVAILABLE
        assert CheckStatus.NOT_APPLICABLE is not CheckStatus.UNAVAILABLE

    def test_unavailable_never_counts_toward_guarded(self, screened):
        """The rule that makes deferring the authority tier need no conditional
        logic anywhere."""
        _, screen = screened
        authority_guarded = [
            row.problem_class
            for row in screen.guard_coverage
            if row.tier is Tier.AUTHORITY and row.guarded
        ]
        assert authority_guarded == []

        for item in screen.items:
            statuses = {check.predicate: check.status for check in item.checks}
            for problem_class in item.guarded_classes:
                guards = GUARD_TABLE[problem_class]
                assert guards
                assert all(statuses[predicate] is CheckStatus.PASS for predicate in guards)

    def test_a_check_that_could_not_run_says_why(self, screened):
        _, screen = screened
        for item in screen.items:
            for check in item.checks:
                if check.status is CheckStatus.UNAVAILABLE:
                    assert check.reason, f"{item.item_id}/{check.predicate} is silently unavailable"

    def test_every_predicate_records_its_tier_and_scope(self, screened):
        """So `screen.md` renders from `screen.json` alone."""
        _, screen = screened
        for item in screen.items:
            assert {check.predicate for check in item.checks} == set(Predicate)


# -- verdicts -------------------------------------------------------------


class TestVerdicts:
    def test_a_clean_movie_is_guarded(self, screened):
        _, screen = screened
        item = next(row for row in screen.items if row.item_id == "fake:1:101")
        assert item.verdict is Verdict.GUARDED
        assert item.applicable >= MIN_APPLICABLE_CHECKS
        assert item.passed == item.applicable
        assert ProblemClass.MISSING_METADATA in item.guarded_classes
        assert ProblemClass.WRONG_MATCH in item.unguarded_classes

    def test_fewer_than_three_applicable_checks_is_insufficient(self, screened):
        """Not guarded and not a candidate: a coverage metric on the screen."""
        _, screen = screened
        part = next(row for row in screen.items if row.item_id == "fake:3:421")
        assert part.verdict is Verdict.INSUFFICIENT
        assert part.applicable < MIN_APPLICABLE_CHECKS
        assert part.failing_predicates == ()

    def test_a_failing_check_outranks_a_thin_check_count(self, tmp_path):
        """A demonstrated problem is information; an item with one applicable
        check that failed is a candidate, not an unknown."""
        library = custom_library(
            [author("401", "X. Author"), author("402", "Author, X.")], sections=(SECTIONS[2],)
        )
        export = run_export(library, tmp_path / "e", count=200, sections=(BOOKS,))
        screen = run_screen(export.directory, tmp_path / "s")
        item = next(row for row in screen.items if row.item_id == "fake:3:401")
        assert item.applicable < MIN_APPLICABLE_CHECKS
        assert item.verdict is Verdict.FAILED

    def test_one_failing_check_disqualifies_the_whole_item(self, screened):
        """Per the spec's rule -- not only the classes that check guards. The
        per-class detail is still recorded, because 0.6 may want it."""
        _, screen = screened
        item = next(row for row in screen.items if row.item_id == "fake:1:107")
        assert item.failing_predicates == (Predicate.NO_TITLE_YEAR_TWIN,)
        assert item.verdict is Verdict.FAILED
        assert ProblemClass.MISSING_METADATA in item.guarded_classes
        assert ProblemClass.DUPLICATE_QUALITY in item.unguarded_classes


# -- candidates -----------------------------------------------------------


class TestCandidates:
    def test_a_failing_predicate_yields_a_candidate_carrying_its_evidence(self, screened):
        export, screen = screened
        candidate = next(row for row in screen.candidates if row.item_id == "fake:1:106")
        assert Predicate.SUMMARY_PRESENT in candidate.failing_predicates
        assert ProblemClass.MISSING_METADATA in candidate.proposed_problem_classes
        assert all(check.status is CheckStatus.FAIL for check in candidate.checks)

        item = next(row for row in export.items if str(row.item_id) == "fake:1:106")
        expected = item_evidence_id(export.manifest.export_id, item)
        assert all(check.evidence_id == expected for check in candidate.checks)

    def test_the_candidate_set_is_exactly_the_failed_set(self, screened):
        _, screen = screened
        assert {row.item_id for row in screen.candidates} == {
            row.item_id for row in screen.items if row.verdict is Verdict.FAILED
        }
        assert len(screen.candidates) == screen.counts.failed

    def test_evidence_ids_cite_the_library_read(self, screened):
        """implementation-plan.md §6: a library read is evidence too, which is
        what fills `verification.checks[].evidence_id` for a local check."""
        export, screen = screened
        item = next(row for row in export.items if str(row.item_id) == "fake:1:101")
        expected = item_evidence_id(export.manifest.export_id, item)
        assert expected.startswith("sha256:")
        local = [
            check
            for check in next(r for r in screen.items if r.item_id == "fake:1:101").checks
            if check.tier is Tier.LOCAL and check.status in (CheckStatus.PASS, CheckStatus.FAIL)
        ]
        assert local and all(check.evidence_id == expected for check in local)
        assert Source.LIBRARY  # the endpoint the id is derived from


# -- the local predicates -------------------------------------------------


class TestLocalPredicates:
    def test_an_nfd_path_does_not_fail_an_nfc_title(self, tmp_path):
        """The one door NFD text comes through: `FilePart.path` is deliberately
        not normalized, so the fold has to close this itself."""
        decomposed = unicodedata.normalize("NFD", "/media/Movies/Amélie (2001)/Amélie (2001).mkv")
        assert decomposed != unicodedata.normalize("NFC", decomposed)
        movie = MovieItem(
            item_id=ItemId("fake", MOVIES, "101"),
            fetched=FetchProfile.CORE,
            title="Amélie",
            year=2001,
            summary="A shy waitress.",
            guids=parse_guids(None, ["tmdb://194"]),
            parts=(FilePart(part_id="1", path=decomposed, container="mkv"),),
        )
        export = run_export(custom_library([movie]), tmp_path / "e", count=200)
        screen = run_screen(export.directory, tmp_path / "s")
        assert checks_of(screen, "fake:1:101")[Predicate.FILENAME_MATCHES_METADATA] is (
            CheckStatus.PASS
        )
        assert verdict_of(screen, "fake:1:101") is Verdict.GUARDED

    def test_a_scene_release_name_fails_the_filename_check(self, tmp_path):
        movie = _movie(
            "101",
            "Amélie",
            2001,
            parts=(FilePart(part_id="1", path="/media/Movies/xvid-abc123.avi", container="avi"),),
        )
        export = run_export(custom_library([movie]), tmp_path / "e", count=200)
        screen = run_screen(export.directory, tmp_path / "s")
        statuses = checks_of(screen, "fake:1:101")
        assert statuses[Predicate.FILENAME_MATCHES_METADATA] is CheckStatus.FAIL
        assert verdict_of(screen, "fake:1:101") is Verdict.FAILED

    def test_an_episode_under_the_wrong_season_fails(self, library, tmp_path):
        """`episode_wrong_season` moves exactly this field."""
        moved = library.records["fake:2:2211"].model_copy(update={"parent_index": 2})
        library.records["fake:2:2211"] = moved
        export = run_export(library, tmp_path / "e", count=200)
        screen = run_screen(export.directory, tmp_path / "s")

        assert checks_of(screen, "fake:2:2211")[Predicate.SEASON_MEMBERSHIP_COHERENT] is (
            CheckStatus.FAIL
        )
        # The family carries the failure too: the show is no longer guarded.
        assert checks_of(screen, "fake:2:201")[Predicate.SEASON_MEMBERSHIP_COHERENT] is (
            CheckStatus.FAIL
        )

    def test_a_multi_part_book_is_a_candidate_rather_than_a_guarantee(self, screened):
        """`multi_file_split` cannot be ruled out on a book with several parts,
        and the screen says so rather than guessing."""
        _, screen = screened
        assert checks_of(screen, "fake:3:411")[Predicate.SINGLE_PART] is CheckStatus.FAIL
        assert checks_of(screen, "fake:3:412")[Predicate.SINGLE_PART] is CheckStatus.PASS


# -- the authority seam ---------------------------------------------------


class StubAuthority:
    """What step 1.1 will pass in. Nothing in `screen.py` changes for it."""

    def __init__(self, records):
        self._records = records

    @property
    def name(self) -> str:
        return "stub"

    def by_external_id(self, namespace: IdNamespace, value: str) -> AuthorityRecord | None:
        return self._records.get((namespace, value))


class TestAuthoritySeam:
    def _export(self, tmp_path, **overrides):
        movie = _movie("101", "Amélie", 2001, **overrides)
        return run_export(custom_library([movie]), tmp_path / "e", count=200)

    def test_a_wired_authority_promotes_the_predicates(self, tmp_path):
        export = self._export(tmp_path)
        authority = StubAuthority(
            {
                (IdNamespace.TMDB, "101"): AuthorityRecord(
                    evidence_id="sha256:stub",
                    source=Source.TMDB,
                    title="Le Fabuleux Destin d'Amélie Poulain",
                    aliases=("Amélie",),
                    year=2001,
                )
            }
        )
        screen = build_screen(export.manifest, export.items, None, authority=authority)
        statuses = {check.predicate: check for check in screen.items[0].checks}
        title = statuses[Predicate.TITLE_MATCHES_AUTHORITY]
        assert title.status is CheckStatus.PASS
        assert title.support.strength == "alias"
        assert title.evidence_id == "sha256:stub"
        assert statuses[Predicate.YEAR_MATCHES_AUTHORITY].status is CheckStatus.PASS

        coverage = {row.problem_class: row for row in screen.guard_coverage}
        assert coverage[ProblemClass.FOREIGN_TITLE_VARIANT].guarded == 1

    def test_a_disagreeing_authority_fails_rather_than_being_unavailable(self, tmp_path):
        export = self._export(tmp_path)
        authority = StubAuthority(
            {
                (IdNamespace.TMDB, "101"): AuthorityRecord(
                    evidence_id="sha256:stub", source=Source.TMDB, title="Solaris", year=1972
                )
            }
        )
        screen = build_screen(export.manifest, export.items, None, authority=authority)
        statuses = {check.predicate: check.status for check in screen.items[0].checks}
        assert statuses[Predicate.TITLE_MATCHES_AUTHORITY] is CheckStatus.FAIL
        assert statuses[Predicate.YEAR_MATCHES_AUTHORITY] is CheckStatus.FAIL

    def test_an_authority_with_no_record_is_unavailable_not_failed(self, tmp_path):
        export = self._export(tmp_path)
        screen = build_screen(export.manifest, export.items, None, authority=StubAuthority({}))
        checks = {check.predicate: check for check in screen.items[0].checks}
        assert checks[Predicate.TITLE_MATCHES_AUTHORITY].status is CheckStatus.UNAVAILABLE
        assert checks[Predicate.TITLE_MATCHES_AUTHORITY].reason == "no_authority_record"

    def test_an_item_with_nothing_to_look_up_by_says_so(self, tmp_path):
        export = self._export(tmp_path, guids=())
        screen = build_screen(export.manifest, export.items, None, authority=StubAuthority({}))
        checks = {check.predicate: check for check in screen.items[0].checks}
        assert checks[Predicate.TITLE_MATCHES_AUTHORITY].reason == "no_resolvable_id"

    def test_the_null_authority_answers_nothing(self):
        assert NullAuthority().name == "none"
        assert NullAuthority().by_external_id(IdNamespace.TMDB, "1") is None


# -- guard coverage -------------------------------------------------------


class TestGuardCoverage:
    def test_guard_coverage_per_class_is_reported(self, screened):
        """Decision 3's obligation: `fp_rate_snt` has to be able to state its own
        denominator rather than reading as if it covered all fifteen classes."""
        _, screen = screened
        assert {row.problem_class for row in screen.guard_coverage} == set(ProblemClass)
        assert [row.problem_class for row in screen.guard_coverage] == list(ProblemClass)

        # Seven classes, all local tier. The step plan says six; it undercounts
        # by one because `absolute_vs_seasonal` is guarded -- weakly, on
        # structure alone -- by `episode_numbering_contiguous`, which is local.
        # The number is computed from GUARD_TABLE rather than asserted in prose.
        local = {row.problem_class for row in screen.guard_coverage if row.guarded}
        assert local == {
            ProblemClass.MISSING_METADATA,
            ProblemClass.DUPLICATE_QUALITY,
            ProblemClass.EPISODE_WRONG_SEASON,
            ProblemClass.ABSOLUTE_VS_SEASONAL,
            ProblemClass.FILENAME_UNMATCHABLE,
            ProblemClass.AUTHOR_NAME_VARIANT,
            ProblemClass.MULTI_FILE_SPLIT,
        }
        assert all(row.tier is Tier.LOCAL for row in screen.guard_coverage if row.guarded)

    def test_an_unguardable_class_says_why_rather_than_reporting_a_bare_zero(self, screened):
        _, screen = screened
        rows = {row.problem_class: row for row in screen.guard_coverage}
        assert rows[ProblemClass.ANTHOLOGY_OMNIBUS].guard_predicates == ()
        assert "by design" in rows[ProblemClass.ANTHOLOGY_OMNIBUS].reason
        assert "1.1" in rows[ProblemClass.ALTERNATE_CUT].reason

    def test_the_counts_partition_the_items_in_scope(self, screened):
        _, screen = screened
        for row in screen.guard_coverage:
            assert row.guarded + row.failed + row.blocked == row.in_scope

    def test_the_screen_records_its_own_admission_standard(self, screened):
        """A stored screen states the rule it was taken under."""
        _, screen = screened
        assert screen.min_applicable_checks == MIN_APPLICABLE_CHECKS
        assert screen.policy == SCREEN_POLICY.name


# -- reading and writing --------------------------------------------------


class TestBinding:
    def test_the_screen_is_written_outside_the_export(self, screened, tmp_path):
        export, _ = screened
        assert sorted(path.name for path in (tmp_path / "s").iterdir()) == [
            SCREEN_FILE,
            SCREEN_MARKDOWN_FILE,
        ]
        assert not (export.directory / SCREEN_FILE).exists()

    def test_it_records_what_it_is_a_screen_of(self, screened):
        export, screen = screened
        assert screen.source.export_id == export.manifest.export_id
        assert screen.source.items_sha256 == export.manifest.items_sha256
        assert screen.source.roots_sha256 == export.manifest.roots_sha256
        assert screen.source.export_schema_version == 2

    def test_screen_refuses_an_export_whose_items_sha256_differs(self, library, tmp_path):
        """An edited export is not the export its manifest describes."""
        export = run_export(library, tmp_path / "e", count=200)
        path = export.directory / ITEMS_FILE
        path.write_bytes(path.read_bytes()[:-40])
        with pytest.raises(ScreenError, match="items_sha256"):
            read_export(export.directory)

    def test_a_stored_screen_refuses_to_bind_to_a_different_export(self, library, tmp_path):
        """A guarded label carried onto another export is a wrong label with a
        plausible provenance."""
        first = run_export(library, tmp_path / "e1", count=200)
        run_screen(first.directory, tmp_path / "s")
        other = run_export(FakeLibrary.build(), tmp_path / "e2", count=200, sections=(MOVIES,))
        with pytest.raises(ScreenError, match="was taken of export"):
            load_screen(tmp_path / "s", other.directory)
        assert load_screen(tmp_path / "s", first.directory).source.export_id

    def test_screening_a_census_only_export_is_a_correctable_error(self, library, tmp_path):
        """practices §5.4 applies to the CLI for the same reason it applies to
        tools: an error that does not name a next action is a dead end."""
        export = run_export(library, tmp_path / "e", census_only=True)
        with pytest.raises(ScreenError, match="--census-only"):
            read_export(export.directory)

    def test_a_directory_that_is_not_an_export_says_what_to_point_at(self, tmp_path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(ScreenError, match="not an export directory"):
            read_export(tmp_path / "empty")

    def test_an_edited_population_index_is_refused(self, library, tmp_path):
        export = run_export(library, tmp_path / "e", count=200)
        path = export.directory / ROOTS_FILE
        path.write_bytes(path.read_bytes() + b'{"tampered": true}\n')
        with pytest.raises(ScreenError, match="roots_sha256"):
            read_export(export.directory)

    def test_the_stored_json_round_trips(self, screened, tmp_path):
        _, screen = screened
        reloaded = load_screen(tmp_path / "s")
        assert render_screen(reloaded) == render_screen(screen)
        assert render_screen(screen) == (tmp_path / "s" / SCREEN_FILE).read_bytes()


class TestMarkdown:
    def test_it_renders_from_a_stored_screen_alone(self, screened, tmp_path):
        _, screen = screened
        reloaded = load_screen(tmp_path / "s")
        assert render_markdown(reloaded) == render_markdown(screen)

    def test_guard_coverage_is_a_table_not_a_footnote(self, screened):
        _, screen = screened
        rendered = render_markdown(screen)
        assert "## Guard coverage per class" in rendered
        for problem_class in ProblemClass:
            assert str(problem_class) in rendered

    def test_it_names_the_absent_authority_tier(self, screened):
        _, screen = screened
        assert "Authority: **none**" in render_markdown(screen)

    def test_it_says_which_number_to_read_first(self, screened):
        _, screen = screened
        assert "insufficient" in render_markdown(screen)

    def test_it_reports_the_blocking_scheme(self, screened):
        _, screen = screened
        rendered = render_markdown(screen)
        assert "token_set + (initial, last_token)" in rendered
        assert "pairs possible" in rendered

    def test_a_missing_population_index_is_stated_at_the_top(self, library, tmp_path):
        export = run_export(library, tmp_path / "e", count=200)
        (export.directory / ROOTS_FILE).unlink()
        screen = run_screen(export.directory, tmp_path / "s")
        assert "No population index" in render_markdown(screen)

    def test_it_ends_with_a_newline(self, screened):
        _, screen = screened
        assert render_markdown(screen).endswith("\n")
