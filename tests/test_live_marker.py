"""The `live` marker is opt-in; nothing proved it.

The hook lives in `tests/conftest.py`. These tests run it in a nested session over
two throwaway tests, one marked `live`, using the conftest's own text so the hook
under test is the hook the suite runs.
"""

from pathlib import Path

import pytest

CONFTEST = Path(__file__).parent / "conftest.py"

SAMPLE = """
import pytest

@pytest.mark.live
def test_reaches_out():
    pass

def test_stays_home():
    pass
"""


# The nested session auto-loads every installed plugin, pytest-asyncio included,
# which warns at configure time unless its loop scope is set -- and the outer
# session's `filterwarnings = error` makes that warning fatal. Set as in
# pyproject.toml.
INI = """
[pytest]
addopts = --strict-markers
asyncio_default_fixture_loop_scope = function
markers =
    live: touches a real external service
"""


@pytest.fixture
def session(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeconftest(CONFTEST.read_text(encoding="utf-8"))
    pytester.makeini(INI)
    pytester.makepyfile(SAMPLE)
    return pytester


def test_a_live_test_is_skipped_by_default(session):
    result = session.runpytest("-rs")
    result.assert_outcomes(passed=1, skipped=1)
    result.stdout.fnmatch_lines(["*pass --run-live to run it*"])


def test_run_live_runs_it(session):
    session.runpytest("--run-live").assert_outcomes(passed=2)


def test_ci_deselects_it_without_the_flag(session):
    """What both CI jobs do. A deselect, so it cannot reach a server even if the
    skip hook were removed."""
    session.runpytest("-m", "not live").assert_outcomes(passed=1, deselected=1)
