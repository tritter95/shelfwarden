# ShelfWarden Development Practices

Working rules for the stack described in [`implementation-plan.md`](./implementation-plan.md) and tracked in [`roadmap.md`](./roadmap.md).

This is not a style guide. Nearly every rule here exists because a specific library, API, or architectural decision has a trap in it that was verified rather than assumed. Where a rule has a reason, the reason is stated — follow the reason, not the letter, when they diverge.

**The governing principle, from spec §3.1:** *prompts are guidance; code is enforcement.* Any rule expressible as a predicate belongs in code — a type, a lint contract, a test — not in a document. Treat every entry below that is currently prose as a candidate for mechanization.

---

## 0. House rules

| Rule | Why |
|---|---|
| **Ask before adding a dependency that introduces persistent state or a new service.** | Spec §9. A new datastore or daemon changes the operational surface of a project whose whole point is measurability. |
| **Every fix for an observed failure ships with an eval case that fails before and passes after.** | Spec §9. Without it, "fixed" is a claim about the model rather than a fact about the system. |
| **Never let a provider SDK type or a `plexapi` type escape its adapter module.** | Enforced by `import-linter`, not discipline. See §1.3. |
| **Never treat a model's self-report as the record of what happened.** | Spec §3.5. Outcomes derive from recorded state. This applies to `confidence`, `needs_human`, success claims, and narrative summaries alike. |
| **Return the minimum useful payload from every tool.** | Spec §9. Tool outputs are resent on every subsequent turn — a verbose result is a recurring tax, not a one-time one. |
| **No silent caps.** If code truncates, samples, or drops, it logs what it dropped. | A silently truncated result reads as complete coverage. This applies to pagination, dataset generation, and eval reporting equally. |

---

## 1. Python, uv, and project structure

### 1.1 Environment

Pin 3.13 in `.python-version` (already installed locally; `uv` provisions it otherwise). Never `pip install`, never `source .venv/bin/activate` — every command goes through `uv`:

```bash
uv sync --group dev          # install
uv run pytest -q             # test
uv run shelfwarden --help    # invoke the CLI
uv add --group eval respx    # add a dependency
uv lock --upgrade-package anthropic   # targeted upgrade
```

Dependency groups (PEP 735 `[dependency-groups]`, not `[project.optional-dependencies]` — the latter is for extras shipped to consumers):

| Group | Contents |
|---|---|
| *(default)* | `plexapi`, `httpx`, `pydantic`, `typer`, `openai`, `anthropic` |
| `dev` | `pytest`, `pytest-asyncio`, `respx`, `vcrpy`, `ruff`, `import-linter` |
| `trace` | `opentelemetry-sdk`, `opentelemetry-exporter-otlp`, `arize-phoenix` |

Keep `trace` optional so the agent runs without a tracing stack installed.

### 1.2 Layout

`src/` layout, always. It prevents the classic failure where tests import the working directory instead of the installed package and mask a packaging bug.

Module ownership is single-purpose and stated in `implementation-plan.md` §1. Two rules that matter more than the rest:

- **`agent/loop.py` is the only module LangGraph would replace in Phase 4.** Anything that would have to be rewritten alongside it belongs somewhere else.
- **`library/` and `sources/` are the Phase 5 MCP extraction boundary.** They must not import from `agent/`.

### 1.3 Import contracts

`[tool.importlinter]` in `pyproject.toml`, from step 0.1 — not later. Five contracts, live in CI as of step 0.1: `plexapi` confined to `library/plex.py`, `openai` and `anthropic` each confined to their provider adapter, `agent/tools/` unable to import `agent.loop` / `agent.provider` / `evals`, and `library/` + `sources/` unable to import `agent/` (the §1.2 MCP boundary, in both directions).

```toml
[tool.importlinter]
root_package = "shelfwarden"
include_external_packages = true   # REQUIRED — see below

[[tool.importlinter.contracts]]
name = "plexapi is confined to the Plex adapter"
type = "forbidden"
source_modules = ["shelfwarden"]
forbidden_modules = ["plexapi"]
# Added in step 0.3, when library/plex.py first imports plexapi -- not before:
#   ignore_imports = [
#       "shelfwarden.library.plex -> plexapi",
#       "shelfwarden.library.plex -> plexapi.**",
#   ]
```

**Five things verified by actually running this** (import-linter 2.13 / grimp 3.15), every one of which fails confusingly otherwise:

- **`include_external_packages = true` is mandatory here.** Three of the contracts forbid *external* packages, and without this flag import-linter refuses to run at all: `"The top level configuration must have include_external_packages=True when there are external forbidden modules."`
- **A wildcard can only replace a whole module segment.** `"shelfwarden.library.plex -> plexapi*"` — the form this document recommended until step 0.1 tried it — is not a loose match, it is a configuration error: `ignore_imports: A wildcard can only replace a whole module.` `*` matches one dotted segment and `**` matches any number.
- **For an external package, one bare line is all that matches.** Under `include_external_packages`, grimp collapses the entire external package into a single node: `import plexapi.utils` and `from plexapi.server import PlexServer` are both recorded as `-> plexapi`. So `ignore_imports = ["shelfwarden.library.plex -> plexapi"]` — adding a `plexapi.**` line would match nothing and, per the next bullet, fail the run outright. (Submodule forms *are* needed for **internal** modules, where each is its own node.) Verified in step 0.3 by reading grimp's graph, after the two-line form recommended here broke the build.
- **An `ignore_imports` line that matches nothing fails the run** — `No matches for ignored import ...`, exit 1, because `unmatched_ignore_imports_alerting` defaults to `error`. This is why the contracts ship with the ignore lines commented out until the module they describe actually imports the SDK. Setting the option to `warn` would silence it, at the cost of also silencing a typo'd ignore line — the two become indistinguishable. Prefer the red build: a CI failure at step 0.3 saying `shelfwarden is not allowed to import plexapi` is the contract working.
- **`source_modules` must resolve to a real module** (`Module 'shelfwarden.agent.tools' does not exist.`, exit 1), and its descendants are all checked, so `source_modules = ["shelfwarden"]` covers the whole package and reports violations at the leaf. This is the mechanical reason the empty package skeleton is part of step 0.1: the `agent/tools/` contract cannot run until the package exists. `implementation-plan.md` §1 already asks for those seams to be "present from day one but empty".
- **`forbidden_modules` need not exist** — neither internal nor installed. A contract stayed KEPT with `shelfwarden.agent.loop` absent, and a file containing `import openai` was caught while `openai` was not yet a dependency. So the OpenAI and Anthropic contracts are live gates from day one, before either SDK is installed.

And one operational note: **`lint-imports` needs the package importable**, so it fails with `"Could not find package 'shelfwarden' in your Python path"` until `uv sync` has installed the project. That is a "cannot run" condition, not a contract breach — the practices hook distinguishes the two deliberately (see §10).

These turn four claims the plan makes — "plexapi types never escape `library/plex.py`", "provider SDK types don't spread", "the tool layer is extractable", and "`library/` and `sources/` are the MCP boundary" — from intentions into CI failures. `lint-imports` runs in CI alongside `ruff` and `pytest`, as its own named step. Adding these on day one costs minutes; adding them in Phase 5 means discovering the seam already leaked.

### 1.4 Style

`ruff` for lint and format; no separate formatter. Enable at minimum `E`, `F`, `I`, `UP`, `B`, `SIM`, `RUF`. Line length 100.

Type-annotate every public function. `from __future__ import annotations` is unnecessary on 3.13 — use builtin generics (`list[str]`, `X | None`) directly.

**`docs/` is excluded from ruff** (`extend-exclude = ["docs"]`), and must stay excluded. Ruff formats Python code blocks *inside Markdown*, which is not what you want here: `docs/shelfwarden.md` is the project spec and not ours to rewrite, and this document deliberately shows a recipe that **does not work** (§3.1) — reformatting is a short step from "correcting" an example whose whole point is being wrong. A plain `ruff format .` silently rewrote three documents before this exclusion existed.

Use `Annotated[T, typer.Option(...)]` for CLI parameters rather than a `typer.Option()` call in the default position; `B008` flags the latter, and the annotated form is Typer's current idiom anyway.

---

## 2. Pydantic and data modelling

### 2.1 Where Pydantic belongs

Use `BaseModel` at **boundaries** — anything parsed from a model response, an external API, or a truth file. Use frozen `@dataclass` for internal value objects (`ItemId`, `SubjectMatch`, `Confidence`) where validation is not needed and hashability and cheapness are.

Do not put Pydantic in hot internal loops. Validation at every internal hop is cost without benefit; validate once at the edge and pass typed values inward.

**`model_copy(update=...)` does not validate**, even on a frozen model with `extra="forbid"` and `validate_assignment=True`. It will put a `str` into an `int | None` field without complaint, and silently drop an unknown key rather than raising. This matters because it is the obvious way to write a corruption function, and the bad value would flow straight into the truth file and the snapshot. Mutate through `models.item.with_changes`, which re-validates via the type adapter; `tests/models/test_item.py` pins the difference.

### 2.2 Constraints as parse-time gates

Prefer a constraint that makes an invalid state unconstructible over a check that detects it later:

```python
citations: list[Citation] = Field(min_length=1)
```

This rejects an uncited claim before the validator runs. **But** — see the lesson recorded in `implementation-plan.md` §6 — a constraint that some legitimate case *cannot* satisfy does not produce refusal, it produces laundering. Before adding a hard constraint, enumerate the cases that must satisfy it and confirm every one of them can.

### 2.3 Discriminated unions

Use them wherever a field's meaning depends on a kind tag. The claim union is the canonical example:

```python
Claim = Annotated[ExternalClaim | ObservationClaim | DerivedClaim, Field(discriminator="kind")]
```

The discriminator gives clear validation errors and lets `match` statements be exhaustive.

### 2.4 JSON Schema for tool definitions

**Generate schemas; never hand-write them.** A hand-written schema drifts from the model it describes, and the drift surfaces as a model behaving strangely rather than as an error.

```python
schema = MyToolArgs.model_json_schema()
schema = inline_refs(schema)  # required — see below
```

`model_json_schema()` hoists nested models into `$defs` and references them with `$ref`. Some provider strict-mode validators reject `$ref`, and Pydantic ships no flatten flag. Write one recursive resolver (`agent/provider/schema.py`) that splices `$defs` inline and drops the key, and apply it in **both** provider adapters.

For provider strict mode, the schema also needs `additionalProperties: false` and a complete `required` list. Configure that on the model (`model_config = ConfigDict(extra="forbid")`) rather than patching the emitted schema.

### 2.5 Canonical serialization

Determinism requirements (byte-identical exports, content-addressed evidence, `case_id` hashes) depend on one canonical serializer used everywhere. It lives in `shelfwarden/canonical.py`, from step 0.2:

```python
def canonical_json(obj: object) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,   # REQUIRED -- see below
    ).encode("utf-8")
```

Sorted keys, no insignificant whitespace, stable float formatting. Never hash `str(dict)` or a default `json.dumps`.

**`allow_nan=False` is not defensive, it is a bug fix.** Without it `json.dumps` emits bare `NaN`, `Infinity`, and `-Infinity`, none of which is valid JSON. Python reads them back happily, so the dataset looks fine locally and is rejected by every other parser that touches it. Plex ratings are floats, so this is reachable. Fail at write time.

Two policy decisions the serializer cannot make for you, both verified to change the bytes:

- **Unicode normalization.** `"Amélie"` composed and decomposed are the same string to a reader and different bytes to a hash. Both forms genuinely arrive: macOS filesystems hand out NFD, and `part.file` comes from the filesystem. Normalize human-readable text to **NFC at the model boundary** (`canonical_text`), and deliberately **do not** normalize file paths — a path is an argument to a future filesystem operation, Phase 3 renames real files, and an NFC-normalized NFD path may name nothing on disk. Where a path needs comparing rather than opening, normalize at the comparison site, visibly.
- **Numeric type stability.** `8` and `8.0` serialize differently. A field that is sometimes int and sometimes float breaks byte-identity without changing value, so field types have to be pinned in the model rather than inferred from whatever Plex returned.

And one that bites the moment a timestamp appears: **datetimes serialize by representation, not by instant.** `2024-01-01T05:00:00Z` and `2024-01-01T00:00:00-05:00` are the same moment and different bytes; a naive datetime produces a third form with no offset. Coerce to aware UTC at parse time (`models.item.UtcDatetime`) or byte-identical export becomes a function of the server's timezone setting.

---

## 3. SQLite

### 3.1 Connection setup

Never call `sqlite3.connect` directly. Every connection comes from `shelfwarden.store.db.connect()`, which does this — and **the ordering is load-bearing**:

```python
conn = sqlite3.connect(path, autocommit=True)   # no transaction open yet
conn.execute("PRAGMA journal_mode=WAL")         # persists in the database file
conn.execute("PRAGMA foreign_keys=ON")          # per-connection, off by default
conn.execute("PRAGMA busy_timeout=5000")        # per-connection
conn.row_factory = sqlite3.Row
conn.autocommit = False                         # explicit transaction control from here
```

> **The obvious version of this does not work**, and an earlier draft of this document prescribed it. `sqlite3.connect(path, autocommit=False)` opens a transaction **immediately** — `in_transaction` is `True` before you run a single statement — and `journal_mode` cannot change inside a transaction:
>
> ```
> sqlite3.OperationalError: cannot change into wal mode from within a transaction
> ```
>
> So the pragmas must be set in autocommit mode, and `autocommit` flipped to `False` afterwards. `tests/store/test_db.py::test_wal_cannot_be_set_inside_a_transaction` pins this, so the ordering cannot be "tidied" back into the broken form. Verified on CPython 3.13.12 / SQLite 3.51.3.
>
> Do not pass `isolation_level` alongside `autocommit`: it is ignored unless `autocommit` is `LEGACY_TRANSACTION_CONTROL`, so it reads as meaningful and does nothing.

`autocommit=False` is set **explicitly** rather than relied upon, because the 3.12+ default is still `LEGACY_TRANSACTION_CONTROL` and CPython has announced it will change. `foreign_keys=ON` is per-connection and off by default — a schema with foreign keys and no pragma is decorative, and a fresh connection reads back `0`. `journal_mode`, by contrast, is a property of the file and survives reconnect.

**`BEGIN IMMEDIATE` needs autocommit mode.** Under `autocommit=False` sqlite3 has already opened a transaction, so an explicit `BEGIN` fails. Where a write lock must be taken up front — the migration runner — `db._immediate()` temporarily flips to autocommit, issues `BEGIN IMMEDIATE` / `COMMIT` by hand, and restores the previous mode.

### 3.2 No ORM

Raw `sqlite3` with hand-written SQL and numbered migrations. The store is small, the queries are simple, and the project's stated philosophy is hand-rolled. SQLAlchemy/SQLModel would add a dependency and an abstraction for no benefit at this size.

Migrations are numbered `NNNN_name.sql` files in `store/migrations/`, applied in order inside a transaction, tracked in a `schema_version` table that the **runner** creates (a migration runner cannot depend on a table a migration would have to create).

**Never edit an applied migration; add a new one.** This is enforced, not merely stated: `migrate()` records each migration's SHA-256 and raises `MigrationTamperedError` when a file no longer matches what was applied. Editing one desynchronises every database that already ran it, and the checksum is what catches that.

Discovery uses `importlib.resources`, not `__file__`, so migrations resolve from a non-editable install. Verified: the `uv_build` backend ships `store/migrations/*.sql` and `py.typed` in the wheel with no extra configuration, and migrations apply correctly from a wheel-installed package.

### 3.3 Content-addressed blobs

Large payloads — assembled contexts, raw model responses, tool results, evidence bodies — go in a `blobs` table keyed by content hash, referenced by hash from `steps` and `evidence`. This deduplicates the repeated system prompt across every step of every run, and it makes "the same context" a checkable identity rather than a judgement.

### 3.4 Never store credentials

Strip API keys, tokens, and auth headers from `normalized_params` **before hashing and before storing**. The evidence store is otherwise a credential leak with a long retention period. This is a test, not a convention: assert that no stored evidence body or param blob matches the configured secrets.

---

## 4. python-plexapi

Verified against 4.18.2. The repo moved to `github.com/pushingkarmaorg/python-plexapi`; link there, not to the old `pkkid` path.

### 4.1 Confinement

`library/plex.py` is the only module that imports `plexapi` (import-linter enforced). It maps plexapi objects to `NormalizedItem` and translates plexapi exceptions into `LibraryError`. Nothing downstream should be able to tell which provider it is talking to — that is what lets the agent run against `SnapshotLibrary` unchanged.

### 4.2 Read-only is a property of the type

plexapi has **no read-only mode**; every method is an HTTP call the server accepts based on token permissions. So the `LibraryProvider` protocol simply contains no mutating method. Phase 3 adds a separate `MutableLibraryProvider`. A test asserts the read-only protocol's method set is disjoint from the mutating one.

Do not implement read-only as a runtime check or a flag. Spec §3.2 requires that mutating tools *not exist* outside `executing`.

### 4.3 Turn off auto-reload

`PlexPartialObject.__getattribute__` silently refetches over the network when you touch an attribute that is `None` or `[]` on a partial object. Left on, this makes exports non-deterministic and costs unbounded.

Disable it globally via plexapi's config — `~/.config/plexapi/config.ini`:

```ini
[plexapi]
autoreload = false
```

The env-var form works — `PLEXAPI_PLEXAPI_AUTORELOAD` — **but it fails open, silently, and case-sensitively.** Confirmed in step 0.3:

```
PLEXAPI_PLEXAPI_AUTORELOAD='false'  -> off      'False' -> ON, silently
PLEXAPI_PLEXAPI_AUTORELOAD='0'      -> off      'FALSE' -> ON, silently
                                                'no'    -> ON, silently
```

`plexapi.utils.cast` accepts only `1, True, "1", "true"` and `0, False, "0", "false"` and raises `ValueError` on anything else — but `PlexConfig.get` wraps the lookup in a **bare `except:`** that swallows the error and returns the default, which for this key is `True`. So the natural Python spelling disables nothing and warns about nothing.

Two consequences, both implemented in `library/plex.py`:

- **Set the value yourself, in the environment.** Environment is consulted *before* the config file, so a developer with `PLEXAPI_PLEXAPI_AUTORELOAD="False"` already exported would override a correct `config.ini` and land straight back in the fails-open case. Owning the value is the only way to win.
- **Assert, twice.** Once on `CONFIG.get`, once on a fetched object's `_autoReload`. Reaching for a private attribute is deliberate here: the alternative is trusting a configuration path that fails open.

`_autoReload` is read per object at construction, not at import, so setting it programmatically before building the server does take effect — unlike `TIMEOUT`, which `plexapi/__init__.py` binds once at import time.

**Timestamps have the same shape of problem — twice over.** By default `plexapi.utils.toDatetime` returns **naive local time**: epoch `1704067200` is `2024-01-01T00:00:00Z`, but plexapi renders it as `2023-12-31 19:00` with no tzinfo on a UTC-5 machine. An export would then depend on the timezone of the machine that produced it.

The obvious remedy, `setDatetimeTimezone("utc")`, **fails open in exactly the same way as the auto-reload switch**: it resolves its argument as an IANA name through `ZoneInfo`, and turns `ZoneInfoNotFoundError` into `tzinfo = None` behind a `log.warning` that plexapi's `NullHandler` swallows. On a host whose tzdata lacks that entry, timestamps silently revert to naive local time. This cost a red CI run — it passed locally and failed on the runner, which is the worst way to find out.

Assign the stdlib constant directly instead. `plexapi.utils.DATETIME_TIMEZONE = datetime.UTC` needs no system timezone database at all:

```python
plexapi.utils.DATETIME_TIMEZONE = UTC          # not setDatetimeTimezone("utc")
```

Then refuse a naive datetime at the mapping boundary rather than guessing one — that guard is what turned a silent, machine-dependent export into a loud test failure.

The pattern worth generalizing: **plexapi's configuration helpers prefer a permissive default over an error.** Auto-reload and the timezone both do it. Assume any plexapi setting may not have taken, and assert the property you actually need rather than the call you made.

Then call `reload()` explicitly with the exact includes needed:

```python
item.reload(checkFiles=True, includeChapters=False, includeMarkers=False)
```

**An include dict is a set of overrides, not a description of the request.** Verified in step 0.4, and it matters because the export manifest is supposed to record *what produced this record*. `_buildDetailsKey` starts from all nineteen keys of `PlexPartialObject._INCLUDES`, overlays the kwargs, and drops every key whose resulting value is `False`, `0`, or `'0'`. Our set names eight. Of the eleven left at their defaults, ten are already zero and vanish — and one is not:

```python
'includeFields': 'thumbBlurHash,artBlurHash',   # a string, therefore never falsy
```

So every request we make carries a parameter our dict never mentions:

```
movie   core  /library/metadata/1701?includeFields=thumbBlurHash%2CartBlurHash
movie   full  /library/metadata/1701?checkFiles=1&includeFields=thumbBlurHash%2CartBlurHash
```

Record the **effective** set, never the override dict — `library.plex.effective_request_params()` computes it from `_INCLUDES` with no server involved. Recording the overrides would understate the request while looking authoritative, which is worse than recording nothing. `_INCLUDES` is defined once on `PlexPartialObject` and no subclass overrides it, so the effective set is uniform across every media kind; a test pins that rather than assuming it.

Note also that CORE and FULL differ by exactly one parameter, `checkFiles=1`, which asks the server to stat every backing file and annotate `Part` with `accessible`/`exists`. This model maps neither, so the two profiles produce identical output at different cost — and `accessible` is *volatile*, flipping when a mount drops, so mapping it would have made export byte-identity a function of whether a NAS was awake. The export defaults to CORE for that reason.

**`includeFields` is a naming trap.** It sounds like it governs the `<Field>` elements that carry lock state. It does not — it is a blur-hash selector, and `<Field>` elements arrive from the metadata endpoint regardless of it. Relatedly, **listings already carry guids**: `_buildSearchKey` sets `includeGuids=1` and `_buildQueryKey` hardcodes it, so a per-item fetch is not needed to see `<Guid>` children. The per-item fetch in the export exists for lock state alone.

### 4.4 Pagination

```python
page = section.search(libtype="movie", container_start=offset, maxresults=limit)
```

**Both** arguments are required. `container_start` alone still walks the entire remaining result set, because `fetchItems()` loops internally until it has everything. Default container size is 100 (the docs say 50 — the docs are wrong).

### 4.5 Editing (Phase 3)

Every `edit*`/`add*`/`remove*` helper defaults to `locked=True`, which pins the field against future metadata-agent refreshes. That is a real, persistent side effect that is easy to apply by accident.

- Always pass `locked=` **explicitly**. Never rely on the default.
- Record prior lock state in the snapshot; `revert` must restore locks, not just values.
- `saveEdits()` does not reload the object — call `.reload()` before verifying.
- Prefer `batchEdits()`/`saveEdits()` per item and `section.batchMultiEdits()` across items over per-field calls.

### 4.6 Files

plexapi **cannot move or rename files.** Filename repairs are a filesystem operation followed by `section.update(path=...)`. Treat the filesystem move and the Plex rescan as one compensatable unit with its own revert.

Never delete. Spec §3.3: moving and renaming only, both revertible.

### 4.7 Throttling

plexapi has no retry, no backoff, and no rate limiting; default timeout is 30s and any non-2xx raises immediately.

`PlexServer` accepts a `session=` argument, and `library/session.py` uses it to supply all three. It also solves a second problem: **`PlexServer.query` maps 401 to `Unauthorized`, 404 to `NotFound`, and everything else — 429, 500, 502, 503 alike — to `BadRequest`**, so retryable and terminal conditions are indistinguishable by type. The status survives only inside the message string (`"(503) service_unavailable; ..."`). A response hook on our session records it as an integer, with message parsing kept as a fallback, because a message format is a string contract the library never promised.

Retries are confined to `GET`. A blanket policy is how a mutating request gets replayed by accident in Phase 3.

Note also what plexapi does *not* raise: `requests` is called directly, so a connection refusal or timeout arrives as `requests.exceptions.ConnectionError`/`Timeout`, never wrapped. A boundary catching only `plexapi.exceptions.*` leaks raw `requests` types downstream — the exact leak `library/plex.py` exists to prevent. `refresh()` and `analyze()` are expensive server-side (the docstring likens `analyze()` to transcoding). Our wrapper owns concurrency limits and backoff — and limits concurrent `analyze()`/`refresh()` calls specifically.

---

## 5. External metadata sources

### 5.1 One client, per-source policy

All four sources go through a shared `httpx` client in `sources/base.py` carrying a per-source policy object: throttle rate, retry/backoff, cache TTL, and required headers. No source module makes a bare HTTP call.

| Source | Auth | Throttle | Notes |
|---|---|---|---|
| TMDB | v4 Bearer (current recommendation; works on v3 paths) | Honor 429 + backoff — the old 40/10s limit was removed and the ceiling is unpublished | Prefer `/find/{external_id}` over title search when any external id is known. `append_to_response=external_ids,alternative_titles` |
| TVDB v4 | `POST /login` → JWT, ~1 month | Undocumented; cache aggressively (TVDB's own guidance) | Refresh the JWT proactively, never on 401 alone |
| Audnexus | none | Keep under 100/min; public instance 429s under load | **No book search exists** — ASIN-keyed only |
| Open Library | none | 3 req/s **with** a descriptive contact `User-Agent`, 1 req/s without | The User-Agent is policy, not politeness |

```python
USER_AGENT = "ShelfWarden/0.1 (+https://github.com/…; contact@example.org)"
```

### 5.2 Source-specific traps

- **Audnexus `seriesPrimary.position` is a string** and may be non-integer (`"3.5"` for novellas). Never `int()` it. Standalone books omit `seriesPrimary`/`seriesSecondary` entirely — absence is meaningful and provable here.
- **Audible garbage-splits author names.** A single author can come back as two bogus entries. Validate author lists rather than trusting arity.
- **Open Library's `series` field is not trustworthy** — free-text, edition-level, no numeric position, and a confirmed bug means it is often not returned even when it matches the query. Never use it as a series-order source; an absence claim against it is rejected outright, not downgraded.
- **TVDB episodes need not exist in every season type.** Cross-reference orderings by TVDB's internal episode `id`, never by season/episode numbers. A number-based join silently produces wrong repairs on exactly the problem class that motivated the feature.
- **TVDB absence is a 200 with an empty list, never a 404.** A 404 there means *unknown*, not *absent*.

### 5.3 Every response becomes evidence

Each adapter records an `EvidenceRecord` and emits a `field_index: dict[FieldPath, list[Pointer]]` at parse time. The adapters already parse responses to build minimal tool payloads, so the index is nearly free — and it is what makes citing TMDB's `id` to support a `guids.tvdb` claim structurally impossible.

Cassette-driven test: for every committed cassette, assert the field index is non-empty for each field that source advertises.

### 5.4 Error taxonomy

Spec §9: *tool error messages are prompts.* Every error is classified:

| Class | Handling | Example message |
|---|---|---|
| **Retryable** | Handled in `sources/base.py` with backoff. **Never surfaced to the model.** | — |
| **Correctable** | Surfaced, and says what to do instead | *"No Audnexus record for that ASIN. Audnexus is ASIN-keyed and has no title search. Try `lookup_audiobook` with title+author, or `search_metadata(source='openlibrary')` for bibliographic identity."* |
| **Terminal** | Surfaced, and says retrying will not help | *"Item 41823 is in a photo section; ShelfWarden does not diagnose photos. Do not retry; record no finding for this item."* |

A correctable error that does not name a concrete next action is a bug. "Not found" is not a correctable error message; it is a dead end wearing one.

### 5.5 Attribution

TMDB and TVDB free-tier terms both require attribution. The README carries the TMDB notice and logo and a direct TheTVDB link before any public push.

---

## 6. LLM providers

### 6.1 The interface is thin and the SDKs stay put

`agent/provider/base.py` defines `LLMProvider`, `Proposal`, `ToolSpec`, `Usage`. `openai.py` and `anthropic.py` are the only modules importing their SDKs.

Do not try to unify what genuinely differs. Reasoning/thinking replay is carried as an **opaque `ProviderCarryover` blob** the loop stores and returns without inspecting. Attempting to normalize OpenAI reasoning items and Anthropic thinking blocks into one shape produces a lossy abstraction that breaks both.

### 6.2 Store raw responses verbatim

Every `Proposal` carries `raw: bytes`, persisted to `blobs` before parsing. This is the single highest-leverage decision in Phase 1: it makes Phase 2's deterministic replay a provider swap rather than a rewrite. Store the raw bytes even when parsing fails — *especially* then.

### 6.3 Provider specifics

**OpenAI** — use the **Responses API**, not Chat Completions; it is the recommended surface for agentic tool loops. Tool defs are flat (no nested `function` wrapper). Tool calls arrive as first-class `function_call` items with `call_id`, not inside `message.tool_calls`. `n` does not exist. Reasoning context persists across turns with `store: true`.

**Anthropic** — `stop_reason == "tool_use"` signals a tool call. **Return all `tool_result` blocks in a single user message**; splitting them across messages degrades parallel tool calling. Prompt caching uses `cache_control: {type: "ephemeral", ttl: "5m"|"1h"}`, max 4 breakpoints, ~1024-token minimum prefix. Verify it works by asserting `usage.cache_read_input_tokens > 0` on the second call — if it stays 0, something in the prefix is varying (a timestamp in the system prompt is the usual culprit).

### 6.4 Cost accounting is code, not narrative

Cost comes from `usage` fields and a per-model price table, computed in code and recorded per step. The spec requires median/p95 cost per item and cost per 100 items as headline metrics; those numbers must never depend on a model reporting its own usage.

Keep the price table in one module with the model id as the key, and treat an unknown model id as an error rather than a zero.

---

## 7. Observability

### 7.1 One module owns attribute names

`telemetry/otel.py` is the only place `gen_ai.*` strings appear. The GenAI semantic conventions are **not stable** — every attribute is Development status, they were moved out of the main semconv repo in v1.42.0 into a repo with no tagged releases, and `gen_ai.system` was renamed to `gen_ai.provider.name`.

Emit **both** names during the transition. Do not pin a schema URL; there isn't a stable one to pin.

### 7.2 What a span carries

Per spec §2's Phase 2 requirements, each step span captures: the assembled context **by reference** (blob hash, never inline), the raw model response before parsing, the guard decision, the tool result, tokens, cost, and latency.

Inlining a full context into a span attribute will blow past exporter limits and make traces unreadable. Reference and let the trace link to the store.

### 7.3 Local backend

Arize Phoenix, either as one container (`arizephoenix/phoenix:latest`, 6006 UI + OTLP/HTTP, 4317 gRPC) or via `phoenix serve` with no container at all. Instrumentation stays vanilla OTLP so the backend is swappable.

---

## 8. Testing

### 8.1 Never hit a live API in CI

`vcrpy` captures real response shapes **once** into committed cassettes; `respx` builds hand-crafted responses for unit tests of error paths. Use both, for different jobs:

- **vcrpy** when the test needs a *real* payload shape — including the awkward ones (a garbage-split author list, a book with no `seriesPrimary`, a TVDB series missing an absolute ordering).
- **respx** when the test needs a *specific* condition that is hard to provoke live — a 429, a truncated body, a malformed field.

Re-record cassettes deliberately, never automatically, and review the diff — a changed upstream shape is information, not noise.

### 8.2 Determinism has tests

The invariants that everything downstream rests on:

```python
def test_export_is_byte_identical(): ...  # re-running an export diffs clean
def test_apply_reverse_restores_ground_truth(): ...  # property test over all corruptions
def test_case_ids_stable_across_regeneration(): ...  # semantic ids survive a re-export
def test_readonly_registry_excludes_mutating_tools(): ...
def test_evidence_store_contains_no_secrets(): ...
```

The read-only registry test is the structural guarantee behind spec §3.2 and must run on every commit.

**A same-process determinism test is not a determinism test.** `pytest` runs in one process with one hash seed, so "produce it twice and compare" passes against code whose ordering depends on `PYTHONHASHSEED` — and CI stays green while two developers' exports differ for no visible reason. Anything that aggregates has to be compared across forked subprocesses with an explicit seed on each side:

```python
subprocess.run([sys.executable, "-c", program, out], env={"PYTHONHASHSEED": "0", ...})
subprocess.run([sys.executable, "-c", program, out], env={"PYTHONHASHSEED": "1", ...})
```

The exposure is narrower than it looks and wider than it feels. `canonical_json` sorts mapping keys, so a serialized *object* is safe. **Lists are not**, and neither is anything built from `set` iteration or `Counter.most_common()`, whose tie order is insertion order. Sort every aggregation by an explicit total order — count descending, then key ascending — and cap example lists only *after* sorting, so which examples survive is a function of the data rather than of iteration order.

The mirror-image trap is worth stating too, because it bit step 0.4 and the first trap hides it: since `canonical_json` sorts keys, a count-ordered *mapping* does not survive the round trip either — it comes back alphabetical. Ordering a human is meant to read has to be re-derived at render time, not inherited from the dict it was stored in.

### 8.3 Async

`pytest-asyncio` in `asyncio_mode = "auto"`. Set `asyncio_default_fixture_loop_scope` explicitly to avoid the deprecation warning and pin fixture loop semantics.

### 8.4 CI gating

`.github/workflows/ci.yml`, from step 0.1. The `check` job runs on every push and pull request: `uv sync --locked` (which fails rather than silently re-resolving when `uv.lock` is stale), then `ruff check`, `ruff format --check`, `lint-imports`, `pytest -m "not slow"`, and two CLI smoke steps — `--help`/`--version`, and a migration applied to a fresh database. Each is a separately named step so a red build names the failure without anyone opening the log. A `nightly` job on a cron schedule runs the suite without the `-m` filter.

`slow` and `live` are registered markers, and `--strict-markers` is on: a typo'd marker is an error, not a silent no-op. That matters most for `live`, where a mistyped skip marker is a test that quietly starts calling a real API. `filterwarnings = ["error"]` was turned on while the suite was still small and warning-free — the expensive order is the other one.

Pin third-party actions to a tag that actually exists. `astral-sh/setup-uv` publishes floating major tags only through `v7`, so `@v10` fails to resolve — `Unable to resolve action astral-sh/setup-uv@v10` — even though `v10.0.1` is the current release. Reading a repository's latest release tag is not evidence that the matching floating major exists; check `git/matching-refs/tags` before writing a major-only reference, or pin the exact version.

CI needs no secrets and touches nothing external, per §8.1. The relative-change eval gate below is blocked on the scorer (step 0.8); a stub that always passes would be worse than its absence.

Fast suite on every commit; full suite nightly. Gate on **relative** change — no case that passed may now fail — with a four-bucket case-level diff (`regressed` / `fixed` / `new` / `changed`), not an aggregate.

Gate validator false-rejection rate **paired with** false-positive rate. Loosening comparators moves both in opposite directions, so either number alone is gameable.

---

## 9. Secrets and configuration

Configuration precedence: environment > `~/.config/shelfwarden/config.toml` > defaults.

Secrets — Plex token, TMDB bearer, TVDB apikey/pin, OpenAI and Anthropic keys — come from the environment or a git-ignored `.env`, never from a committed file and never from a CLI argument (arguments land in shell history and process listings).

The Plex token grants whatever the underlying account can do. Prefer a token scoped to a non-admin account for development if the server allows it, and keep destructive-capable tokens out of any eval or CI path.

Redact secrets in logs, spans, evidence records, and error messages. Assert it in a test rather than trusting review.

`config.py` implements this from step 0.4. Three details are load-bearing:

- **The token is `repr=False` *and* the `__repr__` is written out by hand.** The field flag alone is one refactor away from being regenerated with the default, and the leak-shaped mistake is a log line rather than a debugger.
- **`Settings.secrets` is the single list of things that must never be written**, and the export runs `iter_secret_hits` over every payload *before* anything lands on disk. A leak aborts the export rather than being cleaned up afterwards.
- **A missing setting names the variables to export.** "Configuration missing" is a dead end; the §5.4 correctable-error rule applies to configuration too.

Redaction replaces longest secret first, so a secret containing another does not leave a readable fragment behind.

---

## 10. Automated enforcement

`.claude/hooks/py-check.sh` runs on every `Write`/`Edit` via a `PostToolUse` hook configured in `.claude/settings.json`. It is the mechanization of §1.3 and §1.4 — the first pieces of this document to move from prose into code, as the preamble says they should.

What it does, on `.py` files only:

1. `ruff check --fix` then `ruff format` — **silently**. Auto-fixable problems are simply fixed; no output, no interruption.
2. Re-runs `ruff check` and surfaces anything left, since a remaining violation is one a human or Claude has to decide about.
3. Runs `lint-imports` when `[tool.importlinter]` is configured, and surfaces broken contracts with the offending import (`shelfwarden.core is not allowed to import json: -> json (l.1)`).

It stays silent — deliberately, so it never cries wolf — when the file is not Python, the project is not yet scaffolded, `uv` is absent, or the package is not yet installed in the venv. That last case is a "cannot run" condition, not a violation; conflating them would make the hook fire on every edit for the whole of early Phase 0 and train everyone to ignore it.

It also strips import-linter's large box-drawing banner, which is otherwise pure context noise.

**Verified behavior** (tested against a scratch project before being committed): silent on Markdown, silent on an unscaffolded repo, silent on a clean Python file, auto-fixes `def f( x ):` → `def f(x):`, surfaces an unfixable `F821`, and surfaces a real forbidden-import contract breach.

The hook is a fast feedback loop, not a gate — CI is the gate. Anything it catches should have been caught by reading the relevant section of this document first.

---

## 11. Project-specific invariants

The rules most easily violated by well-intentioned code, collected in one place:

1. **The scan and diagnose phases cannot mutate.** Not "should not" — the tools do not exist in those phases.
2. **Confidence is computed in code.** The model's number is recorded as `model_confidence` and gates nothing. The auto-apply guard reads `band`, never `value`.
3. **`band` gates auto-apply; it never gates eval scoring.** Scoring keyed on a tunable constant is gameable by tuning the constant.
4. **Every mutation is reversible, including lock-state changes.**
5. **A finding with an unbound referent is rejected**, however well-cited. Citation integrity and referent binding are separate checks and separate reported numbers.
6. **`unexpected: fail` is the default on every eval case**, not just should-not-touch. Otherwise most of the dataset is blind to fabricated findings.
7. **Case identity is semantic, never positional**, and never derived from a Plex `rating_key` (they move on rescan).
8. **No corruption ships without a detectability witness.** An unsolvable case depresses the score and hides regressions.
9. **Silence is not escalation.** An agent that finds nothing must not score as correctly escalating.
10. **Evidence freshness is checked before execution** — re-fetch by `query_id` and demote any auto-band repair whose backing evidence changed since approval.
