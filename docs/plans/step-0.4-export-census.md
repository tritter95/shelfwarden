# Step 0.4 — Library Export + Census

Implementation plan for roadmap step **0.4 Library export + census**.
Written 2026-08-26 against commit `4abd04f`, with 0.1–0.3 complete and CI green
(223 tests, all offline).

Every finding in §2 was produced by reading and running plexapi 4.18.2 against the
committed fixtures in this repo. Three of them change the design; one of them
contradicts a promise step 0.3 made in its own handoff table.

**Gate for the step** (`roadmap.md`, `implementation-plan.md` §7): re-running the
export against an unchanged library is byte-identical, and the census informs
slice targets.

---

## 1. Scope

0.3 built a provider that can read a library. 0.4 is the step that turns a live,
mutable, network-dependent Plex server into **a file that does not change** — the
ground truth every later phase is measured against. Everything downstream (the
screen in 0.45, the corruptions in 0.5, the truth files in 0.6, `SnapshotLibrary`
in 0.7, the scorer in 0.8) reads this file and nothing else.

That makes determinism the whole job. A census that is merely approximate is a
nuisance; an export whose bytes drift is a silently moving baseline, and the
project's CI gate ("no case that passed may now fail") becomes decorative.

Deliverables:

- `config.py` — settings resolution and secret handling. Does not exist yet, and
  the export is the first command that needs a URL and a token.
- `evals/export.py` — deterministic slice selection, the walk, and the writer.
- `evals/census.py` — the counts, including guid namespaces with `UNKNOWN`
  examples, and the advisory per-class readiness table.
- `cli.py:export` — replacing the `NOT_IMPLEMENTED` stub.
- `library/base.py` — one added protocol method, `provider_info()` (Decision 3).
- `models/item.py` — `FilePart` gains `media_id` / `part_id` (Decision 4).
- A new import contract: `evals/` may not import `library/plex.py`.
- `tests/evals/` with a fixture-backed `FakeLibrary` and the byte-identity suite.

**Not in 0.4:** the mechanical screen and the shared comparators (0.45), any
corruption (0.5), any truth file (0.6), `SnapshotLibrary` (0.7). The census makes
*advisory* per-class counts; it does not verify that any item is free of a
problem. That distinction is 0.45's entire reason to exist and this step must not
blur it.

No new dependencies. `tomllib` is stdlib on 3.13.

---

## 2. Verified findings

### Finding 1 — `RELOAD_INCLUDES` does not describe what is actually requested

Step 0.3's handoff table promised 0.4 that "the export manifest records
`RELOAD_INCLUDES[FULL]` verbatim, so 'what produced this record' is data rather
than folklore." Recording that dict verbatim would be **an understatement of the
request**, which is worse than folklore because it looks authoritative.

`PlexPartialObject._INCLUDES` (plexapi `base.py:610`) carries nineteen keys.
`_buildDetailsKey` starts from all of them, overlays whatever kwargs `reload()`
was given, and **drops every key whose resulting value is `False`, `0`, or
`'0'`**. Our `RELOAD_INCLUDES` names eight. Eleven are left at their defaults, ten
of which are already `0` and vanish — and one of which is not:

```python
'includeFields': 'thumbBlurHash,artBlurHash',   # a string, never falsy
```

Running our real include sets through plexapi's own key builder, for every media
kind we map:

```
movie   core  /library/metadata/1701?includeFields=thumbBlurHash%2CartBlurHash
movie   full  /library/metadata/1701?checkFiles=1&includeFields=thumbBlurHash%2CartBlurHash
```

Two consequences:

1. The manifest must record the **effective** parameter set — what
   `_buildDetailsKey` produces — not our override dict. The honest value is
   computable without a server, so it costs nothing to be right.
2. CORE and FULL differ by **exactly one parameter**, `checkFiles=1`. See
   Finding 2.

`_INCLUDES` is defined once, on `PlexPartialObject`; no subclass overrides it, so
the effective set is uniform across movie / show / season / episode / artist /
album / track. Verified by building one of each from the committed fixtures.

### Finding 2 — `FetchProfile.FULL` buys nothing this model maps, and costs a stat per item

`checkFiles=1` asks the server to stat every file backing the item and annotate
`Part` with `accessible` / `exists`. `models.item.FilePart` maps neither. So
across our field set FULL and CORE are **identical in output** and differ in cost
by one filesystem check per part, server-side, per item.

Worse than useless: `accessible` is *volatile*. It flips when a mount drops. Had
we mapped it, the export's byte-identity would have become a function of whether
a NAS was awake. We do not map it, so we are safe — but exporting at FULL means
paying for an attribute whose only property is that it would have broken the
guarantee this step exists to provide.

One thing this finding does **not** settle: whether `checkFiles=1` can cause Plex
to *omit* a `Part` element for a file it cannot stat. That is unverified, and it
is a determinism hazard if true. It is a reason to prefer CORE rather than a
reason to investigate — see Decision 2 and §8.

### Finding 3 — guids come back on listings; lock state does not

`_buildSearchKey` sets `args['includeGuids'] = int(bool(kwargs.pop('includeGuids',
True)))` (plexapi `library.py:1266`), and `_buildQueryKey` hardcodes
`{'includeGuids': 1, **kwargs}` (`base.py:209`). So **every** listing response
already carries `<Guid>` children — a per-item fetch is not needed to see guids.

`<Field>` lock elements are a different mechanism entirely and arrive only from
the metadata endpoint. The per-item fetch in the export exists for lock state,
not for guids.

Note the trap in the naming: `includeFields` sounds like it governs `<Field>`
elements and does not — it is a blur-hash selector. Since `RELOAD_INCLUDES` never
touches it, lock capture behaves under our reduced include set exactly as it did
under the default one that produced the committed fixtures. Confirmed end to end
against `movie_new_agent.xml`, which carries `<Field name="title" locked="1"/>`
and `<Field name="summary" locked="0"/>`:

```
locked_fields: ('title',)
```

`Field.locked` is `utils.cast(bool, data.attrib.get('locked'))`, and
`cast(bool, None)` returns `None` — so a `<Field>` with no `locked` attribute
reads as unlocked. That is the right reading, and `_locked_fields` filtering on
truthiness already does it.

### Finding 4 — a `FilePart` has no identity

The committed fixtures carry stable numeric ids that the mapping discards:

```xml
<Media id="9" container="mkv" videoResolution="1080" duration="7740000">
  <Part id="10" file="/media/Movies/Amélie (2001)/Amélie (2001).mkv" .../>
</Media>
```

`FilePart` records `path`, `container`, `video_resolution`, `size_bytes`,
`duration_ms` — and no id. Three things want one:

- **Export ordering.** Parts are emitted in XML order. Plex's ordering of `Media`
  elements for a multi-version movie is not a documented guarantee, and there is
  no way to prove stability offline. Recording the ids makes any instability
  diagnosable from the diff instead of mysterious.
- **`filename_unmatchable` (0.5)** rewrites `part.file`. Its truth record has to
  name *which* part, and `parts[2]` is a positional identifier — precisely the
  kind the project's invariant 9 rejects.
- **Phase 3's rename** is a filesystem operation against one specific part, and
  its revert has to find that same part again.

Like `rating_key`, these are database row ids: they are an **address, not an
identity**, and they move if an item is removed and re-added. That is exactly the
caveat `ItemId`'s docstring already carries, and it is fine — the export is a
point-in-time snapshot and everything in it is addressed the same way.

### Finding 5 — the manifest cannot be produced through the protocol

`implementation-plan.md` §3 specifies a manifest carrying "server machine id,
section ids and agents, plexapi version, exporter git sha, reload-includes used."

`LibraryProvider` exposes `sections()`, `list_items()`, `get_item()`,
`get_children()`, `get_files()`, `find_similar()`. There is no way to ask it what
server it is talking to. `PlexServer.machineIdentifier`, `.version`, and
`.platform` exist (plexapi `server.py:124–153`) but are reachable only by
reaching through `PlexLibrary._server` — which would make `evals/export.py` depend
on the Plex adapter concretely, forfeiting the offline byte-identity test and
pre-breaking `SnapshotLibrary`.

### Finding 6 — `PYTHONHASHSEED` is the determinism hazard the existing tests cannot see

Everything the model serializes is already ordered: `canonical_json` sorts keys,
`sort_external_ids` sorts guids, `locked_fields` is `sorted(set(...))`. The census
is new code and is nothing *but* aggregation, and the natural implementations —
`Counter.most_common()` with ties, iterating a `set` of namespaces, `dict`
insertion order from a set comprehension — are stable within a process and
unstable across processes with different hash seeds.

A same-process "run it twice" test cannot catch this. The determinism test has to
fork with an explicit `PYTHONHASHSEED` on each side.

---

## 3. The design

### 3.1 `config.py` — the first module that needs a secret

Precedence per practices §9: environment > `~/.config/shelfwarden/config.toml` >
defaults. Never a CLI argument — argv lands in shell history and process listings.

```python
@dataclass(frozen=True, slots=True)
class Settings:
    plex_url: str | None
    plex_token: str | None
    export_dir: Path = Path("datasets/exports")

    def __repr__(self) -> str:          # the token never renders, not even here
        ...

def load_settings(env: Mapping[str, str] | None = None,
                  config_path: Path | None = None) -> Settings: ...

def require_plex(settings: Settings) -> tuple[str, str]:
    """Both, or a correctable error naming the two env vars."""
```

`SHELFWARDEN_PLEX_URL` / `SHELFWARDEN_PLEX_TOKEN`, with `PLEX_URL` / `PLEX_TOKEN`
accepted as aliases since `scripts/capture_fixtures.py` already established them.

`redact(text, settings)` replaces any configured secret with `<redacted>`, and a
test asserts no byte of any export output matches a configured token. That is
practices §3.4 applied one step earlier than the evidence store.

### 3.2 `library/base.py` — `provider_info()`

```python
@dataclass(frozen=True, slots=True)
class ProviderInfo:
    provider: str            # "plex" | "snapshot"
    server_id: str           # sha256(machineIdentifier)[:16] -- stable, not identifying
    server_version: str | None
    platform: str | None
```

Added to the protocol. It is a read method, so `MUTATING_METHODS` disjointness is
unaffected and the 0.3 gate test keeps passing unchanged.

The machine identifier is **hashed, not stored**. It is a durable server-unique
handle; the hash preserves everything the manifest actually needs (are these two
exports from the same server?) and discards the part that identifies it.
`scripts/capture_fixtures.py` already scrubs it from fixtures — this keeps the two
consistent rather than having one file scrub what another records verbatim.

`SnapshotLibrary` (0.7) returns `provider="snapshot"` with the dataset id as
`server_id`, which is what lets a re-export of a corrupted dataset carry the same
manifest shape.

### 3.3 `models/item.py` — part identity

```python
class FilePart(BaseModel):
    media_id: str | None = None   # Plex Media element id -- an address, not an identity
    part_id: str | None = None    # Plex Part element id
    path: str
    ...
```

Recorded, **not sorted on**. Order carries meaning — disc order, `multi_file_split`
in 0.5 consumes it — and reordering would destroy information to buy a guarantee
that offline tests cannot prove anyway. The ids make an unstable order visible in
a diff, which is the actual need. A validator asserts part ids are unique within
an item where present, so a duplicated `Media` block fails loudly.

### 3.4 `evals/export.py`

**Depends only on `LibraryProvider`.** Never on `PlexLibrary`. This is what makes
the byte-identity test offline, and it is enforced by an import contract rather
than by intent (§3.7).

**Selection is a plan, computed before anything is fetched, and recorded.**

1. `sections()`. Partition into supported and skipped. A skip is never silent: an
   `artist` section that fails audiobook detection is recorded with the verdict's
   own `explain()` string, a `photo` section with the taxonomy message.
2. For each supported section, page the **root kind** (`movie` / `show` /
   `artist`) to exhaustion via `list_items`, both paging arguments as always. This
   is the population census, and it costs `ceil(N/page)` requests of stubs.
3. Sort every stub by the canonical key
   `(section_id, kind_rank, numeric_key_or_string)` — numeric rating keys sort
   numerically, non-numeric ones sort after, lexicographically. Deterministic and
   readable, and it does not depend on Plex's sort order being stable.
4. Allocate quotas across sections proportional to population by the
   largest-remainder method, ties broken by `section_id`. `--all` skips this.
5. Select within each section with `random.Random(seed).sample(stubs, k)` over the
   **sorted list**. Re-sort the selection by the canonical key afterwards, so the
   RNG chooses membership and never ordering.

**The unit of selection is a family, not an item.** A root plus all its
descendants: show → seasons → episodes, author → audiobooks → parts, movie alone.
`episode_wrong_season` needs a show whose other episodes are present to be
solvable at all; `author_name_variant` needs an author's whole shelf. Half a show
is a corrupt ground truth that would silently depress the score in 0.8, so:

> If a family would push the record count past `--max-records`, the **whole
> family is dropped** and recorded in `manifest.dropped`. Never truncated.
> Iteration continues — a later, smaller family may still fit, and the family
> order is deterministic, so which ones fit is reproducible.

`--count` therefore counts **roots**. The manifest reports roots and total records
separately, because they differ by an order of magnitude on a TV library and
`--count 200` meaning 200 movies or 3,400 episodes is exactly the kind of
ambiguity that produces a wrong `composition.toml`.

**The walk** fetches each root and each descendant at the chosen profile via
`get_item`, and pages children with `get_children`. Errors mid-walk:

| Condition | Behaviour |
|---|---|
| `LibraryItemNotFound` on a member (rating key moved mid-export) | drop the family, record it in `manifest.dropped` with the reason, continue |
| `LibraryUnsupported` on a section | record in `manifest.skipped_sections`, continue |
| `LibraryUnavailable` / `LibraryRateLimited` surviving the session's retries | abort the whole export, exit `ERROR`, write nothing |

The last row is deliberate. A partial export that looks complete is the single
worst artifact this step could produce.

**The write is atomic.** Build into a temp directory, `os.replace` into place. An
interrupted export leaves nothing rather than something plausible.

Output layout — a directory per export, not the flat `<timestamp>.jsonl` the
implementation plan sketched, because there are now three files that must travel
together:

```
datasets/exports/2026-08-26T14-02Z/
  items.jsonl        # one canonical-JSON NormalizedItem per line, family-grouped
  manifest.json      # canonical JSON
  census.json        # canonical JSON
  census.md          # the human-readable table -- what you read to set composition.toml
```

**Record order** is `(section_id, root_key, kind_rank, item_key)` — family-grouped
rather than kind-grouped. A human reading the JSONL sees a show followed by its
seasons and episodes, and 0.5 operates on families. The order rule is named in the
manifest so a future reader does not have to infer it.

### 3.5 The manifest

```jsonc
{
  "schema_version": 1,
  "export_id": "exp-9f2a41c8d3b7",          // sha256(items.jsonl)[:12] -- content-addressed
  "created_at": "2026-08-26T14:02:11Z",     // volatile; excluded from the identity comparison
  "shelfwarden_version": "0.1.0",
  "git_sha": "4abd04f", "git_dirty": false, // "unknown" + dirty:true outside a clean checkout
  "plexapi_version": "4.18.2",
  "provider": {"provider":"plex","server_id":"a4f1…","server_version":"1.41.0.1234",
               "platform":"Linux"},
  "profile": "core",
  "request_params": {"includeFields":"thumbBlurHash,artBlurHash"},   // EFFECTIVE -- finding 1
  "record_order": "section_id, root_key, kind_rank, item_key",
  "selection": {"mode":"sample","seed":1518,"requested_roots":200,"max_records":5000,
                "per_section":[{"section_id":"1","population":2431,"quota":120}]},
  "sections": [{"section_id":"1","title":"Movies","section_type":"movie",
                "agent":"tv.plex.agents.movie","population":2431,
                "exported_roots":120,"exported_records":120,"audiobook_verdict":null}],
  "skipped_sections": [{"section_id":"4","title":"Music","reason":"…verdict.explain()…"}],
  "counts": {"roots":200,"records":1847,"by_media_kind":{"movie":120,"show":12,"season":41,…}},
  "dropped": [{"root":"plex:2:9931","title":"…","records":312,
               "reason":"would exceed max_records (5000)"}],
  "items_sha256": "…", "census_sha256": "…"
}
```

`created_at`, and only `created_at`, is expected to differ between two runs of an
unchanged library. The determinism test compares `items.jsonl` byte-for-byte and
the manifest with that one field lifted out — a rule stated once, in a named
constant, rather than a growing list of exceptions.

### 3.6 `evals/census.py`

Two tiers, each labelled with its own basis, because conflating them makes the
numbers unfalsifiable:

- **Population** — exact, from the listing walk. Sections, agents, media kinds,
  totals. Covers every item in every supported section.
- **Exported slice** — from the records actually fetched. Everything that needs a
  full item: guid namespaces, containers, resolutions, lock state, field presence.
  Every block carries `coverage: {n, of}`.

```jsonc
{
  "schema_version": 1,
  "population": { "sections":[…], "by_media_kind":{…}, "by_agent":{…} },
  "exported": {
    "coverage": {"records":1847,"population":11204},
    "by_media_kind": {"movie":120,"episode":1204,…},
    "guid_namespaces": {
      "tmdb": {"items":118,"ids":118},
      "unknown": {"items":3,"ids":3,"distinct_forms":2,
                  "examples":["com.plexapp.agents.plexmovie://…"],
                  "examples_truncated":false,"examples_dropped":0}
    },
    "items_without_guids": {"count":4,"by_media_kind":{…},"examples":["plex:1:1701"]},
    "containers": {"mkv":96,"m4b":340}, "video_resolutions": {"1080":88,"4k":30},
    "locked_fields": {"title":6,"summary":2},
    "field_presence": {"summary":{"present":115,"absent":5},"has_thumb":{…}}
  },
  "readiness": [ {"problem_class":"duplicate_quality","eligible":3,
                  "basis":"same normalized (title, year) in one section",
                  "advisory":true} ]
}
```

The `unknown` block is the point of the whole exercise. 0.2 wrote parsers for
legacy guid forms **against no live legacy-agent library** and said so; this is
where the real forms are counted instead of guessed at. `examples` is capped, and
the cap reports what it dropped — the "no silent caps" house rule, which is
otherwise exactly the rule a census violates by accident.

**Every aggregation sorts by an explicit total order** — count descending, then key
ascending. Not `Counter.most_common()`, whose tie order is insertion order and
therefore hash-seed dependent (Finding 6).

`readiness` is **advisory and labelled as such on every row**. It answers the
roadmap's "the census informs slice targets" and the implementation plan's open
question about whether the audiobook slice can carry six problem classes. It uses
cheap structural heuristics only — same `(normalized title, year)` pair in a
section, a season with more than 30 episodes, an audiobook with `part_count > 1`,
an item with no resolvable external id. It does **not** verify that any item is
free of a problem. That is the mechanical screen in 0.45, it needs the shared
comparators that do not exist yet, and confusing the two would let an advisory
count leak into a `no_action` label.

`census.md` renders the same data as tables. It is what a human reads before
editing `composition.toml`, so it is a deliverable, not a nicety.

### 3.7 The new import contract

```toml
[[tool.importlinter.contracts]]
name = "the export depends on the provider protocol, not the Plex adapter"
type = "forbidden"
source_modules = ["shelfwarden.evals"]
forbidden_modules = ["shelfwarden.library.plex"]
```

`evals/` may import `library/base.py` freely — the protocol is the whole point. It
may not import the adapter. This is what keeps the byte-identity test offline
permanently rather than until someone needs `machineIdentifier` in a hurry, and
it is `library.plex`'s own confinement rule pointed the other way.

The existing global `plexapi` contract already means a stray `import plexapi` in
`evals/export.py` breaks CI, since only `shelfwarden.library.plex` is ignored.

### 3.8 The CLI

```bash
shelfwarden export [--count N | --all] [--seed N] [--max-records N]
                   [--section ID]... [--profile core|full] [--out DIR]
                   [--census-only]
```

`--census-only` walks listings and writes `census.json` + `census.md` with the
population tier only, no per-item fetches. It is cheap, it is the command you run
*first* to choose `--count`, and it needs no decisions from the operator. Defaults:
`--count 200`, `--seed 1518` (matching the spec's own examples), `--max-records
5000`, `--profile core` (Decision 2).

Exit codes reuse the existing enum: `OK` on success, `ERROR` on an aborted export.

---

## 4. Decisions

### Decision 1 — the export is written against `LibraryProvider` (**recommended**)

`export.py` takes a provider, never constructs one. The CLI wires `PlexLibrary` in.

The cost is the extra plumbing in §3.2 to get server identity through the
protocol. Three things are bought: the byte-identity test runs offline against a
fixture-backed fake, `SnapshotLibrary` (0.7) gets the export for free, and the
0.7 conformance suite has a second real consumer of the protocol to exercise
rather than only the tests.

Reject only if the manifest turns out to need something genuinely Plex-shaped
that no snapshot could ever supply. It does not.

### Decision 2 — export at `CORE`, with `--profile full` available (**recommended**)

Finding 2: FULL differs by `checkFiles=1`, maps to no field this model carries,
costs a server-side stat per part, and its only observable output is an attribute
that would have broken byte-identity had we mapped it.

Against: "the ground truth should be everything we know how to ask for", and 0.3
declared `RELOAD_INCLUDES[FULL]` the export's profile. Both are real, and both are
answered by the profile being a flag that is recorded on every record and in the
manifest. `FetchProfile` exists precisely so that "nobody asked" and "not there"
stay different facts; picking the cheaper profile is fine as long as the record
says which one it was, and it does.

A secondary reason: Phase 1's `get_item_details` will run at CORE. An export at
CORE compares to it directly, with no profile-shaped asterisk on every diff.

### Decision 3 — add `provider_info()` to the protocol (**recommended**)

Finding 5. The alternative — the CLI reads `PlexLibrary._server` and passes a
`ProviderInfo` into the export — keeps the protocol smaller at the cost of making
the manifest's provenance optional and adapter-specific, and of a private
attribute access in the one place that is supposed to model a clean seam.

It is a read method. The 0.3 gate test (`protocol_methods(LibraryProvider) &
MUTATING_METHODS == ∅`) passes unchanged, which is worth confirming rather than
assuming.

### Decision 4 — `FilePart` records `media_id` and `part_id`, order preserved (**recommended**)

Finding 4. Recorded because 0.5 and Phase 3 both need to name a part and
`parts[2]` is a positional identifier. Order preserved because it carries meaning
that `multi_file_split` consumes, and because sorting would buy a determinism
guarantee no offline test can actually verify.

This is a `models/item.py` change landing in 0.4 rather than 0.2. Per the house
convention it gets a recorded correction in `implementation-plan.md` with the
reasoning, exactly as the `author` media kind did.

Consequence to schedule: every fixture-based test that compares canonical JSON
gains two fields. Mechanical, but it is not a zero-diff change.

### Decision 5 — `--count` counts roots; over-budget families are dropped whole (**recommended**)

A family is the smallest unit that is solvable. The alternative — count records,
truncate at the boundary — produces shows missing their last four episodes, which
is an unsolvable case in 0.5 and a mysteriously depressed score in 0.8. That is
the same failure the detectability witness exists to prevent, arriving one step
earlier.

Dropped families are recorded with their record count and reason. The manifest
reports roots and records separately.

### Decision 6 — the guid census is slice-scoped, and says so

The guid namespace counts come from exported records, so they cover the slice, not
the library. Every block carries `coverage: {n, of}` and the census names its basis
per tier.

Considered and rejected: putting `guids` on `ItemStub` so the population walk could
count them library-wide. Listings *do* carry them (Finding 3), so it would work —
but `ItemStub` is the payload `list_library_items` returns to the model on every
turn, and practices §0 is explicit that a verbose tool result is a recurring tax.
Inflating the agent's context to improve a census is the wrong trade.

The operator's lever is `--section`: the population census names each section's
agent, so a legacy-agent section can be exported specifically, and the guid
question is about legacy sections in the first place.

### Decision 7 — one directory per export, three files, atomic

`items.jsonl` + `manifest.json` + `census.json` + `census.md` must travel together
and are cross-referenced by hash. A directory makes that structural. Written via
temp-dir-then-`os.replace`, so an interrupted export leaves nothing rather than
something that looks finished.

---

## 5. Tasks

| # | Task | Files | Done when |
|---|---|---|---|
| 1 | Settings + secret handling | `config.py`, `tests/test_config.py` | env > toml > default precedence tested; a token never appears in `repr`, logs, or any output file |
| 2 | `ProviderInfo` on the protocol | `library/base.py`, `library/plex.py`, `tests/library/test_base.py` | `PlexLibrary.provider_info()` hashes the machine id; the mutating-method disjointness test still passes |
| 3 | `FilePart` identity | `models/item.py`, fixtures, `tests/models/test_item.py` | ids round-trip; duplicate part ids within an item are rejected; existing fixture comparisons updated |
| 4 | Effective include set | `library/plex.py` | A function returns what `_buildDetailsKey` actually produces per profile; a test pins `includeFields` being present (finding 1) |
| 5 | `FakeLibrary` test double | `tests/evals/conftest.py` | Implements `LibraryProvider` from the committed XML; serves one movie section, one show family, one author family |
| 6 | Selection plan | `evals/export.py` | Same seed → same selection; different seed → different selection; quotas sum to the request; ties broken by `section_id` |
| 7 | The walk + error handling | `evals/export.py` | Each row of the §3.4 error table has a test; an aborted export writes nothing |
| 8 | Writer + manifest | `evals/export.py` | Atomic; `export_id` is the content hash; `request_params` is the effective set |
| 9 | Census | `evals/census.py` | Population and slice tiers labelled; unknown guids counted with capped, self-reporting examples; every aggregation explicitly ordered |
| 10 | `census.md` renderer | `evals/census.py` | Tables render from `census.json` alone, so a stored census stays readable |
| 11 | CLI | `cli.py`, `tests/test_cli.py` | `export` replaces the stub; `--census-only` needs no item fetches; the 0.4 `NOT_IMPLEMENTED` assertion is removed |
| 12 | Import contract | `pyproject.toml` | `evals` → `library.plex` forbidden; `lint-imports` green |
| 13 | Determinism suite | `tests/evals/test_export.py` | §6 |
| 14 | Doc updates | `implementation-plan.md`, `development-practices.md`, `roadmap.md`, `architecture.md` | Findings 1, 2, and 4 recorded; §4.3 gains the effective-include-set note; the roadmap 0.4 block links here and is checked off |

Task 14 is the same obligation 0.1–0.3 discharged. Finding 1 in particular
contradicts a sentence 0.3 wrote in its own handoff table; leaving both standing
would send the next reader to re-derive it.

---

## 6. Test plan

The gate is one assertion. Everything else supports it.

```python
def test_export_is_byte_identical():
    """Two exports of an unchanged library differ in exactly one manifest field."""
```

Supporting, in rough order of how much they would hurt to lack:

```python
def test_export_is_byte_identical_across_hash_seeds()      # subprocess, PYTHONHASHSEED 0 vs 1
def test_selection_is_stable_under_a_seed()
def test_selection_changes_with_a_different_seed()         # or the seed is decorative
def test_an_over_budget_family_is_dropped_whole_and_reported()
def test_a_family_is_never_partially_exported()
def test_skipped_sections_are_recorded_with_their_reason()
def test_manifest_records_the_effective_request_params()   # pins finding 1
def test_lock_state_survives_the_round_trip()              # the Phase 3 dependency
def test_part_ids_round_trip_and_order_is_preserved()
def test_census_counts_unknown_guids_with_examples()
def test_census_example_cap_reports_what_it_dropped()      # no silent caps
def test_census_population_and_slice_tiers_are_labelled()
def test_no_output_file_contains_a_configured_secret()
def test_an_aborted_export_writes_nothing()
def test_export_never_imports_the_plex_adapter()           # belt to the contract's braces
def test_provider_protocol_still_has_no_mutating_method()  # the 0.3 gate, re-run after §3.2
```

The hash-seed test deserves a note. `pytest` runs in one process, so a same-process
"export twice, compare" test cannot see hash-order leakage at all — it would pass
against code whose census key order is hash-seed dependent, and CI would then be
green while two developers' exports differed. Forking with an explicit
`PYTHONHASHSEED` on each side is the only version of this test that means anything.

`FakeLibrary` builds from the committed XML fixtures, whose paths are already
scrubbed to `/media/...` by `scripts/capture_fixtures.py`. So the fixture export
carries no real paths and could be committed if a later step needs a stable
example dataset — `datasets/` is git-ignored, so that would be a deliberate
addition, not an accident.

### Exit checklist

```bash
uv sync --locked --group dev
uv run ruff check . && uv run ruff format --check .
uv run lint-imports          # now with the evals -> library.plex contract
uv run pytest -q
```

- [ ] All four pass locally and in CI, with no network access from any test
- [ ] Two exports of the fixture library differ in exactly one manifest field
- [ ] The same holds across two different `PYTHONHASHSEED` values
- [ ] Lock state is present on exported records and asserted
- [ ] The census reports unknown guid namespaces with examples, and reports its own caps
- [ ] `census.md` is readable and its numbers are sufficient to edit `composition.toml`
- [ ] Findings 1, 2, and 4 are recorded in the docs they contradict
- [ ] `roadmap.md` 0.4 is `[x]` and 0.45 can begin

### The live sanity check (not CI)

```bash
uv run shelfwarden export --census-only            # read this before choosing --count
uv run shelfwarden export --count 200 --out /tmp/e1
uv run shelfwarden export --count 200 --out /tmp/e2
diff /tmp/e1/items.jsonl /tmp/e2/items.jsonl       # expected: clean
```

Worth stating plainly: this can legitimately differ. `updated_at` moves when Plex
refreshes metadata in the background, and rating keys move on rescan. "Unchanged
library" is a stronger condition than it sounds, which is exactly why the *gate*
runs against the fixture provider and this is a sanity check. A diff here is
information about the server, not a failing test — and reading it is how we learn
what actually drifts before 0.6 keys baselines on it.

---

## 7. What this hands to later steps

| Step | Inherits |
|---|---|
| 0.45 | The export to screen, and a census that already says which classes have enough candidates to be worth screening. The `readiness` heuristics are the strawman the real comparators replace. |
| 0.5 | Family-grouped records, so `episode_wrong_season` and `author_name_variant` have whole families to work on; `part_id` so `filename_unmatchable`'s truth record names a part rather than a position; `locked_fields` already on every record. |
| 0.6 | `census.md` is what `composition.toml` is written from — the implementation plan's own instruction that targets be chosen from evidence. `export_id` and the manifest hashes feed `lineage_id`. The per-class `readiness` counts are the first honest answer to the open question about the audiobook slice. |
| 0.7 | `SnapshotLibrary` implements `provider_info()` alongside the rest of the protocol, so a re-export of a corrupted dataset produces the same manifest shape and the round trip is checkable. |
| Phase 3 | Lock state captured and proven to round-trip, and parts addressable by id — the two things `revert` needs that are painful to retrofit. |

---

## 8. Risks and open questions

- **`checkFiles=1` and missing `Part` elements.** Unverified: whether Plex omits a
  `Part` for a file it cannot stat. If it does, a FULL export's bytes depend on
  mount state. Decision 2 makes CORE the default, which sidesteps it; anyone
  reaching for `--profile full` should verify this first.
- **Part ordering stability is asserted, not proven.** No offline test can prove
  Plex's `Media` element order is stable across requests. Recording the ids makes
  a violation diagnosable from a diff, which is the honest ceiling here.
- **The guid census sees the slice, not the library** (Decision 6). If the census
  reports zero `UNKNOWN` namespaces, that is weak evidence, not proof — 0.2's
  legacy parsers stay unvalidated until a legacy section is exported specifically.
  Worth checking `--census-only` output for a section whose agent starts with
  `com.plexapp.agents.` before concluding anything.
- **`readiness` is one blurred line away from being wrong.** It counts structural
  candidates; it does not verify absence of a problem. If a later step reads a
  `readiness` count as a `no_action` label, the should-not-touch slice becomes
  unfalsifiable — the exact defect `implementation-plan.md` §3 records as Defect 3.
  Every row is flagged `advisory: true` in the JSON for that reason.
- **Full-section listing costs grow with the library, not the slice.** The
  population census pages every supported section to exhaustion. That is stubs
  only and it is what makes the population tier exact, but on a very large library
  it is the dominant cost of `--census-only`. If it becomes a problem the answer is
  to record a sampled population tier with its coverage stated — never to drop the
  tier and let the slice numbers pass as library numbers.
