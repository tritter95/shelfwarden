"""Corruptions whose subject is a show, and whose unit is the whole family.

Both classes here are the reason step 0.5 works on families rather than items.
`episode_wrong_season` moves one episode between two others' parents;
`absolute_vs_seasonal` rewrites every episode of a show and leaves a season with
nothing in it. Neither is a field change on a single record, and neither has a
meaningful "one item" to be about.

Both take their witness from the same place: the `SxxEyy` marker in the file
name, which the corruption does not touch. That is not a convenience. It is what
a real library actually looks like when an episode is misfiled -- the metadata
moved and the file did not -- and it is the reason step 0.5 found that
`season_membership_coherent` did not guard `episode_wrong_season` at all.
"""

from collections.abc import Sequence

from shelfwarden.compare import compare_episode_number, parse_release_path
from shelfwarden.evals import export as export_module
from shelfwarden.evals.corrupt.context import (
    CorruptionContext,
    PartRef,
    pick_by_rank,
    subject_key,
)
from shelfwarden.evals.corrupt.model import Rejection
from shelfwarden.evals.corrupt.registry import Applicability, Mutation, corruption
from shelfwarden.evals.corrupt.witness import LocalWitness, WitnessKind, WitnessTier
from shelfwarden.models.finding import ProblemClass
from shelfwarden.models.item import MediaKind, NormalizedItem, dump_item, with_changes


def _seasons(family: export_module.Family) -> list[NormalizedItem]:
    return [
        record
        for record in family.records
        if record.media_kind is MediaKind.SEASON and record.index is not None
    ]


def _episodes(family: export_module.Family) -> list[NormalizedItem]:
    return [record for record in family.records if record.media_kind is MediaKind.EPISODE]


def _marked(episode: NormalizedItem) -> PartRef | None:
    """The episode's first file that names a season and an episode.

    Without one there is nothing outside the metadata that knows where the
    episode belongs, so the case would not be solvable and must not ship.
    """
    for index, part in enumerate(getattr(episode, "parts", ())):
        parsed = parse_release_path(part.path)
        if parsed.season is not None and parsed.episode is not None:
            return PartRef(str(episode.item_id), index, part.path)
    return None


def _numbered_episodes(family: export_module.Family) -> list[NormalizedItem]:
    """Episodes that carry both a season and an episode number *and* a marked file."""
    return [
        episode
        for episode in _episodes(family)
        if episode.index is not None and episode.parent_index is not None and _marked(episode)
    ]


def _replace_many(
    family: export_module.Family,
    updates: dict[str, dict[str, object]],
    removed: Sequence[str] = (),
) -> tuple[NormalizedItem, ...]:
    dropped = set(removed)
    return tuple(
        with_changes(record, updates[str(record.item_id)])
        if str(record.item_id) in updates
        else record
        for record in family.records
        if str(record.item_id) not in dropped
    )


# -- episode_wrong_season -------------------------------------------------


def _wrong_season_applicable(family: export_module.Family, ctx: CorruptionContext) -> Applicability:
    if len(_seasons(family)) < 2:
        return Applicability.no("one_season", "a misfiled episode needs somewhere else to go")
    if not _numbered_episodes(family):
        return Applicability.no(
            "no_marked_episode",
            "no episode carries both a numbering and a file that names it, so nothing "
            "outside the metadata would know where it belongs",
        )
    return Applicability.yes()


@corruption(
    ProblemClass.EPISODE_WRONG_SEASON,
    applies_to={MediaKind.SHOW},
    variants=("reparent",),
    witness=WitnessKind.VALUE,
    tier=WitnessTier.LOCAL,
    applicable=_wrong_season_applicable,
)
def episode_wrong_season(
    family: export_module.Family, ctx: CorruptionContext
) -> Mutation | Rejection:
    """Move one episode into another season, metadata and parent together.

    The `index_only` variant -- moving `parent_index` and leaving `parent` where
    it was -- is deliberately not implemented. Plex derives an episode's season
    from its parent, so that state is unrepresentable in a real library, and
    shipping it would let an internal-coherence check score detections that no
    live server could ever require.
    """
    episode = pick_by_rank(
        ctx.seed, [(subject_key(item), item) for item in _numbered_episodes(family)]
    )
    if episode is None:  # pragma: no cover -- applicability already checked
        return ctx.reject("no_marked_episode")

    elsewhere = [season for season in _seasons(family) if season.index != episode.parent_index]
    target = pick_by_rank(ctx.seed, [(subject_key(item), item) for item in elsewhere])
    if target is None:
        return ctx.reject("no_other_season", "every season carries this episode's number")

    truth = (episode.parent_index, episode.index)
    mutated = _replace_many(
        family,
        {
            str(episode.item_id): {
                "parent": dump_item(target)["item_id"],
                "parent_index": target.index,
                "parent_title": target.title,
            }
        },
    )

    reference = _marked(episode)
    parsed = parse_release_path(reference.path)
    resolved = (parsed.season, parsed.episode)
    witness = LocalWitness.over(ctx.export_id, mutated).value(
        subject_id=reference.item_id,
        pointer=reference.pointer,
        comparator="compare_episode_number",
        resolved=list(resolved),
        against_truth=compare_episode_number(resolved, truth),
        against_corrupted=compare_episode_number(resolved, (target.index, episode.index)),
        policy=ctx.policy,
    )
    return Mutation(items=mutated, witness=witness)


# -- absolute_vs_seasonal -------------------------------------------------


def _absolute_applicable(family: export_module.Family, ctx: CorruptionContext) -> Applicability:
    seasons = _seasons(family)
    if len(seasons) < 2:
        return Applicability.no(
            "one_season", "a seasonal show needs two seasons for absolute numbering to differ"
        )
    numbered = _numbered_episodes(family)
    if len(numbered) < 2:
        return Applicability.no("too_few_episodes", "fewer than two episodes carry a marked file")
    if len(numbered) != len(_episodes(family)):
        return Applicability.no(
            "unmarked_episodes",
            "some episodes carry no SxxEyy marker, so the renumbering would be only "
            "partly witnessed",
        )
    return Applicability.yes()


@corruption(
    ProblemClass.ABSOLUTE_VS_SEASONAL,
    applies_to={MediaKind.SHOW},
    variants=("collapse_to_absolute",),
    witness=WitnessKind.VALUE,
    tier=WitnessTier.LOCAL,
    applicable=_absolute_applicable,
)
def absolute_vs_seasonal(
    family: export_module.Family, ctx: CorruptionContext
) -> Mutation | Rejection:
    """Renumber a seasonal show with running absolute numbering under one season.

    The now-empty seasons are **removed**, not left behind: a real server does not
    keep a season with nothing in it, and leaving them would make the corrupted
    snapshot describe a library shape Plex cannot produce.
    """
    seasons = sorted(_seasons(family), key=lambda season: season.index)
    container = seasons[0]
    ordered = sorted(_numbered_episodes(family), key=lambda ep: (ep.parent_index, ep.index))

    updates: dict[str, dict[str, object]] = {}
    changed: list[tuple[NormalizedItem, tuple[int, int]]] = []
    parent_id = dump_item(container)["item_id"]
    for absolute, episode in enumerate(ordered, start=1):
        before = (episode.parent_index, episode.index)
        after = (container.index, absolute)
        if before != after:
            changed.append((episode, before))
        updates[str(episode.item_id)] = {
            "parent": parent_id,
            "parent_index": container.index,
            "parent_title": container.title,
            "index": absolute,
        }

    if not changed:
        return ctx.reject(
            "already_absolute", "the show is already numbered as one running sequence"
        )

    removed = [str(season.item_id) for season in seasons[1:]]
    updates[str(family.records[0].item_id)] = {"child_count": 1}
    mutated = _replace_many(family, updates, removed)

    episode, truth = changed[0]
    reference = _marked(episode)
    parsed = parse_release_path(reference.path)
    resolved = (parsed.season, parsed.episode)
    corrupted = (
        updates[str(episode.item_id)]["parent_index"],
        updates[str(episode.item_id)]["index"],
    )
    witness = LocalWitness.over(ctx.export_id, mutated).value(
        subject_id=reference.item_id,
        pointer=reference.pointer,
        comparator="compare_episode_number",
        resolved=list(resolved),
        against_truth=compare_episode_number(resolved, truth),
        against_corrupted=compare_episode_number(resolved, corrupted),
        policy=ctx.policy,
    )
    return Mutation(items=mutated, witness=witness)


__all__ = ["absolute_vs_seasonal", "episode_wrong_season"]
