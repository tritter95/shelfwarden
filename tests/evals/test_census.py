"""The census: what the library actually contains, and on what basis.

Two tiers with two different bases live in one file, and conflating them makes
the numbers unfalsifiable — a guid count taken from a 200-item slice, printed
without its coverage, reads as a statement about the library. Most of these tests
exist to keep that line drawn.

The other theme is ordering, which fails in two opposite directions here.
`Counter.most_common()` breaks ties by insertion order, which is hash-seed
dependent; and `canonical_json` sorts mapping keys, so a count-descending dict
comes back from `census.json` alphabetically. The first would make two
developers' exports differ; the second made `census.md` unreproducible from the
file it is supposed to render from. Both are asserted rather than assumed.
"""

import json

import pytest

from shelfwarden.canonical import canonical_json
from shelfwarden.evals import census as census_module
from shelfwarden.evals.census import (
    EXAMPLE_CAP,
    Census,
    ExportIndex,
    build,
    exported_census,
    population_census,
    readiness,
    render_markdown,
)
from shelfwarden.evals.export import run_export
from shelfwarden.models.ids import IdNamespace, parse_guids
from shelfwarden.models.item import FetchProfile, MediaKind, MovieItem

from .conftest import BOOKS, MOVIES, SECTIONS, SHOWS, FakeLibrary, _id, _library_records


@pytest.fixture
def census(library, tmp_path) -> Census:
    return run_export(library, tmp_path / "e", count=200).census


@pytest.fixture
def index() -> ExportIndex:
    return ExportIndex.build(_library_records())


def _movie(rating_key: str, **overrides) -> MovieItem:
    data: dict = {
        "item_id": _id(MOVIES, rating_key),
        "fetched": FetchProfile.CORE,
        "title": f"Movie {rating_key}",
    }
    data.update(overrides)
    return MovieItem(**data)


# -- the two tiers --------------------------------------------------------


class TestTiersAreLabelled:
    def test_the_population_tier_covers_every_supported_section(self, census):
        """Exact, from the listing walk — not sampled and reported as exact."""
        assert {row.section_id for row in census.population.sections} == {MOVIES, SHOWS, BOOKS}
        assert census.population.sections[0].population == 8

    def test_the_slice_tier_carries_its_own_coverage(self, census):
        """A guid count without an `of` is a slice number wearing a library label."""
        coverage = census.exported.coverage
        assert coverage.records == 20
        assert coverage.population == 11
        assert coverage.records != coverage.population

    def test_the_population_tier_counts_roots_and_the_slice_counts_records(self, census):
        """They answer different questions and are an order of magnitude apart on
        a TV library. Reporting one as the other is how a wrong composition.toml
        gets written."""
        assert census.population.by_media_kind == {"movie": 8, "show": 2, "author": 1}
        assert census.exported.by_media_kind["episode"] == 3
        assert "episode" not in census.population.by_media_kind

    def test_the_census_only_run_has_no_slice_tier_at_all(self, library, tmp_path):
        """Not a block of zeroes: that would read as "the library has no guids"."""
        result = run_export(library, tmp_path / "e", census_only=True)
        assert result.census.exported is None
        assert result.census.readiness == ()
        assert result.census.population.sections

    def test_agents_are_reported_because_legacy_sections_are_the_question(self, census):
        """`--section` is the operator's lever, and this is what tells them which
        section to point it at."""
        agents = census.population.by_agent
        assert agents["com.plexapp.agents.audnexus"].sections == 1
        assert agents["tv.plex.agents.movie"].population == 8


# -- guids: the point of the whole exercise -------------------------------


class TestGuidCensus:
    def test_unknown_namespaces_are_counted_with_their_raw_forms(self, census):
        """Step 0.2 wrote the legacy guid parsers against no legacy-agent library
        and said so. This is where the real forms get counted instead of guessed."""
        unknown = census.exported.guid_namespaces["unknown"]
        assert unknown.items == 1
        assert unknown.distinct_forms == 1
        assert unknown.examples == ("com.plexapp.agents.plexmovie://12345?lang=en",)

    def test_a_recognised_legacy_form_is_counted_under_its_namespace(self, census):
        """`com.plexapp.agents.imdb://tt0111161` parses; it is not the unknown case."""
        assert census.exported.guid_namespaces["imdb"].items == 1

    def test_examples_are_carried_only_where_they_are_informative(self, census):
        """A worked example of `tmdb://194` teaches nobody anything."""
        assert census.exported.guid_namespaces["tmdb"].examples == ()
        assert census.exported.guid_namespaces["unknown"].examples

    def test_items_and_ids_are_counted_separately(self):
        """One item can carry two guids in one namespace — a show with a per-season
        tvdb id does. Counting ids as items would overstate coverage."""
        item = _movie("1", guids=parse_guids(None, ["tvdb://9/1", "tvdb://9/2"]))
        namespace = exported_census((item,), population=1).guid_namespaces["tvdb"]
        assert (namespace.items, namespace.ids) == (1, 2)

    def test_items_with_no_guid_at_all_are_counted_and_named(self, census):
        missing = census.exported.items_without_guids
        assert missing.by_media_kind["movie"] == 1
        assert "fake:1:106" in missing.examples

    def test_the_headline_count_is_broken_down_by_kind(self, census):
        """It has to be. Seasons, episodes and audiobook parts legitimately carry
        no guid, so undifferentiated the number is dominated by structural
        absences and reads as a coverage gap that is not there."""
        missing = census.exported.items_without_guids
        assert missing.count == sum(missing.by_media_kind.values())
        assert set(missing.by_media_kind) > {"movie"}
        assert "items with no guid" in render_markdown(census)
        assert "legitimately carry no guid" in render_markdown(census)


class TestTheCapReportsWhatItDropped:
    """ "No silent caps" is the house rule a census violates by accident."""

    def _many_unknowns(self, count: int):
        return tuple(
            _movie(str(index), guids=parse_guids(f"com.plexapp.agents.bespoke://{index}"))
            for index in range(count)
        )

    def test_below_the_cap_nothing_is_reported_as_dropped(self):
        namespace = exported_census(self._many_unknowns(EXAMPLE_CAP), 1).guid_namespaces["unknown"]
        assert namespace.examples_truncated is False
        assert namespace.examples_dropped == 0
        assert len(namespace.examples) == EXAMPLE_CAP

    def test_above_the_cap_the_overflow_is_counted(self):
        namespace = exported_census(self._many_unknowns(EXAMPLE_CAP + 7), 1).guid_namespaces[
            "unknown"
        ]
        assert namespace.examples_truncated is True
        assert namespace.examples_dropped == 7
        assert len(namespace.examples) == EXAMPLE_CAP

    def test_the_full_distinct_count_survives_the_cap(self):
        """Capping the *examples* must not cap the *count* — otherwise the cap
        silently rewrites the finding it is truncating."""
        namespace = exported_census(self._many_unknowns(40), 1).guid_namespaces["unknown"]
        assert namespace.distinct_forms == 40

    def test_which_examples_survive_is_deterministic(self):
        """Sorted before capping. Otherwise the surviving five are a function of
        set iteration order and the export is no longer byte-identical."""
        forward = exported_census(self._many_unknowns(20), 1).guid_namespaces["unknown"]
        backward = exported_census(self._many_unknowns(20)[::-1], 1).guid_namespaces["unknown"]
        assert forward.examples == backward.examples

    def test_the_no_guid_example_list_reports_its_cap_too(self):
        items = tuple(_movie(str(index), guids=()) for index in range(EXAMPLE_CAP + 3))
        missing = exported_census(items, 1).items_without_guids
        assert missing.count == EXAMPLE_CAP + 3
        assert missing.examples_dropped == 3
        assert missing.examples_truncated is True

    def test_the_markdown_says_out_loud_that_it_truncated(self):
        census = build(
            sections=SECTIONS[:1],
            populations={MOVIES: 20},
            root_kinds={MOVIES: MediaKind.MOVIE},
            items=self._many_unknowns(20),
        )
        assert "+15 more" in render_markdown(census)


# -- ordering -------------------------------------------------------------


class TestOrdering:
    """Finding 6. A same-process "run it twice" test cannot see any of this."""

    def test_counts_sort_by_count_descending_then_key_ascending(self):
        items = (
            _movie("1", guids=parse_guids(None, ["tmdb://1"])),
            _movie("2", guids=parse_guids(None, ["tmdb://2"])),
            _movie("3", guids=parse_guids(None, ["imdb://tt3"])),
            _movie("4", guids=parse_guids(None, ["tvdb://4"])),
        )
        namespaces = exported_census(items, 4).guid_namespaces
        # tmdb has two; imdb and tvdb tie at one and break alphabetically.
        assert list(namespaces) == ["imdb", "tmdb", "tvdb"]

    def test_a_tie_does_not_break_on_insertion_order(self):
        """`Counter.most_common()` would; that is exactly why it is not used."""
        forward = exported_census(
            (
                _movie("1", parts=()),
                _movie("2", guids=parse_guids(None, ["imdb://tt1"])),
                _movie("3", guids=parse_guids(None, ["tvdb://1"])),
            ),
            3,
        )
        backward = exported_census(
            (
                _movie("3", guids=parse_guids(None, ["tvdb://1"])),
                _movie("2", guids=parse_guids(None, ["imdb://tt1"])),
                _movie("1", parts=()),
            ),
            3,
        )
        assert list(forward.guid_namespaces) == list(backward.guid_namespaces)

    def test_sections_sort_numerically_not_lexicographically(self):
        sections = tuple(reversed(SECTIONS[:3]))
        rows = population_census(
            sections,
            populations={MOVIES: 1, SHOWS: 1, BOOKS: 1},
            root_kinds={
                MOVIES: MediaKind.MOVIE,
                SHOWS: MediaKind.SHOW,
                BOOKS: MediaKind.AUTHOR,
            },
        ).sections
        assert [row.section_id for row in rows] == [MOVIES, SHOWS, BOOKS]

    def test_the_readiness_table_keeps_a_fixed_declared_order(self, index):
        """So two censuses diff line by line rather than by set membership."""
        assert [row.problem_class for row in readiness(index)] == [
            name for name, _ in census_module.READINESS_RULES
        ]

    def test_the_serialized_census_is_byte_stable(self, library, tmp_path):
        first = run_export(library, tmp_path / "a", count=200).census
        second = run_export(FakeLibrary.build(), tmp_path / "b", count=200).census
        assert canonical_json(first.model_dump(mode="json")) == canonical_json(
            second.model_dump(mode="json")
        )


# -- readiness ------------------------------------------------------------


class TestReadinessIsAdvisory:
    def test_every_row_is_flagged_advisory(self, census):
        """It counts structural *candidates*. It does not verify that any item is
        free of a problem — that is the mechanical screen in 0.45. A readiness
        count read as a `no_action` label would make the should-not-touch slice
        unfalsifiable, which is Defect 3 in implementation-plan.md §3."""
        assert census.readiness
        assert all(row.advisory for row in census.readiness)

    def test_every_row_states_its_basis(self, census):
        assert all(row.basis.strip() for row in census.readiness)

    def test_a_row_exists_for_every_problem_class(self, census):
        """So a class cannot quietly go uncounted when composition.toml is written."""
        assert len(census.readiness) == len(census_module.READINESS_RULES)

    def test_the_markdown_says_it_is_not_a_no_action_label(self, census):
        rendered = render_markdown(census)
        assert "advisory" in rendered.lower()
        assert "unfalsifiable" in rendered

    def test_a_remake_pair_is_spotted(self, index):
        """Two Solaris, 1972 and 2002."""
        counts = {row.problem_class: row.eligible for row in readiness(index)}
        assert counts["year_collision_remake"] == 1

    def test_two_versions_of_one_film_are_spotted(self, index):
        """Two Blade Runner (1982) — the duplicate_quality candidate."""
        counts = {row.problem_class: row.eligible for row in readiness(index)}
        assert counts["duplicate_quality"] == 1

    def test_a_show_needs_more_than_one_season_to_be_eligible(self, index):
        """`episode_wrong_season` has nowhere to move an episode to otherwise."""
        counts = {row.problem_class: row.eligible for row in readiness(index)}
        assert counts["episode_wrong_season"] == 1  # Cowboy Bebop, not Pilot Only

    def test_a_class_with_no_structural_test_reports_zero_rather_than_being_absent(self, index):
        """`anthology_omnibus` is not detectable from an export. Omitting the row
        would read as "not yet counted"; zero reads as "curate this by hand"."""
        counts = {row.problem_class: row.eligible for row in readiness(index)}
        assert counts["anthology_omnibus"] == 0

    def test_the_audiobook_classes_are_counted(self, index):
        """The implementation plan's open question: can one audiobook slice carry
        six problem classes? This is the first honest answer."""
        counts = {row.problem_class: row.eligible for row in readiness(index)}
        assert counts["author_name_variant"] == 1
        assert counts["multi_file_split"] == 1
        assert counts["series_order_broken"] == 2

    def test_an_unresolvable_item_is_not_a_wrong_match_candidate(self):
        """You cannot swap an id that is not there, and `plex://`/`local://` are
        not external ids."""
        items = (
            _movie("1", guids=()),
            _movie("2", guids=parse_guids("plex://movie/2")),
            _movie("3", guids=parse_guids("plex://movie/3", ["tmdb://3"])),
        )
        counts = {row.problem_class: row.eligible for row in readiness(ExportIndex.build(items))}
        assert counts["wrong_match"] == 1


# -- the markdown ---------------------------------------------------------


class TestMarkdown:
    def test_it_renders_from_a_stored_census_alone(self, census, tmp_path):
        """Not from the export that produced it — a census.json in a dataset
        directory has to stay readable years later."""
        path = tmp_path / "census.json"
        path.write_bytes(canonical_json(census.model_dump(mode="json")))
        reloaded = Census.model_validate(json.loads(path.read_bytes()))
        assert render_markdown(reloaded) == render_markdown(census)

    def test_it_names_both_bases(self, census):
        rendered = render_markdown(census)
        assert "population — exact" in rendered
        assert "scoped to the slice, not to the library" in rendered

    def test_the_census_only_render_says_why_the_slice_tier_is_missing(self, library, tmp_path):
        census = run_export(library, tmp_path / "e", census_only=True).census
        rendered = render_markdown(census)
        assert "Population tier only" in rendered
        assert "### Guid namespaces" not in rendered
        assert "Slice readiness" not in rendered

    def test_the_numbers_needed_for_composition_toml_are_all_present(self, census):
        """It is a deliverable, not a nicety: this is the table composition.toml
        is written from."""
        rendered = render_markdown(census)
        for expected in ("Movies", "unknown", "mkv", "m4b", "duplicate_quality", "author"):
            assert expected in rendered, expected

    def test_an_empty_table_renders_as_none_rather_than_as_a_headerless_gap(self):
        census = build(
            sections=(),
            populations={},
            root_kinds={},
            items=(),
        )
        assert "_(none)_" in render_markdown(census)

    def test_it_ends_with_a_newline(self):
        """So a census.md concatenated or diffed does not lose its last row."""
        census = build(
            sections=SECTIONS[:1],
            populations={MOVIES: 0},
            root_kinds={MOVIES: MediaKind.MOVIE},
            items=None,
        )
        assert render_markdown(census).endswith("\n")


# -- containers, locks, presence ------------------------------------------


class TestSliceDetail:
    def test_containers_are_counted_per_part_and_case_folded(self, census):
        """`MKV` and `mkv` are one container; two rows would be noise."""
        assert census.exported.containers["mkv"] == 11
        assert census.exported.containers["m4b"] == 2

    def test_resolutions_are_counted(self, census):
        assert census.exported.video_resolutions == {"1080": 7, "720": 3, "4k": 1}

    def test_lock_state_is_counted_because_phase_3_has_to_restore_it(self, census):
        assert census.exported.locked_fields == {"title": 2}

    def test_field_presence_reports_absent_as_well_as_present(self, census):
        """Absence is the fact `missing_metadata` is built on, so it is counted
        rather than inferred from a total."""
        summary = census.exported.field_presence["summary"]
        assert summary.present + summary.absent == census.exported.coverage.records
        assert summary.absent > 0

    def test_an_item_carrying_no_parts_contributes_no_containers(self):
        assert exported_census((_movie("1", parts=()),), 1).containers == {}


def test_namespace_names_come_from_the_enum_not_from_free_text():
    """A typo'd namespace key would silently create a second bucket."""
    census = exported_census((_movie("1", guids=parse_guids("com.plexapp.agents.bespoke://x")),), 1)
    assert set(census.guid_namespaces) <= {str(member) for member in IdNamespace}
