# ShelfWarden Roadmap

Progress tracker for the build order in [`shelfwarden.md`](./shelfwarden.md). Design detail for Phases 0–1 lives in [`implementation-plan.md`](./implementation-plan.md); stack conventions and verified library traps live in [`development-practices.md`](./development-practices.md).

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
- [ ] `NormalizedItem` + subtypes covering movie / show / season / episode / audiobook / audiobook_part
- [ ] `ItemId` as composite `(provider, section_id, rating_key)`
- [ ] `ExternalId` parsing for **both** new-agent `guids` (`tmdb://`, `tvdb://`, `imdb://`) and legacy `com.plexapp.agents.*` guid strings
- [ ] Canonical-JSON round-trip
- [ ] **Done when:** round-trip and both guid-form parsers are unit tested

### 0.3 Read-only Plex provider
- [ ] `LibraryProvider` protocol — read methods only, no mutation in the type
- [ ] `PlexLibrary` mapping plexapi → `NormalizedItem`; plexapi types never escape this module
- [ ] Global `autoreload=false`; explicit `reload()` include sets
- [ ] Paging passes **both** `container_start` and `maxresults`
- [ ] `LibraryError` taxonomy translated at the boundary
- [ ] Audiobook detection heuristics (section agent id, `.m4b` share, album-per-book structure)
- [ ] Add the `plexapi` `ignore_imports` pair to the import contract — CI **will** fail on the first `import plexapi` until it is there, by design (see `plans/step-0.1-scaffold.md`)
- [ ] **Done when:** a test asserts the protocol exposes no mutating method, and audiobook detection passes against committed fixtures

### 0.4 Library export + census
- [ ] `shelfwarden export` → deterministic JSONL + `manifest.json`
- [ ] Capture current **field lock state** (needed later for revert)
- [ ] Emit a **census**: counts by section, agent type, media kind, container
- [ ] **Done when:** re-running the export against an unchanged library is byte-identical, and the census informs slice targets

### 0.45 Shared comparators + mechanical screen
- [ ] `evals/compare.py` — the comparator library shared by scorer, screen, and detectability witness (`SupportStrength`, not bool)
- [ ] `evals/screen.py` — LLM-free per-predicate verification over the export
- [ ] Screen emits `guarded_classes` / `unguarded_classes` per item; requires ≥3 applicable checks, all passing
- [ ] Items failing the screen become candidates for the curated real slice
- [ ] **Done when:** the screen classifies the full export and its output feeds both the should-not-touch slice and real-slice labeling

### 0.5 Corruption functions
Movies / TV
- [ ] `wrong_match`
- [ ] `year_collision_remake`
- [ ] `foreign_title_variant`
- [ ] `alternate_cut`
- [ ] `missing_metadata`
- [ ] `duplicate_quality`
- [ ] `episode_wrong_season`
- [ ] `absolute_vs_seasonal`
- [ ] `filename_unmatchable`

Audiobooks
- [ ] `series_order_broken`
- [ ] `author_name_variant`
- [ ] `narrator_as_author`
- [ ] `multi_file_split`
- [ ] `missing_series`
- [ ] `anthology_omnibus`

Detectability (no case ships unproven)
- [ ] `DetectabilityWitness` on every `CorruptionResult`
- [ ] Generator runs the **scorer's own comparator**: witness ≠ corrupted, witness == ground truth
- [ ] No-op `FieldChange` (`before == after`) raises rather than warns
- [ ] Property test: `apply_reverse(changes)` == `ground_truth_item` byte-for-byte
- [ ] Rejected cases → `datasets/<id>/rejected.jsonl`, rolled into the per-class deficit report
- [ ] Witnesses content-addressed into the shared evidence store and committed with the dataset

- [ ] **Done when:** each function has a unit test asserting the mutation applied, the truth record round-trips, and the case is provably detectable

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

## Phase 4 — Framework migration (optional, deliberate)

- [ ] Port the loop to LangGraph for checkpointing/interrupt semantics
- [ ] Keep guard layer, tools, and eval harness framework-independent
- [ ] Run the same eval suite against both implementations and diff results
- [ ] Document what the framework actually bought

---

## Phase 5 — MCP server + durable execution

- [ ] Extract Plex + metadata tools into a standalone, separately published **MCP server**
- [ ] Move orchestration to **Temporal** once full-library scans make crash recovery genuinely necessary
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
