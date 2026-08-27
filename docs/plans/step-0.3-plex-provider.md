# Step 0.3 — The Read-Only Plex Provider

Implementation plan for roadmap step **0.3 Read-only Plex provider**.
Written 2026-08-26 against commit `7e468da`, with 0.1 and 0.2 complete and CI green.

> **Status: implemented 2026-08-26.** All six decisions were taken as recommended.
> 101 new tests; the suite is 223, all offline. Two things were learned during
> implementation that this document did not anticipate, both recorded in
> `development-practices.md`: plexapi returns **naive local-time** datetimes by
> default (§4.3), and an external package needs **one** `ignore_imports` line
> rather than the pair step 0.1 planned, because grimp collapses external
> packages to a single node (§1.3).
>
> A third was found by CI rather than by reading: `setDatetimeTimezone("utc")`
> fails open exactly like the auto-reload switch — a `ZoneInfoNotFoundError`
> becomes `tzinfo = None` behind a swallowed log warning — so it worked locally
> and silently restored naive timestamps on the runner. Assigning `datetime.UTC`
> directly removes the tzdata dependency. The naive-datetime guard at the mapping
> boundary is what turned that into a red build instead of a quietly
> machine-dependent export.

Every finding in §2 was produced by reading and running plexapi 4.18.2. One of them
answers a question `development-practices.md` §4.3 explicitly deferred to this step
— and the answer is worse than the question assumed.

**Gate for the step** (`roadmap.md`, `implementation-plan.md` §7): a test asserts
the protocol exposes no mutating method, and audiobook detection passes against
committed fixtures.

---

## 1. Scope

This is the step where the outside world first touches the project, and it is the
step that makes §3.2 structurally true: *"scan and diagnose cannot mutate — not
'should not', the tools do not exist."*

Deliverables:

- `library/base.py` — the `LibraryProvider` protocol (read methods only) and the
  `LibraryError` taxonomy
- `library/plex.py` — `PlexLibrary`, the only module in the project permitted to
  import `plexapi`
- `library/audiobook.py` — the heuristics that decide an `artist` section holds
  audiobooks
- The `plexapi` `ignore_imports` pair added to the import contract (already a
  tracked checklist item; CI **will** fail on the first `import plexapi` until it
  is there, by design)
- Committed XML fixtures and the offline test harness they run under

`plexapi` and `requests` join the default dependency group. Neither introduces
persistent state or a service; `requests` is plexapi's own HTTP layer, and we take
it on deliberately (Decision 1) rather than inheriting it silently.

**Not in 0.3:** the export and census (0.4), `SnapshotLibrary` (0.7), and every
mutating operation (Phase 3, behind a separate `MutableLibraryProvider`).

---

## 2. Verified findings

### Finding 1 — the auto-reload switch is case-sensitive and fails **open**

`development-practices.md` §4.3 says the `PLEXAPI_<SECTION>_<KEY>` env form
"should work here too — confirm it empirically in step 0.3 rather than assuming."
Confirmed, with a caveat that changes the design:

```
PLEXAPI_PLEXAPI_AUTORELOAD='false'   -> False   OFF
PLEXAPI_PLEXAPI_AUTORELOAD='0'       -> False   OFF
PLEXAPI_PLEXAPI_AUTORELOAD='False'   -> True    ON  <-- auto-reload still live
PLEXAPI_PLEXAPI_AUTORELOAD='FALSE'   -> True    ON  <-- auto-reload still live
PLEXAPI_PLEXAPI_AUTORELOAD='no'      -> True    ON  <-- auto-reload still live
(unset)                              -> True    ON
```

The mechanism, and why it is silent. `plexapi.utils.cast` accepts only
`1, True, "1", "true"` and `0, False, "0", "false"` and raises `ValueError` on
anything else — but `PlexConfig.get` wraps the whole lookup in a **bare `except:`**
that swallows the error and returns the *default*, which for this key is `True`.
So the natural Python spelling, `False`, does not merely fail to disable
auto-reload: it disables nothing, warns about nothing, and leaves every partial
object silently refetching over the network.

One piece of good news. `_autoReload` is read **per object at construction**
(`base.py:115`), not at import:

```python
self._autoReload = CONFIG.get('plexapi.autoreload', True, bool)
```

so unlike `TIMEOUT` — which `plexapi/__init__.py` binds once at import time — this
can be set programmatically before constructing the server and it takes effect.
That asymmetry is what makes Decision 3 possible.

This is precisely why §4.3 already demands `PlexLibrary` *assert* auto-reload is
off rather than trusting configuration. The finding supplies the mechanism, and
Finding 5 supplies a way to prove it in CI.

### Finding 2 — the pagination claim, confirmed line by line

From `PlexObject.fetchItems`:

```python
container_size = container_size or X_PLEX_CONTAINER_SIZE     # 100, not 50
if maxresults is not None:
    container_size = min(container_size, maxresults)
...
    wanted_number_of_items = total_size - offset             # <-- without maxresults
    if maxresults is not None:
        wanted_number_of_items = min(maxresults, wanted_number_of_items)
    if wanted_number_of_items <= len(results):
        break
```

With `container_start` alone, `wanted_number_of_items` is *the entire remaining
result set*, and the loop keeps issuing requests until it has all of it. Both
arguments, every call. `X_PLEX_CONTAINER_SIZE = CONFIG.get('plexapi.container_size', 100, int)`
confirms the default container size is 100 and the published docs saying 50 are
wrong.

### Finding 3 — the total is available without walking the library

`MediaContainer` carries `size`, `offset`, and `totalSize`, and `extend()`
propagates `totalSize` from each fetched page into the accumulated container. So
the value backing `Page.total` comes from the response we already made.
`LibrarySection.totalSize` (a `cached_data_property`) is a cheap independent
fallback. Neither requires fetching items to count them — which matters, because a
`Page` with an inferred total would be a silent cap.

### Finding 4 — plexapi collapses every HTTP failure that is not 401 or 404

`PlexServer.query`, in full:

```python
if response.status_code not in (200, 201, 204):
    if response.status_code == 401:   raise Unauthorized(message)
    elif response.status_code == 404: raise NotFound(message)
    else:                             raise BadRequest(message)
```

So **429, 500, 502, and 503 are all `BadRequest`** — retryable and terminal
conditions are indistinguishable by exception type. The status survives only
embedded in the message string, formatted as `(503) service_unavailable; <url> <body>`.

And what plexapi does *not* raise matters as much: `requests` is called directly,
so a connection refusal or a timeout propagates as `requests.exceptions.ConnectionError`
or `.Timeout`, never wrapped in a `PlexApiException`. A boundary that catches only
`plexapi.exceptions.*` leaks raw `requests` types into the agent — which is exactly
the leak `library/plex.py` exists to prevent.

The full plexapi taxonomy is small: `PlexApiException` → `BadRequest`,
`NotFound`, `UnknownType`, `Unsupported`; `Unauthorized(BadRequest)`;
`TwoFactorRequired(Unauthorized)`.

### Finding 5 — plexapi objects build offline from XML, and the stub is a tripwire

Verified: a `Movie` constructed from a committed XML element with a stub server
resolves `title`, `year`, `editionTitle`, `guid`, `guids`, `fields` (lock state),
and `media`/`parts` — everything the mapping needs — with no network at all.

```python
class StubServer:
    _baseurl = "http://stub"
    def query(self, *a, **k):
        raise AssertionError("network access during a fixture test")

movie = Movie(StubServer(), element, initpath="/library/sections/3/all")
```

The stub is not merely a convenience, it is the **auto-reload tripwire**. On a
partial object, touching an attribute whose value is `None` is exactly what
triggers the silent refetch, so:

```
autoreload=False  -> movie.originalTitle returned None (no network)
autoreload=True   -> AssertionError: NETWORK TOUCHED
```

That turns Finding 1 from a configuration hope into a CI assertion, and it means
step 0.3 needs **no live Plex server in CI** — vcrpy is for the metadata sources in
1.1, not for this.

### Finding 6 — section types, and one trap that turned out not to be one

Only four section types exist: `MovieSection` (`TYPE='movie'`), `ShowSection`
(`'show'`), `MusicSection` (`'artist'`), `PhotoSection` (`'photo'`). There is no
audiobook type, which is the constraint `library/audiobook.py` exists to work
around.

`MusicSection.METADATA_TYPE` is `'track'`, which looks like it would make
`section.all()` return chapter files rather than authors. It does not —
`LibrarySection.all()` uses `self.TYPE`, and `METADATA_TYPE` is referenced only
when building a sync item. Checked rather than assumed, and recorded here so the
next reader does not have to re-check it. The real operational point stands: the
three audiobook levels need an explicit `libtype` (`artist` / `album` / `track`),
because the default gives you only the top one.

---

## 3. The design

### 3.1 `library/base.py` — the protocol and the taxonomy

```python
class LibraryProvider(Protocol):
    def sections(self) -> tuple[SectionRef, ...]: ...
    def list_items(self, section_id: str, offset: int, limit: int,
                   media_kind: MediaKind | None = None) -> Page[ItemStub]: ...
    def get_item(self, item_id: ItemId, profile: FetchProfile = FetchProfile.CORE) -> NormalizedItem: ...
    def get_children(self, item_id: ItemId, offset: int, limit: int) -> Page[ItemStub]: ...
    def get_files(self, item_id: ItemId) -> tuple[FilePart, ...]: ...
    def find_similar(self, section_id: str, title: str, limit: int) -> tuple[ItemStub, ...]: ...
```

Every type in that signature already exists from step 0.2. The protocol introduces
no vocabulary of its own — it only declines to offer `edit`, `merge`, `fixMatch`,
`refresh`, `delete`, or `unmatch`. That absence *is* the read-only guarantee;
plexapi has no read-only mode, so this is the only place it can live.

The error taxonomy, with the classification CLAUDE.md requires baked into the type
rather than left to a caller's judgement:

```python
class Retryability(StrEnum):
    RETRYABLE = "retryable"      # handled in code, never surfaced to the model
    CORRECTABLE = "correctable"  # surfaced WITH a concrete next action
    TERMINAL = "terminal"        # surfaced, and says retrying will not help


class LibraryError(Exception):
    retryability: ClassVar[Retryability]
    def __init__(self, message: str, *, next_action: str | None = None,
                 status: int | None = None) -> None: ...
```

| Error | From | Class | `next_action` |
|---|---|---|---|
| `LibraryAuthError` | 401, `Unauthorized`, `TwoFactorRequired` | terminal | — |
| `LibraryItemNotFound` | 404, `NotFound` | correctable | "re-list the section; rating keys move on rescan" |
| `LibraryRateLimited` | 429 | retryable | — |
| `LibraryUnavailable` | 5xx, `ConnectionError`, `Timeout` | retryable | — |
| `LibraryRequestError` | other 4xx, `BadRequest`, `Unsupported` | terminal | — |
| `LibraryProtocolError` | `UnknownType`, unmappable shape | terminal | — |

A `correctable` error that does not name a next action is a bug per CLAUDE.md, so
`__init__` asserts the pairing rather than trusting each raise site.

### 3.2 `library/plex.py` — the confinement boundary

```python
class PlexLibrary:
    """The only module that imports plexapi. plexapi objects, plexapi exceptions,
    and requests exceptions all stop here."""

    def __init__(self, baseurl: str, token: str, *, session: Session | None = None,
                 timeout: int = 30) -> None:
        _force_autoreload_off()      # finding 1
        self._server = PlexServer(baseurl, token, session=session or build_session(), timeout=timeout)
        _assert_autoreload_off(self._server)
```

Four responsibilities, in order of how easily each is got wrong:

**1. Auto-reload, belt and braces.** Set it ourselves rather than documenting a
shell variable; then verify; then verify again on a real object:

```python
def _force_autoreload_off() -> None:
    # Set unconditionally: a user's PLEXAPI_PLEXAPI_AUTORELOAD="False" would
    # otherwise take precedence over the config file AND silently evaluate to
    # True (finding 1). Env is consulted before file, so the only way to win is
    # to own the env value.
    os.environ["PLEXAPI_PLEXAPI_AUTORELOAD"] = "false"
    if plexapi.CONFIG.get("plexapi.autoreload", True, bool) is not False:
        raise LibraryProtocolError("could not disable plexapi auto-reload")
```

and after the first fetch, assert `obj._autoReload is False`. Touching a private
attribute is deliberate: the alternative is trusting a configuration path that
fails open.

**2. Paging, always both arguments.**

```python
results = section.search(libtype=..., container_start=offset, maxresults=limit)
total = getattr(results, "totalSize", None) or section.totalSize
return Page(items=..., total=total, offset=offset, returned=len(results))
```

`limit` is required, not optional — a caller that forgets it walks the library
(finding 2), so the signature does not let them.

**3. Explicit reload includes.** One include set per `FetchProfile`, declared as
data so the export manifest can record exactly what produced a record:

```python
RELOAD_INCLUDES: dict[FetchProfile, dict[str, bool | int]] = {
    FetchProfile.CORE: dict(includeChapters=False, includeMarkers=False,
                            includeBandwidths=False, includeGeolocation=False,
                            includeLoudnessRamps=False, checkFiles=False),
    FetchProfile.FULL: dict(..., checkFiles=True),
}
```

Never pass `excludeElements` — it would drop the `Guid` and `Media` elements the
whole model is built on (`_EXCLUDES` is opt-in, per step 0.1's neighbouring
finding, so silence is the correct behaviour here).

**4. Mapping and translation.** `_to_normalized(obj, profile)` per media kind,
with `parse_guids(obj.guid, [g.id for g in obj.guids])` doing the guid work from
0.2 and `tuple(f.name for f in obj.fields if f.locked)` capturing lock state. Every
public method is wrapped by one translation decorator so no raise site can forget:

```python
@_translates_errors
def get_item(self, item_id, profile=FetchProfile.CORE) -> NormalizedItem: ...
```

### 3.3 `library/audiobook.py` — detection, with its evidence

There is no audiobook section type, so this is a judgement — which means it must
report *why*, not just *what*, and must never hide how much it looked at.

```python
class Signal(StrEnum):
    AGENT_IDENTIFIER = "agent_identifier"    # 'audnexus' / 'audiobook' in section.agent
    CONTAINER_SHARE = "container_share"      # .m4b/.m4a share of sampled tracks
    ALBUM_STRUCTURE = "album_structure"      # few long tracks per album, not ~12 short ones


@dataclass(frozen=True, slots=True)
class AudiobookVerdict:
    is_audiobook: bool
    signals: tuple[SignalResult, ...]    # each: signal, fired, observed value, threshold
    sampled: int                         # how many items were examined
    population: int                      # out of how many -- no silent caps
```

Thresholds live in one module-level constants block, and the verdict records the
threshold each signal was judged against, so a later argument about a
misclassified section is settled by reading the record rather than re-running it.

Detection is a *sample*, because a large music section should not be walked to
classify it. `sampled` and `population` are on the verdict for the same reason
`Page` carries `total`: a truncation nobody can see reads as full coverage.

---

## 4. Decisions

### Decision 1 — supply our own `requests.Session` (**recommended**)

`PlexServer(baseurl, token, session=...)` accepts one, and it buys three things
the project needs and plexapi does not provide (§4.7: "no retry, no backoff, no
rate limiting anywhere"):

- **Retry and backoff** via a mounted `HTTPAdapter(max_retries=Retry(...))`,
  which is where `RETRYABLE` errors get handled in code and never surfaced.
- **Status-code visibility.** Finding 4 leaves the status only inside a message
  string; a session-level response hook records the last status so the translation
  layer classifies on a number rather than on a regex over prose.
- **One throttle point** for the concurrency limit §4.7 asks for.

The alternative — parsing `(\d{3})` out of `str(exc)` — works today because the
message format is a literal in `query()`, but it is a string contract with a
library that has no reason to keep it. Use the session; keep message parsing as a
fallback for the case where the hook did not fire, and test both paths.

### Decision 2 — set the auto-reload env var ourselves (**recommended**)

Not merely document it. Environment is consulted *before* the config file, so a
developer with `PLEXAPI_PLEXAPI_AUTORELOAD=False` already exported would override
a correct `config.ini` and land back in the fails-open case. Owning the value is
the only way to win, and it makes the guarantee independent of the machine.

Then assert twice — once on `CONFIG.get`, once on a constructed object — because
the two can disagree if plexapi's internals change.

### Decision 3 — committed XML fixtures, no live server in CI (**recommended**)

Finding 5 makes this cheap and total. Fixtures live in `tests/fixtures/plex/` as
`.xml`, one per shape worth pinning: new-agent movie, legacy-agent movie, show,
season, episode, artist/album/track for audiobooks, and the awkward ones (no
guids, absolute-numbered episodes, multi-part file).

A small capture script (`scripts/capture_fixtures.py`, not shipped in the package)
can generate them from the user's real server, scrubbing the token, the server
machine id, and absolute media paths. Fixtures are reviewed on the way in — a
changed upstream shape is information, per §8.1.

**The stub server raises on every `query()`.** That is not incidental: it makes any
accidental network access a test failure, and it is what pins the auto-reload
guarantee.

### Decision 4 — `list_items` requires an explicit limit (**recommended**)

No default. Finding 2 means a forgotten limit is not a slow path, it is a full
library walk with no indication it happened. A required argument is a
parse-time gate in the §2.2 sense; a default of 100 is a trap with a friendly face.

### Decision 5 — audiobook detection returns a verdict, not a bool

Signals and thresholds are recorded on the result. This is the same principle as
`Confidence{value, band, reasons}` in 1.4: a judgement that cannot explain itself
cannot be argued with, and this particular judgement decides how an entire section
is interpreted for the rest of the run.

The thresholds are tunable constants and therefore must never gate eval scoring —
the same reasoning as invariant 6. They gate interpretation only.

### Decision 6 — `find_similar` stays in the protocol but is thin

The spec's `find_similar_items` is a Phase 1 tool. Implementing it here against
`section.search(title=...)` keeps the protocol complete so `SnapshotLibrary` (0.7)
has a fixed target, and the conformance suite covers both. Scoring by similarity
belongs with the comparators in 0.45, so this returns matches and lets that step
rank them.

---

## 5. Tasks

| # | Task | Files | Done when |
|---|---|---|---|
| 1 | Dependencies + the deferred contract edit | `pyproject.toml` | `plexapi` and `requests` added; the `plexapi` `ignore_imports` **pair** added to the contract (`plexapi` and `plexapi.**` — a bare wildcard is rejected); `lint-imports` green with the adapter importing plexapi |
| 2 | Protocol + error taxonomy | `library/base.py`, `tests/library/test_base.py` | A test asserts the protocol's method set is disjoint from a declared `MUTATING_METHODS` set; a `correctable` error without a `next_action` fails construction |
| 3 | Session, retry, status capture | `library/session.py` | A `respx`/`responses`-style unit test proves 429 and 503 retry and 401 does not |
| 4 | `PlexLibrary` construction + auto-reload guarantee | `library/plex.py` | Construction fails loudly if auto-reload is on; the tripwire test passes both ways |
| 5 | Mapping, per media kind | `library/plex.py`, fixtures | Each committed fixture maps to the expected `NormalizedItem`, compared as canonical JSON |
| 6 | Paging | `library/plex.py` | A recording stub asserts **both** `container_start` and `maxresults` reach `search`; `Page.total` comes from the response, never from `len(items)` |
| 7 | Error translation | `library/plex.py` | Every row of the §3.1 table has a test, including `requests` `ConnectionError`/`Timeout` |
| 8 | Audiobook detection | `library/audiobook.py`, fixtures | Classifies a committed audiobook section and a committed music section correctly, and reports `sampled`/`population` |
| 9 | Fixture capture script | `scripts/capture_fixtures.py` | Runs against a real server, scrubs token/machine id/paths, and its output is byte-stable |
| 10 | Doc updates | `docs/development-practices.md`, `roadmap.md` | §4.3 records the fails-open finding and the answer to its own open question; §4.7 records the session decision |

Task 10 is the same obligation 0.1 and 0.2 discharged: §4.3 currently poses a
question this step answers, and leaving it posed would send the next reader to
re-derive Finding 1 the hard way.

---

## 6. Test plan

The gate is two assertions; everything else supports them.

```python
def test_the_protocol_exposes_no_mutating_method():
    """Spec §3.2 is structural. This is the test that makes it so."""
    MUTATING = {"edit", "editTitle", "merge", "split", "fixMatch", "unmatch",
                "refresh", "analyze", "delete", "uploadPoster", "batchEdits",
                "saveEdits", "addCollection", "removeCollection", "update"}
    assert set(_protocol_methods(LibraryProvider)) & MUTATING == set()

def test_audiobook_detection_against_committed_fixtures(): ...
```

Supporting, in rough order of how much they would hurt to lack:

```python
def test_touching_a_none_attribute_does_not_hit_the_network()   # the tripwire
def test_constructing_with_autoreload_on_is_refused()
def test_capital_false_env_value_does_not_disable_autoreload()  # pins finding 1
def test_list_items_passes_both_container_start_and_maxresults()
def test_page_total_comes_from_the_container_not_from_len()
def test_every_plexapi_exception_maps_to_a_library_error()
def test_requests_connection_error_becomes_library_unavailable()
def test_correctable_errors_name_a_next_action()
def test_no_plexapi_type_appears_in_any_returned_value()        # confinement, at runtime
def test_legacy_agent_movie_fixture_maps_to_the_same_item_as_new_agent()
```

The confinement test deserves a note: `lint-imports` proves no *module* imports
plexapi, but not that no plexapi *object* escapes through a return value. Walk the
returned structure and assert no value's type resolves to a `plexapi.*` module.
Static and dynamic checks catch different mistakes.

### Exit checklist

```bash
uv sync --locked --group dev
uv run ruff check . && uv run ruff format --check .
uv run lint-imports          # now with the plexapi ignore pair in place
uv run pytest -q
```

- [ ] All four pass locally and in CI, with no network access from any test
- [ ] `LibraryProvider` has no mutating method, asserted
- [ ] Audiobook detection passes against committed fixtures and reports its sample
- [ ] Auto-reload is proven off by the tripwire, both ways
- [ ] `development-practices.md` §4.3 no longer poses a question this step answered
- [ ] `roadmap.md` 0.3 is `[x]` and 0.4 can begin

---

## 7. What this hands to later steps

| Step | Inherits |
|---|---|
| 0.4 | A provider that pages deterministically and a declared include set per profile — the export manifest records `RELOAD_INCLUDES[FULL]` verbatim, so "what produced this record" is data rather than folklore. The unknown-guid census counter from 0.2 plugs into the mapping here. |
| 0.45 | Real exported items to screen, and `AudiobookVerdict.signals` as a worked example of a judgement that carries its own evidence. |
| 0.7 | `SnapshotLibrary` implements this exact protocol and raises this exact taxonomy; the conformance suite in 0.7 runs against both. |
| 1.3 | `list_items`, `get_item_details`, `get_file_info`, and `find_similar_items` are thin wrappers over methods that already return minimum-useful payloads. |
| Phase 3 | `MutableLibraryProvider` is a *separate* protocol; `locked_fields` is already captured on every item, so revert-restores-locks is a feature of the data rather than a retrofit. |
