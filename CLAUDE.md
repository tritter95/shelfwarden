# ShelfWarden

An agentic media library steward. It audits a Plex library, diagnoses metadata and organization problems, proposes repairs, and — only after approval — applies them.

**This is a learning project with a real use case, not a product, and that ordering shapes every decision.** The goal is a production-grade agentic system built end to end: eval harness, guard layer, durable execution, tracing, staged autonomy. When the learning goal and library coverage conflict, the learning goal wins. A tool that handles 70% of problems with measurable reliability is the target; 95% with no measurement is a failure.

---

## Documents

| File | What it is |
|---|---|
| `docs/shelfwarden.md` | The spec. **Decisions in §3 are settled — do not relitigate them during implementation.** |
| `docs/development-practices.md` | Stack conventions and verified library traps. **Read the relevant section before writing code.** |
| `docs/implementation-plan.md` | Phase 0 + 1 design detail: architecture, schemas, gated build steps |
| `docs/roadmap.md` | Checkable progress across all phases. Update it as work lands. |

---

## Before you change code

**Read the section of `docs/development-practices.md` covering what you are touching.** It is not style advice. Nearly every rule in it exists because a specific library, API, or architectural decision has a verified trap — the kind that fails silently rather than loudly. Working from memory of how these libraries usually behave will reproduce the exact bugs that document exists to prevent.

If you change something that contradicts a rule there, either follow the rule or update the document in the same change with the reason. Do not leave the two disagreeing.

**A `PostToolUse` hook enforces part of this automatically.** `.claude/hooks/py-check.sh` runs on every Python `Write`/`Edit`: it silently auto-fixes formatting and lint, and surfaces back to you anything it cannot fix — remaining `ruff` violations and broken import contracts. It stays silent when the project is not yet scaffolded or the package is not yet installed. It is a fast feedback loop, not a gate; CI is the gate. If it fires, something in this document was skipped.

---

## Non-negotiable invariants

From spec §3 and `development-practices.md` §10. Violating any of these is a bug regardless of what else the change accomplishes.

1. **The model proposes, code disposes.** Any rule expressible as a predicate lives in code — a type, a lint contract, a test — not in a prompt. Prompts are guidance; code is enforcement.
2. **Scan and diagnose cannot mutate.** Not "should not" — mutating tools do not *exist* in those phases. This is structural, enforced by the `LibraryProvider` protocol having no mutating method and by the phase-keyed tool registry.
3. **Every mutation is reversible**, including field lock-state changes. `revert <plan-id>` restores pre-plan condition. Compensation failures alert loudly, never silently.
4. **Dry run is the default.** `apply` simulates unless given `--commit`.
5. **Outcomes derive from recorded state, never from the model.** This covers `confidence`, `needs_human`, success claims, and narrative summaries alike. The model's narrative is presentation only.
6. **Confidence is computed in code.** The model's number is recorded as `model_confidence` and gates nothing. The auto-apply guard reads `band`, never `value`. `band` gates auto-apply but never gates eval scoring — scoring keyed on a tunable constant is gameable by tuning the constant.
7. **A finding with an unbound referent is rejected**, however well-cited. Citation integrity and referent binding are separate checks reported as separate numbers.
8. **`unexpected: fail` is the default on every eval case**, not just should-not-touch — otherwise most of the dataset is blind to fabricated findings.
9. **Case identity is semantic, never positional**, and never derived from a Plex `rating_key` (they move on rescan).
10. **No corruption ships without a detectability witness**, and **silence is not escalation** — an agent that finds nothing must not score as correctly escalating.
11. **Never delete a file. Ever.** Moving and renaming only, both revertible.
12. **No silent caps.** If code truncates, samples, or drops, it logs what it dropped.

---

## Working rules

- **Ask before adding a dependency that introduces persistent state or a new service.**
- **Every fix for an observed failure ships with an eval case that fails before and passes after.** Without it, "fixed" is a claim rather than a fact.
- **Never hit a live external API in tests or CI.** `vcrpy` cassettes for real response shapes, `respx` for hand-crafted error conditions.
- **Tool outputs are resent every turn.** Return the minimum useful payload; paginate with explicit counts.
- **Tool error messages are prompts.** Classify every error as retryable (handled in code, never surfaced), correctable (surfaced with a concrete next action), or terminal (surfaced, states retrying won't help). A correctable error that doesn't name a next action is a bug.
- Prefer many small, well-described tools — but consolidate near-identical ones behind an enum parameter rather than creating five variants.

---

## Build order

Phases are gated. **Do not begin a phase until the previous gate is met** — see `docs/roadmap.md` for the gate text and current state.

- **Phase 0** — the corruption harness. The single most important deliverable; everything downstream is measured against it.
- **Phase 1** — read-only agent, hand-written loop, no framework.
- **Phase 2** — guard layer, budgets, tracing, deterministic replay.
- **Phase 3** — the repair stage, snapshots, `revert`.
- **Phase 4** — LangGraph migration (optional, deliberate).
- **Phase 5** — MCP server extraction + Temporal.

Do not reach for a framework to skip a concept, and do not defer the eval harness.

---

## Commands

The project is scaffolded in step 0.1; until then these are the intended shapes.

```bash
uv sync --group dev
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run lint-imports                     # architectural seam contracts

uv run shelfwarden export --count 200   # pull a real library slice
uv run python -m shelfwarden.evals.generate --count 200 --seed 1518
uv run shelfwarden eval --dataset <id> --provider openai
uv run shelfwarden scan --section <id> --limit 5 --dry-run
```

Everything runs through `uv run`. Never `pip install`, never activate the venv.

---

## Things that look wrong but are correct

Do not "fix" these — each is deliberate and the reasoning is in `docs/development-practices.md`.

- `sqlite3.connect(..., autocommit=False)` passed **explicitly** — the 3.12+ default is still `LEGACY_TRANSACTION_CONTROL` and CPython has announced it will change.
- `PRAGMA foreign_keys=ON` on every connection — it is per-connection and off by default.
- Both `container_start` **and** `maxresults` on every plexapi page call — `container_start` alone still walks the entire remaining result set.
- `locked=` passed explicitly to every plexapi edit helper — the default is `True`, which silently pins the field against future agent refreshes.
- `search_metadata(source=...)` instead of separate `search_tmdb`/`search_tvdb` tools — deliberate consolidation per spec §9.
- `lookup_audiobook` instead of the spec's `search_audnexus` — **Audnexus has no book-search endpoint**; the tool encapsulates an ASIN resolution ladder in code.
- `DerivedClaim` carrying no `asserted_value` — the validator recomputes it from the named rule. The model chooses the rule and inputs; code decides the truth.
- Emitting both `gen_ai.system` and `gen_ai.provider.name` — the OTel GenAI conventions are mid-rename and unstable.

---

## When you find a problem with the spec

Say so in a sentence or two, then keep building under a stated assumption. Several spec assumptions have already been corrected by verified research — `search_audnexus` cannot be built as written, Plex has no audiobook library type, plexapi has no read-only mode. Corrections like these belong in `docs/implementation-plan.md` with the reasoning recorded, not silently worked around.
