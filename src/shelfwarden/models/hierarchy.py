"""The shape of a library: which kinds hang beneath which.

Show -> season -> episode and author -> audiobook -> audiobook part are the same
three-level tree -- step 0.2 added `author` for exactly that symmetry -- and a movie
is a tree of one. The export walks it, the Plex adapter maps sections onto it, the
snapshot provider serves it, and step 0.7's integrity rules check worlds against
it. One table here, rather than a copy in each.

A leaf beside `item.py`: it knows `MediaKind` and nothing else.
"""

from shelfwarden.models.item import MediaKind

CHILD_KIND: dict[MediaKind, MediaKind] = {
    MediaKind.SHOW: MediaKind.SEASON,
    MediaKind.SEASON: MediaKind.EPISODE,
    MediaKind.AUTHOR: MediaKind.AUDIOBOOK,
    MediaKind.AUDIOBOOK: MediaKind.AUDIOBOOK_PART,
}

# Derived rather than declared: a second hand-written table is a second place to
# be wrong. Inverting is only sound while no two kinds share a child, which a test
# pins -- a comprehension would otherwise drop one silently.
PARENT_KIND: dict[MediaKind, MediaKind] = {child: parent for parent, child in CHILD_KIND.items()}


def lineage(kind: MediaKind) -> tuple[MediaKind, ...]:
    """A kind and every kind beneath it, top-down: `SHOW -> (SHOW, SEASON, EPISODE)`."""
    kinds = [kind]
    while kinds[-1] in CHILD_KIND:
        kinds.append(CHILD_KIND[kinds[-1]])
    return tuple(kinds)


__all__ = ["CHILD_KIND", "PARENT_KIND", "lineage"]
