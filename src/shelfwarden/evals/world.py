"""A case's world: the library one eval case is run against.

0.6 wrote a dataset as the clean export plus one delta per case. This module turns
a case back into a library: it binds the dataset to the export it was generated
from, applies the case's delta, checks the result is a library Plex could serve,
gives it addresses Plex could have issued, and hands back a `SnapshotLibrary`.
`library/` may not import `evals/`, so the provider the agent holds knows nothing
of datasets, deltas or truth. Everything that does stays here, on the `CaseWorld`
the runner keeps.

Three decisions from `docs/plans/step-0.7-snapshot-provider.md` shape it.

**The world is the slice plus the delta** (Decision 3). Not the population:
every record a library lists must be fetchable, and `roots.jsonl` holds stubs
for roots the export never fetched.

**A world may not be less coherent than its export** (Decision 7). The structural
rules are absolute. The derived-copy rule -- a show's `child_count` agreeing with
its seasons -- is relative: a disagreement the export already had is a fact about
the source, counted on `CaseWorld.inherited_violations` and served as it is. One
the delta introduced fails the build.

**The agent sees Plex-shaped addresses** (Decision 2). 0.5 mints an added item's
rating key as `sw` plus a digest, so a human reading `truth.json` can tell which
item was minted. Served as it is, that tells the agent too: in an
`author_name_variant` case the keeper is the one member whose key is a number.
So every non-decimal key is reissued above the largest real one, and every blank
part or media id on an added item is minted the same way. The provider label
becomes `snapshot`, which `PlexLibrary` refuses, so no served address is also a
live one. `Addressing` is the one map between the two address spaces. The
dataset is unchanged.
"""

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from itertools import count
from pathlib import Path

from pydantic import ValidationError

from shelfwarden.canonical import canonical_json
from shelfwarden.evals import census as census_module
from shelfwarden.evals import export as export_module
from shelfwarden.evals.corrupt.model import CorruptionError, ItemChange
from shelfwarden.evals.corrupt.reverse import apply_changes, render_family
from shelfwarden.evals.generate import DATASET_FILE, DELTAS_FILE, SCHEMA_VERSION, Dataset
from shelfwarden.library.base import ProviderInfo
from shelfwarden.library.snapshot import (
    PROVIDER,
    SHOWN_VIOLATIONS,
    SnapshotLibrary,
    WorldIntegrityError,
    section_key,
)
from shelfwarden.models.hierarchy import (
    Violation,
    derived_violations,
    newly_violated,
    propagate,
    structural_violations,
)
from shelfwarden.models.ids import ItemId, is_decimal, item_sort_key
from shelfwarden.models.item import (
    FetchProfile,
    NormalizedItem,
    SectionRef,
    dump_item,
    load_item,
)

# The fields of a record that hold an address. A served record must carry served
# addresses in all three, or a parent link would point back into the dataset's
# address space.
ID_FIELDS: tuple[str, ...] = ("item_id", "parent", "grandparent")

# The ids a `FilePart` carries. Plex issues both from server-global sequences.
FILE_ID_FIELDS: tuple[str, ...] = ("media_id", "part_id")

WORLD_ID_CHARS = 16


class WorldError(Exception):
    """These files cannot be made into a world. The message says what to do instead.

    Distinct from `WorldIntegrityError`, which means the files were the right ones
    and the records they describe are not a library.
    """


class AddressingError(LookupError):
    """An id that names nothing in this world's address space.

    A `LookupError` the scorer can catch: an agent's finding that names an id the
    world never served is an unbound referent, not a crash.
    """


# -- addresses ----------------------------------------------------------------


class Addressing:
    """Dataset ids and served ids, one to one.

    Total over every id the case can name, which is more than the world holds: the
    export's records, the world's, and every parent they point at. A REMOVE takes
    a record out of the world, and the truth file still names it -- a soft
    postcondition asks for it to be present again -- so the scorer must be able to
    translate it.

    Decimal keys are served as they are, so a world reads against its export by
    eye. Only the label changes. The minted file ids map back to blank, which is
    what the dataset holds for them.
    """

    def __init__(
        self,
        provider: str,
        items: Mapping[ItemId, ItemId],
        files: Mapping[tuple[ItemId, int, str], str],
    ) -> None:
        dataset: dict[ItemId, ItemId] = {}
        for source, served in sorted(items.items(), key=lambda pair: _id_order(pair[0])):
            if served.provider != provider:
                raise ValueError(f"{source} is served as {served}, outside {provider!r}")
            if served in dataset:
                raise ValueError(f"{dataset[served]} and {source} would both be served as {served}")
            dataset[served] = source
        blank: dict[str, set[str]] = {field: set() for field in FILE_ID_FIELDS}
        for (item_id, index, field), minted in sorted(files.items(), key=_file_order):
            if item_id not in items or field not in blank or not is_decimal(minted):
                raise ValueError(f"cannot mint {field} {minted!r} on part {index} of {item_id}")
            if minted in blank[field]:
                raise ValueError(f"{field} {minted} is minted twice")
            blank[field].add(minted)
        self.provider = provider
        self._served = dict(items)
        self._dataset = dataset
        self._files = dict(files)
        self._blank = {field: frozenset(values) for field, values in blank.items()}

    def __len__(self) -> int:
        return len(self._served)

    def served(self, dataset_id: ItemId) -> ItemId:
        """Where the world serves the item a dataset id names."""
        served = self._served.get(dataset_id)
        if served is None:
            raise AddressingError(f"{dataset_id} names nothing this world's export or delta holds")
        return served

    def dataset(self, served_id: ItemId) -> ItemId:
        """Which dataset item a served id names."""
        source = self._dataset.get(served_id)
        if source is None:
            raise AddressingError(f"{served_id} is not an address this world served")
        return source

    def pairs(self) -> tuple[tuple[ItemId, ItemId], ...]:
        """Every `(dataset id, served id)`, in dataset-id order."""
        return tuple(sorted(self._served.items(), key=lambda pair: _id_order(pair[0])))

    def reissued(self) -> tuple[tuple[ItemId, ItemId], ...]:
        """The pairs whose rating key changed, not only their label."""
        return tuple(
            (source, served)
            for source, served in self.pairs()
            if source.rating_key != served.rating_key
        )

    @property
    def minted_files(self) -> int:
        """How many blank file ids were given one."""
        return len(self._files)

    def serve(self, record: NormalizedItem) -> NormalizedItem:
        """A dataset record as the world serves it."""
        document = dump_item(record)
        for name in ID_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                document[name] = _id_document(self.served(value))
        for index, part in enumerate(document.get("parts", ())):
            for field in FILE_ID_FIELDS:
                minted = self._files.get((record.item_id, index, field))
                if minted is None:
                    continue
                if part[field] is not None:
                    raise ValueError(
                        f"{record.item_id} part {index} already has {field} {part[field]!r}; "
                        "only a blank id is minted"
                    )
                part[field] = minted
        return load_item(document)

    def unserve(self, record: NormalizedItem) -> NormalizedItem:
        """A served record as the dataset holds it: `serve`, inverted exactly.

        A minted file id maps back to blank by value, not by position. The value
        is unique in the world and above every real one, so it cannot be mistaken
        for an id Plex issued.
        """
        document = dump_item(record)
        for name in ID_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                document[name] = _id_document(self.dataset(value))
        for part in document.get("parts", ()):
            for field in FILE_ID_FIELDS:
                if part[field] in self._blank[field]:
                    part[field] = None
        return load_item(document)


def address(
    source: Sequence[NormalizedItem],
    world: Sequence[NormalizedItem],
    provider: str = PROVIDER,
) -> Addressing:
    """Assign served addresses over an export and one world of it.

    * Every id is relabelled to `provider`.
    * A decimal rating key is kept. Any other is reissued above the largest
      decimal key in either set, in `item_sort_key` order, so the newest item has
      the highest key -- the one signal a real Plex server does carry.
    * A blank `media_id` or `part_id` on an item the world added is minted above
      the largest of its kind, in the same order. A blank the export already had
      is real data and stays blank.
    """
    ids: set[ItemId] = set()
    for record in (*source, *world):
        for name in ID_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                ids.add(value)
    fresh = count(_next_after(item_id.rating_key for item_id in ids))
    items = {
        item_id: ItemId(
            provider,
            item_id.section_id,
            item_id.rating_key if is_decimal(item_id.rating_key) else str(next(fresh)),
        )
        for item_id in sorted(ids, key=_id_order)
    }

    known = {record.item_id for record in source}
    added = sorted(
        (record for record in world if record.item_id not in known),
        key=lambda record: _id_order(record.item_id),
    )
    files: dict[tuple[ItemId, int, str], str] = {}
    for field in FILE_ID_FIELDS:
        taken = (
            getattr(part, field)
            for record in (*source, *world)
            for part in getattr(record, "parts", ())
            if getattr(part, field) is not None
        )
        minted = count(_next_after(taken))
        for record in added:
            for index, part in enumerate(getattr(record, "parts", ())):
                if getattr(part, field) is None:
                    files[(record.item_id, index, field)] = str(next(minted))
    return Addressing(provider, items, files)


def _next_after(keys: Iterable[str]) -> int:
    return max((int(key) for key in keys if is_decimal(key)), default=0) + 1


def _id_order(item_id: ItemId) -> tuple[tuple[int, int, str], tuple[int, int, str], str]:
    return (
        item_sort_key(item_id),
        census_module.section_sort_key(item_id.section_id),
        str(item_id),
    )


def _file_order(entry: tuple[tuple[ItemId, int, str], str]) -> tuple[object, ...]:
    (item_id, index, field), _ = entry
    return (_id_order(item_id), index, field)


def _id_document(item_id: ItemId) -> dict[str, str]:
    return {
        "provider": item_id.provider,
        "section_id": item_id.section_id,
        "rating_key": item_id.rating_key,
    }


# -- worlds -------------------------------------------------------------------


def render_world(
    records: Sequence[NormalizedItem], sections: Sequence[SectionRef], profile: FetchProfile
) -> bytes:
    """A served world as bytes: what `world_id` hashes.

    The sections and the profile are part of it. Two record sets served under
    different sections are different libraries, and so are the same records held
    at two profiles. The records follow in family order, a pure function of the
    ids (`reverse.render_family`).
    """
    header = canonical_json(
        {
            "profile": str(profile),
            "sections": [
                section.model_dump(mode="json") for section in sorted(sections, key=section_key)
            ],
        }
    )
    return header + b"\n" + render_family(records)


@dataclass(frozen=True, slots=True)
class CaseWorld:
    """One case's library, and what the runner needs beside it.

    `provider` is the only thing the agent receives. `addressing` is for the
    scorer, which names items in the dataset's address space. `world_id` is the
    served world's digest and the provider's `server_id`, so a run records which
    library it saw. `inherited_violations` are the derived-copy disagreements the
    export already had, served as they were, in dataset ids.
    """

    provider: SnapshotLibrary
    addressing: Addressing
    world_id: str
    size: int
    export_id: str
    dataset_id: str | None
    case_id: str | None
    inherited_violations: tuple[Violation, ...]


@dataclass(frozen=True, slots=True)
class ExportedLibrary:
    """An export, read once and checked against its own manifest.

    Every world is built from this, so parsing the export happens once per dataset
    rather than once per case. `items_sha256` is hashed from the bytes read, never
    copied from the manifest: a stale manifest is the failure binding exists to
    catch.
    """

    directory: Path
    manifest: export_module.Manifest
    items_sha256: str
    items: tuple[NormalizedItem, ...]
    sections: tuple[SectionRef, ...]
    structural: tuple[Violation, ...]
    derived: tuple[Violation, ...]

    @classmethod
    def read(cls, directory: Path) -> "ExportedLibrary":
        if not (directory / export_module.MANIFEST_FILE).exists():
            raise WorldError(
                f"{directory} is not an export directory: no {export_module.MANIFEST_FILE}. "
                "Point this at a directory written by `shelfwarden export`."
            )
        manifest = export_module.load_manifest(directory)
        if manifest.selection.mode == "census":
            raise WorldError(
                f"{directory} is a census-only export: it holds no items, and its world would "
                "score every case as silence. Re-run `shelfwarden export` without "
                "--census-only."
            )
        payload = (directory / export_module.ITEMS_FILE).read_bytes()
        digest = sha256(payload).hexdigest()
        if digest != manifest.items_sha256:
            raise WorldError(
                f"{directory / export_module.ITEMS_FILE} hashes to {digest}, but its manifest "
                f"records {manifest.items_sha256}. The export was edited or truncated after it "
                "was written; re-run `shelfwarden export`."
            )
        items = tuple(load_item(line) for line in payload.splitlines() if line.strip())
        label = manifest.provider.provider
        foreign = sorted(
            {
                str(value)
                for record in items
                for name in ID_FIELDS
                if (value := getattr(record, name, None)) is not None and value.provider != label
            }
        )
        if foreign:
            raise WorldError(
                f"{directory} was exported from provider {label!r}, but {len(foreign)} id(s) "
                f"in it carry another label, first {', '.join(foreign[:SHOWN_VIOLATIONS])}. "
                "Re-run `shelfwarden export`."
            )
        return cls(
            directory=directory,
            manifest=manifest,
            items_sha256=digest,
            items=items,
            sections=tuple(
                SectionRef(
                    section_id=section.section_id,
                    title=section.title,
                    section_type=section.section_type,
                    agent=section.agent,
                )
                for section in manifest.sections
            ),
            structural=structural_violations(items),
            derived=derived_violations(items),
        )

    def world(
        self,
        changes: Sequence[ItemChange] = (),
        *,
        case_id: str | None = None,
        dataset_id: str | None = None,
    ) -> CaseWorld:
        """The export with `changes` applied, checked, addressed and served.

        With no changes, the export's own world -- what a should-not-touch case
        sees, and what the integrity report builds first.
        """
        name = f"case {case_id}" if case_id else "the export's own world"
        try:
            records = apply_changes(self.items, changes)
        except CorruptionError as exc:
            raise WorldError(f"{name}: {exc}") from exc

        broken = structural_violations(records)
        derived = derived_violations(records)
        introduced = newly_violated(derived, self.derived)
        if broken or introduced:
            raise WorldIntegrityError(
                sorted({*broken, *introduced}), note=self._diagnose(name, records)
            )

        addressing = address(self.items, records)
        served = tuple(addressing.serve(record) for record in records)
        profile = self.manifest.profile
        world_id = sha256(render_world(served, self.sections, profile)).hexdigest()
        world_id = world_id[:WORLD_ID_CHARS]
        known = {violation.key for violation in self.derived}
        return CaseWorld(
            provider=SnapshotLibrary(
                served, self.sections, ProviderInfo(provider=PROVIDER, server_id=world_id), profile
            ),
            addressing=addressing,
            world_id=world_id,
            size=len(served),
            export_id=self.manifest.export_id,
            dataset_id=dataset_id,
            case_id=case_id,
            inherited_violations=tuple(v for v in derived if v.key in known),
        )

    def _diagnose(self, name: str, records: Sequence[NormalizedItem]) -> str:
        """What to do about a world that failed integrity.

        If propagating derived copies over it would leave nothing broken, the delta
        is one the recipes have repaired since step 0.7.5, and regenerating is the
        fix. Otherwise a recipe, or a hand edit, broke the tree in a way
        propagation cannot reach.
        """
        repaired, _ = propagate(self.items, records)
        if structural_violations(repaired) or newly_violated(
            derived_violations(repaired), self.derived
        ):
            return (
                f"{name}: the delta breaks the tree in a way propagation cannot repair. "
                "Check the recipe that produced it, or whether deltas.jsonl was edited."
            )
        return (
            f"{name}: every one of these is a copy the propagation pass repairs (step 0.7.5), "
            "so this delta was most likely recorded before it. Regenerate the dataset."
        )


@dataclass(frozen=True, slots=True)
class WorldBuilder:
    """A dataset bound to the export it was generated from.

    `open` reads `dataset.json` and `deltas.jsonl`, and never `truth.json`: the
    answer key is the scorer's, and nothing here needs it. `deltas` holds each
    case's line, unparsed, so a case's changes are parsed only when its world is
    built.
    """

    export: ExportedLibrary
    dataset: Dataset
    deltas: Mapping[str, tuple[bytes, ...]]

    @classmethod
    def open(cls, export_directory: Path, dataset_directory: Path) -> "WorldBuilder":
        return cls.bind(ExportedLibrary.read(export_directory), dataset_directory)

    @classmethod
    def bind(cls, export: ExportedLibrary, dataset_directory: Path) -> "WorldBuilder":
        """`open`, over an export already read."""
        dataset_path = dataset_directory / DATASET_FILE
        if not dataset_path.exists():
            raise WorldError(
                f"{dataset_directory} is not a dataset directory: no {DATASET_FILE}. Point "
                "this at a directory written by `python -m shelfwarden.evals.generate`."
            )
        dataset = Dataset.model_validate_json(dataset_path.read_bytes())
        if dataset.schema_version != SCHEMA_VERSION:
            raise WorldError(
                f"{dataset_path} is dataset schema version {dataset.schema_version}, and this "
                f"code reads version {SCHEMA_VERSION}. Regenerate the dataset."
            )
        bound = dataset.source_export
        if bound.items_sha256 != export.items_sha256:
            raise WorldError(
                f"dataset {dataset.dataset_id} was generated from export {bound.export_id}, "
                f"whose items hash to {bound.items_sha256}. {export.directory} holds export "
                f"{export.manifest.export_id}, whose items hash to {export.items_sha256}. Pass "
                "the export the dataset was generated from, or regenerate the dataset from "
                "this one."
            )

        lines: dict[str, list[bytes]] = {}
        payload = (dataset_directory / DELTAS_FILE).read_bytes()
        for number, line in enumerate(payload.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                case_id = json.loads(line)["case_id"]
            except (ValueError, KeyError, TypeError) as exc:
                raise WorldError(
                    f"{DELTAS_FILE} line {number} is not a delta ({exc!r}). The file has been "
                    "edited; regenerate the dataset."
                ) from exc
            lines.setdefault(case_id, []).append(line)
        if len(lines) != dataset.counts.cases:
            raise WorldError(
                f"{DELTAS_FILE} holds deltas for {len(lines)} case(s), and {DATASET_FILE} counts "
                f"{dataset.counts.cases}. The file has been truncated or edited; regenerate "
                "the dataset."
            )
        return cls(
            export=export,
            dataset=dataset,
            deltas={case_id: tuple(found) for case_id, found in sorted(lines.items())},
        )

    def case_ids(self) -> tuple[str, ...]:
        return tuple(self.deltas)

    def world(self, case_id: str) -> CaseWorld:
        found = self.deltas.get(case_id, ())
        if not found:
            raise WorldError(
                f"{case_id} has no delta in {DELTAS_FILE}, so it is not a case of dataset "
                f"{self.dataset.dataset_id}. Take case ids from that dataset's truth.json."
            )
        if len(found) > 1:
            raise WorldError(
                f"{case_id} has {len(found)} lines in {DELTAS_FILE}. A case is one delta, and "
                "which one to apply would be a guess. The file has been edited; regenerate "
                "the dataset."
            )
        try:
            changes = tuple(
                ItemChange.model_validate(change) for change in json.loads(found[0])["changes"]
            )
        except (ValueError, KeyError, TypeError, ValidationError, CorruptionError) as exc:
            raise WorldError(
                f"{case_id}'s line in {DELTAS_FILE} is not a delta ({exc}). The file has been "
                "edited; regenerate the dataset."
            ) from exc
        return self.export.world(changes, case_id=case_id, dataset_id=self.dataset.dataset_id)


def world_for_case(export_directory: Path, dataset_directory: Path, case_id: str) -> CaseWorld:
    """One case's world, from scratch. 0.6's one-shot interface.

    Parses the whole export. A runner building many worlds opens a `WorldBuilder`
    once and asks it per case.
    """
    return WorldBuilder.open(export_directory, dataset_directory).world(case_id)


# -- the integrity report -----------------------------------------------------


def _tallied(violations: Sequence[Violation]) -> list[str]:
    """Counts by rule and field, then examples. Every count is shown; the examples
    are capped, and the cap says how many it left out."""
    counts = Counter(f"{violation.rule}{violation.path}" for violation in violations)
    lines = [
        f"    {label}: {n}"
        for label, n in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    lines += [f"      {violation}" for violation in violations[:SHOWN_VIOLATIONS]]
    if len(violations) > SHOWN_VIOLATIONS:
        lines.append(f"      ...and {len(violations) - SHOWN_VIOLATIONS} more")
    return lines


def _describe(world: CaseWorld) -> str:
    return (
        f"world {world.world_id}  {world.size} records  "
        f"{len(world.addressing.reissued())} key(s) reissued  "
        f"{world.addressing.minted_files} file id(s) minted  "
        f"{len(world.inherited_violations)} inherited"
    )


def report(export_directory: Path, dataset_directory: Path | None = None) -> tuple[str, bool]:
    """The integrity report: the export alone, then every case world. `(text, ok)`.

    `ok` is false when the export breaks a structural rule or any world cannot be
    built. An inherited derived-copy disagreement is counted, never a failure.
    """
    export = ExportedLibrary.read(export_directory)
    manifest = export.manifest
    lines = [
        f"export {manifest.export_id} ({manifest.provider.provider}, {manifest.profile}): "
        f"{len(export.items)} records in {len(export.sections)} section(s)",
        f"  structural violations: {len(export.structural)}",
        *_tallied(export.structural),
        f"  derived-copy disagreements, inherited by every world: {len(export.derived)}",
        *_tallied(export.derived),
    ]
    ok = not export.structural
    try:
        lines.append(f"  own world: {_describe(export.world())}")
    except (WorldError, WorldIntegrityError) as exc:
        lines.append(f"  own world: REFUSED\n{exc}")
        ok = False
    if dataset_directory is None:
        return "\n".join(lines), ok

    builder = WorldBuilder.bind(export, dataset_directory)
    refused = 0
    started = time.perf_counter()
    lines.append(f"dataset {builder.dataset.dataset_id}: {len(builder.case_ids())} case(s)")
    for case_id in builder.case_ids():
        try:
            lines.append(f"  {case_id}  {_describe(builder.world(case_id))}")
        except (WorldError, WorldIntegrityError) as exc:
            refused += 1
            lines.append(f"  {case_id}  REFUSED\n{exc}")
    elapsed = time.perf_counter() - started
    built = len(builder.case_ids()) - refused
    lines.append(f"  {built} built, {refused} refused, in {elapsed:.2f} s")
    return "\n".join(lines), ok and not refused


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m shelfwarden.evals.world",
        description=(
            "Report whether an export, and every case world of a dataset generated from "
            "it, is a library Plex could serve."
        ),
    )
    parser.add_argument(
        "export", type=Path, help="Export directory written by `shelfwarden export`."
    )
    parser.add_argument(
        "dataset", type=Path, nargs="?", default=None, help="Dataset generated from that export."
    )
    args = parser.parse_args(argv)
    try:
        text, ok = report(args.export, args.dataset)
    except WorldError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(text)
    return 0 if ok else 1


__all__ = [
    "FILE_ID_FIELDS",
    "ID_FIELDS",
    "WORLD_ID_CHARS",
    "Addressing",
    "AddressingError",
    "CaseWorld",
    "ExportedLibrary",
    "WorldBuilder",
    "WorldError",
    "address",
    "main",
    "render_world",
    "report",
    "world_for_case",
]


if __name__ == "__main__":  # pragma: no cover -- exercised as a subprocess
    raise SystemExit(main())
