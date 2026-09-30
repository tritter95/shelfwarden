"""What the dataset is *meant* to hold, and the gap between that and what it can.

Composition is a hand-tuned knob, not a constant -- the census will keep forcing
revisions to it -- so it lives in a checked-in `composition.toml` at the repo root
rather than buried in this package. TOML because `tomllib` is standard library and
this file is read once, at generation time, with no runtime dependency.

**Two targets per cell, never one.** `intended` is what the composition asks for;
`achievable` is what the registry implements and the export can supply. Both are
written into `dataset.json` along with the gap, so a reader years later can tell a
deliberate share from a shortfall *without* the `composition.toml` that produced
it. A single resolved number makes those two indistinguishable, which is how a
dataset comes to report coverage it never had.

**The media axis is the family root kind**, which is what `CorruptionResult`
records and what `--count` counts. `author` is the audiobook root: an audiobook
library is a Plex `artist` section, so a corruption of an audiobook class is handed
an author family. That is also why a per-class share belongs to a *medium*: which
classes can run against a root of that kind is `CorruptionSpec.applies_to`, and a
share declared in the wrong medium shows up as a deficit row rather than silently
producing nothing.

All fifteen classes are declared, including the four with no corruption function.
The file is a statement of design intent and should not churn when step 1.1 lands;
the four appear in the deficit table as `not_implemented`, which is the honest
report.
"""

import heapq
import math
import tomllib
from collections.abc import Mapping, Sequence
from enum import StrEnum
from fractions import Fraction
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from shelfwarden.evals.truth import Slice
from shelfwarden.models.finding import ProblemClass
from shelfwarden.models.item import MediaKind

COMPOSITION_FILE = Path("composition.toml")

# The slices whose cells are split by problem class. `should_not_touch` is not one
# of them: such a case is about an item, not about a class, so its cells are
# slice x media only and the per-class shares are never read for it (which the
# resolver says out loud rather than silently multiplying by a table that does not
# apply).
CLASS_BEARING: frozenset[Slice] = frozenset(
    {Slice.SYNTHETIC, Slice.REAL, Slice.AMBIGUOUS},
)

# Slices the generator cannot synthesize. They are merged from `datasets/curated/`
# and ship empty until step 0.9.
CURATED: frozenset[Slice] = frozenset({Slice.REAL, Slice.AMBIGUOUS})


class CompositionError(Exception):
    """The composition file could not be read. Every message names the next action."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DeficitReason(StrEnum):
    """Why a cell cannot be filled as asked.

    The first two are the split `Rejection.applicable` exists to preserve: *"your
    library has no remake pairs"* and *"the harness rejected what it built"* are
    different facts and only the second is actionable. `run.ClassDeficit` already
    counts them apart, and collapsing them one layer up would throw that away.

    `not_curated` is a fifth reason this step's plan did not name. Folding an empty
    `real.toml` into `no_candidates` would report "your library has no such items"
    when the truth is "nobody has labelled any yet" -- a claim about the library
    standing in for a claim about the work queue.
    """

    NOT_IMPLEMENTED = "not_implemented"
    NO_CANDIDATES = "no_candidates"
    REJECTED = "rejected"
    CAPPED = "capped"
    NOT_CURATED = "not_curated"


class Medium(_Frozen):
    """One media kind's share, and how it splits across the fifteen classes."""

    share: float
    classes: dict[ProblemClass, float]


class Composition(_Frozen):
    """Normalized shares. Slice shares sum to 1, media shares sum to 1, and each
    medium's class shares sum to 1."""

    slices: dict[Slice, float]
    media: dict[MediaKind, Medium]
    composition_id: str


class Cell(_Frozen):
    """One (slice, media, class) target. `problem_class` is `None` for
    should-not-touch."""

    slice: Slice
    media_kind: MediaKind
    problem_class: ProblemClass | None
    share: float
    intended: int


class CompositionDeficit(_Frozen):
    slice: Slice
    media_kind: MediaKind
    problem_class: ProblemClass | None
    intended: int
    achievable: int
    reason: DeficitReason
    detail: str | None = None


def _normalize[K](shares: Mapping[K, float], what: str) -> dict[K, float]:
    """Shares need not sum to 1; they are normalized here rather than validated.

    A zero or negative total is an error, though: it is the one input from which no
    proportion can be recovered, and silently returning zeros everywhere would
    produce an empty dataset that looks like a supply problem.

    A negative share is checked **first**. Otherwise `synthetic = 1, real = -1`
    reports a zero total and advises giving one a positive share -- which one
    already has.

    `math.fsum` rather than `sum`: float addition is not associative, so `sum` over
    the TOML's declaration order can land an ulp away from `sum` over any other
    order, and every share divided by it moves with it. `fsum` is correctly rounded
    whatever the order, which makes the resolved targets a function of the shares
    by construction rather than by luck.
    """
    for key, value in shares.items():
        if value < 0:
            raise CompositionError(
                f"{what} share for {key!r} is negative ({value}). A share is a proportion; "
                "set it to 0 to exclude it."
            )
    total = math.fsum(shares.values())
    if total <= 0:
        raise CompositionError(
            f"the {what} shares in composition.toml sum to {total}, so no proportion can "
            "be derived. Give at least one a positive share."
        )
    return {key: value / total for key, value in shares.items()}


def parse_composition(payload: bytes) -> Composition:
    """Parse and normalize. `composition_id` is a digest of the bytes as given.

    The id is recorded in `dataset.json` as a **diagnostic** beside `lineage_id`,
    never as the baseline key -- see the note on `generate.lineage_id`. A reader can
    then see that the composition moved while the baseline did not, which is the
    fact they want.
    """
    try:
        data = tomllib.loads(payload.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise CompositionError(f"composition.toml is not readable TOML: {exc}") from exc

    unknown = set(data) - {"slices", "media"}
    if unknown:
        raise CompositionError(
            f"composition.toml has unexpected top-level table(s) {sorted(unknown)}; "
            "it holds [slices] and [media.<kind>] and nothing else."
        )
    if "slices" not in data or "media" not in data:
        raise CompositionError(
            "composition.toml needs both a [slices] table and at least one [media.<kind>] table."
        )

    try:
        slice_shares = {Slice(name): float(value) for name, value in data["slices"].items()}
    except ValueError as exc:
        known = ", ".join(str(value) for value in Slice)
        raise CompositionError(f"{exc}. Known slices: {known}") from exc

    media: dict[MediaKind, Medium] = {}
    for name, table in data["media"].items():
        try:
            kind = MediaKind(name)
        except ValueError as exc:
            known = ", ".join(str(value) for value in MediaKind)
            raise CompositionError(f"{exc}. Known media kinds: {known}") from exc
        if "share" not in table:
            raise CompositionError(f"[media.{name}] declares no `share`.")
        if "classes" not in table:
            # A parse-time gate rather than a default. Without a class table the
            # class-bearing slices in this medium resolve to nothing, and the
            # dataset comes out short for a reason no deficit row would explain.
            raise CompositionError(
                f"[media.{name}] declares no [media.{name}.classes] table. A medium's "
                "class shares are what the synthetic, real and ambiguous slices are "
                "split by; declare them (0 is a legal share) rather than leaving the "
                "medium to resolve to nothing."
            )
        try:
            classes = {ProblemClass(key): float(value) for key, value in table["classes"].items()}
        except ValueError as exc:
            known = ", ".join(str(value) for value in ProblemClass)
            raise CompositionError(f"{exc}. Known problem classes: {known}") from exc
        media[kind] = Medium(share=float(table["share"]), classes=classes)

    normalized_media = _normalize({kind: row.share for kind, row in media.items()}, "media")
    return Composition(
        slices=_normalize(slice_shares, "slice"),
        media={
            kind: Medium(
                share=share,
                classes=_normalize(media[kind].classes, f"[media.{kind}.classes]"),
            )
            for kind, share in normalized_media.items()
        },
        composition_id="comp-" + sha256(payload).hexdigest()[:12],
    )


def load_composition(path: Path = COMPOSITION_FILE) -> Composition:
    try:
        payload = path.read_bytes()
    except FileNotFoundError as exc:
        raise CompositionError(
            f"no composition file at {path}. `composition.toml` lives at the repository "
            "root and declares the slice, media, and per-class shares; pass --composition "
            "to point somewhere else."
        ) from exc
    return parse_composition(payload)


def _cell_sort_key(
    slice_: Slice, media_kind: MediaKind, problem_class: ProblemClass | None
) -> tuple[int, int, int]:
    """A total order over cells, by declaration order of the three enums.

    Explicit rather than alphabetical, and explicit rather than dict order:
    largest-remainder rounding breaks ties by position, so the resolved integers
    must be a function of the shares and not of how the TOML happened to be typed
    (practices §8.2).
    """
    return (
        list(Slice).index(slice_),
        list(MediaKind).index(media_kind),
        -1 if problem_class is None else list(ProblemClass).index(problem_class),
    )


def resolve(composition: Composition, count: int) -> tuple[Cell, ...]:
    """Multiply the shares out to absolute per-cell targets summing to `count`.

    **Sequential Webster (Sainte-Laguë) apportionment**, not largest remainder.
    Targets are handed out one at a time, each to the cell with the highest
    `share / (2 * held + 1)`, so the targets for `count + 1` are the targets for
    `count` plus one. Raising `--count` therefore never shrinks a cell, which is the
    prefix stability every selection rule in step 0.6 exists to protect.

    Largest remainder -- what the step plan first named -- does not have that
    property. Measured on the committed `composition.toml`: between counts 0 and
    1000, a cell shrank 210 times when the count rose by one, ten of them in cells
    the generator fills (`should_not_touch | author` 18 -> 17 at 467). A shrunk
    cell drops a case and its history on an edit that asked for *more*. Webster
    rather than D'Hondt because D'Hondt favours large cells, and the small classes
    are the ones a share table is most likely to starve.

    Priorities are compared as exact fractions, so ties are decided by
    `_cell_sort_key` and never by float noise. Cells with a zero share are kept: a
    zero target is a deliberate statement, and dropping it would make a later reader
    unable to tell "we asked for none" from "we never considered it".
    """
    if count < 0:
        raise CompositionError(f"--count must not be negative (got {count})")

    exact: list[tuple[tuple[int, int, int], Slice, MediaKind, ProblemClass | None, float]] = []
    for slice_, slice_share in composition.slices.items():
        for kind, medium in composition.media.items():
            if slice_ in CLASS_BEARING:
                for problem_class, class_share in medium.classes.items():
                    share = slice_share * medium.share * class_share
                    exact.append(
                        (
                            _cell_sort_key(slice_, kind, problem_class),
                            slice_,
                            kind,
                            problem_class,
                            share,
                        )
                    )
            else:
                share = slice_share * medium.share
                exact.append((_cell_sort_key(slice_, kind, None), slice_, kind, None, share))

    exact.sort(key=lambda row: row[0])
    shares = [Fraction(row[4]) for row in exact]
    held = [0] * len(exact)
    # A zero-share cell never enters the queue, so it can never be handed a target.
    # `_normalize` guarantees at least one positive slice and medium share, and a
    # medium's class shares sum to 1, so the queue is never empty while count > 0.
    queue = [(-share, exact[index][0], index) for index, share in enumerate(shares) if share > 0]
    heapq.heapify(queue)
    for _ in range(count):
        _, key, index = heapq.heappop(queue)
        held[index] += 1
        heapq.heappush(queue, (-shares[index] / (2 * held[index] + 1), key, index))

    return tuple(
        Cell(
            slice=row[1],
            media_kind=row[2],
            problem_class=row[3],
            share=row[4],
            intended=held[position],
        )
        for position, row in enumerate(exact)
    )


def deficits(
    cells: Sequence[Cell],
    achieved: Mapping[tuple[Slice, MediaKind, ProblemClass | None], int],
    reasons: Mapping[tuple[Slice, MediaKind, ProblemClass | None], tuple[DeficitReason, str]],
) -> tuple[CompositionDeficit, ...]:
    """One row per cell that came out short, in cell order.

    A cell asked for nothing produces no row -- 45 rows of "we asked for zero" would
    bury the ones that matter. A cell that came out short with no reason recorded is
    itself a bug, so it is reported as `no_candidates` with the omission stated
    rather than skipped: house rule 12 forbids dropping what we cannot explain.
    """
    rows: list[CompositionDeficit] = []
    for cell in cells:
        key = (cell.slice, cell.media_kind, cell.problem_class)
        got = achieved.get(key, 0)
        if cell.intended <= 0 or got >= cell.intended:
            continue
        reason, detail = reasons.get(
            key,
            (
                DeficitReason.NO_CANDIDATES,
                "the cell came out short and the generator recorded no reason; this is a "
                "gap in the generator, not a fact about the library",
            ),
        )
        rows.append(
            CompositionDeficit(
                slice=cell.slice,
                media_kind=cell.media_kind,
                problem_class=cell.problem_class,
                intended=cell.intended,
                achievable=got,
                reason=reason,
                detail=detail,
            )
        )
    return tuple(rows)


__all__ = [
    "CLASS_BEARING",
    "COMPOSITION_FILE",
    "CURATED",
    "Cell",
    "Composition",
    "CompositionDeficit",
    "CompositionError",
    "DeficitReason",
    "Medium",
    "deficits",
    "load_composition",
    "parse_composition",
    "resolve",
]
