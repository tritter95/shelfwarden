"""Turning a live, mutable, network-dependent library into a file that does not change.

Everything downstream reads this file and nothing else: the screen (0.45), the
corruptions (0.5), the truth files (0.6), `SnapshotLibrary` (0.7), the scorer
(0.8). That makes determinism the whole job -- an export whose bytes drift is a
silently moving baseline, and the project's relative CI gate ("no case that
passed may now fail") becomes decorative.

Two structural choices carry most of the weight:

* **This module depends only on `LibraryProvider`**, never on `PlexLibrary`. That
  is what lets the byte-identity test run offline against a fixture-backed fake,
  and it hands `SnapshotLibrary` the export for free. An import contract enforces
  it rather than intent.
* **The unit of selection is a family, not an item.** A show missing its last four
  episodes is an unsolvable case in 0.5 and a mysteriously depressed score in 0.8,
  so a family that will not fit the budget is dropped whole and recorded -- never
  truncated.
"""

import json
import os
import random
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from shelfwarden import __version__
from shelfwarden.canonical import canonical_json
from shelfwarden.config import iter_secret_hits
from shelfwarden.evals import census as census_module
from shelfwarden.library.base import (
    LibraryItemNotFound,
    LibraryProvider,
    LibraryUnsupported,
    ProviderInfo,
)
from shelfwarden.models.ids import ItemId
from shelfwarden.models.item import (
    FetchProfile,
    ItemStub,
    MediaKind,
    NormalizedItem,
    SectionRef,
    dump_item,
    load_item,
)

ITEMS_FILE = "items.jsonl"
ROOTS_FILE = "roots.jsonl"
MANIFEST_FILE = "manifest.json"
CENSUS_FILE = "census.json"
CENSUS_MARKDOWN_FILE = "census.md"

DEFAULT_SEED = 1518
DEFAULT_ROOTS = 200
DEFAULT_MAX_RECORDS = 5000
PAGE_SIZE = 100

# The one manifest field expected to differ between two exports of an unchanged
# library. Named once, here, so the determinism test does not grow a list of
# exceptions that quietly absorbs a real regression.
VOLATILE_MANIFEST_FIELDS: frozenset[str] = frozenset({"created_at"})

# Which media kind is a section's root, and what hangs beneath it. Movie families
# are a single record; the other two are three levels deep and structurally
# identical to each other.
SECTION_ROOT_KIND: dict[str, MediaKind] = {
    "movie": MediaKind.MOVIE,
    "show": MediaKind.SHOW,
    "artist": MediaKind.AUTHOR,
}

CHILD_KIND: dict[MediaKind, MediaKind] = {
    MediaKind.SHOW: MediaKind.SEASON,
    MediaKind.SEASON: MediaKind.EPISODE,
    MediaKind.AUTHOR: MediaKind.AUDIOBOOK,
    MediaKind.AUDIOBOOK: MediaKind.AUDIOBOOK_PART,
}

# Ordering only. Root kinds sort before their descendants so a family reads
# top-down in the JSONL.
KIND_RANK: dict[MediaKind, int] = {
    MediaKind.MOVIE: 0,
    MediaKind.SHOW: 0,
    MediaKind.AUTHOR: 0,
    MediaKind.SEASON: 1,
    MediaKind.AUDIOBOOK: 1,
    MediaKind.EPISODE: 2,
    MediaKind.AUDIOBOOK_PART: 2,
}

RECORD_ORDER = "section_id, root_key, kind_rank, item_key"


class ExportError(Exception):
    """The export could not be completed. Nothing was written."""


# -- models ---------------------------------------------------------------


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SectionQuota(_Frozen):
    section_id: str
    population: int
    quota: int


class SelectionPlan(_Frozen):
    """How the slice was chosen, recorded so it can be argued with."""

    mode: str
    seed: int
    requested_roots: int | None
    max_records: int
    per_section: tuple[SectionQuota, ...]


class SectionManifest(_Frozen):
    section_id: str
    title: str
    section_type: str
    agent: str
    population: int
    exported_roots: int
    exported_records: int


class SkippedSection(_Frozen):
    section_id: str
    title: str
    reason: str


class DroppedFamily(_Frozen):
    """A family the export refused to half-write. See the module docstring."""

    root: str
    title: str
    records: int
    reason: str


class ExportCounts(_Frozen):
    roots: int
    records: int
    by_media_kind: dict[str, int]


class Manifest(_Frozen):
    """What produced these records.

    `schema_version` is 2 from step 0.45, which added `roots.jsonl` and the
    `roots_sha256` that binds it. `roots_sha256` is optional so a version-1
    export still loads -- a screen run against one reports its uniqueness
    predicates as `unavailable` rather than failing to parse the manifest, which
    is the difference between an honest gap and a broken tool.

    `request_params` is the **effective** parameter set -- what plexapi actually
    puts on the wire -- not our override dict. Recording the override dict would
    understate the request while looking authoritative; see
    `library.plex.effective_request_params`.
    """

    schema_version: int = 2
    export_id: str
    created_at: datetime
    shelfwarden_version: str
    git_sha: str | None
    git_dirty: bool
    provider: ProviderInfo
    profile: FetchProfile
    request_params: dict[str, str]
    record_order: str = RECORD_ORDER
    selection: SelectionPlan
    sections: tuple[SectionManifest, ...]
    skipped_sections: tuple[SkippedSection, ...]
    counts: ExportCounts
    dropped: tuple[DroppedFamily, ...]
    items_sha256: str
    census_sha256: str
    roots_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class Family:
    """A root item and everything beneath it, kept together or not at all."""

    root: ItemStub
    records: tuple[NormalizedItem, ...]

    @property
    def size(self) -> int:
        return len(self.records)


@dataclass(frozen=True, slots=True)
class ExportResult:
    directory: Path
    manifest: Manifest
    census: census_module.Census
    items: tuple[NormalizedItem, ...]


# -- ordering -------------------------------------------------------------


def item_sort_key(item_id: ItemId) -> tuple[int, int, str]:
    """Numeric rating keys sort numerically; anything else sorts after, by text.

    Lexicographic ordering alone would be deterministic but would put `"10"`
    before `"9"`, which makes a hand-read of the JSONL needlessly confusing for no
    gain.
    """
    key = item_id.rating_key
    return (0, int(key), "") if key.isdigit() else (1, 0, key)


def _stub_sort_key(stub: ItemStub) -> tuple[tuple[int, int, str], tuple[int, int, str]]:
    return (
        census_module.section_sort_key(stub.item_id.section_id),
        item_sort_key(stub.item_id),
    )


def _record_sort_key(
    root: ItemStub, record: NormalizedItem
) -> tuple[tuple[int, int, str], tuple[int, int, str], int, tuple[int, int, str]]:
    return (
        census_module.section_sort_key(record.item_id.section_id),
        item_sort_key(root.item_id),
        KIND_RANK[record.media_kind],
        item_sort_key(record.item_id),
    )


# -- listing and selection ------------------------------------------------


def list_all(provider: LibraryProvider, section_id: str, kind: MediaKind) -> tuple[ItemStub, ...]:
    """Every root stub in a section. Paged, both arguments, to exhaustion.

    Exhaustive rather than sampled because this *is* the population tier of the
    census: a sampled population reported as exact would be the census lying about
    its own basis.
    """
    stubs: list[ItemStub] = []
    offset = 0
    while True:
        page = provider.list_items(section_id, offset=offset, limit=PAGE_SIZE, media_kind=kind)
        stubs.extend(page.items)
        offset += page.returned
        if page.returned == 0 or offset >= page.total:
            break
    return tuple(stubs)


def allocate(populations: Sequence[tuple[str, int]], requested: int) -> dict[str, int]:
    """Split a root budget across sections, proportional to population.

    Largest-remainder, ties broken by `section_id`, so the allocation is a pure
    function of the inputs. A section is never allocated more roots than it has.
    """
    total = sum(population for _, population in populations)
    if total <= 0 or requested <= 0:
        return {section_id: 0 for section_id, _ in populations}
    if requested >= total:
        return {section_id: population for section_id, population in populations}

    exact = {section_id: requested * population / total for section_id, population in populations}
    quotas = {section_id: int(value) for section_id, value in exact.items()}
    remaining = requested - sum(quotas.values())
    order = sorted(
        populations,
        key=lambda entry: (-(exact[entry[0]] - quotas[entry[0]]), entry[0]),
    )
    for section_id, population in order:
        if remaining <= 0:
            break
        if quotas[section_id] < population:
            quotas[section_id] += 1
            remaining -= 1
    return quotas


def select(
    stubs: Sequence[ItemStub], quota: int, seed: int, section_id: str
) -> tuple[ItemStub, ...]:
    """Choose `quota` roots reproducibly.

    The RNG is seeded per section so adding a section does not reshuffle the
    others, and it chooses *membership* only -- the result is re-sorted afterwards
    so ordering never depends on the draw.
    """
    if quota >= len(stubs):
        chosen = list(stubs)
    else:
        rng = random.Random(f"{seed}:{section_id}")
        chosen = rng.sample(list(stubs), quota)
    return tuple(sorted(chosen, key=_stub_sort_key))


# -- the walk -------------------------------------------------------------


def _children(provider: LibraryProvider, item_id: ItemId) -> tuple[ItemStub, ...]:
    stubs: list[ItemStub] = []
    offset = 0
    while True:
        page = provider.get_children(item_id, offset=offset, limit=PAGE_SIZE)
        stubs.extend(page.items)
        offset += page.returned
        if page.returned == 0 or offset >= page.total:
            break
    return tuple(stubs)


def fetch_family(
    provider: LibraryProvider,
    root: ItemStub,
    profile: FetchProfile,
) -> Family:
    """A root and every descendant, fetched at one profile.

    Raises `LibraryItemNotFound` if any member has gone -- a rating key moving
    mid-export is exactly the case where a partial family would be written, and
    the caller drops the whole family rather than keep what it got.
    """
    records: list[NormalizedItem] = []
    frontier: list[ItemStub] = [root]
    while frontier:
        stub = frontier.pop(0)
        records.append(provider.get_item(stub.item_id, profile))
        if stub.media_kind in CHILD_KIND:
            frontier.extend(_children(provider, stub.item_id))
    return Family(root=root, records=tuple(records))


# -- provenance -----------------------------------------------------------


def git_state(root: Path | None = None) -> tuple[str | None, bool]:
    """The exporter's commit, and whether the tree was dirty when it ran.

    `(None, True)` outside a git checkout: unknown provenance is treated as dirty
    because a record that cannot name the code that produced it should not read as
    cleanly reproducible.
    """
    cwd = str(root) if root else None
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        if sha.returncode != 0:
            return None, True
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        dirty = status.returncode != 0 or bool(status.stdout.strip())
        return sha.stdout.strip() or None, dirty
    except (OSError, subprocess.SubprocessError):
        return None, True


def _digest(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def render_items(items: Iterable[NormalizedItem]) -> bytes:
    """One canonical-JSON record per line."""
    return b"".join(canonical_json(dump_item(item)) + b"\n" for item in items)


def render_roots(roots: Iterable[ItemStub]) -> bytes:
    """The population index: every root stub in every supported section.

    Written because three of the mechanical screen's predicates are *uniqueness*
    claims -- "no other item shares this normalized (title, year)" -- and
    `items.jsonl` is a **slice**. An item whose duplicate simply was not sampled
    would be marked guarded against `duplicate_quality`, and the agent's correct
    finding on it would then score as a false positive: the metric inverted, in
    the one direction this project has forbidden. The population walk already
    visits every root and `ItemStub` already carries the four fields a
    uniqueness index needs, so this costs one file and no extra requests.
    """
    return b"".join(canonical_json(stub.model_dump(mode="json")) + b"\n" for stub in roots)


# -- the export -----------------------------------------------------------


def default_directory(base: Path, now: datetime) -> Path:
    return base / now.strftime("%Y-%m-%dT%H-%MZ")


def run_export(
    provider: LibraryProvider,
    out: Path,
    *,
    profile: FetchProfile = FetchProfile.CORE,
    request_params: dict[str, str] | None = None,
    count: int | None = DEFAULT_ROOTS,
    seed: int = DEFAULT_SEED,
    max_records: int = DEFAULT_MAX_RECORDS,
    sections: Sequence[str] = (),
    census_only: bool = False,
    secrets: tuple[str, ...] = (),
    now: datetime | None = None,
    git_root: Path | None = None,
) -> ExportResult:
    """Walk, select, fetch, and write. Atomic: on failure nothing is written."""
    created_at = now or datetime.now(UTC)
    wanted = set(sections)

    supported: list[SectionRef] = []
    skipped: list[SkippedSection] = []
    populations: dict[str, int] = {}
    root_kinds: dict[str, MediaKind] = {}
    stubs_by_section: dict[str, tuple[ItemStub, ...]] = {}

    for section in provider.sections():
        if wanted and section.section_id not in wanted:
            skipped.append(
                SkippedSection(
                    section_id=section.section_id,
                    title=section.title,
                    reason="not requested (--section)",
                )
            )
            continue
        kind = SECTION_ROOT_KIND.get(section.section_type)
        if kind is None:
            skipped.append(
                SkippedSection(
                    section_id=section.section_id,
                    title=section.title,
                    reason=(
                        f"{section.section_type!r} sections are not modelled; "
                        "ShelfWarden handles movie, show, and audiobook (artist) sections"
                    ),
                )
            )
            continue
        try:
            stubs = list_all(provider, section.section_id, kind)
        except LibraryUnsupported as exc:
            # An artist section that failed audiobook detection. The verdict's own
            # explanation travels in the message, so the skip is arguable rather
            # than a bare refusal.
            skipped.append(
                SkippedSection(section_id=section.section_id, title=section.title, reason=str(exc))
            )
            continue
        supported.append(section)
        stubs_by_section[section.section_id] = stubs
        populations[section.section_id] = len(stubs)
        root_kinds[section.section_id] = kind

    if not supported:
        raise ExportError(
            "no supported sections. ShelfWarden reads movie, show, and audiobook "
            "(artist) sections; check --section, and check that the token can see "
            "the library."
        )

    ordered_populations = sorted(
        ((section.section_id, populations[section.section_id]) for section in supported),
        key=lambda entry: census_module.section_sort_key(entry[0]),
    )
    # Every root in every supported section, in the export's own order. Built
    # here rather than from `families` because the point of the file is that it
    # is the *population*, not the slice -- see `render_roots`.
    roots = tuple(
        sorted(
            (
                stub
                for section_id, _ in ordered_populations
                for stub in stubs_by_section[section_id]
            ),
            key=_stub_sort_key,
        )
    )
    if census_only:
        quotas = {section_id: 0 for section_id, _ in ordered_populations}
    elif count is None:
        quotas = {section_id: population for section_id, population in ordered_populations}
    else:
        quotas = allocate(ordered_populations, count)

    plan = SelectionPlan(
        mode="all"
        if count is None and not census_only
        else ("census" if census_only else "sample"),
        seed=seed,
        requested_roots=None if count is None else count,
        max_records=max_records,
        per_section=tuple(
            SectionQuota(section_id=section_id, population=population, quota=quotas[section_id])
            for section_id, population in ordered_populations
        ),
    )

    families: list[Family] = []
    dropped: list[DroppedFamily] = []
    records_used = 0

    if not census_only:
        for section_id, _ in ordered_populations:
            chosen = select(stubs_by_section[section_id], quotas[section_id], seed, section_id)
            for stub in chosen:
                try:
                    family = fetch_family(provider, stub, profile)
                except LibraryItemNotFound as exc:
                    dropped.append(
                        DroppedFamily(
                            root=str(stub.item_id),
                            title=stub.title,
                            records=0,
                            reason=f"member disappeared mid-export: {exc}",
                        )
                    )
                    continue
                if records_used + family.size > max_records:
                    dropped.append(
                        DroppedFamily(
                            root=str(stub.item_id),
                            title=stub.title,
                            records=family.size,
                            reason=f"would exceed max_records ({max_records})",
                        )
                    )
                    continue
                families.append(family)
                records_used += family.size

    pairs = [(family.root, record) for family in families for record in family.records]
    pairs.sort(key=lambda pair: _record_sort_key(*pair))
    items = tuple(record for _, record in pairs)

    duplicates = _duplicate_ids(items)
    if duplicates:
        raise ExportError(
            f"the same item_id appears more than once: {', '.join(duplicates)}. "
            "A duplicated record would make the export ambiguous about what it holds."
        )

    exported_roots: dict[str, int] = {}
    exported_records: dict[str, int] = {}
    for family in families:
        section_id = family.root.item_id.section_id
        exported_roots[section_id] = exported_roots.get(section_id, 0) + 1
        exported_records[section_id] = exported_records.get(section_id, 0) + family.size

    census = census_module.build(
        sections=tuple(supported),
        populations=populations,
        root_kinds=root_kinds,
        items=None if census_only else items,
        exported_roots=exported_roots,
        exported_records=exported_records,
    )

    items_bytes = render_items(items)
    roots_bytes = render_roots(roots)
    census_bytes = canonical_json(census.model_dump(mode="json"))
    git_sha, git_dirty = git_state(git_root)

    by_kind: dict[str, int] = {}
    for item in items:
        by_kind[str(item.media_kind)] = by_kind.get(str(item.media_kind), 0) + 1

    manifest = Manifest(
        export_id="exp-" + _digest(items_bytes)[:12],
        created_at=created_at,
        shelfwarden_version=__version__,
        git_sha=git_sha,
        git_dirty=git_dirty,
        provider=provider.provider_info(),
        profile=profile,
        request_params=dict(request_params or {}),
        selection=plan,
        sections=tuple(
            SectionManifest(
                section_id=section.section_id,
                title=section.title,
                section_type=section.section_type,
                agent=section.agent,
                population=populations[section.section_id],
                exported_roots=exported_roots.get(section.section_id, 0),
                exported_records=exported_records.get(section.section_id, 0),
            )
            for section in sorted(
                supported, key=lambda s: census_module.section_sort_key(s.section_id)
            )
        ),
        skipped_sections=tuple(
            sorted(skipped, key=lambda s: census_module.section_sort_key(s.section_id))
        ),
        counts=ExportCounts(
            roots=len(families),
            records=len(items),
            by_media_kind=dict(sorted(by_kind.items())),
        ),
        dropped=tuple(sorted(dropped, key=lambda d: d.root)),
        items_sha256=_digest(items_bytes),
        census_sha256=_digest(census_bytes),
        roots_sha256=_digest(roots_bytes),
    )
    manifest_bytes = canonical_json(manifest.model_dump(mode="json"))
    markdown_bytes = census_module.render_markdown(census).encode("utf-8")

    payloads = {
        ITEMS_FILE: items_bytes,
        # Written under --census-only too, where it is the only item-shaped
        # artifact and is what makes that mode useful to the screen at all.
        ROOTS_FILE: roots_bytes,
        MANIFEST_FILE: manifest_bytes,
        CENSUS_FILE: census_bytes,
        CENSUS_MARKDOWN_FILE: markdown_bytes,
    }
    _assert_no_secrets(payloads, secrets)
    write_atomically(out, payloads)

    return ExportResult(directory=out, manifest=manifest, census=census, items=items)


def _duplicate_ids(items: Sequence[NormalizedItem]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in items:
        key = str(item.item_id)
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return sorted(duplicates)


def _assert_no_secrets(payloads: dict[str, bytes], secrets: tuple[str, ...]) -> None:
    """Refuse to write a token into a dataset directory.

    Practices §9 says assert it rather than trust review. Checked before anything
    lands, so a leak aborts the export rather than being cleaned up afterwards.
    """
    for name, payload in sorted(payloads.items()):
        if any(iter_secret_hits(payload, secrets)):
            raise ExportError(
                f"{name} contains a configured secret; refusing to write. This is a "
                "bug in the export, not in your configuration."
            )


def write_atomically(out: Path, payloads: dict[str, bytes]) -> None:
    """Build beside the target, then move it into place in one step.

    An interrupted export must leave nothing rather than something plausible: a
    partial export that looks complete is the worst artifact this module could
    produce.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".export-", dir=out.parent))
    try:
        for name, payload in sorted(payloads.items()):
            (staging / name).write_bytes(payload)
        if out.exists():
            shutil.rmtree(out)
        os.replace(staging, out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def load_manifest(directory: Path) -> Manifest:
    return Manifest.model_validate_json((directory / MANIFEST_FILE).read_bytes())


def load_items(directory: Path) -> tuple[NormalizedItem, ...]:
    """Every record in `items.jsonl`, in file order.

    File order *is* the record order (`RECORD_ORDER`), so a reader never has to
    re-sort and can rely on a family arriving together.
    """
    payload = (directory / ITEMS_FILE).read_bytes()
    return tuple(load_item(line) for line in payload.splitlines() if line.strip())


def load_roots(directory: Path) -> tuple[ItemStub, ...]:
    """The population index. Raises `FileNotFoundError` on a version-1 export.

    The caller decides what an absent population index means; for the screen it
    means the uniqueness predicates are `unavailable`, never that they quietly
    fall back to slice scope.
    """
    payload = (directory / ROOTS_FILE).read_bytes()
    return tuple(
        ItemStub.model_validate_json(line) for line in payload.splitlines() if line.strip()
    )


def load_census(directory: Path) -> census_module.Census:
    return census_module.Census.model_validate_json((directory / CENSUS_FILE).read_bytes())


def comparable_manifest(directory: Path) -> dict[str, object]:
    """The manifest with its volatile fields lifted out.

    What "byte-identical" means in practice: `items.jsonl` compares whole, and the
    manifest compares minus `created_at`. The exception list is a named constant
    so it cannot quietly grow to absorb a real regression.
    """
    data = json.loads((directory / MANIFEST_FILE).read_bytes())
    return {key: value for key, value in data.items() if key not in VOLATILE_MANIFEST_FIELDS}


__all__ = [
    "CENSUS_FILE",
    "CENSUS_MARKDOWN_FILE",
    "DEFAULT_MAX_RECORDS",
    "DEFAULT_ROOTS",
    "DEFAULT_SEED",
    "ITEMS_FILE",
    "MANIFEST_FILE",
    "ROOTS_FILE",
    "VOLATILE_MANIFEST_FIELDS",
    "DroppedFamily",
    "ExportError",
    "ExportResult",
    "Family",
    "Manifest",
    "SelectionPlan",
    "allocate",
    "comparable_manifest",
    "default_directory",
    "fetch_family",
    "git_state",
    "list_all",
    "load_census",
    "load_items",
    "load_manifest",
    "load_roots",
    "render_items",
    "render_roots",
    "run_export",
    "select",
    "write_atomically",
]
