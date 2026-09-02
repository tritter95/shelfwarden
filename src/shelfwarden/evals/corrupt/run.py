"""Running every registered corruption over an export, and counting what happened.

This is a **candidate survey**, not a dataset. It attempts every applicable class
against every applicable family, because the question it answers is the one you
need before writing `composition.toml`: *which classes can this library actually
supply, and where does the supply run out?* Step 0.6 selects from these
candidates and enforces one case per family; here a family is deliberately
offered to every class it fits.

Selection is by **hash rank**, never by a random draw. `random.sample` is not a
prefix-stable function of `k` -- verified in step 0.5: `Random(1518).sample(
range(24), 5)` selects element 23 and `sample(range(24), 6)` does not -- so
raising a limit by one would re-pick different subjects and reset every
`case_id` in that cell. Ranking by `sha256(seed | subject_key)` is additive: a
larger limit is a superset of a smaller one.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from shelfwarden.evals import export as export_module
from shelfwarden.evals.corrupt.context import (
    CorruptionContext,
    group_families,
    rank_key,
    subject_key,
)
from shelfwarden.evals.corrupt.model import Rejection
from shelfwarden.evals.corrupt.registry import (
    CORRUPTION_TABLE,
    UNSYNTHESIZABLE_REASON,
    CorruptionResult,
    CorruptionSpec,
    attempt,
)
from shelfwarden.models.finding import ProblemClass
from shelfwarden.models.item import ItemStub, NormalizedItem


class ClassDeficit(BaseModel):
    """Per class: how far the library got, and where it stopped.

    `candidates` and `attempted` are separate because the difference between them
    is the difference between "your library has no remake pairs" and "the
    generator is broken", and only one of those is actionable. `rejected_by_reason`
    is sorted count-descending then key-ascending, so the table is a function of
    the data rather than of dict order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    problem_class: ProblemClass
    families_in_scope: int
    candidates: int
    attempted: int
    accepted: int
    not_applicable_by_reason: tuple[tuple[str, int], ...] = ()
    rejected_by_reason: tuple[tuple[str, int], ...] = ()
    unsynthesizable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CorruptionRun:
    export_id: str
    seed: int
    limit: int | None
    families: int
    results: tuple[CorruptionResult, ...]
    rejections: tuple[Rejection, ...]
    deficits: tuple[ClassDeficit, ...]


def _counted(reasons: Sequence[str]) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    # Count descending, then key ascending. Practices §8.2: a count-ordered
    # mapping does not survive canonical JSON, so the order is re-derived here and
    # again at render time rather than inherited from a dict.
    return tuple(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def variant_for(spec: CorruptionSpec, seed: int, subject: object) -> str:
    """Which variant this subject gets. Stable, and not drawn from the RNG.

    A drawn variant would move when an unrelated case was added, and the variant
    is part of `case_id` -- so the baseline would reset for cases that did not
    change.
    """
    digest = rank_key(seed, subject)  # type: ignore[arg-type]
    return spec.variants[int(digest[:8], 16) % len(spec.variants)]


def run_corruptions(
    *,
    export_id: str,
    items: Sequence[NormalizedItem],
    roots: Sequence[ItemStub],
    seed: int,
    classes: Sequence[ProblemClass] | None = None,
    limit: int | None = None,
) -> CorruptionRun:
    """Attempt every registered corruption over an export."""
    by_id: Mapping[str, NormalizedItem] = {str(item.item_id): item for item in items}
    families = group_families(items)
    wanted = tuple(classes) if classes else tuple(ProblemClass)

    results: list[CorruptionResult] = []
    rejections: list[Rejection] = []
    deficits: list[ClassDeficit] = []

    for problem_class in wanted:
        spec = CORRUPTION_TABLE.get(problem_class)
        if spec is None:
            deficits.append(
                ClassDeficit(
                    problem_class=problem_class,
                    families_in_scope=0,
                    candidates=0,
                    attempted=0,
                    accepted=0,
                    not_applicable_by_reason=(),
                    rejected_by_reason=(),
                    unsynthesizable_reason=UNSYNTHESIZABLE_REASON.get(
                        problem_class, "no corruption function is registered"
                    ),
                )
            )
            continue

        in_scope = [family for family in families if family.root.media_kind in spec.applies_to]
        ranked = sorted(
            (rank_key(seed, subject_key(family.records[0])), str(family.root.item_id), family)
            for family in in_scope
        )
        chosen = ranked if limit is None else ranked[:limit]

        accepted = 0
        attempted = 0
        not_applicable: list[str] = []
        rejected: list[str] = []
        for _, _, family in chosen:
            subject = subject_key(family.records[0])
            ctx = CorruptionContext.build(
                export_id=export_id,
                seed=seed,
                problem_class=problem_class,
                variant=variant_for(spec, seed, subject),
                root=family.root,
                subject=subject,
                items=by_id,
                roots=roots,
            )
            outcome = attempt(spec, family, ctx)
            if isinstance(outcome, Rejection):
                rejections.append(outcome)
                if outcome.applicable:
                    attempted += 1
                    rejected.append(outcome.reason)
                else:
                    not_applicable.append(outcome.reason)
                continue
            attempted += 1
            accepted += 1
            results.append(outcome)

        deficits.append(
            ClassDeficit(
                problem_class=problem_class,
                families_in_scope=len(in_scope),
                candidates=len(chosen) - len(not_applicable),
                attempted=attempted,
                accepted=accepted,
                not_applicable_by_reason=_counted(not_applicable),
                rejected_by_reason=_counted(rejected),
            )
        )

    return CorruptionRun(
        export_id=export_id,
        seed=seed,
        limit=limit,
        families=len(families),
        results=tuple(results),
        rejections=tuple(rejections),
        deficits=tuple(deficits),
    )


def read_export_with_population(directory: Path):
    """Read an export and refuse one with no population index.

    An absent `roots.jsonl` is refused rather than worked around: a corruption's
    collateral and its screen cross-check are both population-scoped, and scoping
    them to the slice would report the blast radius as smaller than it is -- the
    same wrong direction step 0.45 forbade for `fp_rate_snt`.
    """
    from shelfwarden.evals.screen import ScreenError, read_export

    manifest, items, roots = read_export(directory)
    if roots is None:
        raise ScreenError(
            f"{directory} has no {export_module.ROOTS_FILE}: a corruption's collateral and "
            "its screen cross-check are population-scoped, and scoping them to the slice "
            "would under-report the blast radius. Re-run `shelfwarden export`."
        )
    return manifest, items, roots


__all__ = [
    "ClassDeficit",
    "CorruptionRun",
    "read_export_with_population",
    "run_corruptions",
    "variant_for",
]
