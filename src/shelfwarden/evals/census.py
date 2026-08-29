"""What the library actually contains.

The census exists because `implementation-plan.md` §8 says the slice composition
must be chosen from evidence: "the 200-item slice may contain very few shows with
absolute numbering, or no remake pairs." Guessing at `composition.toml` and
discovering the gap in step 0.6 costs a regeneration; counting first does not.

Two tiers, each labelled with its own basis, because conflating them makes the
numbers unfalsifiable:

* **population** -- exact, from the listing walk. Every root item in every
  supported section.
* **exported** -- from the records actually fetched, since guids, containers and
  lock state need a full item. Carries its own `coverage`.

Every list here is sorted by an explicit total order before it is written.
Sorting is not cosmetic: `Counter.most_common()` breaks ties by insertion order,
which is hash-seed dependent, and two developers' exports would differ for no
visible reason.

The mirror-image trap is that `canonical_json` *also* sorts mapping keys. That
makes the serialized form stable, and it means a count-descending mapping does
not survive the round trip: `census.json` hands its keys back alphabetically.
Ordering that a reader is supposed to see therefore has to be re-derived when the
markdown is rendered, not inherited from the dict. See `by_count`.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from shelfwarden.models.ids import IdNamespace, ItemId
from shelfwarden.models.item import (
    AudiobookItem,
    MediaKind,
    MovieItem,
    NormalizedItem,
    SectionRef,
)

# How many example strings any block may carry. Anything dropped is counted --
# a census that silently truncates reads as complete coverage, which is the
# "no silent caps" house rule applied to the code most likely to break it.
EXAMPLE_CAP = 5

# Above this, a season is more plausibly absolute numbering than a real season.
ABSOLUTE_NUMBERING_EPISODES = 30


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Coverage(_Frozen):
    """How much of the library a tier actually saw."""

    records: int
    population: int


class SectionCensus(_Frozen):
    section_id: str
    title: str
    section_type: str
    agent: str
    population: int
    exported_roots: int = 0
    exported_records: int = 0


class AgentCensus(_Frozen):
    sections: int
    population: int


class NamespaceCensus(_Frozen):
    """One guid namespace, with worked examples where they are informative."""

    items: int
    ids: int
    distinct_forms: int = 0
    examples: tuple[str, ...] = ()
    examples_truncated: bool = False
    examples_dropped: int = 0


class ExampleSet(_Frozen):
    count: int
    by_media_kind: dict[str, int] = {}
    examples: tuple[str, ...] = ()
    examples_truncated: bool = False
    examples_dropped: int = 0


class Presence(_Frozen):
    present: int
    absent: int


class ReadinessRow(_Frozen):
    """An advisory count of items structurally eligible for one problem class.

    `advisory` is `True` on every row and is not a field anyone should ever set to
    `False` here. This counts *candidates*; it does not verify that any item is
    free of a problem. That verification is the mechanical screen in step 0.45, it
    needs comparators that do not exist yet, and reading a readiness count as a
    `no_action` label would make the should-not-touch slice unfalsifiable -- the
    exact defect recorded as Defect 3 in `implementation-plan.md` §3.
    """

    problem_class: str
    eligible: int
    basis: str
    advisory: bool = True


class PopulationCensus(_Frozen):
    sections: tuple[SectionCensus, ...]
    by_media_kind: dict[str, int]
    by_agent: dict[str, AgentCensus]


class ExportedCensus(_Frozen):
    coverage: Coverage
    by_media_kind: dict[str, int]
    guid_namespaces: dict[str, NamespaceCensus]
    items_without_guids: ExampleSet
    containers: dict[str, int]
    video_resolutions: dict[str, int]
    locked_fields: dict[str, int]
    field_presence: dict[str, Presence]


class Census(_Frozen):
    schema_version: int = 1
    population: PopulationCensus
    # None under --census-only: the slice tier needs full items, and reporting it
    # as a block of zeroes would read as "the library has no guids".
    exported: ExportedCensus | None = None
    readiness: tuple[ReadinessRow, ...] = ()


# -- the index ------------------------------------------------------------


@dataclass(slots=True)
class ExportIndex:
    """Exported records, grouped the handful of ways the counters need."""

    items: tuple[NormalizedItem, ...]
    by_kind: dict[MediaKind, list[NormalizedItem]] = field(default_factory=dict)
    children: dict[str, list[NormalizedItem]] = field(default_factory=dict)

    @classmethod
    def build(cls, items: tuple[NormalizedItem, ...]) -> "ExportIndex":
        by_kind: dict[MediaKind, list[NormalizedItem]] = defaultdict(list)
        children: dict[str, list[NormalizedItem]] = defaultdict(list)
        for item in items:
            by_kind[item.media_kind].append(item)
            parent = getattr(item, "parent", None)
            if parent is not None:
                children[str(parent)].append(item)
        return cls(items=items, by_kind=dict(by_kind), children=dict(children))

    def of_kind(self, kind: MediaKind) -> list[NormalizedItem]:
        return self.by_kind.get(kind, [])

    def children_of(self, item_id: ItemId) -> list[NormalizedItem]:
        return self.children.get(str(item_id), [])


def _sorted_counts(counter: Counter[str]) -> dict[str, int]:
    """Count descending, then key ascending. Never `most_common()`."""
    return {key: count for key, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))}


def _examples(values: set[str]) -> tuple[tuple[str, ...], bool, int]:
    """Cap a set of example strings, reporting what the cap dropped.

    Sorted *before* capping, so which examples survive is deterministic rather
    than a function of iteration order.
    """
    ordered = sorted(values)
    kept = tuple(ordered[:EXAMPLE_CAP])
    dropped = len(ordered) - len(kept)
    return kept, dropped > 0, dropped


# -- the tiers ------------------------------------------------------------


def population_census(
    sections: tuple[SectionRef, ...],
    populations: dict[str, int],
    root_kinds: dict[str, MediaKind],
    exported_roots: dict[str, int] | None = None,
    exported_records: dict[str, int] | None = None,
) -> PopulationCensus:
    """Exact counts over every supported section."""
    exported_roots = exported_roots or {}
    exported_records = exported_records or {}

    rows = tuple(
        SectionCensus(
            section_id=section.section_id,
            title=section.title,
            section_type=section.section_type,
            agent=section.agent,
            population=populations.get(section.section_id, 0),
            exported_roots=exported_roots.get(section.section_id, 0),
            exported_records=exported_records.get(section.section_id, 0),
        )
        for section in sections
    )
    rows = tuple(sorted(rows, key=lambda row: section_sort_key(row.section_id)))

    by_kind: Counter[str] = Counter()
    agents: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for section in sections:
        population = populations.get(section.section_id, 0)
        by_kind[str(root_kinds[section.section_id])] += population
        bucket = agents[section.agent or "<none>"]
        bucket[0] += 1
        bucket[1] += population

    return PopulationCensus(
        sections=rows,
        by_media_kind=_sorted_counts(by_kind),
        by_agent={
            name: AgentCensus(sections=counts[0], population=counts[1])
            for name, counts in sorted(agents.items())
        },
    )


def exported_census(items: tuple[NormalizedItem, ...], population: int) -> ExportedCensus:
    """Everything that needs a full item rather than a listing stub."""
    by_kind: Counter[str] = Counter()
    containers: Counter[str] = Counter()
    resolutions: Counter[str] = Counter()
    locks: Counter[str] = Counter()

    namespace_items: Counter[str] = Counter()
    namespace_ids: Counter[str] = Counter()
    namespace_forms: dict[str, set[str]] = defaultdict(set)

    no_guid_kinds: Counter[str] = Counter()
    no_guid_examples: set[str] = set()
    summary_present = 0
    thumb_present = 0
    art_present = 0

    for item in items:
        by_kind[str(item.media_kind)] += 1
        for name in item.locked_fields:
            locks[name] += 1
        if item.summary:
            summary_present += 1
        if item.has_thumb:
            thumb_present += 1
        if item.has_art:
            art_present += 1

        for part in getattr(item, "parts", ()):
            if part.container:
                containers[part.container.lower()] += 1
            if part.video_resolution:
                resolutions[part.video_resolution.lower()] += 1

        seen_namespaces: set[str] = set()
        for external in item.guids:
            namespace = str(external.namespace)
            namespace_ids[namespace] += 1
            seen_namespaces.add(namespace)
            if external.namespace is IdNamespace.UNKNOWN:
                namespace_forms[namespace].add(external.raw)
        for namespace in seen_namespaces:
            namespace_items[namespace] += 1

        if not item.guids:
            no_guid_kinds[str(item.media_kind)] += 1
            no_guid_examples.add(str(item.item_id))

    namespaces: dict[str, NamespaceCensus] = {}
    for namespace in sorted(namespace_ids):
        forms = namespace_forms.get(namespace, set())
        examples, truncated, dropped = _examples(forms)
        namespaces[namespace] = NamespaceCensus(
            items=namespace_items[namespace],
            ids=namespace_ids[namespace],
            distinct_forms=len(forms),
            examples=examples,
            examples_truncated=truncated,
            examples_dropped=dropped,
        )

    missing_examples, missing_truncated, missing_dropped = _examples(no_guid_examples)
    total = len(items)
    return ExportedCensus(
        coverage=Coverage(records=total, population=population),
        by_media_kind=_sorted_counts(by_kind),
        guid_namespaces=namespaces,
        items_without_guids=ExampleSet(
            count=sum(no_guid_kinds.values()),
            by_media_kind=_sorted_counts(no_guid_kinds),
            examples=missing_examples,
            examples_truncated=missing_truncated,
            examples_dropped=missing_dropped,
        ),
        containers=_sorted_counts(containers),
        video_resolutions=_sorted_counts(resolutions),
        locked_fields=_sorted_counts(locks),
        field_presence={
            "summary": Presence(present=summary_present, absent=total - summary_present),
            "has_thumb": Presence(present=thumb_present, absent=total - thumb_present),
            "has_art": Presence(present=art_present, absent=total - art_present),
        },
    )


# -- readiness (advisory) -------------------------------------------------


def _advisory_title_key(item: NormalizedItem) -> str:
    """A deliberately crude title key.

    Casefold and collapse whitespace, nothing more. The real comparators -- alias
    sets, normalization, `SupportStrength` -- land in step 0.45 and this must not
    be mistaken for them. It is here to answer "are there plausibly any remake
    pairs at all", not to decide a match.
    """
    return " ".join(item.title.casefold().split())


def _resolvable(item: NormalizedItem) -> bool:
    return any(
        external.namespace not in (IdNamespace.UNKNOWN, IdNamespace.LOCAL, IdNamespace.PLEX)
        for external in item.guids
    )


def _title_year_groups(index: ExportIndex, kind: MediaKind) -> list[list[NormalizedItem]]:
    groups: dict[tuple[str, str, int | None], list[NormalizedItem]] = defaultdict(list)
    for item in index.of_kind(kind):
        year = getattr(item, "year", None)
        groups[(item.item_id.section_id, _advisory_title_key(item), year)].append(item)
    return [members for members in groups.values() if len(members) > 1]


def _same_title_different_year(index: ExportIndex) -> int:
    by_title: dict[tuple[str, str], set[int]] = defaultdict(set)
    for item in index.of_kind(MediaKind.MOVIE):
        if isinstance(item, MovieItem) and item.year is not None:
            by_title[(item.item_id.section_id, _advisory_title_key(item))].add(item.year)
    return sum(1 for years in by_title.values() if len(years) > 1)


def _shows_with_multiple_seasons(index: ExportIndex) -> int:
    return sum(
        1
        for show in index.of_kind(MediaKind.SHOW)
        if len(index.children_of(show.item_id)) > 1
        and any(index.children_of(season.item_id) for season in index.children_of(show.item_id))
    )


def _long_seasons(index: ExportIndex) -> int:
    return sum(
        1
        for season in index.of_kind(MediaKind.SEASON)
        if len(index.children_of(season.item_id)) > ABSOLUTE_NUMBERING_EPISODES
    )


def _authors_with_multiple_books(index: ExportIndex) -> int:
    return sum(
        1
        for author in index.of_kind(MediaKind.AUTHOR)
        if len(index.children_of(author.item_id)) > 1
    )


def _multi_part_books(index: ExportIndex) -> int:
    count = 0
    for book in index.of_kind(MediaKind.AUDIOBOOK):
        parts = len(index.children_of(book.item_id))
        declared = book.part_count if isinstance(book, AudiobookItem) else None
        if parts > 1 or (declared or 0) > 1:
            count += 1
    return count


def _books_with_series(index: ExportIndex) -> int:
    return sum(
        1
        for book in index.of_kind(MediaKind.AUDIOBOOK)
        if isinstance(book, AudiobookItem) and (book.series or book.index is not None)
    )


def _with_files(index: ExportIndex) -> int:
    return sum(1 for item in index.items if getattr(item, "parts", ()))


def _with_summary(index: ExportIndex) -> int:
    return sum(1 for item in index.items if item.summary)


def _with_edition(index: ExportIndex) -> int:
    return sum(
        1
        for item in index.of_kind(MediaKind.MOVIE)
        if isinstance(item, MovieItem) and item.edition_title
    )


# Declared as data, in a fixed order, so the rendered table does not reorder
# between runs and a reader can diff two censuses line by line.
READINESS_RULES: tuple[tuple[str, str], ...] = (
    ("wrong_match", "movies/shows with a resolvable external id to swap"),
    ("year_collision_remake", "same crude title key, different year, one section"),
    ("foreign_title_variant", "upper bound: movies with a resolvable id (needs TMDB)"),
    ("alternate_cut", "movies carrying an edition_title"),
    ("missing_metadata", "items with a non-empty summary to null out"),
    ("duplicate_quality", "groups sharing a crude (title, year) key in one section"),
    ("episode_wrong_season", "shows with >1 exported season and episodes under them"),
    ("absolute_vs_seasonal", f"seasons with >{ABSOLUTE_NUMBERING_EPISODES} episodes"),
    ("filename_unmatchable", "items with at least one file part"),
    ("series_order_broken", "audiobooks carrying a series name or an index"),
    ("author_name_variant", "authors with >1 exported book"),
    ("narrator_as_author", "authors with >1 exported book"),
    ("multi_file_split", "audiobooks with >1 part"),
    ("missing_series", "audiobooks carrying a series name or an index"),
    ("anthology_omnibus", "not structurally detectable from an export; curate by hand"),
)


def readiness(index: ExportIndex) -> tuple[ReadinessRow, ...]:
    """Advisory per-class candidate counts. See `ReadinessRow`."""
    counts: dict[str, int] = {
        "wrong_match": sum(
            1
            for item in (*index.of_kind(MediaKind.MOVIE), *index.of_kind(MediaKind.SHOW))
            if _resolvable(item)
        ),
        "year_collision_remake": _same_title_different_year(index),
        "foreign_title_variant": sum(
            1 for item in index.of_kind(MediaKind.MOVIE) if _resolvable(item)
        ),
        "alternate_cut": _with_edition(index),
        "missing_metadata": _with_summary(index),
        "duplicate_quality": len(_title_year_groups(index, MediaKind.MOVIE)),
        "episode_wrong_season": _shows_with_multiple_seasons(index),
        "absolute_vs_seasonal": _long_seasons(index),
        "filename_unmatchable": _with_files(index),
        "series_order_broken": _books_with_series(index),
        "author_name_variant": _authors_with_multiple_books(index),
        "narrator_as_author": _authors_with_multiple_books(index),
        "multi_file_split": _multi_part_books(index),
        "missing_series": _books_with_series(index),
        "anthology_omnibus": 0,
    }
    return tuple(
        ReadinessRow(problem_class=name, eligible=counts[name], basis=basis)
        for name, basis in READINESS_RULES
    )


def build(
    sections: tuple[SectionRef, ...],
    populations: dict[str, int],
    root_kinds: dict[str, MediaKind],
    items: tuple[NormalizedItem, ...] | None,
    exported_roots: dict[str, int] | None = None,
    exported_records: dict[str, int] | None = None,
) -> Census:
    """Assemble both tiers. `items=None` produces the population tier alone."""
    population = population_census(
        sections, populations, root_kinds, exported_roots, exported_records
    )
    if items is None:
        return Census(population=population)

    index = ExportIndex.build(items)
    return Census(
        population=population,
        exported=exported_census(items, sum(populations.values())),
        readiness=readiness(index),
    )


# -- rendering ------------------------------------------------------------


def by_count(counts: dict[str, int]) -> list[tuple[str, int]]:
    """Count descending, then key ascending -- re-derived, never assumed.

    `_sorted_counts` builds these mappings in this order, but a *mapping* cannot
    carry it across the wire: `canonical_json` sorts object keys, so a census
    reloaded from `census.json` hands them back alphabetically. Rendering from the
    dict's own order would therefore produce one table on the way out and a
    differently ordered one from the stored file -- which is precisely the
    "renders from `census.json` alone" guarantee the markdown is supposed to make.

    Sorting here rather than switching the schema to lists keeps the stored shape
    the readable one (`{"mkv": 96}`) and puts the ordering where it is actually
    needed.
    """
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    if not rows:
        return ["_(none)_", ""]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    lines.append("")
    return lines


def render_markdown(census: Census) -> str:
    """The table a human reads before editing `composition.toml`.

    Renders from the census alone, so a stored `census.json` stays readable
    without the export that produced it.
    """
    out: list[str] = ["# Library census", ""]

    out += ["## Sections (population — exact)", ""]
    out += _table(
        ("section", "id", "type", "agent", "population", "exported roots", "exported records"),
        [
            (
                row.title,
                row.section_id,
                row.section_type,
                row.agent or "—",
                str(row.population),
                str(row.exported_roots),
                str(row.exported_records),
            )
            for row in census.population.sections
        ],
    )

    out += ["## Root media kinds (population)", ""]
    out += _table(
        ("media kind", "population"),
        [(kind, str(count)) for kind, count in by_count(census.population.by_media_kind)],
    )

    out += ["## Agents (population)", ""]
    out += _table(
        ("agent", "sections", "population"),
        [
            (agent, str(row.sections), str(row.population))
            for agent, row in census.population.by_agent.items()
        ],
    )

    exported = census.exported
    if exported is None:
        out += [
            "> Population tier only (`--census-only`). Guid namespaces, containers,",
            "> and lock state need full items — run a real export for those.",
            "",
        ]
        return "\n".join(out) + "\n"

    coverage = exported.coverage
    out += [
        "## Exported slice",
        "",
        f"{coverage.records} records over a population of {coverage.population} root items.",
        "Everything below is scoped to the slice, not to the library.",
        "",
    ]
    out += _table(
        ("media kind", "records"),
        [(kind, str(count)) for kind, count in by_count(exported.by_media_kind)],
    )

    out += ["### Guid namespaces", ""]
    out += _table(
        ("namespace", "items", "ids", "distinct forms", "examples"),
        [
            (
                namespace,
                str(row.items),
                str(row.ids),
                str(row.distinct_forms) if row.distinct_forms else "—",
                (
                    ", ".join(f"`{example}`" for example in row.examples)
                    + (f" (+{row.examples_dropped} more)" if row.examples_truncated else "")
                )
                or "—",
            )
            for namespace, row in exported.guid_namespaces.items()
        ],
    )
    missing = exported.items_without_guids
    out += [
        f"{missing.count} exported item(s) carry no guid at all"
        + (f" — e.g. {', '.join(f'`{e}`' for e in missing.examples)}" if missing.examples else "")
        + (f" (+{missing.examples_dropped} more)" if missing.examples_truncated else "")
        + ".",
        "",
        # Broken down, because the headline number is normally dominated by kinds
        # that never carry a guid in the first place -- seasons, episodes,
        # audiobook parts. Undifferentiated it reads as a coverage gap.
        "Only movie, show, author and audiobook rows here are findings; seasons,",
        "episodes and audiobook parts legitimately carry no guid.",
        "",
    ]
    out += _table(
        ("media kind", "items with no guid"),
        [(kind, str(count)) for kind, count in by_count(missing.by_media_kind)],
    )

    out += ["### Containers and resolutions", ""]
    out += _table(
        ("container", "parts"),
        [(name, str(count)) for name, count in by_count(exported.containers)],
    )
    out += _table(
        ("resolution", "parts"),
        [(name, str(count)) for name, count in by_count(exported.video_resolutions)],
    )

    out += ["### Locked fields", ""]
    out += _table(
        ("field", "items"),
        [(name, str(count)) for name, count in by_count(exported.locked_fields)],
    )

    out += ["### Field presence", ""]
    out += _table(
        ("field", "present", "absent"),
        [
            (name, str(row.present), str(row.absent))
            for name, row in sorted(exported.field_presence.items())
        ],
    )

    out += [
        "## Slice readiness (advisory)",
        "",
        "Structural candidate counts, **not** verification that an item is problem-free.",
        "The mechanical screen in step 0.45 is what produces `guarded_classes`; reading",
        "a number here as a `no_action` label would make the should-not-touch slice",
        "unfalsifiable.",
        "",
    ]
    out += _table(
        ("problem class", "eligible", "basis"),
        [(row.problem_class, str(row.eligible), row.basis) for row in census.readiness],
    )
    return "\n".join(out) + "\n"


def section_sort_key(section_id: str) -> tuple[int, int, str]:
    """Numeric section ids sort numerically; anything else sorts after, by text."""
    return (0, int(section_id), "") if section_id.isdigit() else (1, 0, section_id)
