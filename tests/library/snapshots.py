"""Snapshots for tests, built from records rather than from a dataset.

The records are the hand-built ones `tests/evals/conftest.py` gives `FakeLibrary`:
a remake pair, two entries for one film, legacy and unknown guids, a show with
two seasons, a show with none, and an author whose book is split across two
files. They are relabelled from `fake` to `snapshot`, because a snapshot's
records carry its own label (`SnapshotRule.PROVIDER_LABEL`).

Step 0.7.6 builds snapshots from an export and a delta. Until then -- and
afterwards, for tests that want a known library rather than a generated one --
these are what a snapshot serves.
"""

from collections.abc import Sequence

from shelfwarden.library.base import ProviderInfo
from shelfwarden.library.snapshot import PROVIDER, SnapshotLibrary
from shelfwarden.models.item import FetchProfile, NormalizedItem, SectionRef, dump_item, load_item
from tests.evals.conftest import SECTIONS, _library_records

# The fake's music and photo sections are left out: a snapshot holds only what an
# export exported, and an export skips both.
SNAPSHOT_SECTIONS: tuple[SectionRef, ...] = tuple(
    section for section in SECTIONS if section.section_id in {"1", "2", "3"}
)

INFO = ProviderInfo(provider=PROVIDER, server_id="0123456789abcdef")


def relabel(records: Sequence[NormalizedItem], provider: str) -> tuple[NormalizedItem, ...]:
    """The same records under another provider label: every id, parent and
    grandparent, and nothing else."""
    relabelled = []
    for record in records:
        document = dump_item(record)
        for name in ("item_id", "parent", "grandparent"):
            if document.get(name):
                document[name] = {**document[name], "provider": provider}
        relabelled.append(load_item(document))
    return tuple(relabelled)


def restamp(records: Sequence[NormalizedItem], profile: FetchProfile) -> tuple[NormalizedItem, ...]:
    return tuple(load_item({**dump_item(record), "fetched": profile}) for record in records)


RECORDS: tuple[NormalizedItem, ...] = relabel(_library_records(), PROVIDER)


def hand_built(
    records: Sequence[NormalizedItem] = RECORDS,
    sections: Sequence[SectionRef] = SNAPSHOT_SECTIONS,
    info: ProviderInfo = INFO,
    profile: FetchProfile = FetchProfile.CORE,
) -> SnapshotLibrary:
    return SnapshotLibrary(records, sections, info, profile)
