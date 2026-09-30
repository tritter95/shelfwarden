"""The two slices a generator cannot synthesize.

Step 0.6.5's gate: an empty curated file yields an empty slice, not an error. The
rest defends the property that makes curated cases worth merging at all -- they
are written in **the same vocabulary** as generated ones, so the scorer cannot
tell them apart and a hand-written file cannot reopen a defect the generated
cases closed.
"""

from pathlib import Path

import pytest

from shelfwarden.evals.curated import (
    CURATED_FILES,
    CURATED_ROOT,
    CuratedError,
    load_curated,
    parse_curated,
)
from shelfwarden.evals.truth import EscalateExpectation, RepairExpectation, Slice

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMITTED = REPO_ROOT / CURATED_ROOT


def _documented_example(slice_):
    """The `#   `-indented example case in a committed file's comment, uncommented.

    Step 0.9's labellers will copy these. An example that no longer validates
    teaches the wrong format to exactly the people writing the real slice.
    """
    lines = (COMMITTED / CURATED_FILES[slice_]).read_text().splitlines()
    block = [line[4:] for line in lines if line.startswith("#   ")]
    return ("schema_version = 1\n" + "\n".join(block)).encode()


class TestTheCommittedFiles:
    @pytest.mark.parametrize("slice_", sorted(CURATED_FILES))
    def test_an_empty_curated_file_is_an_empty_slice_not_an_error(self, slice_):
        """Both ship empty until 0.9. The generator turns the emptiness into a
        `not_curated` deficit row, which is the honest report: nobody has labelled
        any yet, which is not the same as the library having no such problems."""
        assert load_curated(slice_, COMMITTED).cases == ()

    @pytest.mark.parametrize(
        ("slice_", "expectation"),
        [(Slice.REAL, RepairExpectation), (Slice.AMBIGUOUS, EscalateExpectation)],
    )
    def test_the_documented_example_validates(self, slice_, expectation):
        (case,) = parse_curated(_documented_example(slice_), slice_).cases
        assert isinstance(case.expectation, expectation)


class TestTheSameVocabulary:
    def test_silence_cannot_be_written_into_an_escalate_case(self):
        """Defect 2, through the curated door. The model refuses it, so the file does."""
        payload = b"""
schema_version = 1
[[case]]
item_ids = ["fake:3:411"]
problem_class = "anthology_omnibus"
[case.expectation]
kind = "escalate"
require_finding = false
[case.provenance]
method = "single_label"
"""
        with pytest.raises(CuratedError, match="does not validate"):
            parse_curated(payload, Slice.AMBIGUOUS)

    def test_a_misspelled_predicate_is_refused_rather_than_evaluating_nothing(self):
        """`op` is an enum for exactly this: a typo would otherwise parse as a
        predicate name the scorer does not know, and gate nothing."""
        payload = b"""
schema_version = 1
[[case]]
item_ids = ["fake:1:101"]
problem_class = "wrong_match"
[case.expectation]
kind = "repair"
[[case.expectation.required_findings]]
problem_class = "wrong_match"
item_ids = ["fake:1:101"]
[case.expectation.required_findings.postcondition."fake:1:101"."/year"]
op = "equal"
value = 2001
[case.provenance]
method = "single_label"
"""
        with pytest.raises(CuratedError, match="does not validate"):
            parse_curated(payload, Slice.REAL)

    def test_a_curated_case_defaults_to_unexpected_fail(self):
        (case,) = parse_curated(_documented_example(Slice.REAL), Slice.REAL).cases
        assert case.expectation.unexpected == "fail"


class TestRefusals:
    def test_a_missing_file_says_to_restore_it(self, tmp_path):
        """A missing file means an incomplete checkout, not an empty queue."""
        with pytest.raises(CuratedError, match="restore it"):
            load_curated(Slice.REAL, tmp_path)

    def test_a_slice_that_is_not_curated_is_refused(self):
        with pytest.raises(CuratedError, match="not a curated slice"):
            load_curated(Slice.SYNTHETIC, COMMITTED)

    @pytest.mark.parametrize(
        ("payload", "match"),
        [
            (b"schema_version = [", "not readable TOML"),
            (b"schema_version = 1\n[extra]\n", "unexpected table"),
            (b"schema_version = 2\n", "schema_version 2"),
        ],
        ids=["unparseable", "extra-table", "future-version"],
    )
    def test_a_malformed_file_names_its_fix(self, payload, match):
        with pytest.raises(CuratedError, match=match):
            parse_curated(payload, Slice.REAL)
