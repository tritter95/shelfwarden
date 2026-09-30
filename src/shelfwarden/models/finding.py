"""Findings, and the vocabulary of problems the harness can name.

Step 0.45 creates this module with `ProblemClass` and nothing else. The claim
union, `Citation`, and `RepairProposal` land in step 1.4 at this same path, so
the validator fills the file in rather than moving it.

`ProblemClass` arrives early because two step-0.45 consumers need it as a type
rather than as a string literal: `census.READINESS_RULES` (which counted
structural candidates against fifteen hand-written strings) and
`evals.screen.GUARD_TABLE` (which says which predicates verify which class).
A typo in either produced a row naming a class no corruption will ever emit,
and nothing caught it. As an enum, a typo is an import error and a missing
member breaks a test that asserts every class has a row in both tables.

Step 0.6 adds `CLASS_KINDS` here rather than in `evals/`, because two packages
read it and one of them is not `evals`: the screen uses it to keep a guard from
being credited to a media kind the class cannot describe, and the truth file uses
it to say that such a class is *trivially* guarded rather than unverified.
"""

from enum import StrEnum

from shelfwarden.models.item import MediaKind


class ProblemClass(StrEnum):
    """The fifteen problem classes from spec §3 and implementation-plan.md §3.

    Ordered movies/TV first, then audiobooks, matching the corruption table in
    the implementation plan. `StrEnum` because these names are written into
    every dataset this project produces and are read by a human choosing
    `composition.toml` shares.
    """

    WRONG_MATCH = "wrong_match"
    YEAR_COLLISION_REMAKE = "year_collision_remake"
    FOREIGN_TITLE_VARIANT = "foreign_title_variant"
    ALTERNATE_CUT = "alternate_cut"
    MISSING_METADATA = "missing_metadata"
    DUPLICATE_QUALITY = "duplicate_quality"
    EPISODE_WRONG_SEASON = "episode_wrong_season"
    ABSOLUTE_VS_SEASONAL = "absolute_vs_seasonal"
    FILENAME_UNMATCHABLE = "filename_unmatchable"
    SERIES_ORDER_BROKEN = "series_order_broken"
    AUTHOR_NAME_VARIANT = "author_name_variant"
    NARRATOR_AS_AUTHOR = "narrator_as_author"
    MULTI_FILE_SPLIT = "multi_file_split"
    MISSING_SERIES = "missing_series"
    ANTHOLOGY_OMNIBUS = "anthology_omnibus"


# Which media kinds each problem class can *describe*. Not which kinds a
# corruption runs on: `CorruptionSpec.applies_to` names the **root** of the family
# a corruption is handed (`episode_wrong_season` runs on a SHOW and misfiles an
# EPISODE), so the two tables are different statements and neither contains the
# other.
#
# Verified in step 0.6, Finding 5: the screen was crediting `absolute_vs_seasonal`
# -- a TV class -- as guarded on every movie, because its guard predicate
# `filename_matches_metadata` is applicable to movies. The guard was passing for a
# class that can never describe the item.
#
# The bias here is deliberately toward **breadth**. A kind wrongly present costs a
# row of guard coverage the class cannot really claim; a kind wrongly absent makes
# the class trivially guarded on an item it can genuinely describe, and a correct
# agent finding there scores as a false positive -- the direction this project has
# forbidden. When in doubt, include the kind and let the guard report `unguarded`.
CLASS_KINDS: dict[ProblemClass, frozenset[MediaKind]] = {
    # The four kinds an agent matches to an external record.
    ProblemClass.WRONG_MATCH: frozenset(
        {MediaKind.MOVIE, MediaKind.SHOW, MediaKind.EPISODE, MediaKind.AUDIOBOOK}
    ),
    ProblemClass.YEAR_COLLISION_REMAKE: frozenset({MediaKind.MOVIE, MediaKind.SHOW}),
    ProblemClass.FOREIGN_TITLE_VARIANT: frozenset(
        {MediaKind.MOVIE, MediaKind.SHOW, MediaKind.EPISODE, MediaKind.AUDIOBOOK}
    ),
    # A cut is a property of a film. A season has no theatrical release.
    ProblemClass.ALTERNATE_CUT: frozenset({MediaKind.MOVIE}),
    ProblemClass.MISSING_METADATA: frozenset(
        {MediaKind.MOVIE, MediaKind.SHOW, MediaKind.EPISODE, MediaKind.AUDIOBOOK}
    ),
    # Two entries for one work. The corruption only clones movies today; a
    # duplicated show or audiobook is the same problem and an agent may find one.
    ProblemClass.DUPLICATE_QUALITY: frozenset(
        {MediaKind.MOVIE, MediaKind.SHOW, MediaKind.AUDIOBOOK}
    ),
    # Only an episode can be under the wrong season. A show cannot be.
    ProblemClass.EPISODE_WRONG_SEASON: frozenset({MediaKind.EPISODE}),
    # A numbering scheme is a property of the show and is visible on its parts.
    ProblemClass.ABSOLUTE_VS_SEASONAL: frozenset(
        {MediaKind.SHOW, MediaKind.SEASON, MediaKind.EPISODE}
    ),
    # Wherever files live: a movie, an episode, an audiobook part -- and the book
    # itself, whose parts are what carry the unmatchable names.
    ProblemClass.FILENAME_UNMATCHABLE: frozenset(
        {MediaKind.MOVIE, MediaKind.EPISODE, MediaKind.AUDIOBOOK, MediaKind.AUDIOBOOK_PART}
    ),
    ProblemClass.SERIES_ORDER_BROKEN: frozenset({MediaKind.AUDIOBOOK}),
    ProblemClass.AUTHOR_NAME_VARIANT: frozenset({MediaKind.AUTHOR}),
    # The author entry is the wrong person, and the book is where the authority
    # record that says so is fetched -- which is why the guard is on the book.
    ProblemClass.NARRATOR_AS_AUTHOR: frozenset({MediaKind.AUTHOR, MediaKind.AUDIOBOOK}),
    ProblemClass.MULTI_FILE_SPLIT: frozenset({MediaKind.AUDIOBOOK}),
    ProblemClass.MISSING_SERIES: frozenset({MediaKind.AUDIOBOOK}),
    ProblemClass.ANTHOLOGY_OMNIBUS: frozenset({MediaKind.AUDIOBOOK}),
}


def describes(problem_class: ProblemClass, media_kind: MediaKind) -> bool:
    """Can this class be a statement about an item of this kind?

    A `False` here is what makes a finding a false positive rather than an
    unverified claim: nobody has to check whether a film uses absolute episode
    numbering.
    """
    return media_kind in CLASS_KINDS[problem_class]


__all__ = ["CLASS_KINDS", "ProblemClass", "describes"]
