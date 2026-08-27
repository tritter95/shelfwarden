#!/usr/bin/env python
"""Capture XML fixtures from a real Plex server.

Not part of the package: it lives outside src/ so it is neither shipped nor
covered by the import contracts. Run it by hand when a fixture needs refreshing,
review the diff, and commit -- a changed upstream shape is information, not noise.

    export PLEX_URL=http://192.168.1.10:32400
    export PLEX_TOKEN=...            # never passed as an argument: argv lands in
                                     # shell history and process listings
    uv run python scripts/capture_fixtures.py --out tests/fixtures/plex

Everything written is scrubbed first: the token, the server machine identifier,
and absolute media paths, which leak both the library layout and the operator's
directory structure.
"""

import argparse
import os
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

# The adapter owns plexapi configuration; reuse it rather than re-deriving it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shelfwarden.library.plex import configure_plexapi

# Attributes worth keeping. Anything else is either volatile (view counts,
# session state) or noise that makes fixture diffs unreadable.
KEEP = {
    "ratingKey",
    "key",
    "type",
    "title",
    "titleSort",
    "summary",
    "year",
    "guid",
    "index",
    "parentIndex",
    "parentRatingKey",
    "parentTitle",
    "grandparentRatingKey",
    "grandparentTitle",
    "childCount",
    "leafCount",
    "duration",
    "studio",
    "network",
    "contentRating",
    "tagline",
    "rating",
    "audienceRating",
    "originalTitle",
    "editionTitle",
    "showOrdering",
    "originallyAvailableAt",
    "addedAt",
    "updatedAt",
    "thumb",
    "art",
    "container",
    "videoResolution",
    "file",
    "size",
    "id",
    "name",
    "locked",
}

TOKEN_PATTERN = re.compile(r"X-Plex-Token=[^&\"']+")


def scrub(element: ET.Element, media_root: str) -> ET.Element:
    """Drop volatile attributes and anything identifying the operator."""
    for attribute in list(element.attrib):
        if attribute not in KEEP:
            del element.attrib[attribute]
            continue
        value = element.attrib[attribute]
        value = TOKEN_PATTERN.sub("X-Plex-Token=REDACTED", value)
        if attribute == "file":
            # Keep the shape (directory nesting, extension) and drop the location.
            value = "/media/" + value.replace(media_root, "").lstrip("/")
        element.attrib[attribute] = value
    for child in element:
        scrub(child, media_root)
    return element


def capture(server, out: Path, media_root: str) -> list[Path]:
    written: list[Path] = []
    for section in server.library.sections():
        libtypes = {
            "movie": ["movie"],
            "show": ["show", "season", "episode"],
            "artist": ["artist", "album", "track"],
        }.get(section.type, [])
        for libtype in libtypes:
            found = section.search(libtype=libtype, container_start=0, maxresults=1)
            if not found:
                print(f"  {section.title}/{libtype}: nothing to capture")
                continue
            item = found[0]
            item.reload()
            element = scrub(ET.fromstring(ET.tostring(item._data)), media_root)
            target = out / f"{section.type}_{libtype}.xml"
            target.write_text(ET.tostring(element, encoding="unicode") + "\n", encoding="utf-8")
            written.append(target)
            print(f"  wrote {target}")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures/plex"))
    parser.add_argument(
        "--media-root",
        default="",
        help="Absolute media path prefix to strip from part filenames.",
    )
    args = parser.parse_args()

    url, token = os.environ.get("PLEX_URL"), os.environ.get("PLEX_TOKEN")
    if not url or not token:
        print("PLEX_URL and PLEX_TOKEN must be set in the environment.", file=sys.stderr)
        return 1

    configure_plexapi()
    from plexapi.server import PlexServer

    args.out.mkdir(parents=True, exist_ok=True)
    written = capture(PlexServer(url, token), args.out, args.media_root)
    print(f"\n{len(written)} fixture(s) written. Review the diff before committing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
