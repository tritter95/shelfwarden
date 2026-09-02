import pytest
from typer.testing import CliRunner

from shelfwarden import __version__, cli, config
from shelfwarden.cli import ExitCode, app
from shelfwarden.evals import export as export_module
from shelfwarden.library.base import LibraryUnavailable
from shelfwarden.library.plex import effective_request_params
from shelfwarden.models.item import FetchProfile
from tests.evals.conftest import FakeLibrary

runner = CliRunner()

TOP_LEVEL_COMMANDS = ["export", "screen", "scan", "diff", "apply", "revert", "eval", "db"]


def test_help_lists_every_top_level_command():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == ExitCode.OK
    for command in TOP_LEVEL_COMMANDS:
        assert command in result.output


def test_version_prints_the_package_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == ExitCode.OK
    assert __version__ in result.output


def test_no_arguments_shows_help():
    result = runner.invoke(app, [])
    assert "Usage:" in result.output


@pytest.mark.parametrize(
    ("command", "roadmap_step"),
    [
        (["scan"], "1.6"),
        (["diff"], "3"),
        (["apply"], "3"),
        (["revert", "plan-abc"], "3"),
        (["eval", "run"], "1.7"),
        (["eval", "score"], "0.8"),
    ],
)
def test_stub_exits_not_implemented_and_names_its_roadmap_step(command, roadmap_step):
    result = runner.invoke(app, command)
    assert result.exit_code == ExitCode.NOT_IMPLEMENTED
    assert "not implemented" in result.output
    assert roadmap_step in result.output


def test_apply_defaults_to_dry_run():
    result = runner.invoke(app, ["apply"])
    assert "dry run" in result.output
    assert "COMMIT" not in result.output


def test_apply_commit_flag_is_parsed():
    result = runner.invoke(app, ["apply", "--commit"])
    assert "COMMIT" in result.output


def test_unknown_command_is_a_usage_error():
    result = runner.invoke(app, ["nonsense"])
    assert result.exit_code == 2


def test_revert_requires_a_plan_id():
    result = runner.invoke(app, ["revert"])
    assert result.exit_code == 2


def test_db_migrate_then_status(tmp_path):
    store = tmp_path / "store.db"

    migrated = runner.invoke(app, ["--db", str(store), "db", "migrate"])
    assert migrated.exit_code == ExitCode.OK
    assert "0001" in migrated.output

    again = runner.invoke(app, ["--db", str(store), "db", "migrate"])
    assert again.exit_code == ExitCode.OK
    assert "up to date" in again.output

    status = runner.invoke(app, ["--db", str(store), "db", "status"])
    assert status.exit_code == ExitCode.OK
    assert "blobs" in status.output


def test_db_status_on_a_fresh_store(tmp_path):
    result = runner.invoke(app, ["--db", str(tmp_path / "fresh.db"), "db", "status"])
    assert result.exit_code == ExitCode.OK
    assert "no migrations applied" in result.output


# -- export (step 0.4) ----------------------------------------------------


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """No environment variable and no real config file reaches these tests.

    `shelfwarden export` resolves settings from the environment and from
    `~/.config/shelfwarden/config.toml`. Left alone, a developer with a working
    Plex token exported would run a different test than CI does -- and the one
    that could actually reach a server.
    """
    for name in (
        "SHELFWARDEN_PLEX_URL",
        "SHELFWARDEN_PLEX_TOKEN",
        "PLEX_URL",
        "PLEX_TOKEN",
        "SHELFWARDEN_EXPORT_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", tmp_path / "absent.toml")
    return monkeypatch


@pytest.fixture
def configured(isolated_config):
    isolated_config.setenv("SHELFWARDEN_PLEX_URL", "http://plex.invalid:32400")
    isolated_config.setenv("SHELFWARDEN_PLEX_TOKEN", "xxTOKENxx")
    return isolated_config


@pytest.fixture
def fake_plex(configured):
    """Substitute the provider, not the export. The wiring is what is under test."""
    built: list[tuple] = []

    def factory(*args, **kwargs):
        built.append((args, kwargs))
        return FakeLibrary.build()

    configured.setattr(cli, "PlexLibrary", factory)
    return built


class TestExportConfiguration:
    def test_it_refuses_without_a_url_and_a_token(self, isolated_config, tmp_path):
        result = runner.invoke(app, ["export", "--out", str(tmp_path / "e")])
        assert result.exit_code == ExitCode.ERROR
        assert not (tmp_path / "e").exists()

    def test_the_refusal_names_the_variables_to_set(self, isolated_config, tmp_path):
        """A correctable error that does not name a next action is a bug."""
        result = runner.invoke(app, ["export", "--out", str(tmp_path / "e")])
        assert "SHELFWARDEN_PLEX_URL" in result.output
        assert "SHELFWARDEN_PLEX_TOKEN" in result.output

    def test_there_is_no_way_to_pass_a_token_on_the_command_line(self):
        """argv lands in shell history and in every process listing on the box,
        so the absence of the flag is the feature."""
        result = runner.invoke(app, ["export", "--help"])
        assert "--token" not in result.output
        assert "--url" not in result.output

    def test_the_url_and_token_reach_the_provider(self, fake_plex, tmp_path):
        runner.invoke(app, ["export", "--out", str(tmp_path / "e")])
        ((args, _),) = fake_plex
        assert args == ("http://plex.invalid:32400", "xxTOKENxx")


class TestExportCommand:
    def test_it_writes_the_five_files(self, fake_plex, tmp_path):
        """`roots.jsonl` joined the set in step 0.45 -- the population index the
        screen's uniqueness predicates need. See export.render_roots."""
        out = tmp_path / "e"
        result = runner.invoke(app, ["export", "--out", str(out)])
        assert result.exit_code == ExitCode.OK, result.output
        assert sorted(path.name for path in out.iterdir()) == [
            "census.json",
            "census.md",
            "items.jsonl",
            "manifest.json",
            "roots.jsonl",
        ]

    def test_census_only_fetches_no_items(self, fake_plex, tmp_path):
        out = tmp_path / "e"
        result = runner.invoke(app, ["export", "--census-only", "--out", str(out)])
        assert result.exit_code == ExitCode.OK
        assert "Census only" in result.output
        assert (out / "items.jsonl").read_bytes() == b""

    def test_the_defaults_match_the_documented_ones(self, fake_plex, tmp_path):
        out = tmp_path / "e"
        runner.invoke(app, ["export", "--out", str(out)])
        manifest = export_module.load_manifest(out)
        assert manifest.selection.seed == export_module.DEFAULT_SEED
        assert manifest.selection.requested_roots == export_module.DEFAULT_ROOTS
        assert manifest.selection.max_records == export_module.DEFAULT_MAX_RECORDS
        assert manifest.profile is FetchProfile.CORE

    def test_all_overrides_count(self, fake_plex, tmp_path):
        out = tmp_path / "e"
        runner.invoke(app, ["export", "--all", "--count", "2", "--out", str(out)])
        manifest = export_module.load_manifest(out)
        assert manifest.selection.mode == "all"
        assert manifest.selection.requested_roots is None

    def test_seed_and_count_reach_the_manifest(self, fake_plex, tmp_path):
        out = tmp_path / "e"
        runner.invoke(app, ["export", "--count", "3", "--seed", "99", "--out", str(out)])
        manifest = export_module.load_manifest(out)
        assert (manifest.selection.seed, manifest.selection.requested_roots) == (99, 3)

    def test_section_is_repeatable(self, fake_plex, tmp_path):
        out = tmp_path / "e"
        runner.invoke(app, ["export", "--section", "1", "--section", "2", "--out", str(out)])
        manifest = export_module.load_manifest(out)
        assert {row.section_id for row in manifest.sections} == {"1", "2"}

    def test_the_effective_request_params_are_recorded_not_the_overrides(self, fake_plex, tmp_path):
        """Finding 1: `RELOAD_INCLUDES` understates what plexapi actually sends."""
        out = tmp_path / "e"
        runner.invoke(app, ["export", "--out", str(out)])
        params = export_module.load_manifest(out).request_params
        assert params == effective_request_params(FetchProfile.CORE)
        assert params["includeFields"] == "thumbBlurHash,artBlurHash"

    def test_full_profile_is_recorded_and_adds_check_files(self, fake_plex, tmp_path):
        out = tmp_path / "e"
        runner.invoke(app, ["export", "--profile", "full", "--out", str(out)])
        manifest = export_module.load_manifest(out)
        assert manifest.profile is FetchProfile.FULL
        assert manifest.request_params["checkFiles"] == "1"

    def test_a_listing_profile_is_not_on_offer(self, fake_plex, tmp_path):
        """STUB describes what a listing returned, not something an operator can
        ask the server for. Offering it in --help would advertise a dead choice."""
        assert "stub" not in runner.invoke(app, ["export", "--help"]).output
        result = runner.invoke(app, ["export", "--profile", "stub", "--out", str(tmp_path / "e")])
        assert result.exit_code == 2


class TestExportReporting:
    def test_skipped_sections_are_reported_with_their_reason(self, fake_plex, tmp_path):
        """A silent skip is how a library quietly exports two thirds of itself."""
        result = runner.invoke(app, ["export", "--out", str(tmp_path / "e")])
        assert "skipped 4" in result.output
        assert "not audiobooks" in result.output
        assert "skipped 5" in result.output

    def test_dropped_families_are_reported(self, fake_plex, tmp_path):
        """ "No silent caps": a truncated export must not read as full coverage."""
        result = runner.invoke(app, ["export", "--max-records", "9", "--out", str(tmp_path / "e")])
        assert "dropped 'Cowboy Bebop'" in result.output
        assert "max_records" in result.output

    def test_it_reports_roots_and_records_separately(self, fake_plex, tmp_path):
        result = runner.invoke(app, ["export", "--out", str(tmp_path / "e")])
        assert "root(s)" in result.output
        assert "record(s)" in result.output

    def test_it_points_at_the_census_a_human_reads(self, fake_plex, tmp_path):
        result = runner.invoke(app, ["export", "--out", str(tmp_path / "e")])
        assert "census.md" in result.output

    def test_no_output_line_carries_the_token(self, fake_plex, tmp_path):
        result = runner.invoke(app, ["export", "--out", str(tmp_path / "e")])
        assert "xxTOKENxx" not in result.output


class TestExportFailure:
    def test_a_library_error_aborts_with_error_and_writes_nothing(self, configured, tmp_path):
        """A partial export that looks complete is the worst artifact this command
        could produce, so an unavailable server writes no directory at all."""

        def factory(*args, **kwargs):
            raise LibraryUnavailable("the server is down")

        configured.setattr(cli, "PlexLibrary", factory)
        out = tmp_path / "e"
        result = runner.invoke(app, ["export", "--out", str(out)])
        assert result.exit_code == ExitCode.ERROR
        assert "nothing written" in result.output
        assert not out.exists()

    def test_an_export_error_aborts_with_error(self, fake_plex, tmp_path):
        out = tmp_path / "e"
        result = runner.invoke(app, ["export", "--section", "999", "--out", str(out)])
        assert result.exit_code == ExitCode.ERROR
        assert "no supported sections" in result.output
        assert not out.exists()


# -- screen (step 0.45) ---------------------------------------------------


@pytest.fixture
def export_dir(tmp_path):
    """A real export, written offline through the same code path the CLI uses."""
    return export_module.run_export(FakeLibrary.build(), tmp_path / "export", count=200).directory


class TestScreenCommand:
    def test_it_writes_a_screen_beside_the_export_not_inside_it(self, export_dir, tmp_path):
        out = tmp_path / "screen"
        result = runner.invoke(app, ["screen", str(export_dir), "--out", str(out)])
        assert result.exit_code == ExitCode.OK, result.output
        assert sorted(path.name for path in out.iterdir()) == ["screen.json", "screen.md"]
        assert not (export_dir / "screen.json").exists()

    def test_it_reports_the_three_verdicts_and_the_authority_tier(self, export_dir, tmp_path):
        result = runner.invoke(app, ["screen", str(export_dir), "--out", str(tmp_path / "screen")])
        assert "guarded" in result.output
        assert "insufficient" in result.output
        assert "authority tier is 'none'" in result.output

    def test_screening_a_census_only_export_names_its_next_action(self, tmp_path):
        """practices §5.4: a correctable error says what to do instead."""
        directory = export_module.run_export(
            FakeLibrary.build(), tmp_path / "census", census_only=True
        ).directory
        result = runner.invoke(app, ["screen", str(directory), "--out", str(tmp_path / "screen")])
        assert result.exit_code == ExitCode.ERROR
        assert "--census-only" in result.output
        assert "shelfwarden export" in result.output

    def test_a_directory_that_is_not_an_export_is_an_error_not_a_traceback(self, tmp_path):
        (tmp_path / "empty").mkdir()
        result = runner.invoke(app, ["screen", str(tmp_path / "empty")])
        assert result.exit_code == ExitCode.ERROR
        assert "not an export directory" in result.output

    def test_a_missing_population_index_is_reported_rather_than_absorbed(
        self, export_dir, tmp_path
    ):
        (export_dir / export_module.ROOTS_FILE).unlink()
        result = runner.invoke(app, ["screen", str(export_dir), "--out", str(tmp_path / "screen")])
        assert result.exit_code == ExitCode.OK, result.output
        assert "no roots.jsonl" in result.output
