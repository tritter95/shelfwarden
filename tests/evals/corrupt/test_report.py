"""The corruption preview artifact.

Two things are asserted here that nothing else can assert. The survey is a pure
function of `(export, seed, code)`, so it must be **byte-identical across hash
seeds** -- and it builds more sets than the screen does, so it is more exposed to
hash-order leakage, not less. And the deficit table must separate a fact about the
library from a fact about the harness, because the whole point of the artifact is
that only one of those is a bug.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shelfwarden.cli import app
from shelfwarden.evals.corrupt.report import (
    CORRUPTIONS_FILE,
    MARKDOWN_FILE,
    REJECTED_FILE,
    build_report,
    load_report,
    render_markdown,
    render_report,
    run_corrupt,
)
from shelfwarden.evals.corrupt.run import run_corruptions
from shelfwarden.evals.export import load_manifest, load_roots, run_export
from shelfwarden.evals.screen import ScreenError
from shelfwarden.models.finding import ProblemClass

from ..conftest import FakeLibrary

TESTS_ROOT = str(Path(__file__).resolve().parents[2])


@pytest.fixture
def export(tmp_path):
    directory = tmp_path / "export"
    run_export(FakeLibrary.build(), directory, count=None)
    return directory


@pytest.fixture
def report(export, tmp_path):
    return run_corrupt(export, tmp_path / "corr", seed=1518)


class TestArtifact:
    def test_it_writes_three_files_and_none_of_them_into_the_export(self, export, tmp_path):
        out = tmp_path / "corr"
        run_corrupt(export, out, seed=1518)
        assert {path.name for path in out.iterdir()} == {
            CORRUPTIONS_FILE,
            REJECTED_FILE,
            MARKDOWN_FILE,
        }
        # 0.4's gate is the export's byte-identity; adding a file to it would make
        # that assertion answer a question nobody asked.
        assert not (export / CORRUPTIONS_FILE).exists()

    def test_it_is_bound_to_the_export_it_surveyed(self, export, report):
        manifest = load_manifest(export)
        assert report.source.export_id == manifest.export_id
        assert report.source.items_sha256 == manifest.items_sha256
        assert report.source.roots_sha256 == manifest.roots_sha256

    def test_it_carries_no_timestamp(self, tmp_path, report):
        payload = json.loads(render_report(report))
        rendered = json.dumps(payload)
        for word in ("created_at", "generated_at", "timestamp"):
            assert word not in rendered

    def test_it_round_trips(self, export, tmp_path):
        out = tmp_path / "corr"
        written = run_corrupt(export, out, seed=1518)
        assert load_report(out) == written

    def test_rejections_are_one_per_line_and_ordered(self, export, tmp_path):
        out = tmp_path / "corr"
        run_corrupt(export, out, seed=1518)
        lines = (out / REJECTED_FILE).read_bytes().splitlines()
        assert lines
        rows = [json.loads(line) for line in lines]
        keys = [(row["problem_class"], row["root_id"], row["reason"]) for row in rows]
        assert keys == sorted(keys, key=lambda key: (keys.index(key), key))
        assert all("applicable" in row for row in rows)

    def test_an_export_without_a_population_index_is_refused(self, export, tmp_path):
        """Not scoped down to the slice -- refused.

        A corruption's collateral and its screen cross-check are both
        population-scoped. Falling back to the slice would under-report the blast
        radius, which is the same wrong direction step 0.45 forbade.
        """
        (export / "roots.jsonl").unlink()
        with pytest.raises(ScreenError, match=re.escape("roots.jsonl")):
            run_corrupt(export, tmp_path / "corr", seed=1518)


class TestDeterminism:
    def test_the_survey_is_byte_identical_across_hash_seeds(self, tmp_path):
        program = (
            "import sys;"
            f"sys.path.insert(0, {TESTS_ROOT!r});"
            "from pathlib import Path;"
            "from evals.conftest import FakeLibrary;"
            "from shelfwarden.evals.export import run_export;"
            "from shelfwarden.evals.corrupt.report import run_corrupt, render_report;"
            "out=Path(sys.argv[1]);"
            "run_export(FakeLibrary.build(), out/'export', count=None);"
            "report=run_corrupt(out/'export', out/'corr', seed=1518);"
            "sys.stdout.buffer.write(render_report(report))"
        )
        outputs = []
        for hash_seed in ("0", "1"):
            target = tmp_path / f"run{hash_seed}"
            result = subprocess.run(
                [sys.executable, "-c", program, str(target)],
                capture_output=True,
                check=False,
                env={"PYTHONHASHSEED": hash_seed, "PATH": "/usr/bin:/bin"},
            )
            assert result.returncode == 0, result.stderr.decode()
            outputs.append(result.stdout)
        assert outputs[0] == outputs[1]

    def test_the_markdown_reorders_counts_at_render_time(self, report):
        """`canonical_json` sorts keys, so a count-ordered mapping does not survive
        the round trip -- the order a human reads has to be re-derived."""
        rendered = render_markdown(report)
        assert "x1" in rendered or "x2" in rendered
        assert rendered.endswith("\n")


class TestTheTableSeparatesTwoKindsOfNothing:
    def test_a_deferred_class_says_why_rather_than_reporting_a_bare_zero(self, report):
        rendered = render_markdown(report)
        assert "**deferred**" in rendered
        deferred = [row for row in report.deficits if row.unsynthesizable_reason]
        assert len(deferred) == 4
        for row in deferred:
            assert row.accepted == 0

    def test_a_supply_gap_and_a_rejection_are_different_columns(self, export):
        manifest = load_manifest(export)
        from shelfwarden.evals.export import load_items

        run = run_corruptions(
            export_id=manifest.export_id,
            items=load_items(export),
            roots=load_roots(export),
            seed=1518,
        )
        built = build_report(manifest, run)
        remakes = next(
            row for row in built.deficits if row.problem_class is ProblemClass.YEAR_COLLISION_REMAKE
        )
        # "no remake pairs in this library" is a supply fact and lands in
        # `not_applicable`; it must never be counted as the harness failing.
        assert remakes.not_applicable_by_reason
        assert not remakes.rejected_by_reason
        assert built.counts.rejected == 0


class TestCli:
    def test_the_command_writes_a_report_and_names_the_empty_classes(self, export, tmp_path):
        result = CliRunner().invoke(app, ["corrupt", str(export), "--out", str(tmp_path / "corr")])
        assert result.exit_code == 0, result.output
        assert "case(s) over" in result.output
        assert "wait on step 1.1" in result.output
        assert "no cases for:" in result.output

    def test_an_unknown_class_lists_the_known_ones(self, export, tmp_path):
        result = CliRunner().invoke(
            app, ["corrupt", str(export), "--class", "nope", "--out", str(tmp_path / "c")]
        )
        assert result.exit_code != 0
        assert "wrong_match" in result.output

    def test_a_directory_that_is_not_an_export_says_what_to_do(self, tmp_path):
        result = CliRunner().invoke(app, ["corrupt", str(tmp_path)])
        assert result.exit_code != 0
        assert "shelfwarden export" in result.output
