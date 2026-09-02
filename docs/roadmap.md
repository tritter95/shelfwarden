# ShelfWarden Roadmap

Progress tracker for the build order in [`shelfwarden.md`](./shelfwarden.md). System structure and diagrams live in [`architecture.md`](./architecture.md). Design detail for Phases 0–1 lives in [`implementation-plan.md`](./implementation-plan.md); stack conventions and verified library traps live in [`development-practices.md`](./development-practices.md).

**Gating rule (from the spec): do not begin a phase until the previous phase's gate is met.**

Legend — `[ ]` not started · `[~]` in progress · `[x]` done

---

## Phase 0 — The corruption harness

> **Gate:** `python -m shelfwarden.evals.generate --count 200` produces a labeled dataset, and a trivial scorer can compare any proposed repair set against the truth file.

### 0.1 Project scaffold

> Design detail and the verified import-linter findings behind the contracts: [`plans/step-0.1-scaffold.md`](./plans/step-0.1-scaffold.md).

- [x] `uv init`, `.python-version` → 3.13, `src/` layout, `[project.scripts]`
- [x] Typer CLI skeleton — `scan / diff / apply / revert / eval / export`
- [x] SQLite store: WAL, explicit `autocommit=False`, migration runner
- [x] Empty package skeleton per `implementation-plan.md` §1 — the Phase 2–5 seams present from day one (and a hard prerequisite: an import contract's `source_modules` must resolve to a real module)
- [x] pytest + pytest-asyncio (`asyncio_mode="auto"`, exercised by a test), ruff **configured and passing**, `--strict-markers`, `filterwarnings = ["error"]`, `slow`/`live` markers registered
- [x] CI workflow — `.github/workflows/ci.yml`: fast `check` job per push/PR, `nightly` full suite on cron
- [x] **`[tool.importlinter]` contracts** — enforce the architectural seams in CI from day one:
  - [x] `include_external_packages = true` (**required** — import-linter refuses to run without it when forbidden modules are external)
  - [x] `agent/tools/` must not import `agent/loop.py`, `agent/provider/`, or `evals/` (Phase 5 MCP seam)
  - [x] `library/plex.py` is the only module permitted to import `plexapi`
  - [x] the provider modules are the only ones permitted to import their respective SDKs
  - [x] `library/` and `sources/` must not import `agent/` — the same MCP boundary in the other direction (`development-practices.md` §1.2)
- [x] **Practices enforcement hook** — `.claude/hooks/py-check.sh` + `.claude/settings.json` (active; no-ops until scaffolded)
- [x] **Done when:** `uv run shelfwarden --help` works, a migration applies cleanly, and `lint-imports` passes in CI

### 0.2 Normalized media model

> Design detail, the plexapi/pydantic findings behind it, and the eight decisions taken: [`plans/step-0.2-normalized-model.md`](./plans/step-0.2-normalized-model.md).

- [x] `NormalizedItem` + subtypes covering movie / show / season / episode / **author** / audiobook / audiobook_part — `author` added because `author_name_variant` and `narrator_as_author` address the Artist and record an id set to merge (correction recorded in `implementation-plan.md`)
- [x] `ItemId` as composite `(provider, section_id, rating_key)`
- [x] `ExternalId` parsing for **both** new-agent `guids` (`tmdb://`, `tvdb://`, `imdb://`) and legacy `com.plexapp.agents.*` guid strings, including the `thetvdb://<id>/<season>/<episode>` path form and HAMA's nested source; unrecognised guids become `UNKNOWN` with `raw` intact rather than being dropped
- [x] Canonical-JSON round-trip — `canonical.py`, with `allow_nan=False`, NFC text normalization, and paths deliberately exempt
- [x] `FetchProfile` on every item — "no external ids" and "nobody asked" are different facts, and with `autoreload=false` plexapi cannot tell them apart
- [x] `with_changes` as the only mutation path — `model_copy(update=...)` does not validate
- [x] **Done when:** round-trip and both guid-form parsers are unit tested

### 0.3 Read-only Plex provider

> Design detail and the plexapi findings behind it: [`plans/step-0.3-plex-provider.md`](./plans/step-0.3-plex-provider.md).

- [x] `LibraryProvider` protocol — read methods only, no mutation in the type; `MUTATING_METHODS` declared alongside it and asserted disjoint
- [x] `PlexLibrary` mapping plexapi → `NormalizedItem`; plexapi types never escape this module (enforced statically by the import contract **and** dynamically by a test that walks returned values)
- [x] Global `autoreload=false` — set in the environment by `configure_plexapi()`, not merely documented, because the switch **fails open**: only lowercase `false`/`0` work and anything else is swallowed into a permissive default. Asserted on the config and again on a fetched object
- [x] `setDatetimeTimezone("utc")` — plexapi otherwise returns naive **local-time** datetimes, making an export depend on the machine that produced it
- [x] Explicit `reload()` include sets, declared as data per `FetchProfile` so step 0.4's manifest can record what produced a record
- [x] Paging passes **both** `container_start` and `maxresults`; `limit` is a required argument, and `Page.total` comes from the response rather than from `len(items)`
- [x] `LibraryError` taxonomy translated at the boundary, carrying `Retryability` and a required `next_action` for correctable errors; covers the `requests` connection/timeout errors plexapi never wraps
- [x] Own `requests.Session` with retry, backoff, and a status-recording hook — plexapi collapses 429/500/502/503 into `BadRequest`, so the status is otherwise available only as prose
- [x] Audiobook detection heuristics (section agent id, `.m4b` share, album-per-book structure), returning a verdict that carries its signals, thresholds, and sample size
- [x] Added the `plexapi` `ignore_imports` line to the import contract — CI did break on the first `import plexapi`, exactly as designed. **One** line, not the pair originally planned: grimp collapses external packages to a single node, so a `plexapi.**` line matches nothing and fails the run (correction recorded in `development-practices.md` §1.3)
- [x] Offline fixture harness — committed XML plus a stub server whose `query()` raises, so no test can reach the network and the auto-reload guarantee is provable in CI
- [x] **Done when:** a test asserts the protocol exposes no mutating method, and audiobook detection passes against committed fixtures

### 0.4 Library export + census

> Design detail, the plexapi include-set findings behind it, and the seven decisions taken: [`plans/step-0.4-export-census.md`](./plans/step-0.4-export-census.md).

- [x] `shelfwarden export` → deterministic JSONL + `manifest.json`, written into one directory per export via temp-dir-then-`os.replace`, so an interrupted run leaves nothing rather than something plausible
- [x] Capture current **field lock state** (needed later for revert), with a test that it survives the round trip
- [x] Emit a **census**: counts by section, agent type, media kind, container — in **two tiers, each labelled with its own basis**. Population is exact from the listing walk; the slice tier carries `coverage: {records, population}` on every block, because a guid count taken from a 200-item slice and printed without its `of` reads as a statement about the library
- [x] Census counts guids by namespace, **including `UNKNOWN` with examples** — the legacy guid forms in step 0.2 could not be verified without a live legacy-agent library, so the real export reports which forms actually exist instead of the parser guessing. Example lists are capped and the cap reports what it dropped
- [x] `--census-only` walks listings and writes the population tier alone, with no per-item fetches — the command you run *first*, to choose `--count` from evidence
- [x] `--count` counts **roots**, and a family that would exceed `--max-records` is dropped whole and recorded in `manifest.dropped`. Never truncated: half a show is an unsolvable case in 0.5 and a mysteriously depressed score in 0.8
- [x] `provider_info()` added to the protocol so the manifest can name its source without `evals/` reaching through `PlexLibrary._server`; the machine identifier is **hashed**, never recorded raw. New import contract forbids `shelfwarden.evals` → `shelfwarden.library.plex`
- [x] `FilePart` gained `media_id` / `part_id` — a part had no name at all, and `parts[2]` is a positional identifier of the kind invariant 9 rejects (correction recorded in `implementation-plan.md` §3)
- [x] The manifest records the **effective** request parameters — what plexapi's `_buildDetailsKey` actually puts on the wire — not our `RELOAD_INCLUDES` override dict, which understates the request while looking authoritative (finding recorded in `development-practices.md` §4.3)
- [x] **Done when:** re-running the export against an unchanged library is byte-identical, and the census informs slice targets — asserted in-process *and* across two `PYTHONHASHSEED` values in forked subprocesses, since a same-process comparison cannot see hash-order leakage at all

### 0.45 Shared comparators + mechanical screen

> Design detail, the six verified findings behind it, and the seven decisions taken: [`plans/step-0.45-comparators-screen.md`](./plans/step-0.45-comparators-screen.md).

- [x] `compare.py` — the comparator library shared by scorer, screen, detectability witness, **and the Phase 1 validator**. Top-level, not `evals/`: `agent/validate.py` is a consumer, and the agent must not import the answer-key package across the Phase 5 MCP seam (correction recorded in `implementation-plan.md` §7)
- [x] `SupportStrength` as a `StrEnum` (`EXACT|ALIAS|NORMALIZED|FUZZY|NONE`) with an explicit rank table — never bool, and never an `IntEnum` that writes bare integers into every dataset
- [x] Comparators take `(observed, authority)` **in that order**, pinned by a test: `difflib.SequenceMatcher.ratio()` is not symmetric (verified: 9228 asymmetric pairs over a 3-letter alphabet; `ratio('ab','bacb')` is 0.667 one way and 0.333 the other)
- [x] `autojunk=False` passed explicitly on every `SequenceMatcher` — the default silently returns 0.0033 where the honest answer is 0.5, for any comparison where the second string reaches 200 characters. Summaries reach it
- [x] Fold ladder normalizes to NFC **after** casefolding, not before — `casefold()` does not preserve NFC, and `FilePart.path` is deliberately un-normalized, so NFD text reaches the comparators through the filename checks
- [x] `evals/screen.py` — LLM-free per-predicate verification over the export
- [x] Screen emits `guarded_classes` / `unguarded_classes` per item; requires ≥3 applicable checks, all passing. `MIN_APPLICABLE_CHECKS` is a constant with no CLI flag — a flag makes the slice's admission standard a runtime argument
- [x] Four predicate statuses, not two: `pass` / `fail` / `not_applicable` / `unavailable`. Neither of the last two counts as a pass, which is what lets the authority tier be deferred with no conditional logic
- [x] Three verdicts, not two: `guarded` / `failed` / `insufficient`. The `insufficient` count is a coverage metric on the screen itself
- [x] Uniqueness predicates run at **population** scope, not slice scope — a duplicate that simply was not sampled would mark an item guarded and score the agent's correct finding as a false positive. Forces `roots.jsonl` on the export (manifest `schema_version` 1 → 2)
- [x] Authority tier shipped as an `AuthorityIndex` protocol + `NullAuthority`: `sources/` is step 1.1, so six of eleven predicates report `unavailable` and nine of fifteen classes stay unguarded until then. **Guard coverage per class is a published number**, so `fp_rate_snt` states its own denominator
- [x] Author-twin detection blocks on a token-set key; the blocking scheme, bucket count, and comparison count are all recorded (no silent caps)
- [x] `models/finding.py` gains `ProblemClass` and `models/evidence.py` gains `Source` + `evidence_id()` — the minimum the screen needs, at the paths the implementation plan already assigns them
- [x] Items failing the screen become candidates for the curated real slice, carrying the failing predicate and its evidence
- [x] Screen writes to `datasets/screens/<export_id>/`, never into the export directory, bound to its source by `items_sha256`
- [x] New import contract: `compare` may not import `agent`, `evals`, `library`, or `sources`
- [x] Local tier guards **seven** classes, not the six the plan predicted: `absolute_vs_seasonal` is guarded weakly by `episode_numbering_contiguous`, which is local. The count is computed from `GUARD_TABLE`, never asserted in prose
- [x] `screen.json` carries no timestamp — a screen is a pure function of the export and the code that read it, and byte-identity is the cheapest proof of that
- [x] **Done when:** the screen classifies the full export and its output feeds both the should-not-touch slice and real-slice labeling

### 0.5 Corruption functions

> Design detail, the seven verified findings behind it, and the eight decisions taken: [`plans/step-0.5-corruption-functions.md`](./plans/step-0.5-corruption-functions.md).

**Eleven of fifteen classes ship.** Three need an external record as an ingredient or as a witness and land with `sources/` in step 1.1; one is not synthesizable by design. The split is `registry.CORRUPTION_TABLE` and `registry.UNSYNTHESIZABLE_REASON` — a table, so the counts are computed and a test asserts every class appears in exactly one of them.

Movies / TV
- [x] `wrong_match` — donor from the same section; witness is the filename the corruption does not touch
- [x] `year_collision_remake` — donor is a genuine remake partner (same title, **different** year)
- [ ] `foreign_title_variant` — **deferred to 1.1**: the corruption substitutes a real TMDB `alternative_titles` entry, and inventing one produces a case about a film that does not exist
- [x] `alternate_cut` — one variant (`strip_edition`); `collide` needs a library with two *marked* cuts, which the census has not yet shown to exist
- [ ] `missing_metadata` — **deferred to 1.1**: detecting the empty summary is local, resolving its true value is not, and weakening the postcondition to "non-empty and cited" would make the metric meaningless
- [x] `duplicate_quality` — adds a root with a minted key; twin test is `(title, year)`, so a remake pair is not mistaken for a duplicate
- [x] `episode_wrong_season` — `reparent` only; `index_only` is unrepresentable in Plex and is excluded with the reason recorded
- [x] `absolute_vs_seasonal` — renumbers the whole show and **removes** the seasons it empties
- [x] `filename_unmatchable` — the scene name written is deliberately parseable; `opaque_hash` would be unsolvable rather than hard

Audiobooks
- [x] `series_order_broken` — needs three positioned books and a position named in a path segment, matched as a token (`"1" in "CD1"` is not a position)
- [x] `author_name_variant` — witness is `compare_person_name` at ALIAS, so the guard is not threshold-dependent
- [ ] `narrator_as_author` — **deferred to 1.1**: needs a real narrator name; another author's name from the export is `author_name_variant` wearing a different label
- [x] `multi_file_split` — relation witness over stripped disc markers **and** the shared parent directory
- [x] `missing_series` — witness is the series folder; the candidate test applies the *policy*, not a bare non-`NONE` support
- [ ] `anthology_omnibus` — **not synthesizable by design**: the expectation is `escalate`, so it needs an ambiguity witness and constituent titles from an authority. Curated in 0.9

Structure (three corrections to `implementation-plan.md` §3, each forced by a class it could not express)
- [x] **The unit is a family, and the record is an item-set delta** — `ADD` / `REMOVE` / `MODIFY`. Five classes add or remove items, and "a new item appeared" is not a field change on an old one
- [x] **One pointer grammar** — `pointer.py`, RFC 6901, with `*` as a whole segment permitted in a *selector* and forbidden in a change path. `/parts/*/path`, not `parts[*].file`: the model has no `file` field. New import contract, `the pointer language is a leaf`
- [x] **Three witness kinds** — `VALUE` (a field's true value is recoverable), `RELATION` (two or more ids are provably one thing), `AMBIGUITY` (defined, unused until `anthology_omnibus` is curated). The specified witness describes only the first

Detectability (no case ships unproven)
- [x] `DetectabilityWitness` on every `CorruptionResult`, with the tier that decides whether a class can ship before 1.1
- [x] Generator runs the **scorer's own comparators**: witness ≠ corrupted, witness == ground truth, both judged by a `Policy` rather than a threshold in the corruption
- [x] **Anti-circularity is structural**: `LocalWitness` is constructed over the *corrupted* items and cannot see the clean family, and every pointer is verified to resolve against the corrupted world
- [x] No-op `FieldChange` (`before == after`) raises rather than warns — decided on **canonical bytes**, since `True == 1` in Python and `b'true' != b'1'` in JSON
- [x] `before`/`after` **read back from the dumped item**, never from the caller's intent: validation rewrites NFD titles to NFC, re-sorts guids, and deduplicates `locked_fields`
- [x] Property test: `apply_reverse(changes)` == `ground_truth_item` byte-for-byte, over every class
- [x] Rejected cases → `rejected.jsonl`, rolled into the per-class deficit report, with **not-applicable and rejected counted apart** — "no remake pairs in this library" and "the harness refused what it built" are different facts and only one is actionable
- [ ] Witnesses content-addressed into a committed evidence store — **deferred to 1.1** with the authority tier. No corruption emits an authority witness yet, and a stub store would be a shape chosen with no evidence. `WitnessTier.AUTHORITY` exists so 1.1 adds an implementation rather than a concept

Corroboration (found by building it)
- [x] Every emitted case is **re-screened**, and a corruption the screen still guards afterwards is rejected (`screen_intact`). Five verdicts: `broken` / `intact` / `already_failing` / `unavailable` / `unguarded`, comparing **before against after** — with a `NullAuthority` most guards are unavailable anyway, so "not guarded afterwards" alone would report every case as a success
- [x] **Two `GUARD_TABLE` rows corrected**, both claiming guards they did not have. `episode_wrong_season` was guarded by `season_membership_coherent`, which stays *passing* on a re-parented episode because re-parenting is internally consistent; `absolute_vs_seasonal` was guarded by `episode_numbering_contiguous`, which passes on exactly the numbering it was meant to detect (S01E01..S01E52 is contiguous). Both now key on `filename_matches_metadata`
- [x] Corruptions declare `induced` (problems created inside the family) and `collateral` (ids **outside** it whose population-scoped guard moved) — verified: corrupting one film strips an untouched film's `duplicate_quality` guard, and with a stale `roots.jsonl` the twin relation goes *asymmetric* and the screen reports a guard that is false
- [x] Selection is by **hash rank**, never `random.sample` — verified non-prefix-stable in `k`: `Random(1518).sample(range(24), 5)` selects element 23 and `sample(range(24), 6)` does not. A drawn subject would reset every `case_id` in a cell on a one-case composition edit. A test parses the package and fails on any `sample`/`choices` call
- [x] Per-case RNG seeded from `(seed, subject_key, class, variant)` — the same tuple `case_id` will hash — so a case is independent of the run it was generated in
- [x] `subject_key` ladder built here rather than in 0.6, because the RNG needs it: external id → title/year hash → path hash, **never** a rating key

- [x] `shelfwarden corrupt <export>` → `datasets/corruptions/<export_id>/`: `corruptions.json`, `rejected.jsonl`, `corruptions.md`. No timestamp, byte-identical across two `PYTHONHASHSEED` values. The table you read *before* writing `composition.toml`, the way `--census-only` is the one you run before choosing `--count`
- [x] **Done when:** each function has a unit test asserting the mutation applied, the truth record round-trips, and the case is provably detectable

### 0.6 Truth schema + generator
- [ ] `TruthFile` schema with `expectation.kind` ∈ `repair | no_action | escalate`
- [ ] `unexpected: fail` as the **default on every case** (closes the 85%-of-dataset FP blind spot)
- [ ] `known_other_problems` — verified pre-existing issues, neither required nor penalized
- [ ] `no_action` narrowed to screen-verified `guarded_classes`; unguarded findings scored `unverified`
- [ ] `escalate` made threshold-free: `require_finding`, `require_needs_human`, `min_candidates`
- [ ] `postcondition` + `must_not_change` replace operation matching; generated from the inverse of `corruption.changes`
- [ ] Semantic `case_id = sha256(slice, problem_class, media_kind, subject_key, corruption_variant)`
- [ ] `subject_key` ladder (external id → normalized title+year → path hash) — **never** the Plex rating_key
- [ ] `generator_version` excluded from `case_id`; `corruption_fingerprint` carries it separately
- [ ] `lineage_id` for baseline keying (not `dataset_id`, which resets on every re-export)
- [ ] `run_group` — execution grouping that is not a scoring grouping
- [ ] One case = one atomic repair (one duplicate pair / one author's variant set / one book's split files)
- [ ] Seeded RNG; reproducible dataset id
- [ ] `composition.toml` at repo root — per-media-kind and per-class shares, normalized at load
- [ ] Resolved absolute per-cell targets written into `dataset.json` so datasets stay interpretable without the toml
- [ ] Curated slice merge — `datasets/curated/real.yaml`, `ambiguous.yaml`
- [ ] **Explicit per-class deficit reporting** when the export can't fill a slice
- [ ] **Done when:** `generate --count 200 --seed N` is reproducible and never silently unbalances the dataset

### 0.7 Snapshot provider
- [ ] `SnapshotLibrary` serving the corrupted dataset through the identical `LibraryProvider` protocol
- [ ] Same error taxonomy and pagination semantics as `PlexLibrary`
- [ ] **Done when:** a provider-conformance test suite passes against both implementations

### 0.8 Scorer — **the gate**
- [ ] Component scoring (tool-set overlap by problem class; `repair_op` advisory only)
- [ ] Trajectory scoring (external source consulted, budget respected, no out-of-phase tools) — **not** repeated-call detection, which the Phase 2 guard will pin at 100%; score `denial_count` by reason instead
- [ ] Outcome scoring via `postcondition` + `must_not_change` literal comparison against `ground_truth_item`
- [ ] Citation integrity and referent binding reported as **two separate numbers**
- [ ] Metric rollups sliced by media kind and problem class:
  - [ ] pass rate, and pass rate broken down by `provenance.method`
  - [ ] `fp_rate_snt` (should-not-touch) **and** `collateral_fp_rate` (all cases)
  - [ ] correct-escalation rate, `silence` rate, `unverified` rate
  - [ ] validator false-rejection rate (gated in CI *paired* with FP rate)
  - [ ] refutation rate (dataset quality)
  - [ ] median/p95 steps, median/p95 cost, cost per 100
  - [ ] `auto_apply_rate(t)` as a sweep, not a point
- [ ] CI diff with four buckets: `regressed` / `fixed` / `new` / `changed`
- [ ] Markdown report renderer for the README table
- [ ] **Done when:** the scorer produces the metric table from a hand-written repair set, with no agent involved

### 0.9 Real-slice labeling workflow
- [ ] Screen failures pre-populated as candidates with failing predicate + evidence + proposed ground truth
- [ ] Adjudication UI/format targeting ~60s per case
- [ ] 20% double-labeled audit sample; inter-rater agreement published once
- [ ] `provenance` recorded; **no `label_confidence` fed to the scorer**
- [ ] Uncertainty handled by slice reassignment (unsure of answer → ambiguous; unsure a problem exists → discard)
- [ ] Adjudication queue for should-not-touch findings produced during real runs; `human_refuted` demotion path
- [ ] **Done when:** the curated real slice is labeled and the dataset can self-heal from refutations

---

## Phase 1 — Read-only agent, no framework

> **Gate:** 20+ eval cases run end to end and produce a scored report. The score may be bad; it must exist.

### 1.1 Metadata sources
- [ ] Shared HTTP client: per-source throttle, backoff, response cache
- [ ] TMDB (v4 Bearer; `/search/*`, `/find/{external_id}`, `append_to_response`)
- [ ] TVDB v4 (JWT login + refresh; `/search`; `/series/{id}/episodes/{season-type}`)
- [ ] Audnexus (`/books/{asin}`, `/authors?name=`) — **no book search exists**
- [ ] Open Library (mandatory descriptive contact `User-Agent`; 3 req/s)
- [ ] `sources/asin.py` — ASIN resolution ladder (guid → author search → correctable error)
- [ ] vcrpy cassettes captured once from real APIs; respx tests for error paths
- [ ] **Done when:** every source has recorded fixtures and its rate-limit policy is enforced in code

### 1.2 Provider interface
- [ ] `LLMProvider` protocol, `Proposal`, `ToolSpec`, `Usage`
- [ ] Provider-opaque `ProviderCarryover` for reasoning/thinking replay
- [ ] OpenAI **Responses API** implementation
- [ ] Pydantic `$ref`/`$defs` inliner for tool schemas
- [ ] Add the `openai` `ignore_imports` pair to the import contract (both `openai` and `openai.**` — a bare wildcard is rejected)
- [ ] **Done when:** a proposal round-trips with `raw` bytes stored verbatim

### 1.3 Tool registry + read-only tools
- [ ] `ToolRegistry` keyed by `RunPhase` — the structural read/write seam
- [ ] `list_library_items`, `get_item_details`, `get_file_info`, `find_similar_items`
- [ ] `search_metadata` (consolidates TMDB / TVDB / Open Library behind a `source` enum)
- [ ] `get_evidence` (read-only local evidence store lookup — re-grounding after truncation)
- [ ] `get_tvdb_episode_order` (cross-referenced by TVDB episode id, not by numbers)
- [ ] `lookup_audiobook` (replaces the spec's unbuildable `search_audnexus`)
- [ ] `record_finding`
- [ ] Error taxonomy: retryable (never surfaced) / correctable / terminal
- [ ] **Done when:** a test asserts `tools_for(phase) ∩ MUTATING_TOOLS == ∅` for every non-`executing` phase

### 1.4 Findings + validator
- [ ] Discriminated claim union: `ExternalClaim` / `ObservationClaim` / `DerivedClaim`
- [ ] `DerivedClaim` carries **no** `asserted_value`; validator re-executes the named `rule_id`
- [ ] Derived-rule registry (`SAME_WORK_DIFFERENT_QUALITY`, `AUTHOR_NAME_VARIANT`, `PARTS_OF_ONE_BOOK`, …)
- [ ] `EvidenceRecord` with `Source.LIBRARY`, dual `evidence_id` + `query_id`, and `field_index`
- [ ] Auth headers and API keys stripped from `normalized_params` before hashing *and* storing
- [ ] Source adapters emit `field_index` at retrieval; cassette test asserts non-empty per advertised field

Validator checks
- [ ] 1 — provenance (evidence id exists in this run)
- [ ] 2 — resolution (pointer resolves **and** is in `field_index` for the claimed field)
- [ ] 3 — support (per-field comparator returning `SupportStrength`, alias sets consulted before fuzzy)
- [ ] 4 — **referent binding** (`SubjectMatch` computed in code; `unbound` on an identity claim rejects)
- [ ] `AbsenceCitation` + authority table (`can_prove_absence`, `absence_semantics`, `support_cap`)
- [ ] Per-repair-op required-claims table
- [ ] Computed `Confidence{value, band, reasons}`; guard reads `band`, never `value`; `model_confidence` recorded but gates nothing
- [ ] Rejections surfaced to the model as correctable errors, capped at 3 per finding then terminal
- [ ] `get_evidence(evidence_id, pointer)` read-only tool; `field_index` keys retained in `result_summary`
- [ ] **Done when:** fabricated id, unresolvable pointer, wrong-field pointer, non-supporting value, and unbound referent are each rejected by a test

### 1.5 Run state + persistence
- [ ] `RunState`, `Step`, `RunPhase`
- [ ] Tables: `runs`, `steps`, `blobs`, `evidence`, `findings`, `claims`
- [ ] **Raw model responses stored verbatim** (this is what makes Phase 2 replay a config change)
- [ ] **Done when:** a run persists and reloads losslessly

### 1.6 The loop
- [ ] Hand-rolled `while not state.done` loop per the spec skeleton
- [ ] Budget: step cap, cost cap, wall-clock deadline
- [ ] Guard stub with the real `evaluate(proposal, state) -> Decision` signature + schema and phase checks
- [ ] Context assembly with truncation that **preserves evidence ids**
- [ ] **Done when:** a run completes against `SnapshotLibrary` within caps

### 1.7 Eval runner — **the gate**
- [ ] `shelfwarden eval --dataset <id> --provider openai`
- [ ] Scored report across 20+ cases with all spec metrics
- [ ] **Done when:** the report exists and is sliced by media kind and problem class

### 1.8 Second provider
- [ ] Anthropic Messages implementation behind the same interface
- [ ] Per-case result diff between providers
- [ ] Add the `anthropic` `ignore_imports` pair to the import contract
- [ ] **Done when:** the same suite runs against both and the diff is reported

---

## Phase 2 — Guard layer, budgets, observability

> **Gate:** an incident in a real scan can be diagnosed by reading the trace, and replayed deterministically.

- [ ] Ordered guard chain: schema → existence → budget → rate limit → loop detection → business rules
- [ ] Loop detection on `(tool, normalized_args)` hashes
- [ ] Per-run cost cap, step cap, wall-clock deadline (promoted from the Phase 1 stub)
- [ ] OpenTelemetry spans per step: context by reference, raw response pre-parse, guard decision, tool result, tokens, cost, latency
- [ ] `telemetry/otel.py` owns all `gen_ai.*` attribute naming; emits both `gen_ai.system` and `gen_ai.provider.name` during the semconv rename
- [ ] Arize Phoenix wired as the OTLP backend (single container, or `phoenix serve`)
- [ ] `ReplayProvider` — deterministic re-execution against recorded responses, zero model calls
- [ ] Guard unit tests
- [ ] **Done when:** a recorded incident replays byte-identically and the trace alone explains it

---

## Phase 3 — The repair stage

> **Gate:** a full corrupt → repair → verify cycle on the synthetic dataset, plus a successful `revert`.

- [ ] State machine: `scanning → diagnosing → planning → awaiting_approval → executing → done`
- [ ] `MutableLibraryProvider` protocol; mutating tools registered **only** in `executing`
- [ ] Snapshot-before-mutate store
- [ ] `shelfwarden revert <plan-id>` restoring pre-plan state — **including field lock state**
- [ ] Idempotency keys from `(plan_id, item_id, operation)`
- [ ] Approval UI: readable diff grouped by problem class, with evidence and `Confidence.reasons` per finding
- [ ] Dry run as the default; `--commit` required to mutate
- [ ] **Evidence freshness check before execution** — re-fetch by `query_id`; if any `evidence_id` backing an `auto`-band repair changed since approval, demote it to `review`
- [ ] Compensation-failure alerting that never fails silently

Guard rules
- [ ] Never delete a file — move and rename only, both revertible
- [ ] Never merge duplicates without approval, regardless of confidence
- [ ] Never apply below the confidence threshold — surface as "needs human decision"
- [ ] Cap repairs per plan; split beyond it
- [ ] Flag (never auto-apply) any item edited by the user since the last scan

- [ ] **Done when:** corrupt → repair → verify → revert completes on the synthetic dataset

---

## Phase 4 — LangGraph (deliberate, measured)

> **Gate:** the same eval suite runs against both loop implementations and the per-case diff is empty or explained; an approval gate round-trips through `interrupt()` / `Command(resume=...)` across a killed process; and the writeup records what the framework bought, what it cost, and which of the modules it was not supposed to touch it had to.

> Decision record, the seven verified findings and the six decisions taken: [`plans/phase-4-langgraph.md`](./plans/phase-4-langgraph.md). Verified 2026-09-01 against `langgraph` 1.2.11 and `langchain` 1.3.18; no dependency is installed yet, so Findings 3 and 4 are documented behavior awaiting a test in this checkout.

**LangGraph is adopted; LangChain is not.** `langchain` 1.3.18 depends on `langgraph` — it is now the agent layer *above* the runtime, and every abstraction it sells has a counterpart here that exists for a recorded reason: `LLMProvider` (1.2), the `RunPhase`-keyed tool registry (1.3), the groundedness validator (1.4). Its message normalization would sit between the model and the verbatim bytes that make Phase 2 replay a config change rather than a rewrite. `langchain-core` arrives anyway as a hard dependency of `langgraph`, so it is confined by contract rather than avoided — the same treatment `plexapi`, `openai` and `anthropic` already get.

### 4.1 Spike — non-gating, may run as soon as 1.7 exists
- [ ] Port `agent/loop.py` to a `StateGraph` under `agent/graph/`: `InMemorySaver`, no interrupts, no import contract, no promises
- [ ] Run `shelfwarden eval` against both implementations and diff per case
- [ ] **The one exception to the build order in this project.** It is licensed by being non-gating — nothing depends on it, and Phases 2 and 3 proceed unchanged whether it succeeds or fails — and by 1.7 being the earliest point at which a port can be *measured* instead of merely written
- [ ] **Done when:** the diff exists and the findings are written down. The code itself is disposable

### 4.2 The port proper
- [ ] `langgraph` + `langgraph-checkpoint-sqlite` added — **introduces persistent state, so it needs an explicit go-ahead first** per the working rules
- [ ] Eighth import contract: `langgraph` and `langchain_core` confined to `agent/graph/`
- [ ] `SqliteSaver`, with `durability` (`"exit"` / `"async"` / `"sync"`) chosen deliberately and the other two costed rather than defaulted
- [ ] The checkpointer is a **resumption mechanism, never the audit log** — scoring, metrics and `revert` keep reading `runs` / `steps` / `blobs`, and the graph writes the same step rows the hand-rolled loop does
- [ ] `--engine loop|graph` on the eval runner; both run in CI
- [ ] `agent/tools/`, `agent/validate.py`, `agent/provider/`, `agent/guard/`, `agent/state.py`, `compare.py` and `evals/` all untouched. `development-practices.md` §1.2 has claimed since 0.1 that `agent/loop.py` is the only module LangGraph would replace; this is the test of it, and a module the port has to change is a **finding about the seam**, recorded, not a quiet edit
- [ ] **Done when:** the same suite passes on both engines and the per-case diff is empty or explained

### 4.3 Interrupts — the approval gate
- [ ] Phase 3's `awaiting_approval` moves onto `interrupt()` / `Command(resume=...)`
- [ ] **No snapshot write and no mutating tool call above an `interrupt()`.** The node re-executes from the top on resume, so everything above the call runs twice — here that is Plex edits and snapshot writes, on the one transition where mutations live. Phase 3's `(plan_id, item_id, operation)` idempotency keys were designed for retries, not for a framework that replays the node body by design. Enforced by a test, not by review
- [ ] **Done when:** an approval round-trips across a killed process and applies exactly once

### 4.4 The writeup
- [ ] What the framework bought, what it cost, and what it forced
- [ ] Whether LangGraph durability makes Phase 5's Temporal step unnecessary — "we no longer need it" is a legitimate outcome
- [ ] **Done when:** §6 of the decision record is no longer empty

---

## Phase 5 — MCP server + durable execution

- [ ] Extract Plex + metadata tools into a standalone, separately published **MCP server**
- [ ] Move orchestration to **Temporal** once full-library scans make crash recovery genuinely necessary
- [ ] Take that decision *after* 4.2 has a measured answer — Findings 4 and 5 of the Phase 4 record cover exactly the crash-recovery and suspension cases Temporal is being justified by
- [ ] Document what changed and why the pain justified it

---

## Definition of done

- [ ] README opens with a metrics table: pass rate by media type and problem class
- [ ] False-positive rate on the should-not-touch slice
- [ ] Cost per 100 items and p95 steps per item
- [ ] Count of repairs correctly declined as too ambiguous
- [ ] Repo contains: working corruption generator, labeled eval dataset, deterministic replay, guard layer with unit tests, and a `revert` demonstrated on the real library

---

## Standing rules

- [ ] Every fix for an observed failure ships with an eval case that **fails before and passes after** (§9)
- [ ] Ask before adding any dependency that introduces persistent state or a new service (§9)
- [ ] CI: fast suite per commit, full suite nightly; gate on **relative** change — no case that passed may now fail; report a case-level diff
- [ ] Attribution required by TMDB and TVDB free-tier terms is present in the README
