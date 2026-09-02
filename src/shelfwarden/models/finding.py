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
"""

from enum import StrEnum


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


__all__ = ["ProblemClass"]
