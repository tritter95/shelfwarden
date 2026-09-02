# Step 0.45 — Shared Comparators + Mechanical Screen

Implementation plan for roadmap step **0.45 Shared comparators + mechanical
screen**. Written 2026-08-29 against commit `26f359d`, with 0.1–0.4 complete and
CI green.

Every finding in §2 was produced by running CPython 3.13.12 in this checkout, not
recalled. Three of them change the design. One of them requires a change to an
artifact 0.4 already ships, and one of them moves a file the implementation plan
placed in `evals/`.

**Gate for the step** (`roadmap.md`): the screen classifies the full export, and
its output feeds both the should-not-touch slice and real-slice labeling.

---

## 1. Scope

0.4 produced a file that does not change. 0.45 is the step that decides **what can
be claimed about the items in it**.

The motivating defect is recorded as Defect 3 in `implementation-plan.md` §3:
*"this item has no problems" is an open-world claim and cannot be verified;
"this item does not have problem P" can be.* The should-not-touch slice is 15% of
the dataset and is the denominator of the headline false-positive metric. If its
members are chosen by assumption, `fp_rate_snt` is unfalsifiable — and worse, the
project starts scoring true detections as false positives, which trains it to
suppress exactly the behavior it exists to produce.

The census in 0.4 deliberately stopped one step short of this. Its `readiness`
table counts *structural candidates* and is flagged `advisory: true` on every row,
with a docstring that names 0.45 as the step that replaces it. This is that step.

Two deliverables, and the first is the larger one:

- **`compare.py`** — the comparator library. Shared by the screen (0.45), the
  detectability witness (0.5), the scorer (0.8), and the validator's *support* and
  *referent binding* checks (1.4). Four consumers, two of them on opposite sides of
  the Phase 5 MCP seam. That fact moves the file; see Decision 1.
- **`evals/screen.py`** — an LLM-free, per-predicate verification pass over an
  export, emitting `guarded_classes` / `unguarded_classes` per item.

Supporting, each forced by one of the two above:

- `models/finding.py` — `ProblemClass`, and only that. Fifteen class names are
  currently string literals in `census.READINESS_RULES` and in prose.
- `models/evidence.py` — `Source` and `evidence_id()`, and only those. A screen
  check records an `evidence_id`; the schema in `implementation-plan.md` §3 shows
  the field.
- `evals/export.py` — `roots.jsonl` (Finding 4) and a `load_items()` reader.
- `evals/census.py` — `READINESS_RULES` keyed off `ProblemClass` rather than
  strings, so the advisory names and the guarded names cannot drift apart.
- `cli.py:screen`, one new import contract, tests, doc updates.

**Not in 0.45:** any network fetch (`sources/` is 1.1), any corruption (0.5), any
truth file (0.6), `SnapshotLibrary` (0.7), the validator (1.4). The screen ships
with its authority tier **designed and unimplemented** — see Decision 3, which is
the central decision of this step.

No new dependencies. `difflib` and `unicodedata` are stdlib; `rapidfuzz` is
deliberately not adopted (Finding 1's asymmetry is a property of the algorithm we
are choosing, and a C extension would make the score a function of the wheel).

---

## 2. Verified findings

### Finding 1 — `difflib.SequenceMatcher.ratio()` is not symmetric

A comparator that does not pin its argument order returns different numbers for
the same pair of strings depending on which side is passed first. Brute-forced
over the alphabet `abc` for all lengths 1–5, with `autojunk=False` so the result
is pure algorithm and not the junk heuristic:

```
asymmetric pairs found: 9228
('ab', 'bacb')  ratio(a,b)=0.666667   ratio(b,a)=0.333333
('ab', 'baacb') ratio(a,b)=0.571429   ratio(b,a)=0.285714
```

A factor of two on the first pair. `ratio()` is `2·M/T`, and `M` — the total size
of the matching blocks — depends on which sequence gets indexed into `b2j` and on
the leftmost-longest recursion that follows.

Consequence: `compare_title(library_value, authority_value)` and
`compare_title(authority_value, library_value)` are different functions. The screen
and the validator are specified to share comparators; if one calls with the library
value first and the other with the authority value first, they share a name and not
a result — and the disagreement appears as a false-rejection metric that will not
reconcile with the screen's guard coverage.

**Design consequence.** Argument order is part of the contract: every comparator
takes `(observed, authority)` in that order, the parameter names say so, and a test
pins a known-asymmetric pair so a future refactor that swaps them fails loudly
rather than shifting every score by an unnoticed amount.

### Finding 2 — `autojunk=True` is the default and destroys long-text comparison

`SequenceMatcher`'s `autojunk` heuristic marks any element appearing in more than
1% of sequence `b` as "popular" and excludes it from matching, for sequences of
length ≥ 200. Titles are short, so this reads as harmless. Summaries are not:

```
b = "ab" * 150   (300 chars)
a = "a"  * 300

autojunk=True  (default) → 0.0033333
autojunk=False           → 0.5
```

A 150× difference, silently, at exactly the length where `missing_metadata` wants
to compare a local summary against an authority summary. The threshold is on `b`'s
length alone, so the failure switches on partway through a dataset as summaries get
longer — the worst possible shape for a bug.

**Design consequence.** `autojunk=False` is passed **explicitly** on every
construction and belongs in the CLAUDE.md "things that look wrong but are correct"
list beside `autocommit=False` and `locked=`. It is the same species: a default
that fails quietly and expensively.

### Finding 3 — `casefold()` does not preserve NFC, and paths are the entry point

The model NFC-normalizes every string that crosses into it — except `FilePart.path`,
deliberately, because a path is an argument to a future filesystem operation and
macOS hands out NFD. That exemption is documented in `models/item.py` and it is
correct. It also means NFD text reaches the comparators through exactly one door,
and casefolding does not close it:

```
'Å' (NFD: A + U+030A)  .casefold() → 'å'   is_normalized('NFC', …) = False
'İstanbul'             .casefold() → 'i̇stanbul'   (expands to i + U+0307)
'Straße'               .casefold() → 'strasse'    (ß → ss)
```

So `nfc(x).casefold() == nfc(y).casefold()` is only sound when both inputs were
already NFC. A title compared against a title is fine. A title compared against a
name parsed out of a path is not, and `filename_unmatchable` is precisely that
comparison.

The Unicode standard's canonical caseless match is `NFC(casefold(NFD(x)))`. Verified
here: normalizing *after* the fold makes all four sampled cases agree.

**Design consequence.** `fold_text()` normalizes last, not first. A test compares an
NFD path against an NFC title and asserts `EXACT`. The fixtures are currently all
NFC (checked), so that test needs an NFD fixture added on purpose.

### Finding 4 — the slice cannot answer a question about the library

Three of the screen's eleven predicates are *uniqueness* predicates: "no other item
sharing normalized (title, year)", the author-variant check, and the multi-file
sibling check. Each is an absence claim scoped to a population.

`items.jsonl` holds a **slice**. `--count 200` selects 200 roots out of a library
that may hold twenty thousand. "No other item in this file shares this title and
year" is not "no other item in the library does". An item whose duplicate simply was
not sampled would be marked guarded for `duplicate_quality`, and the agent's correct
finding on it would then be scored as a false positive.

That is not a marginal inaccuracy. It inverts the metric on the one class where the
screen looked strongest, and it does so in the direction the project has explicitly
forbidden — training itself to suppress true detections.

The population data already exists and is already paid for. `export.list_all()` walks
every supported section to exhaustion and `ItemStub` carries `item_id`, `media_kind`,
`title`, and `year` — exactly the four fields a uniqueness index needs. The export
walks them, counts them, and throws them away.

**Design consequence.** The export writes a fifth artifact, `roots.jsonl`: every root
stub in every supported section, sorted by the existing `_stub_sort_key`. `Manifest`
gains `roots_sha256` and bumps `schema_version` to 2. No additional server requests.
See Decision 2.

### Finding 5 — the comparator library is used on both sides of the MCP seam

`implementation-plan.md` §7 places the file at `evals/compare.py`. Its four consumers:

| Consumer | Module | Side of the seam |
|---|---|---|
| mechanical screen (0.45) | `evals/screen.py` | harness |
| detectability witness (0.5) | `evals/corrupt/*` | harness |
| scorer (0.8) | `evals/score.py` | harness |
| validator *support* + `bind()` (1.4) | `agent/validate.py` | **agent** |

`agent/validate.py` importing `evals/compare.py` is not caught by any current
contract — the existing rule covers `agent/tools/`, not `agent/` — but it makes the
agent depend on the package that holds the answer key, and it means the Phase 5 MCP
extraction has to carry `evals/` along with it. `evals/` also imports `library/base`
freely; a shared module underneath both is the only placement that does not create a
dependency someone will later have to break.

**Design consequence.** `shelfwarden/compare.py`, top level, a leaf beside
`canonical.py`. New contract forbids it from importing `agent`, `evals`, `library`,
or `sources`. Recorded as a correction in `implementation-plan.md` §7.

### Finding 6 — the plan's illustrative JSON contradicts the prose beside it

`implementation-plan.md` §3 lists `duplicate_quality` in `unguarded_classes` in its
example record, two paragraphs after listing *"no other item sharing normalized
(title, year)"* among the screen's predicates — which guards that class and nothing
else. The prose enumerates the predicates; the JSON is illustrative. Taking the prose
as normative, `duplicate_quality` is guarded whenever the uniqueness predicate runs
at population scope (Finding 4). Noted rather than silently resolved, because a
future reader will hit the same contradiction.

---

## 3. The design

### 3.1 `shelfwarden/compare.py`

A leaf module. Pure functions over strings and primitives, no I/O, no clock, no RNG.

```python
class SupportStrength(StrEnum):
    EXACT = "exact"
    ALIAS = "alias"          # the authority itself says these are the same work
    NORMALIZED = "normalized"  # our fold says so
    FUZZY = "fuzzy"
    NONE = "none"

STRENGTH_RANK: dict[SupportStrength, int] = {NONE: 0, FUZZY: 1, NORMALIZED: 2,
                                             ALIAS: 3, EXACT: 4}
```

`StrEnum`, not `IntEnum`, despite the ordering. These values are written into
`screen.json` and `truth.json` and read by a human choosing `composition.toml`
shares; an `IntEnum` serializes to a bare integer and makes the dataset unreadable
years later. `STRENGTH_RANK` and an `at_least()` helper carry the ordering
explicitly, which also avoids `SupportStrength.NONE == 0` being accidentally falsy.

`ALIAS` outranks `NORMALIZED` on purpose: an alias hit is an assertion by the
authority that two names denote one work, and our fold is an assertion by us.

```python
@dataclass(frozen=True, slots=True)
class Support:
    strength: SupportStrength
    rule: str                    # which fold step or alias source produced it
    score: float | None = None   # FUZZY only, rounded to 4dp
    matched: str | None = None   # which alias actually matched
```

`rule` is what makes the false-rejection metric decompose per check, which
`implementation-plan.md` requires (`rejection_reason` enum). `matched` is what makes
an `ALIAS` result auditable — an alias hit that does not name the alias is a claim
without a citation. `score` is rounded at construction: a raw float is a determinism
hazard in canonical JSON, and `allow_nan=False` already turns the pathological case
into a write-time error.

**The fold ladder**, declared as an ordered tuple of named steps so the returned
`rule` names the last step that made two strings equal:

| Step | Transform | Strength it yields |
|---|---|---|
| `identity` | none | `EXACT` |
| `fold` | NFKC → casefold → **NFC** (Finding 3) → collapse whitespace | `NORMALIZED` |
| `strip_punctuation` | drop `Unicode P*`, collapse again | `NORMALIZED` |
| `strip_articles` | leading `the/a/an/le/la/les/el/der/die/das` | `NORMALIZED` |
| `strip_diacritics` | NFKD → drop combining → NFC | `NORMALIZED` |
| `ratio` | `SequenceMatcher(None, observed, authority, autojunk=False)` | `FUZZY` |

NFKC rather than NFC in the fold — it folds `Ⅻ` → `XII`, `ﬁ` → `fi`, and fullwidth
forms, all of which are the same title. It is lossy in ways NFC is not, which is
exactly why it belongs in the comparison fold and never in `canonical_text()`. These
two functions must not be "unified"; a comment in each says so.

Verified limit: `Æ` and `œ` survive NFKD, so `Cœur` and `Coeur` fall to `FUZZY`
rather than `NORMALIZED`. A hand-maintained ligature table would rot; recording the
limit is the honest ceiling.

**The comparators**, all `(observed, authority)` per Finding 1:

```python
def compare_title(observed, authority, *, aliases=()) -> Support
def compare_year(observed, authority) -> tuple[Support, int | None]   # + year_delta
def compare_text_block(observed, authority) -> Support                # autojunk=False
def compare_person_name(observed, authority) -> Support               # "Last, First"
def compare_series_position(observed, authority) -> Support           # STRING; never int()
def id_overlap(observed, authority) -> frozenset[IdNamespace]
def parse_release_name(filename: str) -> ParsedRelease
def ratio(observed: str, authority: str) -> float
```

Two carry project-specific traps in their bodies:

- `compare_series_position` compares strings. Audnexus returns `"3.5"` for novellas
  (practices §5.2) and `int()` on it raises — or worse, `float()` on it succeeds and
  `3.5 == 3.50` starts being true while `"3.5" != "3.50"`. String comparison with a
  declared normalization (strip leading zeros, strip trailing `.0`) and no numeric
  coercion.
- `compare_person_name` folds `"Sanderson, Brandon"` and `"Brandon Sanderson"` to a
  token set and returns `ALIAS`, not `FUZZY`. This is `author_name_variant`'s entire
  premise; letting it land at `FUZZY` makes the class's own guard threshold-dependent.

**Policies.** Comparators return strength; *policies* turn strength into a decision,
and they are declared data rather than inline constants:

```python
@dataclass(frozen=True, slots=True)
class Policy:
    name: str
    minimum: SupportStrength
    fuzzy_floor: float | None

SCREEN_POLICY = Policy("screen", minimum=SupportStrength.NORMALIZED, fuzzy_floor=None)
# VALIDATOR_POLICY lands in 1.4 — deliberately absent, not stubbed.
```

The asymmetry between them is the point and must not be refactored away: **the screen
and the validator share comparators but not thresholds.** The screen breaks ties
toward `unguarded` — a missed guard costs coverage. The validator breaks ties toward
*accepting* a finding — a wrong rejection costs a true detection. Same functions,
opposite tie-breaking, which is why the policy is a separate object.

### 3.2 `evals/screen.py`

Input: an export directory. Output: `screen.json` + `screen.md` in
`datasets/screens/<export_id>/`.

**The export directory is never written to.** Its byte-identity is 0.4's gate and
adding a file to it makes that assertion answer a question nobody asked. The screen
records `source.export_id` and `source.items_sha256`, and loading a screen against an
export whose hash differs raises rather than proceeding — a guarded label carried
onto a different export is a wrong label with a plausible provenance.

**The eleven predicates**, from `implementation-plan.md` §3, split by what they need:

| Predicate | Tier | Scope | Guards |
|---|---|---|---|
| `resolvable_id_present` | local | item | `wrong_match`, `filename_unmatchable` |
| `summary_present` | local | item | `missing_metadata` |
| `single_part` | local | item | `multi_file_split` |
| `season_membership_coherent` | local | family | `episode_wrong_season` |
| `episode_numbering_contiguous` | local | family | `absolute_vs_seasonal` (weak) |
| `no_title_year_twin` | local | **population** | `duplicate_quality` |
| `no_author_name_twin` | local | **population** | `author_name_variant` |
| `filename_matches_metadata` | local | item | `filename_unmatchable` |
| `title_matches_authority` | authority | item | `wrong_match`, `foreign_title_variant` |
| `year_matches_authority` | authority | item | `year_collision_remake` |
| `series_position_matches_authority` | authority | item | `series_order_broken`, `missing_series`, `narrator_as_author` |

Each predicate returns one of four statuses, and the four-way split is load-bearing:

- `pass` — ran, and the item is clean on this predicate.
- `fail` — ran, and the item is dirty. The item leaves the should-not-touch slice and
  becomes a **real-slice candidate** carrying the failing predicate and its evidence.
- `not_applicable` — the predicate does not apply to this media kind.
- `unavailable` — the predicate applies but could not be run. In 0.45 that is
  `reason="no_authority"` for every authority-tier predicate.

`not_applicable` and `unavailable` are different facts and collapsing them is the
same mistake `FetchProfile` exists to prevent: "this item has no external ids" and
"nobody asked for its external ids" are not the same claim, and neither is "this
predicate does not apply to a movie" and "we have no TMDB record".

**Neither counts as `pass`.** That single rule is what makes Decision 3 free: with no
authority, six of the fifteen problem classes fall to `unguarded` with no conditional
logic anywhere.

**The guard table**, declared as data, `dict[ProblemClass, frozenset[Predicate]]`. A
class is guarded on an item iff its guard set is non-empty and every predicate in it
passed. `anthology_omnibus` maps to the empty set — permanently unguardable by
mechanism, matching its `readiness` row from 0.4. A test asserts every `ProblemClass`
member has an entry, so adding a sixteenth class breaks the build rather than
silently defaulting to unguarded.

**Three verdicts**, not two:

- `guarded` — ≥ `MIN_APPLICABLE_CHECKS` (3) applicable, all passed.
- `failed` — at least one applicable check failed → real-slice candidate.
- `insufficient` — fewer than 3 applicable → neither slice. Reported as a count,
  because it is a coverage metric on the screen itself.

`MIN_APPLICABLE_CHECKS = 3` is a module constant with no CLI flag. A rule expressible
as a predicate lives in code (invariant 1); a `--min-checks` flag makes the
should-not-touch slice's own admission standard a runtime argument.

Per the spec's rule, **one failing applicable check disqualifies the whole item**, not
just the classes that check guards. The per-class detail is recorded anyway — it costs
nothing and 0.6 may want it — but the verdict follows the spec.

**Population scope** (Finding 4). Uniqueness predicates read `roots.jsonl`, not
`items.jsonl`, and every uniqueness check records `scope: "population"` plus the
population size it consulted. If `roots.jsonl` is absent (an export written before
the schema bump), those predicates are `unavailable` with `reason="no_population_index"`
— never silently downgraded to slice scope.

Twin detection: exact fold key on `(section_id, fold_title(title), year)` for movies,
a dict, `O(n)`. Author names need fuzzy comparison, which is `O(n²)` unbounded, so it
blocks on a token-set key — `frozenset(fold(name).split())` collides `"Sanderson,
Brandon"` with `"Brandon Sanderson"` exactly — plus a second pass on an
`(initial, last-token)` key. Buckets are sorted before comparison. Blocking means some
pairs are never compared, which is a cap; per house rule 12, `screen.json` records the
blocking scheme, the bucket count, and the number of comparisons actually made.

**The threshold sweep.** The one place 0.45 uses a float threshold is the author-twin
fuzzy floor. Rather than hardcoding it, `screen.json` carries a small sweep — twin
counts at 0.80 / 0.85 / 0.90 / 0.95 — so the constant is arguable from evidence. This
is `auto_apply_rate(t)` as a sweep rather than a point, applied one phase early. Low
priority; drop it if the step runs long.

### 3.3 The authority tier seam

Defined in 0.45, implemented in 1.1:

```python
class AuthorityIndex(Protocol):
    def by_external_id(self, ns: IdNamespace, value: str) -> AuthorityRecord | None: ...

class NullAuthority:      # every lookup returns None
    ...
```

`AuthorityRecord` carries `evidence_id`, `field_index`, and the parsed fields the
comparators need (title, aliases, year, series position, summary). With
`NullAuthority`, every authority predicate returns `unavailable`. When 1.1 lands, a
cassette-backed or evidence-store-backed implementation is passed via
`screen --authority <dir>`, the same run promotes classes from unguarded to guarded,
and nothing in `screen.py` changes.

### 3.4 `models/finding.py` and `models/evidence.py`

Both created with the minimum the screen needs and nothing speculative, at the paths
`implementation-plan.md` §1 already assigns them — so 1.4 fills them in rather than
moving them.

- `finding.py`: `ProblemClass` (15 members). `census.READINESS_RULES` is re-keyed
  onto it, and a test asserts every member has a readiness row *and* a guard-table
  entry. Today a typo in a class name produces a `guarded_classes` entry naming a
  class no corruption ever emits, and nothing catches it.
- `evidence.py`: `Source` (`LIBRARY | TMDB | TVDB | AUDNEXUS | OPENLIBRARY`) and
  `evidence_id(source, endpoint, params, body) -> str`, per §5's
  `sha256(source|endpoint|params|body)`. A local screen check cites the export itself
  — `Source.LIBRARY`, endpoint `export`, params `{export_id, item_id}`, body the
  item's canonical JSON — which is what `implementation-plan.md` means by *"a library
  read is evidence too"*.

### 3.5 `evals/export.py` changes

- Write `roots.jsonl` — every root `ItemStub` from the population walk, one canonical
  JSON object per line, ordered by the existing `_stub_sort_key`. Written under
  `--census-only` too, where it is the *only* item-shaped artifact and is what makes
  that mode useful to the screen.
- `Manifest` gains `roots_sha256`; `schema_version` 1 → 2.
- Add `load_items(directory) -> tuple[NormalizedItem, ...]` and
  `load_roots(directory)`, beside the existing `load_manifest` / `load_census`. The
  screen is the first reader and there is no reader today.
- `VOLATILE_MANIFEST_FIELDS` is unchanged: `roots.jsonl` is deterministic by the same
  rules as `items.jsonl` and belongs inside the byte-identity assertion, not outside it.

### 3.6 The new import contract

```toml
[[tool.importlinter.contracts]]
name = "the comparator library is a leaf"
type = "forbidden"
source_modules = ["shelfwarden.compare"]
forbidden_modules = [
    "shelfwarden.agent",
    "shelfwarden.evals",
    "shelfwarden.library",
    "shelfwarden.sources",
]
```

Same shape as the four existing internal contracts, and the reason it is written now
rather than in 1.4 is that `agent/validate.py` is the import that would break it and
it does not exist yet — the contract is honest today and breaks CI at exactly the step
that would violate it, which is how the other five behave.

### 3.7 The CLI

```
shelfwarden screen <export-dir> [--out DIR] [--authority DIR]
```

Exit `OK` or `ERROR`; the screen finds nothing in the agent sense, so `FINDINGS` (3)
does not apply. Screening a `--census-only` export is a **correctable** error naming
its next action — *"this export holds no items; re-run `shelfwarden export` without
`--census-only`"* — per practices §5.4, which applies to the CLI for the same reason it
applies to tools. `screen.md` renders from `screen.json` alone, like `census.md`, so a
stored screen stays readable.

---

## 4. Decisions

### Decision 1 — `compare.py` is a top-level leaf, not `evals/compare.py` (**recommended**)

Contradicts `implementation-plan.md` §7's file column. Finding 5 is the reason: four
consumers, one of them `agent/validate.py`. The alternative — leave it in `evals/` and
let the agent import the harness — creates a dependency the Phase 5 MCP extraction has
to break, and puts the answer-key package one import away from the tool layer that a
current contract already works to keep it away from.

Rejected alternative: duplicate the comparators, one copy per side. That is the version
where the false-rejection rate and the guard coverage stop being comparable numbers,
which is the whole reason the plan said "the exact comparators the scorer uses".

Correction recorded in `implementation-plan.md` §7.

### Decision 2 — the export writes `roots.jsonl` (**recommended**)

Finding 4. Uniqueness predicates need population scope, the population walk already
happens, and `ItemStub` already carries the four fields required. Cost: one file, one
manifest field, one schema bump, zero additional requests.

The alternative is Decision 2b: scope uniqueness to the slice and mark those predicates
`unavailable` whenever `selection.mode != "all"`. Honest, zero export change, and it
permanently gives up guarding `duplicate_quality` and `author_name_variant` on any
sampled export — which is every export anyone will actually run. Keep it as the
fallback if `roots.jsonl` turns out to be expensive on a very large library.

This is the same species of change as 0.4's own `provider_info()` and `FilePart` ids: a
later step discovering that an earlier artifact is one field short of usable.

### Decision 3 — ship the local tier complete, the authority tier as a protocol (**recommended**)

The central decision. Six of the eleven predicates need an external authority, and
`sources/` is step **1.1** — a phase later. The options:

1. **Reorder**: pull `sources/` forward into Phase 0. Rejected: it drags the shared
   HTTP client, four adapters, throttling, cassettes, and the evidence store across the
   phase gate, and the gate exists precisely to stop that.
2. **Block**: leave 0.45 unstarted until 1.1. Rejected: 0.5 and 0.6 both depend on the
   comparator library, so blocking 0.45 blocks the Phase 0 gate on a Phase 1 step.
3. **Ship the local tier, define the authority seam.** Recommended.

Option 3 is not a compromise, because the design already has the vocabulary for it.
`unguarded_classes` exists exactly to say *"we did not verify this"*, and a finding in
an unguarded class scores `unverified` — counted and reported, never pass or fail. A
thin guard set produces a smaller should-not-touch slice and more `unverified`
findings. Both are visible, neither is a wrong number.

What the local tier guards, honestly: `duplicate_quality`, `missing_metadata`,
`multi_file_split`, `author_name_variant`, `filename_unmatchable`, and — weakly, on
structure alone — `episode_wrong_season`. Six of fifteen. The other nine wait for 1.1.

The obligation this creates: **guard coverage per class is a published number in
`screen.json`**, not a footnote. Otherwise `fp_rate_snt` gets read at the Phase 1 gate
as if it covered fifteen classes when it covers six.

### Decision 4 — `unavailable` and `not_applicable` are distinct, and neither is `pass`

Stated as a decision because the two-status version is genuinely simpler and genuinely
wrong. Collapsing them makes "we have no TMDB record" indistinguishable from "movies
have no season", and the screen's own coverage becomes unmeasurable — the same defect
`FetchProfile` was added to 0.2 to prevent, one layer up. It is also what makes
Decision 3 require no conditional logic.

### Decision 5 — `MIN_APPLICABLE_CHECKS` is a constant, not a flag

Invariant 1. A flag makes the should-not-touch slice's admission standard a runtime
argument, and the first time the slice comes out small, someone lowers it. The value is
recorded in `screen.json` so a stored screen states its own standard.

### Decision 6 — the screen writes outside the export directory

`datasets/screens/<export_id>/`, bound to its source by `items_sha256`. Keeps 0.4's
byte-identity gate answering the question it was written to answer, and makes a screen
carried onto the wrong export an error rather than a plausible-looking mislabel.

### Decision 7 — `StrEnum` for `SupportStrength`, with an explicit rank table

An `IntEnum` gives free ordering and writes integers into every dataset this project
produces. The values are read by a human choosing `composition.toml` shares. Rank lives
in `STRENGTH_RANK`.

---

## 5. Tasks

| # | Task | Files | Done when |
|---|---|---|---|
| 1 | `ProblemClass` | `models/finding.py` | 15 members; `census.READINESS_RULES` re-keyed; a test asserts every member has a readiness row |
| 2 | `Source` + `evidence_id` | `models/evidence.py` | Derivation matches §5's `sha256(source\|endpoint\|params\|body)`; ids are stable across processes |
| 3 | Fold ladder | `compare.py` | Each step named in `Support.rule`; NFC applied *after* casefold (Finding 3); NFKC vs `canonical_text` divergence commented in both files |
| 4 | Comparators | `compare.py` | `(observed, authority)` order pinned by a test on a known-asymmetric pair (Finding 1); `autojunk=False` explicit (Finding 2); series position never coerced to a number |
| 5 | `Policy` + `SCREEN_POLICY` | `compare.py` | Declared data; `VALIDATOR_POLICY` deliberately absent |
| 6 | Import contract | `pyproject.toml` | `compare` forbidden from `agent`/`evals`/`library`/`sources`; `lint-imports` green |
| 7 | `roots.jsonl` | `evals/export.py` | Written in both modes; `roots_sha256` in the manifest; `schema_version` = 2; byte-identity suite still passes |
| 8 | Export readers | `evals/export.py` | `load_items` / `load_roots` beside the existing loaders |
| 9 | Local predicates | `evals/screen.py` | Eight predicates, four statuses, family- and population-scoped variants |
| 10 | Population index + blocking | `evals/screen.py` | Twin detection at population scope; blocking scheme, buckets, and comparison count all recorded |
| 11 | Authority seam | `evals/screen.py` | `AuthorityIndex` protocol + `NullAuthority`; every authority predicate `unavailable` with a reason |
| 12 | Guard table + verdicts | `evals/screen.py` | `guarded`/`failed`/`insufficient`; a test asserts every `ProblemClass` has a guard entry |
| 13 | Writer + binding | `evals/screen.py` | Atomic write; `items_sha256` binding enforced on load; every aggregation explicitly ordered |
| 14 | `screen.md` renderer | `evals/screen.py` | Renders from `screen.json` alone; guard coverage per class is a table, not a footnote |
| 15 | CLI | `cli.py`, `tests/test_cli.py` | `screen` command; census-only export gives a correctable error naming its next action |
| 16 | NFD fixture | `tests/fixtures/plex/` | A movie whose `file` is NFD and whose `title` is NFC (none exists today — checked) |
| 17 | Doc updates | `implementation-plan.md`, `development-practices.md`, `roadmap.md`, `architecture.md`, `CLAUDE.md` | Findings 1–6 recorded; `autojunk=False` added to the "looks wrong, is correct" list; roadmap 0.45 links here and is checked off |

Task 17 is the same obligation 0.1–0.4 discharged. Findings 5 and 6 contradict sentences
in `implementation-plan.md`; leaving both standing sends the next reader to re-derive them.

---

## 6. Test plan

The gate is one assertion. Everything else supports it.

```python
def test_screen_classifies_every_exported_item_into_exactly_one_verdict(): ...
```

Supporting, in rough order of how much they would hurt to lack:

```python
def test_screen_is_byte_identical_across_hash_seeds()        # subprocess, PYTHONHASHSEED 0 vs 1
def test_twin_detection_uses_population_scope_not_the_slice()  # Finding 4 — the correctness test
def test_uniqueness_is_unavailable_when_roots_jsonl_is_absent()
def test_authority_predicates_are_unavailable_not_failed()   # Decision 3
def test_unavailable_never_counts_toward_guarded()           # Decision 4
def test_not_applicable_and_unavailable_are_distinguishable()
def test_a_failing_predicate_yields_a_real_slice_candidate_with_its_evidence()
def test_fewer_than_three_applicable_checks_is_insufficient()
def test_every_problem_class_has_a_guard_table_entry()
def test_every_problem_class_has_a_readiness_row()
def test_fuzzy_ratio_argument_order_is_pinned()              # Finding 1
def test_long_text_comparison_passes_autojunk_false()        # Finding 2
def test_an_nfd_path_matches_an_nfc_title()                  # Finding 3
def test_series_position_is_compared_as_a_string()           # "3.5" never int()
def test_person_name_inversion_is_alias_not_fuzzy()
def test_screen_refuses_an_export_whose_items_sha256_differs()
def test_screen_reports_blocking_scheme_and_comparison_count()  # no silent caps
def test_guard_coverage_per_class_is_reported()              # Decision 3's obligation
def test_export_is_still_byte_identical_with_roots_jsonl()   # 0.4's gate, re-run
def test_compare_imports_no_package_module()                 # belt to the contract's braces
```

Two deserve a note.

**The hash-seed test.** Same argument as 0.4's: `pytest` runs in one process with one
hash seed, so "screen twice and compare" passes against code whose bucket order is
hash-seed dependent while two developers' screens differ. The screen builds more sets
than the census does — blocking buckets, guarded-class sets, twin groups — so it is
*more* exposed, not less. Fork with an explicit `PYTHONHASHSEED` on each side.

**The population-scope test.** Construct a fixture export where a movie's duplicate
exists in `roots.jsonl` but was not selected into `items.jsonl`, and assert the item is
`failed`, not `guarded`. Under slice scope it comes back guarded, which is the bug
Finding 4 describes. This is the single test that would have caught it.

### Exit checklist

```bash
uv sync --locked --group dev
uv run ruff check . && uv run ruff format --check .
uv run lint-imports          # now with the compare-is-a-leaf contract
uv run pytest -q
```

- [x] All four pass locally and in CI, with no network access from any test
- [x] Two screens of the same export are byte-identical, including across two `PYTHONHASHSEED` values
- [x] The export's own byte-identity gate still passes with `roots.jsonl` added
- [x] A duplicate outside the slice prevents a `guarded` verdict
- [x] Every authority predicate reports `unavailable`, never `pass` and never `fail`
- [x] `screen.md` states guard coverage per class in a table
- [x] Findings 1–6 are recorded in the documents they contradict
- [x] `roadmap.md` 0.45 is `[x]` and 0.5 can begin

### The live sanity check (not CI)

```bash
uv run shelfwarden export --count 200 --out /tmp/e1
uv run shelfwarden screen /tmp/e1 --out /tmp/s1
# read /tmp/s1/screen.md: how many items are guarded, and for which classes?
```

The number to look at is not the guarded count — it is the **`insufficient` count**. A
large one means the local tier does not have three applicable predicates for most of the
library, which is information about `composition.toml` and about how much of the
should-not-touch slice is really blocked on 1.1.

---

## 7. What this hands to later steps

| Step | Inherits |
|---|---|
| 0.5 | The comparator library the detectability witness runs — `witness ≠ corrupted` then `witness == ground_truth` are two `compare_*` calls. `ProblemClass` as an enum rather than fifteen string literals. |
| 0.6 | `guarded_classes` / `unguarded_classes` per item, which is what `verification` in the truth schema is. `failed` items as the pre-populated real-slice candidate queue. `evidence_id()` for the `checks[]` array. |
| 0.8 | The same comparators, so postcondition evaluation and the screen cannot disagree about what "the same title" means. Guard coverage per class, so `fp_rate_snt` can state its own denominator. |
| 0.9 | Real-slice candidates already carrying the failing predicate, its evidence, and a proposed ground truth — the ~60s-per-case adjudication form, pre-filled. |
| 1.1 | `AuthorityIndex`, with the six authority predicates already written against it. Wiring it up is a constructor argument, not a redesign. |
| 1.4 | `compare.py` as a leaf the agent may import without dragging `evals/` across the MCP seam. `SupportStrength`, `Support.rule` for `rejection_reason`, and `Policy` as the place `VALIDATOR_POLICY` goes. |

---

## 8. Risks and open questions

- **Guard coverage is six of fifteen classes until 1.1** (Decision 3). The should-not-touch
  slice will be smaller and more findings will score `unverified` than the spec's
  composition assumes. That is visible and correct, but `composition.toml` in 0.6 is
  written against it — so if 1.1 slips, the 15% should-not-touch share may need to shrink
  rather than be filled with weakly-guarded items.
- **Blocking can miss an author pair.** Two variants sharing no token and no initial —
  a transliteration difference, say — never get compared. The comparison count and
  scheme are reported, which makes the gap measurable rather than invisible; it does not
  close it. Raising it to an all-pairs comparison is `O(n²)` on the whole population and
  should be measured before being dismissed.
- **`ratio()` is a CPython implementation detail.** Finding 1 pins argument order and
  Finding 2 pins `autojunk`, but the algorithm itself is not a specified interface. A
  CPython change would move every `FUZZY` score. Mitigation: `FUZZY` gates nothing in
  0.45 except author twins, and the threshold sweep makes a shift visible. If fuzzy
  scores ever gate scoring, they need pinning to a vendored implementation — worth
  deciding *before* 0.8, not after.
- **`roots.jsonl` grows with the library, not the slice.** Same trade-off already
  accepted for the population census. If it becomes a problem the answer is a sampled
  population index that states its coverage — never silently reverting uniqueness to
  slice scope, which is Finding 4 arriving again with a plausible excuse.
- **`strip_articles` is anglocentric.** The listed prefixes cover English, French,
  Spanish, and German. A Japanese or Korean title has no article to strip and is
  unaffected; a Swedish or Polish one may be. This is a `NORMALIZED`-tier loosening, so
  the failure mode is a missed guard rather than a wrong one, but the article list should
  be revisited against the real census once a live export exists.
- **The screen has no notion of `known_other_problems` yet.** An item that fails one
  predicate is disqualified whole (§3.2), even when the failure is a known, accepted
  library quirk. 0.6 introduces the field; if the `failed` count comes back dominated by
  one predicate, that is the signal to wire it back into the screen rather than to
  loosen the predicate.

---

## 9. What shipped, and where it differs from this plan

Written after the step landed. Every task in §5 shipped, including the threshold
sweep §3.2 marked droppable. Ten deviations, each small enough to have been a
judgement call while building and large enough that a later reader would
otherwise have to re-derive it.

**1. The local tier guards seven classes, not six.** §3.2's prose count omits
`absolute_vs_seasonal`, which `episode_numbering_contiguous` guards weakly — and
that predicate is local tier. The number is computed from `GUARD_TABLE` and
published per class in `screen.json`, so no prose count is load-bearing. Recorded
as correction 3 in `implementation-plan.md` §7.

**2. `screen.json` carries no timestamp.** A screen is a pure function of the
export and the code that read it. A `generated_at` field would force the same
volatile-field exception list the manifest needs, to buy information the export
id and `shelfwarden_version` already carry — and it would make byte-identity
untestable without that list. 0.6's `verification.verified_at` is stamped by the
truth generator, which is the step that actually has a clock.

**3. `render_screen` passes `exclude_none=True`.** Nulls were roughly a quarter
of the file: a check that ran no comparator carries five of them, eleven times per
item. Every optional field re-parses to `None` when absent, and no reader
distinguishes absent from null here, so this is a size decision rather than a
semantic one. It is the one place in the project that treats the two as the same
thing, and the docstring says so.

**4. No `--authority` CLI flag.** §3.7 sketched one, but nothing can implement it
until 1.1, and a flag whose every invocation is an error is dead surface
advertising a capability that does not exist. The seam is a `run_screen(...,
authority=...)` argument, already exercised by a stub implementation in the tests.
1.1 adds the flag alongside the thing it points at.

**5. `export._write_atomically` became `export.write_atomically`.** The screen
needs the same temp-dir-then-`os.replace` guarantee for the same reason, and a
second copy of it would be a second thing to get wrong.

**6. Predicate applicability is declared per media kind** (`PREDICATE_KINDS`),
which decides the `insufficient` count more than anything else does. Applicable
local checks by kind: movie 4, show 5, episode 4, audiobook 3, season 2, author 1,
audiobook_part 0. So seasons, authors, and audiobook parts are structurally
`insufficient` until the authority tier lands — which is the honest answer, and
the reason `insufficient` is reported as a headline number rather than a remainder.

**7. `episode_numbering_contiguous` has a declared rule**: no duplicate indices,
contiguous from the lowest, and the lowest is 0 or 1. A season whose episodes
start at 13 is exactly the `absolute_vs_seasonal` signal, so `offset_start` is a
failure rather than a tolerated shape.

**8. `single_part` on a book with no parts is `unavailable`, not `fail`.** Zero
parts is not evidence of a split; it is evidence that nothing was recorded.

**9. `filename_matches_metadata` requires *every* part to match**, not any. A part
whose filename does not name its item is worth a human look, and the cost of being
wrong is adjudication time rather than a wrong label.

**10. `Blocking` reports `pairs_resolved` / `pairs_skipped`** rather than
"comparisons made". The exact-key uniqueness predicate makes no pairwise
comparisons at all yet decides every pair by grouping, and one field name has to
be honest for both schemes.

Not run: the live sanity check in §6, which needs a configured Plex server. The
`insufficient` count it exists to surface is asserted against the fixture library
instead, and the first real export should still be read for it.