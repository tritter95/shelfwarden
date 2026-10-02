"""Suite-wide hooks.

A `live` test touches a real external service, and the marker's own description
says it never runs in CI. Until step 0.7.1 nothing enforced that. The nightly job
runs the suite without `-m "not slow"` so that `slow` tests run, which would have
run every `live` test too -- harmless only because none existed yet.

So a `live` test is now opt-in in two places. Here it is skipped unless `--run-live`
is given, and both CI jobs deselect it with `-m "not live"`. A skip rather than a
deselect locally, so the count of what did not run stays in the summary line.
"""

import pytest

# `pytester` runs a nested pytest session; `tests/test_live_marker.py` uses it to
# prove the hook below, which no ordinary test can observe from inside the session
# it governs.
pytest_plugins = ["pytester"]

RUN_LIVE = "--run-live"

# What a bounded `live` run actually covered. A live library is too large to walk
# whole, so the conformance suite reads each listing over a window and records
# what that window held; the summary prints it, so the bound is never a silent cap.
LIVE_COVERAGE = pytest.StashKey[list[str]]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        RUN_LIVE,
        action="store_true",
        default=False,
        help="run tests marked `live`, which touch a real external service (never in CI)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption(RUN_LIVE):
        return
    skip = pytest.mark.skip(reason=f"touches a real external service; pass {RUN_LIVE} to run it")
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip)


def pytest_configure(config: pytest.Config) -> None:
    config.stash[LIVE_COVERAGE] = []


def pytest_terminal_summary(terminalreporter, exitstatus: int, config: pytest.Config) -> None:
    covered = config.stash.get(LIVE_COVERAGE, [])
    if covered:
        terminalreporter.section("live coverage")
        for line in covered:
            terminalreporter.write_line(line)
