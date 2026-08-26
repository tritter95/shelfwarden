import pytest
from typer.testing import CliRunner

from shelfwarden import __version__
from shelfwarden.cli import ExitCode, app

runner = CliRunner()

TOP_LEVEL_COMMANDS = ["export", "scan", "diff", "apply", "revert", "eval", "db"]


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
        (["export"], "0.4"),
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
