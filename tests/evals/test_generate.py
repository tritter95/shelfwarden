"""The generator: a labelled, balanced, regenerable dataset from an export.

Step 0.6's gate is that `generate --count N --seed S` is reproducible and never
silently unbalances the dataset. The first half of this module concerns the
composition and the curated slices (build step 3); the second, selection (build
step 4) -- hash-seed identity, prefix stability, re-export survival, subject
collisions, and the should-not-touch slice.

The property this file opens with is the one a composition edit rests on: the
**lineage** is the library and the generator, never `composition.toml`. Editing a
share is the one thing that file exists to let a human do, and keyed on it the CI
baseline would reset -- discarding the history of every case whose id did not
change.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from shelfwarden.evals import curated as curated_module
from shelfwarden.evals.composition import (
    COMPOSITION_FILE,
    CURATED,
    DeficitReason,
    parse_composition,
    resolve,
)
from shelfwarden.evals.corrupt.model import ItemChange
from shelfwarden.evals.corrupt.registry import CORRUPTION_TABLE
from shelfwarden.evals.corrupt.reverse import apply_changes, apply_reverse, render_family
from shelfwarden.evals.corrupt.run import read_export_with_population
from shelfwarden.evals.curated import CURATED_FILES, CURATED_ROOT
from shelfwarden.evals.export import run_export
from shelfwarden.evals.generate import (
    DATASET_FILE,
    DELTAS_FILE,
    MAX_CASES_PER_SUBJECT,
    TRUTH_FILE,
    Dataset,
    GenerateError,
    generate,
    render_dataset,
    run_generate,
)
from shelfwarden.evals.screen import Verdict, build_screen
from shelfwarden.evals.truth import (
    RepairExpectation,
    Slice,
    Unexpected,
    case_id,
    load_truth,
    render_truth,
)
from shelfwarden.models.finding import ProblemClass
from shelfwarden.models.ids import parse_guids
from shelfwarden.models.item import MediaKind, dump_item, load_item

from .conftest import SECTIONS, FakeLibrary, _movie

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSITION = (REPO_ROOT / COMPOSITION_FILE).read_bytes()


@pytest.fixture(scope="module")
def export_directory(tmp_path_factory):
    return run_export(FakeLibrary.build(), tmp_path_factory.mktemp("export"), count=200).directory


def _generate(export_directory, out, *, composition=COMPOSITION, curated=None, **kwargs):
    """Generate against the fixture export, with the composition and curated slices
    given as bytes rather than read from the checkout."""
    out.mkdir(parents=True, exist_ok=True)
    composition_path = out.parent / f"{out.name}-composition.toml"
    composition_path.write_bytes(composition)
    curated_root = REPO_ROOT / CURATED_ROOT
    if curated is not None:
        curated_root = out.parent / f"{out.name}-curated"
        curated_root.mkdir()
        for slice_, name in CURATED_FILES.items():
            curated_root.joinpath(name).write_bytes(curated.get(slice_, b"schema_version = 1\n"))
    return run_generate(
        export_directory,
        out / "dataset",
        count=kwargs.pop("count", 200),
        seed=kwargs.pop("seed", 1518),
        composition_path=composition_path,
        curated_root=curated_root,
        **kwargs,
    )


@pytest.fixture(scope="module")
def committed(export_directory, tmp_path_factory):
    """The fixture export, generated with everything as committed."""
    return _generate(export_directory, tmp_path_factory.mktemp("committed"))


# -- lineage ----------------------------------------------------------------


class TestLineage:
    def test_a_composition_edit_does_not_change_the_lineage_id(
        self, export_directory, committed, tmp_path
    ):
        """Moving a share does not make an older result untrue. Cases the new shares
        dropped are absent, cases they added are `new`, and every case in both keeps
        its id and its fingerprint -- so its history."""
        assert COMPOSITION.count(b"wrong_match = 0.25") == 1
        edited = _generate(
            export_directory,
            tmp_path / "edited",
            composition=COMPOSITION.replace(b"wrong_match = 0.25", b"wrong_match = 0.60"),
        )
        assert edited.dataset.lineage_id == committed.dataset.lineage_id
        assert edited.dataset.composition_id != committed.dataset.composition_id

        before = {case.case_id: case for case in committed.truth.cases}
        after = {case.case_id: case for case in edited.truth.cases}
        shared = before.keys() & after.keys()
        assert shared
        for identifier in shared:
            assert after[identifier].corruption_fingerprint == (
                before[identifier].corruption_fingerprint
            )

    def test_a_minor_generator_bump_keeps_the_lineage_and_a_major_one_resets_it(
        self, export_directory, committed, tmp_path
    ):
        """The lineage keys on the generator's major version: a minor bump that moves
        a corruption is the CI diff's `changed` bucket, not a new baseline."""
        version = committed.dataset.generator_version
        major = int(version.split(".")[0])
        minor = _generate(export_directory, tmp_path / "minor", generator_version=f"{major}.999.0")
        breaking = _generate(
            export_directory, tmp_path / "major", generator_version=f"{major + 1}.0.0"
        )
        assert minor.dataset.lineage_id == committed.dataset.lineage_id
        assert breaking.dataset.lineage_id != committed.dataset.lineage_id


# -- deficits ---------------------------------------------------------------


class TestDeficits:
    def test_an_empty_curated_slice_is_a_deficit_not_an_error(self, committed):
        """The committed curated files ship empty, and the dataset still generates."""
        assert not [case for case in committed.truth.cases if case.slice in CURATED]
        short = [row for row in committed.dataset.deficits if row.slice in CURATED]
        assert short, "the curated cells asked for cases and got none"

    def test_a_curated_cell_is_short_because_nobody_labelled_it(self, committed):
        """Never `not_implemented`. A curated slice does not use corruption functions,
        so whether one is registered says nothing about why `real.toml` holds no
        `foreign_title_variant` case -- and reporting it would point at step 1.1
        when the work waiting is step 0.9's."""
        rows = [row for row in committed.dataset.deficits if row.slice in CURATED]
        assert {row.reason for row in rows} == {DeficitReason.NOT_CURATED}
        uncorruptible = [row for row in rows if row.problem_class not in CORRUPTION_TABLE]
        assert uncorruptible, "the test needs a curated cell whose class has no corruption"

    def test_the_deficit_names_which_of_five_reasons_applies(self, committed):
        """Each row's reason is the one that accounts for it, and the whole
        breakdown travels in `detail`."""
        for row in committed.dataset.deficits:
            assert row.detail, row
            if row.slice in CURATED:
                assert row.reason is DeficitReason.NOT_CURATED, row
            elif row.problem_class is not None and row.problem_class not in CORRUPTION_TABLE:
                assert row.reason is DeficitReason.NOT_IMPLEMENTED, row
            else:
                assert row.reason in {
                    DeficitReason.NO_CANDIDATES,
                    DeficitReason.REJECTED,
                    DeficitReason.CAPPED,
                }, row
        observed = {row.reason for row in committed.dataset.deficits}
        assert {
            DeficitReason.NOT_CURATED,
            DeficitReason.NOT_IMPLEMENTED,
            DeficitReason.NO_CANDIDATES,
            DeficitReason.CAPPED,
        } <= observed

    def test_every_cell_the_composition_asked_for_is_accounted_for(self, committed):
        """Never silently unbalanced: a cell is filled, or it has a deficit row."""
        short = {
            (row.slice, row.media_kind, row.problem_class) for row in committed.dataset.deficits
        }
        for cell in committed.dataset.cells:
            key = (cell.slice, cell.media_kind, cell.problem_class)
            assert cell.achievable >= cell.intended or key in short, cell


# -- curated cases ----------------------------------------------------------

REAL_CASE = b"""
schema_version = 1

[[case]]
item_ids = ["fake:1:106"]
problem_class = "wrong_match"
notes = "Matched to a rip nobody can identify."

[case.expectation]
kind = "repair"

[[case.expectation.required_findings]]
problem_class = "wrong_match"
item_ids = ["fake:1:106"]
must_not_change = ["/parts/*/path"]

[case.expectation.required_findings.postcondition."fake:1:106"."/year"]
op = "equals"
value = 1999

[case.provenance]
method = "single_label"
"""


class TestCuratedCases:
    def test_a_curated_case_is_bound_to_the_export_by_the_generator(
        self, export_directory, tmp_path
    ):
        """A human writes the items, the class and the expectation. Everything
        mechanical -- media kind, subject, `case_id`, the ground-truth family -- is
        derived from the export, so a curated case cannot carry an identity that
        disagrees with the library it describes."""
        result = _generate(export_directory, tmp_path, curated={Slice.REAL: REAL_CASE})
        (case,) = [case for case in result.truth.cases if case.slice is Slice.REAL]

        assert case.problem_class is ProblemClass.WRONG_MATCH
        assert case.media_kind is MediaKind.MOVIE
        assert case.corruption_variant is None and case.witness is None
        assert case.case_id == case_id(
            slice_=Slice.REAL,
            problem_class=ProblemClass.WRONG_MATCH,
            media_kind=MediaKind.MOVIE,
            subject_key=str(case.subject_key),
            corruption_variant=None,
        )
        assert [str(item.item_id) for item in case.ground_truth] == ["fake:1:106"]
        assert isinstance(case.expectation, RepairExpectation)
        assert case.provenance.method == "single_label"

        cell = next(
            cell
            for cell in result.dataset.cells
            if (cell.slice, cell.media_kind, cell.problem_class)
            == (Slice.REAL, MediaKind.MOVIE, ProblemClass.WRONG_MATCH)
        )
        assert cell.achievable == 1

    def test_a_curated_case_naming_an_id_the_export_lacks_names_its_fix(
        self, export_directory, tmp_path
    ):
        """Rating keys move on rescan, so a curated case is addressed by live ids and
        goes stale on a re-export. Generation stops and says to re-point it; it does
        not ship a case bound to nothing."""
        stale = REAL_CASE.replace(b"fake:1:106", b"fake:1:99999")
        with pytest.raises(GenerateError, match=r"re-run the 0\.9 adjudication"):
            _generate(export_directory, tmp_path, curated={Slice.REAL: stale})


# -- build step 4: selection ------------------------------------------------
#
# Everything below is about *which* cases a dataset holds, and whether that
# answer moves when nothing about the case did.

TESTS_ROOT = str(Path(__file__).resolve().parent.parent)
EMPTY_CURATED = {slice_: curated_module.CuratedFile() for slice_ in CURATED_FILES}


def _inputs(library, directory):
    """What `generate` reads, from an export of `library`, read once."""
    export = run_export(library, directory, count=500).directory
    manifest, items, roots = read_export_with_population(export)
    return manifest, items, roots, build_screen(manifest, items, roots)


def _run(inputs, *, composition=COMPOSITION, count=200, **kwargs):
    manifest, items, roots, screen = inputs
    return generate(
        manifest=manifest,
        items=items,
        roots=roots,
        screen=screen,
        composition=parse_composition(composition),
        curated=kwargs.pop("curated", EMPTY_CURATED),
        count=count,
        seed=kwargs.pop("seed", 1518),
        **kwargs,
    )


@pytest.fixture(scope="module")
def fixture_inputs(tmp_path_factory):
    return _inputs(FakeLibrary.build(), tmp_path_factory.mktemp("inputs"))


def _ids(result):
    return {case.case_id for case in result.truth.cases}


def _movie_library(films, remakes=4):
    """A movie section big enough for cells to contest a subject.

    Every film is a candidate for five corruption classes and a should-not-touch
    case against a cap of three, so tickets are genuinely scarce -- which the
    11-family shared fixture cannot show. The first `remakes` titles are remade
    decades later, so `year_collision_remake` has partners to draw on.
    """
    records = [_movie(str(1000 + n), f"Film {n:02d}", 1960 + n) for n in range(films)]
    records += [_movie(str(2000 + n), f"Film {n:02d}", 2005 + n) for n in range(remakes)]
    return FakeLibrary(records={str(r.item_id): r for r in records}, sections_=SECTIONS[:1])


# Uneven on purpose: the shape under which largest-remainder rounding shrank a
# cell that had supply, measured at --count 17 on `_movie_library(40, remakes=12)`.
UNEVEN_MOVIES = b"""
[slices]
synthetic = 1
should_not_touch = 0.1
[media.movie]
share = 1
[media.movie.classes]
wrong_match = 0.15
year_collision_remake = 0.1
duplicate_quality = 0.2
filename_unmatchable = 0.45
"""

MOVIES_ONLY = b"""
[slices]
synthetic = 3
should_not_touch = 1
[media.movie]
share = 1
[media.movie.classes]
wrong_match = 1
year_collision_remake = 1
duplicate_quality = 1
filename_unmatchable = 1
alternate_cut = 1
"""


class TestReproducible:
    def test_a_dataset_is_byte_identical_across_hash_seeds(self, tmp_path):
        """The gate's first half. A same-process "generate it twice" cannot see
        hash-order leakage at all (practices §8.2), and the generator builds sets of
        subjects, tickets, and labels."""
        program = (
            "import sys, hashlib;"
            f"sys.path.insert(0, {TESTS_ROOT!r});"
            "from pathlib import Path;"
            "from evals.conftest import FakeLibrary;"
            "from shelfwarden.evals.export import run_export;"
            "from shelfwarden.evals.generate import run_generate;"
            "out = Path(sys.argv[1]);"
            "export = run_export(FakeLibrary.build(), out / 'e', count=200).directory;"
            "run_generate(export, out / 'd', count=200, seed=1518,"
            f" composition_path=Path({str(REPO_ROOT / COMPOSITION_FILE)!r}),"
            f" curated_root=Path({str(REPO_ROOT / CURATED_ROOT)!r}));"
            "digest = hashlib.sha256();"
            "[digest.update(p.name.encode() + p.read_bytes())"
            " for p in sorted((out / 'd').iterdir())];"
            "print(digest.hexdigest())"
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


class TestPrefixStability:
    @pytest.mark.parametrize("library", ["fixture", "movies"])
    def test_raising_the_count_adds_cases_without_moving_existing_ones(
        self, library, fixture_inputs, tmp_path
    ):
        """The gate's second half, at dataset scale. Targets are house-monotone
        (Webster), each cell selects by hash rank inside a window that only grows,
        and tickets are drawn before any target is read -- so a larger count is a
        superset of a smaller one, case for case, and nobody's history resets.

        The `movies` world is chosen so this test can fail: under the
        largest-remainder rounding `resolve` used first, it drops a real case at
        --count 17. The shared fixture cannot show that -- its shrinking cells are
        ones it has no supply for."""
        if library == "fixture":
            inputs, composition, counts = fixture_inputs, COMPOSITION, range(0, 241, 3)
        else:
            inputs = _inputs(_movie_library(40, remakes=12), tmp_path)
            composition, counts = UNEVEN_MOVIES, range(0, 61)
        previous = set()
        for count in counts:
            current = _ids(_run(inputs, composition=composition, count=count))
            assert previous <= current, f"--count {count} dropped {sorted(previous - current)}"
            previous = current
        assert previous, "the sweep never produced a case"

    def test_the_subject_cap_does_not_move_cases_when_the_count_rises(self, tmp_path):
        """Step 0.6, Decision 9. A cap consumed first-come in cell order moves a
        case from one cell to another when an earlier cell's target grows. Tickets
        drawn by hash rank before any target is read cannot. The library here is
        big enough that the cap actually bites, which the shared fixture is not."""
        inputs = _inputs(_movie_library(24), tmp_path)
        small = _run(inputs, composition=MOVIES_ONLY, count=30)
        large = _run(inputs, composition=MOVIES_ONLY, count=90)
        assert sum(cell.capped_away for cell in large.dataset.cells) > 0, "the cap never bit"
        assert _ids(small) <= _ids(large)

    def test_no_subject_hosts_more_cases_than_the_cap(self, tmp_path):
        inputs = _inputs(_movie_library(24), tmp_path)
        result = _run(inputs, composition=MOVIES_ONLY, count=120)
        per_subject = {}
        for case in result.truth.cases:
            per_subject[str(case.subject_key)] = per_subject.get(str(case.subject_key), 0) + 1
        assert max(per_subject.values()) <= MAX_CASES_PER_SUBJECT
        assert result.dataset.counts.subjects_covered == len(per_subject)

    def test_a_cell_does_not_depend_on_another_cell_s_target(self, fixture_inputs):
        """An unfillable cell is a deficit row, never a re-draw from a class that still
        has supply. Asking for far more remakes than the library holds must leave
        every other cell whose own target did not move exactly as it was."""
        assert COMPOSITION.count(b"year_collision_remake = 0.15") == 1
        greedy = COMPOSITION.replace(
            b"year_collision_remake = 0.15", b"year_collision_remake = 5.0"
        )
        before = _run(fixture_inputs)
        after = _run(fixture_inputs, composition=greedy)

        def by_cell(result):
            cells = {}
            for case in result.truth.cases:
                key = (case.slice, case.media_kind, case.problem_class)
                cells.setdefault(key, set()).add(case.case_id)
            return cells

        targets_before = {
            (c.slice, c.media_kind, c.problem_class): c.intended for c in before.dataset.cells
        }
        cases_before, cases_after = by_cell(before), by_cell(after)
        for cell in after.dataset.cells:
            key = (cell.slice, cell.media_kind, cell.problem_class)
            assert cell.achievable <= cell.intended, cell
            if targets_before[key] == cell.intended:
                assert cases_after.get(key, set()) == cases_before.get(key, set()), key

        remakes = next(
            row
            for row in after.dataset.deficits
            if (row.slice, row.media_kind, row.problem_class)
            == (Slice.SYNTHETIC, MediaKind.MOVIE, ProblemClass.YEAR_COLLISION_REMAKE)
        )
        assert remakes.achievable < remakes.intended


class TestIdentity:
    def test_case_ids_survive_a_re_export(self, fixture_inputs, tmp_path):
        """The property the CI gate rests on. Rating keys move on rescan, so the same
        library re-exported with every key moved must yield the same cases -- and the
        same corruptions, delta for delta once the keys are mapped back.

        Not asserted: `corruption_fingerprint`. It hashes the delta, which carries
        rating keys, so a rescan moves it for every case whose delta names an item
        (18 of 23 synthetic cases here). Recorded for 0.8's CI diff in the step plan."""
        library = FakeLibrary.build()
        moved = [load_item(_rekey(dump_item(record))) for record in library.records.values()]
        rescanned = FakeLibrary(records={str(r.item_id): r for r in moved}, sections_=SECTIONS)

        before = _run(fixture_inputs)
        after = _run(_inputs(rescanned, tmp_path))
        assert _ids(before) == _ids(after)

        deltas_after = dict(after.deltas)
        for case_id_, changes in before.deltas:
            mapped = [_rekey(change.model_dump(mode="json")) for change in changes]
            assert mapped == [change.model_dump(mode="json") for change in deltas_after[case_id_]]

    def test_generator_version_does_not_change_case_ids(self, fixture_inputs):
        """But it does change `corruption_fingerprint`, which is what feeds the CI
        diff's `changed` bucket rather than resetting the baseline."""
        first = _run(fixture_inputs, generator_version="0.1.0")
        second = _run(fixture_inputs, generator_version="0.2.0")
        assert _ids(first) == _ids(second)
        prints = {case.case_id: case.corruption_fingerprint for case in first.truth.cases}
        for case in second.truth.cases:
            if case.corruption_fingerprint is not None:
                assert case.corruption_fingerprint != prints[case.case_id]

    def test_a_colliding_subject_is_excluded_and_counted(self, tmp_path):
        """Step 0.6, Finding 2: two entries of one work share a guid, so they share a
        subject, and two cases on them could share one id. The subject is dropped
        before selection, listed in `excluded_subjects`, and -- per Decision 5 --
        counted in the cells it would have supplied, so the shortfall is attributed
        to it rather than to a library that simply lacks candidates."""
        heat = parse_guids("plex://movie/949", ["tmdb://949"])
        records = [_movie(str(100 + n), f"Film {n}", 1990 + n) for n in range(6)]
        records += [
            _movie("200", "Heat", 1995, guids=heat),
            _movie("201", "Heat", 1995, guids=heat),
        ]
        library = FakeLibrary(records={str(r.item_id): r for r in records}, sections_=SECTIONS[:1])
        result = _run(_inputs(library, tmp_path), composition=MOVIES_ONLY, count=40)

        (excluded,) = result.dataset.excluded_subjects
        assert excluded.subject_key == "external_id:tmdb://949"
        assert excluded.root_ids == ("fake:1:200", "fake:1:201")
        assert not [case for case in result.truth.cases if "949" in str(case.subject_key)]

        filenames = next(
            cell
            for cell in result.dataset.cells
            if cell.problem_class is ProblemClass.FILENAME_UNMATCHABLE
        )
        assert filenames.excluded_away == 2
        row = next(
            row
            for row in result.dataset.deficits
            if row.problem_class is ProblemClass.FILENAME_UNMATCHABLE
        )
        assert "2 excluded as non-unique" in row.detail

    def test_a_duplicate_case_id_raises_rather_than_being_disambiguated(
        self, export_directory, tmp_path
    ):
        """Two identical curated labels are one case written twice. An ordinal
        suffix would make identity positional again, so generation stops and names
        both."""
        with pytest.raises(GenerateError, match="two cases share"):
            _generate(
                export_directory, tmp_path, curated={Slice.REAL: REAL_CASE + _cases_only(REAL_CASE)}
            )

    def test_a_duplicate_curated_label_is_caught_even_when_the_cell_keeps_one(self, fixture_inputs):
        """Uniqueness is asserted on the curated pool, not only on what survives each
        cell's target. At a count where the cell keeps one case, the second copy was
        dropped as surplus and the duplicate went unnoticed."""
        cell = (Slice.REAL, MediaKind.MOVIE, ProblemClass.WRONG_MATCH)
        composition = parse_composition(COMPOSITION)
        count = next(
            n
            for n in range(1, 400)
            if next(
                c.intended
                for c in resolve(composition, n)
                if (c.slice, c.media_kind, c.problem_class) == cell
            )
            == 1
        )
        twice = curated_module.parse_curated(REAL_CASE + _cases_only(REAL_CASE), Slice.REAL)
        with pytest.raises(GenerateError, match="two cases share"):
            _run(fixture_inputs, count=count, curated={**EMPTY_CURATED, Slice.REAL: twice})


@pytest.fixture(scope="module")
def every_slice(export_directory, tmp_path_factory):
    """A dataset holding all four slices, and where it was written: generated cases,
    plus one curated repair case and one curated escalate case."""
    directory = tmp_path_factory.mktemp("every")
    result = _generate(
        export_directory,
        directory,
        curated={Slice.REAL: REAL_CASE, Slice.AMBIGUOUS: AMBIGUOUS_CASE},
    )
    return result, directory / "dataset"


class TestEveryCase:
    def test_the_fixture_dataset_holds_every_slice(self, every_slice):
        result, _ = every_slice
        assert {case.slice for case in result.truth.cases} == set(Slice)

    def test_unexpected_fail_is_the_default_on_every_case(self, every_slice):
        """Defect 1's regression, over a real dataset rather than a model default."""
        result, _ = every_slice
        for case in result.truth.cases:
            assert case.expectation.unexpected is Unexpected.FAIL, case.case_id

    def test_a_should_not_touch_case_has_no_class_and_still_has_a_unique_id(self, every_slice):
        """No class, no variant, no fingerprint, no witness, and a world that is the
        clean export -- and an id that no repair case on the same subject shares."""
        result, _ = every_slice
        untouched = [c for c in result.truth.cases if c.slice is Slice.SHOULD_NOT_TOUCH]
        assert untouched
        repaired = {
            str(c.subject_key): c.case_id
            for c in result.truth.cases
            if c.slice is not Slice.SHOULD_NOT_TOUCH
        }
        deltas = dict(result.deltas)
        for case in untouched:
            assert case.problem_class is None and case.corruption_variant is None
            assert case.corruption_fingerprint is None and case.witness is None
            assert deltas[case.case_id] == ()
            assert case.case_id != repaired.get(str(case.subject_key))

    def test_the_screen_and_the_truth_file_agree_about_guarded_classes(
        self, every_slice, fixture_inputs
    ):
        """A should-not-touch label is the clean screen's own verdict, unmodified:
        guarded, all three buckets, and the item it was taken of."""
        result, _ = every_slice
        screens = {row.item_id: row for row in fixture_inputs[3].items}
        for case in result.truth.cases:
            if case.slice is not Slice.SHOULD_NOT_TOUCH:
                continue
            (item_id,) = case.item_ids
            screen = screens[item_id]
            assert screen.verdict is Verdict.GUARDED
            assert case.expectation.guarded_classes == screen.guarded_classes
            assert case.expectation.trivially_guarded_classes == screen.trivially_guarded_classes
            assert case.expectation.unguarded_classes == screen.unguarded_classes

    def test_every_case_has_exactly_one_delta_and_it_reverses(self, every_slice):
        """0.7's contract: `SnapshotLibrary.for_case` composes a case's world from the
        clean export and its line in `deltas.jsonl`, and needs no special case for a
        world that is just the clean export."""
        result, directory = every_slice
        payload = (directory / DELTAS_FILE).read_bytes()
        lines = [json.loads(line) for line in payload.splitlines()]
        assert [line["case_id"] for line in lines] == sorted(_ids(result))

        by_id = {case.case_id: case for case in result.truth.cases}
        for line in lines:
            case = by_id[line["case_id"]]
            changes = [ItemChange.model_validate(change) for change in line["changes"]]
            if case.slice is not Slice.SYNTHETIC:
                assert changes == []
                continue
            corrupted = apply_changes(case.ground_truth, changes)
            assert render_family(corrupted) != render_family(case.ground_truth)
            restored = apply_reverse(corrupted, changes)
            assert render_family(restored) == render_family(case.ground_truth)

    def test_the_truth_file_is_sorted_and_is_what_was_rendered(self, every_slice):
        """Sorted by `case_id`, so its bytes do not depend on selection order."""
        result, directory = every_slice
        ids = [case.case_id for case in result.truth.cases]
        assert ids == sorted(ids)
        payload = (directory / TRUTH_FILE).read_bytes()
        assert payload == render_truth(result.truth)
        assert render_truth(load_truth(payload)) == payload

    def test_the_dataset_file_reads_back_as_what_was_rendered(self, every_slice):
        """Written without nulls, so every `None`-able field must be defaulted to
        parse again. A should-not-touch cell's `problem_class` was not: nothing read
        `dataset.json` back until the world builder (step 0.7.6) did, and failed."""
        result, directory = every_slice
        payload = (directory / DATASET_FILE).read_bytes()
        assert payload == render_dataset(result.dataset)
        assert any(cell.problem_class is None for cell in result.dataset.cells)
        assert Dataset.model_validate_json(payload) == result.dataset


def _cases_only(payload):
    """A curated file's cases without its header, so two can be concatenated."""
    return payload.replace(b"schema_version = 1\n", b"")


def _rekey(value):
    """A dumped value with every rating key moved, as a rescan would move it."""
    if isinstance(value, dict):
        if set(value) == {"provider", "section_id", "rating_key"} and value["rating_key"].isdigit():
            return {**value, "rating_key": str(int(value["rating_key"]) + 5000)}
        return {key: _rekey(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rekey(item) for item in value]
    if isinstance(value, str):
        provider, _, rest = value.partition(":")
        section, _, key = rest.partition(":")
        if provider == "fake" and section and key.isdigit():
            return f"{provider}:{section}:{int(key) + 5000}"
    return value


AMBIGUOUS_CASE = b"""
schema_version = 1

[[case]]
item_ids = ["fake:1:107", "fake:1:108"]
problem_class = "wrong_match"
notes = "Two Blade Runner entries. Which cut each file is cannot be told from the library."

[case.expectation]
kind = "escalate"

[[case.expectation.acceptable_resolutions]]
relation = "same_work"
item_ids = ["fake:1:107", "fake:1:108"]

[case.provenance]
method = "single_label"
"""
