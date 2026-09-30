"""The two slices a generator cannot synthesize.

The `real` (25%) and `ambiguous` (10%) slices are human-curated: a real case's
ground truth is a person's judgement about a genuine library problem, and an
ambiguous case is one where the *answer* is legitimately contested. Neither derives
from an export, so `generate` merges them from files rather than producing them.

**TOML, not YAML.** The spec names `datasets/curated/real.yaml`. YAML would be a
new runtime dependency for two files read once at generation time, and
`composition.toml` has already established TOML here with `tomllib` in the standard
library. Multi-line strings and arrays of tables cover what an adjudication record
needs. Recorded as a spec deviation rather than silently made, and cheap to
revisit: step 0.9 owns the adjudication format and is the step that will know
whether a human editing 60-second records wants YAML's ergonomics badly enough to
pay for the dependency.

**Both files ship empty at step 0.6**, and an empty file is not an error: it yields
an empty slice and a `not_curated` deficit row. That distinction matters -- folding
it into `no_candidates` would report "your library has no such items" when the
truth is "nobody has labelled any yet".

A curated case supplies only what a human knows: which items, which class, what the
expectation is, and where the label came from. Everything mechanical -- the ground
truth family, the media kind, the subject key, `case_id`, `run_group` -- is bound
to the export by the generator, exactly as it is for a synthetic case. So a curated
case cannot carry an identity that disagrees with the library it describes.
"""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from shelfwarden.evals.truth import Expectation, Provenance, Slice
from shelfwarden.models.finding import ProblemClass

CURATED_ROOT = Path("datasets/curated")
CURATED_FILES: dict[Slice, str] = {Slice.REAL: "real.toml", Slice.AMBIGUOUS: "ambiguous.toml"}
SCHEMA_VERSION = 1


class CuratedError(Exception):
    """A curated slice file could not be read. Every message names the next action."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CuratedCase(_Frozen):
    """One hand-labelled case, before the generator binds it to an export.

    `item_ids` is the only address a human writes, and it is a *live* address --
    rating keys move on rescan (invariant 9). So a curated case whose ids no longer
    resolve in the export **stops generation** with a correctable error naming the
    case (`generate._case_from_curated`), rather than shipping: the semantic
    identity is derived from the export record the ids point at, and there is
    nothing to derive it from if they point at nothing. This docstring first said
    such a case became a deficit row; the code never did that, and step 0.6's tests
    pin what it does. Whether a stale label should instead degrade to a deficit is
    step 0.9's call, once there are labels to go stale.
    """

    item_ids: tuple[str, ...]
    problem_class: ProblemClass | None = None
    expectation: Expectation
    provenance: Provenance
    notes: str | None = None


class CuratedFile(_Frozen):
    schema_version: int = SCHEMA_VERSION
    cases: tuple[CuratedCase, ...] = ()


def parse_curated(payload: bytes, slice_: Slice) -> CuratedFile:
    try:
        data = tomllib.loads(payload.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise CuratedError(f"the {slice_} curated slice is not readable TOML: {exc}") from exc

    unknown = set(data) - {"schema_version", "case"}
    if unknown:
        raise CuratedError(
            f"the {slice_} curated slice has unexpected table(s) {sorted(unknown)}; it holds "
            "`schema_version` and a `[[case]]` array and nothing else."
        )
    version = data.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise CuratedError(
            f"the {slice_} curated slice is schema_version {version}, not {SCHEMA_VERSION}. "
            "Update the file, or check out the generator version that wrote it."
        )
    try:
        return CuratedFile(schema_version=version, cases=tuple(data.get("case", ())))
    except ValueError as exc:
        raise CuratedError(
            f"a case in the {slice_} curated slice does not validate: {exc}. A curated "
            "expectation uses the same vocabulary as a generated one -- see "
            "`evals/truth.py` -- so that the scorer cannot tell them apart."
        ) from exc


def load_curated(slice_: Slice, root: Path = CURATED_ROOT) -> CuratedFile:
    """Read one curated slice. An empty file is legal; a missing one is not.

    A missing file means the checkout is incomplete rather than the queue is empty,
    and those want different next actions.
    """
    if slice_ not in CURATED_FILES:
        raise CuratedError(
            f"{slice_} is not a curated slice; the curated slices are "
            f"{', '.join(str(name) for name in CURATED_FILES)}."
        )
    path = root / CURATED_FILES[slice_]
    try:
        payload = path.read_bytes()
    except FileNotFoundError as exc:
        raise CuratedError(
            f"no curated slice at {path}. The file is committed and ships empty; restore it "
            "(a `schema_version` line and no cases is valid) or pass --curated to point "
            "somewhere else."
        ) from exc
    return parse_curated(payload, slice_)


__all__ = [
    "CURATED_FILES",
    "CURATED_ROOT",
    "SCHEMA_VERSION",
    "CuratedCase",
    "CuratedError",
    "CuratedFile",
    "load_curated",
    "parse_curated",
]
