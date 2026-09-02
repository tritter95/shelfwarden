"""What a corruption is handed: a family, a subject key, and its own RNG.

Two things here are load-bearing and neither is obvious.

**The RNG is per case, not per run.** `Random.sample` is not a prefix-stable
function of `k`: it holds two algorithms and picks between them on a size
heuristic, so `Random(1518).sample(range(24), 5)` selects element 23 and
`sample(range(24), 6)` does not. `Random.shuffle` has the same shape for a
different reason -- Fisher-Yates draws depend on the list length. A single RNG
stream consumed in iteration order therefore makes every case's corruption a
function of how many cases came before it, and step 0.6 needs the opposite:
`case_id` must survive regeneration so the CI baseline is not reset by adding one
case. So each case seeds its own `Random` from the same tuple its `case_id` will
hash, and subject *selection* never uses the RNG at all -- it ranks candidates by
`sha256(seed | subject_key)`, which is additive by construction.

**The subject key is not a rating key.** Invariant 9: rating keys move on
rescan, so seeding from one would re-corrupt the library differently after every
Plex maintenance pass. Step 0.6 owns `case_id` and the collision policy; the
ladder lives here because the RNG needs it one step earlier.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from random import Random

from shelfwarden.canonical import canonical_json
from shelfwarden.compare import RESOLVABLE_NAMESPACES, SCREEN_POLICY, Policy, fold_text
from shelfwarden.evals import export as export_module
from shelfwarden.evals.corrupt.model import Rejection
from shelfwarden.models.finding import ProblemClass
from shelfwarden.models.item import ItemStub, MediaKind, NormalizedItem

# How many bytes of a digest seed a `Random`. Eight is the whole of a 64-bit
# seed; more would be discarded by Mersenne Twister's initialisation anyway.
_SEED_BYTES = 8

# Minted rating keys carry this prefix so a synthetic item is visibly synthetic
# in every log, path, and diff. Plex's own keys are numeric, so the namespace
# cannot collide -- and `item_sort_key` places non-numeric keys after numeric
# ones, which keeps record order stable rather than interleaved.
MINTED_PREFIX = "sw"


@dataclass(frozen=True, slots=True)
class SubjectKey:
    """What a case is *about*, in a form that survives a re-export.

    Three rungs, tried in order:

    1. `external_id` -- the first resolvable guid, sorted so the choice is not a
       function of XML order.
    2. `title_year` -- a hash over the item's kind, folded title, year, and (for
       a child item) its position under its parent. Two different episodes of one
       show must not share a key, so season and episode participate.
    3. `path` -- a hash of the first file path, for an item with no title at all.
       Unreachable today because `title` is required; the rung exists rather than
       being asserted impossible.
    """

    kind: str
    value: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


def _digest(payload: object) -> str:
    return sha256(canonical_json(payload)).hexdigest()


def subject_key(item: NormalizedItem) -> SubjectKey:
    """The subject ladder. Never a Plex rating key (invariant 9)."""
    resolvable = sorted(
        (str(external.namespace), external.value)
        for external in item.guids
        if external.namespace in RESOLVABLE_NAMESPACES
    )
    if resolvable:
        namespace, value = resolvable[0]
        return SubjectKey("external_id", f"{namespace}://{value}")

    if item.title:
        return SubjectKey(
            "title_year",
            _digest(
                {
                    "kind": str(item.media_kind),
                    "title": fold_text(item.title),
                    "year": getattr(item, "year", None),
                    "index": getattr(item, "index", None),
                    "parent_index": getattr(item, "parent_index", None),
                    "parent_title": fold_text(getattr(item, "parent_title", None) or ""),
                }
            )[:16],
        )

    parts = getattr(item, "parts", ())
    return SubjectKey("path", _digest([part.path for part in parts])[:16])


def rank_key(seed: int, subject: SubjectKey) -> str:
    """The ordering that replaces a random draw.

    Sorting candidates by this and taking the first *N* is prefix-stable: raising
    *N* adds subjects without moving the ones already chosen. `random.sample` is
    not, and the difference is a reset baseline on every composition edit.
    """
    return _digest({"seed": seed, "subject": str(subject)})


@dataclass(frozen=True, slots=True)
class CorruptionContext:
    """Everything a corruption may read, and nothing it may write.

    `items` is the whole export rather than the family, because three classes
    need a *donor* from elsewhere in the library: `wrong_match` takes another
    film's identity, `year_collision_remake` needs the other half of a remake
    pair, `alternate_cut` needs a sibling cut. `roots` is the population index,
    for the same reason the screen reads it -- an absence claim scoped to a slice
    is not an absence claim about the library.
    """

    export_id: str
    seed: int
    problem_class: ProblemClass
    variant: str
    subject: SubjectKey
    root: ItemStub
    items: Mapping[str, NormalizedItem]
    roots: tuple[ItemStub, ...]
    rng: Random
    policy: Policy = SCREEN_POLICY

    @classmethod
    def build(
        cls,
        *,
        export_id: str,
        seed: int,
        problem_class: ProblemClass,
        variant: str,
        root: ItemStub,
        subject: SubjectKey,
        items: Mapping[str, NormalizedItem],
        roots: Sequence[ItemStub],
        policy: Policy = SCREEN_POLICY,
    ) -> "CorruptionContext":
        digest = sha256(
            canonical_json(
                {
                    "seed": seed,
                    "subject": str(subject),
                    "problem_class": str(problem_class),
                    "variant": variant,
                }
            )
        ).digest()
        return cls(
            export_id=export_id,
            seed=seed,
            problem_class=problem_class,
            variant=variant,
            subject=subject,
            root=root,
            items=items,
            roots=tuple(roots),
            rng=Random(int.from_bytes(digest[:_SEED_BYTES])),
            policy=policy,
        )

    def mint(self, ordinal: int) -> str:
        """A rating key for an added item: deterministic, and visibly synthetic."""
        digest = _digest(
            {"subject": str(self.subject), "class": str(self.problem_class), "n": ordinal}
        )
        return f"{MINTED_PREFIX}{digest[:10]}"

    def reject(
        self, reason: str, detail: str | None = None, *, applicable: bool = True
    ) -> Rejection:
        """Decline to emit this case, saying which population it falls into.

        `applicable=False` means the family was never a candidate -- a supply fact
        about the library. The default means a corruption was attempted and failed
        -- a fact about this machinery. Collapsing the two turns "your library has
        no remake pairs" into "the generator is broken".
        """
        return Rejection(
            problem_class=self.problem_class,
            media_kind=self.root.media_kind,
            root_id=str(self.root.item_id),
            reason=reason,
            detail=detail,
            applicable=applicable,
        )


def stub_of(item: NormalizedItem) -> ItemStub:
    """A root stub derived from an item rather than patched alongside one.

    Step 0.5 verified what happens when the two drift: with `items.jsonl`
    corrupted and `roots.jsonl` stale, the twin relation goes *asymmetric* -- the
    corrupted item finds its victim, the victim does not find it back -- and the
    screen reports a guard that is not true, silently. A stub is a projection of
    an item, so it is derived.
    """
    return ItemStub(
        item_id=item.item_id,
        media_kind=item.media_kind,
        title=item.title,
        year=getattr(item, "year", None),
    )


def group_families(items: Sequence[NormalizedItem]) -> tuple[export_module.Family, ...]:
    """Split an export into families: a root and everything beneath it.

    A root is an item with no parent. Descendants are found by walking `parent`
    transitively rather than by trusting file order, so a family is intact even
    if a caller hands the items in some other order.
    """
    by_id = {str(item.item_id): item for item in items}
    children: dict[str, list[NormalizedItem]] = {}
    roots: list[NormalizedItem] = []
    for item in items:
        parent = getattr(item, "parent", None)
        if parent is None or str(parent) not in by_id:
            roots.append(item)
        else:
            children.setdefault(str(parent), []).append(item)

    families: list[export_module.Family] = []
    for root in roots:
        records: list[NormalizedItem] = []
        frontier = [root]
        while frontier:
            current = frontier.pop(0)
            records.append(current)
            frontier.extend(children.get(str(current.item_id), ()))
        families.append(export_module.Family(root=stub_of(root), records=tuple(records)))
    return tuple(families)


@dataclass(frozen=True, slots=True)
class PartRef:
    """One file, and the item that owns it.

    A corruption that rewrites a path must say *which* part it rewrote, and
    `parts[2]` is the positional identifier invariant 9 rejects -- so a reference
    carries the owning item id and the RFC 6901 pointer that addresses it.
    """

    item_id: str
    index: int
    path: str

    @property
    def pointer(self) -> str:
        return f"/parts/{self.index}/path"


def parts_in(family: export_module.Family) -> tuple[PartRef, ...]:
    """Every file in a family, in record order then part order.

    Eight of the eleven implementable classes take their detectability witness
    from a file path, so this is the most-used accessor in the package.
    """
    refs: list[PartRef] = []
    for record in family.records:
        for index, part in enumerate(getattr(record, "parts", ())):
            refs.append(PartRef(str(record.item_id), index, part.path))
    return tuple(refs)


def pick_by_rank[T](seed: int, candidates: Sequence[tuple[SubjectKey, T]]) -> T | None:
    """The first candidate by hash rank, or `None`.

    Not `rng.choice`: a draw's result depends on how many draws came before it and
    on the length of the candidate list, so adding one item to a library would
    re-pick donors across unrelated cases. Ranking is a pure function of the
    candidate, which is what keeps `corruption_fingerprint` stable for cases that
    did not change.
    """
    ranked = sorted(candidates, key=lambda pair: (rank_key(seed, pair[0]), str(pair[0])))
    return ranked[0][1] if ranked else None


def kinds_in(family: export_module.Family) -> frozenset[MediaKind]:
    return frozenset(record.media_kind for record in family.records)


__all__ = [
    "MINTED_PREFIX",
    "CorruptionContext",
    "PartRef",
    "SubjectKey",
    "group_families",
    "kinds_in",
    "parts_in",
    "pick_by_rank",
    "rank_key",
    "stub_of",
    "subject_key",
]
