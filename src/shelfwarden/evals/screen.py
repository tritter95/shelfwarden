"""The mechanical screen: which problems has an item been *verified* not to have?

This is the fix for Defect 3 in `implementation-plan.md` §3. "This item has no
problems" is an open-world claim and cannot be verified; "this item does not
have problem P" can be. The should-not-touch slice is 15% of the eval dataset
and is the denominator of the headline false-positive metric, so if its members
are chosen by assumption then `fp_rate_snt` is unfalsifiable -- and worse, the
project starts scoring true detections as false positives, which trains it to
suppress exactly the behavior it exists to produce.

The screen is **LLM-free**. It runs eleven predicates over an export using the
comparators in `shelfwarden.compare` -- the same ones the scorer (0.8) and the
validator (1.4) use, so the screen and the scorer cannot disagree about what
"the same title" means.

Four statuses, not two. `not_applicable` ("a movie has no season") and
`unavailable` ("nobody has asked TMDB yet") are different facts, and **neither
counts as a pass**. That single rule is what lets the authority tier ship as a
protocol with no implementation and no conditional logic anywhere: with
`NullAuthority`, six of the eleven predicates report `unavailable` and the
classes they guard simply stay unguarded. See `AuthorityIndex`.

Three verdicts, not two. `insufficient` -- fewer than `MIN_APPLICABLE_CHECKS`
applicable predicates -- is neither a guarded item nor a candidate; it is a
coverage metric on the screen itself, and it is the number to read first.

Two boundaries worth stating out loud:

* **The export directory is never written to.** Its byte-identity is 0.4's gate,
  and adding a file to it would make that assertion answer a question nobody
  asked. A screen lands in `datasets/screens/<export_id>/` and is bound to its
  source by `items_sha256`; loading one against a different export raises,
  because a guarded label carried onto another export is a wrong label with a
  plausible provenance.
* **Uniqueness predicates read `roots.jsonl`, never `items.jsonl`.** An absence
  claim scoped to a slice is not an absence claim about the library. See
  `PopulationIndex`.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from shelfwarden import __version__
from shelfwarden.canonical import canonical_json
from shelfwarden.compare import (
    SCREEN_POLICY,
    STRENGTH_RANK,
    Policy,
    Support,
    SupportStrength,
    compare_person_name,
    compare_series_position,
    compare_title,
    compare_year,
    fold_text,
    has_resolvable_id,
    name_tokens,
    parse_release_name,
)
from shelfwarden.evals import export as export_module
from shelfwarden.models.evidence import Source, evidence_id
from shelfwarden.models.finding import ProblemClass
from shelfwarden.models.ids import IdNamespace
from shelfwarden.models.item import (
    AudiobookItem,
    EpisodeItem,
    FetchProfile,
    ItemStub,
    MediaKind,
    NormalizedItem,
    dump_item,
)

SCREEN_FILE = "screen.json"
SCREEN_MARKDOWN_FILE = "screen.md"
SCHEMA_VERSION = 1
DEFAULT_SCREEN_ROOT = Path("datasets/screens")

# How many applicable checks an item needs before "guarded" means anything.
# A constant, deliberately with no CLI flag: invariant 1 says a rule expressible
# as a predicate lives in code, and a `--min-checks` flag would make the
# should-not-touch slice's own admission standard a runtime argument -- which
# would be lowered the first time the slice came out small. The value is
# recorded in `screen.json` so a stored screen states its own standard.
MIN_APPLICABLE_CHECKS = 3

# The one float threshold in the whole step is the author-twin fuzzy floor, so
# it is published as a sweep rather than chosen. This is `auto_apply_rate(t)` as
# a sweep instead of a point, applied one phase early.
FUZZY_SWEEP: tuple[float, ...] = (0.80, 0.85, 0.90, 0.95)

# How many example ids any list in the report carries. Whatever it drops, it
# counts (house rule 12).
EXAMPLE_CAP = 5

NO_AUTHORITY = "none"


class ScreenError(Exception):
    """The screen could not run. Every message names a concrete next action."""


# -- vocabulary -----------------------------------------------------------


class CheckStatus(StrEnum):
    """What happened when a predicate was run against an item.

    `NOT_APPLICABLE` and `UNAVAILABLE` are distinct and neither is `PASS`.
    Collapsing them makes "we have no TMDB record" indistinguishable from
    "movies have no season", and the screen's own coverage becomes
    unmeasurable -- the same defect `FetchProfile` was added to step 0.2 to
    prevent, one layer up.
    """

    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"


class Predicate(StrEnum):
    """The eleven checks, from `implementation-plan.md` §3."""

    RESOLVABLE_ID_PRESENT = "resolvable_id_present"
    SUMMARY_PRESENT = "summary_present"
    SINGLE_PART = "single_part"
    SEASON_MEMBERSHIP_COHERENT = "season_membership_coherent"
    EPISODE_NUMBERING_CONTIGUOUS = "episode_numbering_contiguous"
    NO_TITLE_YEAR_TWIN = "no_title_year_twin"
    NO_AUTHOR_NAME_TWIN = "no_author_name_twin"
    FILENAME_MATCHES_METADATA = "filename_matches_metadata"
    TITLE_MATCHES_AUTHORITY = "title_matches_authority"
    YEAR_MATCHES_AUTHORITY = "year_matches_authority"
    SERIES_POSITION_MATCHES_AUTHORITY = "series_position_matches_authority"


class Tier(StrEnum):
    LOCAL = "local"
    AUTHORITY = "authority"


class Scope(StrEnum):
    ITEM = "item"
    FAMILY = "family"
    POPULATION = "population"


class Verdict(StrEnum):
    GUARDED = "guarded"
    FAILED = "failed"
    INSUFFICIENT = "insufficient"


PREDICATE_TIER: dict[Predicate, Tier] = {
    Predicate.RESOLVABLE_ID_PRESENT: Tier.LOCAL,
    Predicate.SUMMARY_PRESENT: Tier.LOCAL,
    Predicate.SINGLE_PART: Tier.LOCAL,
    Predicate.SEASON_MEMBERSHIP_COHERENT: Tier.LOCAL,
    Predicate.EPISODE_NUMBERING_CONTIGUOUS: Tier.LOCAL,
    Predicate.NO_TITLE_YEAR_TWIN: Tier.LOCAL,
    Predicate.NO_AUTHOR_NAME_TWIN: Tier.LOCAL,
    Predicate.FILENAME_MATCHES_METADATA: Tier.LOCAL,
    Predicate.TITLE_MATCHES_AUTHORITY: Tier.AUTHORITY,
    Predicate.YEAR_MATCHES_AUTHORITY: Tier.AUTHORITY,
    Predicate.SERIES_POSITION_MATCHES_AUTHORITY: Tier.AUTHORITY,
}

PREDICATE_SCOPE: dict[Predicate, Scope] = {
    Predicate.RESOLVABLE_ID_PRESENT: Scope.ITEM,
    Predicate.SUMMARY_PRESENT: Scope.ITEM,
    Predicate.SINGLE_PART: Scope.ITEM,
    Predicate.SEASON_MEMBERSHIP_COHERENT: Scope.FAMILY,
    Predicate.EPISODE_NUMBERING_CONTIGUOUS: Scope.FAMILY,
    Predicate.NO_TITLE_YEAR_TWIN: Scope.POPULATION,
    Predicate.NO_AUTHOR_NAME_TWIN: Scope.POPULATION,
    Predicate.FILENAME_MATCHES_METADATA: Scope.ITEM,
    Predicate.TITLE_MATCHES_AUTHORITY: Scope.ITEM,
    Predicate.YEAR_MATCHES_AUTHORITY: Scope.ITEM,
    Predicate.SERIES_POSITION_MATCHES_AUTHORITY: Scope.ITEM,
}

# Which kinds each predicate is *about*. Anything else is `not_applicable`,
# which is a fact about the media kind and never about our coverage.
PREDICATE_KINDS: dict[Predicate, frozenset[MediaKind]] = {
    Predicate.RESOLVABLE_ID_PRESENT: frozenset(
        {MediaKind.MOVIE, MediaKind.SHOW, MediaKind.EPISODE, MediaKind.AUDIOBOOK}
    ),
    Predicate.SUMMARY_PRESENT: frozenset(
        {MediaKind.MOVIE, MediaKind.SHOW, MediaKind.EPISODE, MediaKind.AUDIOBOOK}
    ),
    Predicate.SINGLE_PART: frozenset({MediaKind.AUDIOBOOK}),
    Predicate.SEASON_MEMBERSHIP_COHERENT: frozenset(
        {MediaKind.SHOW, MediaKind.SEASON, MediaKind.EPISODE}
    ),
    Predicate.EPISODE_NUMBERING_CONTIGUOUS: frozenset({MediaKind.SHOW, MediaKind.SEASON}),
    Predicate.NO_TITLE_YEAR_TWIN: frozenset({MediaKind.MOVIE, MediaKind.SHOW}),
    Predicate.NO_AUTHOR_NAME_TWIN: frozenset({MediaKind.AUTHOR}),
    Predicate.FILENAME_MATCHES_METADATA: frozenset({MediaKind.MOVIE, MediaKind.EPISODE}),
    Predicate.TITLE_MATCHES_AUTHORITY: frozenset(
        {MediaKind.MOVIE, MediaKind.SHOW, MediaKind.EPISODE, MediaKind.AUDIOBOOK}
    ),
    Predicate.YEAR_MATCHES_AUTHORITY: frozenset({MediaKind.MOVIE, MediaKind.SHOW}),
    Predicate.SERIES_POSITION_MATCHES_AUTHORITY: frozenset({MediaKind.AUDIOBOOK}),
}

# Which predicates verify which problem class. A class is guarded on an item iff
# its guard set is non-empty and **every** predicate in it passed on that item.
#
# Two classes map to the empty set, and the difference between them matters:
# `anthology_omnibus` is unguardable by mechanism and always will be, while
# `alternate_cut` simply has no predicate yet. `UNGUARDABLE_REASON` records
# which is which so a reader is not left to guess.
GUARD_TABLE: dict[ProblemClass, frozenset[Predicate]] = {
    ProblemClass.WRONG_MATCH: frozenset(
        {Predicate.RESOLVABLE_ID_PRESENT, Predicate.TITLE_MATCHES_AUTHORITY}
    ),
    ProblemClass.YEAR_COLLISION_REMAKE: frozenset({Predicate.YEAR_MATCHES_AUTHORITY}),
    ProblemClass.FOREIGN_TITLE_VARIANT: frozenset({Predicate.TITLE_MATCHES_AUTHORITY}),
    ProblemClass.ALTERNATE_CUT: frozenset(),
    ProblemClass.MISSING_METADATA: frozenset({Predicate.SUMMARY_PRESENT}),
    ProblemClass.DUPLICATE_QUALITY: frozenset({Predicate.NO_TITLE_YEAR_TWIN}),
    # `season_membership_coherent` alone does NOT guard this class, which step 0.5
    # discovered by building the corruption. The realistic mutation re-parents an
    # episode -- setting `parent`, `parent_index`, and `parent_title` together,
    # the only form Plex can represent -- and that leaves the predicate internally
    # consistent and *passing* on a plainly misfiled episode. The predicate guards
    # only the incoherent form, which a real server does not produce, so the class
    # was effectively unguarded and reported as guarded.
    #
    # `filename_matches_metadata` is what actually caught it. Adding it narrows
    # the guard to episodes whose files carry an `SxxEyy` marker -- and since that
    # predicate is not applicable to a show or a season, those items now report
    # `blocked` rather than `guarded`, which is correct: the evidence lives in the
    # episode's own filename. See docs/plans/step-0.5-corruption-functions.md,
    # Finding 5.
    ProblemClass.EPISODE_WRONG_SEASON: frozenset(
        {Predicate.SEASON_MEMBERSHIP_COHERENT, Predicate.FILENAME_MATCHES_METADATA}
    ),
    # Corrected in step 0.5, and for the same reason as `episode_wrong_season`
    # above. `episode_numbering_contiguous` does not guard this class at all:
    # real absolute numbering is S01E01..S01E52, which is perfectly contiguous, so
    # the predicate passes on exactly the shape the class is about. It catches
    # gaps and duplicate indices -- both real problems, neither this one.
    #
    # What actually catches a renumbered show is the episode's own filename still
    # saying S02E01 while the metadata says S01E13. The roadmap's note that this
    # class was "guarded weakly" by contiguity was optimistic; it was not guarded.
    ProblemClass.ABSOLUTE_VS_SEASONAL: frozenset({Predicate.FILENAME_MATCHES_METADATA}),
    ProblemClass.FILENAME_UNMATCHABLE: frozenset(
        {Predicate.RESOLVABLE_ID_PRESENT, Predicate.FILENAME_MATCHES_METADATA}
    ),
    ProblemClass.SERIES_ORDER_BROKEN: frozenset({Predicate.SERIES_POSITION_MATCHES_AUTHORITY}),
    ProblemClass.AUTHOR_NAME_VARIANT: frozenset({Predicate.NO_AUTHOR_NAME_TWIN}),
    # The Audnexus record that carries `seriesPrimary.position` carries the
    # author too, so one authority lookup guards both. Step 1.1 may split this
    # into its own predicate; until the record exists there is nothing to split.
    ProblemClass.NARRATOR_AS_AUTHOR: frozenset({Predicate.SERIES_POSITION_MATCHES_AUTHORITY}),
    ProblemClass.MULTI_FILE_SPLIT: frozenset({Predicate.SINGLE_PART}),
    ProblemClass.MISSING_SERIES: frozenset({Predicate.SERIES_POSITION_MATCHES_AUTHORITY}),
    ProblemClass.ANTHOLOGY_OMNIBUS: frozenset(),
}

UNGUARDABLE_REASON: dict[ProblemClass, str] = {
    ProblemClass.ALTERNATE_CUT: (
        "no mechanical predicate yet: edition markers are per-part and no authority "
        "enumerates cuts reliably. Revisit at step 1.1."
    ),
    ProblemClass.ANTHOLOGY_OMNIBUS: (
        "not mechanically detectable, by design: an omnibus is deliberately ambiguous "
        "and is routed to the escalate slice. Curate by hand."
    ),
}


# -- the authority seam ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthorityRecord:
    """One external record, already parsed into the fields the comparators need.

    Carries `evidence_id` so an authority-tier check cites the retrieval rather
    than asserting it, and `field_index` keys so step 1.4's *resolution* check
    can reject a pointer that is not in the index for the claimed field.
    """

    evidence_id: str
    source: Source
    title: str | None = None
    aliases: tuple[str, ...] = ()
    year: int | None = None
    summary: str | None = None
    series: str | None = None
    series_position: str | None = None
    field_index: tuple[str, ...] = ()


class AuthorityIndex(Protocol):
    """Lookup by external id, and nothing else.

    Defined in 0.45, implemented in 1.1. When a cassette- or evidence-store-backed
    implementation arrives it is passed as a constructor argument, the same run
    promotes six predicates from `unavailable` to pass/fail, and nothing in this
    module changes.
    """

    @property
    def name(self) -> str: ...

    def by_external_id(self, namespace: IdNamespace, value: str) -> AuthorityRecord | None: ...


@dataclass(frozen=True, slots=True)
class NullAuthority:
    """No authority. Every lookup returns `None`, every authority predicate is
    `unavailable` with `reason="no_authority"`, and nine of the fifteen problem
    classes stay unguarded until step 1.1 lands."""

    @property
    def name(self) -> str:
        return NO_AUTHORITY

    def by_external_id(self, namespace: IdNamespace, value: str) -> AuthorityRecord | None:
        return None


# -- the record -----------------------------------------------------------


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CheckSupport(_Frozen):
    """What a comparator said, recorded verbatim.

    `rule` is what makes step 1.4's false-rejection rate decompose per check
    rather than arriving as one number, and `matched` is what makes an alias hit
    auditable.
    """

    strength: SupportStrength
    rule: str
    score: float | None = None
    matched: str | None = None


class Check(_Frozen):
    predicate: Predicate
    tier: Tier
    scope: Scope
    status: CheckStatus
    # Present on UNAVAILABLE (why nobody could answer) and on FAIL (what broke).
    reason: str | None = None
    detail: str | None = None
    support: CheckSupport | None = None
    evidence_id: str | None = None
    # Uniqueness predicates only: how many items the absence claim was made over.
    population: int | None = None


class ItemScreen(_Frozen):
    item_id: str
    media_kind: MediaKind
    title: str
    verdict: Verdict
    applicable: int
    passed: int
    checks: tuple[Check, ...]
    guarded_classes: tuple[ProblemClass, ...]
    unguarded_classes: tuple[ProblemClass, ...]
    failing_predicates: tuple[Predicate, ...] = ()


class Candidate(_Frozen):
    """A real-slice candidate: an item the screen found something wrong with.

    Carries the failing predicate, its evidence, and the classes that predicate
    guards -- which is the ~60s-per-case adjudication form of step 0.9, already
    filled in.
    """

    item_id: str
    media_kind: MediaKind
    title: str
    failing_predicates: tuple[Predicate, ...]
    checks: tuple[Check, ...]
    proposed_problem_classes: tuple[ProblemClass, ...]


class Blocking(_Frozen):
    """How a population-scoped predicate decided which pairs to compare.

    House rule 12: if code caps, samples, or drops, it says what it dropped.
    Blocking means some pairs are never compared at all, so the scheme, the
    bucket count, and the number of pairs actually resolved are all published.
    """

    predicate: Predicate
    scheme: str
    population: int
    buckets: int
    pairs_possible: int
    pairs_resolved: int
    pairs_skipped: int
    note: str


class VerdictCounts(_Frozen):
    items: int = 0
    guarded: int = 0
    failed: int = 0
    insufficient: int = 0


class PredicateSummary(_Frozen):
    predicate: Predicate
    tier: Tier
    scope: Scope
    passed: int = 0
    failed: int = 0
    not_applicable: int = 0
    unavailable: int = 0
    reasons: dict[str, int] = {}


class GuardCoverage(_Frozen):
    """Per class, how much of the export this screen actually verified.

    Published rather than footnoted (Decision 3's obligation): with no authority
    tier the screen guards six classes of fifteen, and `fp_rate_snt` read at the
    Phase 1 gate would otherwise look like it covered all of them.
    """

    problem_class: ProblemClass
    guard_predicates: tuple[Predicate, ...]
    in_scope: int
    guarded: int
    failed: int
    blocked: int
    tier: Tier | None = None
    reason: str | None = None


class ScreenSource(_Frozen):
    """What this screen is a screen *of*. Binding, not provenance decoration."""

    export_id: str
    items_sha256: str
    export_schema_version: int
    roots_sha256: str | None = None
    population_index: bool


class ScreenCounts(_Frozen):
    items: int
    guarded: int
    failed: int
    insufficient: int
    by_media_kind: dict[str, VerdictCounts]


class Screen(_Frozen):
    """The screen document.

    Deliberately carries **no timestamp**. A screen is a pure function of the
    export and the code that read it, and byte-identity is the cheapest way to
    prove that; a `generated_at` field would force the same volatile-field
    exception list the manifest needs, to buy information the export id and the
    version already carry.
    """

    schema_version: int = SCHEMA_VERSION
    shelfwarden_version: str
    source: ScreenSource
    authority: str
    min_applicable_checks: int
    policy: str
    counts: ScreenCounts
    guard_coverage: tuple[GuardCoverage, ...]
    predicates: tuple[PredicateSummary, ...]
    blocking: tuple[Blocking, ...]
    author_fuzzy_sweep: dict[str, int]
    items: tuple[ItemScreen, ...]
    candidates: tuple[Candidate, ...]


# -- the population index -------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthorTwins:
    twins: dict[str, tuple[str, ...]]
    blocking: Blocking
    sweep: dict[str, int]


@dataclass(frozen=True, slots=True)
class PopulationIndex:
    """Every root in the library, which is what an absence claim needs.

    `items.jsonl` holds a **slice**: "no other item in this file shares this
    title and year" is not "no other item in the library does". An item whose
    duplicate simply was not sampled would be marked guarded against
    `duplicate_quality`, and the agent's correct finding on it would then score
    as a false positive -- the metric inverted, on the one class the local tier
    looked strongest at. So uniqueness reads `roots.jsonl`, and when that file
    is absent the predicates report `unavailable`, never a quiet fallback to
    slice scope.
    """

    size: int
    title_year: dict[tuple[str, str, str, str], tuple[str, ...]]
    group_sizes: dict[tuple[str, str], int]
    authors: AuthorTwins

    @classmethod
    def build(cls, roots: Sequence[ItemStub]) -> "PopulationIndex":
        title_year: dict[tuple[str, str, str, str], list[str]] = {}
        group_sizes: dict[tuple[str, str], int] = {}
        for stub in roots:
            group = (stub.item_id.section_id, str(stub.media_kind))
            group_sizes[group] = group_sizes.get(group, 0) + 1
            key = (*group, fold_text(stub.title), "" if stub.year is None else str(stub.year))
            title_year.setdefault(key, []).append(str(stub.item_id))
        authors = _author_twins([stub for stub in roots if stub.media_kind is MediaKind.AUTHOR])
        return cls(
            size=len(roots),
            title_year={key: tuple(sorted(value)) for key, value in title_year.items()},
            group_sizes=group_sizes,
            authors=authors,
        )

    def title_year_twins(self, item: NormalizedItem) -> tuple[tuple[str, ...], int]:
        group = (item.item_id.section_id, str(item.media_kind))
        year = getattr(item, "year", None)
        key = (*group, fold_text(item.title), "" if year is None else str(year))
        members = self.title_year.get(key, ())
        own = str(item.item_id)
        return tuple(member for member in members if member != own), self.group_sizes.get(group, 0)


def _author_bucket_keys(name: str) -> tuple[tuple[str, ...], ...]:
    """Two blocking keys for one name.

    The token set collides `"Sanderson, Brandon"` with `"Brandon Sanderson"`
    exactly, which is the whole variant family the class is about. The
    (initial, last token) key catches `"B. Sanderson"`, which the token set
    misses. Two variants sharing neither a token nor an initial -- a
    transliteration difference, say -- are never compared; that gap is reported
    rather than closed, and `Blocking.pairs_skipped` is where it shows up.
    """
    tokens = name_tokens(name)
    if not tokens:
        return ()
    ordered = fold_text(name).split()
    keys: list[tuple[str, ...]] = [("token_set", *tokens)]
    if ordered:
        keys.append(("initial_last", ordered[0][:1], ordered[-1]))
    return tuple(keys)


def _author_twins(authors: Sequence[ItemStub]) -> AuthorTwins:
    ordered = sorted(authors, key=lambda stub: str(stub.item_id))
    buckets: dict[tuple[str, ...], list[int]] = {}
    for index, stub in enumerate(ordered):
        for key in _author_bucket_keys(stub.title):
            buckets.setdefault(key, []).append(index)

    pairs: set[tuple[int, int]] = set()
    for members in buckets.values():
        for position, left in enumerate(members):
            for right in members[position + 1 :]:
                pairs.add((left, right) if left < right else (right, left))

    twins: dict[str, list[str]] = {}
    sweep = {f"{floor:.2f}": 0 for floor in FUZZY_SWEEP}
    for left, right in sorted(pairs):
        support = compare_person_name(ordered[left].title, ordered[right].title)
        if SCREEN_POLICY.satisfied_by(support):
            twins.setdefault(str(ordered[left].item_id), []).append(str(ordered[right].item_id))
            twins.setdefault(str(ordered[right].item_id), []).append(str(ordered[left].item_id))
        for floor in FUZZY_SWEEP:
            policy = Policy("sweep", minimum=SupportStrength.FUZZY, fuzzy_floor=floor)
            if policy.satisfied_by(support):
                sweep[f"{floor:.2f}"] += 1

    population = len(ordered)
    possible = population * (population - 1) // 2
    return AuthorTwins(
        twins={key: tuple(sorted(value)) for key, value in twins.items()},
        blocking=Blocking(
            predicate=Predicate.NO_AUTHOR_NAME_TWIN,
            scheme="token_set + (initial, last_token)",
            population=population,
            buckets=len(buckets),
            pairs_possible=possible,
            pairs_resolved=len(pairs),
            pairs_skipped=possible - len(pairs),
            note=(
                "Author names need a fuzzy comparison, which is O(n^2) unbounded, so "
                "candidates are blocked on two exact keys. Pairs sharing neither key are "
                "never compared: the count above is the size of that gap, not a claim "
                "that it is empty."
            ),
        ),
        sweep=dict(sorted(sweep.items())),
    )


# -- the run --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScreenContext:
    export_id: str
    items: tuple[NormalizedItem, ...]
    by_id: Mapping[str, NormalizedItem]
    children: Mapping[str, tuple[NormalizedItem, ...]]
    population: PopulationIndex | None
    authority: AuthorityIndex
    policy: Policy

    @classmethod
    def build(
        cls,
        export_id: str,
        items: Sequence[NormalizedItem],
        roots: Sequence[ItemStub] | None,
        authority: AuthorityIndex,
        policy: Policy = SCREEN_POLICY,
    ) -> "ScreenContext":
        children: dict[str, list[NormalizedItem]] = {}
        for item in items:
            parent = getattr(item, "parent", None)
            if parent is not None:
                children.setdefault(str(parent), []).append(item)
        return cls(
            export_id=export_id,
            items=tuple(items),
            by_id={str(item.item_id): item for item in items},
            children={key: tuple(value) for key, value in children.items()},
            population=None if roots is None else PopulationIndex.build(roots),
            authority=authority,
            policy=policy,
        )

    def children_of(self, item: NormalizedItem) -> tuple[NormalizedItem, ...]:
        return self.children.get(str(item.item_id), ())

    def episodes_under(self, item: NormalizedItem) -> tuple[NormalizedItem, ...]:
        if item.media_kind is MediaKind.SEASON:
            return self.children_of(item)
        found: list[NormalizedItem] = []
        for season in self.children_of(item):
            found.extend(self.children_of(season))
        return tuple(found)


def _support(support: Support) -> CheckSupport:
    return CheckSupport(
        strength=support.strength,
        rule=support.rule,
        score=support.score,
        matched=support.matched,
    )


def _check(
    predicate: Predicate,
    status: CheckStatus,
    *,
    reason: str | None = None,
    detail: str | None = None,
    support: Support | None = None,
    evidence: str | None = None,
    population: int | None = None,
) -> Check:
    return Check(
        predicate=predicate,
        tier=PREDICATE_TIER[predicate],
        scope=PREDICATE_SCOPE[predicate],
        status=status,
        reason=reason,
        detail=detail,
        support=None if support is None else _support(support),
        evidence_id=evidence,
        population=population,
    )


def _examples(ids: Sequence[str]) -> str:
    """A capped id list that says what it dropped."""
    kept = list(ids[:EXAMPLE_CAP])
    dropped = len(ids) - len(kept)
    rendered = ", ".join(kept)
    return f"{rendered} (+{dropped} more)" if dropped else rendered


# -- the local predicates -------------------------------------------------


def _resolvable_id_present(ctx: ScreenContext, item: NormalizedItem, evidence: str) -> Check:
    predicate = Predicate.RESOLVABLE_ID_PRESENT
    if item.fetched is FetchProfile.STUB:
        return _check(
            predicate,
            CheckStatus.UNAVAILABLE,
            reason="stub_profile",
            detail="fetched as a listing stub; guids were never requested",
            evidence=evidence,
        )
    if has_resolvable_id(item.guids):
        namespaces = sorted({str(external.namespace) for external in item.guids})
        return _check(
            predicate,
            CheckStatus.PASS,
            detail=f"guid namespaces: {', '.join(namespaces)}",
            evidence=evidence,
        )
    return _check(
        predicate,
        CheckStatus.FAIL,
        reason="no_resolvable_id",
        detail=(
            "no guid an external source could be asked about" if item.guids else "no guids at all"
        ),
        evidence=evidence,
    )


def _summary_present(ctx: ScreenContext, item: NormalizedItem, evidence: str) -> Check:
    predicate = Predicate.SUMMARY_PRESENT
    if item.fetched is FetchProfile.STUB:
        return _check(
            predicate,
            CheckStatus.UNAVAILABLE,
            reason="stub_profile",
            detail="fetched as a listing stub; the summary was never requested",
            evidence=evidence,
        )
    if item.summary and item.summary.strip():
        return _check(
            predicate,
            CheckStatus.PASS,
            detail=f"{len(item.summary)} characters",
            evidence=evidence,
        )
    return _check(
        predicate,
        CheckStatus.FAIL,
        reason="summary_empty",
        detail="no summary to null out, so `missing_metadata` cannot be ruled out here",
        evidence=evidence,
    )


def _single_part(ctx: ScreenContext, item: NormalizedItem, evidence: str) -> Check:
    predicate = Predicate.SINGLE_PART
    children = len(ctx.children_of(item))
    declared = item.part_count if isinstance(item, AudiobookItem) else None
    count = children if children else (declared or 0)
    if count == 0:
        return _check(
            predicate,
            CheckStatus.UNAVAILABLE,
            reason="no_parts_recorded",
            detail="no part children exported and no part_count declared",
            evidence=evidence,
        )
    if count == 1:
        return _check(predicate, CheckStatus.PASS, detail="one part", evidence=evidence)
    return _check(
        predicate,
        CheckStatus.FAIL,
        reason="multiple_parts",
        detail=(
            f"{count} parts: legitimately multi-file, or one book split across "
            "several -- not decidable without an authority"
        ),
        evidence=evidence,
    )


def _season_membership_coherent(ctx: ScreenContext, item: NormalizedItem, evidence: str) -> Check:
    predicate = Predicate.SEASON_MEMBERSHIP_COHERENT
    if isinstance(item, EpisodeItem):
        if item.parent is None:
            return _check(
                predicate,
                CheckStatus.UNAVAILABLE,
                reason="no_parent",
                detail="episode records no parent season",
                evidence=evidence,
            )
        season = ctx.by_id.get(str(item.parent))
        if season is None:
            return _check(
                predicate,
                CheckStatus.UNAVAILABLE,
                reason="parent_not_exported",
                detail=f"parent {item.parent} is not in this export",
                evidence=evidence,
            )
        season_index = getattr(season, "index", None)
        if item.parent_index is None or season_index is None:
            return _check(
                predicate,
                CheckStatus.UNAVAILABLE,
                reason="season_index_missing",
                detail="episode.parent_index or season.index is not recorded",
                evidence=evidence,
            )
        if item.parent_index == season_index:
            return _check(
                predicate,
                CheckStatus.PASS,
                detail=f"parent_index {item.parent_index} matches season {season_index}",
                evidence=evidence,
            )
        return _check(
            predicate,
            CheckStatus.FAIL,
            reason="season_mismatch",
            detail=(f"parent_index {item.parent_index} but hangs under season {season_index}"),
            evidence=evidence,
        )

    episodes = ctx.episodes_under(item)
    if not episodes:
        return _check(
            predicate,
            CheckStatus.UNAVAILABLE,
            reason="no_episodes_exported",
            detail="nothing beneath this item to check",
            evidence=evidence,
        )
    offenders: list[str] = []
    unknown = 0
    for episode in episodes:
        season = ctx.by_id.get(str(getattr(episode, "parent", "")))
        season_index = getattr(season, "index", None) if season else None
        parent_index = getattr(episode, "parent_index", None)
        if season_index is None or parent_index is None:
            unknown += 1
            continue
        if parent_index != season_index:
            offenders.append(str(episode.item_id))
    if offenders:
        return _check(
            predicate,
            CheckStatus.FAIL,
            reason="season_mismatch",
            detail=f"{len(offenders)} episode(s) under the wrong season: {_examples(offenders)}",
            evidence=evidence,
        )
    if unknown == len(episodes):
        return _check(
            predicate,
            CheckStatus.UNAVAILABLE,
            reason="season_index_missing",
            detail=f"no season index recorded on any of {len(episodes)} episode(s)",
            evidence=evidence,
        )
    return _check(
        predicate,
        CheckStatus.PASS,
        detail=f"{len(episodes) - unknown} episode(s) coherent"
        + (f", {unknown} without an index" if unknown else ""),
        evidence=evidence,
    )


def _season_numbering(episodes: Sequence[NormalizedItem]) -> tuple[str, str]:
    """`(status_reason, detail)` for one season's episode numbering."""
    indices = [getattr(episode, "index", None) for episode in episodes]
    known = sorted(index for index in indices if index is not None)
    if not known:
        return "episode_index_missing", "no episode carries an index"
    if len(known) != len(set(known)):
        duplicates = sorted({index for index in known if known.count(index) > 1})
        return "duplicate_index", f"duplicate episode indices: {duplicates}"
    if known != list(range(known[0], known[0] + len(known))):
        return "gap", f"episode indices are not contiguous: {known[0]}..{known[-1]}"
    if known[0] > 1:
        return "offset_start", f"numbering starts at {known[0]}, not 0 or 1"
    return "", f"{len(known)} episode(s), {known[0]}..{known[-1]}"


def _episode_numbering_contiguous(ctx: ScreenContext, item: NormalizedItem, evidence: str) -> Check:
    predicate = Predicate.EPISODE_NUMBERING_CONTIGUOUS
    seasons = [item] if item.media_kind is MediaKind.SEASON else list(ctx.children_of(item))
    checked = 0
    problems: list[str] = []
    for season in seasons:
        episodes = ctx.children_of(season)
        if not episodes:
            continue
        checked += 1
        reason, detail = _season_numbering(episodes)
        if reason:
            problems.append(f"{season.item_id}: {detail}")
    if checked == 0:
        return _check(
            predicate,
            CheckStatus.UNAVAILABLE,
            reason="no_episodes_exported",
            detail="no season beneath this item has exported episodes",
            evidence=evidence,
        )
    if problems:
        return _check(
            predicate,
            CheckStatus.FAIL,
            reason="numbering_broken",
            detail="; ".join(problems[:EXAMPLE_CAP])
            + (f" (+{len(problems) - EXAMPLE_CAP} more)" if len(problems) > EXAMPLE_CAP else ""),
            evidence=evidence,
        )
    return _check(
        predicate,
        CheckStatus.PASS,
        detail=f"{checked} season(s) numbered contiguously from 0 or 1",
        evidence=evidence,
    )


def _no_title_year_twin(ctx: ScreenContext, item: NormalizedItem, evidence: str) -> Check:
    predicate = Predicate.NO_TITLE_YEAR_TWIN
    if ctx.population is None:
        return _check(
            predicate,
            CheckStatus.UNAVAILABLE,
            reason="no_population_index",
            detail=(
                "this export has no roots.jsonl (schema_version 1). Re-run "
                "`shelfwarden export` to write one; uniqueness is never scoped to "
                "the slice instead"
            ),
            evidence=evidence,
        )
    twins, population = ctx.population.title_year_twins(item)
    if twins:
        return _check(
            predicate,
            CheckStatus.FAIL,
            reason="title_year_twin",
            detail=f"{len(twins)} other root(s) share (title, year): {_examples(twins)}",
            evidence=evidence,
            population=population,
        )
    return _check(
        predicate,
        CheckStatus.PASS,
        detail=f"unique across {population} root(s) of this kind in the section",
        evidence=evidence,
        population=population,
    )


def _no_author_name_twin(ctx: ScreenContext, item: NormalizedItem, evidence: str) -> Check:
    predicate = Predicate.NO_AUTHOR_NAME_TWIN
    if ctx.population is None:
        return _check(
            predicate,
            CheckStatus.UNAVAILABLE,
            reason="no_population_index",
            detail=(
                "this export has no roots.jsonl (schema_version 1). Re-run "
                "`shelfwarden export` to write one"
            ),
            evidence=evidence,
        )
    population = ctx.population.authors.blocking.population
    twins = ctx.population.authors.twins.get(str(item.item_id), ())
    if twins:
        return _check(
            predicate,
            CheckStatus.FAIL,
            reason="author_name_twin",
            detail=f"{len(twins)} name variant(s) in the population: {_examples(twins)}",
            evidence=evidence,
            population=population,
        )
    return _check(
        predicate,
        CheckStatus.PASS,
        detail=f"no variant among {population} author(s) compared under the blocking scheme",
        evidence=evidence,
        population=population,
    )


def _filename_matches_metadata(ctx: ScreenContext, item: NormalizedItem, evidence: str) -> Check:
    predicate = Predicate.FILENAME_MATCHES_METADATA
    parts = getattr(item, "parts", ())
    if not parts:
        return _check(
            predicate,
            CheckStatus.UNAVAILABLE,
            reason="no_file_parts",
            detail="no file parts recorded at this fetch profile",
            evidence=evidence,
        )

    if item.media_kind is MediaKind.EPISODE:
        index = getattr(item, "index", None)
        parent_index = getattr(item, "parent_index", None)
        if index is None or parent_index is None:
            return _check(
                predicate,
                CheckStatus.UNAVAILABLE,
                reason="episode_numbering_missing",
                detail="the item carries no (season, episode) to compare a filename against",
                evidence=evidence,
            )
        for part in parts:
            parsed = parse_release_name(part.path)
            if parsed.season is None or parsed.episode is None:
                return _check(
                    predicate,
                    CheckStatus.FAIL,
                    reason="no_season_episode_marker",
                    detail=f"{parsed.source!r} carries no SxxEyy marker",
                    evidence=evidence,
                )
            if (parsed.season, parsed.episode) != (parent_index, index):
                return _check(
                    predicate,
                    CheckStatus.FAIL,
                    reason="season_episode_mismatch",
                    detail=(
                        f"{parsed.source!r} says S{parsed.season:02d}E{parsed.episode:02d}, "
                        f"metadata says S{parent_index:02d}E{index:02d}"
                    ),
                    evidence=evidence,
                )
        return _check(
            predicate,
            CheckStatus.PASS,
            detail=f"{len(parts)} part(s) name S{parent_index:02d}E{index:02d}",
            evidence=evidence,
        )

    year = getattr(item, "year", None)
    weakest: Support | None = None
    for part in parts:
        parsed = parse_release_name(part.path)
        support = compare_title(parsed.title, item.title)
        if not ctx.policy.satisfied_by(support):
            return _check(
                predicate,
                CheckStatus.FAIL,
                reason="title_mismatch",
                detail=f"{parsed.source!r} parses to {parsed.title!r}, title is {item.title!r}",
                support=support,
                evidence=evidence,
            )
        if parsed.year is not None and year is not None and parsed.year != year:
            return _check(
                predicate,
                CheckStatus.FAIL,
                reason="year_mismatch",
                detail=f"{parsed.source!r} says {parsed.year}, metadata says {year}",
                support=support,
                evidence=evidence,
            )
        # Report the weakest rung any part needed, not the last one seen: a pass
        # carried by `strip_diacritics` is a weaker claim than one carried by an
        # exact match, and the recorded support is what 0.6 reads.
        if weakest is None or STRENGTH_RANK[support.strength] < STRENGTH_RANK[weakest.strength]:
            weakest = support
    return _check(
        predicate,
        CheckStatus.PASS,
        detail=f"{len(parts)} part filename(s) parse to the recorded title",
        support=weakest,
        evidence=evidence,
    )


# -- the authority predicates ---------------------------------------------


def _authority_record(
    ctx: ScreenContext, item: NormalizedItem
) -> tuple[AuthorityRecord | None, str, str]:
    """`(record, reason, detail)` -- reason is empty when a record came back."""
    if ctx.authority.name == NO_AUTHORITY:
        return None, "no_authority", "no metadata source is wired up yet (step 1.1)"
    if not has_resolvable_id(item.guids):
        return None, "no_resolvable_id", "nothing to look the item up by"
    for external in item.guids:
        record = ctx.authority.by_external_id(external.namespace, external.value)
        if record is not None:
            return record, "", f"matched on {external.namespace}://{external.value}"
    return None, "no_authority_record", "no source returned a record for any of this item's ids"


def _title_matches_authority(ctx: ScreenContext, item: NormalizedItem, evidence: str) -> Check:
    predicate = Predicate.TITLE_MATCHES_AUTHORITY
    record, reason, detail = _authority_record(ctx, item)
    if record is None:
        return _check(predicate, CheckStatus.UNAVAILABLE, reason=reason, detail=detail)
    support = compare_title(item.title, record.title, aliases=record.aliases)
    status = CheckStatus.PASS if ctx.policy.satisfied_by(support) else CheckStatus.FAIL
    return _check(
        predicate,
        status,
        reason=None if status is CheckStatus.PASS else "title_mismatch",
        detail=f"{detail}; authority title {record.title!r}",
        support=support,
        evidence=record.evidence_id,
    )


def _year_matches_authority(ctx: ScreenContext, item: NormalizedItem, evidence: str) -> Check:
    predicate = Predicate.YEAR_MATCHES_AUTHORITY
    record, reason, detail = _authority_record(ctx, item)
    if record is None:
        return _check(predicate, CheckStatus.UNAVAILABLE, reason=reason, detail=detail)
    support, delta = compare_year(getattr(item, "year", None), record.year)
    status = CheckStatus.PASS if ctx.policy.satisfied_by(support) else CheckStatus.FAIL
    return _check(
        predicate,
        status,
        reason=None if status is CheckStatus.PASS else "year_mismatch",
        detail=f"{detail}; authority year {record.year}, delta {delta}",
        support=support,
        evidence=record.evidence_id,
    )


def _series_position_matches_authority(
    ctx: ScreenContext, item: NormalizedItem, evidence: str
) -> Check:
    predicate = Predicate.SERIES_POSITION_MATCHES_AUTHORITY
    record, reason, detail = _authority_record(ctx, item)
    if record is None:
        return _check(predicate, CheckStatus.UNAVAILABLE, reason=reason, detail=detail)
    observed = item.series_position if isinstance(item, AudiobookItem) else None
    support = compare_series_position(observed, record.series_position)
    status = CheckStatus.PASS if ctx.policy.satisfied_by(support) else CheckStatus.FAIL
    return _check(
        predicate,
        status,
        reason=None if status is CheckStatus.PASS else "series_position_mismatch",
        detail=f"{detail}; authority position {record.series_position!r}",
        support=support,
        evidence=record.evidence_id,
    )


PREDICATE_RUNNERS = {
    Predicate.RESOLVABLE_ID_PRESENT: _resolvable_id_present,
    Predicate.SUMMARY_PRESENT: _summary_present,
    Predicate.SINGLE_PART: _single_part,
    Predicate.SEASON_MEMBERSHIP_COHERENT: _season_membership_coherent,
    Predicate.EPISODE_NUMBERING_CONTIGUOUS: _episode_numbering_contiguous,
    Predicate.NO_TITLE_YEAR_TWIN: _no_title_year_twin,
    Predicate.NO_AUTHOR_NAME_TWIN: _no_author_name_twin,
    Predicate.FILENAME_MATCHES_METADATA: _filename_matches_metadata,
    Predicate.TITLE_MATCHES_AUTHORITY: _title_matches_authority,
    Predicate.YEAR_MATCHES_AUTHORITY: _year_matches_authority,
    Predicate.SERIES_POSITION_MATCHES_AUTHORITY: _series_position_matches_authority,
}


# -- classification -------------------------------------------------------


def item_evidence_id(export_id: str, item: NormalizedItem) -> str:
    """A library read is evidence too (implementation-plan.md §6).

    The screen cites the export it read rather than asserting what it found,
    which is what fills `verification.checks[].evidence_id` in step 0.6's truth
    schema for local checks -- not only for the authority tier.
    """
    return evidence_id(
        Source.LIBRARY,
        "export",
        {"export_id": export_id, "item_id": str(item.item_id)},
        dump_item(item),
    )


def screen_item(ctx: ScreenContext, item: NormalizedItem) -> ItemScreen:
    evidence = item_evidence_id(ctx.export_id, item)
    checks: list[Check] = []
    for predicate in Predicate:
        if item.media_kind not in PREDICATE_KINDS[predicate]:
            # No `detail`: the status and the item's own media kind already say
            # everything there is to say, and this is the most repeated record in
            # the file.
            checks.append(_check(predicate, CheckStatus.NOT_APPLICABLE))
            continue
        checks.append(PREDICATE_RUNNERS[predicate](ctx, item, evidence))

    status = {check.predicate: check.status for check in checks}
    applicable = [check for check in checks if check.status in (CheckStatus.PASS, CheckStatus.FAIL)]
    failing = tuple(check.predicate for check in checks if check.status is CheckStatus.FAIL)
    passed = sum(1 for check in applicable if check.status is CheckStatus.PASS)

    if failing:
        # One failing applicable check disqualifies the whole item, per the
        # spec's own rule -- not only the classes that check guards. A
        # demonstrated problem outranks a thin check count, so this is tested
        # before `insufficient`.
        verdict = Verdict.FAILED
    elif len(applicable) >= MIN_APPLICABLE_CHECKS:
        verdict = Verdict.GUARDED
    else:
        verdict = Verdict.INSUFFICIENT

    guarded: list[ProblemClass] = []
    unguarded: list[ProblemClass] = []
    for problem_class in ProblemClass:
        guards = GUARD_TABLE[problem_class]
        if guards and all(status[predicate] is CheckStatus.PASS for predicate in guards):
            guarded.append(problem_class)
        else:
            unguarded.append(problem_class)

    return ItemScreen(
        item_id=str(item.item_id),
        media_kind=item.media_kind,
        title=item.title,
        verdict=verdict,
        applicable=len(applicable),
        passed=passed,
        checks=tuple(checks),
        guarded_classes=tuple(guarded),
        unguarded_classes=tuple(unguarded),
        failing_predicates=failing,
    )


def _guard_coverage(screens: Sequence[ItemScreen]) -> tuple[GuardCoverage, ...]:
    rows: list[GuardCoverage] = []
    for problem_class in ProblemClass:
        guards = tuple(sorted(GUARD_TABLE[problem_class]))
        tier = (
            None
            if not guards
            else (
                Tier.AUTHORITY
                if any(PREDICATE_TIER[predicate] is Tier.AUTHORITY for predicate in guards)
                else Tier.LOCAL
            )
        )
        in_scope = guarded = failed = blocked = 0
        if guards:
            for screen in screens:
                status = {check.predicate: check.status for check in screen.checks}
                relevant = [status[predicate] for predicate in guards]
                if all(value is CheckStatus.NOT_APPLICABLE for value in relevant):
                    continue
                in_scope += 1
                if CheckStatus.FAIL in relevant:
                    failed += 1
                elif CheckStatus.UNAVAILABLE in relevant:
                    blocked += 1
                elif all(value is CheckStatus.PASS for value in relevant):
                    guarded += 1
                else:
                    # A mix of PASS and NOT_APPLICABLE: nothing failed, but the
                    # guard set was not satisfied either.
                    blocked += 1
        rows.append(
            GuardCoverage(
                problem_class=problem_class,
                guard_predicates=guards,
                tier=tier,
                in_scope=in_scope,
                guarded=guarded,
                failed=failed,
                blocked=blocked,
                reason=UNGUARDABLE_REASON.get(problem_class),
            )
        )
    return tuple(rows)


def _predicate_summaries(screens: Sequence[ItemScreen]) -> tuple[PredicateSummary, ...]:
    counts: dict[Predicate, dict[str, int]] = {
        predicate: {status.value: 0 for status in CheckStatus} for predicate in Predicate
    }
    reasons: dict[Predicate, dict[str, int]] = {predicate: {} for predicate in Predicate}
    for screen in screens:
        for check in screen.checks:
            counts[check.predicate][check.status.value] += 1
            if check.reason:
                bucket = reasons[check.predicate]
                bucket[check.reason] = bucket.get(check.reason, 0) + 1
    return tuple(
        PredicateSummary(
            predicate=predicate,
            tier=PREDICATE_TIER[predicate],
            scope=PREDICATE_SCOPE[predicate],
            passed=counts[predicate][CheckStatus.PASS.value],
            failed=counts[predicate][CheckStatus.FAIL.value],
            not_applicable=counts[predicate][CheckStatus.NOT_APPLICABLE.value],
            unavailable=counts[predicate][CheckStatus.UNAVAILABLE.value],
            reasons=dict(sorted(reasons[predicate].items())),
        )
        for predicate in Predicate
    )


def _candidates(screens: Sequence[ItemScreen]) -> tuple[Candidate, ...]:
    candidates: list[Candidate] = []
    for screen in screens:
        if screen.verdict is not Verdict.FAILED:
            continue
        failing = set(screen.failing_predicates)
        proposed = sorted(
            problem_class for problem_class, guards in GUARD_TABLE.items() if guards & failing
        )
        candidates.append(
            Candidate(
                item_id=screen.item_id,
                media_kind=screen.media_kind,
                title=screen.title,
                failing_predicates=screen.failing_predicates,
                checks=tuple(check for check in screen.checks if check.status is CheckStatus.FAIL),
                proposed_problem_classes=tuple(proposed),
            )
        )
    return tuple(candidates)


def build_screen(
    manifest: export_module.Manifest,
    items: Sequence[NormalizedItem],
    roots: Sequence[ItemStub] | None,
    authority: AuthorityIndex | None = None,
    policy: Policy = SCREEN_POLICY,
) -> Screen:
    """Classify every item. Pure: no I/O, no clock."""
    ctx = ScreenContext.build(
        export_id=manifest.export_id,
        items=items,
        roots=roots,
        authority=authority or NullAuthority(),
        policy=policy,
    )
    screens = tuple(screen_item(ctx, item) for item in ctx.items)

    by_kind: dict[str, VerdictCounts] = {}
    totals = {Verdict.GUARDED: 0, Verdict.FAILED: 0, Verdict.INSUFFICIENT: 0}
    for screen in screens:
        totals[screen.verdict] += 1
        current = by_kind.get(str(screen.media_kind), VerdictCounts())
        by_kind[str(screen.media_kind)] = VerdictCounts(
            items=current.items + 1,
            guarded=current.guarded + (screen.verdict is Verdict.GUARDED),
            failed=current.failed + (screen.verdict is Verdict.FAILED),
            insufficient=current.insufficient + (screen.verdict is Verdict.INSUFFICIENT),
        )

    blocking: list[Blocking] = []
    sweep: dict[str, int] = {}
    if ctx.population is not None:
        blocking.append(
            Blocking(
                predicate=Predicate.NO_TITLE_YEAR_TWIN,
                scheme="exact key (section_id, media_kind, fold_text(title), year)",
                population=ctx.population.size,
                buckets=len(ctx.population.title_year),
                pairs_possible=ctx.population.size * (ctx.population.size - 1) // 2,
                pairs_resolved=ctx.population.size * (ctx.population.size - 1) // 2,
                pairs_skipped=0,
                note=(
                    "An exact key decides every pair by grouping, so nothing is skipped. "
                    "Its blindness is elsewhere: two spellings that do not fold to the "
                    "same key are not twins here, which is a missed guard rather than a "
                    "wrong one."
                ),
            )
        )
        blocking.append(ctx.population.authors.blocking)
        sweep = ctx.population.authors.sweep

    return Screen(
        shelfwarden_version=__version__,
        source=ScreenSource(
            export_id=manifest.export_id,
            items_sha256=manifest.items_sha256,
            roots_sha256=manifest.roots_sha256,
            export_schema_version=manifest.schema_version,
            population_index=roots is not None,
        ),
        authority=ctx.authority.name,
        min_applicable_checks=MIN_APPLICABLE_CHECKS,
        policy=policy.name,
        counts=ScreenCounts(
            items=len(screens),
            guarded=totals[Verdict.GUARDED],
            failed=totals[Verdict.FAILED],
            insufficient=totals[Verdict.INSUFFICIENT],
            by_media_kind=dict(sorted(by_kind.items())),
        ),
        guard_coverage=_guard_coverage(screens),
        predicates=_predicate_summaries(screens),
        blocking=tuple(blocking),
        author_fuzzy_sweep=sweep,
        items=screens,
        candidates=_candidates(screens),
    )


# -- reading an export, writing a screen ----------------------------------


def _digest(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def read_export(
    directory: Path,
) -> tuple[export_module.Manifest, tuple[NormalizedItem, ...], tuple[ItemStub, ...] | None]:
    """Load an export and verify it is the one its manifest describes.

    Errors here are *correctable* in the sense of practices §5.4: each names the
    next action. That taxonomy applies to the CLI for the same reason it applies
    to tools -- an error message that does not say what to do next is a dead end
    wearing the costume of a diagnosis.
    """
    manifest_path = directory / export_module.MANIFEST_FILE
    if not manifest_path.exists():
        raise ScreenError(
            f"{directory} is not an export directory: no {export_module.MANIFEST_FILE}. "
            "Point `shelfwarden screen` at a directory written by `shelfwarden export`."
        )
    manifest = export_module.load_manifest(directory)

    items_path = directory / export_module.ITEMS_FILE
    if not items_path.exists() or not items_path.read_bytes().strip():
        raise ScreenError(
            f"{directory} holds no items (selection mode {manifest.selection.mode!r}); "
            "there is nothing to screen. Re-run `shelfwarden export` without "
            "--census-only to fetch records."
        )
    payload = items_path.read_bytes()
    if _digest(payload) != manifest.items_sha256:
        raise ScreenError(
            f"{export_module.ITEMS_FILE} does not match the manifest's items_sha256. "
            "This export has been edited or truncated since it was written; re-run "
            "`shelfwarden export` rather than screening it."
        )
    items = export_module.load_items(directory)

    roots_path = directory / export_module.ROOTS_FILE
    if not roots_path.exists():
        # A version-1 export. Uniqueness reports `unavailable`; it never silently
        # falls back to slice scope, which is the bug that would invert
        # `fp_rate_snt` on `duplicate_quality`.
        return manifest, items, None
    roots_payload = roots_path.read_bytes()
    if manifest.roots_sha256 is not None and _digest(roots_payload) != manifest.roots_sha256:
        raise ScreenError(
            f"{export_module.ROOTS_FILE} does not match the manifest's roots_sha256. "
            "Re-run `shelfwarden export` rather than screening an edited population index."
        )
    return manifest, items, export_module.load_roots(directory)


def render_screen(screen: Screen) -> bytes:
    """Canonical JSON, with null fields omitted.

    `exclude_none` is a size decision, not a semantic one: every optional field
    here re-parses to `None` when absent, and the nulls are the bulk of the file
    -- a check that did not run a comparator carries five of them, eleven times
    per item. It is the one place this project treats absent and null as the
    same thing, and it is safe precisely because no reader distinguishes them.
    """
    return canonical_json(screen.model_dump(mode="json", exclude_none=True))


def run_screen(
    export_directory: Path,
    out: Path,
    authority: AuthorityIndex | None = None,
) -> Screen:
    """Screen an export and write `screen.json` + `screen.md`. Atomic."""
    manifest, items, roots = read_export(export_directory)
    screen = build_screen(manifest, items, roots, authority=authority)
    payload = render_screen(screen)
    export_module.write_atomically(
        out,
        {
            SCREEN_FILE: payload,
            SCREEN_MARKDOWN_FILE: render_markdown(screen).encode("utf-8"),
        },
    )
    return screen


def default_directory(export_id: str, base: Path = DEFAULT_SCREEN_ROOT) -> Path:
    return base / export_id


def load_screen(directory: Path, export_directory: Path | None = None) -> Screen:
    """Load a stored screen, optionally binding it to the export it describes.

    The binding check is not paranoia. A `guarded` label carried onto a
    different export is a wrong label with a plausible provenance, and it would
    be discovered as an unexplained false-positive rate three steps later.
    """
    screen = Screen.model_validate_json((directory / SCREEN_FILE).read_bytes())
    if export_directory is None:
        return screen
    manifest = export_module.load_manifest(export_directory)
    if manifest.items_sha256 != screen.source.items_sha256:
        raise ScreenError(
            f"this screen was taken of export {screen.source.export_id} "
            f"(items_sha256 {screen.source.items_sha256[:12]}), not of "
            f"{manifest.export_id} ({manifest.items_sha256[:12]}). Re-run "
            "`shelfwarden screen` against this export instead of reusing the stored one."
        )
    return screen


# -- rendering ------------------------------------------------------------


def _table(headers: tuple[str, ...], rows: Iterable[tuple[str, ...]]) -> list[str]:
    materialized = list(rows)
    if not materialized:
        return ["_(none)_", ""]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(row) + " |" for row in materialized]
    lines.append("")
    return lines


def render_markdown(screen: Screen) -> str:
    """The table a human reads before writing `composition.toml`.

    Renders from the screen alone, like `census.md`, so a stored `screen.json`
    stays readable without the export that produced it.
    """
    counts = screen.counts
    out: list[str] = [
        "# Mechanical screen",
        "",
        f"Export `{screen.source.export_id}` "
        f"(items_sha256 `{screen.source.items_sha256[:12]}`, "
        f"schema_version {screen.source.export_schema_version}).",
        "",
        f"Authority: **{screen.authority}**. Policy: `{screen.policy}` "
        f"(minimum support `{SCREEN_POLICY.minimum}`). "
        f"An item is guarded when at least {screen.min_applicable_checks} predicates "
        "are applicable and all of them pass.",
        "",
    ]
    if not screen.source.population_index:
        out += [
            "> **No population index.** This export predates `roots.jsonl`, so the two",
            "> uniqueness predicates report `unavailable` rather than being scoped to the",
            "> slice. Re-run `shelfwarden export` to restore them.",
            "",
        ]

    out += ["## Verdicts", ""]
    out += _table(
        ("verdict", "items", "share"),
        [
            (name, str(value), f"{(value / counts.items * 100):.1f}%" if counts.items else "—")
            for name, value in (
                ("guarded", counts.guarded),
                ("failed (real-slice candidates)", counts.failed),
                ("insufficient", counts.insufficient),
            )
        ],
    )
    out += [
        "`insufficient` is the number to read first: it counts items for which the",
        "local tier does not have three applicable predicates at all, which is how much",
        "of the should-not-touch slice is blocked on step 1.1 rather than on data.",
        "",
    ]
    out += _table(
        ("media kind", "items", "guarded", "failed", "insufficient"),
        [
            (kind, str(row.items), str(row.guarded), str(row.failed), str(row.insufficient))
            for kind, row in screen.counts.by_media_kind.items()
        ],
    )

    out += [
        "## Guard coverage per class",
        "",
        "The denominator `fp_rate_snt` is entitled to. A class with no guarded items is",
        "one where a finding scores `unverified` — counted and reported, never pass or",
        "fail.",
        "",
    ]
    out += _table(
        ("problem class", "guards", "tier", "in scope", "guarded", "failed", "blocked", "note"),
        [
            (
                str(row.problem_class),
                ", ".join(str(predicate) for predicate in row.guard_predicates) or "—",
                str(row.tier) if row.tier else "—",
                str(row.in_scope),
                str(row.guarded),
                str(row.failed),
                str(row.blocked),
                row.reason or "",
            )
            for row in screen.guard_coverage
        ],
    )

    out += ["## Predicates", ""]
    out += _table(
        ("predicate", "tier", "scope", "pass", "fail", "n/a", "unavailable", "reasons"),
        [
            (
                str(row.predicate),
                str(row.tier),
                str(row.scope),
                str(row.passed),
                str(row.failed),
                str(row.not_applicable),
                str(row.unavailable),
                ", ".join(f"{name} x{count}" for name, count in row.reasons.items()) or "—",
            )
            for row in screen.predicates
        ],
    )

    out += [
        "## Population scope and blocking",
        "",
        "Uniqueness is claimed over the library's roots, never over the exported slice:",
        "a duplicate that was simply not sampled would otherwise mark an item guarded and",
        "score the agent's correct finding as a false positive.",
        "",
    ]
    out += _table(
        ("predicate", "scheme", "population", "buckets", "pairs possible", "resolved", "skipped"),
        [
            (
                str(row.predicate),
                row.scheme,
                str(row.population),
                str(row.buckets),
                str(row.pairs_possible),
                str(row.pairs_resolved),
                str(row.pairs_skipped),
            )
            for row in screen.blocking
        ],
    )
    for row in screen.blocking:
        out += [f"- **{row.predicate}** — {row.note}"]
    out += [""]

    if screen.author_fuzzy_sweep:
        out += [
            "### Author-twin threshold sweep",
            "",
            "The one float threshold in this step, published as a sweep rather than",
            "chosen. The screen itself counts a twin only at `normalized` support or",
            "better; these are the pair counts a fuzzy floor would add.",
            "",
        ]
        out += _table(
            ("fuzzy floor", "twin pairs"),
            [(floor, str(count)) for floor, count in screen.author_fuzzy_sweep.items()],
        )

    out += ["## Real-slice candidates", ""]
    if not screen.candidates:
        out += ["_(none)_", ""]
    else:
        shown = screen.candidates[:EXAMPLE_CAP]
        out += _table(
            ("item", "kind", "title", "failing predicates", "proposed classes"),
            [
                (
                    f"`{candidate.item_id}`",
                    str(candidate.media_kind),
                    candidate.title,
                    ", ".join(str(p) for p in candidate.failing_predicates),
                    ", ".join(str(c) for c in candidate.proposed_problem_classes) or "—",
                )
                for candidate in shown
            ],
        )
        if len(screen.candidates) > len(shown):
            out += [
                f"…and {len(screen.candidates) - len(shown)} more in `{SCREEN_FILE}`.",
                "",
            ]
    return "\n".join(out) + "\n"


__all__ = [
    "DEFAULT_SCREEN_ROOT",
    "FUZZY_SWEEP",
    "GUARD_TABLE",
    "MIN_APPLICABLE_CHECKS",
    "PREDICATE_KINDS",
    "PREDICATE_SCOPE",
    "PREDICATE_TIER",
    "SCREEN_FILE",
    "SCREEN_MARKDOWN_FILE",
    "AuthorityIndex",
    "AuthorityRecord",
    "Blocking",
    "Candidate",
    "Check",
    "CheckStatus",
    "GuardCoverage",
    "ItemScreen",
    "NullAuthority",
    "PopulationIndex",
    "Predicate",
    "Scope",
    "Screen",
    "ScreenError",
    "Tier",
    "Verdict",
    "build_screen",
    "default_directory",
    "item_evidence_id",
    "load_screen",
    "read_export",
    "render_markdown",
    "render_screen",
    "run_screen",
    "screen_item",
]
