"""The world builder: a case, served as a library Plex could have served.

Step 0.7.6's gate is that every case in the fixture dataset builds a world that
serves `apply_changes(export, delta)` byte for byte through `Addressing`, and that
no served key is non-decimal. The first is checked through the protocol -- every
section, every kind, every record fetched -- because "the snapshot was built from
the right records" and "the snapshot serves them" are different claims.

The rest is what keeps the world an honest instrument:

* **binding** -- a dataset is refused against any export but its own;
* **integrity** -- a world is no less coherent than its export, and a quirk the
  export already had is inherited, not introduced;
* **addressing** -- the served address space carries no tell that a real server
  would not, and no served address is also a live one;
* **confinement** -- nothing reachable from the provider holds a record the
  corruption changed.
"""

import dataclasses
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import BaseModel

from shelfwarden.canonical import canonical_json
from shelfwarden.evals.composition import COMPOSITION_FILE
from shelfwarden.evals.corrupt.model import ItemChange
from shelfwarden.evals.corrupt.reverse import apply_changes, diff_items, render_family
from shelfwarden.evals.corrupt.run import read_export_with_population, run_corruptions
from shelfwarden.evals.curated import CURATED_ROOT
from shelfwarden.evals.export import ITEMS_FILE, MANIFEST_FILE, run_export
from shelfwarden.evals.generate import DATASET_FILE, DELTAS_FILE, TRUTH_FILE, run_generate
from shelfwarden.evals.truth import RepairExpectation, load_truth
from shelfwarden.evals.world import (
    Addressing,
    AddressingError,
    CaseWorld,
    ExportedLibrary,
    WorldBuilder,
    WorldError,
    address,
    main,
    render_world,
    world_for_case,
)
from shelfwarden.library.base import SECTION_KINDS, LibraryItemNotFound, LibraryProvider
from shelfwarden.library.plex import PlexLibrary
from shelfwarden.library.snapshot import PROVIDER, SnapshotLibrary, WorldIntegrityError
from shelfwarden.models.hierarchy import Rule
from shelfwarden.models.ids import ItemId, is_decimal
from shelfwarden.models.item import (
    BaseItem,
    FilePart,
    MovieItem,
    NormalizedItem,
    SeasonItem,
    dump_item,
    with_changes,
)
from tests.library.fake_plex import FakePlexServer

from .conftest import BOOKS, MOVIES, SHOWS, FakeLibrary, _id, _library_records, _movie

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = str(Path(__file__).resolve().parent.parent)

# The fixture's Edgedancer declares a part it does not have: a derived copy the
# source already gets wrong, so every world inherits it.
QUIRK = ("fake:3:412", "/part_count")


@pytest.fixture(scope="module")
def export_directory(tmp_path_factory) -> Path:
    return run_export(FakeLibrary.build(), tmp_path_factory.mktemp("export"), count=200).directory


@pytest.fixture(scope="module")
def dataset_directory(export_directory, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("dataset") / "dataset"
    run_generate(
        export_directory,
        out,
        count=200,
        seed=1518,
        composition_path=REPO_ROOT / COMPOSITION_FILE,
        curated_root=REPO_ROOT / CURATED_ROOT,
    )
    return out


@pytest.fixture(scope="module")
def builder(export_directory, dataset_directory) -> WorldBuilder:
    return WorldBuilder.open(export_directory, dataset_directory)


@pytest.fixture(scope="module")
def worlds(builder) -> dict[str, CaseWorld]:
    return {case_id: builder.world(case_id) for case_id in builder.case_ids()}


@pytest.fixture(scope="module")
def deltas(dataset_directory) -> dict[str, tuple[ItemChange, ...]]:
    found = {}
    for line in (dataset_directory / DELTAS_FILE).read_bytes().splitlines():
        row = json.loads(line)
        found[row["case_id"]] = tuple(ItemChange.model_validate(c) for c in row["changes"])
    return found


@pytest.fixture(scope="module")
def truth(dataset_directory):
    return load_truth((dataset_directory / TRUTH_FILE).read_bytes())


@pytest.fixture(scope="module")
def exported(export_directory) -> ExportedLibrary:
    return ExportedLibrary.read(export_directory)


def served_records(provider: LibraryProvider) -> tuple[NormalizedItem, ...]:
    """Every record the provider serves, found through the protocol alone."""
    records = []
    for section in provider.sections():
        for kind in SECTION_KINDS[section.section_type]:
            total = provider.list_items(section.section_id, 0, 0, kind).total
            for stub in provider.list_items(section.section_id, 0, total, kind).items:
                records.append(provider.get_item(stub.item_id))
    return tuple(records)


def unserved(world: CaseWorld) -> bytes:
    return render_family([world.addressing.unserve(r) for r in served_records(world.provider)])


# -- the gate -----------------------------------------------------------------


class TestEveryCaseServesItsWorld:
    def test_the_fixture_dataset_has_cases_of_every_shape(self, deltas):
        """Guards the gate against a fixture too small to test it: some deltas add,
        some only modify, and some are empty."""
        kinds = {change.kind for changes in deltas.values() for change in changes}
        assert {"add", "modify"} <= {str(kind) for kind in kinds}
        assert any(not changes for changes in deltas.values())
        assert len(deltas) == 25

    def test_every_case_serves_its_delta_applied_to_the_export(self, worlds, deltas, exported):
        assert worlds.keys() == deltas.keys()
        for case_id, world in worlds.items():
            expected = render_family(apply_changes(exported.items, deltas[case_id]))
            assert unserved(world) == expected, case_id

    def test_no_served_key_is_non_decimal(self, worlds):
        for case_id, world in worlds.items():
            for record in served_records(world.provider):
                for name in ("item_id", "parent", "grandparent"):
                    value = getattr(record, name, None)
                    if value is not None:
                        assert value.provider == PROVIDER, (case_id, value)
                        assert is_decimal(value.rating_key), (case_id, value)

    def test_every_corruption_the_recipes_produce_builds_a_world(self, export_directory):
        """Wider than the dataset: the survey covers every recipe that applies to
        the fixture, including `absolute_vs_seasonal`'s REMOVE, which no fixture
        case draws."""
        manifest, items, roots = read_export_with_population(export_directory)
        survey = run_corruptions(export_id=manifest.export_id, items=items, roots=roots, seed=1518)
        exported = ExportedLibrary.read(export_directory)
        removes = 0
        for result in survey.results:
            world = exported.world(result.changes)
            assert unserved(world) == render_family(apply_changes(items, result.changes))
            for change in result.changes:
                if str(change.kind) == "remove":
                    removes += 1
                    gone = ItemId.parse(change.item_id)
                    # Out of the world, still addressable: the truth file names it.
                    assert world.addressing.dataset(world.addressing.served(gone)) == gone
                    with pytest.raises(LibraryItemNotFound):
                        world.provider.get_item(world.addressing.served(gone))
        assert removes


# -- binding --------------------------------------------------------------------


def _copy(directory: Path, to: Path) -> Path:
    shutil.copytree(directory, to)
    return to


class TestBinding:
    def test_a_dataset_is_refused_against_another_export(self, dataset_directory, tmp_path):
        records = {str(r.item_id): r for r in _library_records()}
        records["fake:1:101"] = with_changes(records["fake:1:101"], {"title": "Another Film"})
        other = run_export(FakeLibrary(records=records), tmp_path / "other", count=200)
        with pytest.raises(WorldError) as caught:
            WorldBuilder.open(other.directory, dataset_directory)
        message = str(caught.value)
        assert other.manifest.items_sha256 in message
        assert (
            json.loads((dataset_directory / DATASET_FILE).read_bytes())["source_export"][
                "items_sha256"
            ]
            in message
        )
        assert "regenerate" in message

    def test_an_export_edited_after_its_manifest_is_refused(self, export_directory, tmp_path):
        edited = _copy(export_directory, tmp_path / "edited")
        items = edited / ITEMS_FILE
        items.write_bytes(items.read_bytes().replace(b"Home Video", b"Home Videos", 1))
        with pytest.raises(WorldError, match="edited or truncated"):
            ExportedLibrary.read(edited)

    def test_a_census_only_export_is_refused_by_name(self, tmp_path):
        census = run_export(FakeLibrary.build(), tmp_path / "census", census_only=True)
        with pytest.raises(WorldError, match="census-only"):
            ExportedLibrary.read(census.directory)

    def test_a_directory_with_no_manifest_is_refused(self, tmp_path):
        with pytest.raises(WorldError, match="not an export directory"):
            ExportedLibrary.read(tmp_path)

    def test_a_directory_with_no_dataset_is_refused(self, export_directory, tmp_path):
        with pytest.raises(WorldError, match="not a dataset directory"):
            WorldBuilder.open(export_directory, tmp_path)

    def test_an_id_under_another_label_is_refused(self, export_directory, tmp_path):
        """The world relabels by the export's provider. An id under another label
        would be relabelled silently into the same address space, so it is refused."""
        edited = _copy(export_directory, tmp_path / "foreign")
        payload = (edited / ITEMS_FILE).read_bytes()
        lines = payload.splitlines(keepends=True)
        record = json.loads(lines[0])
        record["item_id"]["provider"] = "elsewhere"
        lines[0] = canonical_json(record) + b"\n"
        payload = b"".join(lines)
        (edited / ITEMS_FILE).write_bytes(payload)
        manifest = json.loads((edited / MANIFEST_FILE).read_bytes())
        manifest["items_sha256"] = sha256(payload).hexdigest()
        (edited / MANIFEST_FILE).write_bytes(canonical_json(manifest))
        with pytest.raises(WorldError, match="elsewhere:1:101"):
            ExportedLibrary.read(edited)

    def test_an_unknown_case_is_refused(self, builder):
        with pytest.raises(WorldError, match="not a case of dataset"):
            builder.world("case-000000000000")

    def test_a_duplicated_delta_line_is_refused(
        self, export_directory, dataset_directory, tmp_path
    ):
        edited = _copy(dataset_directory, tmp_path / "duplicated")
        lines = (edited / DELTAS_FILE).read_bytes().splitlines(keepends=True)
        (edited / DELTAS_FILE).write_bytes(b"".join([*lines, lines[3]]))
        case_id = json.loads(lines[3])["case_id"]
        builder = WorldBuilder.open(export_directory, edited)
        with pytest.raises(WorldError, match="2 lines"):
            builder.world(case_id)
        builder.world(json.loads(lines[4])["case_id"])

    def test_a_truncated_delta_file_is_refused(self, export_directory, dataset_directory, tmp_path):
        edited = _copy(dataset_directory, tmp_path / "truncated")
        lines = (edited / DELTAS_FILE).read_bytes().splitlines(keepends=True)
        (edited / DELTAS_FILE).write_bytes(b"".join(lines[:-1]))
        with pytest.raises(WorldError, match="24 case"):
            WorldBuilder.open(export_directory, edited)

    def test_a_delta_that_does_not_fit_the_export_names_its_case(
        self, export_directory, dataset_directory, tmp_path
    ):
        """Binding hashes the export, so this takes a hand edit. Strict application
        (step 0.7.5) catches it, and the builder says which case."""
        edited = _copy(dataset_directory, tmp_path / "hand-edited")
        lines = (edited / DELTAS_FILE).read_bytes().splitlines(keepends=True)
        index, row = next(
            (n, json.loads(line))
            for n, line in enumerate(lines)
            if any(c["kind"] == "modify" for c in json.loads(line)["changes"])
        )
        change = next(c for c in row["changes"] if c["kind"] == "modify")
        change["fields"][0]["before"] = "not what the export holds"
        lines[index] = canonical_json(row) + b"\n"
        (edited / DELTAS_FILE).write_bytes(b"".join(lines))
        with pytest.raises(WorldError, match=row["case_id"]):
            WorldBuilder.open(export_directory, edited).world(row["case_id"])

    def test_the_one_shot_form_builds_the_same_world(
        self, export_directory, dataset_directory, worlds
    ):
        case_id = sorted(worlds)[0]
        once = world_for_case(export_directory, dataset_directory, case_id)
        assert once.world_id == worlds[case_id].world_id


# -- integrity ------------------------------------------------------------------


class TestIntegrity:
    def test_a_quirk_in_the_source_is_inherited_not_introduced(self, exported, worlds):
        assert [(v.subject, v.path) for v in exported.derived] == [QUIRK]
        for case_id, world in worlds.items():
            assert [(v.subject, v.path) for v in world.inherited_violations] == [QUIRK], case_id
            record = world.provider.get_item(world.addressing.served(ItemId.parse(QUIRK[0])))
            assert record.part_count == 1

    def test_a_stale_derived_copy_is_refused_and_regenerating_is_named(self, exported):
        """A `wrong_match` on a show as 0.6 recorded it: the show retitled, its
        seasons and episodes still naming the true show."""
        show = next(r for r in exported.items if str(r.item_id) == "fake:2:201")
        changes = diff_items([show], [with_changes(show, {"title": "Space Dandy"})])
        with pytest.raises(WorldIntegrityError) as caught:
            exported.world(changes, case_id="case-old")
        stale = {(v.rule, v.subject, v.path) for v in caught.value.violations}
        assert (Rule.DERIVED_COPY, "fake:2:211", "/parent_title") in stale
        assert (Rule.DERIVED_COPY, "fake:2:2211", "/grandparent_title") in stale
        assert "case case-old" in str(caught.value)
        assert "Regenerate the dataset" in str(caught.value)

    def test_a_broken_tree_is_refused_and_regenerating_is_not_offered(self, exported):
        orphan = SeasonItem(
            item_id=_id(SHOWS, "299"),
            fetched=exported.manifest.profile,
            title="Season 9",
            parent=_id(SHOWS, "298"),
            index=9,
        )
        changes = diff_items([], [orphan])
        with pytest.raises(WorldIntegrityError) as caught:
            exported.world(changes, case_id="case-orphan")
        assert [(v.rule, v.subject) for v in caught.value.violations] == [
            (Rule.ORPHAN, "fake:2:299")
        ]
        assert "propagation cannot repair" in str(caught.value)
        assert "Regenerate" not in str(caught.value)


# -- addressing -----------------------------------------------------------------


def _part(media_id: str | None, part_id: str | None, name: str) -> FilePart:
    return FilePart(media_id=media_id, part_id=part_id, path=f"/m/{name}.mkv", container="mkv")


class TestAddress:
    def test_decimal_keys_are_kept_and_the_rest_reissued_above_them(self):
        source = (_movie("7", "A", 2001), _movie("40", "B", 2002))
        world = (
            *source,
            _movie("swb", "C", 2003),
            MovieItem(item_id=ItemId("fake", BOOKS, "swa"), fetched="core", title="X"),
        )
        addressing = address(source, world)
        assert addressing.served(_id(MOVIES, "7")) == ItemId(PROVIDER, MOVIES, "7")
        assert addressing.served(_id(MOVIES, "40")) == ItemId(PROVIDER, MOVIES, "40")
        # In `item_sort_key` order, across sections: Plex's keys are server-global.
        assert addressing.served(ItemId("fake", BOOKS, "swa")) == ItemId(PROVIDER, BOOKS, "41")
        assert addressing.served(_id(MOVIES, "swb")) == ItemId(PROVIDER, MOVIES, "42")
        assert [pair[0].rating_key for pair in addressing.reissued()] == ["swa", "swb"]

    def test_a_removed_items_key_is_never_reissued_to_another(self):
        """The largest key is taken over the export as well as the world, so a
        minted item cannot land on the address of one the delta took away."""
        source = (_movie("7", "A", 2001), _movie("99", "Gone", 2002))
        world = (source[0], _movie("swa", "New", 2003))
        addressing = address(source, world)
        assert addressing.served(_id(MOVIES, "swa")).rating_key == "100"
        assert addressing.served(_id(MOVIES, "99")).rating_key == "99"

    def test_added_parts_are_given_ids_and_existing_blanks_are_not(self):
        existing = _movie("1", "A", 2001, parts=(_part("900", "100", "a"), _part(None, None, "b")))
        added = _movie("swa", "A", 2001, parts=(_part(None, None, "c"), _part(None, "5", "d")))
        addressing = address((existing,), (existing, added))
        assert addressing.minted_files == 3

        kept = addressing.serve(existing)
        assert [(p.media_id, p.part_id) for p in kept.parts] == [("900", "100"), (None, None)]
        minted = addressing.serve(added)
        assert [(p.media_id, p.part_id) for p in minted.parts] == [("901", "101"), ("902", "5")]
        assert addressing.unserve(minted) == added
        assert addressing.unserve(kept) == existing

    def test_parent_links_follow_their_parent(self):
        records = [r for r in _library_records() if r.item_id.section_id == BOOKS]
        author = next(r for r in records if r.media_kind == "author")
        variant = with_changes(
            author,
            {
                "item_id": {"provider": "fake", "section_id": BOOKS, "rating_key": "swv"},
                "title": "B. Sanderson",
            },
        )
        book = next(r for r in records if r.media_kind == "audiobook")
        moved = with_changes(book, {"parent": dump_item(variant)["item_id"]})
        world = [*[r for r in records if r.item_id != book.item_id], variant, moved]
        addressing = address(records, world)
        served = addressing.serve(moved)
        assert served.parent == addressing.served(variant.item_id)
        assert is_decimal(served.parent.rating_key)
        assert addressing.unserve(served) == moved

    def test_an_unknown_id_is_an_addressing_error_in_both_directions(self):
        addressing = address((_movie("1", "A", 2001),), (_movie("1", "A", 2001),))
        with pytest.raises(AddressingError):
            addressing.served(_id(MOVIES, "2"))
        with pytest.raises(AddressingError):
            addressing.dataset(_id(MOVIES, "1"))

    def test_two_ids_cannot_share_one_address(self):
        served = ItemId(PROVIDER, MOVIES, "1")
        with pytest.raises(ValueError, match="both be served"):
            Addressing(PROVIDER, {_id(MOVIES, "1"): served, ItemId("x", MOVIES, "1"): served}, {})

    def test_an_address_outside_the_label_is_refused(self):
        with pytest.raises(ValueError, match="outside"):
            Addressing(PROVIDER, {_id(MOVIES, "1"): _id(MOVIES, "1")}, {})


class TestServedAddresses:
    def test_addressing_is_a_bijection_over_the_world(self, worlds):
        for case_id, world in worlds.items():
            pairs = world.addressing.pairs()
            assert len({served for _, served in pairs}) == len(pairs) == len(world.addressing)
            for source, served in pairs:
                assert world.addressing.dataset(served) == source
            for record in served_records(world.provider):
                assert world.addressing.served(world.addressing.dataset(record.item_id)) == (
                    record.item_id
                ), case_id

    def test_addressing_round_trips_every_id_a_truth_file_names(
        self, worlds, dataset_directory, exported
    ):
        """Every id anywhere in a case: `item_ids`, resolutions and keepers,
        postcondition keys, collateral, the ground truth, and the ids postconditions
        hold as *values* -- an episode's expected `/parent`."""
        label = exported.manifest.provider.provider
        payload = json.loads((dataset_directory / TRUTH_FILE).read_bytes())
        named = 0
        for case in payload["cases"]:
            world = worlds[case["case_id"]]
            for item_id in _ids_in(case, label):
                served = world.addressing.served(item_id)
                assert served.provider == PROVIDER and is_decimal(served.rating_key)
                assert world.addressing.dataset(served) == item_id
                named += 1
        assert named > 100

    def test_no_served_address_reveals_the_keeper(self, worlds, truth):
        """In the dataset's address space the minted member of every relation is
        the one whose key is not a number, so the keeper is the other. Served, every
        member is decimal and every part has its ids."""
        relations = 0
        for case in truth.cases:
            if not isinstance(case.expectation, RepairExpectation):
                continue
            for finding in case.expectation.required_findings:
                if finding.resolution is None:
                    continue
                relations += 1
                members = [ItemId.parse(i) for i in finding.resolution.item_ids]
                assert len({is_decimal(m.rating_key) for m in members}) == 2, case.case_id
                world = worlds[case.case_id]
                served = [world.addressing.served(m) for m in members]
                assert all(is_decimal(s.rating_key) for s in served), case.case_id
                for item_id in served:
                    for part in getattr(world.provider.get_item(item_id), "parts", ()):
                        assert part.media_id is not None and part.part_id is not None
        assert relations == 7

    def test_a_snapshot_address_cannot_reach_the_live_provider(self, worlds):
        server = FakePlexServer()
        live = PlexLibrary(server=server)
        mark = len(server.queries)
        world = worlds[next(c for c, w in sorted(worlds.items()) if w.addressing.reissued())]
        for record in served_records(world.provider):
            for call in (live.get_item, live.get_files):
                with pytest.raises(LibraryItemNotFound):
                    call(record.item_id)
            with pytest.raises(LibraryItemNotFound):
                live.get_children(record.item_id, 0, 10)
        assert server.requests_since(mark) == []


def _ids_in(value: object, label: str) -> Iterator[ItemId]:
    """Every dataset id in a parsed truth case: as a string, a key, or an object."""
    if isinstance(value, Mapping):
        if set(value) == {"provider", "section_id", "rating_key"}:
            yield ItemId(**value)
            return
        for key, inner in value.items():
            yield from _ids_in(key, label)
            yield from _ids_in(inner, label)
    elif isinstance(value, list):
        for inner in value:
            yield from _ids_in(inner, label)
    elif isinstance(value, str) and value.startswith(f"{label}:") and value.count(":") == 2:
        yield ItemId.parse(value)


# -- confinement ------------------------------------------------------------------


def _reachable(root: object) -> list[object]:
    found, seen, stack = [], set(), [root]
    while stack:
        value = stack.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        found.append(value)
        if isinstance(value, Mapping):
            stack += [*value.keys(), *value.values()]
        elif isinstance(value, tuple | list | set | frozenset):
            stack += list(value)
        elif isinstance(value, BaseModel):
            stack += [getattr(value, name) for name in type(value).model_fields]
        elif dataclasses.is_dataclass(value) and not isinstance(value, type):
            stack += [getattr(value, f.name) for f in dataclasses.fields(value)]
        elif hasattr(value, "__dict__") and not isinstance(value, type):
            stack += list(vars(value).values())
    return found


class TestConfinement:
    def test_the_provider_holds_no_ground_truth(self, worlds, deltas, exported):
        """Everything reachable from the object the agent receives: the served
        records and nothing else. No changed item's clean record, no path, no delta,
        no addressing."""
        for case_id, world in worlds.items():
            reachable = _reachable(world.provider)
            assert not [
                v for v in reachable if isinstance(v, Path | ItemChange | Addressing | CaseWorld)
            ], case_id
            assert all(v.provider == PROVIDER for v in reachable if isinstance(v, ItemId))

            records = {canonical_json(dump_item(r)) for r in served_records(world.provider)}
            reachable_records = {
                canonical_json(dump_item(v)) for v in reachable if isinstance(v, BaseItem)
            }
            assert reachable_records == records, case_id

            changed = {c.item_id for c in deltas[case_id] if str(c.kind) != "add"}
            for record in exported.items:
                if str(record.item_id) in changed:
                    clean = canonical_json(dump_item(world.addressing.serve(record)))
                    assert clean not in reachable_records, (case_id, record.item_id)

    def test_the_provider_names_its_world(self, worlds):
        for world in worlds.values():
            info = world.provider.provider_info()
            assert (info.provider, info.server_id) == (PROVIDER, world.world_id)
            assert (info.server_version, info.platform) == (None, None)
            assert isinstance(world.provider, SnapshotLibrary)


# -- identity -----------------------------------------------------------------------


class TestWorldId:
    def test_world_id_is_shared_by_identical_worlds_and_only_by_them(self, worlds, exported):
        rendered = {
            case_id: render_world(
                served_records(world.provider), exported.sections, exported.manifest.profile
            )
            for case_id, world in worlds.items()
        }
        for a in worlds:
            for b in worlds:
                assert (worlds[a].world_id == worlds[b].world_id) == (rendered[a] == rendered[b])
        # Both outcomes occur, so the equivalence is not vacuous.
        assert 1 < len({world.world_id for world in worlds.values()}) < len(worlds)

    def test_the_sections_are_part_of_the_world(self, exported):
        """The same records under a renamed section are another library."""
        renamed = tuple(
            section.model_copy(update={"title": "Films"})
            if section.section_id == MOVIES
            else section
            for section in exported.sections
        )
        elsewhere = dataclasses.replace(exported, sections=renamed)
        assert elsewhere.world().world_id != exported.world().world_id

    def test_the_two_should_not_touch_cases_share_the_exports_own_world(
        self, worlds, deltas, exported
    ):
        empty = sorted(case_id for case_id, changes in deltas.items() if not changes)
        assert len(empty) == 2
        assert {worlds[c].world_id for c in empty} == {exported.world().world_id}

    def test_worlds_are_byte_identical_across_hash_seeds(self, tmp_path):
        """Practices §8.2: the builder takes sets of ids and sorts them, which a
        same-process test cannot check."""
        program = (
            "import sys, hashlib;"
            f"sys.path.insert(0, {TESTS_ROOT!r});"
            "from pathlib import Path;"
            "from evals.conftest import FakeLibrary;"
            "from shelfwarden.evals.export import run_export;"
            "from shelfwarden.evals.generate import run_generate;"
            "from shelfwarden.evals.world import WorldBuilder;"
            "out = Path(sys.argv[1]);"
            "export = run_export(FakeLibrary.build(), out / 'e', count=200).directory;"
            "run_generate(export, out / 'd', count=200, seed=1518,"
            f" composition_path=Path({str(REPO_ROOT / COMPOSITION_FILE)!r}),"
            f" curated_root=Path({str(REPO_ROOT / CURATED_ROOT)!r}));"
            "builder = WorldBuilder.open(export, out / 'd');"
            "worlds = [builder.world(c) for c in builder.case_ids()];"
            "[print(w.case_id, w.world_id, hashlib.sha256(repr(w.addressing.pairs())"
            ".encode()).hexdigest()) for w in worlds]"
        )
        outputs = []
        for index, seed in enumerate(("0", "1")):
            result = subprocess.run(
                [sys.executable, "-c", program, str(tmp_path / f"run{index}")],
                capture_output=True,
                text=True,
                env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": seed},
                check=False,
            )
            assert result.returncode == 0, result.stderr
            outputs.append(result.stdout)
        assert outputs[0] == outputs[1]
        assert len(outputs[0].splitlines()) == 25


# -- the round trip ---------------------------------------------------------------


class TestRoundTrip:
    def test_exporting_a_case_world_reproduces_it(self, worlds, deltas, exported, tmp_path):
        """The export's own walk over every fixture world, reissued keys and minted
        file ids included, gives back the world modulo addressing. The clean-world
        version, byte for byte against `items.jsonl`, is in the conformance suite."""
        for case_id, world in worlds.items():
            again = run_export(world.provider, tmp_path / case_id, count=None)
            assert again.manifest.provider.server_id == world.world_id
            assert render_family([world.addressing.unserve(r) for r in again.items]) == (
                render_family(apply_changes(exported.items, deltas[case_id]))
            ), case_id


# -- the report -----------------------------------------------------------------------


class TestReport:
    def test_the_fixture_reports_clean_with_its_quirk_counted(
        self, export_directory, dataset_directory, capsys
    ):
        assert main([str(export_directory), str(dataset_directory)]) == 0
        out = capsys.readouterr().out
        assert "structural violations: 0" in out
        assert "derived-copy disagreements, inherited by every world: 1" in out
        assert "fake:3:412/part_count" in out
        assert "25 built, 0 refused" in out

    def test_the_export_alone_can_be_reported(self, export_directory, capsys):
        assert main([str(export_directory)]) == 0
        out = capsys.readouterr().out
        assert "own world: world " in out
        assert "dataset" not in out

    def test_a_refused_binding_exits_nonzero_and_says_why(
        self, dataset_directory, tmp_path, capsys
    ):
        census = run_export(FakeLibrary.build(), tmp_path / "census", census_only=True)
        assert main([str(census.directory), str(dataset_directory)]) == 1
        assert "census-only" in capsys.readouterr().err

    def test_it_runs_as_a_module(self, export_directory, dataset_directory):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "shelfwarden.evals.world",
                str(export_directory),
                str(dataset_directory),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "25 built, 0 refused" in result.stdout
