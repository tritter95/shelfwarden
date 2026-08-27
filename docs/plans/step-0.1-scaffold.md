# Step 0.1 — Completing the Scaffold

Implementation plan for the remainder of roadmap step **0.1 Project scaffold**.
Written 2026-08-26 against commit `36c5641`. Every claim in
[Verified findings](#2-verified-findings) was produced by running the tool, not by
recollection — the exact commands are shown so they can be re-run.

> **Status: implemented 2026-08-26.** All three decisions were taken as
> recommended — the full package skeleton, strict contracts with no
> `ignore_imports` until the seam exists, and the fifth `library`/`sources`
> contract included. Every contract was breached deliberately and seen to fail
> before being restored. The document is kept as the record of *why* the
> configuration looks the way it does; §2 is the part worth re-reading before
> touching `[tool.importlinter]` again.
>
> One clause is dormant by design: `agent/tools/` → `agent.loop` cannot fire until
> step 1.6 creates `loop.py`, because grimp records no edge to a module that does
> not exist (finding 4). It arms itself the moment the file appears — no action
> needed. The other two clauses of that contract (`agent.provider`, `evals`) are
> live now and were verified BROKEN against a deliberate breach.

**Gate for the step (unchanged, from `roadmap.md` and `implementation-plan.md` §7):**
`uv run shelfwarden --help` works, a migration applies cleanly, and `lint-imports`
passes **in CI**.

---

## 1. Where this stands

| Roadmap bullet | State | Evidence |
|---|---|---|
| `uv init`, `.python-version` → 3.13, `src/` layout, `[project.scripts]` | done | `uv run shelfwarden --help` exits 0 |
| Typer CLI skeleton — `scan / diff / apply / revert / eval / export` | done | `tests/test_cli.py`, 29 tests green |
| SQLite store: WAL, explicit `autocommit=False`, migration runner | done | `tests/store/test_db.py`; `db migrate` → `db status` → `db migrate` is idempotent |
| pytest + pytest-asyncio, ruff configured and passing | **partly** | ruff, format, and pytest are green locally; `asyncio_mode="auto"` is configured but **no async test exercises it**, and there is **no CI** |
| `[tool.importlinter]` contracts | **not started** | `[tool.importlinter]` header exists with `include_external_packages = true`; zero contracts — `lint-imports` reports `0 kept, 0 broken` |
| Practices enforcement hook | done | `.claude/hooks/py-check.sh` + `.claude/settings.json` |

So the remaining work is three deliverables plus two bits of bookkeeping:

- **A — the empty package skeleton** (a hard prerequisite for one of the contracts; see finding 3)
- **B — the four/five import-linter contracts**
- **C — the GitHub Actions workflow**
- **D — correcting `development-practices.md` §1.3**, which currently documents a config form that import-linter rejects
- **E — updating `roadmap.md`**

There is also one **contract-adjacent gap worth closing in the same change**: nothing
currently proves `asyncio_mode="auto"` works, and nothing pins the test suite
against warning regressions. Both are one-line-ish and belong with the CI work
rather than after it.

---

## 2. Verified findings

Six behaviours of `import-linter 2.13` / `grimp 3.15`, each confirmed against this
repository. Four of them change what the contracts should look like.

### Finding 1 — `plexapi*` is not a valid wildcard. **The practices doc is wrong.**

`docs/development-practices.md` §1.3 shows:

```toml
ignore_imports = ["shelfwarden.library.plex -> plexapi*"]
```

Running that produces a configuration error, not a contract result:

```
Contract "plexapi is confined to the Plex adapter" is not configured correctly:
    ignore_imports: A wildcard can only replace a whole module.
```

A wildcard must occupy a whole dotted segment. `*` matches one segment, `**`
matches any number. The working form needs **two** lines, because `plexapi.**`
does not cover the bare top-level `plexapi`:

```toml
ignore_imports = [
    "shelfwarden.library.plex -> plexapi",
    "shelfwarden.library.plex -> plexapi.**",
]
```

This is exactly the class of trap `development-practices.md` exists to prevent, so
the correction goes into that document (task D) rather than only into this plan.

### Finding 2 — an `ignore_imports` line that matches nothing is a hard failure

With the corrected two-line form, but before `library/plex.py` exists:

```
No matches for ignored import shelfwarden.library.plex -> plexapi.
No matches for ignored import shelfwarden.library.plex -> plexapi.**.
```

…and a non-zero exit. The default `unmatched_ignore_imports_alerting` is `error`.
Setting it to `warn` downgrades this to a warning and exits 0 — verified — but see
[Decision 2](#decision-2--how-to-handle-ignore_imports-for-seams-that-do-not-exist-yet):
the recommendation is **not** to use `warn`.

**Consequence:** do not write `ignore_imports` lines in step 0.1. Nothing imports
`plexapi`, `openai`, or `anthropic` yet, so the contracts are honest and strict
without them.

### Finding 3 — `source_modules` must exist; this forces the skeleton into 0.1

A contract whose source module is absent aborts the whole run:

```
Module 'shelfwarden.agent.tools' does not exist.
```

exit 1. So the `agent/tools/` seam contract — required by `CLAUDE.md`, the roadmap,
and `implementation-plan.md` §1 — **cannot land in 0.1 unless the package exists in
0.1**. An `__init__.py` containing nothing but a docstring is sufficient: verified
KEPT with 27 files analysed.

This is not scope creep. `implementation-plan.md` §1 already says the Phase 2–5
seams are *"present from day one but empty"*; finding 3 is the mechanical reason why.

### Finding 4 — `forbidden_modules` need **not** exist

The asymmetry matters and is not documented upstream in an obvious place:

- Internal, absent: `shelfwarden.agent.loop` was deleted and the contract still
  reported **KEPT** — no error.
- External, not installed: with `openai` absent from the dependency tree, a file
  containing `import openai` was still caught —
  `shelfwarden._iltest -> openai (l.2)`, contract **BROKEN**.

So the OpenAI and Anthropic contracts are real gates from day one, even though
neither SDK is a dependency yet. They will fail the first time an SDK import lands
outside its adapter, which is precisely when we want to hear about it.

### Finding 5 — descendants of `source_modules` are checked

`source_modules = ["shelfwarden"]` covers the entire package; the violation is
reported at the leaf:

```
shelfwarden is not allowed to import json:
-   shelfwarden._iltest -> json (l.1)
```

No need to enumerate subpackages.

### Finding 6 — `include_external_packages = true` is mandatory and already set

Confirmed in `pyproject.toml` with a comment explaining why. Unchanged by this plan.

### Verification transcript

The final contract block below was applied to `pyproject.toml` against a temporary
skeleton and run:

```
plexapi is confined to the Plex adapter KEPT
the OpenAI SDK is confined to its provider adapter KEPT
the Anthropic SDK is confined to its provider adapter KEPT
the tool layer is MCP-extractable KEPT
library and sources do not import the agent KEPT

Contracts: 5 kept, 0 broken.
```

Then deliberately breached, to prove the contracts are not vacuous:

```
shelfwarden.agent.tools is not allowed to import shelfwarden.evals:
-   shelfwarden.agent.tools -> shelfwarden.evals (l.2)

shelfwarden.library is not allowed to import shelfwarden.agent:
-   shelfwarden.library -> shelfwarden.agent (l.2)
```

exit 1. The scratch changes were reverted; the working tree is clean.

---

## 3. Tasks

Ordered by dependency. A → B → (C ∥ D) → E.

### Task A — the empty package skeleton

**Files:** `src/shelfwarden/**/__init__.py` — the layout in `implementation-plan.md` §1.

```
src/shelfwarden/
  models/__init__.py
  library/__init__.py
  sources/__init__.py
  agent/__init__.py
  agent/provider/__init__.py
  agent/tools/__init__.py
  agent/guard/__init__.py
  agent/guard/checks/__init__.py
  evals/__init__.py
  evals/corrupt/__init__.py
  telemetry/__init__.py
```

Each file is a single docstring naming what will live there and the roadmap step
that fills it — for example:

```python
"""Tool implementations and the phase-keyed registry.

Roadmap step 1.3. This package is the Phase 5 MCP extraction boundary: it must not
import `agent.loop`, `agent.provider`, or `evals`, enforced by the import contract
"the tool layer is MCP-extractable" in pyproject.toml.
"""
```

Do **not** create empty `.py` modules for files that will hold real code
(`loop.py`, `plex.py`, `item.py`, …). Finding 4 says they are not needed for the
contracts, and an empty `loop.py` is a lie about progress that `roadmap.md` already
tracks properly.

**Why it is here and not in 0.2:** finding 3 — `shelfwarden.agent.tools` must exist
for the seam contract to run at all.

**Verify:** `uv run lint-imports` proceeds past module resolution;
`uv run ruff check .` stays green.

**Done when:** every package in `implementation-plan.md` §1 exists with a docstring
that names its roadmap step.

---

### Task B — the import contracts

**File:** `pyproject.toml`, appended under the existing `[tool.importlinter]` header.
The comment currently sitting there ("Contracts land in roadmap step 0.1 feature 5")
is replaced by the contracts themselves.

```toml
[tool.importlinter]
root_package = "shelfwarden"
# REQUIRED: import-linter refuses to run at all when a contract forbids an external
# module and this is unset.
include_external_packages = true

# Contracts deliberately carry no `ignore_imports` yet. Nothing imports these
# packages, so the strict form is honest, and the first import that lands outside
# its adapter breaks CI at exactly the step that introduces it. The exact lines to
# add then are recorded per contract below. Note the wildcard form: `plexapi*` is
# rejected ("a wildcard can only replace a whole module") — two lines are needed.

[[tool.importlinter.contracts]]
name = "plexapi is confined to the Plex adapter"
type = "forbidden"
source_modules = ["shelfwarden"]
forbidden_modules = ["plexapi"]
# Step 0.3 adds:
#   ignore_imports = [
#       "shelfwarden.library.plex -> plexapi",
#       "shelfwarden.library.plex -> plexapi.**",
#   ]

[[tool.importlinter.contracts]]
name = "the OpenAI SDK is confined to its provider adapter"
type = "forbidden"
source_modules = ["shelfwarden"]
forbidden_modules = ["openai"]
# Step 1.2 adds:
#   ignore_imports = [
#       "shelfwarden.agent.provider.openai -> openai",
#       "shelfwarden.agent.provider.openai -> openai.**",
#   ]

[[tool.importlinter.contracts]]
name = "the Anthropic SDK is confined to its provider adapter"
type = "forbidden"
source_modules = ["shelfwarden"]
forbidden_modules = ["anthropic"]
# Step 1.8 adds the equivalent pair for
# shelfwarden.agent.provider.anthropic -> anthropic.

[[tool.importlinter.contracts]]
name = "the tool layer is MCP-extractable"
type = "forbidden"
source_modules = ["shelfwarden.agent.tools"]
forbidden_modules = [
    "shelfwarden.agent.loop",
    "shelfwarden.agent.provider",
    "shelfwarden.evals",
]

# Optional fifth contract — see Decision 3.
[[tool.importlinter.contracts]]
name = "library and sources do not import the agent"
type = "forbidden"
source_modules = ["shelfwarden.library", "shelfwarden.sources"]
forbidden_modules = ["shelfwarden.agent"]
```

**Verify:** `uv run lint-imports` → `Contracts: 5 kept, 0 broken`, exit 0. Then
temporarily add `from shelfwarden import evals` to `agent/tools/__init__.py` and
confirm it breaks; revert.

**Done when:** all contracts are KEPT, and a deliberate breach of each is
demonstrated once (by hand, then reverted — the automated version is Task C's job,
since CI running `lint-imports` is what the gate actually asks for).

---

### Task C — CI

**File:** `.github/workflows/ci.yml`.

Two jobs, matching `development-practices.md` §8.4 (fast per commit, full nightly).
Nothing is slow yet, so the nightly job is a placeholder that costs nothing but
means the split exists before the first slow test wants it — the alternative is
adding a scheduled job later, at which point somebody has to decide what "full"
means without a precedent.

Action versions verified current on 2026-08-26: `actions/checkout@v7`,
`astral-sh/setup-uv@v10`. No `actions/setup-python` step is needed — `uv sync`
provisions the interpreter from `.python-version`.

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
  schedule:
    - cron: "0 7 * * *"   # nightly full suite

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  check:
    name: lint, contracts, tests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7

      - uses: astral-sh/setup-uv@v10
        with:
          enable-cache: true
          cache-dependency-glob: uv.lock

      # --locked fails if uv.lock is out of date, rather than silently resolving
      # something different from what is committed.
      - run: uv sync --locked --group dev

      - name: ruff lint
        run: uv run ruff check .

      - name: ruff format
        run: uv run ruff format --check .

      # The step-0.1 gate: the architectural seams are a CI failure, not a
      # convention. See docs/development-practices.md §1.3.
      - name: import contracts
        run: uv run lint-imports

      - name: tests
        run: uv run pytest -q -m "not slow"

      # The other half of the step-0.1 gate: the CLI is invocable and a migration
      # applies to a fresh database.
      - name: cli smoke
        run: |
          uv run shelfwarden --help
          uv run shelfwarden --version
          uv run shelfwarden --db "${RUNNER_TEMP}/ci.db" db migrate
          uv run shelfwarden --db "${RUNNER_TEMP}/ci.db" db status

  nightly:
    name: full suite
    if: github.event_name == 'schedule'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10
        with:
          enable-cache: true
          cache-dependency-glob: uv.lock
      - run: uv sync --locked --group dev
      - name: tests (including slow)
        run: uv run pytest -q
```

Each command was run locally first; all pass against the current tree.

**Deliberately not in this workflow yet:**

- **No relative-change eval gate.** §8.4's four-bucket `regressed / fixed / new /
  changed` diff needs the scorer (step 0.8). Adding a stub that always passes is
  worse than not having it.
- **No coverage.** Adding `pytest-cov` is a dependency decision, and a coverage
  number on a 29-test scaffold measures nothing.
- **No matrix.** `requires-python = ">=3.13"` and `.python-version` pin 3.13; a
  matrix would test a portability claim the project does not make.
- **No secrets.** Standing rule: nothing in CI touches a live external API.

**Done when:** the workflow is green on a pull request, and each of the four gate
commands (`--help`, migrate, `lint-imports`, `pytest`) is a separately named step
so a red build says which one failed without opening the log.

---

### Task C2 — pytest hardening (ships with C)

**File:** `pyproject.toml`, `[tool.pytest.ini_options]`.

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
addopts = "--strict-markers --strict-config"
xfail_strict = true
filterwarnings = ["error"]
markers = [
    "slow: excluded from the per-commit suite; runs nightly (practices §8.4)",
    "live: touches a real external service; never runs in CI (practices §8.1)",
]
```

Verified: the current suite passes under `-W error` with no warnings, so
`filterwarnings = ["error"]` can be turned on today at zero cost. Turning it on
later, after warnings have accumulated, is the expensive order.

**File:** `tests/test_async_mode.py` — one test, closing the roadmap's `[~]`:

```python
async def test_async_tests_run_without_a_decorator():
    """asyncio_mode="auto" is configured; nothing proved it until this test."""
    await asyncio.sleep(0)
```

Verified to pass under the current configuration. Without it, an accidental removal
of `asyncio_mode` would go unnoticed until the first real async test in step 1.1
silently skipped.

**Done when:** `uv run pytest -q` is green with the stricter config and the async
test present.

---

### Task D — correct `development-practices.md` §1.3

Non-optional, and required by `CLAUDE.md`: *"If you change something that
contradicts a rule there, either follow the rule or update the document in the same
change with the reason."* Here the document is simply wrong, and it is wrong in a
way that costs the next reader a debugging session.

Edits to §1.3:

1. Replace the `plexapi*` example with the verified two-line form, and add one
   sentence: *"`plexapi*` is rejected — a wildcard can only replace a whole module
   segment; `plexapi.**` does not cover the bare top-level import, so both lines are
   needed."*
2. Add finding 2 to the list of "things verified by actually running this": an
   unmatched `ignore_imports` line fails the run outright, which is why the
   contracts ship without ignores until the seam they describe actually exists.
3. Add finding 3: `source_modules` must resolve to a real module, which is why the
   empty package skeleton is part of step 0.1 rather than 0.2.
4. Add finding 4: `forbidden_modules` need not exist — the OpenAI and Anthropic
   contracts are live gates before either SDK is a dependency.

Consider also adding a §8.5 (or extending §8.4) with the CI job shape, so the
workflow file has a prose counterpart the way the store and hook do.

**Done when:** no example in §1.3 fails when pasted into `pyproject.toml`.

---

### Task E — roadmap and doc index

**`docs/roadmap.md` §0.1** — tick:

- pytest/pytest-asyncio/ruff bullet → `[x]` (CI now exists)
- all four `[tool.importlinter]` sub-bullets → `[x]`
- the "Done when" bullet → `[x]`

Then add to **step 0.3** a new checklist line, so the deferred ignore lines are not
lost:

```
- [ ] Add the plexapi `ignore_imports` pair to the import contract (see docs/plans/step-0.1-scaffold.md, Task B)
```

…and the equivalent under **1.2** and **1.8** for the SDK contracts.

**`CLAUDE.md`** — add one row to the Documents table:

```
| `docs/plans/` | Per-step implementation plans and the verified findings behind them |
```

**Done when:** 0.1 is fully `[x]`, and the three deferred ignore-line follow-ups are
checklist items in the steps that will do them.

---

## 4. Decisions

### Decision 1 — how much skeleton

**Recommended: the full `implementation-plan.md` §1 package layout, docstrings only.**

Only `agent/tools/` is strictly required (finding 3). But the plan already commits
to *"seams present from day one but empty"*, the marginal cost is eleven files, and
a half-skeleton invites the "where does this go?" drift the layout exists to
prevent. The restraint that matters is the one stated in Task A: packages yes,
placeholder modules no.

### Decision 2 — how to handle `ignore_imports` for seams that do not exist yet

Two workable options:

| | Strict (recommended) | `unmatched_ignore_imports_alerting = "warn"` |
|---|---|---|
| Contracts today | No `ignore_imports` at all | Full final ignores, unmatched → warning |
| CI now | Clean, silent | Green, but with permanent warning noise |
| When `library/plex.py` lands | **CI fails**, developer adds the two documented lines | Passes silently |
| Typo in an ignore line | Caught — it will not match | **Masked** — indistinguishable from a not-yet-existing seam |

The strict form is recommended precisely because of the third row: a red build at
step 0.3 that says *"shelfwarden is not allowed to import plexapi"* is the contract
doing its job. The `warn` alternative trades that forcing function for warning
noise that everyone learns to skim — and the fourth row shows it also degrades the
tool's ability to catch its own misconfiguration.

Cost: one extra edit at each of steps 0.3, 1.2, and 1.8. Task E makes that edit a
tracked checklist item so it is not a surprise.

### Decision 3 — the fifth contract

`development-practices.md` §1.2 states plainly: *"`library/` and `sources/` are the
Phase 5 MCP extraction boundary. They must not import from `agent/`."* That is a
decided rule with no enforcement, and it is four lines of TOML — verified KEPT
against the skeleton and verified BROKEN against a deliberate breach.

It is beyond the roadmap's four literal sub-bullets, so it is called out here rather
than assumed. **Recommended: include it.** Skipping it means the seam is documented
in two places and enforced in neither, which is the state §1.3 exists to end.

### Decision 4 — the nightly job

**Recommended: register the markers and add the scheduled job now**, both empty of
content. The fast/nightly split is a §8.4 requirement; standing it up while the
suite takes 0.5s is free, and it means the first `@pytest.mark.slow` test in step
0.5 or 1.1 has somewhere to go.

---

## 5. Explicitly out of scope for 0.1

Named here so they are decisions rather than omissions:

- **README with TMDB/TVDB attribution.** The repository has no README at all. The
  attribution requirement is a standing rule and appears under Definition of Done,
  and the metrics table it opens with does not exist until step 0.8. Worth doing
  before the first source client ships in step 1.1; not part of the scaffold gate.
- **`config.py`** — settings precedence and secret handling (`practices` §9). Its
  first real consumer is the Plex provider in step 0.3.
- **Coverage, pre-commit, Dependabot/Renovate, release automation.** None are gate
  conditions; each is a dependency or service decision under the house rule.
- **The eval CI gate** (four-bucket relative diff) — blocked on the scorer, step 0.8.

---

## 6. Exit checklist

```bash
uv sync --locked --group dev
uv run ruff check .                  # All checks passed
uv run ruff format --check .         # N files already formatted
uv run lint-imports                  # Contracts: 5 kept, 0 broken
uv run pytest -q                     # green, incl. the async-mode test
uv run shelfwarden --help            # exit 0
uv run shelfwarden --db "$(mktemp -d)/ci.db" db migrate   # Applied 1 migration(s)
```

- [x] All six commands pass locally
- [ ] The same six run in GitHub Actions on a pull request, as separately named
      steps — **the one item that cannot be verified locally**; it needs a push
- [x] Each contract has been seen to break once, deliberately, and then restored —
      all five reported BROKEN together, exit 1, then `5 kept, 0 broken` after revert
- [x] `development-practices.md` §1.3 contains no example that fails when pasted
- [x] `roadmap.md` 0.1 is fully `[x]`; the deferred `ignore_imports` edits are
      checklist items under 0.3, 1.2, and 1.8
- [x] Step 0.2 can begin — the gating rule permits it once 0.1's "Done when" holds

---

## 7. What this hands to later steps

| Step | Inherits |
|---|---|
| 0.2 | `models/` exists; canonical-JSON work has a home and a green CI to land on |
| 0.3 | The plexapi contract is armed. Adding the import **will** fail CI until the two documented ignore lines are added — by design |
| 0.5 / 1.1 | `@pytest.mark.slow` and `@pytest.mark.live` are registered, and `--strict-markers` means a typo'd marker is an error rather than a silent no-op |
| 1.2 / 1.8 | The OpenAI and Anthropic contracts are armed the same way as plexapi |
| 1.3 | `agent/tools/` cannot import `agent.loop`, `agent.provider`, or `evals` — the Phase 5 MCP seam is enforced before the first tool is written |
| 0.8 | CI has a place to add the four-bucket relative eval gate |
