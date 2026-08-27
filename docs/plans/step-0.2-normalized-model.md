# Step 0.2 — The Normalized Media Model

Implementation plan for roadmap step **0.2 Normalized media model**.
Written 2026-08-26 against commit `5530075`, with step 0.1 complete and CI green.

> **Status: implemented 2026-08-26.** All eight decisions were taken as
> recommended. 92 new tests; the suite is 122. The document is kept as the record
> of *why* the model looks the way it does — §2 is the part worth re-reading
> before changing a field type, adding a media kind, or touching the serializer.

Every finding in §2 was produced by running plexapi 4.18.2 and pydantic 2.13.4,
not from recollection. Two of them contradict what the surrounding documents
currently assume, and one exposes a gap in the roadmap's own list of media kinds.

**Gate for the step** (`roadmap.md`, `implementation-plan.md` §7): canonical-JSON
round-trip and **both** guid-form parsers are unit tested.

---

## 1. Scope

`models/item.py` is the vocabulary every later package speaks. Getting it wrong is
expensive in a specific way: the export (0.4) writes these records to disk, the
corruption functions (0.5) mutate them, the truth files (0.6) embed them, the
snapshot provider (0.7) serves them, and the scorer (0.8) compares them
byte-for-byte. A field that cannot represent a distinction here becomes a
distinction the harness cannot measure.

Deliverables:

- `canonical.py` — the one serializer, plus the text-normalization policy
- `models/ids.py` — `ItemId`, `ExternalId`, `IdNamespace`, and the guid parsers
- `models/item.py` — `MediaKind`, the item union and its subtypes, `FilePart`,
  `ItemStub`, `SectionRef`, `Page[T]`
- Tests for round-trip, both guid forms, and the determinism traps in §2

`pydantic` joins the default dependency group here. It is on the planned list in
`development-practices.md` §1.1 and introduces neither persistent state nor a
service, so the house rule's "ask first" does not bite.

**Not in 0.2:** the `LibraryProvider` protocol and any plexapi mapping (0.3), the
export (0.4), comparators and title normalization for *matching* (0.45), the
`subject_key` ladder (0.6). This step defines the shape; those steps fill and
consume it.

---

## 2. Verified findings

### Finding 1 — plexapi does no guid parsing whatsoever

`media.Guid` in its entirety:

```python
class Guid(PlexObject):
    TAG = 'Guid'
    def _loadData(self, data):
        self.id = data.attrib.get('id')
```

That is the whole class. `Video.guid` is likewise a bare string attribute. A
search of the entire package for `com.plexapp.agents` finds only docstrings
listing agent identifiers — no parser, no namespace enum, no helper. Every guid
form this project handles, we handle ourselves. The roadmap's dual-form parsing
requirement is not a nicety; it is the only parsing that will exist.

Verified new-agent shapes, from plexapi's own `getGuid` documentation:
`plex://show/5d9c086c46115600200aa2fe`, `imdb://tt0944947`, `tmdb://1399`,
`tvdb://121361`. Verified agent identifiers, from the `library.py` docstrings:
`com.plexapp.agents.imdb`, `com.plexapp.agents.themoviedb`,
`com.plexapp.agents.thetvdb`, `com.plexapp.agents.none`, and the new
`tv.plex.agents.movie` / `tv.plex.agents.series`.

### Finding 2 — "empty" and "not fetched" are the same value, and `autoreload=false` makes that permanent

`guids`, `fields`, `media`, `genres` and friends are `@cached_data_property`
readers over the cached XML — not eagerly assigned attributes. Whether they hold
anything depends on what the response contained. The reload trigger, from
`PlexPartialObject.__getattribute__`:

```python
if attr in _DONT_RELOAD_FOR_KEYS: return value
if attr.startswith('_'):          return value
if value not in (None, []):       return value      # <-- only None and [] reload
if self.isFullObject():           return value
if self._autoReload is False:     return value      # <-- our setting
```

Two consequences the plan already half-anticipates:

- Only `None` and `[]` trigger a refetch. With `autoreload=false` — which
  `implementation-plan.md` §2 mandates — an item fetched as part of a list
  returns `guids == []` and `fields == []`, and those are **indistinguishable
  from an item that genuinely has no external ids and no locked fields**.
- `missing_metadata` corruption, the 0.45 mechanical screen, and any
  "this item has no guid" finding all rest on that distinction.

So the model must record *what was fetched*, not merely what was found. See
Decision 3 — this is the single most consequential design choice in the step.

### Finding 3 — plexapi's `_EXCLUDES` are opt-in, and do not silently drop `Guid`

Worth stating precisely, because the constant reads alarmingly:

```python
_EXCLUDES = {'excludeElements': 'Media,Genre,Country,Guid,Rating,...', ...}
```

But `_buildDetailsKey` applies them only when the caller passes them explicitly:

```python
for k, v in self._EXCLUDES.items():
    value = kwargs.pop(k, None)
    if value is not None:            # absent unless the caller asked
        params[k] = ...
```

whereas `_INCLUDES` *are* applied by default, each with its default value —
markers, chapters, geolocation, bandwidths, loudness ramps. So the risk with a
default `reload()` is cost, not silent field loss, and step 0.3's explicit include
set is about turning expensive includes **off** while never passing
`excludeElements` (which would drop exactly the `Guid` and `Media` elements this
model is built on).

### Finding 4 — three determinism traps in the canonical serializer

The `canonical_json` recipe in `development-practices.md` §2.5, run as written:

| Input | Output | Problem |
|---|---|---|
| `"Amélie"` NFC vs NFD | `b'...Am\xc3\xa9lie'` vs `b'...Ame\xcc\x81lie'` | Same string to a reader, different bytes. macOS filesystems hand out **NFD** paths, and `part.file` comes from the filesystem. |
| `8` vs `8.0` | `b'{"r":8}'` vs `b'{"r":8.0}'` | A rating that is sometimes int and sometimes float breaks byte-identity without changing value. |
| `float("nan")` | `b'{"r":NaN}'` | **Not valid JSON.** `json.loads` accepts it; most other parsers reject the dataset outright. |

The third is a straight bug in the recipe — `allow_nan=False` is missing. The
first two are policy decisions the model must make once, at the boundary
(Decision 4).

### Finding 5 — `model_copy(update=...)` bypasses validation entirely

On a frozen model with `extra="forbid"` and `validate_assignment=True`:

```python
bad = item.model_copy(update={"year": "nineteen ninety five"})
bad.year   # 'nineteen ninety five'  -- a str, in an `int | None` field
```

No error. The validating equivalent (`M.model_validate({**m.model_dump(), ...})`)
raises as expected. This matters because `model_copy(update=...)` is precisely
what step 0.5's corruption functions will reach for, and a type-invalid value
would flow straight into the truth file and the snapshot. The mitigation is a
project-owned mutation helper that re-validates, plus a test that no corruption
function calls `model_copy` directly (Task 4).

Related, smaller: `model_copy(update={"not_a_field": 1})` silently drops the
unknown key rather than raising. Benign, but it is a silent drop.

### Finding 6 — datetimes serialize by *representation*, not by instant

```
datetime(1995,12,15,5,tzinfo=UTC)              -> '1995-12-15T05:00:00Z'
datetime(1995,12,15,0,tzinfo=UTC-5)            -> '1995-12-15T00:00:00-05:00'
datetime(1995,12,15)          (naive)          -> '1995-12-15T00:00:00'
```

The first two are the same instant and produce different bytes. Naive datetimes
produce a third form with no offset at all. Any timestamp entering the model must
be coerced to aware UTC at parse time, or byte-identical export is decided by the
server's timezone configuration.

### Finding 7 — the roadmap's media-kind list cannot express two audiobook corruptions

The roadmap names six kinds: `movie / show / season / episode / audiobook /
audiobook_part`, mapping Album→audiobook and Track→audiobook_part. But
`implementation-plan.md` §3 defines two corruptions that operate on the **Artist**:

- `author_name_variant` — "Split one artist into 2–3 variants … reassigning
  albums"; truth holds "canonical author + **the id set to merge**"
- `narrator_as_author` — "Set the **artist**/author to the narrator's name"

An id set of artists cannot be recorded if artists are not addressable items. The
mapping is Artist→Album→Track, which is structurally the same three-level shape as
Show→Season→Episode, and the model should mirror it. See Decision 1; this needs a
correction recorded in `implementation-plan.md`, per the CLAUDE.md rule about spec
problems.

---

## 3. The design

### 3.1 `canonical.py` — one serializer, top level

```python
"""The canonical serializer. Determinism for exports, evidence ids, and case ids
all reduce to this function producing the same bytes for the same value."""

import json
import unicodedata


def canonical_json(obj: object) -> bytes:
    """Deterministic JSON bytes. The only serializer used for anything hashed,
    compared, or written to a dataset."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,   # NaN/Infinity are not JSON; fail loudly rather than
                           # write a dataset other parsers reject
    ).encode("utf-8")


def canonical_text(value: str) -> str:
    """NFC-normalize text crossing into the model. See Decision 4 for why paths
    are exempt."""
    return unicodedata.normalize("NFC", value)
```

Top level rather than under `models/` because `store/` and the evidence hashing in
1.4 need it without importing the media model. It has no imports of its own beyond
the stdlib, so it stays free of every architectural contract.

### 3.2 `models/ids.py`

```python
class IdNamespace(StrEnum):
    IMDB = "imdb"
    TMDB = "tmdb"
    TVDB = "tvdb"
    PLEX = "plex"          # plex://movie/<hash> -- the new-agent primary guid
    ASIN = "asin"          # Audible/Audnexus
    MBID = "mbid"          # MusicBrainz, for non-audiobook artist sections
    LOCAL = "local"        # local:// and com.plexapp.agents.none
    UNKNOWN = "unknown"    # parsed shape not recognised -- raw is retained


@dataclass(frozen=True, slots=True)
class ItemId:
    provider: str      # "plex" | "snapshot" -- keeps live and snapshot ids apart
    section_id: str
    rating_key: str
    # __post_init__ rejects ":" in any component so str()/parse() round-trip
    # unambiguously; "plex:3:1701" is the canonical form used as a dict key and
    # in truth files.


@dataclass(frozen=True, slots=True)
class ExternalId:
    namespace: IdNamespace
    value: str
    raw: str                    # exactly what Plex returned; never discarded
    season: int | None = None   # legacy thetvdb://73739/1/1 path components
    episode: int | None = None
```

`ItemId` and `ExternalId` are frozen dataclasses, not models: they are internal
value objects wanting hashability and cheapness, which is exactly the split
`development-practices.md` §2.1 draws. Verified: a frozen dataclass nests inside a
frozen `BaseModel`, round-trips through `model_dump(mode="json")` →
`validate_python`, and leaves the model hashable.

**The parser ladder** (`parse_guid`), in order, each form falling through to the
next:

| Input | → | Notes |
|---|---|---|
| `plex://movie/5d776b9a…` | `(PLEX, "5d776b9a…")` | new-agent primary |
| `imdb://tt0111161` | `(IMDB, "tt0111161")` | new-agent child |
| `tmdb://278`, `tvdb://121361` | `(TMDB, …)`, `(TVDB, …)` | |
| `com.plexapp.agents.imdb://tt0111161?lang=en` | `(IMDB, "tt0111161")` | query string dropped, `raw` keeps it |
| `com.plexapp.agents.themoviedb://278?lang=en` | `(TMDB, "278")` | |
| `com.plexapp.agents.thetvdb://73739/1/1?lang=en` | `(TVDB, "73739", season=1, episode=1)` | path carries the numbering |
| `com.plexapp.agents.hama://tvdb-73739/1/1` | `(TVDB, "73739", season=1, episode=1)` | anime agent nests the real source |
| `com.plexapp.agents.audnexus://B08G9PRS1K` | `(ASIN, "B08G9PRS1K")` | |
| `com.plexapp.agents.none://…`, `local://…` | `(LOCAL, …)` | |
| anything else | `(UNKNOWN, raw, raw=raw)` | **never dropped** |

The legacy forms below `themoviedb` are asserted from the agent names plexapi
documents, but **cannot be verified without a live legacy-agent library**. That is
exactly why the fallback row exists, and why Task 6 adds an unknown-namespace
counter to the 0.4 census: rather than guessing which forms exist, the real
library reports them. An unparseable guid that vanished silently would breach the
"no silent caps" house rule; one that lands in `UNKNOWN` with `raw` intact is a
measurement.

### 3.3 `models/item.py`

```python
class MediaKind(StrEnum):
    MOVIE = "movie"
    SHOW = "show"
    SEASON = "season"
    EPISODE = "episode"
    AUTHOR = "author"                    # Artist -- see Decision 1
    AUDIOBOOK = "audiobook"              # Album
    AUDIOBOOK_PART = "audiobook_part"    # Track


class FetchProfile(StrEnum):
    """What was actually requested from the server. Absence only means absence
    relative to a profile -- see finding 2."""
    STUB = "stub"    # identity + title/year, from a list call
    CORE = "core"    # single-item default
    FULL = "full"    # the export include set; absence here is real absence


class BaseItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: ItemId
    media_kind: MediaKind
    fetched: FetchProfile
    title: str
    title_sort: str | None = None
    summary: str | None = None
    guids: tuple[ExternalId, ...] = ()      # sorted; see Decision 5
    locked_fields: tuple[str, ...] = ()     # from media.Field(name, locked)
    has_thumb: bool | None = None           # presence, not URL -- Decision 6
    has_art: bool | None = None
    added_at: datetime | None = None        # aware UTC -- finding 6
    updated_at: datetime | None = None
```

Subtypes, each pinning `media_kind` with a `Literal` so the union discriminates:

| Kind | Adds | Source attribute |
|---|---|---|
| `MovieItem` | `year`, `edition_title`, `original_title`, `content_rating`, `studio`, `tagline`, `rating`, `audience_rating`, `originally_available_at`, `duration_ms`, `parts` | `Movie.editionTitle` confirmed present |
| `ShowItem` | `year`, `content_rating`, `studio`, `network`, `show_ordering`, `child_count`, `leaf_count` | `Show.showOrdering` drives `absolute_vs_seasonal` |
| `SeasonItem` | `parent` (`ItemId`), `parent_title`, `index`, `year` | `Season.index`, `parentTitle` |
| `EpisodeItem` | `parent`, `grandparent`, `parent_title`, `grandparent_title`, `index`, `parent_index`, `year`, `originally_available_at`, `duration_ms`, `parts` | `Episode.index` / `parentIndex` are the numbers `episode_wrong_season` moves |
| `AuthorItem` | `album_count` | `Artist` |
| `AudiobookItem` | `parent` (author), `parent_title`, `index`, `year`, `studio`, `series`, `series_position`, `part_count` | `series_position` is a **string** — Audnexus returns `"3.5"` |
| `AudiobookPartItem` | `parent`, `grandparent`, `index`, `duration_ms`, `parts` | `Track` |

```python
NormalizedItem = Annotated[
    MovieItem | ShowItem | SeasonItem | EpisodeItem
    | AuthorItem | AudiobookItem | AudiobookPartItem,
    Field(discriminator="media_kind"),
]
ItemAdapter: TypeAdapter[NormalizedItem] = TypeAdapter(NormalizedItem)
```

Verified working: discriminated dispatch by enum value, `extra="forbid"`
rejection, and a `model_dump(mode="json")` → `validate_python` round-trip that
compares equal.

Supporting types, all in this module because the 0.3 protocol signature needs
them and `library/base.py` should introduce no vocabulary of its own:

```python
class FilePart(BaseModel):     # get_file_info's payload
    path: str                  # raw, un-normalized -- Decision 4
    container: str | None
    video_resolution: str | None
    size_bytes: int | None
    duration_ms: int | None

class ItemStub(BaseModel):     # what list_items returns
    item_id: ItemId
    media_kind: MediaKind
    title: str
    year: int | None = None

class SectionRef(BaseModel):
    section_id: str
    title: str
    section_type: str          # movie | show | artist | photo -- Plex's own
    agent: str                 # com.plexapp.agents.* | tv.plex.agents.*

class Page[T](BaseModel):      # explicit counts; no silent caps
    items: tuple[T, ...]
    total: int
    offset: int
    returned: int
```

`SectionRef.agent` carries the raw agent identifier because 0.3's audiobook
detection keys off it (`audnexus` in the string) and 0.4's census reports by it.

### 3.4 The mutation helper

```python
def with_changes(item: NormalizedItem, changes: Mapping[str, object]) -> NormalizedItem:
    """Apply field changes and RE-VALIDATE. `model_copy(update=...)` does not
    validate (see step-0.2 plan finding 5), which would let a corruption function
    write a str into an int field and have it reach the truth file."""
    return ItemAdapter.validate_python({**item.model_dump(mode="json"), **changes})
```

This is the only sanctioned way to produce a modified item, and 0.5's
`FieldChange` records are generated from the same mapping.

---

## 4. Decisions

### Decision 1 — add an `author` media kind (**recommended**)

Finding 7: two audiobook corruptions address Artists, and one records "the id set
to merge", which requires artists to have `ItemId`s. Author/Audiobook/Part mirrors
Show/Season/Episode exactly, so it costs one subtype and no new concepts.

The alternative — storing the author as a plain string field on `AudiobookItem` —
makes `author_name_variant` unrepresentable: you cannot cite which artist records
merge into which. **Record the correction in `implementation-plan.md`**, per the
CLAUDE.md rule, rather than only here.

### Decision 2 — Pydantic models for items, frozen dataclasses for ids (**recommended**)

Items are parsed from export files, snapshots, and truth files — boundaries, where
§2.1 puts `BaseModel`. `ItemId`/`ExternalId` are internal value objects wanting
hashability — where §2.1 puts frozen dataclasses. Verified that the combination
works and stays hashable.

Frozen, additionally, because it forces every mutation through `with_changes` and
therefore through a `FieldChange` record. That is the property 0.5's
`apply_reverse(changes) == ground_truth` invariant depends on.

### Decision 3 — the model records what was fetched (**recommended**)

The `FetchProfile` field. This is the answer to finding 2, and it is not
optional-feeling once you look at 1.3: `get_item_details(item_id, include[])`
returns "minimal by default", so the *same* `NormalizedItem` type flows out of the
export at `FULL` and out of an agent tool call at `CORE`. Without the marker,
`guids == ()` means "no external ids" in one path and "you didn't ask" in the
other, and the screen cannot tell which.

Cost: one enum field, and export/corruption asserting `fetched is FULL`. The
alternative — separate types per profile — multiplies seven subtypes by three.

### Decision 4 — NFC for text, raw bytes for paths (**recommended**)

Normalize every human-readable string (`title`, `summary`, `title_sort`, author
and series names) to NFC on the way in. Do **not** normalize `FilePart.path`.

The asymmetry is deliberate: comparison and hashing need one representation
(finding 4), but a path is an argument to a future filesystem operation, and
Phase 3 renames files. Normalizing a macOS NFD path to NFC produces a string that
may not name any file on disk. Where a path needs comparing rather than opening,
normalize at the comparison site in 0.45 — visibly, not silently at parse time.

### Decision 5 — guids are stored sorted (**recommended**)

Plex returns `Guid` elements in whatever order the XML held. Byte-identical export
requires a canonical order, so sort by `(namespace, value)` at construction. Store
as a `tuple`, not a `list`, so the frozen model stays hashable.

### Decision 6 — artwork as presence booleans, not URLs (**recommended**)

`thumb` and `art` are keys like `/library/metadata/1701/thumb/1699999999` — the
trailing component is a mutable timestamp. The only corruption that touches
artwork (`missing_metadata`) cares whether artwork exists. Storing the URL imports
a volatile field into a record whose whole purpose is stable comparison; storing
`has_thumb: bool | None` (with `None` meaning "not fetched at this profile")
captures everything the harness uses.

### Decision 7 — `models/ids.py` split out from `models/item.py`

`implementation-plan.md` §7 lists 0.2's file as `models/item.py` alone. The guid
parser is the most heavily tested code in the step and pulls in its own enum and
ladder; keeping it beside seven item subtypes makes both harder to read. Minor,
stated for the record rather than because it is contentious.

### Decision 8 — no property-based testing yet

Hypothesis would suit the round-trip invariant, but it is a new dev dependency and
0.5 is where property testing genuinely earns its place (`apply_reverse` over all
fifteen corruption classes). Table-driven parametrized tests cover 0.2's surface.
Revisit at 0.5 and add it deliberately, once.

---

## 5. Tasks

| # | Task | Files | Done when |
|---|---|---|---|
| 1 | Add `pydantic` to default deps | `pyproject.toml`, `uv.lock` | `uv sync --locked` clean; CI green |
| 2 | Canonical serializer + text policy | `canonical.py`, `tests/test_canonical.py` | NFC, int/float, and `allow_nan=False` each have a test asserting the finding-4 behaviour |
| 3 | Ids and the guid ladder | `models/ids.py`, `tests/models/test_ids.py` | Every row of the §3.2 table is a parametrized case; `ItemId` str/parse round-trips; a `":"` in a component raises |
| 4 | The item union + `with_changes` | `models/item.py`, `tests/models/test_item.py` | Round-trip equality **and** byte-stability for one item of each of the seven kinds; a test proves `with_changes` rejects what `model_copy` accepts |
| 5 | Supporting types | `models/item.py` | `Page`, `ItemStub`, `SectionRef`, `FilePart` round-trip; `Page` carries explicit counts |
| 6 | Follow-ups recorded | `docs/implementation-plan.md`, `docs/roadmap.md` | The `author` kind correction (Decision 1) and the 0.4 unknown-guid census counter are written down where the step that acts on them will see them |
| 7 | Doc corrections | `docs/development-practices.md` | §2.5 gains `allow_nan=False` and the NFC/float policy; §2.1 gains the `model_copy` trap |

Task 7 is the same obligation 0.1 discharged for §1.3: the recipe in the practices
document is currently wrong in a way that fails silently, and CLAUDE.md forbids
leaving the code and the document disagreeing.

---

## 6. Test plan

The gate is "round-trip and both guid-form parsers are unit tested". Concretely:

```python
# the round-trip that everything downstream rests on
def test_item_round_trips_byte_identically(kind):
    item = sample(kind)
    once = canonical_json(item.model_dump(mode="json"))
    twice = canonical_json(ItemAdapter.validate_json(once).model_dump(mode="json"))
    assert once == twice          # bytes, not just equality
    assert ItemAdapter.validate_json(once) == item

# the two guid worlds, per the roadmap's explicit requirement
def test_new_agent_guids_parse(): ...        # plex:// imdb:// tmdb:// tvdb://
def test_legacy_agent_guids_parse(): ...     # com.plexapp.agents.* incl. path forms
def test_unknown_guid_is_preserved_not_dropped(): ...

# the determinism traps, each pinned so a future "cleanup" cannot reintroduce them
def test_nfd_and_nfc_titles_normalize_to_the_same_bytes(): ...
def test_nan_is_rejected_rather_than_written(): ...
def test_naive_datetime_is_rejected_or_coerced_to_utc(): ...
def test_guids_are_stored_sorted_regardless_of_input_order(): ...

# finding 5, as an executable warning
def test_with_changes_validates_where_model_copy_does_not(): ...
```

A test file per module under `tests/models/`, matching the existing
`tests/store/` layout.

### Exit checklist

```bash
uv sync --locked --group dev
uv run ruff check . && uv run ruff format --check .
uv run lint-imports          # models/ imports no SDK; contracts unchanged
uv run pytest -q
```

- [x] All four pass locally and in CI
- [x] One item of each of the seven media kinds round-trips byte-identically, and
      a test asserts a sample exists for every `MediaKind` so a new kind cannot
      quietly go untested
- [x] Both guid worlds parse, and an unrecognised guid survives as `UNKNOWN` with
      `raw` intact
- [x] `development-practices.md` §2.5 no longer ships a serializer that can emit
      invalid JSON; §2.1 records the `model_copy` trap
- [x] The `author`-kind correction is recorded in `implementation-plan.md`
- [x] `roadmap.md` 0.2 is `[x]` and 0.3 can begin

---

## 7. What this hands to later steps

| Step | Inherits |
|---|---|
| 0.3 | `LibraryProvider`'s entire signature vocabulary already exists, so `library/base.py` introduces no types of its own — it only removes the mutating methods. `SectionRef.agent` feeds audiobook detection. |
| 0.4 | `FetchProfile.FULL` is the export's precondition; `locked_fields` is captured from day one, so Phase 3's revert-lock-state requirement is not a retrofit; the unknown-guid counter turns §3.2's unverifiable legacy forms into a census number. |
| 0.45 | Comparators normalize at the comparison site, knowing text is already NFC and paths deliberately are not. |
| 0.5 | `with_changes` is the mutation path, so `FieldChange` records and the `apply_reverse` invariant have a single chokepoint. Frozen models mean no corruption can mutate in place and lose its own audit trail. |
| 0.6 | `subject_key`'s ladder (external id → title+year → path hash) has all three inputs present and canonically normalized. |
| 1.3 | `ItemStub` and `Page` are already the minimum-useful-payload shapes the tool schemas in `implementation-plan.md` §5 specify. |
