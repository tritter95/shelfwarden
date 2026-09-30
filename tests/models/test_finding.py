"""`ProblemClass` is an enum so that a typo is an import error.

Before step 0.45 the fifteen class names were string literals in
`census.READINESS_RULES` and in prose. A typo there produced a readiness row
naming a class no corruption will ever emit, and nothing caught it. The tests
here are the mechanization of that: every member must have a row in the census's
readiness table and an entry in the screen's guard table, so adding a sixteenth
class breaks the build rather than defaulting to unguarded.
"""

from shelfwarden.evals.census import READINESS_RULES
from shelfwarden.evals.screen import GUARD_TABLE
from shelfwarden.models.finding import CLASS_KINDS, ProblemClass, describes
from shelfwarden.models.item import MediaKind


def test_there_are_fifteen_classes():
    """The number is load-bearing: `composition.toml` allocates shares per class,
    and guard coverage is reported as "n of 15"."""
    assert len(ProblemClass) == 15


def test_every_problem_class_has_a_readiness_row():
    assert {name for name, _ in READINESS_RULES} == set(ProblemClass)


def test_every_problem_class_has_a_guard_table_entry():
    """An absent entry would read as "unguarded" and be indistinguishable from a
    deliberate empty guard set."""
    assert set(GUARD_TABLE) == set(ProblemClass)


def test_the_readiness_table_and_the_guard_table_cannot_drift():
    """They are keyed on the same enum, which is the point of introducing it."""
    assert {name for name, _ in READINESS_RULES} == set(GUARD_TABLE)


def test_it_serializes_as_its_name():
    """These strings are written into every dataset the project produces."""
    assert f"{ProblemClass.WRONG_MATCH}" == "wrong_match"
    assert ProblemClass("anthology_omnibus") is ProblemClass.ANTHOLOGY_OMNIBUS


def test_every_problem_class_declares_a_kind_it_can_describe():
    """A missing row would raise `KeyError` inside the screen; an empty one would
    make the class trivially guarded on every item, so every finding of it -- right
    or wrong -- would score as a false positive."""
    assert set(CLASS_KINDS) == set(ProblemClass)
    assert all(CLASS_KINDS[problem_class] for problem_class in ProblemClass)


def test_a_tv_numbering_class_cannot_describe_a_film():
    """Step 0.6, Finding 5, in its smallest form."""
    assert not describes(ProblemClass.ABSOLUTE_VS_SEASONAL, MediaKind.MOVIE)
    assert describes(ProblemClass.ABSOLUTE_VS_SEASONAL, MediaKind.EPISODE)
