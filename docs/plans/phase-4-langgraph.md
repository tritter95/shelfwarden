# Phase 4 — LangGraph: decision record and verified findings

Written 2026-09-01 against commit `b1368f4`, with 0.1–0.45 complete and CI green.
This is **not** an implementation plan. `implementation-plan.md` covers Phase 0
and Phase 1 in executable detail and deliberately treats Phases 2–5 as a roadmap,
on the grounds that planning against unknowns the earlier phases will surface is
planning fiction. That reasoning still holds. What this document records is the
part that can be settled now and would be expensive to get wrong later: **which
of the two libraries this project adopts, what that costs, and where the port is
allowed to touch.**

Every version and API fact in §2 was read from PyPI release metadata and the
current LangChain documentation on 2026-09-01, and each is cited. Unlike the
findings in `step-0.45-comparators-screen.md`, none of them was produced by
running code in this checkout — no LangGraph dependency is installed and this
change adds none. They are therefore **documented behavior, not observed
behavior**, and Phase 4's first task is to re-verify Findings 3 and 4 against the
installed version before building on them.

---

## 1. The question

The spec settled the direction in §4 and §5: hand-rolled loop first, *"LangGraph
in phase 4 only"*, ported *"only to learn it and to gain checkpointing/interrupt
semantics."* Nothing here relitigates that. Two things about it were never
settled, and both matter more than they look:

1. **LangChain and LangGraph were treated as one choice.** They are now two
   layers, and only one of them fits this design. See Finding 1.
2. **Phase 4 had no gate.** Every other phase in `roadmap.md` carries a
   `> **Gate:**` line stating what "done" means. Phase 4 and Phase 5 do not, and
   Phase 4 additionally carries the word *optional*. In a gated build order, an
   optional phase with no gate is a phase that never happens — which is the
   ordinary fate of the "then we'll evaluate the framework" line in any plan.

---

## 2. Verified findings

### Finding 1 — LangChain is now a layer *above* LangGraph, and this project already owns that layer

`langchain` 1.3.18 declares `langgraph<1.3.0,>=1.2.11` as a runtime dependency.
The relationship inverted somewhere around the 1.0 releases: `langgraph` is the
low-level orchestration runtime, and `langchain` is the agent layer built on top
of it — `create_agent`, middleware, and a provider extra per vendor
(`langchain[anthropic]`, `langchain[openai]`, …). Its own documentation positions
it for *"rapid agent and autonomous application development"*, reserving LangGraph
for *"a combination of deterministic and agentic workflows, heavy customization,
and carefully controlled latency."*

That description is this project. Every abstraction `langchain` sells has a
hand-built counterpart here that exists for a reason recorded in a step plan:

| `langchain` provides | This project already has | Why the local one exists |
|---|---|---|
| Chat model wrappers | `LLMProvider`, `Proposal`, `Usage` (1.2) | The eval harness must invoke the agent through an interface we own |
| Message normalization | Raw responses stored **verbatim** (1.5) | It is the single thing that makes Phase 2 replay a config change |
| Tool binding + schema gen | `ToolSpec` + the `$ref`/`$defs` inliner (1.2/1.3) | Registry keyed by `RunPhase` is the structural read/write seam |
| Output parsers | The groundedness validator (1.4) | Invariant 1: code disposes, and a parser that "fixes" a claim erases the evidence |
| Provider-agnostic reasoning | `ProviderCarryover` (1.2) | Reasoning replay is provider-opaque on purpose |

Adopting `langchain` would replace the measured interface with an unmeasured one
and put a normalization step between the model and the bytes Phase 2 replays.
**LangGraph yes; LangChain no.**

### Finding 2 — `langchain-core` arrives anyway, as a hard dependency

`langgraph` 1.2.11 requires `langchain-core<2,>=1.4.7`, `langgraph-checkpoint`,
`langgraph-prebuilt`, `langgraph-sdk`, `pydantic>=2.7.4` and `xxhash>=3.5.0`.
There is no LangGraph-without-langchain-core install.

So "no LangChain" cannot mean "no `langchain*` package in the tree", and a
decision phrased that way would be discovered to be false by `uv sync` and then
quietly abandoned. It means what it already means for `plexapi`, `openai` and
`anthropic`: **an import contract confines it to one module.** An eighth contract
forbids `langgraph` and `langchain_core` anywhere outside `agent/graph/`, which
keeps the claim testable instead of aspirational.

### Finding 3 — `interrupt()` re-executes its node from the top, so any side effect above it runs twice

The human-in-the-loop primitive is `interrupt(value)`, resumed with
`Command(resume=value)`. The documented resume semantics are that **the node
restarts from the beginning and all code before the `interrupt()` call runs
again**; the prescribed pattern is pure computation before, side effects after,
and anything above the call must be idempotent.

This is the single most dangerous fact in this document for this project
specifically, because the collision is exact:

- The approval gate is Phase 3's `awaiting_approval` state — the intended use.
- The side effects are Phase 3's Plex edits and snapshot writes.
- Invariant 3 is *every mutation is reversible*, and invariant 11 is *never
  delete a file*.

The mitigation already exists and was designed for a different threat: Phase 3
derives idempotency keys from `(plan_id, item_id, operation)`. That was written
for retries. Here the replay is not an error path — it is the framework's normal
operation, on the one transition where mutations live. The rule that follows is
narrow and testable: **no snapshot write and no mutating tool call may appear
above an `interrupt()` in any node.**

### Finding 4 — durability is a three-position knob, and the right position is a measurement

`durability` accepts `"exit"` (persist when execution completes or interrupts),
`"async"` (persist during the next step) and `"sync"` (persist before the next
step starts). Approval gaps are covered by all three, since an interrupt is a
persistence point under `"exit"` as well. Crashes mid-scan are not: under
`"exit"`, a scan that dies at step 40 of 60 has persisted nothing.

A full-library scan costs real tokens, and the project's own posture is that a
number nobody measured is not a number. Phase 4 records the setting it chose and
what the other two cost, rather than inheriting a default.

### Finding 5 — the checkpointer is a second store, and it must not become the audit log

`langgraph-checkpoint-sqlite` 3.1.1 provides `SqliteSaver` / `AsyncSqliteSaver`
over `aiosqlite`, and is positioned by its own documentation for *"experimentation
and local workflows"*; `langgraph-checkpoint-postgres` is the production
recommendation. For a single-user local tool, SQLite is the right call regardless
of that positioning — the project is already SQLite end to end.

The real cost is not the backend, it is that checkpoint tables are LangGraph's
schema, thread-scoped by `{"configurable": {"thread_id": ...}}`, and they overlap
in purpose with the `runs` / `steps` / `blobs` tables from 1.5. Two stores holding
run state is how invariant 5 — *outcomes derive from recorded state* — starts
being ambiguous about **which** recorded state. The decision is in §3.4: the
checkpointer is a resumption mechanism and never a source of truth. `get_state`,
`get_state_history` and `update_state` are how the port re-enters a run; the audit
log stays where the scorer already reads it.

### Finding 6 — the graph the port needs is already written, in prose

Phase 3's state machine is specified in `roadmap.md` as
`scanning → diagnosing → planning → awaiting_approval → executing → done`. That is
a `StateGraph` with six nodes, one of which interrupts. The port is therefore not
a translation exercise; it is transcribing a design that already exists.

The alternative is the functional API — `@entrypoint` / `@task`, tasks memoized on
resume, documented as the way to add LangGraph *"with minimal changes to your
existing code"* and mapping almost directly onto a `while not state.done` loop. It
is the cheaper port and it forfeits graph visualization. Recommendation:
`StateGraph`, because the learning goal is the point of the phase and the state
machine is the artifact worth seeing drawn. The functional API is the documented
fallback if the port stalls — not a lesser outcome, just a different one to write
up.

### Finding 7 — the earliest point where a port can be *measured* is 1.7

The eval harness is framework-independent by construction and invokes the agent
through an interface this project owns. So a second implementation of the loop can
be scored by the same runner with no harness changes — that affordance was
designed in, and it is the only thing that makes a framework migration falsifiable
here.

Before 1.7 there is no scored report to diff against, so a port would be an
unmeasured rewrite: the project's own definition of failure. After 1.7 it is an
experiment. This is what licenses the spike in §4.1 to run ahead of the gate order
without violating it.

---

## 3. Decisions

### Decision 1 — adopt LangGraph, not LangChain

Runtime dependency: `langgraph` (with `langgraph-checkpoint-sqlite`).
`langchain-core` enters transitively and is confined, not used directly.
`langchain` itself is not adopted, for the reasons in Finding 1. This is
revisitable on evidence — the writeup in §5 is where the evidence would appear —
but it is not revisitable on convenience.

### Decision 2 — an eighth import contract, added when the dependency is

```toml
[[tool.importlinter.contracts]]
name = "the graph runtime is confined to agent/graph"
type = "forbidden"
source_modules = ["shelfwarden"]
forbidden_modules = ["langgraph", "langchain_core"]
# with an ignore_imports pair for agent.graph.*, in the shape the openai and
# anthropic contracts already use (both the bare name and the `.**` wildcard —
# a bare wildcard alone is rejected).
```

### Decision 3 — the port is a second implementation, not a replacement

`agent/graph/` is new; `agent/loop.py` stays. Both run the same suite in CI until
the diff is empty or explained.

The load-bearing part is what the port is **not allowed to touch**: `agent/tools/`,
`agent/validate.py`, `agent/provider/`, `agent/guard/`, `agent/state.py`,
`compare.py`, `evals/`. `development-practices.md` §1.2 has claimed since 0.1 that
`agent/loop.py` is the only module LangGraph would replace. That claim has never
been tested, and Phase 4 is the test. If the port needs to change any of those
modules, the change is a **finding about the seam** — recorded here, in the shape
0.4 and 0.45 recorded theirs — and not a quiet edit.

### Decision 4 — the checkpointer is a resumption mechanism, never the audit log

Per Finding 5. Scoring, `revert`, and every metric keep reading `runs` / `steps` /
`blobs`. A checkpoint may be discarded without loss of anything the project makes
claims about. Corollary: the port writes the same step rows the hand-rolled loop
does, which is also what makes the per-case diff meaningful.

### Decision 5 — no side effect above an `interrupt()`

Per Finding 3, and it survives as a rule whether or not Phase 4 ever lands, since
Phase 3 builds the approval gate first. Enforced by a test, not by review.

### Decision 6 — the spike may run out of gate order; the phase may not

Phase 4's first step is a timeboxed, non-gating spike that may begin as soon as
1.7 exists (Finding 7). It is the one exception to the build order in this
project, and it is narrow: it produces a comparison and a writeup, no dependency
on it is added to any other phase, and Phases 2 and 3 proceed as specified whether
it succeeds or fails. The remaining steps stay behind the Phase 3 gate, because
interrupts without a repair stage would be an approval gate with nothing to
approve.

---

## 4. Shape of the work

Sketch, not a plan. Phases 2 and 3 will change it.

**4.1 Spike (non-gating, may run after 1.7).** Port `agent/loop.py` to a
`StateGraph` in `agent/graph/`, `InMemorySaver` only, no interrupts, no new
contract. Run `shelfwarden eval` against both implementations and diff per case.
The output is knowledge and a paragraph in §6; the code may be thrown away.

**4.2 The port proper.** `SqliteSaver`, the eighth import contract, both
implementations in CI, `--engine loop|graph` on the eval runner.

**4.3 Interrupts.** Move Phase 3's `awaiting_approval` onto
`interrupt()` / `Command(resume=...)`. The test that matters kills the process
between approval and apply.

**4.4 The writeup.** §6 of this document, filled in.

---

## 5. Gate

> The same eval suite runs against both loop implementations and the per-case diff
> is empty or explained; an approval gate round-trips through
> `interrupt()`/`Command(resume=...)` across a killed process; and §6 records what
> the framework bought, what it cost, and which of Decision 3's forbidden modules
> the port had to touch.

The third clause is the one that makes this a learning artifact rather than a
migration. A port that reports "it worked" and nothing else has produced no
knowledge, and this phase exists for the knowledge.

---

## 6. What the framework actually bought

*Filled in when 4.4 lands. Empty on purpose — a section written before the
experiment is a hypothesis wearing a conclusion's clothes.*

---

## 7. Open questions

- **Does LangGraph durability remove the need for Temporal?** Phase 5 justifies
  Temporal by crash recovery and suspension on full-library scans. Findings 4 and
  5 cover exactly those. Phase 5's decision should be taken *after* 4.2 has a
  measured answer, and "we no longer need it" is a legitimate — arguably the most
  interesting — outcome for a phase whose stated rule is *let the pain justify it*.
- **Where does the guard chain sit in a graph?** Phase 2's ordered chain is a
  function call in the hand-rolled loop. As a node it becomes visible and
  interruptible; as a wrapper it stays framework-independent per the spec's own
  Phase 4 instruction. Unresolved, and resolvable only against Phase 2's code.
- **Does the eval runner need concurrency changes?** `SqliteSaver` has an async
  twin and the project is `asyncio_mode = "auto"` already, but running 200 cases
  through one checkpoint database is a contention question nobody has measured.
- **Re-verify Findings 3 and 4 on the installed version before building on them.**
  They are documented behavior read from the docs, and this project's standard for
  a behavioral claim is a test in the checkout.
