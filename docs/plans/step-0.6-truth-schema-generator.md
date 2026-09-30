# Step 0.6 — Truth Schema + Generator

Implementation plan for roadmap step **0.6 Truth schema + generator**. Written
2026-09-01 against commit `4b05c74`, with 0.1–0.5 complete and CI green.

Every finding in §2 was produced by running CPython 3.13.12 in this checkout, not
recalled. Two of them are defects in code that shipped in 0.5, one changes the
shape of the dataset, and one settles a question the implementation plan left
open since it was written.

**Revised 2026-09-08**, still against `4b05c74`. All six findings in §2 were
re-measured and hold. The revision is to §3 and §4, where four decisions were
wrong or incomplete: the outcome shape was keyed on the delta rather than on the
witness (Decision 2), the subject cap broke the prefix stability it was written to
protect (Decision 9), the should-not-touch slice had no cell to be selected into
and no legal `case_id` (Decision 10), and the screen change was described as an
intersection when what it needs is a third bucket (Decision 6). Findings 1, 3 and
5 carry the measurements that forced those.

**Gate for the step** (`roadmap.md`): `generate --count 200 --seed N` is
reproducible and never silently unbalances the dataset.

---

## 1. Scope

0.5 produced corruptions that are individually provable. 0.6 is the step that
turns them into a **dataset**: a labelled, balanced, regenerable set of cases with
an expectation attached to each, and a truth file the scorer can read without the
generator present.

The distinction that matters is that 0.5's output is a *survey* — every applicable
class against every applicable family, no slices, no balance, no identity that
survives a re-export. A dataset is the opposite of a survey: it is a deliberate
selection with a published composition, and its cases must keep their identity
when the library beneath them is re-exported, or the spec's relative CI gate ("no
case that passed may now fail") is decorative.

Three defects recorded in `implementation-plan.md` §3 are this step's substance,
and all three are about the same thing — an expectation that can be satisfied
without doing the work:

- **Defect 1**, the false-positive blind spot: `unexpected: fail` becomes the
  default on every case, not just should-not-touch.
- **Defect 2**, escalation satisfiable by silence: the `escalate` expectation
  becomes threshold-free and demands positive behavior.
- **Defect 3**, should-not-touch resting on an open-world claim: `no_action` is
  narrowed to the screen's `guarded_classes`, and findings elsewhere are scored
  `unverified` rather than as failures.

Deliverables:

- **`evals/truth.py`** — the `TruthFile` schema and its expectations, with the
  postcondition vocabulary and its mechanical derivation from a 0.5 delta.
- **`evals/generate.py`** — selection, slicing, composition resolution, the
  curated-slice merge, and the deficit report. Runnable as
  `python -m shelfwarden.evals.generate`, which is what the Phase 0 gate names.
- **`composition.toml`** at the repo root, and the resolver that turns shares into
  absolute per-cell targets.
- **`datasets/curated/`** — the real and ambiguous slice files, empty at this step
  and filled by 0.9.

Supporting, each forced by one of the above:

- `evals/corrupt/collateral.py` — the symmetry fix (Finding 1).
- `models/finding.py` — `CLASS_KINDS`, so a class cannot be credited as guarded on
  a media kind it can never apply to (Finding 5).
- `evals/screen.py` — a third guard bucket, `trivially_guarded_classes`, and the
  `SCHEMA_VERSION` bump the new field forces.
- `pointer.py` — `matches(selector, pointer)`, so a tier table written in
  selectors can be applied to a concrete change path.
- `cli.py`, tests, doc updates.

**Not in 0.6:** `SnapshotLibrary` (0.7) — but §4.7 fixes the interface it will
consume. No scorer (0.8), no labelling workflow (0.9), no network. No new
dependency: `tomllib` is stdlib and `composition.toml` is read-only at runtime.

---

## 2. Verified findings

### Finding 1 — `collateral` is one-directional, and therefore incomplete

0.5 records, for every case, the ids outside its family whose population-scoped
guard it moved. Re-screening the whole corrupted world and diffing every item's
verdict against the clean world shows four cases whose screen impact lands outside
`family ∪ collateral`:

```
wrong_match          fake:1:108   leaked: ['fake:1:107']
wrong_match          fake:1:107   leaked: ['fake:1:108']
filename_unmatchable fake:1:108   leaked: ['fake:1:107']
filename_unmatchable fake:1:107   leaked: ['fake:1:108']
```

`fake:1:107` and `fake:1:108` are the two Blade Runner entries — a genuine
title/year twin pair, both **failing** `no_title_year_twin` in the clean world.
Corrupting one gives it a different title, so the other stops having a twin and
its verdict *improves* from `failed` to `guarded`.

`collateral_ids` looks for population members that now share the corrupted item's
key. It never looks for members that shared the item's **old** key and no longer
do. So it detects a guard newly broken and misses a guard newly granted.

A newly granted guard is the more insidious of the two: it is a claim that is true
only inside one case's world. Decision 1 closes the exposure that motivated this
finding — no should-not-touch case is ever screened inside a case world, so no item
is admitted to that slice carrying a guard the real library contradicts — but the
field survives the decision and is still read twice: by the scorer, to decide which
of a repair case's findings on other items are excused, and by 0.7, which rebuilds
a case world's population index from it. And 0.5's own handoff ("should-not-touch
selection must exclude every id named in `collateral`") and
`development-practices.md` §11.13 both describe it as complete. A field documented
as complete and known to be half a field is the thing to fix.

**Design consequence.** `collateral_ids` gains a symmetric second pass over the
pre-corruption keys, and its docstring stops saying "whose guard this corruption
moved" as though movement had one direction. The completeness property is then
asserted directly, by the diff above: for every case, the set of items whose screen
verdict changes is a subset of `family ∪ collateral`.

The second pass **over-reports**, deliberately. An outside item that shared the old
key and still has another twin has no verdict change, and deciding that exactly
means deriving the population index twice per case. The asserted property is a
subset, so an over-approximation satisfies it; what it costs is a little scorer
leniency — a finding on an over-reported id in that case's world is excused rather
than counted — and never the direction the project has forbidden.

### Finding 2 — a genuine duplicate pair shares a `subject_key`

The subject ladder's first rung is the first resolvable guid. Two library entries
that are duplicates of one work carry the same guid, and therefore the same
subject:

```
subject_key(item) == subject_key(its duplicate)   ->  True
```

Across the fixture export no collision occurs — 30 cases produce 30 distinct
`case_id`s — because no two families there share a guid. But a library with real
duplicates is not a hypothetical; it is the condition `duplicate_quality` exists
to describe, and any such library makes two families collide.

`case_id = sha256(slice, problem_class, media_kind, subject_key,
corruption_variant)`, so two colliding subjects selected for the same class and
variant produce **one id for two cases**. The baseline would then track one of
them arbitrarily, and the CI diff would report a phantom `changed` every time
selection order shifted.

**Design consequence.** A `case_id` collision is an **error**, never repaired by
appending an ordinal — a disambiguating suffix is positional identity smuggled
back in, which is exactly what invariant 9 forbids. Subject uniqueness becomes a
**selection precondition**: the generator indexes subjects across the export
first, and a subject that is not unique in the population is not eligible to be
one, with the exclusion counted in the deficit report rather than silently
applied.

### Finding 3 — the outcome shape follows the witness, not the delta

The implementation plan states that generating postconditions from the inverse of
`corruption.changes` "covers roughly 12 of the 15 classes with no simulator at
all". Measured against the deltas 0.5 actually produces, with each class's witness
kind beside it:

```
wrong_match            {'modify': 1}                value
year_collision_remake  {'modify': 1}                value
alternate_cut          {'modify': 1}                value
episode_wrong_season   {'modify': 1}                value
filename_unmatchable   {'modify': 1}                value
series_order_broken    {'modify': 1}                value
missing_series         {'modify': 1}                value
absolute_vs_seasonal   {'remove': 1, 'modify': 2}   value
duplicate_quality      {'add': 1}                   relation   same_work
author_name_variant    {'add': 1, 'modify': 2}      relation   same_author
multi_file_split       {'add': 1, 'modify': 2}      relation   same_book
```

`duplicate_quality`'s delta is a **pure ADD**. There is no field change to invert,
so no postcondition can be derived at all — and inventing one ("the clone's title
must equal…") would describe an item the repair is supposed to make disappear.

The other three deltas that do more than modify a field look like one case and are
not. Splitting on change kind is wrong in both directions, and each direction is a
measurement:

**`absolute_vs_seasonal` has no id set and no keeper.** Its witness is
`kind=value, relation=None, subjects=('fake:2:2221',)`. What its delta does is
renumber an episode — `/index 1→3`, `/parent_index 2→1` — and empty a season. Both
numbers are hard fields with an invertible postcondition, and the emptied season is
a container Plex re-creates on rescan. Grouped with the ADD classes it would be
scored on a shape its witness cannot produce.

**The relation classes still rewrite fields on items that survive the repair.**
`multi_file_split` renames the *pre-existing* book:

```
modify fake:3:411  /part_count 2 → 1  /title 'The Way of Kings' → 'The Way of Kings CD1'
modify fake:3:422  /index 2 → 1       /parent 411 → swea3728f407
```

Scored on a `resolution` alone, an agent that merges the two entries and leaves the
title reading `CD1` passes.

**Design consequence.** The shape is keyed on `witness.kind`, and the two shapes
are **additive rather than exclusive**:

- every MODIFY on an item that exists in the ground truth yields a `postcondition`
  — the eight `kind=value` classes, and two of the three `kind=relation` ones;
- a `kind=relation` witness additionally yields a `resolution`, built from the
  `relation` and `subjects` the witness already carries — three classes;
- an ADD yields neither (it names the item the repair removes), and a REMOVE is a
  soft `absent → present` report (§4.4).

Ten of the eleven implementable classes therefore carry a postcondition, three
carry a resolution, and `duplicate_quality` is the only class with a resolution and
no postcondition. Collapsing the two shapes into one would mean either scoring
three classes on a predicate that cannot express their repair, or leaving a
surviving item's mangled title ungated.

### Finding 4 — `must_not_change` derives from the witness, minus the delta

`must_not_change` is what stops "rewrite the title and let Plex rescan" scoring
equal to a clean rematch. The implementation plan declares one by hand
(`parts[*].file` on `wrong_match`) and does not say where the rest come from.

There is a principled answer: **a repair must not destroy the evidence that made
the case solvable.** Comparing each case's witness pointers against the paths its
delta touched:

```
wrong_match            witness=['/parts/0/path']  overlap with delta: none
year_collision_remake  witness=['/parts/0/path']  overlap with delta: none
episode_wrong_season   witness=['/parts/0/path']  overlap with delta: none
absolute_vs_seasonal   witness=['/parts/0/path']  overlap with delta: none
duplicate_quality      witness=['/title']         overlap with delta: none
author_name_variant    witness=['/title']         overlap with delta: none
filename_unmatchable   witness=['/parts/0/path']  overlap with delta: ['/parts/0/path']
multi_file_split       witness=['/title']         overlap with delta: ['/title']
```

Six of eight are clean, and the two exceptions are the point. For
`filename_unmatchable` the witness *is* the corrupted path — the scene name the
corruption wrote — and renaming that file is precisely the repair. Making the
witness untouchable would forbid the correct answer.

**Design consequence.** `must_not_change` = witness pointers **minus** the paths
the delta touched, unioned with a small per-class declared floor (invariant 11:
never delete a file, so `/parts/*/path` is a floor on every class that does not
rename). Derived, checked, and with the one exception that makes the rule correct
rather than merely plausible.

### Finding 5 — the guard table credits classes to kinds they cannot apply to

Screening the clean fixture export and counting, per class, how many items are
recorded as guarded:

```
absolute_vs_seasonal : 11      <- includes every movie
duplicate_quality    :  8
missing_metadata     :  8
filename_unmatchable :  6
episode_wrong_season :  3
```

`absolute_vs_seasonal` is a TV class. It is credited on movies because 0.5
re-pointed its guard at `filename_matches_metadata`, whose applicable kinds are
`{MOVIE, EPISODE}` — so a movie with a well-named file passes the guard for a
class that can never describe it.

The consequence is small but in a load-bearing number: `guard_coverage.in_scope`
is published so `fp_rate_snt` can state its denominator, and it is now inflated
for that class. The scoring behavior it implies is accidentally correct — a
finding of `absolute_vs_seasonal` on a movie *is* a false positive — but it is
correct for the wrong reason, and the reason is what 0.6's `no_action` expectation
has to be built on.

**Design consequence.** `CLASS_KINDS` in `models/finding.py`: which media kinds
each problem class can describe. It is **not** applied as an intersection. Removing
`absolute_vs_seasonal` from a movie's `guarded_classes` moves it into
`unguarded_classes`, which the scorer reads as *nobody checked* and scores
`unverified` — a weaker claim than the one available, and the wrong direction: a
finding of absolute numbering on a movie is a false positive, not an open question.
The screen gains a **third bucket** instead, and `no_action` reads all three
(Decision 6). That is what makes "an agent claiming absolute numbering on a movie
is a false positive" a statement with a reason behind it.

### Finding 6 — supply is bounded by families × classes, not by `--count`

The fixture export holds 11 families and yields 30 candidate cases over **10
distinct subjects**, with up to 4 cases falling on a single family:

```
families: 11    cases: 30    subjects covered: 10
cases per family: {'fake:1:103': 4, 'fake:2:201': 4, 'fake:1:102': 4, ...}
```

Two facts follow. A dataset of *N* cases does not imply *N* items examined — the
same film can be the subject of a `wrong_match` case and a `filename_unmatchable`
case, and an agent that mishandles that film fails twice. And `--count 200`
against a small export cannot be satisfied by a class whose applicable families
have run out, whatever the composition says.

**Design consequence.** `subjects_covered` is published beside `cases` in
`dataset.json`, so concentration is visible rather than inferred; a declared cap
limits cases per subject; and an unfillable cell is a **deficit row**, never a
silent re-draw from a class that still has supply. A dataset that quietly
rebalances is one that reports coverage it does not have.

---

## 3. Decisions

### Decision 1 — the dataset is the clean export plus per-case deltas

The implementation plan's example truth file names `item_ids: ["snap:1:41823"]`
and a `source_export`, which reads as one corrupted world holding every injected
corruption. Finding 1 is the argument against it: corruptions interact through the
two population-scoped predicates, so in a shared world one case's donor is another
case's subject, and the should-not-touch slice has to be filtered against the
union of every case's collateral.

So the dataset stores the **clean export plus one delta per case**, and a world is
composed per case:

```
datasets/evals/<dataset_id>/
  dataset.json      # composition targets, deficits, counts, lineage, provenance
  truth.json        # the cases, with expectations and ground truth
  deltas.jsonl      # one ItemChange set per case_id
  rejected.jsonl    # attempts that did not ship, with reasons
  report.md         # the table a human reads
```

`datasets/evals/<id>/` rather than the `datasets/<id>/` the implementation plan
names: `exports/`, `screens/`, `corruptions/` and `curated/` already live one level
down, and a bare dataset id beside them reads as a fifth artifact *type* — with
`curated/` in particular reading as a dataset. The deviation is recorded in §9.

Four consequences, and the third is the one that pays for the decision:

1. **No cross-case interference.** A case's world holds exactly one corruption, so
   its collateral is scoped to itself.
2. **The dataset is small.** Deltas, not *N* copies of a library.
3. **A `no_action` case's world is the clean export.** Its guard claims are
   *exactly* the clean screen's, with no collateral filtering at all — Finding 1's
   problem disappears rather than being managed. This **overrides 0.5's handoff**
   that "should-not-touch selection must exclude every id named in `collateral`",
   and the same instruction in `development-practices.md` §11.13: with per-case
   worlds there is no shared world for a collateral id to be wrong in. Finding 1's
   fix is still required for the other two readers of the field — the scorer and
   0.7 — but the should-not-touch slice no longer depends on it.
4. **0.7 gets a narrow contract**: `SnapshotLibrary(export, delta)`, and
   `apply_changes` already exists.

The cost is that a case is never seen against a realistically messy library. That
is the right trade at Phase 0, where the job is measurement rather than
simulation, and it is recorded here rather than discovered in 0.8.

### Decision 2 — three expectation kinds, and two outcome shapes that compose

`expectation.kind` is `repair | no_action | escalate`, per the spec. Within
`repair`, Finding 3 gives two shapes keyed on `witness.kind`, and they are **not**
alternatives — a relation class carries both:

```jsonc
// witness kind=value — eight classes. Postcondition only.
"required_findings": [{
  "problem_class": "wrong_match",
  "item_ids": ["fake:1:104"],
  "postcondition": {"/title": {"normalized_equals": "The Shawshank Redemption"},
                    "/year":  {"equals": 1994},
                    "/guids": {"contains": ["imdb://tt0111161"]}},
  "soft_postcondition": {"/summary": {"normalized_equals": "..."}},
  "must_not_change": ["/parts/*/path"],
  "repair_op": {"any_of": ["rematch", "set_field"]}      // advisory, never a gate
}]

// witness kind=relation — three classes. A resolution, *plus* a postcondition on
// every surviving item the delta touched.
"required_findings": [{
  "problem_class": "multi_file_split",
  "resolution": {"relation": "same_book",
                 "item_ids": ["fake:3:411", "fake:3:swea3728f407"],
                 "keeper": "fake:3:411"},
  "postcondition": {"/title": {"normalized_equals": "The Way of Kings"}},
  "soft_postcondition": {"/part_count": {"equals": 2}},
  "must_not_change": ["/parts/*/path"],
  "repair_op": {"any_of": ["merge_items"]}
}]
```

`duplicate_quality` is the one class carrying a `resolution` and no
`postcondition`: its delta is a pure ADD, so no surviving item's field changed.

A `resolution` is scored on the **finding's** identified id set and keeper, not on
a simulated end state. That does not violate invariant 5: the finding is recorded
state, and the scorer decides whether it is right — what the invariant forbids is
taking the model's word for whether it succeeded.

`resolution.item_ids` is compared as a **set**, and merging two of three author
variants leaves the library broken, so the comparison is equality rather than
overlap. That is what makes the case binary and keeps the CI gate's boolean.

**`keeper` is gated only where the ground truth settles it.** The mechanical rule
is "the member of `item_ids` that exists in the ground-truth family", and for
`author_name_variant` and `multi_file_split` that is exactly the question the case
asks: one name is canonical, one file set is one book, and merging into the minted
item is the wrong answer. It is wrong for `duplicate_quality`.
`DUPLICATE_VARIANTS["resolution"]` mints the clone at 2160p against a 1080
original, so "keep the entry that already existed" would score a steward that keeps
the better copy as wrong. Nothing in the ground truth settles which entry of a real
duplicate pair should survive — that is a keep policy, and Phase 3's repair stage
owns it.

So `duplicate_quality` gates the relation and the id set, records `keeper: null`,
and reports whichever id the agent named. A test pins the 2160p variant
specifically, because the rule looks safe until you read the variant table.

### Decision 3 — hard and soft postconditions, declared as field tables

`implementation-plan.md` specifies the split — "hard (fields the repair directly
sets, gated) and `soft_postcondition` (downstream of Plex's own agent, reported
not gated)" — and never says which is which. It is a property of the field, so it
is a table:

| Tier | Fields | Why |
|---|---|---|
| **hard** | `/title`, `/title_sort`, `/year`, `/guids`, `/index`, `/parent_index`, `/series`, `/series_position`, `/edition_title`, `/content_rating`, `/studio` | the repair sets these directly through a plexapi edit |
| **soft** | `/summary`, `/parent_title`, `/parent`, `/child_count`, `/leaf_count`, `/album_count`, `/part_count`, `/parts/*/path`, `/has_thumb`, `/has_art` | derived by Plex's own agent after a rescan, or by the filesystem |

`/parent` is soft and `/parent_index` is hard, which looks inconsistent and is
not: an agent repairs a misfiled episode by setting its season number, and Plex
re-derives the parent link on the next scan. Gating on the derived value would
fail a correct repair for a reason the agent does not control.

`/parts/*/path` is soft on every class, including `filename_unmatchable` whose
truth record holds "a **suggested** filename". A rename is a Phase 3 operation and
the spec's own word is suggested; gating on it here would score a correct
diagnosis as a failure.

**The table is matched as a pattern, not looked up as a key.** A `FieldChange.path`
is always concrete — `/parts/0/path` — and `model.py` forbids a wildcard in one
outright; the tables carry `/parts/*/path` because a tier is a statement about a
field, not about a slot. `pointer.select` cannot answer this: it needs a document,
and the question here is selector-against-pointer with no document in hand. So
`pointer.py` gains `matches(selector, pointer)`, a segment-wise comparison beside
`has_wildcard`, and the derivation uses it. A path in **neither** table raises at
derivation time rather than defaulting to hard: every path the eleven corruptions
touch is covered today, and a twelfth class that touches something new should stop
here rather than silently acquire a gate nobody chose.

### Decision 4 — `must_not_change` is derived, with a declared floor

Per Finding 4: `witness.pointers − delta.paths`, unioned with a per-class floor.
The floor exists for invariant 11 (never delete a file) and holds `/parts/*/path`
on every class whose repair does not legitimately rename — which, with `/parts`
soft in Decision 3, is the one place a file path is gated at all. A path that both
tiers claim is resolved in favour of `must_not_change`: not gating the value while
gating its destruction is exactly the intent.

### Decision 5 — a `case_id` collision is an error, and subject uniqueness is a precondition

Per Finding 2. Two mechanisms, in this order:

1. **Selection excludes non-unique subjects.** The generator builds a subject
   index over the whole export before selecting anything. A subject key held by
   more than one family is ineligible, and the exclusion is counted per class in
   the deficit report — a library full of duplicates will see this, and seeing it
   is the point.
2. **The dataset still asserts uniqueness.** After assembly, duplicate `case_id`s
   raise and name both cases. A generator that silently disambiguates is a
   generator whose ids are positional again.

`generator_version` stays **out** of `case_id` — otherwise every version bump nukes
the baseline — and `corruption_fingerprint = sha256(delta ‖ generator_version)`
carries that signal separately, which is what feeds the CI diff's `changed`
bucket.

The digest is pinned rather than left to the implementation:
`"case-" + sha256(canonical_json({...}))[:12]` over the five fields as one dict,
matching `implementation-plan.md` and `context._digest`. `subject_key` enters it as
the `f"{kind}:{value}"` string `CorruptionResult.subject_key` already records, so
the id does not move if the `SubjectKey` dataclass ever gains a field.

### Decision 6 — `CLASS_KINDS`, and trivial guarding

Per Finding 5. `CLASS_KINDS: dict[ProblemClass, frozenset[MediaKind]]` in
`models/finding.py`, beside `ProblemClass` itself, because two packages need it:
the screen sorts its guard claims with it, and 0.6's `no_action` uses it to decide
that a class which cannot describe an item is **trivially guarded**.

That last point is what gives the should-not-touch expectation a complete
partition. Every problem class on every should-not-touch item is exactly one of:
*guarded* (the screen verified it), *trivially guarded* (the class cannot apply to
this media kind), or *unguarded* (nobody checked). A finding in the first two is a
false positive; a finding in the third is `unverified` — counted, reported, never
scored as pass or fail. Without `CLASS_KINDS` the second bucket collapses into the
third, and the project starts recording "we could not verify that this movie has
no absolute-numbering problem".

Three consequences the one-line version of this hid:

- **`ItemScreen` gains a third field**, `trivially_guarded_classes`, beside the two
  it has. An intersection alone would move the class into `unguarded_classes` and
  score a false positive as `unverified` (Finding 5).
- **`screen.json`'s `SCHEMA_VERSION` moves to 2.** 0.5's Decision 7 explicitly
  recorded that its `GUARD_TABLE` correction did *not* move it, because the
  document shape was unchanged. This changes the shape, so it does. Screens are
  regenerable and `datasets/` is not committed, so nothing needs migrating — the
  version is what distinguishes a stored screen from an older one.
- **`GuardCoverage` gains a `trivial` count** beside `in_scope`, so the denominator
  `fp_rate_snt` states is the one it claims to be rather than one inflated by kinds
  the class cannot describe.

`CLASS_KINDS` overlaps `CorruptionSpec.applies_to`, which already declares the
kinds each *corruption* runs on. They are not the same statement — a class can
describe a kind no corruption can synthesize on — so the containment is asserted
rather than the table derived. Without it this is a third hand-written table about
problem classes, and the previous two both shipped wrong.

**Corrected 2026-09-29: the containment is not `applies_to ⊆ CLASS_KINDS`.** That
is false by design for four classes. `applies_to` names the family **root** a
corruption is handed, while `episode_wrong_season` runs on a SHOW and describes an
EPISODE, and `multi_file_split`, `missing_series` and `series_order_broken` run on
an AUTHOR and describe an AUDIOBOOK. The containment that matters is the one the
dataset would contradict itself without: **every item a case's required finding
names is a kind its class describes.** Otherwise the should-not-touch slice would
call that same finding a false positive on that same kind. Measured over every
fixture case, it holds for all eleven classes, and it is the test
(`TestClassKinds` in `tests/evals/corrupt/test_corruptions.py`).

### Decision 7 — curated slices are TOML, not YAML

The spec names `datasets/curated/real.yaml` and `ambiguous.yaml`. YAML would be a
new runtime dependency for two files that are read once at generation time, and
`composition.toml` has already established TOML in this project with `tomllib` in
the standard library. Multi-line strings and arrays of tables cover what an
adjudication record needs.

So: `datasets/curated/real.toml` and `ambiguous.toml`. This is a spec deviation
and it is recorded rather than silently made. It is also cheap to revisit — 0.9
owns the adjudication format and is the step that will know whether a human
editing 60-second records wants YAML's ergonomics badly enough to pay for the
dependency. If it does, the converter is a morning's work.

### Decision 8 — composition declares all fifteen classes; the resolver reports two numbers

`composition.toml` at the repo root, per-media-kind shares with per-class shares
nested, normalized at load rather than required to sum exactly. It declares all
**fifteen** classes, including the four that cannot yet be generated, because the
file is a statement of design intent and should not churn when 1.1 lands.

The resolver therefore emits two targets per cell:

- **intended** — what the composition asks for, over all fifteen.
- **achievable** — what the export and the registry can actually supply.

`dataset.json` carries both plus the gap, so a dataset stays interpretable years
later without the `composition.toml` that produced it, *and* a reader can tell a
deliberate share from a shortfall. A single resolved number would make those
indistinguishable, which is how a dataset comes to report coverage it never had.

The per-class shares are read for the `synthetic`, `real` and `ambiguous` slices
only. `should_not_touch` resolves to media cells alone, per Decision 10.

### Decision 9 — one case per (family, class), and the cap is drawn before any target is read

Per Finding 6. Decision 1 removes the reason for family exclusivity — with
per-case worlds two cases on one family cannot interfere — so a family may host
one case per class. That roughly triples supply on a small export.

It is one case per (family, class), **not** per (family, class, variant):
`run.variant_for` makes the variant a function of `(seed, subject, spec)`, so a
family has exactly one variant per class and there is no second case to draw from
a second variant. `corruption_variant` stays inside `case_id` anyway, because it
moves when the variant table moves — that is a case whose identity should change,
not extra supply.

Concentration follows, so a cap is needed — but **the cap must not be consumed in
cell order**, which is how the first draft of this decision was written. A
first-come cap breaks the prefix stability everything else here exists to protect.
With the cap at 1 and cells walked in class order: at a low `--count`, cell A takes
`s1` and cell C takes `s2`; raise the count until A's target reaches 2 and A now
takes `s1` and `s2`, so C's case on `s2` **disappears** and reappears on `s3`. That
is `test_raising_the_count_adds_cases_without_moving_existing_ones` failing against
a real library while passing against an 11-family fixture, which is the worst shape
a test can have.

So cap eligibility is a function of hash rank alone, decided before any target is
read. For each subject, rank the classes it is a candidate for by
`sha256(seed ‖ subject_key ‖ problem_class)` and issue tickets to the first
`MAX_CASES_PER_SUBJECT`. A cell then selects only among subjects already holding a
ticket for it, and no cell's choices depend on another cell's target — or on
whether another cell exists at all, which is what keeps a `composition.toml` edit
from moving cases in cells it did not touch.

`MAX_CASES_PER_SUBJECT = 3` — the fixture's busiest family hosts 4 candidates, so
the cap bites there and its effect is visible in the fixture's own deficit table
rather than only on a real library. `subjects_covered` is published beside `cases`,
and the per-class deficit records how many candidates the cap turned away, because
a cap that drops work without saying so is the silent truncation house rule 12
forbids.

### Decision 10 — a should-not-touch case has no problem class, and its cell is media-only

The composition resolver multiplies slice × media × class and `case_id` hashes
`(slice, problem_class, media_kind, subject_key, corruption_variant)`. Neither
works for the 15% of the dataset that is should-not-touch: such a case is about an
item, not about a class, and there is no corruption and therefore no variant. The
first draft of this plan carried the omission silently, which would have surfaced
as a `None` in a hash halfway through building `generate.py`.

- `problem_class` and `corruption_variant` are `None` on a `no_action` case and
  enter the digest as JSON `null`. They stay in the digest rather than being
  dropped from it, so one hash covers every slice and a should-not-touch case can
  never collide with a synthetic case on the same subject.
- The `should_not_touch` slice resolves **media cells only** — slice share × media
  share × `--count`. Per-class shares are not read for it, and the resolver says so
  in the deficit table rather than silently multiplying by a table that does not
  apply to it.
- Eligibility is the clean screen's own verdict: `Verdict.GUARDED`, which already
  means every applicable check passed and at least `MIN_APPLICABLE_CHECKS` were
  applicable. An item the screen calls `failed` is a 0.9 curated candidate instead
  — `screen.Candidate` is already exactly that hand-off — and an `insufficient` one
  is neither.
- Selection ranks eligible items by `sha256(seed ‖ subject_key)` like everything
  else, and Decision 9's tickets apply with `no_action` participating in the
  ranking as a pseudo-class.

An item may be both a should-not-touch case and a repair case's subject: Decision 1
gives them different worlds and they are scored independently. The subject cap is
what keeps that from becoming concentration.

The `ambiguous` slice keeps a real `problem_class` — `anthology_omnibus` today —
because a curated escalate case is about a named ambiguity.

### Decision 11 — a lineage is the library and the generator, not the composition

`implementation-plan.md` gives `lineage_id = sha256(composition.toml + slice defs)`
and makes it the baseline key so that re-exporting a live library does not reset
the baseline. Keyed that way it resets on something else instead: editing a share —
the one thing `composition.toml` exists to let a human do — starts a fresh lineage
and discards the history of every case whose id did not change. That is the failure
Decision 5 is about, arrived at from the other side.

A composition edit is already expressed correctly by the four CI buckets. Cases the
new shares dropped are absent, cases they added are `new` and gate nothing, and
everything else keeps its `case_id` and its history. Nothing about moving a share
makes an older result untrue.

So `lineage_id = sha256({provider, section_ids, generator_version_major,
schema_version})` — what actually decides whether two datasets are comparable — and
`composition_id = sha256(composition.toml)` is recorded beside it in `dataset.json`
as a diagnostic. A reader can then see that the composition moved while the
baseline did not, which is the fact they want.

---

## 4. Design

### 4.1 Modules

```
composition.toml                     # NEW, repo root
datasets/curated/real.toml           # NEW, empty until 0.9
datasets/curated/ambiguous.toml      # NEW, empty until 0.9
src/shelfwarden/
  models/finding.py                  # + CLASS_KINDS (D6)
  pointer.py                         # + matches(selector, pointer) (D3)
  evals/
    screen.py                        # + trivially_guarded_classes, SCHEMA_VERSION 2 (D6)
    corrupt/collateral.py            # symmetric second pass (F1)
    truth.py                         # NEW: TruthFile, expectations, postconditions
    composition.py                   # NEW: the toml loader and cell resolver
    curated.py                       # NEW: the curated slice reader
    generate.py                      # NEW: selection, assembly, deficits, __main__
```

### 4.2 The truth file

One `truth.json` per dataset. Cases sorted by `case_id`, so the file is diffable
and its bytes do not depend on selection order.

```jsonc
{
  "schema_version": 1,
  "dataset_id": "sw-20260901-a1b2c3",     // f(seed, items_sha256, generator_version)
  "lineage_id": "lin-7f3a91",             // f(provider, sections, generator major, schema) — D11
  "seed": 1518,
  "generator_version": "0.1.0",
  "source_export": {"export_id": "...", "items_sha256": "...", "roots_sha256": "..."},
  "screen": {"items_sha256": "...", "authority": "none", "min_applicable_checks": 3},
  "cases": [{
    "case_id": "case-a3f91c2b8e04",
    "slice": "synthetic",                 // synthetic | real | should_not_touch | ambiguous
    "run_group": "grp-8e04a3f91c2b",
    "problem_class": "wrong_match",       // null on a should-not-touch case (D10)
    "media_kind": "movie",
    "subject_key": {"kind": "external_id", "value": "imdb://tt0111161"},
    "corruption_variant": "donor_same_section",   // null likewise (D10)
    "corruption_fingerprint": "sha256:…",
    "item_ids": ["fake:1:104"],
    "expectation": { /* §4.3 */ },
    "witness": { /* the 0.5 DetectabilityWitness, verbatim */ },
    "ground_truth": [ /* the clean family, as NormalizedItem records */ ],
    "provenance": {"method": "synthetic"}
  }]
}
```

`ground_truth` holds the **family**, not one item, because 0.5's unit is a family
and four classes change more than one record. `deltas.jsonl` holds the
corresponding `ItemChange` set keyed by `case_id`; keeping it out of `truth.json`
keeps the truth file readable and lets 0.7 stream one case's world without parsing
every case's ground truth.

`run_group` is `grp-<sha256(seed ‖ subject_key)[:12]>` for a synthetic case:
everything one run must see together is, by Decision 1, exactly one family. It is
keyed on the subject rather than on the family's root id because a rating key moves
on rescan, and a file whose entire purpose is surviving a re-export should not
carry one where a semantic key is free. It is a separate field rather than a
derived one because 0.9's curated cases may group differently, and because the spec
is explicit that execution grouping is not a scoring grouping.

### 4.3 Expectations

```jsonc
// repair — the synthetic and real slices
{"kind": "repair",
 "required_findings": [ /* Decision 2: a postcondition, a resolution, or both */ ],
 "unexpected": "fail",                       // DEFAULT on every case, every slice
 "known_other_problems": ["duplicate_quality"]}

// no_action — should-not-touch
{"kind": "no_action",
 "guarded_classes": ["wrong_match", "missing_metadata"],
 "trivially_guarded_classes": ["absolute_vs_seasonal", "series_order_broken"],
 "unguarded_classes": ["alternate_cut"],
 "verification": {"method": "mechanical",
                  "checks": [{"predicate": "resolvable_id_present",
                              "evidence_id": "sha256:…", "result": "pass"}]},
 "unexpected": "fail"}

// escalate — ambiguous, curated only
{"kind": "escalate",
 "require_finding": true,                    // silence is not escalation
 "require_needs_human": true,
 "min_candidates": 2,                        // == witness.MIN_AMBIGUITY_CANDIDATES
 "acceptable_resolutions": [...],
 "forbidden_findings": [{"repair_op": "merge_items"}],
 "unexpected": "fail"}
```

`unexpected: fail` is a field with a default on the model, not a value the
generator writes per case — a default in code cannot be forgotten for one slice,
which is how 85% of the dataset came to be false-positive-blind in the first
draft.

`min_candidates` reads `witness.MIN_AMBIGUITY_CANDIDATES` rather than repeating the
literal: 0.5 declared that constant *because* 0.6 puts the same floor on an
escalate case, and two 2s that must agree are one constant and a copy of it.

`known_other_problems` has exactly two sources, both mechanical:

1. `CorruptionResult.induced` — the problems the corruption knowingly created.
   `wrong_match` and `year_collision_remake` both induce `duplicate_quality` by
   copying a real title.
2. Classes the **ground-truth family already fails** per the screen — computed by
   running `screen_item` over the clean family and collecting every class whose
   guard holds a failing predicate. **Not** read off `CorruptionResult.cross_check`:
   that carries one verdict for the case's *own* class, so its `already_failing`
   answers "was this class guarded before I broke it" and says nothing about the
   other fourteen. `multi_file_split` is always in that state — it targets a book
   with two files and its guard is `single_part` — which is what makes the
   distinction easy to miss.

Nothing here is hand-written, and nothing is inferred from the corrupted world.

### 4.4 The postcondition vocabulary

Five predicates, each evaluated by literal comparison against `ground_truth`:

| Predicate | Applies to | Comparison |
|---|---|---|
| `equals` | numbers, booleans, nulls, `ItemId` refs | canonical bytes |
| `normalized_equals` | text | `compare.fold_text` equality |
| `contains` | `/guids` | every ground-truth external id present, and none of `excludes` |
| `absent` | any | the path resolves to `null` |
| `non_empty` | text, lists | resolves to a non-empty value |

Derivation from a delta's `FieldChange(path, before, after)`:

- `/guids` → `contains` the ground-truth ids, and `excludes` the ids the
  corruption injected (`after − before`). Strict equality would fail a repair
  that leaves an extra `plex://` id in place, which is not a defect. Leaving the
  donor's id in place is. *Corrected 2026-09-30.* This row first read "every
  ground-truth id present" alone, which was `contains []` on an item with no ids,
  true in every world. `excludes` is a qualifier on `contains`, refused on any
  other predicate, so the vocabulary is still five predicates and one per
  location (§10).
- text fields → `normalized_equals`. Case and NFC form are not the repair.
- everything else → `equals`.
- tier from Decision 3's table; a soft field lands in `soft_postcondition`.

The derivation runs over `ChangeKind.MODIFY` on items that exist in the ground
truth — for **every** class, including the three whose witness is a relation
(Finding 3). An `ADD` yields no postcondition, because it names the item the repair
is supposed to make disappear; the resolution covers it. A `REMOVE` is recorded in
`soft_postcondition` as `absent → present` and reported rather than gated, because
re-creating a container is Plex's job after a rescan. `absolute_vs_seasonal` is the
class holding both, and it is a postcondition class: the emptied season is the
REMOVE, and the renumbered `/index` and `/parent_index` are the gate.

### 4.5 Composition

```toml
# composition.toml — shares are normalized at load; they need not sum to 1.
[slices]
synthetic = 0.50
real = 0.25
should_not_touch = 0.15
ambiguous = 0.10

[media.movie]
share = 0.40
[media.movie.classes]
wrong_match = 0.20
year_collision_remake = 0.15
foreign_title_variant = 0.10     # declared, not yet generable (step 1.1)
# ...
```

The resolver:

1. normalizes slice shares, then media shares, then per-class shares within a
   medium;
2. multiplies out to **intended** per-cell counts against `--count`;
3. intersects with what the registry implements and the export can supply, giving
   **achievable**;
4. emits a `CompositionDeficit` row for every cell where the two differ, naming
   which of **four** reasons applies — `not_implemented` (no corruption function;
   waiting on 1.1), `no_candidates` (no family in the export has the shape, per
   `Applicability.no`), `rejected` (a corruption was attempted and failed an
   acceptance check), or `capped` (`MAX_CASES_PER_SUBJECT`, or a subject dropped as
   non-unique per Decision 5).

The middle two are the split `Rejection.applicable` exists to preserve — "your
library has no remake pairs" and "the harness rejected what it built" are different
facts and only the second is actionable — and `run.ClassDeficit` already counts them
separately. Collapsing them here would throw that away one layer above the code
that computed it.

Largest-remainder rounding, applied over cells sorted by `(media_kind,
problem_class)`, so the resolved integers are a function of the shares and not of
dict order.

**Corrected 2026-09-30: sequential Webster apportionment, not largest remainder.**
Largest remainder is not house-monotone, which is the Alabama paradox: raising
`--count` by one can *shrink* a cell. Measured on the committed `composition.toml`
between 0 and 1000, that happened 210 times, ten of them in cells the generator
fills (`should_not_touch | author` 18 → 17 at 467). A shrunk cell drops a case and
its history on an edit that asked for more. That breaks the prefix stability
Decision 9 and §4.6 exist to protect, one layer above them. Webster hands targets
out one at a time by `share / (2·held + 1)`, so the targets at N+1 are the targets
at N plus one. It is Webster rather than D'Hondt because D'Hondt favours large
cells. Webster can miss a cell's exact quota; on the committed file it does so once
in 0..1000, by 0.005. Priorities are exact fractions, and the share totals use
`math.fsum`, so the result does not depend on declaration order by construction.

### 4.6 Selection

Per-class, and prefix-stable throughout — 0.5's rule, unchanged and for the same
reason: `random.sample` is not a prefix-stable function of `k`, so raising a cell's
target by one must add a case rather than re-pick every case in the cell.

```
1. index subjects across the export; drop non-unique ones (D5), counting them
2. issue each subject its cap tickets by hash rank (D9) — before any target is read
3. per cell, rank eligible families by sha256(seed ‖ subject_key)
4. take the first N holding a ticket for this cell
5. run the 0.5 corruption; a rejection consumes no target and is recorded
6. assemble the case, derive the expectation, assert case_id uniqueness
```

Step 5 is the one that needs care: a rejected attempt must not silently shrink the
cell, and must not cause an unbounded walk down the ranked list either. The
generator takes `ceil(N * OVERSAMPLE)` candidates, stops at *N* successes, and
records both the shortfall and the number of attempts — so "we tried 30 families
to fill 20 cases" is visible.

The `should_not_touch` cells run the same loop with step 5 skipped: the world is
the clean export, eligibility is `Verdict.GUARDED`, and "assembly" is sorting the
item's own `ItemScreen` into Decision 6's three buckets. The `real` and `ambiguous`
cells read `curated.py` instead of the export, and ship empty at this step.

### 4.7 What 0.7 will consume

Fixed here so 0.7 has no design left to do:

```python
SnapshotLibrary.for_case(export_directory: Path, dataset: Path, case_id: str) -> LibraryProvider
```

It reads the clean export, applies that case's delta with `apply_changes`, derives
the population index with `stub_of`, and serves the result through the identical
`LibraryProvider` protocol with the identical `LibraryError` taxonomy. Nothing in
`evals/` needs to change for it.

---

## 5. Build steps

**0.6.1 — `CLASS_KINDS` and the third guard bucket.** `models/finding.py` gains
the table and the `applies_to ⊆ CLASS_KINDS` test; `screen.py` gains
`trivially_guarded_classes`, a `trivial` count on `GuardCoverage`, and
`SCHEMA_VERSION` 2. *Done when* a movie reports `absolute_vs_seasonal` as
**trivially** guarded rather than as either guarded or unguarded, and
`guard_coverage.in_scope` for that class counts only shows, seasons, and
episodes.

**0.6.2 — the collateral symmetry fix.** *Done when* the completeness property
holds: for every case in the fixture survey, the set of items whose screen verdict
changes is a subset of `family ∪ collateral`. That test is the one that failed
while this plan was written.

**0.6.3 — `truth.py`: the schema and the derivation.** `TruthFile`, the three
expectations, the five predicates, the hard/soft tables and their selector matcher,
the keeper rule, and derivation from a delta keyed on `witness.kind`. *Done when* a
derived postcondition holds against the ground truth and fails against the
corrupted world for all **ten** classes that carry one — the eight `kind=value`
classes plus `author_name_variant` and `multi_file_split` — and the three
`kind=relation` classes carry a resolution built from the witness.

**0.6.4 — `composition.py`.** Loader, normalization, largest-remainder rounding,
intended/achievable, deficit rows. *Done when* shares that do not sum to 1
normalize, and the resolved integers sum to `--count`.

**0.6.5 — `curated.py`.** Reader for `real.toml` / `ambiguous.toml`, validating
against the same case schema. *Done when* an empty curated file yields an empty
slice and a deficit row rather than an error.

**0.6.6 — `generate.py`.** Subject index, cap tickets, selection, assembly,
uniqueness assertion, artifact writing, `__main__`. *Done when*
`python -m shelfwarden.evals.generate <export-dir> --count 200 --seed 1518` runs
against the fixture export and writes a dataset. The export directory is a
positional argument, as it is on `shelfwarden corrupt`; the gate line in
`roadmap.md` and `CLAUDE.md` omits it and means the same command.

**0.6.7 — the report and the CLI.** `report.md`, and `shelfwarden eval generate`
delegating to the same code path. *Done when* the report names every deficit with
its reason.

---

## 6. Tests

Beyond the gate (reproducible, never silently unbalanced):

- **`test_a_dataset_is_byte_identical_across_hash_seeds`** — forked subprocesses,
  `PYTHONHASHSEED` 0 and 1, per practices §8.2.
- **`test_raising_the_count_adds_cases_without_moving_existing_ones`** — the
  prefix-stability property, now at dataset scale.
- **`test_case_ids_survive_a_re_export`** — regenerate from a second export of the
  same library; ids must be unchanged. The property the CI gate rests on.
- **`test_a_colliding_subject_is_excluded_and_counted`** — a hand-built library
  with two entries sharing a guid (Finding 2).
- **`test_a_duplicate_case_id_raises_rather_than_being_disambiguated`**.
- **`test_generator_version_does_not_change_case_ids`** — but does change
  `corruption_fingerprint`.
- **`test_unexpected_fail_is_the_default_on_every_case`** — parameterized over
  every slice, including `no_action`; the Defect 1 regression.
- **`test_silence_does_not_satisfy_an_escalate_case`** — the Defect 2 regression.
- **`test_a_derived_postcondition_holds_on_truth_and_fails_on_the_corruption`** —
  parameterized over the ten classes that carry one.
- **`test_a_relation_case_carries_a_resolution`** — parameterized over the three
  `kind=relation` classes, and `duplicate_quality` specifically, whose delta is a
  pure ADD and which therefore carries a resolution and no postcondition.
- **`test_merging_a_split_book_does_not_excuse_the_mangled_title`** — the
  `multi_file_split` postcondition rejects a repair that leaves `/title` reading
  `CD1` (Finding 3).
- **`test_the_keeper_is_null_where_the_ground_truth_does_not_settle_it`** — the
  `duplicate_quality` `resolution` variant, whose clone is 2160p against a 1080
  original.
- **`test_the_subject_cap_does_not_move_cases_when_the_count_rises`** — the
  Decision 9 regression, on a library big enough for two cells to contest a
  subject. The fixture is not; this one builds its own.
- **`test_a_should_not_touch_case_has_no_class_and_still_has_a_unique_id`** —
  Decision 10, including that a `no_action` case and a synthetic case on the same
  subject do not collide.
- **`test_a_composition_edit_does_not_change_the_lineage_id`** — Decision 11.
- **`test_every_delta_path_has_a_tier`** — the hard/soft tables cover every path
  the eleven corruptions touch, matched as selectors, and an unknown path raises
  (Decision 3).
- **`test_must_not_change_never_forbids_the_repair`** — for every class, the
  derived selectors must not intersect the paths the repair has to set (Finding 4).
- **`test_a_class_that_cannot_apply_is_trivially_guarded`** (Finding 5).
- **`test_an_unfillable_cell_is_a_deficit_row_not_a_re_draw`** — a composition
  demanding more `year_collision_remake` than the library has remake pairs.
- **`test_the_deficit_names_which_of_three_reasons_applies`**.
- **`test_an_empty_curated_slice_is_a_deficit_not_an_error`**.
- **`test_the_screen_and_the_truth_file_agree_about_guarded_classes`** — the
  should-not-touch cross-check.

---

## 7. What 0.6 does not do

- No `SnapshotLibrary` — 0.7. The interface is fixed in §4.7; the implementation
  is not.
- No scoring — 0.8. The truth file is written to be readable without the
  generator, which is the whole contract between the two steps.
- No labelling — 0.9. `real.toml` and `ambiguous.toml` ship empty, and their
  slices report as deficits.
- No network, and no authority tier. Four classes stay unfillable and say so.
- No repair simulator. Decision 2 exists precisely to avoid needing one.

---

## 8. Risks and open questions

**The dataset will be 65% of its intended size, and that is the honest outcome.**
The real (25%) and ambiguous (10%) slices are curated and empty until 0.9, and
four classes wait on 1.1. `--count 200` will produce roughly 130 cases with a
deficit table explaining every missing one. The Phase 0 gate does not require a
full dataset — it requires a reproducible one that reports its own gaps — and the
Phase 1 gate needs 20 cases. Both are reachable. The temptation to backfill the
shortfall by over-sampling the classes that *do* work is exactly what the deficit
report exists to resist.

**Concentration is the subtler risk.** Decision 9 lets one family host several
cases, so 130 cases might cover 40 subjects. Pass rate then over-weights whichever
items happen to be corruptible, and a single unusual film can move a class's score
several points. `subjects_covered` is published for this reason, and 0.8 should
consider reporting pass rate per subject as well as per case before anyone reads
the headline number as a library-wide estimate.

**The subject cap is stable against the two things that move often, and not
against the one that moves once.** Decision 9's tickets are a function of
`(seed, subject, class)`, so raising `--count` and editing `composition.toml`
cannot move an existing case — those are the edits a human makes weekly. Shipping a
new corruption in 1.1 *can*: a subject that becomes a candidate for a twelfth class
re-ranks its tickets, and a class holding one may lose it. There is no cap that
avoids this — capping at all means some class goes without — so it is recorded
rather than engineered around. It lands once, in the CI diff's `new` and absent
buckets, and 1.1 should expect it rather than debug it.

**`MAX_CASES_PER_SUBJECT = 3` is a guess.** It is chosen against an 11-family
fixture whose worst case is 4, not against a real library. The number to set it by
is the concentration in the first real export's census — `subjects_covered` against
`cases` — and it should be revisited there rather than defended here.

**`normalized_equals` on text is a judgement call that could hide a real failure.**
It exists so that case and NFC form are not scored as repairs, but it also means an
agent that returns `THE DARK KNIGHT` passes. If that turns out to matter, the fix
is a tier — `equals` for hard fields, `normalized_equals` for soft — not a
threshold. Recorded now because it is the kind of leniency that is invisible once
the numbers look reasonable.

**Open: whether `real` cases can share the synthetic postcondition machinery.**
0.9 will produce curated cases whose ground truth is a human's judgement rather
than a recorded pre-corruption state, so there is no delta to invert. They will
need hand-written postconditions in the same vocabulary. The vocabulary is designed
for that, but nothing has exercised it, and the first curated case is the test of
whether §4.4's five predicates are enough.

---

## 9. Documents to update in the same change

- **`roadmap.md`** — 0.6's checkboxes, plus the two 0.5 defects this step fixes,
  plus the curated-slice line, which still names `real.yaml` / `ambiguous.yaml`
  (Decision 7).
- **`implementation-plan.md` §3** — corrections: postconditions cover 10 of the 11
  implementable classes rather than "roughly 12 of 15", with a `resolution` on the
  three relation classes and `duplicate_quality` alone carrying no postcondition;
  the hard/soft field tables; the dataset as clean export plus per-case deltas,
  under `datasets/evals/<id>/`; curated slices in TOML; `lineage_id` keyed on the
  library and the generator rather than on `composition.toml`.
- **`development-practices.md`** — §11 gains the invariant behind Finding 2 (a
  `case_id` collision is an error, never disambiguated by position) and Finding 5
  (a guard is only meaningful for a class that can describe the item). §11.13 is
  **amended**: it says a corruption's blast radius moves in one direction, and it
  instructs 0.6 to evict collateral ids from the should-not-touch slice. Finding 1
  corrects the first; Decision 1 retires the second. §8.2 gains a trap beside
  `random.sample`'s: **largest-remainder apportionment is not house-monotone** (the
  Alabama paradox), so it moves cases on a count increase just as a re-drawn sample
  does (§4.5).
- **`evals/corrupt/run.py`** and **`evals/corrupt/report.py`** — both state that
  "step 0.6 selects from these and enforces one case per family". Decision 9
  reverses it: one case per (family, class).
- **`CLAUDE.md`**, *things that look wrong but are correct* — `/parent` soft while
  `/parent_index` is hard; `composition.toml` declaring classes that cannot yet be
  generated; `must_not_change` deliberately omitting a witness the repair must
  rewrite; `duplicate_quality` recording `keeper: null` while the other two
  relation classes name one; a subject's cap tickets drawn before any cell target
  is read; `/guids` tolerating extra ids while excluding the injected ones.
- **`architecture.md` §5 and §13** — the measurement loop gains the generator and
  the per-case world; the status table gains 0.6.
- **`collateral.py`** — a note naming 0.6 as the step that found the asymmetry, so
  the next reader sees the reason rather than the edit.

---

## 10. Status and remaining work

**Measured 2026-09-29** against `4b05c74` plus the uncommitted 0.6 working tree.

Build steps 0.6.1–0.6.6 are implemented. Against the fixture export,
`python -m shelfwarden.evals.generate <export> --count 200 --seed 1518` writes all
five artifacts: 25 cases over 10 subjects, with 59 short cells each carrying a
reason. Two in-process runs are byte-identical, and so are runs under
`PYTHONHASHSEED` 0 and 1. So the gate's behavior holds, but **nothing yet asserts
it**: the suite is still at 0.5's 646 tests, and none of §6 exists.

Gaps:

- **No tests for any 0.6 code**, including Finding 1's collateral fix. That fix
  has never been shown to fail on 0.5's `collateral.py` and pass on the new one.
- **0.6.7 is not started.** `cli.py` has no `eval generate`.
- **No §9 document updates.** `evals/corrupt/run.py` and `report.py` still say
  "one case per family".
- **This plan contradicts itself about deficit reasons.** §4.5 lists four, the §6
  test name says three, and the code emits five: `not_curated` is added for the
  empty curated slices. The code is right. §4.5 and the test name follow it.

Remaining work, in order:

1. ✅ **Prove the leaf changes (0.6.1, 0.6.2).** Done 2026-09-29, 24 tests, suite
   at 670. `CLASS_KINDS` and trivial guarding, the class-kind containment (restated
   in Decision 6: the plan's `applies_to ⊆ CLASS_KINDS` is false by design),
   `GuardCoverage.trivial` and the schema-version refusal, and `pointer.matches`,
   cross-checked against `select` on every leaf of every media kind.
   The collateral completeness test was **run against 0.5's `collateral.py` and
   failed**: `wrong_match on fake:1:108 moved undeclared ['fake:1:107']`, which is
   Finding 1's leak. It also failed with a leak Finding 1 did not list,
   `filename_unmatchable on fake:1:502 moved undeclared ['fake:1:501']`, in the
   edition fixture. Finding 1 measured only the shared library. The fix covers
   both, and against it the test passes.
2. ✅ **`truth.py` tests (0.6.3).** Done 2026-09-29: `tests/evals/test_truth.py`, 94
   tests, suite at 762 passed and 2 xfailed. `truth.py` has no predicate
   evaluator, since scoring is 0.8's, so the tests carry a reference reading of
   §4.4's five predicates (`_holds`). **0.8 should replace it with the scorer's
   own.** Measured along the way:
   - **Nine classes gate on a hard postcondition, not ten.** `author_name_variant`
     rewrites only derived fields on its surviving items, so its postcondition is
     all soft and the resolution alone gates it. That is correct, but Finding 3's
     "ten carry a postcondition" needs the qualifier.
   - **`known_other_problems` could excuse a false positive.** Fixed.
     `already_failing_classes` credited a failing guard to every class holding
     it, so a film with a scene-release file would have listed
     `absolute_vs_seasonal` and `episode_wrong_season` as known problems. That is
     Finding 5, one layer up. It is latent in the fixture, whose clean films are
     all well named. The test failed first, and passes with `CLASS_KINDS`
     applied.
   - **`EscalateExpectation` accepted `require_finding = false`.** Fixed. Curated
     cases are hand-written, so Defect 2 could reopen one file at a time. Both
     flags are now `Literal[True]`, and `min_candidates` has a floor of
     `MIN_AMBIGUITY_CANDIDATES`. Three tests failed first and pass now.
   - **A `/guids` postcondition did not forbid the injected id.** Fixed
     2026-09-30. `contains` checked only that the ground-truth ids were present.
     On `fake:1:106`, whose ground truth has no ids, that was `contains []`,
     which passed on the corrupted world. On `fake:1:104`, an item carrying both
     the correct id and the donor's passed every hard predicate, though Plex
     re-derives metadata from the wrong one on refresh. `Expect` now carries
     `excludes`, the ids the corruption injected. A validator refuses it on any
     predicate but `contains`, and refuses an id both required and excluded.
     `expectation_for` takes the corrupted value as a required argument, so a
     caller cannot silently drop the exclusions. The two strict xfails now pass,
     along with eight new tests, including one that a rematch gaining a correct
     `plex://` id still passes. The fixture dataset stays byte-identical across
     hash seeds. The suite is at 772 passed, with nothing xfailed.
   - The fixture's `duplicate_quality` `known_other_problems` are genuine. The
     clean `fake:1:105` has only an unparseable legacy guid, so it fails
     `resolvable_id_present`, which guards both `wrong_match` and
     `filename_unmatchable`.
   - Where the code departs from this plan, it says so in `truth.py`:
     postconditions are keyed item → pointer rather than flat, because three
     classes modify more than one item, and the file floor is one derived rule
     rather than a per-class declaration.
   - Questions for 0.8, not defects here:
     - Does a value case's `item_ids` compare by equality or by overlap?
       `absolute_vs_seasonal` names the show as well as the episode, because its
       delta rewrites the show's soft `/child_count`.
     - Which items does `must_not_change` apply to? For a relation class, `/title`
       on the item the merge removes has no meaning.
     - Should `multi_file_split` hard-gate a part's `/index`? A merge alone may
       not restore a track number.
     - Should `contains` require an id in the `unknown` namespace? The clean
       `fake:1:105` holds only an unparseable legacy guid, so its `wrong_match`
       case requires `unknown://12345` back, which a real rematch may never
       restore.
     - How does the CI diff tell "the corruption changed" from "the rating keys
       moved"? `corruption_fingerprint` moves on every rescan (step 4, below).
3. ✅ **Composition and curated tests (0.6.4, 0.6.5).** Done 2026-09-30:
   `test_composition.py`, `test_curated.py`, and the step 3 half of
   `test_generate.py`, 47 tests. The suite is at 819. Measured along the way:
   - **Raising `--count` could shrink a cell.** Fixed. Largest remainder is not
     house-monotone, so `resolve` is now sequential Webster (§4.5, corrected). The
     test failed first, at --count 46.
   - **Curated cells reported `not_implemented`.** Fixed. `_deficit_reason`
     checked for a missing corruption function before checking for a curated
     slice. Nine empty real and ambiguous cells therefore pointed at step 1.1 when
     the waiting work is 0.9's. The fixture's tally moved from `not_curated ×27,
     not_implemented ×16` to `×36, ×7`. The test failed first.
   - **A negative share reported the wrong fix.** Fixed. `synthetic = 1, real =
     -1` hit the zero-total check first, which advises giving one share a positive
     value, and one already has one. Negatives are now checked first.
   - **Declaration order.** No counterexample in 28,000 trials, but the share total
     is now `math.fsum`, so independence from declaration order holds by
     construction. A permutation test pins it on the committed file.
   - **A lineage test at dataset level.** Editing a share keeps `lineage_id`,
     changes `composition_id`, and keeps every shared case's id and fingerprint.
     A minor generator bump keeps the lineage, and a major one resets it.
   - **Doc and code disagreements, corrected to match the code.** `CuratedCase`
     said a curated case with stale ids becomes a deficit row, but generation
     actually stops with a correctable error. `real.toml` said `unexpected` is "not
     writable per case", but it is, per `implementation-plan.md` §3. Both commented
     examples in the curated files are now tests, so they cannot drift from the
     schema that step 0.9's labellers will copy them into.
   - **Deferred to 0.9 (decided 2026-09-30).** The ambiguous slice inherits each
     medium's synthetic class mix. So `anthology_omnibus`, the class the slice
     exists for, resolves to 0 of 19 ambiguous cases at `--count 200` and 1 of 100
     at 1000. Meanwhile `wrong_match` and `duplicate_quality` get ambiguous
     targets. A labelled case beyond its cell's target is dropped as surplus. The
     options were to give the slice its own class table now, or to correct the
     `composition.toml` comment (which claimed the share "is actually spent"
     there) and let 0.9 set the mix from real ambiguous cases. The second was
     chosen, so the shares will not be guessed before any case exists. The
     comment now states what the resolver does, and the roadmap's 0.9 section
     carries the decision as a checkbox, so labelling cannot start without it.
4. ✅ **Generator property tests (0.6.6).** Done 2026-09-30: 17 tests in the
   second half of `test_generate.py`, bringing the suite to 836. **The gate's
   behavior is now asserted, not just observed.** The dataset is byte-identical
   across `PYTHONHASHSEED` 0 and 1 in forked processes. A larger `--count` is a
   superset of a smaller one, case for case. Every short cell has a deficit row,
   and an unfillable cell never re-draws from another. Measured along the way:
   - **Case ids survive a re-export.** With every rating key moved, all 25 case
     ids are unchanged, and every delta is identical once the keys are mapped
     back.
   - **For 0.8: `corruption_fingerprint` does not survive a re-export.** It
     hashes the delta, which carries rating keys, so a rescan moves it for 18 of
     the fixture's 23 synthetic cases although no corruption changed. The CI gate
     keys on `case_id` and is unaffected. But 0.8's `changed` bucket would fill
     with every case after every rescan unless it compares key-normalized deltas.
     The re-export test asserts the deltas' equivalence and deliberately not the
     fingerprint's.
   - **Excluded subjects were not counted per cell.** Fixed. Decision 5 and §4.5
     require it. The exclusion appeared only in the dataset-wide
     `excluded_subjects`, so a cell short because its candidates were excluded
     read as a library that had none. `CellResult.excluded_away` now counts them,
     the deficit detail names them, and they fall in the `capped` bucket as §4.5
     specifies. The test failed first.
   - **A duplicate curated label could go unnoticed.** Fixed. `case_id`
     uniqueness was asserted only on the cases each cell kept, so two copies of
     one label in a cell whose target is 1 lost the second as surplus. It is now
     asserted on the whole curated pool. The test failed first.
   - **The prefix test was given teeth.** On the shared fixture, and on an
     evenly weighted movie library, largest-remainder rounding drops no case at
     dataset scale: its shrinking cells are ones with no supply. The movie branch
     uses an uneven composition under which it drops a real case at --count 17.
     Run against that rounding, the test fails naming the case, and against
     Webster it passes. The composition-level monotonicity test is still the one
     that catches the rounding on any file.
5. **0.6.7**: `shelfwarden eval generate` calling `run_generate`, with a CLI test.
6. **§9 document updates**, the stale docstrings, and §4.5 corrected to five
   reasons. The roadmap's share was done 2026-09-29: the 0.6 checkboxes (`[x]`
   tested, `[~]` implemented and untested), the curated line changed to TOML, and
   the two 0.5 defects recorded. When steps 2–5 land, their `[~]` rows become `[x]`.
7. **Gate**: full suite, `ruff`, `lint-imports`, the roadmap ticked, one commit.
