"""Corruptions whose subject is a film, and the one that is about files.

Five classes, all with local ingredients and local witnesses. The witness for
four of them is the same fact: **a corruption does not rewrite the filesystem.**
Plex's metadata and the file on disk are two records of the same claim, and
breaking one leaves the other standing -- which is exactly how a real library
betrays a wrong match, and exactly what `parse_release_path` recovers.

`foreign_title_variant` is not here. It substitutes a real TMDB
`alternative_titles` entry, and inventing one produces a case about a film that
does not exist; see `registry.UNSYNTHESIZABLE_REASON`.
"""

from collections.abc import Sequence

from shelfwarden.compare import (
    compare_title,
    compare_year,
    find_in_path,
    fold_text,
    parse_release_path,
)
from shelfwarden.evals import export as export_module
from shelfwarden.evals.corrupt.context import (
    CorruptionContext,
    PartRef,
    SubjectKey,
    parts_in,
    pick_by_rank,
    subject_key,
)
from shelfwarden.evals.corrupt.model import Rejection
from shelfwarden.evals.corrupt.registry import Applicability, Mutation, corruption
from shelfwarden.evals.corrupt.witness import LocalWitness, WitnessKind, WitnessTier
from shelfwarden.models.finding import ProblemClass
from shelfwarden.models.item import MediaKind, NormalizedItem, dump_item, with_changes

# Edition markers a folder or filename may carry. The four already in
# `compare.RELEASE_TAGS` (extended, unrated, remastered, imax) are deliberately
# repeated: this set is about *which cut a disc holds*, and the fact that the
# release-tag vocabulary overlaps is a coincidence of naming rather than a shared
# meaning. Extend from a census, not from imagination.
EDITION_MARKERS: frozenset[str] = frozenset(
    {
        "director's cut",
        "directors cut",
        "final cut",
        "theatrical cut",
        "theatrical",
        "extended",
        "extended cut",
        "unrated",
        "uncut",
        "remastered",
        "imax",
        "redux",
        "ultimate edition",
        "special edition",
    }
)

# Scene-release furniture. `filename_unmatchable` writes a name that is genuinely
# parseable, because the opposite -- a name carrying no signal at all -- is not a
# hard case but an unsolvable one, and the harness would be measuring nothing.
SCENE_TAGS: tuple[tuple[str, ...], ...] = (
    ("1080p", "BluRay", "x264"),
    ("2160p", "UHD", "BluRay", "x265"),
    ("1080p", "WEB-DL", "DDP5", "H", "264"),
    ("720p", "HDTV", "x264"),
)
SCENE_GROUPS: tuple[str, ...] = ("GRP", "NTb", "RARBG", "SPARKS", "FLUX")


def _root(family: export_module.Family) -> NormalizedItem:
    return family.records[0]


def _replace(
    family: export_module.Family, item_id: str, changes: dict[str, object]
) -> tuple[NormalizedItem, ...]:
    """The family with one item mutated through `with_changes`, which re-validates."""
    return tuple(
        with_changes(record, changes) if str(record.item_id) == item_id else record
        for record in family.records
    )


def _sibling_roots(ctx: CorruptionContext, victim: NormalizedItem) -> list[NormalizedItem]:
    """Every other root of the victim's kind in the victim's section."""
    return [
        candidate
        for candidate in ctx.items.values()
        if candidate.media_kind is victim.media_kind
        and candidate.item_id != victim.item_id
        and candidate.item_id.section_id == victim.item_id.section_id
        and getattr(candidate, "parent", None) is None
    ]


def _ranked(candidates: Sequence[NormalizedItem]) -> list[tuple[SubjectKey, NormalizedItem]]:
    return [(subject_key(candidate), candidate) for candidate in candidates]


def _other_titles(ctx: CorruptionContext, victim: NormalizedItem) -> list[NormalizedItem]:
    """Roots with a *different* title: the donor pool for `wrong_match`."""
    folded = fold_text(victim.title)
    return [
        candidate
        for candidate in _sibling_roots(ctx, victim)
        if fold_text(candidate.title) != folded
    ]


def _remake_partners(ctx: CorruptionContext, victim: NormalizedItem) -> list[NormalizedItem]:
    """Roots sharing the title with a *different* year -- a remake pair, not a duplicate."""
    folded = fold_text(victim.title)
    year = getattr(victim, "year", None)
    return [
        candidate
        for candidate in _sibling_roots(ctx, victim)
        if fold_text(candidate.title) == folded
        and getattr(candidate, "year", None) is not None
        and getattr(candidate, "year", None) != year
    ]


def _title_year_twins(ctx: CorruptionContext, victim: NormalizedItem) -> list[NormalizedItem]:
    """Roots the screen would already call twins.

    Title **and** year, matching `screen.PopulationIndex`'s key. Title alone would
    count a remake pair as a duplicate and block `duplicate_quality` on every film
    that has ever been remade.
    """
    folded = fold_text(victim.title)
    year = getattr(victim, "year", None)
    return [
        candidate
        for candidate in _sibling_roots(ctx, victim)
        if fold_text(candidate.title) == folded and getattr(candidate, "year", None) == year
    ]


# -- wrong_match ----------------------------------------------------------


def _wrong_match_applicable(family: export_module.Family, ctx: CorruptionContext) -> Applicability:
    if not parts_in(family):
        return Applicability.no("no_file_parts", "nothing on disk to witness the true identity")
    victim = _root(family)
    if not _other_titles(ctx, victim):
        return Applicability.no(
            "no_donor", f"no other {victim.media_kind} in section {victim.item_id.section_id}"
        )
    return Applicability.yes()


@corruption(
    ProblemClass.WRONG_MATCH,
    applies_to={MediaKind.MOVIE, MediaKind.SHOW},
    variants=("donor_same_section",),
    witness=WitnessKind.VALUE,
    tier=WitnessTier.LOCAL,
    applicable=_wrong_match_applicable,
    induces=(ProblemClass.DUPLICATE_QUALITY,),
)
def wrong_match(family: export_module.Family, ctx: CorruptionContext) -> Mutation | Rejection:
    """Give an item another real title's identity, and leave the files alone."""
    victim = _root(family)
    donor = pick_by_rank(ctx.seed, _ranked(_other_titles(ctx, victim)))
    if donor is None:  # pragma: no cover -- applicability already checked
        return ctx.reject("no_donor")

    donated = dump_item(donor)
    mutated = _replace(
        family,
        str(victim.item_id),
        {
            "title": donated["title"],
            "year": donated.get("year"),
            "summary": donated.get("summary"),
            "guids": donated["guids"],
        },
    )
    return _path_title_witness(family, ctx, mutated, victim, donor.title)


def _path_title_witness(
    family: export_module.Family,
    ctx: CorruptionContext,
    mutated: tuple[NormalizedItem, ...],
    victim: NormalizedItem,
    corrupted_title: str,
) -> Mutation | Rejection:
    """The witness shared by `wrong_match`: the path still names the real film."""
    reference = _best_title_part(family, victim.title)
    if reference is None:
        return ctx.reject("no_parseable_path", "no file in this family parses to a title")
    parsed = parse_release_path(reference.path)
    witness = LocalWitness.over(ctx.export_id, mutated).value(
        subject_id=reference.item_id,
        pointer=reference.pointer,
        comparator="compare_title",
        resolved=parsed.title,
        against_truth=compare_title(parsed.title, victim.title),
        against_corrupted=compare_title(parsed.title, corrupted_title),
        policy=ctx.policy,
    )
    return Mutation(items=mutated, witness=witness)


def _best_title_part(family: export_module.Family, title: str) -> PartRef | None:
    """The part whose path speaks most strongly for a title.

    Ranked rather than first-wins: a family may hold several files and only one of
    them may carry the name, and picking the wrong one produces a rejection that
    looks like a library problem.
    """
    best: tuple[float, PartRef] | None = None
    for ref in parts_in(family):
        support = find_in_path(ref.path, title)
        score = (support.score or 0.0) + len(support.strength)
        if support.strength.value == "none":
            continue
        if best is None or score > best[0]:
            best = (score, ref)
    return best[1] if best else None


# -- year_collision_remake ------------------------------------------------


def _remake_applicable(family: export_module.Family, ctx: CorruptionContext) -> Applicability:
    victim = _root(family)
    if getattr(victim, "year", None) is None:
        return Applicability.no("no_year", "the victim records no year to collide")
    if not _remake_partners(ctx, victim):
        return Applicability.no(
            "no_remake_pair",
            f"no other item titled {victim.title!r} with a different year in this section",
        )
    if not any(parse_release_path(ref.path).year == victim.year for ref in parts_in(family)):
        return Applicability.no(
            "no_year_in_path", "no file names the real year, so nothing could witness it"
        )
    return Applicability.yes()


@corruption(
    ProblemClass.YEAR_COLLISION_REMAKE,
    applies_to={MediaKind.MOVIE},
    variants=("swap_to_other_version",),
    witness=WitnessKind.VALUE,
    tier=WitnessTier.LOCAL,
    applicable=_remake_applicable,
    induces=(ProblemClass.DUPLICATE_QUALITY,),
)
def year_collision_remake(
    family: export_module.Family, ctx: CorruptionContext
) -> Mutation | Rejection:
    """Move a film onto the other version's year and id. The title was right all along."""
    victim = _root(family)
    other = pick_by_rank(ctx.seed, _ranked(_remake_partners(ctx, victim)))
    if other is None:  # pragma: no cover -- applicability already checked
        return ctx.reject("no_remake_pair")

    donated = dump_item(other)
    mutated = _replace(
        family, str(victim.item_id), {"year": donated["year"], "guids": donated["guids"]}
    )

    reference = next(
        (ref for ref in parts_in(family) if parse_release_path(ref.path).year == victim.year), None
    )
    if reference is None:  # pragma: no cover -- applicability already checked
        return ctx.reject("no_year_in_path")
    parsed = parse_release_path(reference.path)
    truth_support, _ = compare_year(parsed.year, victim.year)
    corrupt_support, _ = compare_year(parsed.year, donated["year"])
    witness = LocalWitness.over(ctx.export_id, mutated).value(
        subject_id=reference.item_id,
        pointer=reference.pointer,
        comparator="compare_year",
        resolved=parsed.year,
        against_truth=truth_support,
        against_corrupted=corrupt_support,
        policy=ctx.policy,
    )
    return Mutation(items=mutated, witness=witness)


# -- alternate_cut --------------------------------------------------------


def _edition_in_path(family: export_module.Family, edition: str) -> PartRef | None:
    for ref in parts_in(family):
        if find_in_path(ref.path, edition).strength.value in {"exact", "normalized", "alias"}:
            return ref
    return None


def _alternate_cut_applicable(
    family: export_module.Family, ctx: CorruptionContext
) -> Applicability:
    victim = _root(family)
    edition = getattr(victim, "edition_title", None)
    if not edition:
        return Applicability.no("no_edition", "the item records no edition to strip")
    if fold_text(edition) not in {fold_text(marker) for marker in EDITION_MARKERS}:
        return Applicability.no("unrecognised_edition", f"{edition!r} is not a known cut marker")
    if _edition_in_path(family, edition) is None:
        return Applicability.no(
            "no_edition_marker_in_path",
            f"no file names {edition!r}, so nothing outside the metadata knows the cut",
        )
    return Applicability.yes()


@corruption(
    ProblemClass.ALTERNATE_CUT,
    applies_to={MediaKind.MOVIE},
    variants=("strip_edition",),
    witness=WitnessKind.VALUE,
    tier=WitnessTier.LOCAL,
    applicable=_alternate_cut_applicable,
)
def alternate_cut(family: export_module.Family, ctx: CorruptionContext) -> Mutation | Rejection:
    """Clear the edition marker so two cuts of one film become indistinguishable.

    Only `strip_edition` ships. The `collide` variant -- copying a sibling's
    edition onto this one -- needs a library with two *marked* cuts of one film,
    which the census has not yet shown to exist. Declaring a variant that always
    rejects would put a permanent zero in the deficit table and read as a bug.
    """
    victim = _root(family)
    edition = victim.edition_title
    reference = _edition_in_path(family, edition)
    if reference is None:  # pragma: no cover -- applicability already checked
        return ctx.reject("no_edition_marker_in_path")

    mutated = _replace(family, str(victim.item_id), {"edition_title": None})
    support = find_in_path(reference.path, edition)
    witness = LocalWitness.over(ctx.export_id, mutated).value(
        subject_id=reference.item_id,
        pointer=reference.pointer,
        comparator="find_in_path",
        resolved=support.matched,
        against_truth=support,
        # There is nothing left in the metadata to agree with, which is the
        # corruption: an absent edition cannot be supported by any evidence.
        against_corrupted=compare_title(support.matched, None),
        policy=ctx.policy,
    )
    return Mutation(items=mutated, witness=witness)


# -- duplicate_quality ----------------------------------------------------

# What the clone differs by, and how its file is named. One case is one duplicate
# pair, so the variant is the *axis* of duplication rather than a count.
DUPLICATE_VARIANTS: dict[str, dict[str, object]] = {
    "resolution": {"video_resolution": "2160", "marker": "2160p"},
    "container": {"container": "avi", "marker": "XviD"},
    "bitrate": {"size_bytes": 2_000_000_000, "marker": "SD"},
}


def _duplicate_applicable(family: export_module.Family, ctx: CorruptionContext) -> Applicability:
    victim = _root(family)
    if not getattr(victim, "parts", ()):
        return Applicability.no("no_file_parts", "a duplicate of nothing is nothing")
    if _title_year_twins(ctx, victim):
        return Applicability.no(
            "already_duplicated",
            "this title already has a twin in the library; the case would not be one atomic repair",
        )
    return Applicability.yes()


@corruption(
    ProblemClass.DUPLICATE_QUALITY,
    applies_to={MediaKind.MOVIE},
    variants=tuple(DUPLICATE_VARIANTS),
    witness=WitnessKind.RELATION,
    tier=WitnessTier.LOCAL,
    applicable=_duplicate_applicable,
)
def duplicate_quality(family: export_module.Family, ctx: CorruptionContext) -> Mutation | Rejection:
    """Clone a film as a second library entry at a different quality."""
    victim = _root(family)
    recipe = DUPLICATE_VARIANTS[ctx.variant]
    marker = str(recipe["marker"])

    document = dump_item(victim)
    document["item_id"] = {
        "provider": victim.item_id.provider,
        "section_id": victim.item_id.section_id,
        "rating_key": ctx.mint(0),
    }
    stem = f"{victim.title} ({getattr(victim, 'year', '')}) [{marker}]".replace("()", "").strip()
    cloned_parts = []
    for part in document["parts"]:
        container = recipe.get("container") or part["container"] or "mkv"
        directory = part["path"].rsplit("/", 1)[0]
        cloned_parts.append(
            {
                **part,
                # Cleared, not copied: Plex's own element ids belong to one entry,
                # and two items claiming the same `part_id` would make the id
                # useless as the address a repair names.
                "media_id": None,
                "part_id": None,
                "path": f"{directory}/{stem}.{container}",
                **{key: value for key, value in recipe.items() if key != "marker"},
            }
        )
    document["parts"] = cloned_parts
    from shelfwarden.models.item import load_item

    clone = load_item(document)
    mutated = (*family.records, clone)

    title_support = compare_title(clone.title, victim.title)
    year_support, _ = compare_year(getattr(clone, "year", None), getattr(victim, "year", None))
    witness = LocalWitness.over(ctx.export_id, mutated).relation(
        subject_ids=(str(victim.item_id), str(clone.item_id)),
        relation="same_work",
        pointers=("/title", "/title"),
        comparator="compare_title+compare_year",
        supports=(title_support, year_support),
        policy=ctx.policy,
    )
    return Mutation(items=mutated, witness=witness)


# -- filename_unmatchable -------------------------------------------------


def _scene_name(ctx: CorruptionContext, title: str, year: int | None, marker: str | None) -> str:
    tags = ctx.rng.choice(SCENE_TAGS)
    group = ctx.rng.choice(SCENE_GROUPS)
    words = [part for part in fold_text(title).title().split(" ") if part]
    pieces = [".".join(words)]
    if marker:
        pieces.append(marker)
    if year is not None:
        pieces.append(str(year))
    pieces.extend(tags)
    return f"{'.'.join(pieces)}-{group}"


def _unmatchable_target(family: export_module.Family) -> NormalizedItem | None:
    """The item whose files get renamed: the root, or its first episode."""
    root = family.records[0]
    if getattr(root, "parts", ()):
        return root
    for record in family.records:
        if record.media_kind is MediaKind.EPISODE and getattr(record, "parts", ()):
            return record
    return None


def _unmatchable_applicable(family: export_module.Family, ctx: CorruptionContext) -> Applicability:
    target = _unmatchable_target(family)
    if target is None:
        return Applicability.no("no_file_parts", "nothing to rename")
    if not target.title:
        return Applicability.no("no_title", "no true title for the scene name to encode")
    return Applicability.yes()


@corruption(
    ProblemClass.FILENAME_UNMATCHABLE,
    applies_to={MediaKind.MOVIE, MediaKind.SHOW},
    variants=("scene_release",),
    witness=WitnessKind.VALUE,
    tier=WitnessTier.LOCAL,
    applicable=_unmatchable_applicable,
)
def filename_unmatchable(
    family: export_module.Family, ctx: CorruptionContext
) -> Mutation | Rejection:
    """Rename the files to a scene release and let the metadata fall back to the stem.

    The name written is genuinely parseable. The `opaque_hash` variant -- a name
    carrying no signal at all -- is excluded on purpose: it is not a hard case but
    an unsolvable one, and a dataset full of them measures nothing while looking
    rigorous.
    """
    target = _unmatchable_target(family)
    if target is None:  # pragma: no cover -- applicability already checked
        return ctx.reject("no_file_parts")

    marker = None
    if target.media_kind is MediaKind.EPISODE:
        season, episode = target.parent_index, target.index
        if season is None or episode is None:
            return ctx.reject("no_episode_numbering", "an episode with no numbering to encode")
        marker = f"S{season:02d}E{episode:02d}"

    stem = _scene_name(ctx, target.title, getattr(target, "year", None), marker)
    document = dump_item(target)
    document["parts"] = [
        {**part, "path": f"{part['path'].rsplit('/', 1)[0]}/{stem}.{part['container'] or 'mkv'}"}
        for part in document["parts"]
    ]
    document["title"] = stem
    document["guids"] = []
    from shelfwarden.models.item import load_item

    corrupted_item = load_item(document)
    mutated = tuple(
        corrupted_item if str(record.item_id) == str(target.item_id) else record
        for record in family.records
    )

    parsed = parse_release_path(corrupted_item.parts[0].path)
    witness = LocalWitness.over(ctx.export_id, mutated).value(
        subject_id=str(target.item_id),
        pointer="/parts/0/path",
        comparator="compare_title",
        resolved=parsed.title,
        against_truth=compare_title(parsed.title, target.title),
        against_corrupted=compare_title(parsed.title, stem),
        policy=ctx.policy,
    )
    return Mutation(items=mutated, witness=witness)


__all__ = [
    "DUPLICATE_VARIANTS",
    "EDITION_MARKERS",
    "SCENE_GROUPS",
    "SCENE_TAGS",
    "alternate_cut",
    "duplicate_quality",
    "filename_unmatchable",
    "wrong_match",
    "year_collision_remake",
]
