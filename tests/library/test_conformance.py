"""One suite, every provider: the properties in §4.6 of the step-0.7 plan.

Each property is a statement about *any* `LibraryProvider` over *any* library,
written so it can run against a subject without knowing what that subject holds.
The same tests can then run against the real server later, under `-m live`.

Subjects so far:

* `snapshot` -- `SnapshotLibrary` over the hand-built records
  (`tests/library/snapshots.py`).
* `plex` -- `PlexLibrary` over `FakePlexServer`, with nothing faked above
  plexapi's network seam.

Step 0.7.7 adds `FakeLibrary`, the differential (a snapshot of an export of the
fake server, compared answer by answer), and the round trip.

Two properties are about the model of Plex (P10, P15). Both offline subjects
follow it by construction -- the fake server orders and matches with the
snapshot's own functions -- so offline these show only that `PlexLibrary`'s client
logic preserves the model. Whether Plex follows it is the live run's question.
"""

import contextlib
import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from itertools import product

import pytest
from pydantic import BaseModel

from shelfwarden.canonical import canonical_json
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
from shelfwarden.library.snapshot import children_key, listing_key, section_key, title_matches
from shelfwarden.models.hierarchy import CHILD_KIND
from shelfwarden.models.ids import ItemId
from shelfwarden.models.item import FetchProfile, ItemStub, MediaKind, NormalizedItem, SectionRef
from tests.library.fake_plex import FakePlexServer
from tests.library.snapshots import hand_built

REQUESTABLE = (FetchProfile.CORE, FetchProfile.FULL)

# Big enough to take any offline section in one call. PlexLibrary still pages it
# underneath, 100 at a time, which is part of what is being tested.
EVERYTHING = 10_000


@dataclass(frozen=True)
class Subject:
    name: str
    provider: LibraryProvider
    profiles: frozenset[FetchProfile]
    follows_model: bool
    # The request log, where there is a server to log requests.
    server: FakePlexServer | None = None

    @property
    def label(self) -> str:
        return self.provider.provider_info().provider

    def id(self, section_id: str, rating_key: str) -> ItemId:
        return ItemId(self.label, section_id, rating_key)


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


SUBJECTS = {"snapshot": _snapshot, "plex": _plex}


@pytest.fixture(params=sorted(SUBJECTS))
def subject(request) -> Subject:
    return SUBJECTS[request.param]()


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


def listing(subject: Subject, section: SectionRef, kind: MediaKind) -> tuple[ItemStub, ...]:
    page = subject.provider.list_items(section.section_id, 0, EVERYTHING, kind)
    assert page.returned == page.total, "EVERYTHING is not everything; raise it"
    return page.items


def every_listing(subject: Subject) -> Iterator[tuple[SectionRef, MediaKind, tuple[ItemStub, ...]]]:
    for section in listable(subject):
        for kind in SECTION_KINDS[section.section_type]:
            yield section, kind, listing(subject, section, kind)


def every_stub(subject: Subject) -> list[ItemStub]:
    return [stub for _, _, stubs in every_listing(subject) for stub in stubs]


def children(subject: Subject, item_id: ItemId) -> tuple[ItemStub, ...]:
    page = subject.provider.get_children(item_id, 0, EVERYTHING)
    assert page.returned == page.total
    return page.items


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
        for offset, limit in product(range(len(full) + 2), range(len(full) + 2)):
            page = subject.provider.list_items(section.section_id, offset, limit, kind)
            assert page.offset == offset
            assert page.total == len(full)
            assert page.items == full[offset : offset + limit]


def test_p3_pages_of_any_size_partition_the_listing(subject):
    for section, kind, full in every_listing(subject):
        for size in range(1, len(full) + 2):
            pages = [
                subject.provider.list_items(section.section_id, offset, size, kind).items
                for offset in range(0, len(full), size)
            ]
            assert tuple(stub for page in pages for stub in page) == full


def test_p4_past_the_end_and_count_only_pages_are_empty_with_the_true_total(subject):
    for section, kind, full in every_listing(subject):
        for offset, limit in ((len(full), 5), (len(full) + 7, 5), (0, 0)):
            page = subject.provider.list_items(section.section_id, offset, limit, kind)
            assert (page.items, page.returned, page.total) == ((), 0, len(full))


def test_p5_the_default_kind_is_the_root_and_a_foreign_kind_is_refused(subject):
    for section in listable(subject):
        root = SECTION_ROOT_KIND[section.section_type]
        default = subject.provider.list_items(section.section_id, 0, EVERYTHING)
        assert default.items == listing(subject, section, root)
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
    items = [subject.provider.get_item(stub.item_id) for stub in every_stub(subject)]
    for item in items:
        found = children(subject, item.item_id)
        expected = {
            str(other.item_id) for other in items if getattr(other, "parent", None) == item.item_id
        }
        assert {str(stub.item_id) for stub in found} == expected
        if item.media_kind not in CHILD_KIND:
            assert found == ()
        for size in range(1, len(found) + 2):
            pages = [
                subject.provider.get_children(item.item_id, offset, size).items
                for offset in range(0, len(found), size)
            ]
            assert tuple(stub for page in pages for stub in page) == found


def test_p9_files_are_the_items_parts(subject):
    for stub in every_stub(subject):
        assert subject.provider.get_files(stub.item_id) == getattr(
            subject.provider.get_item(stub.item_id), "parts", ()
        )


# -- P10: search ----------------------------------------------------------------


def _queries(title: str) -> set[str]:
    return {title, title.casefold(), title.upper(), title[:3]}


def test_p10_a_search_finds_exactly_the_matching_roots_in_listing_order(subject):
    if not subject.follows_model:
        pytest.skip("this subject does not claim the model of Plex")
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
        subject.id(stub.item_id.section_id, "999999"),
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
    huge, non-decimal, NFD."""
    provider = subject.provider
    sections = ["1", "2", "3", "5", "999", "x"]
    kinds = [None, MediaKind.MOVIE, MediaKind.EPISODE, MediaKind.AUDIOBOOK_PART]
    numbers = [-1, 0, 1, 10**9]
    labels = [subject.label, "plex", "other"]
    keys = ["101", "1701", "2", "999999", "sw3f9a", "١٢", "0"]
    titles = ["", " ", "a", "Amélie", "Amélie", "%", "x" * 300]
    for section, offset, limit, kind in product(sections, numbers, numbers, kinds):
        yield lambda s=section, o=offset, n=limit, k=kind: provider.list_items(s, o, n, k)
    for section, title, limit in product(sections, titles, numbers):
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
