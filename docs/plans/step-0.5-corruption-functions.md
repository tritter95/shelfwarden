# Step 0.5 — Corruption Functions

Implementation plan for roadmap step **0.5 Corruption functions**. Written
2026-09-01 against commit `b1368f4`, with 0.1–0.45 complete and CI green.

Every finding in §2 was produced by running CPython 3.13.12 in this checkout, not
recalled. Four of them change the design, one of them changes a table 0.45
already ships, and one of them corrects the `CorruptionResult` shape that
`implementation-plan.md` §3 specifies.

**Gate for the step** (`roadmap.md`): each function has a unit test asserting the
mutation applied, the truth record round-trips, and the case is provably
detectable.

---

## 1. Scope

0.4 produced an export that does not change. 0.45 decided **what can be claimed
about the items in it**. 0.5 is the step that **breaks them on purpose**, and it
is the first step whose output is a claim about the *agent* rather than about the
library: a case that cannot be solved is not a hard case, it is a broken ruler.

The motivating rule is `implementation-plan.md` §3: *every corruption must prove
its own detectability, or the case is not emitted.* Left to scoring time, an
unsolvable case depresses the pass rate and hides real regressions — and it does
so in the one place nobody looks, because a low score on a hard class reads as a
model problem rather than a harness problem.

Deliverables:

- **`evals/corrupt/`** — the registry and the corruption functions. Fifteen
  problem classes are declared; **eleven are implemented at this step** and four
  are declared with a recorded reason and zero cases (Decision 4).
- **`pointer.py`** — one pointer grammar, RFC 6901, shared by the changes, the
  witnesses, `must_not_change`, and step 1.4's referent binding. Top-level for
  the same reason `compare.py` is (Decision 2).
- **`DetectabilityWitness`** in three kinds, and the checks that reject a case
  the harness cannot solve (Decision 3).
- **`apply_reverse`** over an item-set delta, and the property test that it
  reproduces the ground-truth family byte-for-byte.
- **`shelfwarden corrupt`** — a preview artifact and a per-class deficit report:
  the table you read *before* writing `composition.toml`, the way
  `--census-only` is the command you run before choosing `--count`.

Supporting, each forced by one of the above:

- `compare.py` — `path_segments()`, `parse_release_path()`, `find_in_path()`.
  Four classes' local witnesses live in a directory name, and
  `parse_release_name` reads the basename only (Finding 6).
- `evals/screen.py` — one row of `GUARD_TABLE` corrected (Finding 5, Decision 7).
- `cli.py`, one new import contract, tests, doc updates.

**Not in 0.5:** the truth file schema and `case_id` (0.6), `composition.toml`
(0.6), `SnapshotLibrary` (0.7), the scorer (0.8), any network fetch (1.1). No new
dependencies.

**Before writing code**, run `shelfwarden screen` against a real export and read
two numbers off `screen.md`: the pass rate of `filename_matches_metadata`, and
the `insufficient` count. Eight of the eleven implementable classes take their
detectability witness from a file path, so that pass rate *is* this step's yield
forecast. If it is low, the deficit report in §5.8 will be the real output of the
step, and that is a finding about the library rather than a failure of the step.

---

## 2. Verified findings

### Finding 1 — the model normalizes on write-back, so `after` must be read back

`with_changes` re-validates, and validation is not the identity function. Three
fields rewrite what you hand them:

```
title = NFD("Amélie")   -> stored as NFC       intended == stored: False
guids  = [tvdb, imdb]   -> stored sorted        [imdb, tvdb]
locked_fields = ("summary","title","summary") -> ('summary', 'title')
```

A `FieldChange` that records the *intended* value as `after` therefore describes
a mutation that did not happen. The consequence is not cosmetic: the reverse of
that change writes the un-normalized value back, `apply_reverse` produces bytes
the ground truth never had, and the property test fails on a case whose
corruption was in fact correct — or worse, passes for the wrong reason when the
input happened to already be normalized.

**Design consequence.** `before` and `after` are both **read back from the dumped
item**, never taken from the caller's argument. A corruption function returns a
mutated item and the recorder diffs it; it never reports its own intent. Guid
edits additionally go through `parse_guids`, because `ExternalId.raw` is part of
the record and a hand-built `ExternalId` whose `raw` still names the old guid is
an internally inconsistent record that a witness could cite.

### Finding 2 — canonical bytes, not Python equality, decide a no-op

`implementation-plan.md` §3 requires that a `FieldChange` with `before == after`
raises. Python equality is the wrong comparison:

```
canonical_json(True) -> b'true'     canonical_json(1) -> b'1'      True == 1 -> True
```

A change from `1` to `True` compares equal in Python and produces different
bytes; the byte-identity gate in §6 would then fail on a change the no-op check
had already blessed. The mirror case is real too — `8` and `8.0` compare equal
and, once pydantic has coerced both to a float field, serialize identically, so
byte comparison correctly calls *that* one a no-op.

**Design consequence.** The no-op check is
`canonical_json(before) == canonical_json(after)`, and it raises. Same serializer
as the gate it protects.

### Finding 3 — `random.sample` is not a prefix-stable function of `k`

`Random.sample` contains two algorithms and picks between them on a size
heuristic (`setsize`, which itself depends on `k`). Same seed, same population,
`k` raised by one:

```
Random(1518).sample(range(24), 5) -> [6, 15, 23, 8, 7]
Random(1518).sample(range(24), 6) -> [6, 15, 8, 7, 3, 1]
```

Element 23 was selected and is now not. Three such divergences appear in
`n = 22..89` alone.

The consequence lands squarely on 0.6's requirement that `case_id` survive
regeneration. If a class's share in `composition.toml` moves from 5 cases to 6,
seeded sampling re-picks *different subjects*, every `case_id` in that cell
changes, and the CI baseline resets on a one-case edit — the exact failure
`case_id` was made semantic to prevent. `Random.shuffle` has the same shape for a
different reason: it is Fisher–Yates over `_randbelow`, so its draws depend on
the list length, and a candidate list that grows by one item permutes everything.

**Design consequence.** Subject selection is by **hash-rank**, never by a random
draw: sort candidates by `sha256(seed | subject_key)` and take the first *N*.
Raising *N* is then additive by construction. The RNG is used only *within* a
case, from a per-case seed (Decision 5).

### Finding 4 — corruption has a blast radius, and a stale root index hides it

Two of the screen's predicates are population-scoped. Corrupting one family
therefore changes the screen verdict of items in *other* families. Corrupting
`fake:1:101` (Amélie) to carry the identity of `fake:1:103` (Solaris 2002):

```
fake:1:101  guarded -> failed   guards lost: duplicate_quality, filename_unmatchable
fake:1:103  guarded -> failed   guards lost: duplicate_quality        <- untouched item
```

`fake:1:103` was never mutated. It lost a guard because the *other* item became
its twin. If it had been drawn into the should-not-touch slice, a correct agent
finding on it would score as a false positive — the direction this project has
forbidden.

The second half is worse, because it looks fine. Re-running the same screen with
`items.jsonl` corrupted but `roots.jsonl` left stale:

```
fake:1:103 keeps  duplicate_quality   (the guard is now false)
fake:1:101 still fails no_title_year_twin
```

The twin relation becomes **asymmetric**: A sees B, B does not see A, because A's
lookup key comes from the corrupted item while the index it probes still
describes the clean world. Nothing raises, and the screen reports a guard that is
not true.

**Design consequence.** Two rules. The population index is **derived from the
corrupted items**, never carried over — a root stub is a projection of an item,
so 0.6 rebuilds `roots.jsonl` from the corrupted world rather than patching
stubs. And every corruption computes and declares its **collateral**: the ids
outside its own family whose population-scope key it moved. Because only two
predicates are population-scoped, collateral is computed by recomputing two keys
for the changed items, not by re-screening the export.

### Finding 5 — the realistic `episode_wrong_season` leaves its guard intact

`GUARD_TABLE[EPISODE_WRONG_SEASON]` is `{season_membership_coherent}`. That
predicate compares `episode.parent_index` against `season.index` **for the season
the episode actually hangs under**. Re-parenting an episode into another season —
setting `parent`, `parent_index`, and `parent_title` together, which is the only
form Plex can represent — leaves the two consistent:

```
episode fake:2:2212 moved from season 1 to season 2
  season_membership_coherent : PASS      (internally coherent, and wrong)
  filename_matches_metadata  : FAIL      ('S01E02.mkv' says S01E02, metadata says S02E02)
  guarded classes            : episode_wrong_season   <- unchanged by the corruption
```

So an item can be labelled *verified not to have* `episode_wrong_season` while
having exactly that. The predicate guards only the incoherent form — an episode
whose `parent_index` disagrees with its parent — which a real Plex server does
not produce. The class was effectively unguarded and reported as guarded.

**Design consequence.** `filename_matches_metadata` joins that guard set
(Decision 7). It is local, already implemented, and it caught the realistic form
in the run above. The cost is honest and visible: for episodes whose files carry
no `SxxEyy` marker the predicate already returns `fail`, so those items were
never guarded anyway.

### Finding 6 — `parse_release_name` reads the basename, and four witnesses live in the directory

```
'/media/Movies/The Dark Knight (2008)/The Dark Knight (2008).mkv'
    -> title='The Dark Knight'  year=2008
'/media/TV/Cowboy Bebop/S01E02.mkv'
    -> title=''  season=1  episode=2
'/media/Books/Sanderson/The Stormlight Archive/The Way of Kings/CD1.m4b'
    -> title='CD1'
```

The function splits on `[\\/]` and keeps the last segment — correct and
deliberate for what 0.45 needed. But `multi_file_split`, `missing_series`,
`series_order_broken`, and `alternate_cut` all take their local witness from a
*directory*: the shared parent that proves two albums are one book, the series
folder that proves a stripped series membership, the `[Final Cut]` folder that
proves which cut a file holds. On the third path above, the basename tells you
nothing and the directory tells you everything.

**Design consequence.** `compare.py` gains `path_segments()`,
`parse_release_path()` (basename first, falling back to the parent directory for
a missing title or year, and reporting which segment supplied it), and
`find_in_path()` (the best `Support` for a string across the segments, via the
existing fold ladder). `parse_release_name` is left exactly as it is — 0.45's
tests pin its behavior and the screen's byte output depends on it.

### Finding 7 — no model field name needs RFC-6901 escaping, or collides with `*`

Across all seven item subtypes plus `FilePart`, `ItemStub`, `ItemId`, and
`ExternalId` — 51 distinct field names — none contains `/`, `~`, or `*`. Pydantic
dumps `ItemId` and `ExternalId` (both dataclasses) to plain objects, and
`dump -> set at pointer -> validate` preserves every unrelated field:

```
dump_item -> mutate /parts/0/path -> ItemAdapter.validate_python
    parts[0].path changed, year/guids/item_id intact, ItemId re-parsed as ItemId
round-trip: canonical_json(dump_item(x)) == canonical_json(dump_item(load_item(...)))  True
```

**Design consequence.** RFC 6901 addresses this model without escaping, and `*`
— a legal literal token character in the grammar — can be reserved as a wildcard
without ambiguity, because no field is named `*`. `select()` still raises if it
ever meets a mapping with a literal `*` key, so the reservation is checked rather
than assumed.

---

## 3. Decisions

### Decision 1 — the unit of corruption is a family, and the record is an item-set delta

`implementation-plan.md` §3 gives `CorruptionResult` a single `item` and a list
of `FieldChange`. Five of the fifteen classes cannot be expressed that way:
`duplicate_quality` **adds** an item, `author_name_variant` and
`multi_file_split` add items *and* re-parent existing ones, `absolute_vs_seasonal`
rewrites every episode of a show and empties a season, `anthology_omnibus` splits
one item into several. "A new item appeared" is not a field change on an old one.

So a corruption takes a **family** — a root and everything beneath it, which is
already 0.4's unit: `RECORD_ORDER` groups records by family and `--count` counts
roots, so that a family is never half-exported — and returns an **item-set
delta**:

```python
class ChangeKind(StrEnum):
    ADD = "add"; REMOVE = "remove"; MODIFY = "modify"

@dataclass(frozen=True, slots=True)
class ItemChange:
    kind: ChangeKind
    item_id: str
    fields: tuple[FieldChange, ...] = ()   # MODIFY only
    record: dict[str, Any] | None = None   # ADD: the whole item; REMOVE: the item removed
```

`apply_reverse` inverts the delta: remove what was added, re-add what was
removed, restore each field. It does **not** record ordering, because ordering is
not information: `_record_sort_key` is a pure function of the ids, so the
ground-truth family's record order is recoverable by sorting. The gate is that
the reversed set, rendered by `render_items`, equals the ground-truth family's
bytes exactly.

### Decision 2 — one pointer grammar: RFC 6901, with `*` as a selector-only extension

The truth file will carry two kinds of path in one document:
`corruption.changes[].path` into an item, and `witness.pointer` into an evidence
body — which `implementation-plan.md` already specifies as RFC 6901. Two
grammars in one file is one resolver call away from a silent bug in 0.8, where
the scorer picks the wrong one and gets a plausible answer.

So: **one grammar for both.** `/guids`, `/parts/0/path`, `/title`. Finding 7
shows it addresses this model unescaped.

`must_not_change` needs a wildcard, which RFC 6901 does not have. It gets one
documented extension: `*` as a **whole segment**, matching every index of an
array or every key of an object, and legal **only in a selector**. A
`FieldChange.path` containing `*` raises — a change addresses exactly one
location; a constraint may address many. `pointer.py` exposes `resolve()` (strict,
one location) and `select()` (returns `(pointer, value)` pairs), and a plain
pointer is a selector matching one thing, so there is one grammar with one
extension rather than two languages.

> **Correction to `implementation-plan.md` §3.** The illustrative truth JSON
> writes `{"path": "guids"}` and `must_not_change: ["parts[*].file"]`. Those
> become `"/guids"` and `"/parts/*/path"`. Two changes: the syntax, per the
> paragraph above, and the field name — the normalized model calls it
> `FilePart.path`, and there is no `file`. The truth file compares against a
> `NormalizedItem`, so it must speak that model's field names.

`pointer.py` is top-level beside `compare.py` and `canonical.py`, for the reason
recorded in 0.45's Decision 1: `agent/validate.py` resolves citation pointers in
1.4, and the agent must not import the package holding the answer key. A new
import contract, `"the pointer language is a leaf"`, forbids
`shelfwarden.pointer` from importing `agent`, `evals`, `library`, or `sources`.

### Decision 3 — three witness kinds, not one

`DetectabilityWitness` as specified proves one shape of solvability: a pointer
resolves a value that differs from the corrupted value and equals the ground
truth. That covers the classes whose repair restores a field. It does not
describe `duplicate_quality`, `author_name_variant`, or `multi_file_split`, where
the ground truth is a **relation over ids** — *these two entries are one work,
and this one is the keeper* — and no single resolved value is the answer. Nor
does it describe `anthology_omnibus`, whose expectation is `escalate`: there the
case is solvable precisely when the ambiguity is *evidenced*, and a witness that
resolved a unique value would prove the case is not ambiguous at all.

```python
class WitnessKind(StrEnum):
    VALUE = "value"          # a field's true value is recoverable
    RELATION = "relation"    # two or more ids are provably one thing
    AMBIGUITY = "ambiguity"  # >= min_candidates supported resolutions exist

@dataclass(frozen=True, slots=True)
class DetectabilityWitness:
    kind: WitnessKind
    source: Source
    tier: WitnessTier            # LOCAL | AUTHORITY
    evidence_id: str
    pointers: tuple[str, ...]    # RFC 6901 into the cited body/bodies
    comparator: str              # the compare.py function that judged it
    resolved: tuple[JSONValue, ...]
    subjects: tuple[str, ...] = ()   # RELATION/AMBIGUITY: the item ids related
    discriminates: bool = False
```

The acceptance rule per kind, run with **the scorer's own comparators** so the
generator and the scorer cannot disagree:

- **VALUE** — `compare(resolved, corrupted)` is below the policy minimum **and**
  `compare(resolved, ground_truth)` meets it. Inequality then equality, as
  specified.
- **RELATION** — the named comparator holds over the cited subjects **in the
  corrupted world** (`compare_person_name(a, b) >= ALIAS`, `compare_title` at the
  screen policy, a shared path segment), and the ground truth records exactly
  that relation. A relation that only holds in the clean world proves nothing.
- **AMBIGUITY** — at least `min_candidates` distinct resolutions, each with its
  own evidence. Defined here and unused: `anthology_omnibus` is curated, not
  synthesized (Decision 4).

One rule outranks all three, and it is the anti-circularity rule: **a witness
pointer must resolve against the corrupted world, never against the ground
truth.** A witness citing the pre-corruption record is the "well-cited finding
with an unbound referent" that invariant 7 rejects, wearing a generator's badge —
it proves only that we knew the answer, which was never in question. `LocalWitness`
is constructed with the corrupted family and has no access to the clean one.

### Decision 4 — the witness tier decides which classes ship at 0.5

`sources/` is step 1.1. Some corruptions need an external record as an
**ingredient** (a real TMDB alternate title; a real narrator's name — inventing
one makes the case fiction), some need it only as a **witness**, and some need
neither. Only the third group can be built now, and the honest response is 0.45's:
declare all fifteen, implement what the tier supports, and **publish the gap as a
number computed from a table rather than asserted in prose.**

| Class | Ingredient | Witness | 0.5 |
|---|---|---|---|
| `wrong_match` | local (donor root) | VALUE — path title/year | ✅ |
| `year_collision_remake` | local (remake pair) | VALUE — path year | ✅ |
| `foreign_title_variant` | **authority** | authority | ⛔ 1.1 |
| `alternate_cut` | local | VALUE — edition marker in a path segment | ✅ |
| `missing_metadata` | local | **authority** — nothing local restores a nulled summary | ⛔ 1.1 |
| `duplicate_quality` | local | RELATION — the twin | ✅ |
| `episode_wrong_season` | local | VALUE — `SxxEyy` in the path | ✅ |
| `absolute_vs_seasonal` | local | VALUE — `SxxEyy` in the path | ✅ |
| `filename_unmatchable` | local | VALUE — the scene name we wrote | ✅ |
| `series_order_broken` | local | VALUE — position in a path segment | ✅ |
| `author_name_variant` | local | RELATION — `compare_person_name` | ✅ |
| `narrator_as_author` | **authority** | authority | ⛔ 1.1 |
| `multi_file_split` | local | RELATION — shared parent directory | ✅ |
| `missing_series` | local | VALUE — series in a path segment | ✅ |
| `anthology_omnibus` | **authority** | AMBIGUITY | ⛔ curated (0.9) |

Eleven implemented, three waiting on 1.1, one that is not synthesizable by design.
The table lives in code as `CORRUPTION_TABLE`, `UNSYNTHESIZABLE_REASON` mirrors
0.45's `UNGUARDABLE_REASON`, and a test asserts every `ProblemClass` has a row in
one or the other. The counts in this document are computed from it, never typed.

`missing_metadata` deserves its own note, because it is the largest share in most
plausible compositions and it is one of the three that cannot ship. Detecting the
*problem* is trivially local — the summary is empty. Resolving the *ground truth
value* is not, and the postcondition is the original text. Weakening the
expectation to "non-empty and cited" would make the class shippable and the
metric meaningless; it stays deferred and loudly reported.

### Decision 5 — per-case RNG, derived from the case key

The RNG is not one stream consumed in iteration order. Each case gets
`Random(int.from_bytes(sha256(canonical_json({seed, subject_key, problem_class,
variant}))[:8]))`. Two consequences, both required by 0.6:

- Adding, removing, or reordering cases cannot perturb another case's draws, so
  `corruption_fingerprint` changes only for cases that actually changed and the
  CI diff's `changed` bucket stays meaningful.
- The seed derives from the same tuple `case_id` will, so a case's corruption is
  reproducible from its identity rather than from its position.

Within a case, the RNG surface is deliberately small — `randrange`, `choice`, and
a project-local `shuffled()` built on `randrange` — because Finding 3 is about an
algorithm inside the stdlib rather than about the seed, and the small surface is
what makes a CPython upgrade a diffable event rather than a silent renumbering.
`random.sample` is not used anywhere in `evals/corrupt/`, and a test asserts that.

### Decision 6 — corruptions declare induced problems and computed collateral

A `wrong_match` built from a donor inside the export gives the victim the donor's
title and year, which manufactures a title/year twin — verified in Finding 4. The
agent may correctly report `duplicate_quality` on it, and `unexpected: fail` (0.6)
would score that correct finding as a false positive.

Two fields, and the distinction between them matters:

- `induced: tuple[ProblemClass, ...]` — problems this corruption knowingly
  creates **inside its own family**. 0.6 writes them into
  `known_other_problems`, where they are neither required nor penalized.
- `collateral: tuple[str, ...]` — ids **outside** the family whose
  population-scope key this corruption moved, computed by recomputing the two
  population keys for the changed items. 0.6 evicts those ids from the
  should-not-touch slice; a corruption whose collateral is empty costs nothing.

The alternative — forbid corruptions that manufacture a collision — was rejected
because it would require every `wrong_match` donor to come from outside the
library, which is an authority ingredient, which would defer the class to 1.1
along with the other three. Declaring is honest and cheap; deferring is neither.

### Decision 7 — `filename_matches_metadata` joins the `episode_wrong_season` guard

Per Finding 5. One line of `GUARD_TABLE`, one test that a re-parented episode is
no longer guarded, and a note in the screen module naming 0.5 as the step that
found it. `screen.json`'s `schema_version` does not move — the document shape is
unchanged and `shelfwarden_version` already records which code produced the
guards. Screens are regenerable and `datasets/` is not committed, so no stored
artifact needs migrating.

This is the second time a 0.45 table has been corrected by the step that consumed
it, and the mechanism was the same both times: a table asserted against a class
nobody had yet produced an instance of. Which is the argument for §6's
cross-check test running for **every** locally-guarded class rather than only for
the one that failed.

### Decision 8 — `shelfwarden corrupt` ships a preview artifact; the dataset is 0.6's

0.6 owns `datasets/<dataset_id>/` and the truth file. 0.5 could therefore ship
library code and unit tests only. It ships one command instead, writing
`datasets/corruptions/<export_id>/`:

| File | What it holds |
|---|---|
| `corruptions.json` | every attempt: class, subject, variant, the delta, the witness, induced, collateral, verdict, `ground_truth_sha256` / `corrupted_sha256` |
| `rejected.jsonl` | every rejected attempt with its reason code |
| `corruptions.md` | the per-class deficit table a human reads |

The justification is `--census-only`'s: this is the report you need in order to
choose `composition.toml` shares from evidence rather than from hope, and at this
step it is also the only way to see which of the eleven classes your library can
actually supply. It carries **no timestamp** and no item bodies — the delta plus
the export reconstructs any corrupted item, and a stored body would duplicate
`items.jsonl` while inviting the two to drift. Byte-identity across two
`PYTHONHASHSEED` values is the artifact's gate, as it is for the export and the
screen.

---

## 4. Design

### 4.1 Modules

```
src/shelfwarden/
  pointer.py                     # NEW leaf: resolve(), select(), one grammar (D2)
  compare.py                     # + path_segments, parse_release_path, find_in_path (F6)
  evals/
    screen.py                    # GUARD_TABLE row corrected (D7)
    corrupt/
      __init__.py                # public API; re-exports the registry
      model.py                   # FieldChange, ItemChange, CorruptionResult, Rejection
      registry.py                # @corruption, CorruptionSpec, CORRUPTION_TABLE
      context.py                 # CorruptionContext, per-case RNG, synthetic id minting
      witness.py                 # DetectabilityWitness, WitnessProvider, LocalWitness
      reverse.py                 # apply_changes / apply_reverse over an item set
      collateral.py              # population-key recomputation (F4)
      movies.py  tv.py  audiobooks.py
      report.py                  # the preview artifact + deficit table
```

### 4.2 The registry

```python
@corruption(
    ProblemClass.WRONG_MATCH,
    applies_to={MediaKind.MOVIE, MediaKind.SHOW},
    variants=("donor_same_section",),
    witness=WitnessKind.VALUE,
    tier=WitnessTier.LOCAL,
)
def wrong_match(family: Family, ctx: CorruptionContext) -> CorruptionResult: ...
```

Applicability is a **separate, cheap, non-mutating** call so the generator can
count candidates without building them:

```python
def applicable(family: Family, ctx: CorruptionContext) -> Applicability
# -> Applicability.yes() | Applicability.no(reason: str)
```

Two rejection populations, kept apart for the same reason 0.45 keeps
`not_applicable` and `unavailable` apart. **`not_applicable`** means the family
was never a candidate — a supply fact about the library. **`rejected`** means a
corruption was attempted and failed one of the acceptance checks — a fact about
this step's machinery. Collapsing them turns "your library has no remake pairs"
into "the generator is broken", and only one of those is actionable.

### 4.3 Acceptance checks, in order

Every attempt runs all of these; the first failure rejects the case with a
reason code that lands in `rejected.jsonl`.

1. **`empty_delta`** — the corruption changed nothing.
2. **`noop_change`** — some `FieldChange` has `canonical_json(before) ==
   canonical_json(after)`. Raises rather than rejects: a corruption that emits a
   no-op is a bug in the corruption, not a property of the family (Finding 2).
3. **`reverse_mismatch`** — `apply_reverse(delta)` does not reproduce the
   ground-truth family byte-for-byte under `render_items`.
4. **`no_witness`** — no witness could be built at the class's tier.
5. **`witness_circular`** — a pointer resolves only against the clean family.
6. **`witness_indiscriminate`** — the witness fails its kind's rule in §3's
   Decision 3.
7. **`screen_intact`** — the class is locally guarded, and the screen still
   guards it on the corrupted family. Either the corruption did not do what it
   claims or the guard is wrong; neither case may ship. Where the class's guard
   is authority-tier, the check reports `unavailable` and is recorded as such,
   never counted as a pass.

Check 7 is the one that would have caught Finding 5 before it reached a dataset.

### 4.4 The eleven recipes

Each entry gives the applicability precondition, the mutation, the variants, and
the witness. Truth postconditions are the mechanical inverse of the delta and are
0.6's to write; they are named here only where the class's postcondition is not
simply "restore these fields".

**`wrong_match`** (movie, show). Needs a part whose path parses to a non-empty
title, and a donor root of the same kind in the same section with a different
folded title and at least one resolvable guid. Copies the donor's `title`,
`year`, `summary`, and `guids` onto the victim; paths untouched. Witness: VALUE at
`/parts/0/path`, `compare_title(parsed.title, ground_truth.title)` at the screen
policy and below it against the corrupted title. Induced: `duplicate_quality`.
Collateral: the donor. Rejected when the parsed title also matches the donor —
the case would be ambiguous rather than hard.

**`year_collision_remake`** (movie). Needs a genuine remake pair already in the
population: two roots sharing a folded title with different years, each carrying a
resolvable guid. Sets one member's `year` and its tmdb/imdb guid to the other's,
so the two collide completely. Witness: VALUE — the year parsed from the path.
Distinct from `wrong_match` because the title was right all along, which is the
whole reason the class exists. Induced: `duplicate_quality`.

**`alternate_cut`** (movie). Needs `edition_title` set and an edition marker
recoverable from a path segment (`director's cut`, `final cut`, `theatrical`,
`redux`, `ultimate`, plus the four already in `RELEASE_TAGS`: `extended`,
`unrated`, `remastered`, `imax`). Variant `strip_edition` clears `edition_title`;
variant `collide` copies a sibling cut's edition onto it. Witness: VALUE — the
marker found by `find_in_path`, which is why Finding 6's directory support is a
prerequisite rather than a nicety.

**`duplicate_quality`** (movie). Needs a movie root with at least one part and
**no** existing title/year twin, so the case stays one atomic repair. Adds a clone
root with a minted rating key (§4.5), same title/year/guids, and a different
`video_resolution` / `container` / `size_bytes` / path. Variants: `resolution`,
`container`, `bitrate`. Witness: RELATION over the two ids —
`compare_title` at the screen policy and `compare_year` EXACT, both evaluated in
the corrupted world. Truth records the id pair and which is the keeper (the
higher resolution, decided in code).

**`episode_wrong_season`** (episode). Needs a show with two exported seasons and
an episode whose parts carry `SxxEyy`. Sets `parent`, `parent_index`, and
`parent_title` together — the only form Plex can represent. Witness: VALUE — the
`(season, episode)` parsed from the path. The `index_only` variant, which moves
`parent_index` alone, is deliberately **not** implemented: it is unrepresentable
in a real library, and shipping it would let an internal-coherence check score
detections that no live server could ever require.

**`absolute_vs_seasonal`** (show family). Needs at least two seasons, at least two
episodes each, and `SxxEyy` on every episode path. Re-parents every episode under
season 1 with running numbering (S01E01..S01E*n*), then removes the seasons left
empty. One show is one case. Witness: VALUE per episode, from the paths.

**`filename_unmatchable`** (movie, episode). Needs `title` and `year`. Rewrites
each part's path to a scene release name built from the true title, and sets
`title` to that stem, and clears `guids` — the state a library is in when Plex
matched nothing and fell back to the filename. Witness: VALUE — the title and
year `parse_release_name` recovers from the name we wrote, which differs from the
corrupted title and equals the ground truth. The `opaque_hash` variant (a name
carrying no signal) is excluded with a recorded reason: it is not a hard case, it
is an unsolvable one, and the harness would be measuring nothing.

**`series_order_broken`** (audiobook). Needs an author with three or more books in
one series carrying positions. Scrambles one book's `index` and rewrites its
title/sort markers inconsistently (`Book 3` / `Part 3` / `#3` / none — the
variants). Witness: VALUE — the position recovered from a path segment. Rejected
when the path does not carry it; on many libraries this class will yield little,
and the deficit report is the honest way to say so.

**`author_name_variant`** (author family). Needs an author with two or more books.
Adds one or two author roots spelling the name differently (`Sanderson, Brandon`;
a double space) and re-parents a deterministic subset of the books to them,
adjusting `album_count`. Witness: RELATION — `compare_person_name` over the
variant pair, verified to return ALIAS (`token_set`) for the inversion and
NORMALIZED (`fold`) for the double space. Truth records the canonical author and
the full id set to merge; merging two of three leaves the library broken, so the
case is binary by nature rather than by convention.

**`multi_file_split`** (audiobook). Needs an audiobook with two or more part
children whose paths share a parent directory. Adds *n*−1 audiobook roots under
the same author titled `… CD2`, re-parents the parts, retitles the original
`… CD1`, and fixes `part_count`. Witness: RELATION — the shared parent segment
plus fold-equal title prefixes.

**`missing_series`** (audiobook). Needs `series` set and the series name present
in a path segment. Clears `series` and `series_position` and strips the series
from `title` / `title_sort`. Witness: VALUE — the series recovered from the path.

### 4.5 Minting ids for added items

An ADD needs a `rating_key` that collides with nothing in the export. Plex keys
are numeric strings, so a synthetic key is minted as
`"sw" + sha256(case_key | ordinal)[:10]` — deterministic from the case, visibly
synthetic, and legal for `ItemId` (which forbids only `:`). Verified: such a key
parses, and `item_sort_key` places it after every numeric key rather than
interleaving with them, so record order stays stable and legible.

Two rules follow. A minted key is an **address**, never an identity — `subject_key`
(0.6) climbs the external-id ladder and never touches a rating key, per invariant
9. And `CorruptionResult` carries `added_roots: tuple[ItemStub, ...]`, derived
from the added items rather than hand-built, so 0.6 rebuilds `roots.jsonl` from
the corrupted world (Finding 4) without patching stubs — a stub is a projection of
an item and deriving it is the only way the two cannot drift.

### 4.6 The witness provider seam

Mirroring 0.45's `AuthorityIndex` exactly, so that 1.1 wires a source in and
nothing else changes:

```python
class WitnessProvider(Protocol):
    @property
    def name(self) -> str: ...
    def for_value(self, subject: NormalizedItem, pointer: str,
                  ground_truth: JSONValue) -> DetectabilityWitness | None: ...
    def for_relation(self, subjects: Sequence[NormalizedItem],
                     relation: str) -> DetectabilityWitness | None: ...
```

`LocalWitness` is constructed with the **corrupted** family and cites it through
`item_evidence_id()` — the function 0.45 already ships for exactly this purpose,
because a library read is evidence too. `StoredEvidenceWitness` reads a committed,
content-addressed store and **never** touches the network; populating that store
is 1.1's job. With the store empty, the three authority classes produce zero cases
and the deficit report says which and why.

`datasets/` is gitignored except `datasets/curated/`. The change adds
`!datasets/evidence/` with a `.gitkeep` and a README now, rather than at 1.1, so
that the first captured witness body is not silently ignored by git on the day it
matters.

---

## 5. Build steps

Ordered so that each step is testable on its own and nothing is written twice.

**0.5.1 — `pointer.py`.** `resolve()`, `select()`, escaping (`~0`, `~1`), the `*`
extension, and the guard that raises on a literal `*` key. *Done when* a
round-trip test resolves every leaf of a dumped item of each media kind, and a
`FieldChange` path containing `*` raises.

**0.5.2 — `compare.py` path support.** `path_segments`, `parse_release_path`,
`find_in_path`. `parse_release_name` untouched. *Done when* the three paths in
Finding 6 resolve their title, series, and shared parent, and 0.45's existing
comparator tests still pass unchanged.

**0.5.3 — the delta and its inverse.** `FieldChange`, `ItemChange`,
`apply_changes`, `apply_reverse`, the byte-level no-op check, and the read-back
rule from Finding 1. *Done when* a hand-built delta over each media kind reverses
to byte-identical ground truth, and a change recording an un-normalized `after`
fails the test rather than the property.

**0.5.4 — registry, context, witness.** `@corruption`, `CORRUPTION_TABLE`,
`UNSYNTHESIZABLE_REASON`, per-case RNG, id minting, the three witness kinds, and
the seven acceptance checks. *Done when* a test asserts every `ProblemClass` has a
row in exactly one of the two tables, and `random.sample` appears nowhere in
`evals/corrupt/`.

**0.5.5 — the five movie/TV classes with local ingredients.** `wrong_match`,
`year_collision_remake`, `alternate_cut`, `duplicate_quality`,
`filename_unmatchable`.

**0.5.6 — the two family-shaped TV classes.** `episode_wrong_season`,
`absolute_vs_seasonal`, and the `GUARD_TABLE` correction from Decision 7 with its
regression test.

**0.5.7 — the four audiobook classes.** `series_order_broken`,
`author_name_variant`, `multi_file_split`, `missing_series`.

**0.5.8 — `shelfwarden corrupt` and the deficit report.** The artifact, the
markdown table, and the two-hash-seed byte-identity test. *Done when* the command
runs against the committed fixture export and its deficit table names, per class,
the candidate count, the attempt count, and the rejections grouped by reason.

---

## 6. Tests

Beyond the per-class trio the gate names (mutation applied, truth round-trips,
case provably detectable), these defend a specific way 0.5 could be wrong while
looking right:

- **`test_apply_reverse_restores_ground_truth`** — the property test named in
  practices §8.2, over every accepted case of every class, comparing
  `render_items` bytes rather than objects.
- **`test_a_change_records_what_was_stored_not_what_was_asked`** — a corruption
  writing NFD text, unsorted guids, and duplicated `locked_fields` records the
  normalized values (Finding 1).
- **`test_a_noop_change_raises_on_bytes_not_equality`** — `1` → `True` raises
  (Finding 2).
- **`test_selection_is_prefix_stable_in_n`** — the subjects chosen at `--limit N`
  are a prefix of those at `--limit N+1` (Finding 3).
- **`test_a_case_rng_is_independent_of_the_run`** — running one class alone
  produces the same delta as running it inside the full set.
- **`test_corrupting_a_family_breaks_the_guard_for_its_class`** — parameterized
  over every locally-guarded class; the test Finding 5 would have failed.
- **`test_a_manufactured_twin_is_declared_as_collateral`** — the `wrong_match`
  donor appears in `collateral`, and the induced class in `induced` (Finding 4).
- **`test_roots_derived_from_corrupted_items_see_the_twin_both_ways`** — the
  asymmetry in Finding 4 cannot occur when the index is derived rather than
  carried.
- **`test_a_witness_may_not_resolve_against_the_clean_family`** — a hand-built
  circular witness is rejected (Decision 3).
- **`test_an_authority_class_yields_no_cases_and_says_why`** — the three deferred
  classes report `no_witness` rather than silently producing nothing.
- **`test_a_minted_key_never_collides_with_the_export`** — over the whole fixture
  export, for every class that adds items.
- **`test_a_corruption_preview_is_byte_identical_across_hash_seeds`** — forked
  subprocesses with `PYTHONHASHSEED` 0 and 1, per practices §8.2, because a
  same-process comparison cannot see hash-order leakage at all.

---

## 7. What 0.5 does not do

- No truth file, no `case_id`, no `composition.toml`, no slice merge — 0.6.
- No `SnapshotLibrary` — 0.7. 0.5 produces deltas over an export; serving them
  through the `LibraryProvider` protocol is the next step's problem.
- No scoring — 0.8.
- No network, and no evidence capture — 1.1. The witness provider seam exists and
  its authority implementation reads a store that is empty today.
- No corrupted `items.jsonl`. The delta plus the export is the record.

---

## 8. Risks and open questions

**The offline coverage gap is the headline risk.** Three classes wait on 1.1 and
one on curation, and `missing_metadata` is among them. If `composition.toml` at
0.6 is written to the fifteen-class table it will be unfillable on day one. The
recommendation is that 0.6 normalizes shares over the *implemented* classes,
writes the resolved per-cell targets into `dataset.json` as already planned, and
prints the deferred classes as an explicit deficit — which keeps the Phase 1 gate
(20+ cases scored end to end) reachable without pretending to coverage that does
not exist.

**Eight of the eleven local witnesses come from a file path.** On a library whose
files are named `movie.mkv`, most of them reject and the dataset is thin. This is
knowable before any code is written — it is the `filename_matches_metadata` pass
rate on a real screen — which is why §1 asks for that number first. If it is low,
the honest response is to move weight toward the three path-independent classes
(`duplicate_quality`, `author_name_variant`, `multi_file_split`) and to record the
rest as blocked on 1.1 rather than on effort.

**`alternate_cut`, `series_order_broken`, and `missing_series` may yield near
zero** even on a well-named library, because each needs a fairly specific shape.
They are implemented anyway: a class that yields nothing on this library but is
correct is one export away from yielding, and the deficit report distinguishes
"no candidates" from "rejected" so the difference is visible rather than inferred.

**Open: whether the screen's `filename_matches_metadata` should adopt
`parse_release_path`.** It would raise guard coverage on libraries that name the
title only at the folder level, which is common. It is deliberately out of scope
here because it changes screen output for every existing export, and this step
already changes one row of `GUARD_TABLE`; two semantic changes to the screen in
one step is one more than can be attributed cleanly if a number moves.

---

## 9. Documents to update in the same change

- **`roadmap.md`** — 0.5's checkboxes, plus a line recording that four classes
  ship declared-and-deferred with the reason table in code.
- **`implementation-plan.md` §3** — three corrections: `CorruptionResult` is a
  family-scoped item-set delta (Decision 1); pointers are RFC 6901 and the field
  is `/parts/*/path`, not `parts[*].file` (Decision 2); `DetectabilityWitness`
  has three kinds (Decision 3).
- **`development-practices.md`** — §8.2 gains Finding 3 (`random.sample` is not
  prefix-stable in `k`; select by hash-rank); §11 gains the invariant behind
  Finding 4 (*a corruption declares its collateral, and a population index is
  derived from the world it describes, never carried over from another one*).
- **`CLAUDE.md`**, *things that look wrong but are correct* — `before`/`after`
  read back from the mutated item rather than from the caller's intent; the
  byte-level no-op check; per-case RNG rather than one stream; `*` legal in a
  selector and illegal in a change path.
- **`architecture.md` §5** — the measurement loop gains the corruption stage and
  its rejection path.
- **`screen.py`** — a note on the corrected `GUARD_TABLE` row naming 0.5 as the
  step that found it, so the next reader sees the reason rather than the edit.

---

## 10. What landed, and where it differed from this plan

Written after the step, against the working tree. Eleven classes ship, the gate
is met, and 646 tests pass offline. Six things came out differently, and four of
them are worth reading before starting 0.6.

**Two `GUARD_TABLE` rows were wrong, not one.** §2's Finding 5 predicted
`episode_wrong_season`. The cross-check found a second of the same species:
`absolute_vs_seasonal` was guarded by `episode_numbering_contiguous`, and real
absolute numbering — S01E01..S01E52 — is perfectly contiguous, so the predicate
passes on exactly the shape the class is about. Both rows now key on
`filename_matches_metadata`. The consequence is honest and visible: a show or a
season can no longer be guarded for either class, because the evidence lives in
the episode's own filename.

**The cross-check compares before against after, and has five verdicts.** The
plan specified a single check on the corrupted family. That is not falsifiable:
with a `NullAuthority` most guards are unavailable anyway, so "not guarded
afterwards" reports every case as a success. It also has to be **scoped to the
items the delta touched** — reading the whole family lets an untouched, correctly
filed sibling keep the guard alive, and a real corruption reads as having changed
nothing. One wrinkle the scoping created: a corruption that only *adds* an item
has no subject present on both sides, so the family's roots join the subject set.
Verdicts are `broken` / `intact` / `already_failing` / `unavailable` /
`unguarded`; only `intact` rejects.

**`already_failing` is a real verdict, not a rejection.** `multi_file_split`
targets a book with two files and its guard is `single_part`, so the ground truth
never satisfies the guard. That is a limit of the screen rather than a defect in
the corruption, and it is recorded rather than papered over.

**Two comparators were added that this plan did not anticipate.**
`compare_episode_number` — because a witness needs a named comparator and a
`Support`, and comparing `(season, episode)` as an identifier is the same
reasoning `compare_series_position` already records. And `find_in_path` had to
search *chunks* of a segment, not only whole segments: `Blade Runner (1982)
[Final Cut]` never equals `Final Cut`, and a substring test would return a hit
with no fold rung behind it.

**Three witnesses were built wrong first, and the machinery caught all three.**
Each was the same mistake: treating a bare non-`NONE` support as a hit, which is
precisely what `find_in_path`'s own docstring warns against. They surfaced as
`witness_indiscriminate` rejections rather than as shipped unsolvable cases,
which is the acceptance ladder doing its job. A fourth, subtler one: matching a
series position by substring makes `"1"` match `CD1`, so positions are matched as
tokens.

**Two things were deliberately not built.**

- **The stored evidence store and `StoredEvidenceWitness`.** No corruption emits
  an authority-tier witness yet, so the store would have no contents and its
  shape would be chosen with no evidence — and then copied. `WitnessTier.AUTHORITY`
  and the `tier` field exist so 1.1 adds an implementation rather than a concept.
  The `.gitignore` change waits with it. This is the same call 0.45 made about
  `VALIDATOR_POLICY`.
- **`alternate_cut`'s `collide` variant.** It needs a library with two *marked*
  cuts of one film, which no census has yet shown to exist. A variant that always
  rejects would put a permanent zero in the deficit table and read as a bug.

**One scope adjustment.** `subject_key` was specified for 0.6 and built here,
because the per-case RNG seed derives from it and seeding from a rating key would
re-corrupt the library differently after every Plex rescan. 0.6 still owns
`case_id` and the collision policy.

### What 0.6 should know

- Eleven classes can supply cases; `missing_metadata` is not among them, and it
  is the largest share in most plausible compositions. Normalize shares over the
  *implemented* classes and print the deferred ones as an explicit deficit.
- `CorruptionResult` already carries `added_roots`, `collateral`, and `induced`.
  The population index for a corrupted world must be rebuilt from them, and
  should-not-touch selection must exclude every id named in `collateral`.
- Selection ranks by `sha256(seed | subject_key)`. Keep it. `random.sample` is
  banned in the package by a test that parses the source.
