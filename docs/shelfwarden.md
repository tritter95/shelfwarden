# ShelfWarden

An agentic media library steward. It audits a Plex library, diagnoses metadata and organization problems, proposes repairs, and — only after approval — applies them.

---

## 1. Purpose

**This is a learning project with a real use case, not a product.** That ordering matters and should shape every decision.

The primary goal is to build a production-grade agentic system end to end: eval harness, guard layer, durable execution, tracing, staged autonomy. The secondary goal is a genuinely useful tool for maintaining a personal Plex library.

When the two conflict, the learning goal wins. Specifically:

- Do **not** reach for a framework to skip a concept. The first version has no agent framework at all.
- Do **not** defer the evaluation harness. It is the first deliverable, before the agent works.
- Do **not** optimize for library coverage. A tool that handles 70% of problems with measurable reliability is the goal; 95% with no measurement is a failure.

### Why this domain was chosen

Media metadata has **cheap, automatic ground truth** (TMDB, TVDB, Audnexus, Open Library). That makes it one of the few personal-project domains where an agent can verify its own work — the same property that makes coding agents viable. It also means a perfectly-labeled eval dataset can be generated synthetically (see §6).

---

## 2. What the tool actually does

### Problems it diagnoses

**Movies / TV**
- Wrong match (item matched to the wrong film or show)
- Year collisions and remakes matched to the original
- Foreign-title variants and alternate-cut confusion
- Missing or incorrect metadata (artwork, summary, cast, ratings)
- Duplicate items at different qualities/encodes
- Episodes matched to the wrong season, or absolute-vs-seasonal numbering mismatches
- Files whose names prevent correct matching

**Audiobooks** — the hardest slice, and the most interesting
- Series ordering broken (`Book 3` vs `Part 3` vs `#3` vs no marker)
- Author name variants splitting one author into several
- Narrator recorded as author
- Multi-file books scanned as separate items
- Missing series membership entirely
- Anthologies and omnibus editions

### What it does about them

For each detected problem, produce a **finding**: the item, the problem class, the evidence (which external source says what), the proposed repair, and a confidence level. Findings are grouped into a **repair plan** the user reviews. Only approved repairs execute.

---

## 3. Non-negotiable architecture

These are decisions already made. Do not relitigate them during implementation.

### 3.1 The model proposes, code disposes

Every action passes through a deterministic guard layer before execution. Prompts are guidance; code is enforcement. Any rule expressible as a predicate lives in code, not in the system prompt.

### 3.2 Read/write separation by phase

The scan and diagnose phases are **strictly read-only**. No tool that mutates anything is registered or reachable during those phases. All mutations are batched into a single execution stage that runs after approval.

This is enforced structurally (a state machine gates which tools exist per phase), not by instruction.

### 3.3 Every mutation is reversible

Before any repair, snapshot the prior state of the item to a local store. Every repair has a corresponding `revert` operation. `shelfwarden revert <plan-id>` must restore the library to its pre-plan condition.

Compensation failures alert loudly and never fail silently.

### 3.4 Dry run is the default

`apply` without an explicit `--commit` flag simulates. The simulation must be realistic enough that the resulting plan is identical to what a real run would produce.

### 3.5 Outcomes derive from recorded state, never from the model

The final report is a deterministic rendering of what the code recorded — which repairs were attempted, which succeeded, which failed. The model's narrative is presentation only. Never treat the model's self-report as the record of what happened.

### 3.6 Structured findings, mechanically validated

Findings are structured objects, not prose. Every claim of the form "this is item X" must cite the external source record that supports it. A finding with an uncited claim is rejected by a validator before it reaches the user.

---

## 4. Stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12+ | Marketability; ecosystem |
| Agent loop | **Hand-rolled first.** LangGraph in phase 4 only | See build order |
| Models | Anthropic + OpenAI APIs directly | Behind a thin interface; no cloud-vendor wrapper |
| Plex access | `python-plexapi` | Verify current API surface before designing tools |
| Metadata sources | TMDB, TVDB, Audnexus, Open Library | Verify auth requirements and rate limits per source |
| State / audit | SQLite | Run state, snapshots, audit log |
| Tracing | OpenTelemetry → Langfuse or Phoenix | Self-hosted; both are free |
| Evals | Custom harness (see §6) | Framework-independent; invokes the agent through an interface we own |
| Durability | Local checkpointing first, **Temporal** in phase 5 | Only when full-library scans justify it |
| CLI | Typer or Click | `shelfwarden scan / diff / apply / revert / eval` |

**Keep model access behind a thin interface.** The eval suite must be runnable against multiple models per segment. Do not let a provider SDK's types spread through the codebase.

---

## 5. Build order

Each phase has a gate. Do not begin a phase until the previous gate is met.

### Phase 0 — The corruption harness ← **START HERE**

Before any agent code exists.

1. Connect to Plex read-only. Export a slice of the library (target: 200 items across movies, TV, and audiobooks) with full metadata.
2. Snapshot that slice as ground truth.
3. Write a corruption generator that breaks items in **specified, labeled ways** — one function per problem class from §2, each recording exactly what it did.
4. Produce a corrupted dataset plus a truth file mapping each corrupted item to its correct state and the problem class introduced.

**Gate:** `python -m shelfwarden.evals.generate --count 200` produces a labeled dataset, and a trivial scorer can compare any proposed repair set against the truth file.

This is the single most important deliverable in the project. Everything downstream is measured against it.

### Phase 1 — Read-only agent, no framework

Hand-write the loop. Roughly:

```python
while not state.done:
    budget.check()
    context = assemble_context(state)
    proposal = model.propose(context)
    if proposal.is_final: break
    decision = guard.evaluate(proposal, state)
    if decision.denied:
        state.record_denial(decision); continue
    result = execute(proposal.tool_call)
    state.record(proposal, result)
    store.save(state)
```

Tools (all read-only):
- `list_library_items(section, offset, limit)`
- `get_item_details(rating_key)`
- `search_tmdb(title, year, media_type)`
- `search_tvdb(title, year)`
- `search_audnexus(title, author, narrator)`
- `search_openlibrary(title, author)`
- `get_file_info(rating_key)` — filename, path, container, resolution
- `find_similar_items(title, section)` — duplicate detection support
- `record_finding(...)` — the only "write", and it writes to the plan, not to Plex

**Gate:** 20+ eval cases run end to end and produce a scored report. The score can be bad. It must exist.

### Phase 2 — Guard layer, budgets, observability

- Guard checks in order: schema → existence → budget → rate limit → loop detection → business rules
- Per-run cost cap, step cap, wall-clock deadline; loop detection on `(tool, normalized_args)` hashes
- OTel spans per step capturing **the full assembled context by reference**, raw model response before parsing, guard decision, tool result, tokens, cost, latency
- Deterministic replay: re-execute a recorded run against recorded model responses to test code changes without model calls

**Gate:** an incident in a real scan can be diagnosed by reading the trace, and replayed deterministically.

### Phase 3 — The repair stage

- State machine: `scanning → diagnosing → planning → awaiting_approval → executing → done`
- Tools available per state; mutating tools exist **only** in `executing`
- Snapshot-before-mutate, with a working `revert`
- Approval UI: a readable diff of proposed changes, grouped by problem class, with the evidence for each
- Idempotency keys on every mutation, derived from `(plan_id, item_id, operation)`

Guard rules to implement:
- Never delete a file. Ever. Moving and renaming only, and both are revertible.
- Never merge duplicates without approval, regardless of confidence
- Never apply a repair whose confidence is below threshold — surface it as "needs human decision"
- Cap repairs per plan; beyond it, split into multiple plans
- Any repair to an item edited by the user since the last scan → flag, do not auto-apply

**Gate:** a full corrupt→repair→verify cycle on the synthetic dataset, plus a successful `revert`.

### Phase 4 — Framework migration (optional, deliberate)

Port the loop to LangGraph **only** to learn it and to gain checkpointing/interrupt semantics. Keep the guard layer, tools, and eval harness framework-independent. Validate the migration by running the same eval suite against both implementations and diffing results.

### Phase 5 — MCP server + durable execution

- Extract the Plex and metadata tools into a standalone **MCP server**, published separately. This is a reusable artifact independent of the agent.
- Move orchestration to **Temporal** when full-library scans (thousands of items) make crash recovery and suspension genuinely necessary. Let the pain justify it; document what changed.

---

## 6. Evaluation

The centerpiece of the project. Treat the eval suite as the product and the agent as the thing being measured.

### Dataset composition

| Slice | Share | Source |
|---|---|---|
| Synthetic corruptions | 50% | Generated (Phase 0) |
| Real problems from my library | 25% | Manually labeled |
| Should-not-touch | 15% | Correct items the agent must leave alone |
| Ambiguous / escalate | 10% | Cases with no confident answer |

The **should-not-touch** slice is critical. An agent that "fixes" correct items is worse than useless. False-positive rate is a headline metric, not a footnote.

### Scoring levels

- **Component:** does tool selection match expectation for a given item type and problem?
- **Trajectory:** did it consult an external source before proposing? did it stay within step budget? no repeated identical calls? no mutating tools outside `executing`?
- **Outcome:** does the proposed repair match ground truth? Deterministic where the truth file allows.
- **Groundedness:** does every claim cite a retrieved source record? Mechanical check, hard failure if not.

### Reported metrics

Report all of these, sliced by media type and problem class:

- Pass rate
- **False-positive rate** on the should-not-touch slice
- Correct-escalation rate on the ambiguous slice
- Median and p95 steps per item
- Median and p95 cost per item
- Total cost per 100 items

### CI

Fast suite on every commit; full suite nightly. Gate on **relative** change: no case that passed may now fail. Report a case-level diff, not just an aggregate.

---

## 7. Out of scope

Explicitly not building these. Do not add them opportunistically.

- Transcoding, re-encoding, or any modification of media files themselves
- Downloading or acquiring media
- Subtitle management
- A web UI (CLI + approval diff is sufficient; revisit only after Phase 5)
- Multi-agent decomposition (a single agent with good tools handles this; revisit only with measured evidence)
- Support for Jellyfin/Emby (keep the abstraction clean so it stays possible, but do not build it)
- User-facing recommendation or "what should I watch" features

---

## 8. Definition of done

The project is complete when the README opens with a table showing:

- Pass rate by media type and problem class
- False-positive rate on the should-not-touch slice
- Cost per 100 items and p95 steps per item
- A count of repairs correctly declined as too ambiguous

...and the repository contains a working corruption generator, a labeled eval dataset, deterministic replay, a guard layer with unit tests, and a `revert` that has been demonstrated on a real library.

The agent itself is the least interesting artifact here. The measurement apparatus around it is the point.

---

## 9. Notes for Claude Code

- Verify current `python-plexapi` capabilities and the auth/rate-limit requirements of TMDB, TVDB, Audnexus, and Open Library before designing tool schemas. Do not assume API shapes from memory.
- Prefer many small, well-described tools over few general ones — but consolidate near-identical tools behind an enum parameter rather than creating five variants.
- Tool error messages are prompts. Every error must tell the model what went wrong **and what to do instead**. Classify errors as retryable (handle in code, never surface), correctable (surface with guidance), or terminal (surface and state that retrying will not help).
- Tool outputs are resent on every subsequent turn. Return the minimum useful payload; paginate with explicit counts.
- Ask before adding any dependency that introduces persistent state or a new service.
- When a change is proposed to fix an observed failure, it must be accompanied by an eval case that fails before and passes after.
