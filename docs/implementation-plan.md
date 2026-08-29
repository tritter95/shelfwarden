# ShelfWarden — Phase 0 + Phase 1 Implementation Plan

## Context

`docs/shelfwarden.md` specifies an agentic Plex library steward: it audits a library, diagnoses metadata/organization problems, proposes repairs, and applies them only after approval. The repo is empty apart from that spec and a LICENSE — this is a greenfield build.

The spec is explicit that **this is a learning project first**: the deliverable is a production-grade agentic system (eval harness, guard layer, tracing, staged autonomy) with no agent framework in v1. It states the ordering plainly — "the eval suite is the product and the agent is the thing being measured," and "the agent itself is the least interesting artifact here."

Accordingly this plan covers **Phase 0 (the corruption harness) and Phase 1 (the read-only agent loop)** in executable detail, and treats Phases 2–5 as a roadmap. The spec's own gating rule forbids starting a phase before the previous gate is met, so planning Phase 3 in detail now would be planning against unknowns that Phase 0 and 1 will surface.

Decisions confirmed with the user before planning:
- A live Plex server and token are available now, so Phase 0 exports a real library slice as ground truth.
- OpenAI is implemented first behind the provider interface; Anthropic follows.
- TMDB and TVDB v4 API keys are available.

---

## Verified external constraints

The spec's §9 requires verifying API surfaces rather than assuming them. This was done live on 2026-08-25; several findings **contradict assumptions embedded in the spec** and change the design.

### python-plexapi 4.18.2 (repo moved to `github.com/pushingkarmaorg/python-plexapi`)

| Finding | Consequence for this plan |
|---|---|
| **No read-only mode exists.** Every method is an HTTP call the server accepts based on token permissions. | The read-only guarantee must be enforced structurally in our own code. §3.2's "no mutating tool is registered or reachable" is on us. |
| **No audiobook library type.** Sections are only `movie`, `show`, `artist`, `photo`. Audiobooks are conventionally a **Music/`artist`** section: Author→Artist, Book→Album, Chapters→Tracks, via the legacy `Audnexus.bundle` agent. | Audiobook handling needs detection heuristics (section agent identifier, `.m4b` container, structure) — there is no `item.type == 'audiobook'` to branch on. |
| `section.all()`/`search()` internally **loop until the whole remaining result set is fetched**. True single-page paging requires passing *both* `container_start` and `maxresults`. Default container size is 100 (docs wrongly say 50). | The `list_library_items` tool must use the `container_start` + `maxresults` pair, or it will silently pull the entire library. |
| **Partial objects auto-reload**: touching a `None`/`[]` attribute on a partial object silently triggers a network refetch. Disableable via config `autoreload=false`. | Snapshot export must control this explicitly or the export is neither deterministic nor cost-bounded. |
| **plexapi cannot rename or move files on disk.** | Filename-driven repairs require an out-of-band filesystem move plus `section.update(path=...)`. Affects Phase 3 design; noted now so Phase 0 labels these cases correctly. |
| **No retry, backoff, or rate limiting anywhere.** Default timeout 30s; non-2xx raises immediately. | We own throttling and retry. |
| Every `edit*`/`add*`/`remove*` helper **defaults to `locked=True`**, pinning the field against future agent refreshes. | A real, easily-missed side effect. Revert must restore lock state, not just values. |

### Metadata sources

- **TMDB** — v4 Bearer token is the current recommended auth and works on v3 paths. The old 40 req/10s limit was removed in 2019; there is now an unpublished ceiling, so honor HTTP 429 with backoff. `/find/{external_id}` gives unambiguous cross-database lookup and should be preferred over title search whenever an IMDb/TVDB id is already known. `append_to_response=external_ids,alternative_titles` folds sub-resources into one call. Free for non-commercial use **with attribution**.
- **TVDB v4** — `POST /login` returns a JWT valid ~1 month. Free tier for projects under $50k/yr revenue, **with an attribution link**. The endpoint that matters here is `GET /series/{id}/episodes/{season-type}` with season-type ∈ `default|official|dvd|absolute|alternate|regional`. **The docs explicitly warn that an episode need not exist in every season type** — so absolute-vs-seasonal reconciliation must fetch each ordering separately and cross-reference by TVDB's internal episode `id`, never by season/episode numbers. Rate limits are undocumented; TVDB recommends local caching over hammering.
- **Audnexus** — free, no auth, still live, but **the spec's proposed `search_audnexus(title, author, narrator)` tool cannot be built as written: Audnexus has no book-search endpoint.** It offers only `/books/{ASIN}`, `/books/{ASIN}/chapters`, `/authors?name=` (authors only, fuzzy, many false positives), and `/authors/{ASIN}`. Resolving title+author → ASIN is a genuine unsolved step; the usual route is Audible's unofficial catalog API. Mitigation is in the design below. Other gotchas: `seriesPrimary.position` is a **string** and may be non-integer (`"3.5"` for novellas); standalone books omit the series keys entirely; author names are sometimes garbage-split by Audible itself; the public instance returns 429s under bulk load.
- **Open Library** — no auth, but a descriptive `User-Agent` with contact info is **mandatory policy** (1 req/s without, 3 req/s with). **Not usable as a series-order source**: the `series` field is free-text, edition-level, has no numeric position, and a confirmed open bug means it is often not returned even when it matches the query. It *is* useful for author-variant disambiguation via `alternate_names`, though only ~4% of authors have that field populated.

Net: **Audnexus is the authority for audiobook series position and narrators; Open Library is a secondary cross-check for author aliases and bibliographic identity.**

### Stack versions

- **openai 3.3.1** — the **Responses API is the officially recommended surface for new agentic tool loops**. Tool defs are flat (`{"type":"function","name",...,"parameters","strict"}`); tool calls arrive as first-class `function_call` items with `call_id`, not inside `message.tool_calls`; reasoning items persist across turns with `store: true`.
- **anthropic 1.0.0** — Messages tool shape `{name, description, input_schema}`; `stop_reason == "tool_use"`; all `tool_result` blocks must go back in a **single** user message. Prompt caching via `cache_control` with 5m/1h TTLs.
- The two genuinely diverge on parallel-tool-call semantics, reasoning-block replay, and strict/structured-output syntax. The provider interface must carry a **provider-opaque reasoning carryover blob** rather than trying to unify those shapes.
- **Arize Phoenix** for tracing — one container (`arizephoenix/phoenix:latest`, 6006 UI+OTLP/HTTP, 4317 gRPC, embedded SQLite) versus Langfuse's mandatory six services. Both ingest vanilla OTLP, so instrumentation stays backend-agnostic.
- **OTel GenAI semantic conventions are not stable.** All `gen_ai.*` attributes are Development status and were moved out of the main semconv repo in v1.42.0 into a repo with no tagged releases. `gen_ai.system` was **renamed to `gen_ai.provider.name`**. Attribute naming must be centralized in one module and emit both names during the transition.
- **Pydantic 2.13.4** — `model_json_schema()` emits `$defs`/`$ref`, which some provider strict-mode validators reject; there is no built-in flatten flag, so a small recursive `$ref` inliner is needed.
- **Typer 0.27.1** (vendors Click internally as of 0.26.0), **uv** for project management (system Python is 3.11.1, so uv must provision 3.12+), **respx 0.23.1** + **vcrpy 8.3.0**, **pytest-asyncio 1.x** with `asyncio_mode="auto"`, stdlib **sqlite3** with explicit `autocommit=False` and WAL.

Local environment confirmed: `uv 0.10.4`, CPython **3.13.12** already installed (so `.python-version` = 3.13, no download needed), Docker 29.3.0 available. Note that `arize-phoenix` also ships a standalone server (`phoenix serve`), so Phase 2 tracing needs no container at all if preferred.

---

## Design

### 1. Package layout

```
shelfwarden/
  pyproject.toml            # uv project, [project.scripts] shelfwarden = "shelfwarden.cli:app"
  .python-version           # 3.13
  src/shelfwarden/
    cli.py                  # Typer app: scan / diff / apply / revert / eval / export
    config.py               # settings from env + ~/.config/shelfwarden/config.toml
    models/
      item.py               # NormalizedItem + subtypes — the canonical media model
      finding.py            # Finding, Claim, Citation, RepairProposal, ProblemClass
      evidence.py           # EvidenceRecord, evidence_id derivation
    library/
      base.py               # LibraryProvider protocol — READ-ONLY surface only
      plex.py               # PlexLibrary: plexapi -> NormalizedItem
      snapshot.py           # SnapshotLibrary: serves a corrupted/ground-truth dataset
      audiobook.py          # heuristics for "this artist section is audiobooks"
    sources/
      base.py               # MetadataSource protocol, shared HTTP client, throttle, cache
      tmdb.py  tvdb.py  audnexus.py  openlibrary.py
      asin.py               # ASIN resolution ladder (see Risks)
    agent/
      loop.py               # the hand-rolled while-loop
      state.py              # RunState, Step, persistence
      provider/
        base.py             # LLMProvider protocol, ToolSpec, Proposal, Usage
        openai.py           # Responses API
        anthropic.py        # Messages API  (Phase 1b)
      context.py            # assemble_context + truncation policy
      tools/
        registry.py         # ToolRegistry keyed by RunPhase  <-- the read/write seam
        readonly.py         # all Phase 1 tools
        # mutating.py       <-- Phase 3 only; not imported by the read-only registry
      guard/
        base.py             # Guard, Decision  (Phase 1 stub; Phase 2 fills the chain)
        checks/             # schema, existence, budget, ratelimit, loop, business
      budget.py             # step / cost / wall-clock caps
      validate.py           # groundedness validator
    store/
      db.py                 # sqlite3, WAL, autocommit=False, migrations/
      migrations/
    evals/
      export.py             # pull the real slice from Plex -> ground truth snapshot
      corrupt/
        __init__.py         # registry of corruption functions
        movies.py  tv.py  audiobooks.py
      generate.py           # `python -m shelfwarden.evals.generate --count 200`
      truth.py              # TruthFile schema + load/save
      score.py              # the four scoring levels + metric rollups
      report.py             # renders the README metrics table
    telemetry/
      otel.py               # tracer setup; ONE module owning gen_ai.* attribute names
  tests/
  datasets/                 # generated; git-ignored except a small committed fixture set
```

**Phase 2–5 seams, present from day one but empty:** `guard/checks/` (Phase 2 fills the ordered chain), `telemetry/otel.py` (Phase 2 wires spans), `agent/tools/mutating.py` + a `RunPhase` enum that already includes `executing` (Phase 3), `library/` + `sources/` as the clean extraction boundary for the Phase 5 MCP server, and `agent/loop.py` being the *only* module that would be replaced by LangGraph in Phase 4.

**The seams are enforced mechanically, not by convention.** Add `[tool.importlinter]` to `pyproject.toml` in step 0.1 — not later — with three contracts:

1. `agent/tools/` must not import `agent/loop.py`, `agent/provider/`, or `evals/` — this is the Phase 5 MCP extraction boundary.
2. `library/plex.py` is the **only** module permitted to import `plexapi`.
3. `agent/provider/openai.py` and `agent/provider/anthropic.py` are the only modules permitted to import their respective SDKs.

This is the §3.1 principle ("prompts are guidance; code is enforcement") applied to the architecture itself. Contracts 2 and 3 turn two claims this plan makes — "plexapi types never escape `library/plex.py`" and the spec's "do not let a provider SDK's types spread through the codebase" — from stated intentions into CI failures. Adding them on day one costs minutes; adding them in Phase 5 means discovering the seam already leaked.

### 2. The library provider abstraction — the crux

The agent must run identically against the live Plex server and against the corrupted synthetic dataset. The wrong way to do this is to fake `plexapi`. The right way:

**Normalize at the edge.** `PlexLibrary` maps plexapi objects into `NormalizedItem` at the boundary; the snapshot stores `NormalizedItem` records directly. `SnapshotLibrary` therefore has nothing to fake — it serves the same dataclasses from JSON. plexapi types never escape `library/plex.py`.

```python
class LibraryProvider(Protocol):
    def sections(self) -> list[SectionRef]: ...
    def list_items(self, section_id: str, offset: int, limit: int) -> Page[ItemStub]: ...
    def get_item(self, item_id: ItemId) -> NormalizedItem: ...
    def get_children(self, item_id: ItemId, offset: int, limit: int) -> Page[ItemStub]: ...
    def get_files(self, item_id: ItemId) -> list[FilePart]: ...
    def find_similar(self, section_id: str, title: str, limit: int) -> list[ItemStub]: ...
```

Note what is **absent**: there is no `edit`, `merge`, `fixMatch`, or `refresh` on this protocol at all. §3.2's requirement that mutating tools "not exist" outside `executing` is satisfied by the type, not by a runtime check. Phase 3 adds a *separate* `MutableLibraryProvider` protocol that `PlexLibrary` implements and `SnapshotLibrary` implements against its in-memory copy.

**Identity.** `ItemId` is a composite `(provider, section_id, rating_key)` rather than a bare int, so snapshot ids and live ids never collide and a truth file can reference either. External ids are kept as a parsed `guids: list[ExternalId]` with `(namespace, value)` — this normalizes the new-agent `plex://` + `tmdb://` list *and* the legacy `com.plexapp.agents.imdb://tt0468569?lang=en` single-guid form into one shape, which matters because a legacy-agent library is exactly where wrong-match problems concentrate.

**Audiobooks.** `NormalizedItem` carries a `media_kind` discriminator (`movie|show|season|episode|author|audiobook|audiobook_part`) that is *derived*, not taken from Plex. `library/audiobook.py` decides whether an `artist` section is an audiobook library (section agent identifier contains `audnexus`/`audiobooks`, or ≥N% of tracks are `.m4b`/`.m4a`, or album-per-book structure) and maps Artist→author, Album→audiobook, Track→audiobook_part. This mapping is itself a source of bugs, so it gets its own unit tests against committed fixtures.

> **Correction, recorded in step 0.2.** The kind list above originally ran `movie|show|season|episode|audiobook|audiobook_part`, mapping Album→audiobook and Track→audiobook_part but leaving the Artist unmodelled. Two corruption classes in §3 operate on the Artist: `author_name_variant` splits one artist into variants and its truth record holds *"the id set to merge"*, and `narrator_as_author` sets the artist. An id set of artists cannot be recorded if artists have no `ItemId`. An `author` kind was added, making Author→Audiobook→Part structurally identical to Show→Season→Episode. See `plans/step-0.2-normalized-model.md` §2 finding 7.

**Faithfulness to plexapi quirks.** Rather than reimplementing them, we *neutralize* them in `PlexLibrary`:
- Set `autoreload=false` globally and call `reload()` explicitly with the exact includes needed. This makes both cost and the exported field set deterministic.
- `list_items` always passes both `container_start` and `maxresults` and returns a `Page` with an explicit `total` so pagination semantics are identical in both providers.
- plexapi exceptions are translated into our own `LibraryError` taxonomy at the boundary, so `SnapshotLibrary` can raise the same errors for the same conditions.

### 3. The corruption harness (Phase 0 — the most important deliverable)

**Export.** `evals/export.py` pulls a slice into `datasets/exports/<timestamp>/`, holding `items.jsonl` (one canonical-JSON `NormalizedItem` per line), `manifest.json`, `census.json`, and `census.md`. The manifest records section ids and agents, plexapi version, exporter git sha and dirty flag, the `FetchProfile`, the effective request parameters, the selection plan, and a hashed server identity. Determinism comes from: `autoreload=false`, an explicit include set, canonical JSON with sorted keys, an explicit total order on every record *and every aggregation*, and a seeded selection that chooses membership but never ordering. Re-running the export against an unchanged library must produce byte-identical `items.jsonl` and a manifest differing only in `created_at` — that is the export's own test, and it runs both in-process and across two `PYTHONHASHSEED` values, since a same-process comparison cannot see hash-order leakage at all.

> **Correction, recorded in step 0.4.** The paragraph above originally specified a flat `<timestamp>.jsonl` plus a manifest carrying *"server machine id … reload-includes used"*, with *"records sorted by `item_id`"*. Four changes, each forced by something verified while building it. See `plans/step-0.4-export-census.md`.
>
> 1. **A directory, not a file.** There are four artifacts that must travel together and are cross-referenced by hash. Written temp-dir-then-`os.replace`, so an interrupted export leaves nothing rather than something that looks finished.
> 2. **The machine id is hashed, not stored.** It is durable and server-unique — which is what makes it worth recording and what makes recording it verbatim a leak. The hash answers the only question the manifest asks (*are these two exports from the same server?*). `scripts/capture_fixtures.py` already scrubs the raw value from fixtures; storing it here would have undone that.
> 3. **The manifest records the *effective* request parameters, not `RELOAD_INCLUDES`.** Our dict is a set of *overrides*. plexapi's `_buildDetailsKey` starts from all nineteen keys of `PlexPartialObject._INCLUDES`, overlays them, and drops whatever ends up falsy — leaving `includeFields=thumbBlurHash,artBlurHash` in every request we make, which our dict never mentions. Recording the override dict would understate the request while looking authoritative, which is worse than recording nothing. `library.plex.effective_request_params()` computes the honest value with no server.
> 4. **Records are family-grouped, not sorted by `item_id`.** The order is `(section_id, root_key, kind_rank, item_key)`, so a show is followed by its seasons and episodes. Step 0.5 operates on families, and `--count` counts *roots*: a family that would exceed `--max-records` is dropped whole and recorded in `manifest.dropped`, never truncated. Half a show is an unsolvable case for `episode_wrong_season` and a mysteriously depressed score in 0.8.
>
> A fifth change landed in the model rather than in the export: `FilePart` gained `media_id` and `part_id`. A part had no identity at all, and both `filename_unmatchable`'s truth record and Phase 3's rename-revert must name *which* part — `parts[2]` is a positional identifier of exactly the kind invariant 9 rejects. Order is preserved and deliberately not sorted by id: it carries meaning that `multi_file_split` consumes.
>
> Also settled: the export runs at **`FetchProfile.CORE`** by default, with `--profile full` available. FULL differs by `checkFiles=1` alone, which maps to no field this model carries and costs a server-side stat per part. Its only observable output, `Part.accessible`, is *volatile* — it flips when a mount drops — so had we mapped it, byte-identity would have become a function of whether a NAS was awake.

**Corruption functions.** One per problem class, registered by decorator, each returning the mutated item *and* a precise record of what it did:

```python
@corruption(ProblemClass.WRONG_MATCH, applies_to={MediaKind.MOVIE, MediaKind.SHOW})
def wrong_match(item: NormalizedItem, rng: Random, ctx: CorruptionContext) -> CorruptionResult: ...


@dataclass
class CorruptionResult:
    item: NormalizedItem
    changes: list[FieldChange]  # path, before, after — the diffable record
    truth: TruthRecord
    witness: DetectabilityWitness  # proof this case is solvable at all
```

**Every corruption must prove its own detectability, or the case is not emitted.** This is the fix for a whole family of silently-unpassable cases — nulling a summary that was already empty in the source export, applying `foreign_title_variant` to a film TMDB has no alternate title for, swapping a `wrong_match` into a TMDB record too ambiguous to discriminate. Left to scoring time these depress the pass rate and hide real regressions.

```python
@dataclass
class DetectabilityWitness:
    source: str
    evidence_id: str
    pointer: str  # RFC-6901 into the stored body
    resolved_value: JSONValue
    discriminates: bool  # != corrupted value AND == ground truth, under the scorer's comparator
```

The generator runs **the scorer's own comparator** over (witness, corrupted) and (witness, ground truth), requiring inequality then equality. A no-op `FieldChange` where `before == after` raises rather than warns. One cheap property test catches the rest: **`apply_reverse(changes)` on the corrupted item must equal `ground_truth_item` byte-for-byte under the canonical serializer.**

Rejected cases land in `datasets/<id>/rejected.jsonl` with reasons and roll into the per-class deficit report — so "TMDB has no alternate title for 40% of this library" surfaces as a coverage gap rather than a mysteriously low score. The cost is that generation becomes network-dependent; mitigate by content-addressing witnesses into the same evidence store the agent uses and committing them alongside the dataset. Useful side effect: the scorer can then check whether the agent cited *the same record the generator did*.

| Problem class | What the corruption mutates | Truth record holds |
|---|---|---|
| `wrong_match` | Swap `guids` + title/year/summary for those of a different real film from the same export | Correct `guids`; expected repair = re-match to correct TMDB id |
| `year_collision_remake` | Swap year + guid to the *other* version of a real remake pair | Correct year + guid. Distinct from `wrong_match` because the title is right |
| `foreign_title_variant` | Replace title with a real TMDB `alternative_titles` entry; clear `guids` | Canonical title + guid |
| `alternate_cut` | Strip or mismatch the edition marker so two cuts collide | Correct `editionTitle` per part |
| `missing_metadata` | Null out a parameterized subset of summary/artwork/cast/contentRating/rating | Original values, per field |
| `duplicate_quality` | Clone the item as a second entry at different resolution/container/bitrate | The id pair that should merge, and which is the keeper |
| `episode_wrong_season` | Move an episode's `parentIndex` to a different season | Correct (season, episode) |
| `absolute_vs_seasonal` | Renumber a seasonal show with absolute numbering (S01E01..S01E52) | Correct (season, episode) from TVDB `official`, cross-referenced by episode id |
| `filename_unmatchable` | Rewrite `part.file` to a scene-release/garbage name; clear `guids` | Correct title/year + a suggested filename |
| `series_order_broken` | Rewrite album title/sort with inconsistent markers (`Book 3`/`Part 3`/`#3`/none); scramble `index` | Series name + Audnexus `seriesPrimary.position` (**string**, may be `"3.5"`) |
| `author_name_variant` | Split one artist into 2–3 variants (`Brandon Sanderson`, `Sanderson, Brandon`, double-space), reassigning albums | Canonical author + the id set to merge |
| `narrator_as_author` | Set the artist/author to the narrator's name | Correct author; flag that the present value is the narrator |
| `multi_file_split` | Split one album's tracks into N albums (`… CD1`, `… CD2`) | The id set that is one book + correct title |
| `missing_series` | Strip series membership from title/sort/collection | Series name + position |
| `anthology_omnibus` | Mark an omnibus as book N, or split it into constituents | Deliberately **ambiguous** — usually routed to the escalate slice |

**Truth file.** One file per dataset, `datasets/<dataset_id>/truth.json`. A second adversarial review found three defects in the first draft of this schema; the corrected version and the reasoning are below.

```jsonc
{
  "dataset_id": "sw-20260825-a1b2c3", "seed": 1518, "generator_version": "0.1.0",
  "lineage_id": "lin-7f3a91",                 // sha256(composition.toml + slice defs)
  "source_export": "exports/2026-08-25T22-10Z.jsonl",
  "cases": [{
    "case_id": "case-a3f91c2b8e04",           // semantic hash, NOT positional
    "subject_key": {"kind":"external_id","value":"tmdb://155"},
    "corruption_fingerprint": "sha256:…",
    "slice": "synthetic",                     // synthetic | real | should_not_touch | ambiguous
    "run_group": "grp-dupes-shawshank",       // executed together, scored independently
    "item_ids": ["snap:1:41823"],             // regenerable, non-identity
    "media_kind": "movie",
    "problem_class": "wrong_match",
    "expectation": {
      "kind": "repair",
      "required_findings": [{
        "problem_class": "wrong_match", "item_ids": ["snap:1:41823"],
        "postcondition": { "guids": {"contains":["tmdb://155"]},
                           "title": {"normalized_equals":"The Dark Knight"},
                           "year":  {"equals":2008} },
        "repair_op": {"any_of":["rematch","set_field"]},   // advisory, not a gate
        "must_not_change": ["parts[*].file"]
      }],
      "unexpected": "fail",                   // fail | warn | ignore — DEFAULT fail
      "known_other_problems": []
    },
    "corruption": { "changes": [ {"path":"guids","before":[...],"after":[...]} ] },
    "ground_truth_item": { ... }
  }]
}
```

**Defect 1 — the false-positive metric had the wrong denominator.** `forbidden_findings: []` was the default on every `repair` case, so an agent that correctly found the injected `wrong_match` *and* fabricated a `missing_metadata` finding on the same item still passed clean. 85% of the dataset was FP-blind. A reported 2% false-positive rate could really have been 20%. The fix is to make the closed-world assumption explicit and universal via `unexpected: fail` as the default, with `known_other_problems` naming verified pre-existing issues that are neither required nor penalized. Two numbers get reported: `fp_rate_snt` (the spec's headline, scoped to should-not-touch) and **`collateral_fp_rate`** (unexpected findings across *all* cases) — the second is the one that predicts real-world behavior.

**Defect 2 — `escalate` was satisfiable by doing nothing.** The old pass criterion was "no auto-applicable repair, *or* one below threshold flagged `needs_human`" — so an agent that found nothing at all on every ambiguous case scored 100% correct-escalation. Worse, both `confidence` and `needs_human` were model-authored (see §6). The corrected expectation is threshold-free and demands *positive* behavior:

```jsonc
"expectation": {
  "kind": "escalate",
  "require_finding": true,        // silence is not escalation
  "require_needs_human": true,
  "min_candidates": 2,            // enumerate the ambiguity, or name the missing evidence
  "acceptable_resolutions": [
    {"repair_op":"set_series_position","target":{"series":"Mistborn","position":"3.5"}},
    {"repair_op":"split_item","target":{"into":3}}],
  "forbidden_findings": [{"repair_op":"merge_items"}]
}
```

Confidence leaves the pass predicate **entirely** and becomes a reported diagnostic; `auto_apply_rate(t)` is published as a *sweep* rather than a point, so threshold sensitivity is visible instead of tunable. `silence` is reported as an outcome distinct from `escalated`.

> This is the one place the two reviews disagreed. The validator review proposed keying `escalate` on the code-computed `band` from §6; the eval review argued confidence must leave the pass predicate altogether, because code-computing it puts the same constant on both sides — input to behavior *and* boundary for scoring — so a formula change silently moves every escalate score. The second argument wins on the merits. The resolution: **`band` gates auto-apply in the guard** (a safety decision, correctly gated) **but never gates escalate scoring** (a measurement, which must be threshold-free).

**Defect 3 — should-not-touch rested on an unverified assumption.** "This item has no problems" is an open-world claim and cannot be verified. "This item does not have problem P" can be. `evals/screen.py` runs a mechanical, LLM-free screen over the export using **the exact comparators the scorer uses**: resolvable external id present, authority fetched by id, normalized title equality, year equality, season/episode membership, series position, author name, summary non-empty in both, part count == 1, no other item sharing normalized `(title, year)`, filename parsing to the same title+year. All *applicable* checks must pass, and at least three must be applicable.

What was verified is then recorded, and forbid-all is narrowed to it:

```jsonc
"verification": {
  "method": "mechanical", "verified_at": "...",
  "checks": [{"predicate":"title_matches_tmdb","evidence_id":"sha256:…","result":"pass"}],
  "guarded_classes": ["wrong_match","year_collision_remake","foreign_title_variant","missing_metadata"],
  "unguarded_classes": ["alternate_cut","duplicate_quality"]
}
```

A finding in a `guarded_class` is a false positive. A finding in an `unguarded_class` is scored **`unverified`** — counted and reported, never pass or fail. That is what stops the project from training itself to suppress true detections. Human labeling cost is zero: items failing the screen are discarded from the slice and become candidates for the curated real slice instead. Any finding produced on a should-not-touch item during a real run enters an adjudication queue; if the agent turns out to be right, the case is demoted and marked `"method":"human_refuted"`. The dataset self-heals, and **refutation rate becomes a published dataset-quality metric.**

**Postconditions, not operations.** `required_findings` specifies end-state rather than the operation used to reach it, because different repair paths can produce identical correct results. `must_not_change` is what stops "rewrite the title and let Plex rescan" from scoring equal to a clean rematch when it is actually destructive. The sequencing catch: evaluating arbitrary postconditions needs a repair simulator, which is Phase 3 machinery, but step 0.8 is the Phase 0 gate. Resolution — generate postconditions mechanically from the inverse of `corruption.changes` (already recorded) and have the Phase 0 scorer evaluate them by literal field comparison against `ground_truth_item`. That covers roughly 12 of the 15 classes with no simulator at all. Postconditions split into hard (fields the repair directly sets, gated) and `soft_postcondition` (downstream of Plex's own agent, reported not gated).

**Case identity must survive regeneration**, or the spec's relative CI gate ("no case that passed may now fail") is decorative. Positional `case-0001` ids against a live, re-exported library reset the baseline constantly:

```
case_id = "case-" + sha256(canonical_json({slice, problem_class, media_kind,
                                           subject_key, corruption_variant}))[:12]
```

`subject_key` must not be the Plex `rating_key` — rating keys move on rescan. Use a ladder: ground-truth external id (`tmdb://155`, `tvdb://81189/s2e5`, `asin://B002V0QCEC`) → hash of `(section_kind, normalized_title, year)` → hash of path relative to the section root. Three consequences to accept explicitly:
- **`generator_version` is excluded from the id** — otherwise every version bump nukes the baseline. `corruption_fingerprint` carries that signal separately.
- The CI diff has four buckets, not two: `regressed` (gate fails), `fixed`, `new` (id absent — no gate), `changed` (id present, fingerprint differs — warn, needs review).
- **Baselines key on `case_id` per `lineage_id`, not per `dataset_id`.** The original `dataset_id = f(seed, export hash, generator_version)` guaranteed a fresh id on every re-export of a live library — i.e. a guaranteed baseline reset.

**One case = one atomic repair.** The partial-credit dilemma for `duplicate_quality`, `author_name_variant`, and `multi_file_split` ("finding 4 of 5 groups scores the same as 0") is an artifact of stuffing unrelated groups into one case. One duplicate pair, one book's split files, one author's variant set = one case each. After that split every remaining set-valued target is genuinely atomic — merging 2 of 3 author variants leaves the library broken — so binary pass/fail is correct, and the CI gate keeps the boolean it needs. `run_group` preserves the need for the agent to see related items in a single run without making them a single score. Per-case `finding_precision`/`finding_recall` are emitted as diagnostics that never aggregate into pass rate.

**Determinism and slice composition.** `--count 200 --seed N` is fully reproducible: seeded RNG, dataset id derived from `(seed, export hash, generator version)`. Crucially, the generator can only synthesize the **synthetic (50%)** and **should-not-touch (15%)** slices — those derive mechanically from the export. The **real (25%)** and **ambiguous (10%)** slices are human-curated files (`datasets/curated/real.yaml`, `ambiguous.yaml`) that `generate` merges in. When the export lacks enough items for a problem class, the generator **reports the deficit per class rather than over-sampling a handful of items** — a silently unbalanced dataset would read as coverage that does not exist.

Composition itself is a hand-tuned knob, not a constant, so it lives in a checked-in **`composition.toml` at the repo root** rather than buried in `evals/` — the census from step 0.4 will almost certainly force revisions to it. Nested tables, with shares normalized at load rather than required to sum exactly:

```toml
[media.movie]
share = 0.40
[media.movie.classes]
wrong_match = 0.20
year_collision_remake = 0.15
# ...
```

The loader writes the **resolved absolute per-cell targets** into the dataset's `dataset.json`, so a dataset stays interpretable years later without the `composition.toml` that produced it.

**Scorer** (`evals/score.py`) implements the spec's four levels against a run's recorded state:
- *Component* — did the tool set used overlap the expected set for this problem class (e.g. an audiobook series case must consult Audnexus)? Set overlap, not exact sequence. `repair_op` correctness is scored here, advisory only.
- *Trajectory* — consulted ≥1 external source before proposing; stayed within step budget; no tool outside the phase's allowed set. Derived from `RunState`, never from the model. Note that "no repeated identical `(tool, normalized_args)`" is deliberately **not** scored here: Phase 2's guard *blocks* repeats, which would pin the metric at 100% forever. Score `denial_count` grouped by reason instead — that stays informative after the guard lands.
- *Outcome* — proposed repairs evaluated against `postcondition` and `must_not_change`. A case passes or fails as a unit; the report shows which predicate broke.
- *Citation integrity and referent binding* — reported as two separate numbers (see §6). Collapsing them into a single "groundedness" figure is what let the first draft's empty check masquerade as the strictest one.

Metrics emitted as JSON plus a markdown table for the README, each sliced by media kind and problem class: pass rate, **`fp_rate_snt`** (should-not-touch), **`collateral_fp_rate`** (unexpected findings across all cases), correct-escalation rate, `silence` rate, `unverified` rate, validator false-rejection rate, refutation rate, median/p95 steps, median/p95 cost, cost per 100, and the `auto_apply_rate(t)` sweep. Pass rate is additionally broken down by `provenance.method` so a regression concentrated in single-labeled cases is visibly suspect.

**Labeling the real 25% slice** reuses the screen rather than hand-authored YAML. Items that *fail* a screen predicate become pre-populated candidates carrying the failing predicate, the retrieved evidence, and a proposed ground truth — so the human work is adjudicating a filled form, targeting ~60s per case. Double-label a 20% audit sample only and publish inter-rater agreement once as a dataset-quality figure. **No `label_confidence` field feeds the scorer** — a float the scorer weights by has exactly the gaming property that sank the confidence threshold. Uncertainty is expressed by slice reassignment instead: unsure of the answer means it *is* an ambiguous case, so move it; unsure a problem exists at all means discard. Provenance is recorded (`labeled_by`, `method`, `screen_predicate`, `evidence_ids`, `second_labeler`, `agreement`, `notes`) and affects reporting only.

### 4. The Phase 1 agent loop

**Unit of work: one run = one item** (or one small item group for duplicate/merge cases). A `scan` over a section is an outer loop of deterministic code spawning one run per item — that is not the model's job. This bounds context, makes eval cases 1:1 with runs, makes replay simple, and makes the spec's required "steps per item" and "cost per 100 items" metrics fall out naturally. `state.done` means this item has been diagnosed.

```python
@dataclass
class RunState:
    run_id: str
    case_id: str | None
    target: ItemId | list[ItemId]
    phase: RunPhase  # scanning|diagnosing|planning|awaiting_approval|executing|done
    steps: list[Step]
    findings: list[Finding]
    evidence: EvidenceStore
    usage: Usage
    done: bool
    carryover: ProviderCarryover | None  # provider-opaque reasoning blob
```

Each `Step` records the assembled context *by reference*, **the raw model response verbatim before parsing**, the parsed proposal, the guard decision, the tool result by reference, tokens, cost, latency.

SQLite tables: `runs`, `steps`, `blobs` (content-addressed — contexts, raw responses, tool results), `evidence`, `findings`, `claims`. **Storing raw model responses verbatim from day one is the single highest-leverage Phase-1 decision**: it makes Phase 2's deterministic replay a configuration change (swap in a `ReplayProvider` that returns recorded responses by step index) rather than a rewrite.

**Provider interface:**

```python
class LLMProvider(Protocol):
    def propose(
        self, ctx: Context, tools: list[ToolSpec], carryover: ProviderCarryover | None
    ) -> Proposal: ...


@dataclass
class Proposal:
    text: str | None
    tool_calls: list[ToolCall]  # id, name, arguments: dict
    is_final: bool
    stop_reason: str
    usage: Usage
    carryover: ProviderCarryover | None  # opaque; never inspected outside the provider
    raw: bytes  # verbatim, for replay and tracing
```

The opaque `carryover` is the honest answer to the OpenAI-reasoning-items vs Anthropic-thinking-blocks divergence: the loop stores and returns it without ever interpreting it. Trying to unify those two shapes is the mistake the research explicitly warns against.

**Guard.** Phase 1 ships the real signature `evaluate(proposal, state) -> Decision` with two real checks — JSON-schema validation of tool arguments, and the phase/registry check — implemented as the first entries in an ordered list. Phase 2 appends the remaining checks (existence, budget, rate limit, loop detection, business rules) to that same list. No refactor, because the interface is right from the start.

**Context assembly and truncation.** System prompt + task + the target's normalized record + a rolling window of steps. Older tool results collapse to `{tool, args_summary, result_summary, evidence_ids}` — **the evidence ids survive truncation**, so citations stay valid after the full payload leaves context. This is what keeps groundedness checkable on long runs.

### 5. Phase 1 tool schemas

Applying §9's "many small tools, but consolidate near-identical ones behind an enum":

| Tool | Arguments | Returns (minimum useful payload) |
|---|---|---|
| `list_library_items` | `section_id, offset, limit` | `{items:[{item_id,title,year,media_kind}], total, offset, returned}` |
| `get_item_details` | `item_id, include[]` | normalized record; minimal by default, `include` enum gates cast/artwork/children |
| `get_file_info` | `item_id` | `{parts:[{path,container,resolution,size_mb}], count}` |
| `find_similar_items` | `section_id, title, limit` | stubs + similarity score |
| **`search_metadata`** | `source` ∈ `tmdb\|tvdb\|openlibrary`, `media_type`, `title`, `year?`, `author?`, `external_id?` | `{results:[{...,evidence_id}], total, returned}` |
| **`get_tvdb_episode_order`** | `series_tvdb_id`, `order` ∈ `default\|official\|absolute\|dvd` | episodes keyed by **TVDB episode id** so orderings can be cross-referenced |
| **`lookup_audiobook`** | `asin?`, `title?`, `author?` | Audnexus book record + `evidence_id` |
| `record_finding` | see §6 | validation result; the only "write", and it writes to the plan |

Three deliberate departures from the spec's tool list, each forced by verified API reality:

1. `search_tmdb` / `search_tvdb` / `search_openlibrary` collapse into **`search_metadata`** with a `source` enum — exactly what §9 asks for.
2. **`search_audnexus(title, author, narrator)` cannot be built as specified** — Audnexus has no book-search endpoint. It becomes `lookup_audiobook`, which encapsulates the resolution ladder *in code*: use `asin` if given → else extract the ASIN already present in the Plex guid (audiobook libraries using `Audnexus.bundle` usually carry it) → else `/authors?name=` plus local catalog narrowing → else return a **correctable** error steering the model to Open Library. The model is never asked to know that Audnexus lacks search.
3. `get_tvdb_episode_order` is split out rather than folded into `search_metadata` because its return shape is genuinely different, and because absolute-vs-seasonal reconciliation is a headline problem class that deserves a first-class tool.

**Error taxonomy** (§9: "tool error messages are prompts"):
- **Retryable** — 429, 5xx, timeouts. Handled in `sources/base.py` with backoff and per-source throttles (Open Library 3 req/s *with* the mandatory contact `User-Agent`; Audnexus conservatively under 100/min; TMDB backoff-on-429; TVDB cached aggressively). **Never surfaced to the model.**
- **Correctable** — surfaced with what to do instead: *"No Audnexus record for that ASIN. Audnexus is ASIN-keyed and has no title search. Try `lookup_audiobook` with title+author, or `search_metadata(source='openlibrary')` for bibliographic identity."*
- **Terminal** — surfaced and states retrying will not help: *"Item 41823 is in a photo section; ShelfWarden does not diagnose photos. Do not retry; record no finding for this item."*

### 6. The finding model and the groundedness validator

This is the spec's hardest correctness requirement (§3.6): every claim of the form "this is item X" must cite the external record supporting it, and a validator must reject uncited findings **mechanically**, before the user sees them.

> An adversarial review of the first draft of this section found three defects serious enough to invalidate it. They are recorded here because each one is a trap worth not re-entering.
>
> 1. **The checks validated transcription, not reference.** Provenance + pointer resolution + value comparison confirm the model *copied a value correctly from a record it actually fetched*. Nothing bound that record to the library item. A `wrong_match` finding proposing `rematch → tmdb:155` passes all three checks without anything ever examining whether TMDB 155 is the right film for this file — which is precisely the claim §3.6 exists to police. Groundedness would have reported ~99% on day one while wrong matches sailed through, and it would have looked like the strictest check passing rather than the emptiest.
> 2. **`min_length=1` on unsatisfiable claims produces citation laundering, not refusal.** Six of the fifteen problem classes rest on internally-derived claims (`duplicate_quality`, `author_name_variant`, `multi_file_split`, `narrator_as_author`, `episode_wrong_season`, `missing_series`). Facing a schema wall it cannot satisfy, a model attaches the nearest evidence id with a pointer that happens to resolve. The constraint would have manufactured exactly the behavior it was meant to prevent — across the audiobook slice, the one the spec calls most interesting.
> 3. **The `escalate` expectation was unfalsifiable.** It passed when `confidence < threshold` *and* `needs_human == True` — both model-authored fields. A model passes the entire ambiguous slice by emitting `0.3, needs_human=True` on anything it finds hard, and "what it finds hard" is the thing under test. That metric measured willingness to self-report doubt, not ability to detect ambiguity.

**Evidence, including library reads.** Every retrieval records an `EvidenceRecord`. Crucially `Source` includes `LIBRARY` — a library read is evidence too, with `endpoint` = the provider method, `normalized_params` = its arguments, `body` = the `NormalizedItem` JSON. That single change makes internally-derived claims citable without inventing a second citation type.

Two hashes, not one:
- `evidence_id = sha256(source|endpoint|params|body)` — immutable, content-addressed, what citations reference.
- `query_id = sha256(source|endpoint|params)` — the cache key and the cross-run join.

`query_id` enables a check the first draft missed entirely: **before executing an approved plan, re-fetch by `query_id`; if any `evidence_id` backing an auto-band repair has changed, demote it to review.** Without it, approved plans execute against data that no longer exists. Strip API keys and auth headers from `normalized_params` before hashing *and* before storing, or the evidence store becomes a credential leak.

Each adapter also emits a **`field_index: dict[FieldPath, list[Pointer]]`** at retrieval time. The adapters already parse responses to build minimal tool payloads, so this is nearly free, and it makes cross-source contamination structurally impossible: citing TMDB's `id` to support a claim about `guids.tvdb` fails because that pointer is not in the index for that field. This is better than a hand-maintained pointer/field compatibility table, which would rot — the mapping lives beside the parsing code that has to exist anyway, and every vcrpy cassette can assert the index is non-empty for each field the source advertises.

**A discriminated claim union**, so identity claims stay external-only while derived claims become *more* constrained, not less:

```python
class ExternalClaim(BaseModel):
    kind: Literal["external"]
    field: FieldPath
    asserted_value: JSONValue
    citations: list[Citation] = Field(min_length=1)


class ObservationClaim(BaseModel):  # library state
    kind: Literal["observation"]
    field: FieldPath
    asserted_value: JSONValue
    citations: list[Citation] = Field(min_length=1)  # must be Source.LIBRARY


class DerivedClaim(BaseModel):
    kind: Literal["derived"]
    rule_id: RuleId
    inputs: list[ClaimRef] = Field(min_length=1)
    # deliberately no asserted_value — the validator recomputes it


Claim = Annotated[ExternalClaim | ObservationClaim | DerivedClaim, Field(discriminator="kind")]
```

`DerivedClaim` carries no asserted value at all. `rule_id` names a code predicate (`SAME_WORK_DIFFERENT_QUALITY`, `AUTHOR_NAME_VARIANT`, `PARTS_OF_ONE_BOOK`), and **the validator re-executes that rule over the cited inputs and rejects on disagreement.** The model chooses the rule and its inputs; code decides the truth. That is §3.1 verbatim, and it is stronger than the original requirement — a derived claim structurally *cannot* carry an identity assertion, and the required-claims table still gates every `rematch` on an `ExternalClaim` over `guids.*`.

**The four checks.** The first three are renamed to what they honestly are:

*Citation integrity* — (1) **provenance**: the `evidence_id` exists in this run's store; (2) **resolution**: the pointer resolves, and is present in that record's `field_index` for the claimed field; (3) **support**: a per-field comparator returns a `SupportStrength`, never a bool.

*Groundedness proper* — (4) **referent binding**, computed in code and never asserted by the model:

```python
@dataclass(frozen=True)
class SubjectMatch:
    evidence_id: str
    title_strength: SupportStrength      # EXACT|ALIAS|NORMALIZED|FUZZY(score)|NONE
    year_delta: int | None
    runtime_delta_s: int | None          # movies; duration for audiobook_part
    id_overlap: frozenset[Namespace]     # guids shared by item and record
    verdict: Literal["bound", "weak", "unbound"]

def bind(item: NormalizedItem, record: EvidenceRecord) -> SubjectMatch
```

`Finding.subject_bindings` is populated by the validator from the item and the cited records. `unbound` on the record backing an identity claim is a rejection. These two numbers — citation integrity and referent binding — are reported **separately** in the metrics table; collapsing them into one "groundedness" figure is what hid the problem in the first place.

**Absence claims** get a probe that code executes, not a pointer:

```python
class AbsenceCitation(BaseModel):
    evidence_id: str
    container: str  # pointer that MUST resolve — proves we looked in the right place
    key: str | None
    probe: Literal["key_absent", "empty_collection", "not_in_result_set"]
```

The load-bearing half is an authority table keyed by `(Source, Endpoint, FieldPath)` carrying `can_prove_absence`, `absence_semantics ∈ {404_means_absent, 404_means_unknown}`, and a `support_cap`. The verified research fills it directly: Audnexus `/books/{ASIN}` × `seriesPrimary` can prove absence (standalone books genuinely omit the keys); **Open Library × `series` cannot** — the confirmed "often not returned even when it matches" bug means an absence claim there is rejected outright, not merely downgraded; TVDB absence is a 200 with an empty list, never a 404, so a 404 there means *unknown*. Encoding that distinction in a typed field is what stops "TVDB has no absolute ordering" from being asserted off a transport error.

**Confidence is computed, not reported.** The model's number is retained as `model_confidence`, recorded, and gates nothing.

```python
@dataclass(frozen=True)
class Confidence:
    value: float
    band: Literal["auto", "review", "human"]
    reasons: list[str]                   # rendered in the approval diff

def compute(bindings, corroborating, contradicting, support,
            problem_class, library_signals) -> Confidence
```

**The auto-apply guard reads `band`, never `value`.** `band == "auto"` requires a conjunction of predicates rather than a threshold: ≥2 independent sources corroborating, or one authoritative source with non-empty `id_overlap`; no contradicting source; the item not edited by the user since the last scan; and the problem class in `AUTO_APPLICABLE` (duplicates never — §3.3 already says so). `value` exists only to rank the review queue. The `escalate` expectation keys on computed `band`, which is what makes the ambiguous slice falsifiable. `model_confidence` then becomes a free **calibration** metric: its distribution conditioned on `band` and on outcome correctness.

**False rejection is a tracked cost.** Comparators return `SupportStrength`; only `NONE` rejects. Title comparison consults alias sets before falling back to fuzzy — TMDB `alternative_titles` and `original_title`, TVDB `aliases`, Open Library `alternate_names` — and an alias hit is `ALIAS`, a correct match rather than a rejection. This promotes `append_to_response=alternative_titles` from an optimization to a requirement. Identifiers dominate titles: never reject on a title comparator alone when an `ExternalClaim` on `guids.*` in the same finding is `EXACT`. This matters most for `foreign_title_variant`, a class whose entire premise is that the local title differs from the canonical one — strict equality would reject exactly the findings that class exists to produce.

Validator false-rejection rate is a headline metric alongside the spec's false-positive rate, defined on the eval set as `|rejected findings ∩ findings matching required_findings|`. **Anti-gaming:** report it paired with should-not-touch FP rate and gate CI on the pair, since loosening comparators moves both in opposite directions. Hold out a comparator-tuning split from the curated real slice, and never tune against should-not-touch. Recorded rejections carry a `rejection_reason` enum so the metric decomposes per check.

**Rejections are surfaced to the model as correctable errors**, per §9's own taxonomy — otherwise the model is punished without learning why. Two consequences: rejection retries escape `(tool, normalized_args)` loop detection, because re-emitting `record_finding` with a different pointer hashes differently, so rejections are capped explicitly at 3 per finding before turning terminal. And because truncation preserves evidence ids while discarding bodies, the model would otherwise hold citable ids it can no longer read and start guessing pointers — so `result_summary` retains the `field_index` *keys*, and a read-only **`get_evidence(evidence_id, pointer)`** tool reads the local store, making re-grounding cost no API call and no rate limit.

### 7. Build steps and gates

**Phase 0 — the corruption harness.** Gate: `python -m shelfwarden.evals.generate --count 200` produces a labeled dataset, and a trivial scorer can compare a proposed repair set against the truth file.

| # | Step | Files | Done when |
|---|---|---|---|
| 0.1 | Scaffold: `uv init`, 3.13, Typer entry point, sqlite store with WAL + `autocommit=False`, migrations, **`[tool.importlinter]` contracts** | `pyproject.toml`, `cli.py`, `store/db.py` | `uv run shelfwarden --help` works; a migration applies; `lint-imports` passes in CI |
| 0.2 | Normalized model | `models/item.py` | Round-trips to canonical JSON; unit tests on `ExternalId` parsing for **both** new-agent `guids` and legacy `com.plexapp.agents.*` guid forms |
| 0.3 | `PlexLibrary` read-only + audiobook detection | `library/base.py`, `library/plex.py`, `library/audiobook.py` | Connects; `autoreload=false`; paging passes both `container_start` and `maxresults`; **a test asserts the protocol exposes no mutating method** |
| 0.4 | Export + census | `config.py`, `evals/export.py`, `evals/census.py`, `cli.py:export` | `shelfwarden export --count 200` writes a deterministic `items.jsonl` + manifest + two-tier census; re-run is byte-identical, including across two `PYTHONHASHSEED` values |
| 0.45 | Comparators + mechanical screen | `evals/compare.py`, `evals/screen.py` | The comparator library the scorer, screen, and detectability witness all share; screen classifies export items into guarded/unguarded classes |
| 0.5 | Corruption functions + detectability | `evals/corrupt/*.py` | All 15 classes implemented; each emits a `DetectabilityWitness`; no-op `FieldChange` raises; the `apply_reverse(changes) == ground_truth_item` invariant holds byte-for-byte |
| 0.6 | Truth schema + generator + composition config | `evals/truth.py`, `evals/generate.py`, `composition.toml` | `generate --count 200 --seed N` is reproducible; semantic `case_id`s stable across regeneration; per-cell targets and rejected cases both written out |
| 0.7 | `SnapshotLibrary` | `library/snapshot.py` | Serves the corrupted dataset through the identical `LibraryProvider` protocol; same error taxonomy |
| 0.8 | Scorer | `evals/score.py`, `evals/report.py` | Scores a hand-written proposed repair set against the truth file via postcondition comparison and emits the metric table — **this is the gate** |

**Phase 1 — read-only agent, no framework.** Gate: 20+ eval cases run end to end and produce a scored report. The score may be bad; it must exist.

| # | Step | Files | Done when |
|---|---|---|---|
| 1.1 | Metadata sources + throttle/cache/retry | `sources/*.py` | Each source has vcrpy cassettes captured once from the real API; respx unit tests for error paths; Open Library sends the mandatory contact `User-Agent` |
| 1.2 | Provider interface + OpenAI Responses | `agent/provider/base.py`, `openai.py` | Returns a `Proposal` with verbatim `raw`; `$ref` inliner applied to Pydantic-generated tool schemas |
| 1.3 | Tool registry + read-only tools | `agent/tools/registry.py`, `readonly.py` | A test asserts `tools_for(phase) ∩ MUTATING_TOOLS == ∅` for every non-`executing` phase |
| 1.4 | Findings + validator (4 checks) | `models/finding.py`, `models/evidence.py`, `agent/validate.py` | Fabricated `evidence_id`, unresolvable pointer, wrong-field pointer, non-supporting value, and an **unbound referent** are each rejected by a test; `DerivedClaim` rules re-execute; absence authority table enforced; confidence computed in code |
| 1.5 | RunState + persistence | `agent/state.py`, `store/` | A run persists and reloads losslessly, raw responses included |
| 1.6 | The loop + budget + guard stub | `agent/loop.py`, `budget.py`, `guard/base.py` | Runs to completion against `SnapshotLibrary` within step/cost caps |
| 1.7 | Eval runner + report | `evals/` + `cli.py:eval` | `shelfwarden eval --dataset <id>` runs 20+ cases and emits the scored report — **this is the gate** |
| 1.8 | Anthropic provider | `agent/provider/anthropic.py` | Same suite runs against both providers; results diffed per case |

### 8. Risks and open questions

- **ASIN resolution is the real audiobook risk.** Audnexus has no book search, and the usual workaround (Audible's unofficial catalog API) is undocumented and fragile. The ladder in `sources/asin.py` degrades explicitly: guid-embedded ASIN → author search + local narrowing → correctable error. **Do not add the unofficial Audible dependency in Phase 1**; measure how far the guid route gets first, since libraries using `Audnexus.bundle` likely already carry the ASIN. If coverage is poor, that is a measured finding worth acting on, which is exactly the posture the spec asks for.
- **The real library's problem distribution is unknown.** The 200-item slice may contain very few shows with absolute numbering, or no remake pairs. Step 0.4 therefore emits a **census** of the export *before* the corruption step, so the slice composition is chosen from evidence rather than assumed. Expect to revise target counts after seeing the census. Two cautions carried forward from building it. The census's `readiness` table counts *structural candidates* and is flagged `advisory: true` on every row — it does **not** verify that any item is free of a problem, and a readiness count read as a `no_action` label would make the should-not-touch slice unfalsifiable, which is Defect 3 above arriving one step early. And the guid tier is **slice-scoped**: if it reports zero `UNKNOWN` namespaces that is weak evidence, not proof, because 0.2's legacy parsers stay unvalidated until a legacy section is exported specifically with `--section`. Check `--census-only` output for a section whose agent starts with `com.plexapp.agents.` before concluding anything.
- **Corrupting a snapshot instead of a live server is a real fidelity risk** and worth stating plainly. It is mitigated three ways: the normalized model is derived from real exports so field shapes are genuine; the 25% real-problems slice is labeled from the live library and never synthesized; and `PlexLibrary` and `SnapshotLibrary` are exercised through one protocol with one error taxonomy. It is not fully eliminated — a corruption generator can only produce failure modes we thought of, which is precisely why the real slice is 25% and not 0%.
- **TVDB season types do not align.** Episodes need not exist in every ordering, so absolute↔seasonal reconciliation must join on TVDB episode ids. A naive number-based join will silently produce wrong repairs on exactly the class the spec calls out as interesting.
- **`locked=True` is plexapi's default on every edit helper.** It pins fields against future agent refreshes. Phase 3's revert must restore lock state, not just values. Lock state is recorded in the export as of step 0.4, and a test asserts it survives the round trip. Note that `includeFields` is *not* what governs this: despite the name it is a blur-hash selector, and `<Field>` lock elements arrive from the metadata endpoint regardless. The per-item fetch in the export exists for lock state, not for guids — listings already carry `<Guid>` children.
- **OTel GenAI semconv is mid-rename** (`gen_ai.system` → `gen_ai.provider.name`, no pinnable schema URL). Centralize attribute names in `telemetry/otel.py` and emit both during the transition.
- **Open question for after the census:** whether the audiobook slice is large enough to support all six audiobook problem classes at meaningful counts, or whether some classes should be represented only in the curated real slice.

---

## Verification

**Phase 0 gate**
```bash
uv run shelfwarden export --count 200 --out datasets/exports/     # deterministic; re-run diffs clean
uv run python -m shelfwarden.evals.generate --count 200 --seed 1518
uv run pytest tests/evals -q          # every corruption function asserts its own mutation + truth
uv run shelfwarden eval score --dataset <id> --proposals tests/fixtures/handwritten_repairs.json
```
Passing means: a labeled dataset exists, slice deficits are reported rather than hidden, and the scorer produces the metric table from a hand-written repair set with no agent involved.

**Phase 1 gate**
```bash
uv run pytest -q                       # includes the read-only registry assertion and validator rejection tests
uv run shelfwarden eval --dataset <id> --provider openai --limit 20
uv run shelfwarden eval --dataset <id> --provider openai --replay <run_ids>   # same result, zero model calls
```
Passing means: 20+ cases run end to end, a scored report exists with all the spec's metrics sliced by media kind and problem class, and replay reproduces it from recorded responses.

**Sanity check against the live library** (read-only, no mutation exists in Phase 1):
```bash
uv run shelfwarden scan --section <movies> --limit 5 --dry-run
```

**Per §9**, any change made to fix an observed failure must come with an eval case that fails before and passes after.
