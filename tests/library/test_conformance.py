"""One suite, every provider: the properties in §4.6 of the step-0.7 plan.

Each property is a statement about *any* `LibraryProvider` over *any* library,
written so it can run against a subject without knowing what that subject holds.

Subjects:

* `snapshot` -- `SnapshotLibrary` over the hand-built records
  (`tests/library/snapshots.py`).
* `plex` -- `PlexLibrary` over `FakePlexServer`, with nothing faked above
  plexapi's network seam.
* `fake` -- `FakeLibrary`, the in-memory stand-in the export's tests run against.
  It answers any profile and keeps insertion order (step 0.7, Decision 9), so it
  is held to everything a provider owes and to nothing the model of Plex claims.
* `live` -- `PlexLibrary` against the real server, under `--run-live` only, never
  in CI. Its listings are read over a window, and what the window covered is
  printed in the run's summary.
* `plex-windowed` -- the live subject's bounded mode, offline: `PlexLibrary` over
  the fake server with a window smaller than its listings, so the code only a live
  run would otherwise reach runs on every commit.

Two properties are about the model of Plex (P10's second half, and P15). Both
offline subjects that claim the model follow it by construction -- the fake
server orders and matches with the snapshot's own functions -- so offline these
show only that `PlexLibrary`'s client logic preserves the model. Whether Plex
follows it is the live run's question.

Beyond the properties: the differential, which compares a snapshot of an export
with the server it was exported from, answer for answer; and the round trip,
which exports the snapshot again and gets the export back.
"""

import contextlib
import dataclasses
import functools
import json
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from itertools import product

import pytest
from pydantic import BaseModel

from shelfwarden.canonical import canonical_json
from shelfwarden.compare import fold_text
from shelfwarden.config import load_settings, require_plex
from shelfwarden.evals.export import (
    CENSUS_FILE,
    ITEMS_FILE,
    ROOTS_FILE,
    ExportResult,
    load_roots,
    render_items,
    render_roots,
    run_export,
)
from shelfwarden.evals.world import Addressing, CaseWorld, ExportedLibrary
from shelfwarden.library.base import (
    SECTION_KINDS,
    SECTION_ROOT_KIND,
    LibraryError,
    LibraryInvalidArgument,
    LibraryItemNotFound,
    LibraryProvider,
    LibrarySectionNotFound,
    LibraryUnsupported,
    Retryability,
)
from shelfwarden.library.plex import PlexLibrary
from shelfwarden.library.snapshot import (
    PROVIDER,
    children_key,
    listing_key,
    section_key,
    title_matches,
)
from shelfwarden.models.hierarchy import CHILD_KIND
from shelfwarden.models.ids import ItemId
from shelfwarden.models.item import (
    FetchProfile,
    ItemStub,
    MediaKind,
    NormalizedItem,
    SectionRef,
    dump_item,
)
from tests.conftest import LIVE_COVERAGE
from tests.evals.conftest import FakeLibrary
from tests.library.fake_plex import FakePlexServer
from tests.library.snapshots import hand_built

REQUESTABLE = (FetchProfile.CORE, FetchProfile.FULL)

# Big enough to take any offline section in one call. PlexLibrary still pages it
# underneath, 100 at a time, which is part of what is being tested.
EVERYTHING = 10_000

# The live window: the first two pages of every listing. The plan's bound
# (§4.6), checked over what a model would actually page through first.
LIVE_PAGE = 25
LIVE_WINDOW = 2 * LIVE_PAGE


@dataclass(frozen=True)
class Subject:
    name: str
    provider: LibraryProvider
    profiles: frozenset[FetchProfile]
    follows_model: bool
    # The request log, where there is a server to log requests.
    server: FakePlexServer | None = None
    # How much of each listing the properties read: `None` for all of it, which is
    # right for an offline library of a dozen records. A live library holds
    # thousands, so there each listing is read over its first `window` entries and
    # every bound is recorded in `covered`.
    window: int | None = None
    covered: list[str] | None = None

    @property
    def label(self) -> str:
        return self.provider.provider_info().provider

    def id(self, section_id: str, rating_key: str) -> ItemId:
        return ItemId(self.label, section_id, rating_key)

    def cover(self, line: str) -> None:
        if self.covered is not None:
            self.covered.append(line)


def _snapshot() -> Subject:
    return Subject("snapshot", hand_built(), frozenset({FetchProfile.CORE}), follows_model=True)


def _plex() -> Subject:
    server = FakePlexServer()
    return Subject(
        "plex",
        PlexLibrary(server=server),
        frozenset(REQUESTABLE),
        follows_model=True,
        server=server,
    )


def _windowed() -> Subject:
    """The live subject's bounded mode, offline. A window of 2 is smaller than most
    of the fake server's listings, so every branch a live run takes -- spread
    windows, trimmed pages, unchecked completeness, title probes -- runs on every
    commit rather than only when someone has a server to hand."""
    server = FakePlexServer()
    return Subject(
        "plex-windowed",
        PlexLibrary(server=server),
        frozenset(REQUESTABLE),
        follows_model=True,
        server=server,
        window=2,
        covered=[],
    )


def _fake() -> Subject:
    return Subject("fake", FakeLibrary.build(), frozenset(REQUESTABLE), follows_model=False)


@functools.cache
def _live() -> Subject:
    """The real server, read only -- the protocol offers nothing else. Built once
    per session; missing configuration fails the run, naming what to set."""
    baseurl, token = require_plex(load_settings())
    return Subject(
        "live",
        PlexLibrary(baseurl, token),
        frozenset(REQUESTABLE),
        follows_model=True,
        window=LIVE_WINDOW,
        covered=[],
    )


SUBJECTS: dict[str, Callable[[], Subject]] = {
    "fake": _fake,
    "plex": _plex,
    "plex-windowed": _windowed,
    "snapshot": _snapshot,
    "live": _live,
}


@pytest.fixture(
    params=[
        "fake",
        "plex",
        "plex-windowed",
        "snapshot",
        pytest.param("live", marks=pytest.mark.live),
    ]
)
def subject(request) -> Iterator[Subject]:
    made = SUBJECTS[request.param]()
    mark = len(made.covered) if made.covered is not None else 0
    yield made
    if made.covered is not None and request.node.get_closest_marker("live") is not None:
        request.config.stash[LIVE_COVERAGE].extend(
            f"{request.node.name}: {line}" for line in dict.fromkeys(made.covered[mark:])
        )


# -- walking a subject --------------------------------------------------------


def listable(subject: Subject) -> list[SectionRef]:
    """The sections that can be listed. Every other one must say it is unsupported
    -- the only legitimate reason a section from `sections()` cannot be listed."""
    found = []
    for section in subject.provider.sections():
        try:
            subject.provider.list_items(section.section_id, 0, 0)
        except LibraryUnsupported:
            continue
        assert section.section_type in SECTION_KINDS, (
            f"section {section.section_id} is a {section.section_type!r} section, which is not "
            "modelled, yet it listed without raising LibraryUnsupported"
        )
        found.append(section)
    return found


def listing(subject: Subject, section: SectionRef, kind: MediaKind | None) -> tuple[ItemStub, ...]:
    """A whole listing, or under a window, its first `window` entries."""
    if subject.window is None:
        page = subject.provider.list_items(section.section_id, 0, EVERYTHING, kind)
        assert page.returned == page.total, "EVERYTHING is not everything; raise it"
        return page.items
    page = subject.provider.list_items(section.section_id, 0, subject.window, kind)
    subject.cover(
        f"section {section.section_id} {kind or 'default'}: first {page.returned} of {page.total}"
    )
    return page.items


def total_of(subject: Subject, section: SectionRef, kind: MediaKind | None) -> int:
    return subject.provider.list_items(section.section_id, 0, 0, kind).total


def every_listing(subject: Subject) -> Iterator[tuple[SectionRef, MediaKind, tuple[ItemStub, ...]]]:
    for section in listable(subject):
        for kind in SECTION_KINDS[section.section_type]:
            yield section, kind, listing(subject, section, kind)


def every_stub(subject: Subject) -> list[ItemStub]:
    return [stub for _, _, stubs in every_listing(subject) for stub in stubs]


def children(subject: Subject, item_id: ItemId) -> tuple[ItemStub, ...]:
    if subject.window is None:
        page = subject.provider.get_children(item_id, 0, EVERYTHING)
        assert page.returned == page.total
        return page.items
    page = subject.provider.get_children(item_id, 0, subject.window)
    if page.returned < page.total:
        subject.cover(f"children of {item_id}: first {page.returned} of {page.total}")
    return page.items


def windows(subject: Subject, n: int) -> list[tuple[int, int]]:
    """`(offset, limit)` pairs over a listing of which `n` entries were read: every
    pair, or under a window a spread of them, each inside what was read."""
    if subject.window is None:
        return list(product(range(n + 2), range(n + 2)))
    offsets = sorted({0, 1, n // 2, max(n - 1, 0)})
    limits = sorted({0, 1, 2, n})
    return [(offset, limit) for offset in offsets for limit in limits if offset + limit <= n]


def assert_pages_partition(
    subject: Subject,
    full: tuple[ItemStub, ...],
    fetch: Callable[[int, int], tuple[ItemStub, ...]],
) -> None:
    """Pages of every size, concatenated from 0, reproduce `full`: no gap, no
    duplicate, the same order. Under a window, the last page is trimmed to what
    was read, and the sizes are a spread."""
    n = len(full)
    sizes = range(1, n + 2) if subject.window is None else sorted({1, 2, 7, n} - {0})
    for size in sizes:
        pages = [
            fetch(offset, size if subject.window is None else min(size, n - offset))
            for offset in range(0, n, size)
        ]
        assert tuple(stub for page in pages for stub in page) == full, size


def a_mark(subject: Subject) -> int:
    return len(subject.server.queries) if subject.server else 0


def no_request_since(subject: Subject, mark: int) -> bool:
    return subject.server is None or subject.server.requests_since(mark) == []


# -- P1 to P5: paging ---------------------------------------------------------


@pytest.mark.parametrize(("offset", "limit"), [(-1, 1), (0, -1), (-3, -3)])
def test_p1_invalid_paging_is_refused_before_any_request(subject, offset, limit):
    section = listable(subject)[0]
    parent = next(s for s in every_stub(subject) if s.media_kind in CHILD_KIND)
    for call in (
        lambda: subject.provider.list_items(section.section_id, offset, limit),
        lambda: subject.provider.get_children(parent.item_id, offset, limit),
    ):
        mark = a_mark(subject)
        with pytest.raises(LibraryInvalidArgument) as caught:
            call()
        assert caught.value.retryability is Retryability.CORRECTABLE
        assert caught.value.next_action
        assert no_request_since(subject, mark)


def test_p2_a_page_reports_its_own_window(subject):
    for section, kind, full in every_listing(subject):
        total = total_of(subject, section, kind)
        for offset, limit in windows(subject, len(full)):
            page = subject.provider.list_items(section.section_id, offset, limit, kind)
            assert page.offset == offset
            assert page.total == total
            assert page.items == full[offset : offset + limit]


def test_p3_pages_of_any_size_partition_the_listing(subject):
    for section, kind, full in every_listing(subject):
        assert_pages_partition(
            subject,
            full,
            lambda offset, limit, s=section, k=kind: (
                subject.provider.list_items(s.section_id, offset, limit, k).items
            ),
        )


def test_p4_past_the_end_and_count_only_pages_are_empty_with_the_true_total(subject):
    for section, kind, _ in every_listing(subject):
        total = total_of(subject, section, kind)
        for offset, limit in ((total, 5), (total + 7, 5), (0, 0)):
            page = subject.provider.list_items(section.section_id, offset, limit, kind)
            assert (page.items, page.returned, page.total) == ((), 0, total)


def test_p5_the_default_kind_is_the_root_and_a_foreign_kind_is_refused(subject):
    for section in listable(subject):
        root = SECTION_ROOT_KIND[section.section_type]
        assert listing(subject, section, None) == listing(subject, section, root)
        for kind in set(MediaKind) - set(SECTION_KINDS[section.section_type]):
            with pytest.raises(LibraryInvalidArgument):
                subject.provider.list_items(section.section_id, 0, 5, kind)


# -- P6 to P9: items -----------------------------------------------------------


def _bytes(value) -> bytes:
    if isinstance(value, tuple):
        return b"\n".join(_bytes(entry) for entry in value)
    if isinstance(value, BaseModel):
        return canonical_json(value.model_dump(mode="json"))
    return canonical_json(dataclasses.asdict(value))


def test_p6_identical_calls_return_identical_bytes(subject):
    provider = subject.provider
    stub = every_stub(subject)[0]
    section = listable(subject)[0]
    calls = [
        provider.provider_info,
        provider.sections,
        lambda: provider.list_items(section.section_id, 0, 3),
        lambda: provider.get_item(stub.item_id),
        lambda: provider.get_children(stub.item_id, 0, 3),
        lambda: provider.get_files(stub.item_id),
        lambda: provider.find_similar(section.section_id, stub.title[:3], 5),
    ]
    for call in calls:
        assert _bytes(call()) == _bytes(call())


def test_p7_every_listed_stub_is_fetchable_and_agrees_with_its_item(subject):
    for stub in every_stub(subject):
        item = subject.provider.get_item(stub.item_id)
        assert item.item_id == stub.item_id
        assert item.media_kind is stub.media_kind
        assert item.title == stub.title
        assert getattr(item, "year", None) == stub.year


def test_p8_children_are_exactly_the_items_whose_parent_is_the_argument(subject):
    """Every child names the argument as its parent. *Exactly* those is checked only
    over a whole library: under a window a child may lie past its own listing's
    window, so the run records that completeness went unchecked."""
    provider = subject.provider
    items = [provider.get_item(stub.item_id) for stub in every_stub(subject)]
    for item in items:
        found = children(subject, item.item_id)
        for stub in found:
            assert getattr(provider.get_item(stub.item_id), "parent", None) == item.item_id
        if subject.window is None:
            expected = {
                str(other.item_id)
                for other in items
                if getattr(other, "parent", None) == item.item_id
            }
            assert {str(stub.item_id) for stub in found} == expected
        if item.media_kind not in CHILD_KIND:
            assert found == ()
        assert_pages_partition(
            subject,
            found,
            lambda offset, limit, i=item.item_id: provider.get_children(i, offset, limit).items,
        )
    subject.cover("children: each child's parent checked; completeness not checkable in a window")


def test_p9_files_are_the_items_parts(subject):
    for stub in every_stub(subject):
        assert subject.provider.get_files(stub.item_id) == getattr(
            subject.provider.get_item(stub.item_id), "parts", ()
        )


# -- P10: search ----------------------------------------------------------------


def test_p10_a_search_returns_only_the_sections_roots_and_at_most_limit(subject):
    """The half of P10 every subject owes, model or not. Queries come from every
    kind's titles, so a search that leaks a season or a book is asked for one."""
    provider = subject.provider
    for section in listable(subject):
        root = SECTION_ROOT_KIND[section.section_type]
        roots = listing(subject, section, root)
        titles = {
            stub.title
            for kind in SECTION_KINDS[section.section_type]
            for stub in listing(subject, section, kind)
        }
        for query in sorted(titles):
            found = provider.find_similar(section.section_id, query, EVERYTHING)
            for stub in found:
                assert stub.media_kind is root, (section.section_id, query, stub)
                assert stub.item_id.section_id == section.section_id
            if subject.window is None:
                assert all(stub in roots for stub in found), (section.section_id, query)
                assert list(found) == [stub for stub in roots if stub in found], "listing order"
            limits = range(len(found) + 1) if subject.window is None else {0, 1, len(found)}
            for limit in sorted(limits):
                assert provider.find_similar(section.section_id, query, limit) == found[:limit]
        for stub in roots:
            assert stub in provider.find_similar(section.section_id, stub.title, EVERYTHING)


def _queries(title: str) -> set[str]:
    return {title, title.casefold(), title.upper(), title[:3]}


def test_p10_a_search_finds_exactly_the_matching_roots_in_listing_order(subject):
    if not subject.follows_model:
        pytest.skip("this subject does not claim the model of Plex")
    if subject.window is not None:
        _probe_title_matching(subject)
        return
    for section in listable(subject):
        roots = listing(subject, section, SECTION_ROOT_KIND[section.section_type])
        records = [subject.provider.get_item(stub.item_id) for stub in roots]
        for query in sorted({q for stub in roots for q in _queries(stub.title)}):
            expected = tuple(
                stub
                for stub, record in zip(roots, records, strict=True)
                if title_matches(query, record)
            )
            for limit in (0, 1, EVERYTHING):
                found = subject.provider.find_similar(section.section_id, query, limit)
                assert found == expected[:limit], (section.section_id, query, limit)


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    kept = "".join(c for c in decomposed if not unicodedata.combining(c))
    return unicodedata.normalize("NFC", kept)


def _probes(record: NormalizedItem) -> list[tuple[str, str]]:
    """The three open questions about `title=` (plan §8), as queries for one item:
    does it ignore case, does it fold accents, does it match the sort title?"""
    found = [("case-flipped", record.title.swapcase())]
    if _strip_accents(record.title) != record.title:
        found.append(("accent-stripped", _strip_accents(record.title)))
    if record.title_sort and fold_text(record.title_sort) != fold_text(record.title):
        found.append(("title-sort", record.title_sort))
    return [(kind, probe) for kind, probe in found if probe.strip()]


def _probe_title_matching(subject: Subject) -> None:
    """P10 against a library too large to list whole: probe each root in the window,
    and check two things the window can see. The probed root is found exactly when
    the model says it matches, and everything found matches. Each disagreement is a
    counterexample to `title_matches`, and the function changes, not this test."""
    provider = subject.provider
    counterexamples: list[str] = []
    for section in listable(subject):
        tally: Counter[str] = Counter()
        for stub in listing(subject, section, SECTION_ROOT_KIND[section.section_type]):
            record = provider.get_item(stub.item_id)
            for kind, probe in _probes(record):
                tally[kind] += 1
                found = provider.find_similar(section.section_id, probe, EVERYTHING)
                expected = title_matches(probe, record)
                if (stub in found) != expected:
                    counterexamples.append(
                        f"{kind} {probe!r} for {record.title!r}: Plex "
                        f"{'found' if stub in found else 'did not find'} it; the model says it "
                        f"{'matches' if expected else 'does not'}"
                    )
                for other in found:
                    if not title_matches(probe, provider.get_item(other.item_id)):
                        counterexamples.append(
                            f"{kind} {probe!r} found {other.title!r}, which the model says it "
                            "does not match"
                        )
        subject.cover(f"section {section.section_id} title probes: {dict(sorted(tally.items()))}")
    assert not counterexamples, "\n".join(counterexamples)


@pytest.mark.parametrize(
    ("server_matches", "kind"),
    [
        (lambda query, record: title_matches(_strip_accents(query), _unaccented(record)), "accent"),
        (
            lambda query, record: (
                title_matches(query, record)
                or fold_text(query) in fold_text(record.title_sort or "")
            ),
            "title-sort",
        ),
    ],
)
def test_the_live_probe_reports_a_server_that_departs_from_the_model(
    monkeypatch, server_matches, kind
):
    """The probes exist to overturn `title_matches`, so they are shown overturning
    it: the fake server is made to fold accents, or to match the sort title, as
    Plex might. The windowed run must name the probe that disagreed."""
    monkeypatch.setattr("tests.library.fake_plex.title_matches", server_matches)
    with pytest.raises(AssertionError) as caught:
        _probe_title_matching(_windowed())
    assert f"{kind}" in str(caught.value)
    assert "the model says it does not" in str(caught.value)


def _unaccented(record: NormalizedItem) -> NormalizedItem:
    return record.model_copy(update={"title": _strip_accents(record.title)})


# -- P11 to P14: errors, types, labels -------------------------------------------


def test_p11_every_edge_error_has_its_declared_type_and_advice(subject):
    provider = subject.provider
    stub = every_stub(subject)[0]
    elsewhere = next(s for s in listable(subject) if s.section_id != stub.item_id.section_id)
    misfiled = subject.id(elsewhere.section_id, stub.item_id.rating_key)

    with pytest.raises(LibrarySectionNotFound) as caught:
        provider.list_items("999", 0, 5)
    assert "sections" in caught.value.next_action

    for item_id in (
        # Far past any real library's keys, so the live server cannot hold it.
        subject.id(stub.item_id.section_id, "999999999"),
        subject.id(stub.item_id.section_id, "sw3f9a"),
        ItemId("elsewhere", stub.item_id.section_id, stub.item_id.rating_key),
    ):
        with pytest.raises(LibraryItemNotFound) as caught:
            provider.get_item(item_id)
        assert caught.value.retryability is Retryability.CORRECTABLE
        assert caught.value.next_action

    with pytest.raises(LibraryItemNotFound) as caught:
        provider.get_item(misfiled)
    assert caught.value.next_action == f"use {stub.item_id}"

    with pytest.raises(LibraryInvalidArgument):
        provider.find_similar(stub.item_id.section_id, "  ", 5)
    with pytest.raises(ValueError, match="what a listing returns"):
        provider.get_item(stub.item_id, FetchProfile.STUB)


def test_p11_each_profile_is_answered_or_declared_unsupported(subject):
    stub = every_stub(subject)[0]
    for profile in REQUESTABLE:
        if profile in subject.profiles:
            assert subject.provider.get_item(stub.item_id, profile).fetched is profile
        else:
            with pytest.raises(LibraryUnsupported):
                subject.provider.get_item(stub.item_id, profile)


def _adversarial(subject: Subject):
    """Arguments chosen to reach every edge: empty, unknown, foreign, negative,
    huge, non-decimal, NFD. Every section the subject names is included, so an
    unmodelled one is reached through every method too."""
    provider = subject.provider
    sections = sorted(
        {"1", "2", "3", "5", "999", "x", *(s.section_id for s in provider.sections())}
    )
    kinds = [None, MediaKind.MOVIE, MediaKind.EPISODE, MediaKind.AUDIOBOOK_PART]
    offsets = [-1, 0, 1, 10**9]
    # A huge limit walks a live section to its end, thousands of items at a time;
    # under a window the largest limit sent is the window, and the run says so.
    limits = [-1, 0, 1, 10**9 if subject.window is None else subject.window]
    if subject.window is not None:
        subject.cover(f"adversarial: limits capped at {subject.window}, not 10**9")
    labels = [subject.label, "plex", "other"]
    keys = ["101", "1701", "2", "999999", "sw3f9a", "١٢", "0"]
    titles = ["", " ", "a", "Amélie", "Amélie", "%", "x" * 300]
    for section, offset, limit, kind in product(sections, offsets, limits, kinds):
        yield lambda s=section, o=offset, n=limit, k=kind: provider.list_items(s, o, n, k)
    for section, title, limit in product(sections, titles, limits):
        yield lambda s=section, t=title, n=limit: provider.find_similar(s, t, n)
    for label, section, key in product(labels, sections, keys):
        item_id = ItemId(label, section, key)
        yield lambda i=item_id: provider.get_files(i)
        yield lambda i=item_id: provider.get_children(i, 0, 5)
        for profile in REQUESTABLE:
            yield lambda i=item_id, p=profile: provider.get_item(i, p)


def test_p12_nothing_outside_the_taxonomy_escapes(subject):
    """Every call either answers or raises a `LibraryError`. A bare `ValueError`
    from `int()` or a `KeyError` from a dict would be a bug the model can trigger
    with a typo; the one programmer error, `STUB`, is P11's."""
    for call in _adversarial(subject):
        with contextlib.suppress(LibraryError):
            call()


LEAF_TYPES = (str, int, float, bool, type(None), date, datetime, Enum, bytes)
# Walked into rather than judged: what matters is what they hold.
CONTAINER_TYPES = (tuple, list)


def _walk(value) -> Iterator[object]:
    yield value
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _walk(getattr(value, name))
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            yield from _walk(getattr(value, field.name))
    elif isinstance(value, tuple | list):
        for entry in value:
            yield from _walk(entry)


def test_p13_every_returned_value_is_a_model_type(subject):
    provider = subject.provider
    returned: list[object] = [provider.provider_info(), provider.sections()]
    for stub in every_stub(subject):
        returned += [stub, provider.get_item(stub.item_id), provider.get_files(stub.item_id)]
        returned.append(provider.get_children(stub.item_id, 0, EVERYTHING))
    for value in (v for r in returned for v in _walk(r)):
        module = type(value).__module__
        ours = module.startswith("shelfwarden")
        allowed = ours or isinstance(value, LEAF_TYPES + CONTAINER_TYPES)
        assert allowed, type(value)


def test_p14_every_served_id_carries_the_providers_label(subject):
    for stub in every_stub(subject):
        item = subject.provider.get_item(stub.item_id)
        for ref in (
            stub.item_id,
            item.item_id,
            getattr(item, "parent", None),
            getattr(item, "grandparent", None),
        ):
            assert ref is None or ref.provider == subject.label


# -- P15: order -------------------------------------------------------------------


def test_p15_listings_children_and_sections_are_in_the_models_order(subject):
    if not subject.follows_model:
        pytest.skip("this subject does not claim the model of Plex")
    provider = subject.provider
    sections = provider.sections()
    assert list(sections) == sorted(sections, key=section_key)

    def records(stubs) -> list[NormalizedItem]:
        return [provider.get_item(stub.item_id) for stub in stubs]

    for _, _, full in every_listing(subject):
        served = records(full)
        assert served == sorted(served, key=listing_key)
        for item in served:
            kids = records(children(subject, item.item_id))
            assert kids == sorted(kids, key=children_key)


# -- the differential ---------------------------------------------------------------


@pytest.fixture(scope="module")
def exported_server(tmp_path_factory) -> tuple[PlexLibrary, ExportResult, CaseWorld]:
    """`PlexLibrary` over the fake server, exported whole, and the world of that
    export with no delta: the source, what it wrote, and what a snapshot serves."""
    source = PlexLibrary(server=FakePlexServer())
    result = run_export(source, tmp_path_factory.mktemp("export"), count=None)
    return source, result, ExportedLibrary.read(result.directory).world()


def _served(addressing: Addressing, stubs: tuple[ItemStub, ...]) -> tuple[ItemStub, ...]:
    return tuple(
        ItemStub(
            item_id=addressing.served(stub.item_id),
            media_kind=stub.media_kind,
            title=stub.title,
            year=stub.year,
        )
        for stub in stubs
    )


def _raised(call: Callable[[], object]) -> LibraryError:
    with pytest.raises(LibraryError) as caught:
        call()
    return caught.value


class TestDifferential:
    """A snapshot of an export of the fake server, against the server itself,
    answer for answer through `Addressing`.

    This is the test that `SnapshotLibrary` serves an export the way its source
    served it. For server semantics it is circular by construction -- the fake
    server orders and matches with the snapshot's own functions -- so what it
    checks is the rest: that nothing is lost or changed between `PlexLibrary`'s
    answers, the export, the world builder and the snapshot. The three declared
    differences are each asserted here, so none can drift silently.
    """

    def test_the_snapshot_lists_exactly_the_sections_its_export_holds(self, exported_server):
        """Declared difference 1: Plex also lists sections this project does not
        model. Each of those refuses to list; nothing else is missing."""
        source, _, world = exported_server
        held = world.provider.sections()
        kept = {section.section_id for section in held}
        assert held == tuple(s for s in source.sections() if s.section_id in kept)
        for section in source.sections():
            if section.section_id not in kept:
                assert isinstance(
                    _raised(lambda s=section: source.list_items(s.section_id, 0, 1)),
                    LibraryUnsupported,
                )
        assert len(kept) == 3

    def test_every_page_of_every_listing_agrees(self, exported_server):
        source, _, world = exported_server
        for section in world.provider.sections():
            for kind in (None, *SECTION_KINDS[section.section_type]):
                total = source.list_items(section.section_id, 0, 0, kind).total
                for offset, limit in product(range(total + 2), range(total + 2)):
                    theirs = source.list_items(section.section_id, offset, limit, kind)
                    ours = world.provider.list_items(section.section_id, offset, limit, kind)
                    assert ours.items == _served(world.addressing, theirs.items)
                    assert (ours.total, ours.offset, ours.returned) == (
                        theirs.total,
                        theirs.offset,
                        theirs.returned,
                    )

    def test_every_item_its_children_and_its_files_agree(self, exported_server):
        source, result, world = exported_server
        addressing, snapshot = world.addressing, world.provider
        assert len(result.items) == 16
        for record in result.items:
            item_id = record.item_id
            served = addressing.served(item_id)
            theirs = source.get_item(item_id, result.manifest.profile)
            ours = addressing.unserve(snapshot.get_item(served, result.manifest.profile))
            assert canonical_json(dump_item(ours)) == canonical_json(dump_item(theirs))
            assert snapshot.get_files(served) == source.get_files(item_id)
            total = source.get_children(item_id, 0, 0).total
            for offset, limit in product(range(total + 2), range(total + 2)):
                assert snapshot.get_children(served, offset, limit).items == _served(
                    addressing, source.get_children(item_id, offset, limit).items
                )

    def test_another_profile_is_refused_where_the_source_answers(self, exported_server):
        """Declared difference 2: the snapshot answers the one profile its export
        fetched; the server answers any."""
        source, result, world = exported_server
        record = result.items[0]
        other = next(p for p in REQUESTABLE if p is not result.manifest.profile)
        assert source.get_item(record.item_id, other).fetched is other
        refused = _raised(
            lambda: world.provider.get_item(world.addressing.served(record.item_id), other)
        )
        assert isinstance(refused, LibraryUnsupported)

    def test_every_search_agrees(self, exported_server):
        source, result, world = exported_server
        titles = {record.title for record in result.items}
        for section in world.provider.sections():
            for query in sorted({q for title in titles for q in _queries(title)}):
                for limit in (0, 1, EVERYTHING):
                    assert world.provider.find_similar(section.section_id, query, limit) == _served(
                        world.addressing, source.find_similar(section.section_id, query, limit)
                    ), (section.section_id, query, limit)

    def test_every_edge_error_agrees(self, exported_server):
        """The same refusal, of the same type and retryability, with advice in both.
        A misfiled id is answered with the right one, translated."""
        source, result, world = exported_server
        movie = result.items[0]
        elsewhere = next(
            s.section_id
            for s in world.provider.sections()
            if s.section_id != movie.item_id.section_id
        )

        def edges(provider: LibraryProvider, label: str) -> list[Callable[[], object]]:
            section = movie.item_id.section_id
            return [
                lambda: provider.list_items("999", 0, 5),
                lambda: provider.list_items(section, -1, 5),
                lambda: provider.list_items(section, 0, 5, MediaKind.EPISODE),
                lambda: provider.find_similar(section, " ", 5),
                lambda: provider.find_similar(section, "a", -1),
                lambda: provider.get_item(ItemId(label, section, "999999")),
                lambda: provider.get_item(ItemId(label, section, "sw3f9a")),
                lambda: provider.get_files(ItemId(label, section, "999999")),
                lambda: provider.get_children(ItemId(label, section, "999999"), 0, 5),
                lambda: provider.get_item(ItemId(label, elsewhere, movie.item_id.rating_key)),
            ]

        pairs = zip(
            edges(source, source.provider_info().provider),
            edges(world.provider, PROVIDER),
            strict=True,
        )
        for theirs, ours in ((_raised(a), _raised(b)) for a, b in pairs):
            assert type(ours) is type(theirs)
            assert ours.retryability is theirs.retryability
            assert bool(ours.next_action) == bool(theirs.next_action)
            if theirs.next_action.startswith("use "):
                right = ItemId.parse(theirs.next_action.removeprefix("use "))
                assert ours.next_action == f"use {world.addressing.served(right)}"

    def test_ids_are_relabelled_and_nothing_else(self, exported_server):
        """Declared difference 3: every id carries the snapshot's label, and with no
        delta, every rating key is the source's own."""
        source, result, world = exported_server
        assert source.provider_info().provider == "plex"
        assert world.provider.provider_info().provider == PROVIDER
        assert world.addressing.reissued() == ()
        for record in result.items:
            served = world.addressing.served(record.item_id)
            assert (served.section_id, served.rating_key) == (
                record.item_id.section_id,
                record.item_id.rating_key,
            )


# -- the round trip -----------------------------------------------------------------


def _unserve_ids(value: object, addressing: Addressing) -> object:
    """A parsed JSON document with every served id string put back in dataset ids."""
    if isinstance(value, dict):
        return {key: _unserve_ids(inner, addressing) for key, inner in value.items()}
    if isinstance(value, list):
        return [_unserve_ids(inner, addressing) for inner in value]
    if isinstance(value, str) and value.startswith(f"{PROVIDER}:") and value.count(":") == 2:
        return str(addressing.dataset(ItemId.parse(value)))
    return value


class TestRoundTrip:
    def test_exporting_the_snapshot_reproduces_the_export(self, exported_server, tmp_path):
        """0.4 promised that an export of a snapshot reproduces the export it was
        made from (step 0.4, §7). Here it is checked: the records byte for byte and
        the population index too, both modulo addressing.

        In full (`count=None`): the export samples roots in listing order, and the
        snapshot's order is the model's rather than the server's, so a partial
        export of each would choose different roots (plan §8)."""
        _, result, world = exported_server
        again = run_export(world.provider, tmp_path / "again", count=None)
        unserved = render_items(world.addressing.unserve(record) for record in again.items)
        assert unserved == (result.directory / ITEMS_FILE).read_bytes()

        roots = load_roots(again.directory)
        assert (
            render_roots(
                ItemStub(
                    item_id=world.addressing.dataset(stub.item_id),
                    media_kind=stub.media_kind,
                    title=stub.title,
                    year=stub.year,
                )
                for stub in roots
            )
            == (result.directory / ROOTS_FILE).read_bytes()
        )
        # The census names example items, so it too matches only once translated.
        census = json.loads((again.directory / CENSUS_FILE).read_bytes())
        assert (
            canonical_json(_unserve_ids(census, world.addressing))
            == (result.directory / CENSUS_FILE).read_bytes()
        )

        assert again.manifest.provider.provider == PROVIDER
        assert again.manifest.provider.server_id == world.world_id
        assert again.manifest.profile == result.manifest.profile
        assert again.manifest.sections == result.manifest.sections
        assert again.manifest.counts == result.manifest.counts
