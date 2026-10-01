"""Every corruption builds a world Plex could serve.

Step 0.7, Finding 5: three recipes edited one record and left the copies Plex
derives from it stale on its neighbours.

* `wrong_match` on a show left every season naming the true show.
* `author_name_variant` left a moved book's parts pointing at the canonical
  author.
* `multi_file_split` left the author's album count one short.

The first two leak the answer to anything that reads the neighbours. The shared
fixture missed the second because the one book it moves has no parts, so this
sweep runs over a library built to exercise each case.

The check is relative. A disagreement the source already had (the fixture's
Edgedancer declares one part and has none) is inherited, not introduced.
"""

import pytest

from shelfwarden.compare import Support, SupportStrength
from shelfwarden.evals.corrupt import run_corruptions
from shelfwarden.evals.corrupt.context import CorruptionContext, group_families, subject_key
from shelfwarden.evals.corrupt.model import CorruptionError, Rejection
from shelfwarden.evals.corrupt.registry import CORRUPTION_TABLE, CorruptionSpec, Mutation, attempt
from shelfwarden.evals.corrupt.reverse import apply_changes
from shelfwarden.evals.corrupt.witness import LocalWitness
from shelfwarden.models.finding import ProblemClass
from shelfwarden.models.hierarchy import derived_violations, newly_violated, structural_violations
from shelfwarden.models.ids import ItemId
from shelfwarden.models.item import (
    AudiobookItem,
    AudiobookPartItem,
    AuthorItem,
    FetchProfile,
    FilePart,
    stub_of,
    with_changes,
)

from ..conftest import BOOKS, _library_records


def _author_whose_moved_books_have_parts():
    """Four books of one part each, so a variant spelling takes two books that
    both have parts -- the case the shared fixture's Edgedancer cannot show."""
    author = AuthorItem(
        item_id=ItemId("fake", BOOKS, "601"),
        fetched=FetchProfile.CORE,
        title="Robin Hobb",
        album_count=4,
    )
    records = [author]
    for n in range(4):
        book = AudiobookItem(
            item_id=ItemId("fake", BOOKS, f"61{n}"),
            fetched=FetchProfile.CORE,
            title=f"Book {n}",
            parent=author.item_id,
            parent_title=author.title,
            index=n + 1,
            part_count=1,
        )
        part = AudiobookPartItem(
            item_id=ItemId("fake", BOOKS, f"62{n}"),
            fetched=FetchProfile.CORE,
            title="Part 1",
            parent=book.item_id,
            grandparent=author.item_id,
            index=1,
            parts=(
                FilePart(
                    media_id=f"96{n}",
                    part_id=f"16{n}",
                    path=f"/media/Books/Robin Hobb/Book {n}/Book {n}.m4b",
                    container="m4b",
                ),
            ),
        )
        records += [book, part]
    return records


@pytest.fixture(scope="module")
def library():
    return (*_library_records(), *_author_whose_moved_books_have_parts())


@pytest.fixture(scope="module")
def survey(library):
    roots = [stub_of(item) for item in library if getattr(item, "parent", None) is None]
    return run_corruptions(export_id="exp-coherence", items=library, roots=roots, seed=1518)


def _violations(records):
    return (*structural_violations(records), *derived_violations(records))


def test_the_sweep_reaches_the_three_classes_finding_5_names(survey):
    """So the next test cannot pass by having nothing to check."""
    emitted = {str(result.problem_class) for result in survey.results}
    assert {"wrong_match", "author_name_variant", "multi_file_split"} <= emitted
    shows = [
        r for r in survey.results if str(r.problem_class) == "wrong_match" and ":2:" in r.root_id
    ]
    assert shows, "no wrong_match case on a show"


def test_no_corruption_introduces_an_incoherence(library, survey):
    source = _violations(library)
    introduced = {}
    for result in survey.results:
        world = apply_changes(library, result.changes)
        found = newly_violated(_violations(world), source)
        if found:
            introduced[f"{result.problem_class} {result.variant} on {result.root_id}"] = [
                str(violation) for violation in found[:3]
            ]
    assert introduced == {}


def test_the_sources_own_disagreement_is_inherited_not_introduced(library):
    (inherited,) = derived_violations(library)
    assert (inherited.subject, inherited.path) == ("fake:3:412", "/part_count")


def test_nothing_was_rejected_as_incoherent(survey):
    """Propagation repairs every derived copy a recipe makes stale, so the
    `world_incoherent` check is a backstop that should never fire on today's
    recipes."""
    assert [r for r in survey.rejections if r.reason == "world_incoherent"] == []


# -- the backstop and the guard, forced -----------------------------------------


def _show_family(library):
    payload = list(library)
    roots = [stub_of(item) for item in payload if getattr(item, "parent", None) is None]
    family = next(f for f in group_families(payload) if str(f.root.item_id) == "fake:2:201")
    return family, roots, payload


def _context(family, roots, payload, problem_class=ProblemClass.WRONG_MATCH):
    return CorruptionContext.build(
        export_id="exp-coherence",
        seed=1518,
        problem_class=problem_class,
        variant="forced",
        root=family.root,
        subject=subject_key(family.records[0]),
        items={str(item.item_id): item for item in payload},
        roots=roots,
    )


def _spec(corrupt):
    real = CORRUPTION_TABLE[ProblemClass.WRONG_MATCH]
    return CorruptionSpec(
        problem_class=real.problem_class,
        applies_to=real.applies_to,
        variants=("forced",),
        witness_kind=real.witness_kind,
        tier=real.tier,
        induces=(),
        applicable=real.applicable,
        corrupt=corrupt,
        doc="",
    )


def _witness(ctx, records, subject_id, pointer, resolved):
    return LocalWitness.over(ctx.export_id, records).value(
        subject_id=subject_id,
        pointer=pointer,
        comparator="compare_title",
        resolved=resolved,
        against_truth=Support(SupportStrength.EXACT, "identity"),
        against_corrupted=Support(SupportStrength.NONE, "no_match"),
        policy=ctx.policy,
    )


def test_a_recipe_that_breaks_the_tree_is_rejected_as_incoherent(library):
    """Propagation repairs derived copies; it cannot repair a missing season. A
    recipe that deletes one and keeps its episodes is refused with the rule."""
    family, roots, payload = _show_family(library)
    ctx = _context(family, roots, payload)

    def orphan_the_episodes(fam, c):
        kept = tuple(r for r in fam.records if str(r.item_id) != "fake:2:211")
        return Mutation(items=kept, witness=_witness(c, kept, "fake:2:201", "/title", "x"))

    outcome = attempt(_spec(orphan_the_episodes), family, ctx)
    assert isinstance(outcome, Rejection)
    assert outcome.reason == "world_incoherent"
    assert "orphan" in (outcome.detail or "")


def test_a_witness_citing_a_copy_propagation_rewrites_is_a_recipe_bug(library):
    """The witness is built before propagation. Citing a field it then changes
    would describe a world that no longer exists, so this raises, not rejects."""
    family, roots, payload = _show_family(library)
    ctx = _context(family, roots, payload)

    def retitle_and_cite_a_season(fam, c):
        renamed = (with_changes(fam.records[0], {"title": "Pilot Only"}), *fam.records[1:])
        return Mutation(
            items=renamed,
            witness=_witness(c, renamed, "fake:2:211", "/parent_title", "Cowboy Bebop"),
        )

    with pytest.raises(CorruptionError, match="which the witness cites"):
        attempt(_spec(retitle_and_cite_a_season), family, ctx)
