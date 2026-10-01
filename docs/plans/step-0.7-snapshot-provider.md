# Step 0.7 — The Snapshot Provider

Implementation plan for roadmap step **0.7 Snapshot provider**. Written 2026-09-30
against commit `3e2563e`. Steps 0.1–0.6.6 are complete and the suite is at 836
passing. 0.6.7 (`shelfwarden eval generate`) is still open, and nothing here
depends on it.

Every finding in §2 was produced by running code in this checkout (CPython
3.13.12, plexapi 4.18.2), or by reading the installed plexapi source at a cited
line, and each says which. Three are defects in code that already shipped:
`PlexLibrary` leaks exceptions and fabricates an id (Finding 1), three of 0.5's
recipes build worlds Plex cannot serve (Finding 5), and CI does not exclude the
`live` tests its marker promises to (Finding 8). One changes what the agent is
allowed to see (Finding 4).

**Gate for the step** (`roadmap.md`): a provider-conformance test suite passes
against both implementations.

---

## 1. Scope

0.6 produced a dataset: a clean export plus one delta per case. 0.7 turns a case
into a **library**. The agent can list, page, fetch, and search it through
`LibraryProvider`, and nothing on the protocol says whether a Plex server is
behind it.

This is the first step where the corrupted records meet the expectation that they
look like something Plex would serve, and most of the findings below are about
the gap between the two. The world the agent sees is the measurement instrument
for every number Phase 1 reports. If it carries a tell that no real Plex server
produces, the eval measures how well the agent reads the tell.

Deliverables:

- **`library/snapshot.py`** — `SnapshotLibrary`, an in-memory provider over a
  fixed record set. It also holds the three named functions that model Plex's
  server-side ordering and matching (Decision 6).
- **`evals/world.py`** — builds a case's world from an export and a dataset. It
  binds the two, applies the delta, checks integrity, and assigns served
  addresses. It exposes `WorldBuilder`, `world_for_case`, and a `python -m`
  integrity report.
- **`tests/library/fake_plex.py`** and **`tests/library/test_conformance.py`** —
  an offline Plex server at plexapi's `query()` seam, and one property suite that
  runs against every provider. A `live` mode runs the same suite against the real
  server.
- **`PlexLibrary` hardening** — the behaviors in §4.2 that are undefined or wrong
  today.

Supporting changes, each forced by a finding:

- `library/base.py` — `LibraryInvalidArgument` (correctable), one shared check for
  paging arguments, and the section/kind vocabulary that exists in three copies
  today.
- `models/hierarchy.py` — the parent/child kind tables and `DERIVED_COPIES`: the
  fields Plex derives from the hierarchy rather than stores (Finding 5).
- `evals/corrupt/registry.py` — a derived-copy propagation pass, and a
  `world_incoherent` rejection (Finding 5).
- `evals/truth.py` — `/grandparent` and `/grandparent_title` join `SOFT_FIELDS`,
  and a child whose only changes are derived copies is no longer a finding's
  subject (Finding 6).
- `evals/corrupt/reverse.py` — `apply_changes` checks `before`, and
  `apply_reverse` checks `after` (Finding 7).
- `models/item.py` — `Page` validates its own counts.
- `models/ids.py` — `item_sort_key` moves here from `evals/export.py`. The library
  needs it and must not import `evals`.
- `tests/evals/conftest.py` — `FakeLibrary`'s divergences fixed. It joins the
  conformance suite as a third subject.
- `pyproject.toml` gains two import contracts, and `.github/workflows/ci.yml`
  actually excludes `live` tests (Finding 8).

Like 0.6, this step fixes defects it inherited: three in 0.5's recipes and three
in `PlexLibrary`. Each fix ships with a test that fails against today's code
first.

**Not in 0.7:**

- `MutableLibraryProvider` — Phase 3.
- The tools that wrap the provider — 1.3.
- The eval runner, which builds one world per case and hands the agent its
  provider — 1.7.
- The scorer — 0.8. It consumes `Addressing`, but is not written here.
- A CLI command. Only tests and the runner read a world, and
  `python -m shelfwarden.evals.world` covers inspection.
- New dependencies, and network access in CI.

---

## 2. Verified findings

### Finding 1 — `PlexLibrary` leaks two exception types past its taxonomy, and fabricates an id

`_translates_errors` catches `LibraryError` and the families in
`FOREIGN_EXCEPTIONS`, and nothing else. That is deliberate: a `TypeError` from our
own code is a bug and should surface as one. But three things reachable from
ordinary arguments fall through the gap. Probed against
`PlexLibrary(server=StubServer)` with the committed `movie_new_agent` fixture:

```
get_item(plex:1:1, FetchProfile.STUB)  -> KeyError: <FetchProfile.STUB: 'stub'>   (plex.py:496)
get_item(plex:1:sw3f9a)                -> ValueError: invalid literal for int()   (plex.py:487)
get_files(plex:1:abc)                  -> ValueError: invalid literal for int()
get_children(plex:1:abc, 0, 10)        -> ValueError: invalid literal for int()
get_item(snapshot:1:1)                 -> LibraryItemNotFound (correctable)       -- correct
```

The non-numeric key matters because the model types item ids into tool
arguments. Today a typo crashes the loop where it should produce a correctable
error. `STUB` is different: it is a programming error the model cannot send.
`effective_request_params` already refuses it by name, but `get_item` reaches
`RELOAD_INCLUDES[profile]` first and raises a bare `KeyError` that names neither
the cause nor the fix.

The third defect is quieter, and worse, because the call succeeds:

```
get_item(plex:999:1701)  -> MovieItem(item_id=plex:999:1701)   # the film is in another section
```

`_fetch` ignores `section_id`, because Plex rating keys are server-global, and
`_normalize` then stamps the caller's `section_id` onto the result (plex.py:498).
An agent that passes a wrong section gets back a well-formed id for a section the
item is not in. CLAUDE.md already explains why that is wrong, in its note on
recording `/parent` whole: half of an `ItemId` is an id whose section no longer
matches its key.

The check is available. `fetchItems` copies the container's `librarySectionID`
onto every item it builds (plexapi `base.py:359–362`). The committed fixtures
lack it only because the `KEEP` list in `scripts/capture_fixtures.py` strips it.

### Finding 2 — the protocol's edge behavior is undefined, and the obvious implementation answers it with plausible wrong data

Neither provider validates paging arguments, so `PlexLibrary` does whatever the
server does with them. From plexapi's `fetchItems` (`base.py:336–341`):

- `container_start = container_start or 0`. A negative offset is truthy, so it
  goes on the wire as `X-Plex-Container-Start: -1`.
- `container_size = min(container_size, maxresults)`. A negative `maxresults`
  puts a negative container size on the wire. The loop exits after one request
  with whatever the server sent.
- `maxresults=0` gives `container_size=0`, one count-only request, and an empty
  result with the total intact. This case is well defined and useful.

`FakeLibrary`, the in-memory provider the test suite already has, shows what
slicing does with the same inputs. Probed over its eight-movie section:

```
offset=-1 limit=100   -> returned 1  ['108']            # the LAST item
offset=0  limit=-1    -> returned 7  ['101' .. '107']   # all but the last
offset=0  limit=-3    -> returned 5
offset=50 limit=10    -> returned 0, total 8            # correct
get_files(fake:1:999) -> KeyError                       # not LibraryItemNotFound
```

Python's negative-index slicing turns a caller error into a believable page, and
a `SnapshotLibrary` written the obvious way would inherit all of it. "The same
pagination semantics as `PlexLibrary`" means nothing while `PlexLibrary`'s are
undefined. The first job is to define them; §4.2 is the table.

### Finding 3 — a default listing and `find_similar` return root kinds only, and the fake disagrees

`PlexLibrary.list_items` defaults `media_kind` to the section's root kind and
passes it as `libtype`. `find_similar` passes **no** `libtype` at all, so
`_buildSearchKey` (plexapi `library.py:1259–1292`) emits
`/library/sections/{key}/all?includeGuids=1&title=…` with no `type`. The answer
is whatever Plex returns for an untyped `/all`, which is the section's primary
type. So on an audiobook section, `find_similar` finds **authors**, never books.

`FakeLibrary` does neither:

```
list_items(SHOWS, 0, 100)          -> kinds {episode, season, show}, total 7
find_similar(SHOWS, "Session", 10) -> [episode, episode, episode]
find_similar(BOOKS, "Part", 10)    -> [audiobook_part, audiobook_part]
```

The export never noticed, because it always passes `media_kind`. But the export's
byte-identity test runs against `FakeLibrary`, which makes `FakeLibrary` the
suite's stand-in for Plex. A stand-in that disagrees with Plex wherever the
export happens not to look is a test double nobody can rely on. The protocol has
**three** implementations, not two, and the suite runs against all three
(Decision 9).

The root-kind default and the partial match on `title=` come from plexapi's
source and docstring. Whether that match is sensitive to case, to accents, or to
`titleSort` is **server** behavior, and no offline test can settle it
(Decision 6).

### Finding 4 — the served world would carry the answer in its addresses

Three classes add items, and 0.5 mints their rating keys as
`"sw" + sha256(…)[:10]` (`context.MINTED_PREFIX`) — "deterministic, and visibly
synthetic", by design (step 0.5, §4.5). In the fixture dataset
(`generate --count 200 --seed 1518`, 25 cases):

```
minted ids appear in:   duplicate_quality, author_name_variant, multi_file_split
null media_id/part_id:  duplicate_quality   (movies.py:410-411 clears them on the clone)

resolution author_name_variant  keeper fake:3:401  minted members ['fake:3:swde205235d4']
resolution multi_file_split     keeper fake:3:411  minted members ['fake:3:swea3728f407']
resolution duplicate_quality    keeper None        minted members ['fake:1:swa7dff4c095']  (x5)
```

`derive_resolution` defines the keeper as *the member of the relation that
existed before the corruption* (truth.py:735). Every added member is minted. So
for `author_name_variant` and `multi_file_split`, **the keeper is the only member
whose rating key is a number**. Served verbatim, the world states the answer in
its addresses. No Plex server produces a rating key containing letters, or a
`Part` with no id.

The opposite hazard is the live server. Export rating keys *are* the user's real
Plex keys. A world served under `provider="plex"` holds addresses that are also
live addresses, and `PlexLibrary._fetch` accepts any id whose provider is `plex`.
Nothing mutates today, so this is harmless today. In Phase 3, a plan recorded
during an eval run and handed to the live provider would edit a real item. The
composite `ItemId` exists to prevent exactly this — "snapshot and live ids can
never collide" (`ids.py`, step 0.2) — but the guard only holds if the snapshot
uses a different provider label. Verified: `PlexLibrary.get_item(snapshot:1:1)`
already raises `LibraryItemNotFound`.

The dataset should keep `sw`. 0.5 chose it so a human reading `truth.json` can
see which item was minted, and that is still worth having. The leak exists only
in what the agent can read.

### Finding 5 — three recipes produce worlds Plex cannot serve, and two of them leak the answer

Plex derives some fields from the hierarchy when it answers a request:

- an episode's `grandparentTitle` is its show's current title;
- a track's `grandparentRatingKey` is its album's artist;
- an artist's `childCount` is its album count.

A recipe that edits one record and leaves the derived copies on its neighbours
produces a library no server could return. Applying each fixture case's delta to
the export, and checking the derived copies against the hierarchy:

```
wrong_match (donor_same_section) on a show:
  fake:2:211  parent_title 'Cowboy Bebop' != 'Pilot Only'     # the season still names the true show
  fake:2:2211 grandparent_title 'Cowboy Bebop' != 'Pilot Only'
multi_file_split:
  fake:3:401  album_count 2 != 3 children                     # the split added an album
clean export, and therefore every world:
  fake:3:412  part_count 1 != 0 children                      # a quirk of the hand-built fixture
```

The fixture misses the third defect. `author_name_variant` moves books to the
minted variants, but leaves each moved book's **parts** with `grandparent` still
set to the original author. The one book it moves in the fixture, Edgedancer, has
no parts. On a hand-built author with four books of one part each:

```
inverted INCOHERENT: fake:3:621 grandparent fake:3:601 but its book fake:3:611 belongs to fake:3:sw9d992b72a2
inverted INCOHERENT: fake:3:623 grandparent fake:3:601 but its book fake:3:613 belongs to fake:3:sw9d992b72a2
```

Two of these leak the answer. Every season of a wrong-matched show names the
show's true title, and every part of a re-homed book points at the canonical
author. On a real library, `author_name_variant` almost always moves a book that
has parts, so the fixture's miss is luck, not coverage.

The `fake:3:412` line is a different kind of fact. It is in the clean export, so
it is in every world, including the should-not-touch ones. A world must not be
held to a coherence its own source never had (Decision 7).

### Finding 6 — fixing those recipes in place collides with two truth rules

The fix is to propagate the derived copies, and then the delta carries the
propagated changes. Reading `truth.py` shows two consequences:

- **`field_tier` raises.** It raises on any path that is in neither tier table,
  deliberately: "a twelfth class that touches something new should stop here".
  `/grandparent` and `/grandparent_title` are in neither table, so the fix stops
  there, as designed. Plex derives both, which puts them in `SOFT_FIELDS` beside
  `/parent` and `/parent_title`.
- **The finding would have to name every episode.** For a value-witness class,
  `required_finding.item_ids` is *every pre-existing item the delta modified*
  (truth.py:800). With titles propagated, a `wrong_match` finding on a show would
  be required to name every season and every episode. A season whose only change
  is inheriting its show's new title is not what went wrong, so it is not what a
  finding is *about*.

0.6 met a neighbouring case and left it open for 0.8: `absolute_vs_seasonal`
names the show, because its delta rewrites the show's `/child_count` (step 0.6,
§10 item 2). That question is about a family's *root*, and it stays 0.8's. The
rule in Decision 8 is written so that it does not move.

### Finding 7 — a delta applies to any world, without checking it is the right one

Going forward, `_apply` (reverse.py:111) writes `field.after` at each path; in
reverse it writes `field.before`. It never reads what is already there. Applied
to an export other than the one it was recorded against, a delta produces a
hybrid record and raises nothing.

Within 0.5 and 0.6 that cannot happen, because each delta is applied to the items
it was diffed from. 0.7 is the first consumer to apply a delta to a world loaded
from a *separate* file. The `items_sha256` binding (§4.4) prevents the mismatch.
The check inside `_apply` makes a binding bug, or a hand-edited `deltas.jsonl`,
raise instead of serving a silently wrong world.

### Finding 8 — the `live` marker says "never runs in CI", and nothing enforces it

`pyproject.toml` registers
`live: touches a real external service; never runs in CI`. But in `ci.yml`, the
per-commit job runs `pytest -m "not slow"`, and the nightly job runs `pytest -q`
with **no filter** — deliberately, so the slow tests run. No `live` test exists
yet, so nothing has broken. 0.7 adds the first ones, and they would run nightly:
they would either skip silently for want of a server, or reach for one.

### Finding 9 — rebuilding a world per case is cheap enough not to cache

Measured on 5,000 synthetic movie records, the export's default `--max-records`:

- relabelling every record's `item_id` through `load_item` takes **38 ms**;
- rendering the world to canonical JSONL takes **63 ms** (4.1 MiB).

At 130 cases that is about 13 s for a whole dataset, against an agent run that
costs seconds of model latency per case. So the simple design — build each world
from the parsed export, and hash its bytes — needs no incremental scheme. The
first real export confirms the number; §11 records it when it exists.

---

## 3. Decisions

### Decision 1 — `SnapshotLibrary` serves records; the case world is built on the evals side (**recommended**)

0.6 §4.7 fixed the interface as
`SnapshotLibrary.for_case(export_directory, dataset, case_id)`. Taken literally,
`library/snapshot.py` would read `deltas.jsonl` and import `apply_changes` from
`evals.corrupt`. That has two costs. The library package would import the
answer-key package, carrying `evals/` across the Phase 5 MCP seam. And the object
the agent holds would hold a dataset path.

So the work splits:

- **`library/snapshot.py`** — `SnapshotLibrary(records, sections, info, profile)`.
  It knows nothing of datasets, deltas, or truth. It refuses a record set no
  library could be (§4.3, structural rules) and serves the rest.
- **`evals/world.py`** — `WorldBuilder.open(export_directory, dataset_directory)`,
  then `.world(case_id) -> CaseWorld`, with `world_for_case(...)` as the one-shot
  form of 0.6's signature. It binds, applies, checks, and addresses.

`CaseWorld` holds `provider`, `addressing`, `world_id`, and the provenance ids.
The runner (1.7) passes `provider` alone to the agent.

Two new contracts enforce the split:

- **The library does not import the answer key**
  (`shelfwarden.library` ↛ `shelfwarden.evals`). The direction becomes a CI
  failure.
- **The agent cannot name a concrete provider**
  (`shelfwarden.agent` ↛ `library.plex`, `library.snapshot`). Architecture.md's
  "the agent cannot tell which world it is in" stops being a claim about code
  and becomes a property of the import graph.

### Decision 2 — the agent sees Plex-shaped addresses; one `Addressing` map translates (**recommended**)

From Finding 4: the dataset and the world speak different address spaces, and
one function maps between them.

- **Provider.** Relabelled to `snapshot` on every `ItemId` — `item_id`, `parent`,
  `grandparent`. `PlexLibrary` already refuses it.
- **Rating keys.** Decimal keys from the export are kept verbatim, so a world
  stays diffable against its export by eye. Every non-decimal key — in practice,
  every minted `sw…` key — gets a fresh decimal key above the world's largest,
  assigned in `item_sort_key` order. That reproduces the one signal real Plex
  does carry: the newest item has the highest key.
- **Part and media ids.** A `None` id on an item the delta **added** is minted
  the same way, above the world's largest part or media id. A `None` already
  present in the export is real data, and is left alone.

`Addressing` is a bijection over the world's ids. The world builder builds it and
`CaseWorld` holds it; the provider never does. The scorer (0.8) uses it to
translate the agent's finding ids. The dataset itself is unchanged: `truth.json`,
`deltas.jsonl`, and the `sw` keys are exactly as 0.6 writes them.

**Rejected: fix it at the source, by minting decimal keys in `context.mint`.**
That would change every delta containing an ADD, and cost the dataset the
legibility 0.5 chose `sw` for. Neither file is agent-facing, so neither is where
the leak is.

**Recorded, not solved:** the label `snapshot` is itself visible to the model if
tool payloads render `str(item_id)`. Whether 1.3's tools carry the provider
component is 1.3's decision (§9).

### Decision 3 — the world is the slice plus the delta, not the population

0.6 §4.7 also says the snapshot "derives the population index with `stub_of`".
It does not need one.

`roots.jsonl` is the *screen's* input. It exists so that a uniqueness claim
states its population (practices §11.11). A library is different: every record
it lists must be fetchable. Listing population roots that `items.jsonl` does not
hold would serve stubs whose `get_item` fails. No Plex server does that, and the
conformance suite rejects it (§4.6, P7).

So `list_items` totals count the world, not the library the export came from.
That is consistent with every truth expectation:

- A should-not-touch item guarded against `duplicate_quality` has no twin in the
  population, and therefore none in the world.
- An item whose twin lies outside the slice fails the clean screen, so the class
  lands in `known_other_problems`. It is neither required nor penalized, and the
  agent loses nothing by not seeing the twin.
- A `duplicate_quality` case's clone is an ADD, so it is in the world.

### Decision 4 — undefined behavior is defined in code, identically in every provider (**recommended**)

Findings 1–3 show that the protocol's edge cases are currently whatever the
server, or Python slicing, makes them. §4.2 is the table. The shared parts live
in `library/base.py`; the rest is implemented in each provider. Two principles
decide every row:

- **Bad input the model can send is a `LibraryError`.** Values that originate in
  tool arguments — an item id, a section id, `offset`, `limit`, a title, a media
  kind — produce a correctable error whose `next_action` says what to send
  instead. That needs one new class, `LibraryInvalidArgument`, `CORRECTABLE`.
  `LibraryRequestError` is the wrong home: it is terminal, and it means *the
  server refused*.
- **A programmer error stays a Python exception.** A `FetchProfile.STUB` request
  is a bug in our code, not something the model can send. Both providers raise
  `ValueError` naming the fix. That matches `effective_request_params`, and the
  reasoning in the comment on `FOREIGN_EXCEPTIONS`.

`limit=0` stays legal in both. It is Plex's count-only query, and the snapshot
answers it the same way. There is still no upper bound on `limit`: Plex has
none, and a cap would be a silent one.

### Decision 5 — the snapshot answers exactly the profile its export holds

An export records `fetched` on every record and `profile` in its manifest.
`FakeLibrary` restamps whatever profile is requested onto its record. A snapshot
doing the same would claim a `checkFiles=1` request that never happened.

That looks harmless today, because FULL maps no extra field (0.4 Finding 2). But
0.4 also records an open risk: Plex may omit a `Part` it cannot stat. If so, a
CORE record restamped as FULL, or a FULL record restamped as CORE, can disagree
with what the server would have returned.

So `get_item(profile)` returns the record when `profile` is the held one, and
otherwise raises `LibraryUnsupported` (terminal), naming the held profile.
Exports default to CORE and the tools will ask for CORE, so the refusal costs
nothing anyone uses. It appears among the suite's declared divergences (§4.6)
rather than being papered over.

### Decision 6 — the serving model is three named functions, verified two ways (**recommended**)

Some of what a snapshot must reproduce is server behavior: the order of an
untyped `/all`, what `title=` matches, the order of `/children`, and the order of
`/library/sections`. None of it is in plexapi, and none of it can be checked
offline, but the snapshot still has to pick an answer. So the answers become
**named pure functions** in `library/snapshot.py` rather than inline sort keys:

```python
def listing_key(record) -> tuple: ...        # (fold_text(title_sort or title), item_sort_key(item_id))
def children_key(record) -> tuple: ...       # (index is None, index or 0, item_sort_key(item_id))
def title_matches(query, record) -> bool: ...   # fold_text(query) in fold_text(record.title)
```

They are verified against Plex in two steps that do not overlap:

1. **Offline: client logic agrees with the model.** `tests/library/fake_plex.py`
   serves XML in the order these functions define, filtered by `title_matches`.
   The suite then checks that `PlexLibrary`'s own paging loop, totals, kind
   handling, and error translation reproduce the model's answers. It also checks
   that a snapshot of an export of that fake server agrees with the server, item
   for item. For server semantics this step is circular by construction, and it
   says so.
2. **Live: the model agrees with Plex.** The same suite runs under `-m live`
   against the real server. It applies the three functions to what Plex returns
   and asserts agreement. A disagreement is a counterexample: the function
   changes, not the test.

Step 2 exists for the open questions: whether `title=` folds accents, and whether
it matches `titleSort` or `originalTitle`. The model starts at `fold_text` —
case-insensitive and accent-sensitive — because that is the narrower claim.

### Decision 7 — world integrity: structural rules are absolute, derived copies are relative to the source (**recommended**)

Finding 5 shows two kinds of fact, so there are two tiers.

**Structural rules**, enforced by `SnapshotLibrary.__init__`, which refuses to
construct. No Plex response can violate these:

- ids are unique;
- every id carries the same provider label, and it is not a live one;
- every record's section exists and allows the record's kind;
- every `parent` resolves within the world, to the right kind, in the same
  section;
- `grandparent` is the parent's parent;
- a root kind has no parent, and every other kind has one;
- no `part_id` appears on two items;
- every record carries the held profile.

**Derived copies**, checked by the world builder *relative to the source export*.
`models/hierarchy.py` declares `DERIVED_COPIES`, a table from field to its
derivation over the world:

| Field | Derived as |
|---|---|
| `/parent_title` | the parent's `title` |
| `/grandparent` | the parent's `parent` (also a structural rule) |
| `/grandparent_title` | the grandparent's `title` |
| `/child_count` (show) | the number of seasons |
| `/leaf_count` (show) | the number of episodes |
| `/album_count` | the number of audiobooks |
| `/part_count` | the number of parts |

The rule is **a world may not be less coherent than its export**. A disagreement
the export already had, like `fake:3:412`, is counted and reported but
tolerated: it is a fact about the source, and a real library may have some. A
disagreement the delta introduced fails the build. A `None` copy means "not
reported", and is never a disagreement.

`/parent_index` is deliberately absent from the table. `truth.py` treats it as
the primary, hard field, from which Plex re-derives `/parent`, so a table
claiming the reverse would contradict a settled decision. Both corruptions that
move episodes set the two fields together anyway.

### Decision 8 — recipes stop producing incoherent worlds: propagate, then gate (**recommended**)

Three hand fixes would close Finding 5 and leave a twelfth recipe free to repeat
the mistake. Instead, `registry.attempt` gains one pass, which runs after the
recipe returns and before `diff_items`. For each field in `DERIVED_COPIES`, on
each item in the corrupted family:

- **An item that existed before**, whose copy agreed with the hierarchy before
  and disagrees now: set the copy to the derived value.
- **An item the recipe added**, with a non-`None` copy: set the copy to the
  derived value. Plex computes these fields for a new item.
- **Anything else:** leave it alone. That keeps a pre-existing quirk out of the
  delta, which must describe the corruption and nothing else.

The propagated changes are then read back from dumps, like every other change
(practices §11.15).

A new acceptance check, **`world_incoherent`**, runs between `reverse_mismatch`
and `witness_indiscriminate`. It applies the structural rules, and the relative
derived-copy rule, to the corrupted family against its ground truth, and rejects
into `rejected.jsonl` with the rule named. A world that cannot exist should be
refused before anyone asks whether it is solvable. A recipe whose breakage the
propagation cannot cover — a future derived field, or a structural break — is
then refused with a reason instead of shipped.

Two `truth.py` changes follow from Finding 6:

- `/grandparent` and `/grandparent_title` join `SOFT_FIELDS`.
- `required_finding` excludes from a value class's `item_ids` any **non-root**
  item whose every change is a derived copy.

The root exception is what leaves every existing case's `item_ids` untouched.
Today, every non-root item a delta modifies carries at least one hard change, and
`absolute_vs_seasonal`'s show is a root. 0.6's open question about naming the
show stays open for 0.8. The derived-copy postconditions stay in
`soft_postcondition`, where 0.8 reports them.

One guard: if propagation touches a path that the recipe's witness cites,
`attempt` raises `CorruptionError`. The witness was built before propagation, so
it would describe a different world. No current witness cites a derived copy,
and the guard keeps it that way.

### Decision 9 — three implementations, one suite; `FakeLibrary` stays a fake *live server*

Rebasing `FakeLibrary` on `SnapshotLibrary` is tempting, and wrong. `FakeLibrary`
stands in for a server, which answers any profile. A snapshot stands in for an
export, which answers one (Decision 5). `test_export.py:354` exports
`FakeLibrary` at FULL.

So `FakeLibrary` keeps its role and its fault injection, and joins the
conformance suite as a third subject. Four divergences get fixed: the default
kind, negative slicing, the kinds `find_similar` returns, and `get_files` on an
unknown id.

It keeps insertion order. That is its documented purpose: the export must impose
its own order, and an order that differs from the export's proves it does.
Insertion order satisfies the suite's ordering properties — stable, total, and
pages partition the listing. The *model-agreement* properties apply only to the
snapshot and to the fake Plex server.

### Decision 10 — `live` tests are opt-in, and CI says so explicitly

A new `tests/conftest.py` skips every `live` test unless `--run-live` is passed.
It is a skip, not a deselect, so the count of tests not run stays visible. Both
CI jobs also add `-m "not live"`: a configuration mistake should be visible in
two places, not one.

The live subject reads `SHELFWARDEN_PLEX_URL` and `SHELFWARDEN_PLEX_TOKEN` (or
`PLEX_URL` and `PLEX_TOKEN`) through `config.load_settings`, exactly as `export`
does. It only reads, because the protocol offers nothing else.

---

## 4. Design

### 4.1 Modules

| Module | Change |
|---|---|
| `library/base.py` | `LibraryInvalidArgument`; `check_page(offset, limit)`; `SECTION_ROOT_KIND`, `SECTION_KINDS`; `LIVE_PROVIDERS`; a section-specific `next_action` for an unknown section |
| `library/plex.py` | §4.2's rows; `SECTION_TYPE_TO_KIND` replaced by the base vocabulary |
| `library/snapshot.py` | **new** — `SnapshotLibrary`, `listing_key`, `children_key`, `title_matches`, the structural rules, `WorldIntegrityError` |
| `models/hierarchy.py` | **new** — `PARENT_KIND`, `CHILD_KIND`, `DERIVED_COPIES`, `derived_violations(records)` |
| `models/ids.py` | `item_sort_key`, moved from `evals/export.py` and re-exported there |
| `models/item.py` | `Page` validates `returned == len(items)`, `offset >= 0`, `total >= 0` |
| `evals/world.py` | **new** — `WorldBuilder`, `CaseWorld`, `Addressing`, `world_for_case`, `__main__` |
| `evals/corrupt/registry.py` | the propagation pass; the `world_incoherent` check |
| `evals/corrupt/reverse.py` | `_apply` checks `before` going forward and `after` in reverse |
| `evals/truth.py` | two `SOFT_FIELDS` entries; the derived-copy exclusion in `required_finding` |
| `evals/export.py` | imports the moved vocabulary and `item_sort_key` |
| `scripts/capture_fixtures.py` | `librarySectionID` joins `KEEP` |
| `tests/conftest.py` | **new** — `--run-live` |
| `tests/library/fake_plex.py` | **new** — `FakePlexServer` |
| `tests/library/test_conformance.py` | **new** — the property suite, the differential, the round trip, `live` mode |
| `tests/library/test_snapshot.py`, `tests/evals/test_world.py` | **new** |
| `tests/evals/conftest.py` | `FakeLibrary`'s divergences fixed |

### 4.2 Edge semantics, identical in every provider

| Input | Plex today | Fake today | Defined as |
|---|---|---|---|
| `offset < 0` | sent to the server | the tail of the listing | `LibraryInvalidArgument`: "offset is ≥ 0; to start at the beginning, pass 0" |
| `limit < 0` | negative container size | drops items | `LibraryInvalidArgument` |
| `limit == 0` | count-only page | empty page | empty page with the true `total` (unchanged) |
| `offset ≥ total` | empty page (plexapi logs it) | empty page | empty page with the true `total` (unchanged) |
| `media_kind` not one of the section's kinds | sent to the server | empty page | `LibraryInvalidArgument`, naming the section's kinds |
| `media_kind=None` | the section's root kind | every kind | the section's root kind |
| unknown section | `LibraryItemNotFound`, with advice about item ids | empty page, total 0 | `LibraryItemNotFound`: "call sections() and use one of its ids" |
| non-decimal rating key | `ValueError` escapes | `LibraryItemNotFound` | `LibraryItemNotFound`, before any request |
| item is in a different section | **fabricated id** | `LibraryItemNotFound` | `LibraryItemNotFound`. A response with no `librarySectionID` is a `LibraryProtocolError`, never trusted |
| item in an unmodelled section, fetched by id | **reads a music track as an audiobook part** | not applicable | `LibraryUnsupported`, as a listing of that section is. Found while building 0.7.2 |
| foreign provider | `LibraryItemNotFound` | `LibraryItemNotFound` | unchanged |
| `get_children` on a leaf kind | sent to the server | empty page | empty page, `total=0`, after confirming the item exists |
| `get_files` on a non-leaf kind | `()` | `()` | `()` (unchanged) |
| `get_files` on an unknown id | `LibraryItemNotFound` | `KeyError` | `LibraryItemNotFound` |
| `find_similar` kinds | the section's root kind | every kind | the section's root kind |
| `find_similar` with a blank title | sent to the server | matches everything | `LibraryInvalidArgument` |
| `find_similar` with `limit < 0` | negative container size | drops items | `LibraryInvalidArgument` |
| `get_item(STUB)` | `KeyError` escapes | restamps the record | `ValueError`, naming the profiles that exist |

"Leaf kind" comes from `CHILD_KIND`, and "the section's kinds" from
`SECTION_KINDS`, so no provider holds its own copy of either.

### 4.3 `SnapshotLibrary`

```python
class SnapshotLibrary:
    def __init__(
        self,
        records: Sequence[NormalizedItem],
        sections: Sequence[SectionRef],
        info: ProviderInfo,
        profile: FetchProfile,
    ) -> None: ...
```

Construction validates the structural rules (Decision 7). It refuses any provider
label in `LIVE_PROVIDERS` (`{"plex"}`), which is declared in `base.py` beside the
protocol, and a test pins it to `library.plex.PROVIDER`.

It then builds four indexes, once:

- records by id;
- records per `(section, kind)`, sorted by `listing_key`;
- children per parent, sorted by `children_key`;
- the section map.

Every method is then a lookup plus a slice of a pre-sorted tuple, behind
`check_page`. Records are frozen pydantic models and are returned as they are: no
copy is needed, and none can be mutated. The class's public methods are exactly
the protocol's seven. The `MUTATING_METHODS` disjointness test is extended from
the protocol to the class.

`provider_info()` returns `ProviderInfo(provider="snapshot", server_id=world_id)`.
`server_version` and `platform` are `None`, because nothing honest can be said
about them. This **replaces** the `ProviderInfo` docstring's "the dataset id as
`server_id`". With per-case worlds, one dataset is many libraries, and
`server_id` answers *is this the same library?*

### 4.4 The world builder and `Addressing`

```python
builder = WorldBuilder.open(export_directory, dataset_directory)   # parses the export once
world = builder.world(case_id)                                     # a CaseWorld
world.provider     # SnapshotLibrary -- the only thing the agent receives
world.addressing   # Addressing -- dataset id <-> served ItemId, total over the world
world.world_id     # sha256(rendered served world)[:16]
```

`open` reads `dataset.json` — **not `truth.json`** — and binds:

- **The export must be the one the dataset was generated from.** `items.jsonl` is
  hashed from its bytes and compared with `dataset.source_export.items_sha256`.
  The manifest's own `items_sha256` is compared as well, because a stale manifest
  is exactly the failure this catches. A mismatch is refused, naming both hashes
  and the fix: regenerate the dataset from this export, or pass the export it was
  generated from.
- **A census-only export is refused by name.** It holds no items, and its world
  would score every case as silence.

`world(case_id)` then:

1. Streams `deltas.jsonl` to the case's line. No line, or more than one, is
   refused.
2. Applies the delta to the parsed export with `apply_changes`, which is now
   strict (Finding 7).
3. Runs the derived-copy rule relative to the export. Disagreements the export
   already had are tolerated, and counted on `CaseWorld.inherited_violations`.
   New ones raise `WorldIntegrityError`.
4. Builds `Addressing` (Decision 2) and the served records.
5. Constructs `SnapshotLibrary`, which applies the structural rules.
6. Hashes the rendered served world to get `world_id`. Measured cost is under
   0.1 s at 5,000 records (Finding 9).

Both kinds of integrity failure name the rule and the ids. When the delta
predates the propagation pass, they also say so and say to regenerate.

`python -m shelfwarden.evals.world <export> [<dataset>]` prints the integrity
report for the export alone, then for every case world in the dataset. It is the
exit checklist's command for the first real export (§6), and the first thing to
run when a recipe is suspected.

### 4.5 The fake Plex server

`FakePlexServer(PlexServer)` overrides
`query(key, method=None, headers=None, params=None, timeout=None, **kwargs)`. That
is the one seam that `PlexServer.__init__`, `library`, `fetchItems`, and `reload`
all go through. Its routes:

| Key | Answer |
|---|---|
| `/` | the root container: `machineIdentifier`, `version`, `platform` |
| `/library`, `/library/sections` | the section directories |
| `/library/sections/{id}/all` | items filtered by `type` (default: the root kind) and by `title` (with `title_matches`), sorted by `listing_key`, and sliced by the `X-Plex-Container-Start`/`-Size` headers. The container carries `totalSize`, `size`, and `librarySectionID` |
| `/library/metadata/{rk}` | one element, in a container carrying `librarySectionID`. An unknown key raises `plexapi.exceptions.NotFound`, as `query` does on a 404 |
| `/library/metadata/{rk}/children` | children by `parentRatingKey`, sorted by `children_key`, and sliced likewise |

Any key it does not route raises, as `StubServer`'s tripwire does.

The library it serves is assembled from the committed fixtures:

- four movies;
- a show with two seasons and three episodes;
- an audiobook author with two books and three parts;
- a music section;
- a photo section.

Where a second element is needed, it is a copy of a captured one, with the
changed attributes listed in a comment. There is no new capture, and no invented
attribute. Every query is recorded, so a test can assert what went on the wire,
as `RecordingServer` does today.

### 4.6 The conformance suite

One parametrized suite, `tests/library/test_conformance.py`, over subjects that
each declare what they hold:

```python
@dataclass(frozen=True)
class Subject:
    name: str                             # plex | snapshot | fake | live
    provider: LibraryProvider
    profiles: frozenset[FetchProfile]     # what get_item answers
    follows_model: bool                   # plex-over-fake, snapshot, live: yes; fake: no
```

Each property is a test over every section that `sections()` returns, and over
every page size from 1 to `total + 1`:

- **P1** — Invalid paging arguments raise `LibraryInvalidArgument` *before any
  request*, asserted against `FakePlexServer`'s query log.
- **P2** — `returned == len(items)`, `offset` echoes the request, and `total`
  depends on neither `offset` nor `limit`.
- **P3** — For every page size *k*, concatenating the pages from 0 reproduces the
  single-page listing exactly: no gap, no duplicate, the same order.
- **P4** — `offset ≥ total` and `limit == 0` each return an empty page with the
  true total.
- **P5** — The default kind is the root kind, and a kind foreign to the section
  is refused.
- **P6** — Two identical calls return byte-identical results.
- **P7** — Every listed stub is fetchable, and `get_item` agrees with it on kind,
  title, and year. A stub is a projection.
- **P8** — `get_children` returns exactly the items whose `parent` is the
  argument, pages as in P2–P4, and is empty for a leaf.
- **P9** — `get_files(x) == get_item(x).parts`, on the model's fields.
- **P10** — `find_similar` returns only root kinds of that section, at most
  `limit` of them, and each satisfies `title_matches` (model subjects only).
- **P11** — Every error in §4.2 has its declared type and retryability, and every
  correctable one has a `next_action`.
- **P12** — Taxonomy closure. An adversarial argument table — negative, zero,
  huge, non-decimal, foreign provider, wrong section, empty, NFD, garbage —
  reaches every method. Nothing escapes except a `LibraryError`, or the declared
  `ValueError` for `STUB`.
- **P13** — Every returned value is a model type: the walk from
  `TestConfinement`, generalized.
- **P14** — `provider_info().provider` equals the provider component of every
  served id.
- **P15** — Listing order equals `sorted(..., key=listing_key)`, and children
  order equals `children_key` (model subjects only).

Declared divergences, each asserted explicitly so it cannot drift silently:

- the snapshot lists only the sections its export holds, while Plex also lists
  its photo and music sections;
- the snapshot answers one profile;
- the snapshot's ids are relabelled.

Three tests go beyond the properties:

- **The differential.** Export `PlexLibrary(FakePlexServer)` in full, build a
  world with an empty delta, and compare every protocol answer between the two
  subjects through `Addressing`. This is the test that `SnapshotLibrary` serves
  an export the way its source served it.
- **The round trip.** Exporting a snapshot reproduces the original export's
  `items.jsonl` byte for byte, modulo addressing. 0.4's §7 promised this; here it
  becomes checkable.
- **Live.** P1–P15 against the real server, bounded and reported. Ordering is
  checked on the first two pages of each section. Title matching is checked on a
  fixed set of probes derived from listed titles: a case-flipped title, an
  accent-stripped title, and a match on `titleSort` only. Each reports how much
  it covered, so the bound is not a silent cap.

### 4.7 Import contracts

```toml
[[tool.importlinter.contracts]]
name = "the library does not import the answer key"
type = "forbidden"
source_modules = ["shelfwarden.library"]
forbidden_modules = ["shelfwarden.evals"]

[[tool.importlinter.contracts]]
name = "the agent cannot name a concrete provider"
type = "forbidden"
source_modules = ["shelfwarden.agent"]
forbidden_modules = ["shelfwarden.library.plex", "shelfwarden.library.snapshot"]
```

Both are honest today. Like the existing eight, each breaks CI at exactly the
step that would violate it.

---

## 5. Build steps

**0.7.1 — vocabulary and the argument error.** `SECTION_ROOT_KIND`,
`SECTION_KINDS`, `LIVE_PROVIDERS`, and `LibraryInvalidArgument` in `base.py`.
`models/hierarchy.py`, without `DERIVED_COPIES` yet. `item_sort_key` moved.
`Page` validation. `--run-live` and the CI filter.
*Done when:* the suite is still at 836 with no behavior change, `lint-imports`
passes, and a test of the `--run-live` hook shows a `live` test skipped without
the flag.

**0.7.2 — `PlexLibrary` hardening.** Every row of §4.2 that changes, each with a
test that fails against today's `plex.py` first. The rows marked "unchanged" get
a test that pins them. The commit names the four that matter most: the
fabricated id, the two escaping exceptions, and the unknown-section
`next_action`.
*Done when:* every row of §4.2 holds for `PlexLibrary` over a stub server.

**0.7.3 — `FakePlexServer`.** The routes in §4.5, and the assembled library.
*Done when:* `run_export(PlexLibrary(FakePlexServer()))` writes a complete export
offline, no unrouted query occurs, and a test pins the headers each paged call
sent.

**0.7.4 — `SnapshotLibrary`.** The class, the three model functions, and the
structural rules.
*Done when:* P1–P15 pass against a snapshot over hand-built records, and each
structural rule has a test showing construction refused.

**0.7.5 — integrity and the 0.5 fixes.** `DERIVED_COPIES`; strict `_apply`; the
propagation pass and `world_incoherent`; the two `truth.py` changes.
*Done when:*

- the coherence sweep (§6) **fails against 0.6's recipes** — on the four-book
  author, the fixture show, and the fixture split — and passes afterwards;
- the 0.6 property tests still hold;
- in the regenerated fixture dataset, deltas and fingerprints move only for the
  `wrong_match`-on-a-show and `multi_file_split` cases, and every `case_id` and
  every `item_ids` is unchanged.

**0.7.6 — the world builder.** `WorldBuilder`, `Addressing`, binding,
`world_id`, `__main__`.
*Done when:* every case in the fixture dataset builds a world that serves
`apply_changes(export, delta)` byte for byte through `Addressing`, and no served
key is non-decimal.

**0.7.7 — the conformance suite.** Subjects, properties, the differential, and
the round trip. `FakeLibrary`'s four fixes land here, each with its failing test
first.
*Done when:* the suite passes against all three offline subjects. **This is the
gate.**

**0.7.8 — contracts and documents.** The two contracts, §10's updates, and the
roadmap.
*Done when:* `ruff`, `lint-imports`, and the full suite are green, and the
results of §6's two by-hand checks are recorded in §11.

---

## 6. Tests

Beyond the conformance suite:

- **`test_no_served_address_reveals_the_keeper`** — for every relation case in the
  fixture dataset, every served rating key is decimal, so the keeper cannot be
  identified by its address. It fails against a world served with dataset
  addresses.
- **`test_added_parts_are_given_ids_and_existing_blanks_are_not`**.
- **`test_addressing_is_a_bijection_over_the_world`** and
  **`test_addressing_round_trips_every_id_a_truth_file_names`** — every id in
  every `item_ids`, `resolution`, and postcondition key maps to a served id and
  back.
- **`test_a_snapshot_address_cannot_reach_the_live_provider`** — `PlexLibrary`
  refuses every served id, before any request.
- **`test_the_provider_holds_no_ground_truth`** — a walk of `SnapshotLibrary`'s
  attributes reaches exactly the served world's records and nothing else. No
  modified item's pre-corruption bytes are reachable from the object the agent
  receives.
- **`test_a_world_is_refused_for_the_wrong_export`**, and likewise for a
  census-only export, an unknown case, and a duplicated delta line.
- **`test_a_delta_refuses_a_world_it_was_not_recorded_against`** — strict
  `_apply`, in both directions. It fails against today's `reverse.py`.
- **The coherence sweep** — runs every recipe over a library built to exercise
  propagation: an author whose moved books have parts, a show with seasons, and a
  splittable book. It asserts that `world_incoherent` never fires, and that
  `derived_violations` is empty relative to the source. It **fails against
  today's recipes**, in three ways.
- **`test_a_quirk_in_the_source_is_inherited_not_introduced`** — `fake:3:412`
  survives into every world, is counted, and does not fail the build.
- **`test_propagation_never_touches_a_witness_pointer`**.
- **`test_a_child_whose_only_changes_are_derived_is_not_a_finding_subject`** —
  `wrong_match` on the fixture show names the show alone, although its seasons
  and episodes are now in the delta.
- **`test_world_id_is_shared_by_identical_worlds_and_only_by_them`** — two
  `no_action` cases share one `world_id`.
- **`test_worlds_are_byte_identical_across_hash_seeds`** — every fixture world,
  in forked processes, under `PYTHONHASHSEED` 0 and 1 (practices §8.2).
- **`test_snapshot_class_exposes_no_mutating_method`**.

### Exit checklist

- [ ] The full suite is green, with the conformance suite parametrized over three
      offline subjects
- [ ] `ruff`, `ruff format --check`, and `lint-imports` are green, with ten
      contracts
- [ ] The fixture dataset has been regenerated. Only the Finding 5 cases' deltas
      and fingerprints moved, and every `case_id` is unchanged
- [ ] **Live, by hand, never in CI:**
      `uv run pytest -m live --run-live tests/library/test_conformance.py` against
      the real server. Record the result in §11, along with every model
      counterexample
- [ ] **Real export, by hand:** `uv run python -m shelfwarden.evals.world <export>`
      reports zero structural violations, and records the inherited derived-copy
      count. A structural violation in a real export is a fact about Plex, and
      Decision 7 is revisited before anything else

---

## 7. What 0.7 does not do

- **No mutation.** `MutableLibraryProvider`, and the snapshot's implementation of
  it, belong to Phase 3. Worlds are rebuilt per case, so the mutable form will
  start from the same builder.
- **No population serving** (Decision 3).
- **No restamped profiles** (Decision 5).
- **No tool layer.** Whether tool payloads show the provider label is 1.3's call
  (§9).
- **No change to the format of `truth.json` or `deltas.jsonl`, or to `case_id`.**
  Fingerprints move for the cases whose recipes changed. That is what the CI
  diff's `changed` bucket exists for.
- **No fix to 0.4's `select`**, which samples with `random.sample` over the
  server's listing order. It is noted in §8 rather than changed: the code belongs
  to the export, and the export's tests pin it.

---

## 8. Risks and open questions

- **The serving model is a guess until the live run.** The offline suite proves
  that `PlexLibrary` agrees with the model, not that Plex does. The places the
  guess is most likely wrong, most likely first:
  - whether `title=` folds accents, or matches `titleSort`;
  - how an untyped episode listing (`type=4`) is ordered;
  - how ties in `titleSort` are broken.

  Each has a live probe and a named function to change. Until the live run,
  `find_similar`'s fidelity is the weakest claim in this step, and it is the tool
  the agent uses to find duplicates.
- **`librarySectionID` is refused when it is absent.** That follows from
  plexapi's source, where `fetchItems` copies it from the container, and from
  Plex's container shape. If some real endpoint omits it, the live run fails
  loudly on the first `get_item`. That is the right way to find out. The fallback
  — compare against the listing the id came from — is recorded here so that it is
  not improvised.
- **The model can still tell which world it is in.** With decimal keys, two
  residual tells remain:
  - the `snapshot` label, which is 1.3's decision;
  - recipes that copy a record verbatim. A `duplicate_quality` clone has its
    original's `added_at` to the second, which no real duplicate does.

  The second is a recipe question, not an addressing one, so it is listed for 0.5
  rather than fixed here. Nothing yet measures whether a model *uses* any of these
  tells. 1.8's per-case diff between providers is the first instrument that
  could.
- **Existing datasets are refused after 0.7.5.** A dataset generated by 0.6's
  recipes fails the integrity rules for every `wrong_match`-on-a-show,
  `author_name_variant`, and `multi_file_split` case, and the error says to
  regenerate. The only dataset today is the fixture one, which the tests build
  fresh, so this costs nothing now. It would cost a baseline later.
- **`find_similar` cannot find a book by its title.** On an audiobook section it
  returns authors (Finding 3). That is faithful to Plex, and it limits
  `multi_file_split`. Adding `media_kind` to `find_similar` is a protocol change,
  and it belongs with 1.3's tool design, where the need is concrete.
- **The world is smaller than the library.** An agent that reasons from `total`
  sees 200 roots, not 4,000. Nothing in the truth file depends on the total, but
  a prompt that mentions library size would mislead.
- **The export's `select` depends on listing order.** `random.sample(list(stubs),
  quota)` draws membership in the server's order. So an export of a snapshot at
  `count < population` selects different roots than an export of Plex would. The
  round-trip test exports in full for that reason.

---

## 9. What this hands to later steps

| Step | Inherits |
|---|---|
| 0.8 | `Addressing`, to translate finding ids before comparing them with `truth.json`. Unchanged `item_ids`, plus soft postconditions on propagated children. The runner, not the world builder, should check the case's `corruption_fingerprint` against its delta, since the runner already holds the `Case`. |
| 1.3 | A provider whose every failure the model can cause is a `LibraryError`, with a `next_action` that can be shown to the model as written. The open question of whether tool payloads render the provider component. `find_similar`'s root-kind limit. |
| 1.6 / 1.7 | Call `WorldBuilder.open` once per dataset and `.world(case_id)` once per case, pass `provider` alone to the loop, and record `world_id` on the run. A non-CORE export cannot serve the tools' CORE requests, so the runner should refuse it at startup rather than on the first call. |
| Phase 3 | A world the mutable snapshot can start from, and an address space that a plan recorded during an eval run cannot carry to the live server. |

---

## 10. Documents to update in the same change

- **`roadmap.md`** — expand 0.7's checkboxes to match §1. Record the three 0.5
  defects as "fixed here", as 0.6 did. Add the `live` filter to CI's standing
  rules.
- **`implementation-plan.md`** — in §2 and in §7's row for 0.7, record that:
  - the world is built in `evals/`, not by a `SnapshotLibrary` classmethod;
  - served addresses differ from dataset addresses;
  - the world is the slice;
  - the snapshot answers one profile.

  §3's table gains the derived-copy propagation.
- **`development-practices.md`**:
  - §4.4 gains a summary of the edge-semantics table, and the `limit=0` count
    query.
  - §8.1 gains the `--run-live` rule.
  - §11 gains three invariants: *a world may not be less coherent than its
    source*; *the agent-facing world carries only addresses Plex could produce*;
    *the library does not import the answer key*.
- **`architecture.md`**:
  - §5: narrow "the agent cannot tell which world it is in" to what is enforced.
    Agent code cannot tell; the model can read a label unless 1.3 strips it.
  - §8: the `ItemId` paragraph names `Addressing`.
  - §9: the taxonomy gains `LibraryInvalidArgument`.
  - §13: the status row.
- **`CLAUDE.md`**, under *things that look wrong but are correct*:
  - served keys that differ from the dataset's;
  - `get_item(FULL)` refused on a CORE snapshot;
  - `FakePlexServer` importing the snapshot's ordering functions;
  - `FakeLibrary` keeping insertion order;
  - a derived-copy disagreement tolerated when the export already had it;
  - `/grandparent` being soft.
- **`library/base.py`** — `ProviderInfo`'s docstring: `server_id` is the world
  id, not the dataset id.
- **`step-0.6-truth-schema-generator.md` §4.7** — a one-line pointer to
  Decision 1 here, so its "no design left to do" does not mislead.
- **`pyproject.toml`** and **`ci.yml`** — §4.7 and Decision 10.

---

## 11. Status

**0.7.1 done, 2026-09-30.** The suite went from 836 to 879 passing; every
pre-existing test still passes. `ruff` and `lint-imports` are clean, with 8
contracts kept. The fixture export and the
`generate --count 200 --seed 1518` dataset are byte-identical to the same run
before the change: `items.jsonl`, `roots.jsonl`, `census.json`, `truth.json`,
`deltas.jsonl`, `dataset.json` and `rejected.jsonl`.

What landed:

- **The section vocabulary moved to one place.** `SECTION_ROOT_KIND` is in
  `library/base.py`. `SECTION_KINDS` is *derived* from it with
  `models.hierarchy.lineage`. `plex.SECTION_TYPE_TO_KIND` and the export's two
  copies are gone. The plan put the `plex.py` replacement in 0.7.2, but it is a
  pure rename and doing it here left one copy rather than four.
- **`models/hierarchy.py`** holds `CHILD_KIND`, plus `PARENT_KIND` derived as its
  inverse. Tests tie both to the item classes: a kind has a `parent` field
  exactly when it has a parent kind, and a `grandparent` field exactly when it is
  two levels deep.
- **`item_sort_key`** is in `models/ids.py`, unchanged. `reverse.py` imports it
  from there; `export.py` imports it for its own use and no longer defines it.
- **`LibraryInvalidArgument`** is correctable, with no default next action, and a
  test shows it cannot be constructed without one. **`check_page`** has no
  callers yet; 0.7.2 wires it in.
- **`LIVE_PROVIDERS`**, with a test pinning it to `library.plex.PROVIDER`.
- **`Page` validates its own counts.** `returned == len(items)`, and `offset` and
  `total` must be ≥ 0.
- **`live` is opt-in.** `tests/conftest.py` adds `--run-live`, both CI jobs
  deselect `live`, and `tests/test_live_marker.py` proves the hook in a nested
  pytester session. With the hook disabled, the default-skip test fails.
  Development practices §8.1 and §8.4 now describe this behavior.

Two things worth knowing:

- **A temporary gap until 0.7.2.** `PlexLibrary` given a negative offset now makes
  the request first, and then fails on `Page` construction with a pydantic
  `ValidationError` instead of returning a meaningless page. 0.7.2's
  pre-request `check_page` closes this. `FakeLibrary` behaves the same way until
  0.7.7.
- **The nested pytester session loads pytest-asyncio.** Its configure-time
  warning about an unset loop scope is fatal under the outer
  `filterwarnings = error`, so the nested ini sets
  `asyncio_default_fixture_loop_scope` exactly as `pyproject.toml` does.

**0.7.2 done, 2026-09-30.** The suite went from 879 to 936 passing. `ruff` and
`lint-imports` are clean. The fixture export and dataset are byte-identical to the
pre-0.7.1 run.

Every changed row of §4.2 now holds for `PlexLibrary`. The new tests were run
against `HEAD`'s `plex.py`, and **33 failed** — one per changed row and
parameter. The 13 that pin unchanged rows passed against both versions.

Three departures from the plan:

- **`tests/library/fake_plex.py` landed here, not in 0.7.3.** The id check
  depends on plexapi copying the response container's `librarySectionID` onto
  the item it builds. A stub that sets the attribute by hand would test our code
  against our own idea of plexapi. So the tests needed a real `PlexServer` with
  only `query` overridden, and that is the fake. It serves the committed fixtures:
  three movies; a show, season and episode; an author, book and part; a music
  track; and an empty photo section. Requests it does not route are test
  failures. It also refuses negative paging headers, and refuses to be asked for
  a leaf's children.

  What 0.7.3 still has to do:
  - add a second season and more episodes, for the paging properties;
  - export over the fake;
  - pin the headers;
  - switch the fake's ordering and title matching to the model functions once
    0.7.4 writes them.
- **A row the plan missed: item-level reads skipped the section check.**
  `get_item(plex:4:900)` returned an `AudiobookPartItem` for a track in the
  *music* section, which a listing of that section refuses. `_fetch` now applies
  `_require_supported` to the item's verified section, and §4.2 has the row.
- **`LibrarySectionNotFound`**, a subclass of `LibraryItemNotFound`, carries the
  advice about listing sections. Anything catching "that id does not exist" still
  catches it, and the advice it gives is now right for a section id.

The shared checks the snapshot will reuse live in `library/base.py`, each with
unit tests:

- `check_page`
- `check_search`
- `check_fetchable` — raises `ValueError` for `STUB`, a programming error, not a
  `LibraryError`
- `resolve_kind`

`find_similar` now sends the root kind explicitly. The kinds it returns no longer
depend on the server's default for an untyped `/all`.

`scripts/capture_fixtures.py` keeps `librarySectionID` from now on. The existing
fixtures were not re-captured, because the fake's response containers carry the
section id, as a real server's do. Development practices §4.4 now describes the
edge semantics and the section check.

The 0.7.1 gap is closed: a negative offset is refused before any request.

**0.7.3 done, 2026-10-01.** The suite went from 936 to 961 passing. `ruff` and
`lint-imports` are clean.

- **The fake's library is the one §4.5 describes:** four movies, a show with two
  seasons and three episodes, an author with two books and three parts, a music
  track, and an empty photo section. Every captured fixture is served under its
  captured rating key. The one exception is `movie_nfd_path`, whose key 1702 is
  `movie_legacy_agent`'s. It is served as 1704, which makes it a real duplicate
  of film 1701 and a title tie for the ordering properties.
- **Copies are data, not edited files.** Each `Entry` names a fixture and lists
  its changes, so a reader can tell captured attributes from invented ones.
- **The captured counts were restated.** They describe the captured library
  (`childCount="5"`, `leafCount="60"` on the show; `childCount="12"` on the
  author), not the fake's smaller one. Served as captured, the fake would
  contradict itself, and its export would fail 0.7.5's derived-copy rule for the
  fake's fault rather than the code's.
- **`tests/library/test_fake_plex.py` checks the fake as a library**, at the XML
  level, against the relations 0.7.5 will check on records: every parent served,
  in the same section, of the right type; grandparent equal to the parent's
  parent; denormalized titles and indexes agreeing with what they copy; counts
  agreeing with the items beneath them; and no media or part id on two items. It
  also checks each tripwire fires. The music track's album and artist are not
  served, and the exemption says why: nothing walks an unmodelled section past
  detection.
- **The gate:** `run_export(PlexLibrary(FakePlexServer()), count=None)` writes all
  6 roots and 16 records offline. It skips the music and photo sections, each
  with its reason, and names the server by its hashed identifier. Lock state and
  an NFD path survive the walk, and two runs are byte-identical. No unrouted
  query occurs; one would have raised through the export.
- **Paging headers are pinned** from the request side, through the server's
  query log:
  - a page is one request for exactly its window;
  - `limit=0` sends `Size: 0`;
  - a limit of 250 over four films is one request capped at 100, plexapi's
    container size;
  - over 205 films, `list_items(0, 150)` is exactly `(0, 100)` then `(100, 50)`,
    and `list_items(200, 100)` is one request returning five;
  - `get_children` pages the same way.

  So the practices doc's §4.4 claim — that both arguments stop plexapi's loop at
  the limit — is now a test rather than a reading of the source.

Four 0.7.2 tests had their expected values updated for the larger library: total
4 movies rather than 3, three episodes, and two seasons. The behavior they pin is
unchanged.

The fake's listing order is still insertion order, and its title match is still
a case-insensitive substring. 0.7.4 replaces both with the snapshot's model
functions, as planned.

Next: 0.7.4.
