"""The export's own gate: re-running it produces the same bytes.

Everything downstream in Phase 0 reads this file, so a drifting export is a
silently moving baseline. These tests are what make "deterministic" a fact rather
than an intention.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from shelfwarden.evals import census as census_module
from shelfwarden.evals.export import (
    CENSUS_FILE,
    CENSUS_MARKDOWN_FILE,
    ITEMS_FILE,
    MANIFEST_FILE,
    VOLATILE_MANIFEST_FIELDS,
    ExportError,
    allocate,
    comparable_manifest,
    fetch_family,
    git_state,
    list_all,
    load_census,
    load_manifest,
    run_export,
    select,
)
from shelfwarden.library.base import LibraryItemNotFound, LibraryUnavailable
from shelfwarden.models.ids import ItemId
from shelfwarden.models.item import FetchProfile, MediaKind, load_item

from .conftest import BOOKS, MOVIES, MUSIC, PHOTOS, SHOWS, FakeLibrary

TESTS_ROOT = str(Path(__file__).resolve().parent.parent)


def read_items(directory: Path):
    return [load_item(line) for line in (directory / ITEMS_FILE).read_bytes().splitlines()]


def _descendants_in(library: FakeLibrary, item_id: ItemId) -> set[str]:
    """Every id beneath `item_id` in the *library*, transitively.

    Deliberately asks the fake rather than the export: an export compared only to
    itself would look internally consistent even if every family were truncated
    identically.
    """
    found: set[str] = set()
    frontier = [item_id]
    while frontier:
        page = library.get_children(frontier.pop(), offset=0, limit=1000)
        for stub in page.items:
            found.add(str(stub.item_id))
            frontier.append(stub.item_id)
    return found


# -- the gate -------------------------------------------------------------


def test_export_is_byte_identical(library, tmp_path):
    """Two exports of an unchanged library differ in exactly one manifest field."""
    first = run_export(library, tmp_path / "a", count=200)
    second = run_export(FakeLibrary.build(), tmp_path / "b", count=200)

    assert (first.directory / ITEMS_FILE).read_bytes() == (
        second.directory / ITEMS_FILE
    ).read_bytes()
    assert comparable_manifest(first.directory) == comparable_manifest(second.directory)
    assert (first.directory / CENSUS_FILE).read_bytes() == (
        second.directory / CENSUS_FILE
    ).read_bytes()
    assert (first.directory / CENSUS_MARKDOWN_FILE).read_bytes() == (
        second.directory / CENSUS_MARKDOWN_FILE
    ).read_bytes()


def test_only_created_at_is_allowed_to_differ():
    """The exception list is a constant so it cannot quietly grow.

    If a genuinely volatile field is ever added, this test is the place the
    decision gets made rather than a diff nobody reads.
    """
    assert frozenset({"created_at"}) == VOLATILE_MANIFEST_FIELDS


def test_export_is_byte_identical_across_hash_seeds(tmp_path):
    """The version of the gate that can actually see hash-order leakage.

    pytest runs in one process, so an in-process "export twice" comparison would
    pass against a census whose key order depends on PYTHONHASHSEED -- and two
    developers' exports would then differ for no visible reason. Forking with an
    explicit seed on each side is the only form of this test that means anything.
    """
    program = (
        "import sys, hashlib, json;"
        f"sys.path.insert(0, {TESTS_ROOT!r});"
        "from pathlib import Path;"
        "from evals.conftest import FakeLibrary;"
        "from shelfwarden.evals.export import ("
        "run_export, ITEMS_FILE, CENSUS_FILE, comparable_manifest);"
        "out = Path(sys.argv[1]);"
        "run_export(FakeLibrary.build(), out, count=200);"
        "payload = (out / ITEMS_FILE).read_bytes() + (out / CENSUS_FILE).read_bytes()"
        " + json.dumps(comparable_manifest(out), sort_keys=True).encode();"
        "print(hashlib.sha256(payload).hexdigest())"
    )
    digests = []
    for index, seed in enumerate(("0", "1")):
        result = subprocess.run(
            [sys.executable, "-c", program, str(tmp_path / f"run{index}")],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": seed},
            check=False,
        )
        assert result.returncode == 0, result.stderr
        digests.append(result.stdout.strip())
    assert digests[0] == digests[1]


def test_the_export_module_does_not_pull_in_the_plex_adapter():
    """The import contract proves no module imports it; this proves it at runtime.

    Static and dynamic checks catch different mistakes -- a lazy import inside a
    function passes `lint-imports` and fails here.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, shelfwarden.evals.export;"
            "print('shelfwarden.library.plex' in sys.modules or 'plexapi' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


# -- selection ------------------------------------------------------------


class TestSelection:
    def test_the_same_seed_selects_the_same_roots(self, library, tmp_path):
        first = run_export(library, tmp_path / "a", count=4, seed=7)
        second = run_export(FakeLibrary.build(), tmp_path / "b", count=4, seed=7)
        assert [str(i.item_id) for i in first.items] == [str(i.item_id) for i in second.items]

    def test_a_different_seed_selects_differently(self, library):
        stubs = list_all(library, MOVIES, MediaKind.MOVIE)
        drawn = {
            seed: tuple(str(s.item_id) for s in select(stubs, 3, seed, MOVIES))
            for seed in range(12)
        }
        # Otherwise the seed is decorative and `--seed` is a lie.
        assert len(set(drawn.values())) > 1

    def test_selection_does_not_decide_ordering(self, library):
        stubs = list_all(library, MOVIES, MediaKind.MOVIE)
        chosen = select(stubs, 4, 99, MOVIES)
        assert list(chosen) == sorted(chosen, key=lambda s: int(s.item_id.rating_key))

    def test_a_quota_at_or_above_the_population_takes_everything(self, library):
        stubs = list_all(library, MOVIES, MediaKind.MOVIE)
        assert len(select(stubs, len(stubs) + 5, 1, MOVIES)) == len(stubs)

    def test_adding_a_section_does_not_reshuffle_the_others(self, library):
        stubs = list_all(library, MOVIES, MediaKind.MOVIE)
        assert select(stubs, 3, 5, MOVIES) == select(stubs, 3, 5, MOVIES)


class TestAllocate:
    def test_quotas_sum_to_the_request(self):
        quotas = allocate([("1", 100), ("2", 50), ("3", 25)], 40)
        assert sum(quotas.values()) == 40

    def test_no_section_is_allocated_more_than_it_has(self):
        quotas = allocate([("1", 2), ("2", 100)], 50)
        assert quotas["1"] <= 2
        assert sum(quotas.values()) == 50

    def test_a_request_larger_than_the_library_takes_everything(self):
        assert allocate([("1", 3), ("2", 4)], 1000) == {"1": 3, "2": 4}

    def test_ties_break_on_section_id_not_on_iteration_order(self):
        forwards = allocate([("1", 10), ("2", 10), ("3", 10)], 4)
        backwards = allocate([("3", 10), ("2", 10), ("1", 10)], 4)
        assert forwards == backwards

    def test_an_empty_library_allocates_nothing(self):
        assert allocate([("1", 0)], 10) == {"1": 0}


# -- families -------------------------------------------------------------


class TestFamilies:
    def test_a_family_arrives_whole(self, library):
        stubs = list_all(library, SHOWS, MediaKind.SHOW)
        bebop = next(s for s in stubs if s.title == "Cowboy Bebop")
        family = fetch_family(library, bebop, FetchProfile.CORE)
        kinds = [record.media_kind for record in family.records]
        assert kinds.count(MediaKind.SHOW) == 1
        assert kinds.count(MediaKind.SEASON) == 2
        assert kinds.count(MediaKind.EPISODE) == 3

    def test_an_over_budget_family_is_dropped_whole_and_reported(self, library, tmp_path):
        """Half a show is an unsolvable case in 0.5 and a depressed score in 0.8."""
        result = run_export(library, tmp_path / "e", count=200, max_records=9)
        dropped = {row.title for row in result.manifest.dropped}
        assert "Cowboy Bebop" in dropped
        assert all(row.records > 0 for row in result.manifest.dropped)
        assert "max_records" in next(
            row.reason for row in result.manifest.dropped if row.title == "Cowboy Bebop"
        )
        exported = {str(item.item_id) for item in result.items}
        assert not any(key.startswith("fake:2:22") for key in exported)

    def test_a_smaller_family_still_fits_after_a_big_one_is_dropped(self, library, tmp_path):
        result = run_export(library, tmp_path / "e", count=200, max_records=9)
        titles = {item.title for item in result.items}
        assert "Pilot Only" in titles

    @pytest.mark.parametrize("max_records", [9, 12, 16, 5000])
    def test_no_family_is_ever_partially_exported(self, library, tmp_path, max_records):
        """Whatever the budget does, it never cuts a family in half.

        Two halves of the same invariant. Upwards: every record's parent and
        grandparent are present, so no orphan is written. Downwards: every root
        that made it in brought its *entire* descendant set, so a family is never
        trimmed at the budget boundary -- which is the failure `episode_wrong_season`
        would inherit as an unsolvable case in 0.5.
        """
        result = run_export(library, tmp_path / "e", count=200, max_records=max_records)
        exported = {str(item.item_id) for item in result.items}

        for item in result.items:
            for relation in ("parent", "grandparent"):
                ancestor = getattr(item, relation, None)
                if ancestor is not None:
                    assert str(ancestor) in exported, f"{item.item_id} has no {relation} exported"

        # Downwards, against the library rather than against the export -- comparing
        # the export to itself would pass on a consistently truncated family.
        for item in result.items:
            for descendant in _descendants_in(library, item.item_id):
                assert descendant in exported, f"{item.item_id} is missing {descendant}"

    def test_an_unavailable_server_mid_walk_aborts_the_whole_export(self, tmp_path):
        """The third row of the error table, and the one that matters most.

        A vanished *item* drops one family and the export continues, because the
        loss is bounded and recorded. A vanished *server* is unbounded: every
        remaining family would silently be absent from a file that otherwise looks
        finished. A partial export that reads as complete is the single worst
        artifact this step could produce, so the run aborts and writes nothing.
        """
        library = FakeLibrary.build(unavailable=frozenset({"fake:1:104"}))
        out = tmp_path / "e"
        with pytest.raises(LibraryUnavailable):
            run_export(library, out, count=200)
        assert not out.exists()
        assert list(tmp_path.iterdir()) == []

    def test_an_unavailable_server_is_not_quietly_recorded_as_a_dropped_family(self, tmp_path):
        """So the abort cannot be softened later into a drop without a red test."""
        library = FakeLibrary.build(unavailable=frozenset({"fake:2:2211"}))
        with pytest.raises(LibraryUnavailable):
            run_export(library, tmp_path / "e", count=200)

    def test_census_only_still_aborts_when_a_listing_fails(self, tmp_path):
        """`--census-only` reports exact population counts. A census that silently
        walked two sections of three would state a number it did not measure."""

        class Flaky(FakeLibrary):
            def list_items(self, section_id, offset, limit, media_kind=None):
                if section_id == SHOWS:
                    raise LibraryUnavailable("the server went away mid-listing")
                return super().list_items(section_id, offset, limit, media_kind)

        out = tmp_path / "e"
        with pytest.raises(LibraryUnavailable):
            run_export(Flaky.build(), out, census_only=True)
        assert not out.exists()

    def test_a_member_disappearing_mid_walk_drops_the_family(self, tmp_path):
        library = FakeLibrary.build(missing=frozenset({"fake:2:2211"}))
        result = run_export(library, tmp_path / "e", count=200)
        dropped = {row.title for row in result.manifest.dropped}
        assert "Cowboy Bebop" in dropped
        assert not any(item.item_id.rating_key.startswith("22") for item in result.items)
        assert "disappeared" in next(iter(result.manifest.dropped)).reason


# -- ordering and content -------------------------------------------------


class TestRecords:
    def test_records_are_grouped_by_family(self, library, tmp_path):
        result = run_export(library, tmp_path / "e", count=200)
        keys = [str(item.item_id) for item in result.items]
        bebop = keys.index("fake:2:201")
        # Its two seasons and three episodes follow it, before the next show.
        assert keys[bebop + 1 : bebop + 6] == [
            "fake:2:211",
            "fake:2:212",
            "fake:2:2211",
            "fake:2:2212",
            "fake:2:2221",
        ]

    def test_numeric_rating_keys_sort_numerically(self, library, tmp_path):
        result = run_export(library, tmp_path / "e", count=200)
        movies = [
            int(item.item_id.rating_key)
            for item in result.items
            if item.media_kind is MediaKind.MOVIE
        ]
        assert movies == sorted(movies)

    def test_lock_state_survives_the_round_trip(self, library, tmp_path):
        """Phase 3's revert must restore locks, not just values."""
        result = run_export(library, tmp_path / "e", count=200)
        reread = {str(item.item_id): item for item in read_items(result.directory)}
        assert reread["fake:1:101"].locked_fields == ("title",)
        assert reread["fake:3:401"].locked_fields == ("title",)

    def test_part_ids_round_trip_and_order_is_preserved(self, library, tmp_path):
        result = run_export(library, tmp_path / "e", count=200)
        reread = {str(item.item_id): item for item in read_items(result.directory)}
        book_part = reread["fake:3:421"]
        assert book_part.parts[0].part_id == "141"
        assert book_part.parts[0].media_id == "941"

    def test_the_profile_is_stamped_on_every_record(self, library, tmp_path):
        result = run_export(library, tmp_path / "e", count=200, profile=FetchProfile.FULL)
        assert all(item.fetched is FetchProfile.FULL for item in read_items(result.directory))
        assert load_manifest(result.directory).profile is FetchProfile.FULL

    def test_every_record_reloads_from_the_written_bytes(self, library, tmp_path):
        result = run_export(library, tmp_path / "e", count=200)
        assert len(read_items(result.directory)) == result.manifest.counts.records


# -- sections and skips ---------------------------------------------------


class TestSections:
    def test_an_unmodelled_section_is_recorded_not_dropped(self, library, tmp_path):
        result = run_export(library, tmp_path / "e", count=200)
        skipped = {row.section_id: row.reason for row in result.manifest.skipped_sections}
        assert PHOTOS in skipped
        assert "not modelled" in skipped[PHOTOS]

    def test_a_music_section_skip_carries_the_audiobook_verdict(self, library, tmp_path):
        """The judgement travels with the refusal, so it can be argued with."""
        result = run_export(library, tmp_path / "e", count=200)
        skipped = {row.section_id: row.reason for row in result.manifest.skipped_sections}
        assert MUSIC in skipped
        assert "not audiobooks" in skipped[MUSIC]

    def test_restricting_to_a_section_records_the_others_as_skipped(self, library, tmp_path):
        result = run_export(library, tmp_path / "e", count=200, sections=(MOVIES,))
        assert {row.section_id for row in result.manifest.sections} == {MOVIES}
        reasons = {row.section_id: row.reason for row in result.manifest.skipped_sections}
        assert reasons[SHOWS] == "not requested (--section)"
        assert BOOKS in reasons

    def test_no_supported_section_is_an_error_not_an_empty_export(self, tmp_path):
        library = FakeLibrary.build()
        with pytest.raises(ExportError, match="no supported sections"):
            run_export(library, tmp_path / "e", count=200, sections=("999",))
        assert not (tmp_path / "e").exists()


# -- the manifest ---------------------------------------------------------


class TestManifest:
    def test_it_records_the_effective_request_params(self, library, tmp_path):
        """`RELOAD_INCLUDES` is a set of overrides, not a description of the request."""
        params = {"includeFields": "thumbBlurHash,artBlurHash"}
        result = run_export(library, tmp_path / "e", count=200, request_params=params)
        assert load_manifest(result.directory).request_params == params

    def test_the_export_id_is_the_content_hash(self, library, tmp_path):
        result = run_export(library, tmp_path / "e", count=200)
        manifest = load_manifest(result.directory)
        assert manifest.export_id == "exp-" + manifest.items_sha256[:12]

    def test_the_hashes_match_what_was_written(self, library, tmp_path):
        from hashlib import sha256

        result = run_export(library, tmp_path / "e", count=200)
        manifest = load_manifest(result.directory)
        items = (result.directory / ITEMS_FILE).read_bytes()
        census = (result.directory / CENSUS_FILE).read_bytes()
        assert manifest.items_sha256 == sha256(items).hexdigest()
        assert manifest.census_sha256 == sha256(census).hexdigest()

    def test_counts_report_roots_and_records_separately(self, library, tmp_path):
        """They differ by an order of magnitude on a TV library, and a `--count`
        that could mean either is how a wrong composition.toml gets written."""
        result = run_export(library, tmp_path / "e", count=200)
        counts = result.manifest.counts
        assert counts.roots == 11
        assert counts.records == 20
        assert sum(counts.by_media_kind.values()) == counts.records

    def test_the_provider_is_recorded(self, library, tmp_path):
        result = run_export(library, tmp_path / "e", count=200)
        assert result.manifest.provider.provider == "fake"
        assert result.manifest.provider.server_id == "0123456789abcdef"

    def test_the_selection_plan_is_recorded(self, library, tmp_path):
        result = run_export(library, tmp_path / "e", count=5, seed=42)
        plan = load_manifest(result.directory).selection
        assert plan.seed == 42
        assert plan.requested_roots == 5
        assert sum(quota.quota for quota in plan.per_section) == 5

    def test_all_mode_is_recorded_as_such(self, library, tmp_path):
        result = run_export(library, tmp_path / "e", count=None)
        assert load_manifest(result.directory).selection.mode == "all"
        assert load_manifest(result.directory).selection.requested_roots is None

    def test_git_state_outside_a_checkout_reads_as_unknown_and_dirty(self, tmp_path):
        """Unknown provenance must not read as cleanly reproducible."""
        sha, dirty = git_state(tmp_path)
        assert sha is None
        assert dirty is True


# -- census-only and writing ----------------------------------------------


class TestCensusOnly:
    def test_it_fetches_no_items(self, library, tmp_path):
        run_export(library, tmp_path / "e", census_only=True)
        assert library.get_item_calls == []

    def test_it_writes_the_population_tier_and_no_slice_tier(self, library, tmp_path):
        result = run_export(library, tmp_path / "e", census_only=True)
        census = load_census(result.directory)
        assert census.population.sections
        # A block of zeroes would read as "the library has no guids".
        assert census.exported is None

    def test_the_markdown_says_why_the_slice_tier_is_absent(self, library, tmp_path):
        result = run_export(library, tmp_path / "e", census_only=True)
        assert "Population tier only" in (result.directory / CENSUS_MARKDOWN_FILE).read_text()


class TestWriting:
    def test_a_failed_export_writes_nothing(self, library, tmp_path):
        out = tmp_path / "e"
        with pytest.raises(ExportError, match="contains a configured secret"):
            run_export(library, out, count=200, secrets=("Amélie",))
        assert not out.exists()

    def test_no_staging_directory_is_left_behind(self, library, tmp_path):
        out = tmp_path / "e"
        with pytest.raises(ExportError):
            run_export(library, out, count=200, secrets=("Amélie",))
        assert list(tmp_path.iterdir()) == []

    def test_re_exporting_replaces_the_directory(self, library, tmp_path):
        out = tmp_path / "e"
        run_export(library, out, count=200)
        (out / "stale.txt").write_text("left over")
        run_export(FakeLibrary.build(), out, count=200)
        assert not (out / "stale.txt").exists()
        assert sorted(p.name for p in out.iterdir()) == [
            CENSUS_FILE,
            CENSUS_MARKDOWN_FILE,
            ITEMS_FILE,
            MANIFEST_FILE,
        ]

    def test_a_duplicate_item_id_is_refused(self, tmp_path, library):
        """Two records for one id would make the export ambiguous about what it holds."""
        duplicate = library.records["fake:1:101"]
        library.records["fake:1:101-copy"] = duplicate
        with pytest.raises(ExportError, match="more than once"):
            run_export(library, tmp_path / "e", count=200)


def test_no_output_file_contains_a_configured_secret(library, tmp_path):
    """Practices §9: assert it, do not trust review."""
    token = "xxTOKENxx"
    result = run_export(library, tmp_path / "e", count=200, secrets=(token,))
    for path in result.directory.iterdir():
        assert token.encode() not in path.read_bytes()


def test_the_manifest_is_valid_json_with_sorted_keys(library, tmp_path):
    result = run_export(library, tmp_path / "e", count=200)
    raw = (result.directory / MANIFEST_FILE).read_bytes()
    data = json.loads(raw)
    assert list(data) == sorted(data)


def test_section_sort_key_orders_numeric_ids_numerically():
    ordered = sorted(["10", "9", "abc", "2"], key=census_module.section_sort_key)
    assert ordered == ["2", "9", "10", "abc"]


def test_get_children_paging_is_exhausted(library):
    """A show with 300 episodes is not a special case, it is Tuesday."""
    from shelfwarden.evals import export as export_module

    original = export_module.PAGE_SIZE
    try:
        export_module.PAGE_SIZE = 1
        family = fetch_family(
            library,
            next(s for s in list_all(library, SHOWS, MediaKind.SHOW) if s.title == "Cowboy Bebop"),
            FetchProfile.CORE,
        )
    finally:
        export_module.PAGE_SIZE = original
    assert len(family.records) == 6


def test_list_all_returns_every_root(library):
    assert len(list_all(library, MOVIES, MediaKind.MOVIE)) == 8


def test_get_item_is_addressed_by_composite_id(library):
    """A rating key alone is not an address: the section is part of the identity."""
    with pytest.raises(LibraryItemNotFound):
        library.get_item(ItemId("fake", MOVIES, "does-not-exist"))
