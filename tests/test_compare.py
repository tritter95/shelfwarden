"""The comparators, and the three defaults that would have made them lie.

Every trap tested here was verified by running it, not recalled: argument-order
asymmetry in `difflib`, `autojunk` destroying long-text comparison, and
`casefold()` not preserving NFC. Each is a *silent* failure -- the wrong number
comes back looking like a right one -- so each gets a test that fails loudly if
the guard is ever removed.
"""

import subprocess
import sys
import unicodedata

import pytest

from shelfwarden.compare import (
    FOLD_LADDER,
    RELEASE_TAGS,
    RESOLVABLE_NAMESPACES,
    SCREEN_POLICY,
    STRENGTH_RANK,
    Policy,
    Support,
    SupportStrength,
    at_least,
    compare_person_name,
    compare_series_position,
    compare_text_block,
    compare_title,
    compare_year,
    find_in_path,
    fold_text,
    has_resolvable_id,
    id_overlap,
    is_structural_segment,
    ladder_rule,
    name_tokens,
    normalize_position,
    parse_release_name,
    parse_release_path,
    path_segments,
    ratio,
    strip_articles,
    strip_diacritics,
    strip_punctuation,
)
from shelfwarden.models.ids import IdNamespace, parse_guid, parse_guids


def nfd(value: str) -> str:
    return unicodedata.normalize("NFD", value)


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


# -- the three traps ------------------------------------------------------


class TestArgumentOrderIsPartOfTheContract:
    """Finding 1. `SequenceMatcher.ratio()` is not symmetric."""

    def test_fuzzy_ratio_argument_order_is_pinned(self):
        """A brute force over a 3-letter alphabet found 9228 asymmetric pairs at
        lengths 1-5. This is the smallest of them, and it is a factor of two.

        If a refactor ever swaps the parameters, this fails rather than shifting
        every score in the project by an unnoticed amount -- which would surface
        as a validator false-rejection rate that will not reconcile with the
        screen's guard coverage.
        """
        assert ratio("ab", "bacb") == 0.6667
        assert ratio("bacb", "ab") == 0.3333

    def test_every_comparator_takes_observed_first(self):
        """Spot-checked on the one that carries a direction in its result."""
        forward, delta = compare_year(2001, 1999)
        backward, reverse = compare_year(1999, 2001)
        assert (delta, reverse) == (2, -2)
        assert forward.strength is backward.strength


class TestAutojunk:
    """Finding 2. The default silently returns 0.0033 where 0.5 is the answer."""

    def test_long_text_comparison_passes_autojunk_false(self):
        observed = "a" * 300
        authority = "ab" * 150
        assert ratio(observed, authority) == 0.5

        from difflib import SequenceMatcher

        defaulted = SequenceMatcher(None, observed, authority).ratio()
        assert defaulted < 0.01, "the default is what this test exists to reject"

    def test_it_bites_summaries_specifically(self):
        """The heuristic switches on at 200 characters of the *second* string, so
        the failure would arrive partway through a dataset as summaries get
        longer -- the worst possible shape for a bug."""
        observed = "A shy waitress decides to change the lives of those around her. " * 4
        authority = observed.replace("shy", "quiet")
        support = compare_text_block(observed, authority)
        assert support.strength is SupportStrength.FUZZY
        assert support.score is not None and support.score > 0.9


class TestNfcAfterCasefold:
    """Finding 3. `FilePart.path` is the one un-normalized string in the model."""

    def test_an_nfd_path_matches_an_nfc_title(self):
        parsed = parse_release_name(nfd("/media/Movies/Amélie (2001)/Amélie (2001).mkv"))
        assert parsed.title != nfc("Amélie"), "the fixture must actually be decomposed"
        support = compare_title(parsed.title, nfc("Amélie"))
        assert support.strength is SupportStrength.NORMALIZED
        assert support.rule == "fold"

    @pytest.mark.parametrize("value", ["Å", "Amélie", "Ç", "ñ"])
    def test_folding_normalizes_after_casefolding_not_before(self, value):
        """`nfc(x).casefold()` is not NFC. This is the whole reason for the order."""
        assert fold_text(nfd(value)) == fold_text(nfc(value))

    def test_nfkc_folds_compatibility_forms_that_canonical_text_must_not(self):
        """NFKC belongs in a comparison fold and never in `canonical_text`."""
        assert fold_text("Ⅻ") == fold_text("xii")
        assert fold_text("ﬁlm") == fold_text("film")

    def test_the_ligature_ceiling_is_recorded_rather_than_papered_over(self):
        """`Æ` and `œ` survive NFKD. A hand-maintained table would rot, so this
        lands at FUZZY and the limit is stated instead of hidden."""
        assert compare_title("Cœur", "Coeur").strength is SupportStrength.FUZZY


# -- strengths and policies -----------------------------------------------


class TestSupportStrength:
    def test_every_member_has_a_rank(self):
        assert set(STRENGTH_RANK) == set(SupportStrength)

    def test_alias_outranks_normalized(self):
        """An alias is the authority asserting the identity; a fold is us."""
        assert at_least(SupportStrength.ALIAS, SupportStrength.NORMALIZED)
        assert not at_least(SupportStrength.NORMALIZED, SupportStrength.ALIAS)

    def test_it_serializes_as_a_name_not_an_integer(self):
        """An IntEnum would give free ordering and write bare integers into every
        dataset this project produces."""
        assert f"{SupportStrength.EXACT}" == "exact"

    def test_none_is_not_falsy_by_accident(self):
        assert bool(SupportStrength.NONE) is True

    def test_scores_are_rounded_at_construction(self):
        """A raw float is a determinism hazard in canonical JSON."""
        assert Support(SupportStrength.FUZZY, "ratio", score=1 / 3).score == 0.3333


class TestPolicy:
    def test_the_screen_rejects_fuzzy_support(self):
        assert SCREEN_POLICY.minimum is SupportStrength.NORMALIZED
        assert not SCREEN_POLICY.satisfied_by(Support(SupportStrength.FUZZY, "ratio", 0.99))
        assert SCREEN_POLICY.satisfied_by(Support(SupportStrength.NORMALIZED, "fold"))

    def test_a_fuzzy_floor_gates_a_fuzzy_result(self):
        policy = Policy("sweep", minimum=SupportStrength.FUZZY, fuzzy_floor=0.9)
        assert policy.satisfied_by(Support(SupportStrength.FUZZY, "ratio", 0.91))
        assert not policy.satisfied_by(Support(SupportStrength.FUZZY, "ratio", 0.89))
        # A stronger rung does not have to clear a fuzzy floor.
        assert policy.satisfied_by(Support(SupportStrength.ALIAS, "token_set"))

    def test_the_validator_policy_is_deliberately_absent(self):
        """It lands in 1.4, where there is a false-rejection rate to tune it
        against. A stub here would be a number chosen with no evidence."""
        import shelfwarden.compare as compare_module

        assert not hasattr(compare_module, "VALIDATOR_POLICY")


# -- the fold ladder ------------------------------------------------------


class TestFoldLadder:
    def test_the_rule_names_the_rung_that_did_the_work(self):
        assert ladder_rule("The Matrix", "the matrix") == "fold"
        assert ladder_rule("Spider-Man", "Spider Man") == "strip_punctuation"
        assert ladder_rule("The Matrix", "Matrix") == "strip_articles"
        assert ladder_rule("Amelie", "Amélie") == "strip_diacritics"
        assert ladder_rule("Solaris", "Stalker") is None

    def test_the_rungs_are_declared_in_order(self):
        assert [name for name, _ in FOLD_LADDER] == [
            "fold",
            "strip_punctuation",
            "strip_articles",
            "strip_diacritics",
        ]

    def test_each_step_is_a_pure_function_of_its_input(self):
        assert strip_punctuation("Wall-E: The Movie!") == "Wall E The Movie"
        assert strip_articles("the matrix") == "matrix"
        assert strip_articles("theatre") == "theatre"
        assert strip_diacritics("Amélie") == "Amelie"

    def test_whitespace_is_collapsed_not_merely_stripped(self):
        assert fold_text("  Brandon   Sanderson ") == "brandon sanderson"


# -- the comparators ------------------------------------------------------


class TestCompareTitle:
    def test_identity_is_exact(self):
        assert compare_title("Solaris", "Solaris") == Support(SupportStrength.EXACT, "identity")

    def test_an_alias_hit_names_the_alias_that_matched(self):
        """An alias hit that cannot say which alias matched is a claim without a
        citation."""
        support = compare_title(
            "Le Fabuleux Destin d'Amélie Poulain",
            "Amélie",
            aliases=("Amelie from Montmartre", "Le Fabuleux Destin d'Amélie Poulain"),
        )
        assert support.strength is SupportStrength.ALIAS
        assert support.matched == "Le Fabuleux Destin d'Amélie Poulain"

    def test_the_authority_is_tried_before_its_aliases(self):
        """So `matched` is set only when an alias did the work."""
        support = compare_title("the matrix", "The Matrix", aliases=("Matrix",))
        assert support.strength is SupportStrength.NORMALIZED
        assert support.matched is None

    def test_an_empty_value_supports_nothing(self):
        """Two absent titles are not a match; they are two absences."""
        assert compare_title("", "").strength is SupportStrength.NONE
        assert compare_title(None, "Solaris").strength is SupportStrength.NONE

    def test_an_unrelated_title_falls_all_the_way_to_none(self):
        assert compare_title("Solaris", "xyz").strength is SupportStrength.NONE


class TestCompareYear:
    def test_the_delta_travels_with_the_verdict(self):
        """ "Off by one" and "off by forty" are different facts about a match, and
        a remake pair is exactly the case where the second is the finding."""
        support, delta = compare_year(1972, 2002)
        assert support.strength is SupportStrength.NONE
        assert delta == -30

    def test_one_year_apart_is_fuzzy_never_normalized(self):
        support, delta = compare_year(2001, 2002)
        assert (support.strength, delta) == (SupportStrength.FUZZY, -1)
        assert not SCREEN_POLICY.satisfied_by(support)

    def test_a_missing_year_is_missing_not_equal(self):
        support, delta = compare_year(None, 2002)
        assert (support.strength, support.rule, delta) == (SupportStrength.NONE, "missing", None)


class TestComparePersonName:
    def test_person_name_inversion_is_alias_not_fuzzy(self):
        """`author_name_variant`'s entire premise. Landing this at FUZZY would
        make the class's own guard threshold-dependent."""
        support = compare_person_name("Sanderson, Brandon", "Brandon Sanderson")
        assert support.strength is SupportStrength.ALIAS
        assert support.rule == "token_set"
        assert SCREEN_POLICY.satisfied_by(support)

    def test_a_whitespace_variant_reports_the_tighter_rule(self):
        support = compare_person_name("Brandon  Sanderson", "Brandon Sanderson")
        assert (support.strength, support.rule) == (SupportStrength.NORMALIZED, "fold")

    def test_a_misspelling_stays_fuzzy(self):
        support = compare_person_name("Brandon Sandersen", "Brandon Sanderson")
        assert support.strength is SupportStrength.FUZZY
        assert not SCREEN_POLICY.satisfied_by(support)

    def test_tokens_are_order_independent(self):
        assert name_tokens("Sanderson, Brandon") == name_tokens("Brandon Sanderson")


class TestCompareSeriesPosition:
    def test_series_position_is_compared_as_a_string(self):
        """Audnexus returns "3.5" for novellas. `int()` raises on it, and
        `float()` is worse because it succeeds: `3.5 == 3.50` starts being true
        while `"3.5" != "3.50"`, and the two comparisons disagree silently."""
        assert compare_series_position("3.5", "3.5").strength is SupportStrength.EXACT
        support = compare_series_position("3.50", "3.5")
        assert support.strength is SupportStrength.NORMALIZED
        assert support.rule == "normalized_position"

    def test_leading_zeros_normalize(self):
        assert normalize_position("03") == "3"
        assert normalize_position("3.0") == "3"
        assert normalize_position("0") == "0"

    def test_a_non_numeric_position_survives(self):
        """Nothing here coerces, so a position that is not a number still works."""
        assert normalize_position("0Prequel") == "Prequel"
        assert compare_series_position("Prequel", "Prequel").strength is SupportStrength.EXACT

    def test_there_is_no_fuzzy_rung(self):
        """A position is an identifier; the similarity of "3" and "13" is not
        evidence of anything."""
        assert compare_series_position("3", "13").strength is SupportStrength.NONE


class TestIdentifiers:
    def test_plex_local_and_unknown_are_not_resolvable(self):
        unresolvable = {IdNamespace.UNKNOWN, IdNamespace.LOCAL, IdNamespace.PLEX}
        assert frozenset(IdNamespace) - unresolvable == RESOLVABLE_NAMESPACES
        assert not has_resolvable_id(parse_guids("plex://movie/abc"))
        assert has_resolvable_id(parse_guids("plex://movie/abc", ["tmdb://194"]))

    def test_overlap_reports_the_namespaces_that_agree(self):
        left = parse_guids("plex://movie/1", ["tmdb://194", "imdb://tt0211915"])
        right = parse_guids(None, ["tmdb://194", "imdb://tt0000001"])
        assert id_overlap(left, right) == frozenset({IdNamespace.TMDB})

    def test_values_compare_case_insensitively(self):
        """ASINs arrive uppercase and IMDb ids lowercase, from sources that
        disagree about which."""
        left = (parse_guid("asin://b003zwfo7e"),)
        right = (parse_guid("asin://B003ZWFO7E"),)
        assert id_overlap(left, right) == frozenset({IdNamespace.ASIN})


class TestParseReleaseName:
    @pytest.mark.parametrize(
        ("filename", "title", "year"),
        [
            ("/media/Movies/Amélie (2001)/Amélie (2001).mkv", "Amélie", 2001),
            ("Blade.Runner.2049.2017.2160p.BluRay.x265-GRP.mkv", "Blade Runner 2049", 2017),
            ("2001.A.Space.Odyssey.1968.1080p.mkv", "2001 A Space Odyssey", 1968),
            (r"D:\Media\The Matrix (1999)\The Matrix (1999).mkv", "The Matrix", 1999),
            ("Solaris.mkv", "Solaris", None),
        ],
    )
    def test_it_pulls_a_title_and_a_year(self, filename, title, year):
        parsed = parse_release_name(filename)
        assert (parsed.title, parsed.year) == (title, year)

    def test_the_last_year_wins(self):
        """A title can contain a year; the release year is the trailing one."""
        assert parse_release_name("Blade.Runner.2049.2017.mkv").year == 2017

    def test_season_and_episode_are_parsed_and_the_title_stops_there(self):
        parsed = parse_release_name("Cowboy.Bebop.S02E11.1080p.WEB-DL.mkv")
        assert (parsed.season, parsed.episode, parsed.title) == (2, 11, "Cowboy Bebop")

    def test_recognised_tags_are_reported_rather_than_silently_dropped(self):
        parsed = parse_release_name("Movie.Title.1080p.BluRay.x264.mkv")
        assert parsed.title == "Movie Title"
        assert set(parsed.tags) <= RELEASE_TAGS
        assert "1080p" in parsed.tags

    def test_an_unrecognised_tag_stays_in_the_title(self):
        """So a comparison fails loudly instead of a title being silently
        truncated by a tag list nobody has updated."""
        assert "bespokerip" in parse_release_name("Movie.Title.BespokeRip.mkv").title.casefold()

    def test_windows_and_posix_separators_both_work(self):
        """A Plex server on Windows reports backslash paths to a client on
        anything else."""
        assert parse_release_name(r"C:\x\Solaris (1972).mkv").source == "Solaris (1972).mkv"

    def test_the_parsed_title_is_not_normalized(self):
        """Normalizing here would hide trap 3 rather than exercise it: paths are
        the one string the model deliberately leaves alone."""
        assert parse_release_name(nfd("Amélie (2001).mkv")).title == nfd("Amélie")


class TestPathSegments:
    """The directory half of a path, which `parse_release_name` cannot see.

    Verified in step 0.5: `parse_release_name('.../The Way of Kings/CD1.m4b')`
    returns title `'CD1'`. Four corruption classes take their detectability
    witness from a directory, so the basename answering wrongly-but-plausibly is
    the failure mode these tests exist for.
    """

    def test_segments_drop_only_the_final_extension(self):
        assert path_segments("/media/Books/Sanderson/The Way of Kings/CD1.m4b") == (
            "media",
            "Books",
            "Sanderson",
            "The Way of Kings",
            "CD1",
        )

    def test_a_windows_path_splits_too(self):
        # A Plex server on Windows reports backslash paths to a client on anything
        # else, which is why `parse_release_name` handles both separators.
        assert path_segments(r"D:\Media\Movies\Amelie (2001)\Amelie.mkv") == (
            "D:",
            "Media",
            "Movies",
            "Amelie (2001)",
            "Amelie",
        )

    def test_an_empty_path_has_no_segments(self):
        assert path_segments("") == ()
        assert path_segments("///") == ()


class TestParseReleasePath:
    def test_the_basename_wins_where_it_answers(self):
        parsed = parse_release_path("/media/Movies/Whatever/The Dark Knight (2008).mkv")
        assert (parsed.title, parsed.year, parsed.source) == (
            "The Dark Knight",
            2008,
            "The Dark Knight (2008).mkv",
        )

    def test_a_scene_basename_keeps_its_tags(self):
        # `path_segments` strips the extension and `parse_release_name` strips one
        # too, so composing them naively turns `br.1080p.mkv` into `br`. The
        # basename is parsed from the raw segment for exactly this reason.
        parsed = parse_release_path("/m/The.Dark.Knight.2008.1080p.BluRay.x265-GRP.mkv")
        assert parsed.title == "The Dark Knight"
        assert set(parsed.tags) >= {"1080p", "bluray", "x265"}

    def test_the_parent_supplies_a_title_the_basename_lacks(self):
        parsed = parse_release_path("/media/Books/Sanderson/The Way of Kings/CD1.m4b")
        assert parsed.title == "The Way of Kings"
        assert parsed.source == "The Way of Kings"

    def test_the_parent_supplies_a_year_the_basename_lacks(self):
        parsed = parse_release_path("/media/Movies/Amelie (2001)/movie.mkv")
        assert (parsed.title, parsed.year) == ("Amelie", 2001)

    def test_a_structural_segment_supplies_nothing(self):
        # "Season 1" parses to a title of its own and is the nearest parent. Taking
        # it would be a wrong answer rather than a missing one, so the walk skips it.
        parsed = parse_release_path("/media/TV/Cowboy Bebop/Season 1/S01E02.mkv")
        assert (parsed.season, parsed.episode) == (1, 2)
        assert (parsed.title, parsed.source) == ("Cowboy Bebop", "Cowboy Bebop")

    @pytest.mark.parametrize(
        "segment", ["Season 1", "season 01", "CD1", "Disc 2", "Part 3", "Specials", "Extras"]
    )
    def test_structural_segments_are_recognised(self, segment):
        assert is_structural_segment(segment)

    @pytest.mark.parametrize("segment", ["Cowboy Bebop", "The Way of Kings", "Part of Me"])
    def test_a_real_title_is_not_structural(self, segment):
        assert not is_structural_segment(segment)

    def test_tags_are_the_union_across_segments(self):
        parsed = parse_release_path("/media/Movies/Blade Runner (1982) [Remastered]/br.1080p.mkv")
        assert {"remastered", "1080p"} <= set(parsed.tags)
        # Title and year come together, from the segment that carries the year.
        assert (parsed.title, parsed.year) == ("Blade Runner", 1982)

    def test_it_does_not_change_parse_release_name(self):
        # 0.45's tests pin `parse_release_name` and the screen's byte output
        # depends on it. The new function is additive or it is a regression.
        assert parse_release_name("/media/Books/X/The Way of Kings/CD1.m4b").title == "CD1"


class TestFindInPath:
    def test_it_finds_a_series_name_in_a_directory(self):
        support = find_in_path(
            "/media/Books/Sanderson/The Stormlight Archive/The Way of Kings/CD1.m4b",
            "The Stormlight Archive",
        )
        assert support.strength is SupportStrength.EXACT
        assert support.matched == "The Stormlight Archive"

    def test_it_folds_rather_than_matching_substrings(self):
        # `strip_punctuation` is a rung of the shared ladder, so a hit arrives
        # with the rule that produced it rather than as a bare boolean.
        support = find_in_path("/media/Movies/Spider-Man (2002)/film.mkv", "Spider Man (2002)")
        assert support.strength is SupportStrength.NORMALIZED
        assert support.rule == "strip_punctuation"

    def test_an_nfd_directory_matches_an_nfc_authority(self):
        # Trap 3's door: `FilePart.path` is deliberately un-normalized.
        nfd = unicodedata.normalize("NFD", "Amélie")
        support = find_in_path(f"/media/Movies/{nfd} (2001)/movie.mkv", "Amélie (2001)")
        assert support.strength is SupportStrength.NORMALIZED

    def test_it_applies_no_threshold_of_its_own(self):
        # A path always offers some fuzzy noise; deciding whether that counts is
        # a Policy's job. The comparator reports what it found and nothing more.
        noise = find_in_path("/media/Movies/Other/x.mkv", "Amélie")
        assert noise.strength is SupportStrength.FUZZY
        assert not SCREEN_POLICY.satisfied_by(noise)

    def test_no_authority_is_none(self):
        assert find_in_path("/a/b/c.mkv", None).strength is SupportStrength.NONE

    def test_the_strongest_segment_wins(self):
        support = find_in_path("/Amelie/Amélie (2001)/Amélie.mkv", "Amélie")
        assert support.strength is SupportStrength.EXACT
        assert support.matched == "Amélie"


# -- the leaf property ----------------------------------------------------


def test_compare_imports_no_package_module():
    """Belt to the import contract's braces.

    `lint-imports` proves no static import; this proves no lazy one inside a
    function, which the contract cannot see.
    """
    program = (
        "import sys, shelfwarden.compare;"
        "print(sorted(m for m in sys.modules "
        "if m.startswith(('shelfwarden.agent', 'shelfwarden.evals', "
        "'shelfwarden.library', 'shelfwarden.sources'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
