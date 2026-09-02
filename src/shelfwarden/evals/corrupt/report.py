"""The corruption preview: what this library can actually supply, and where it runs out.

This is the report you read **before** writing `composition.toml`, the way
`--census-only` is the command you run before choosing `--count`. It answers one
question: for each of the fifteen problem classes, how many cases can this export
produce, and when it produces none, is that because the library has no such shape
or because the harness rejected what it built?

Those two are counted separately throughout, for the reason step 0.45 keeps
`not_applicable` and `unavailable` apart: *"your library has no remake pairs"* and
*"the generator is broken"* are different facts and only one is actionable.

The document carries **no timestamp**. A survey is a pure function of the export,
the seed, and the code that read them, and byte-identity is the cheapest proof of
that -- the same rule `screen.json` follows, for the same reason.
"""

from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from shelfwarden import __version__
from shelfwarden.canonical import canonical_json
from shelfwarden.evals import export as export_module
from shelfwarden.evals.corrupt.model import Rejection
from shelfwarden.evals.corrupt.registry import (
    CORRUPTION_TABLE,
    UNSYNTHESIZABLE_REASON,
    CorruptionResult,
)
from shelfwarden.evals.corrupt.run import (
    ClassDeficit,
    CorruptionRun,
    read_export_with_population,
    run_corruptions,
)
from shelfwarden.evals.corrupt.witness import WitnessTier
from shelfwarden.models.finding import ProblemClass

CORRUPTIONS_FILE = "corruptions.json"
REJECTED_FILE = "rejected.jsonl"
MARKDOWN_FILE = "corruptions.md"
SCHEMA_VERSION = 1

DEFAULT_CORRUPTION_ROOT = Path("datasets/corruptions")

# How many rejection examples any list carries. Whatever it drops, it counts
# (house rule 12).
EXAMPLE_CAP = 5


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReportSource(_Frozen):
    """What this survey is a survey *of*. Binding, not provenance decoration."""

    export_id: str
    items_sha256: str
    roots_sha256: str | None = None


class ReportCounts(_Frozen):
    families: int
    accepted: int
    rejected: int
    not_applicable: int
    classes_implemented: int
    classes_deferred: int
    classes_with_cases: int


class CorruptionReport(_Frozen):
    schema_version: int = SCHEMA_VERSION
    shelfwarden_version: str
    source: ReportSource
    seed: int
    # Defaulted because `render_report` uses `exclude_none`: a field with no
    # default would be dropped on write and missing on read, which turns "no
    # limit" into an unparseable document.
    limit: int | None = None
    counts: ReportCounts
    deficits: tuple[ClassDeficit, ...]
    cases: tuple[CorruptionResult, ...]


def _class_rank(problem_class: ProblemClass) -> int:
    return list(ProblemClass).index(problem_class)


def build_report(manifest: export_module.Manifest, run: CorruptionRun) -> CorruptionReport:
    """Assemble the document. Pure: no I/O, no clock."""
    cases = tuple(
        sorted(run.results, key=lambda case: (_class_rank(case.problem_class), case.root_id))
    )
    deficits = tuple(sorted(run.deficits, key=lambda row: _class_rank(row.problem_class)))
    return CorruptionReport(
        shelfwarden_version=__version__,
        source=ReportSource(
            export_id=manifest.export_id,
            items_sha256=manifest.items_sha256,
            roots_sha256=manifest.roots_sha256,
        ),
        seed=run.seed,
        limit=run.limit,
        counts=ReportCounts(
            families=run.families,
            accepted=len(run.results),
            rejected=sum(1 for row in run.rejections if row.applicable),
            not_applicable=sum(1 for row in run.rejections if not row.applicable),
            classes_implemented=len(CORRUPTION_TABLE),
            classes_deferred=len(UNSYNTHESIZABLE_REASON),
            classes_with_cases=len({case.problem_class for case in cases}),
        ),
        deficits=deficits,
        cases=cases,
    )


def render_report(report: CorruptionReport) -> bytes:
    """Canonical JSON, with null fields omitted.

    `exclude_none` is a size decision: every optional field re-parses to `None`
    when absent, and a witness that made no alias match carries several of them
    per case.
    """
    return canonical_json(report.model_dump(mode="json", exclude_none=True))


def render_rejected(rejections: Sequence[Rejection]) -> bytes:
    """One rejection per line, ordered so the file is diffable across runs."""
    ordered = sorted(
        rejections,
        key=lambda row: (_class_rank(row.problem_class), row.root_id, row.reason),
    )
    return b"".join(
        canonical_json(row.model_dump(mode="json", exclude_none=True)) + b"\n" for row in ordered
    )


def _table(headers: tuple[str, ...], rows: Iterable[tuple[str, ...]]) -> list[str]:
    materialized = list(rows)
    if not materialized:
        return ["_(none)_", ""]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(row) + " |" for row in materialized]
    lines.append("")
    return lines


def _reasons(counted: Sequence[tuple[str, int]]) -> str:
    if not counted:
        return "—"
    # Re-derived at render time rather than inherited from the stored order:
    # `canonical_json` sorts keys, so a count-ordered mapping does not survive the
    # round trip (practices §8.2).
    ordered = sorted(counted, key=lambda pair: (-pair[1], pair[0]))
    kept = ordered[:EXAMPLE_CAP]
    rendered = ", ".join(f"{reason} x{count}" for reason, count in kept)
    dropped = len(ordered) - len(kept)
    return f"{rendered} (+{dropped} more)" if dropped else rendered


def render_markdown(report: CorruptionReport) -> str:
    counts = report.counts
    lines = [
        "# Corruption survey",
        "",
        f"Export `{report.source.export_id}` · seed `{report.seed}` · "
        f"{counts.families} famil{'y' if counts.families == 1 else 'ies'}",
        "",
        f"**{counts.accepted} case(s)** across {counts.classes_with_cases} of "
        f"{counts.classes_implemented + counts.classes_deferred} problem classes. "
        f"{counts.not_applicable} famil(y/ies) were never candidates and "
        f"{counts.rejected} attempt(s) were rejected.",
        "",
        "This is a **candidate survey, not a dataset**: every applicable class is",
        "attempted against every applicable family, because the question it answers",
        "is which classes this library can supply. Step 0.6 selects from these and",
        "enforces one case per family.",
        "",
        "## Per class",
        "",
    ]
    rows = []
    for deficit in report.deficits:
        if deficit.unsynthesizable_reason:
            rows.append(
                (
                    f"`{deficit.problem_class}`",
                    "—",
                    "—",
                    "—",
                    "—",
                    "**deferred** — " + deficit.unsynthesizable_reason.split(".")[0] + ".",
                )
            )
            continue
        rows.append(
            (
                f"`{deficit.problem_class}`",
                str(deficit.families_in_scope),
                str(deficit.candidates),
                str(deficit.attempted),
                str(deficit.accepted),
                _reasons(deficit.not_applicable_by_reason + deficit.rejected_by_reason),
            )
        )
    lines += _table(("class", "in scope", "candidates", "attempted", "accepted", "why not"), rows)

    lines += ["## Why a class produces nothing", "", _WHY, "", "## Witness tiers", ""]
    tiers = []
    for problem_class in ProblemClass:
        spec = CORRUPTION_TABLE.get(problem_class)
        if spec is None:
            tiers.append((f"`{problem_class}`", "—", "—", "deferred"))
            continue
        tiers.append(
            (
                f"`{problem_class}`",
                str(spec.witness_kind),
                str(spec.tier),
                ", ".join(spec.variants),
            )
        )
    lines += _table(("class", "witness", "tier", "variants"), tiers)

    authority = any(spec.tier is WitnessTier.AUTHORITY for spec in CORRUPTION_TABLE.values())
    if authority:  # pragma: no cover -- no authority-tier corruption ships before 1.1
        lines += ["Some witnesses need an external record; see step 1.1.", ""]

    lines += ["## Cross-check", "", _CROSS, ""]
    verdicts: dict[str, int] = {}
    for case in report.cases:
        verdicts[case.cross_check.verdict] = verdicts.get(case.cross_check.verdict, 0) + 1
    lines += _table(
        ("verdict", "cases"),
        ((verdict, str(count)) for verdict, count in sorted(verdicts.items())),
    )
    return "\n".join(lines) + "\n"


_WHY = (
    "**candidates** counts families the class could apply to; the gap between "
    "*in scope* and *candidates* is a fact about the library (no remake pairs, no "
    "edition markers on disk). The gap between *attempted* and *accepted* is a "
    "fact about this harness: a case was built and then refused, almost always "
    "because nothing could witness it. Only the second gap is a bug."
)

_CROSS = (
    "Every corruption is re-screened. `broken` means the screen guarded the class "
    "before and does not after -- the corruption is corroborated by an independent "
    "check. `unavailable` means the guard needs an external source (step 1.1). "
    "`already_failing` means the ground truth was never guarded for this class. "
    "`intact` never appears: such a case is rejected, because either the "
    "corruption did not do what it claims or the guard does not cover it."
)


def run_corrupt(
    export_directory: Path,
    out: Path,
    *,
    seed: int,
    classes: Sequence[ProblemClass] | None = None,
    limit: int | None = None,
) -> CorruptionReport:
    """Survey an export and write the three artifacts. Atomic."""
    manifest, items, roots = read_export_with_population(export_directory)
    run = run_corruptions(
        export_id=manifest.export_id,
        items=items,
        roots=roots,
        seed=seed,
        classes=classes,
        limit=limit,
    )
    report = build_report(manifest, run)
    export_module.write_atomically(
        out,
        {
            CORRUPTIONS_FILE: render_report(report),
            REJECTED_FILE: render_rejected(run.rejections),
            MARKDOWN_FILE: render_markdown(report).encode("utf-8"),
        },
    )
    return report


def default_directory(export_id: str, base: Path = DEFAULT_CORRUPTION_ROOT) -> Path:
    return base / export_id


def load_report(directory: Path) -> CorruptionReport:
    return CorruptionReport.model_validate_json((directory / CORRUPTIONS_FILE).read_bytes())


__all__ = [
    "CORRUPTIONS_FILE",
    "DEFAULT_CORRUPTION_ROOT",
    "MARKDOWN_FILE",
    "REJECTED_FILE",
    "SCHEMA_VERSION",
    "CorruptionReport",
    "build_report",
    "default_directory",
    "load_report",
    "render_markdown",
    "render_rejected",
    "render_report",
    "run_corrupt",
]
