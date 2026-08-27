"""Item identity and external metadata ids.

plexapi does no guid parsing at all -- `media.Guid` exposes `id` as a bare string
and the package contains no namespace enum, no parser, and no helper. Every guid
form this project understands is understood here.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

_LEGACY_PREFIX = "com.plexapp.agents."


class IdNamespace(StrEnum):
    """Where an external id lives.

    `UNKNOWN` is not a failure mode, it is the point: an unrecognised guid keeps
    its raw string and gets counted by the census rather than being dropped.
    """

    IMDB = "imdb"
    TMDB = "tmdb"
    TVDB = "tvdb"
    PLEX = "plex"
    ASIN = "asin"
    MBID = "mbid"
    LOCAL = "local"
    UNKNOWN = "unknown"


# Schemes used by the current agents (tv.plex.agents.*), where the guid is already
# in `namespace://value` form.
_SCHEMES: dict[str, IdNamespace] = {
    "imdb": IdNamespace.IMDB,
    "tmdb": IdNamespace.TMDB,
    "themoviedb": IdNamespace.TMDB,
    "tvdb": IdNamespace.TVDB,
    "thetvdb": IdNamespace.TVDB,
    "plex": IdNamespace.PLEX,
    "asin": IdNamespace.ASIN,
    "mbid": IdNamespace.MBID,
    "local": IdNamespace.LOCAL,
}

# Legacy `com.plexapp.agents.<agent>://` identifiers. Deliberately partial:
# plexmovie, plexmusic, lastfm and friends resolve to UNKNOWN rather than to a
# guess, because a wrong namespace is worse than an honest one.
_LEGACY_AGENTS: dict[str, IdNamespace] = {
    "imdb": IdNamespace.IMDB,
    "themoviedb": IdNamespace.TMDB,
    "thetvdb": IdNamespace.TVDB,
    "audnexus": IdNamespace.ASIN,
    "none": IdNamespace.LOCAL,
}

# The HAMA anime agent nests the real source in the path: hama://tvdb-73739/1/1.
_HAMA_SOURCES: dict[str, IdNamespace] = {
    "tvdb": IdNamespace.TVDB,
    "tmdb": IdNamespace.TMDB,
    "imdb": IdNamespace.IMDB,
}


@dataclass(frozen=True, slots=True)
class ItemId:
    """A library item, addressable in both the live and snapshot worlds.

    Composite rather than a bare rating key so snapshot and live ids can never
    collide and a truth file can reference either. Note that a Plex rating key
    moves on rescan -- this is an address, never an identity. Case identity is
    semantic and lives in `evals` (step 0.6).
    """

    provider: str
    section_id: str
    rating_key: str

    def __post_init__(self) -> None:
        for name in ("provider", "section_id", "rating_key"):
            value = getattr(self, name)
            if not value:
                raise ValueError(f"ItemId.{name} must not be empty")
            if ":" in value:
                raise ValueError(
                    f"ItemId.{name} must not contain ':' (got {value!r}); the "
                    "separator is what makes str()/parse() unambiguous"
                )

    def __str__(self) -> str:
        return f"{self.provider}:{self.section_id}:{self.rating_key}"

    @classmethod
    def parse(cls, text: str) -> "ItemId":
        parts = text.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"Expected 'provider:section_id:rating_key', got {text!r}. "
                f"Found {len(parts)} component(s)."
            )
        return cls(*parts)


@dataclass(frozen=True, slots=True)
class ExternalId:
    """A parsed guid. `raw` is always the exact string Plex returned."""

    namespace: IdNamespace
    value: str
    raw: str
    season: int | None = None
    episode: int | None = None


def _sort_key(external: ExternalId) -> tuple[str, str, int, int]:
    # -1 sorts a series-level id ahead of its season/episode-level siblings.
    return (
        str(external.namespace),
        external.value,
        -1 if external.season is None else external.season,
        -1 if external.episode is None else external.episode,
    )


def sort_external_ids(ids: Iterable[ExternalId]) -> tuple[ExternalId, ...]:
    """Canonical ordering. Plex returns Guid elements in XML order, which is not
    stable enough to hash."""
    return tuple(sorted(ids, key=_sort_key))


def _split_path(remainder: str) -> tuple[str, int | None, int | None]:
    """Split `73739/1/1` into (id, season, episode).

    Non-numeric path components are not silently discarded: the whole remainder
    becomes the value instead, so nothing is lost to a form we did not anticipate.
    """
    head, _, tail = remainder.partition("/")
    if not tail:
        return remainder, None, None

    parts = tail.split("/")
    if len(parts) > 2 or not all(p.isdigit() for p in parts):
        return remainder, None, None

    season = int(parts[0])
    episode = int(parts[1]) if len(parts) == 2 else None
    return head, season, episode


def parse_guid(raw: str) -> ExternalId:
    """Parse any guid form Plex produces into a namespace and value.

    Handles the current agents (`tmdb://278`, `plex://movie/<hash>`) and the legacy
    ones (`com.plexapp.agents.imdb://tt0111161?lang=en`, including the
    `thetvdb://<id>/<season>/<episode>` path form and HAMA's nested source).
    Anything unrecognised becomes `UNKNOWN` with `raw` intact -- never dropped.
    """
    scheme, separator, remainder = raw.partition("://")
    if not separator:
        return ExternalId(IdNamespace.UNKNOWN, raw, raw)

    # `?lang=en` is a fetch detail, not part of the identity. `raw` keeps it.
    remainder = remainder.partition("?")[0]
    if not remainder:
        return ExternalId(IdNamespace.UNKNOWN, raw, raw)

    if scheme.startswith(_LEGACY_PREFIX):
        agent = scheme[len(_LEGACY_PREFIX) :]

        if agent == "hama":
            source, _, rest = remainder.partition("-")
            namespace = _HAMA_SOURCES.get(source)
            if namespace is not None and rest:
                value, season, episode = _split_path(rest)
                return ExternalId(namespace, value, raw, season, episode)
            return ExternalId(IdNamespace.UNKNOWN, remainder, raw)

        namespace = _LEGACY_AGENTS.get(agent)
        if namespace is None:
            return ExternalId(IdNamespace.UNKNOWN, remainder, raw)
        value, season, episode = _split_path(remainder)
        return ExternalId(namespace, value, raw, season, episode)

    namespace = _SCHEMES.get(scheme)
    if namespace is None:
        return ExternalId(IdNamespace.UNKNOWN, remainder, raw)

    # plex://movie/<hash>: the leading component is a type, not a season number,
    # and the pair is the identifier -- keep the remainder whole.
    if namespace is IdNamespace.PLEX:
        return ExternalId(namespace, remainder, raw)

    return ExternalId(namespace, remainder, raw)


def parse_guids(primary: str | None, children: Iterable[str] = ()) -> tuple[ExternalId, ...]:
    """Normalize a Plex item's `guid` plus its `guids` children into one sorted set.

    Under the current agents `primary` is a `plex://` id and `children` carry the
    external ones; under a legacy agent `children` is empty and `primary` is the
    only id there is. Both collapse to the same shape here, which is the point --
    a legacy-agent library is exactly where wrong-match problems concentrate.
    """
    found: dict[tuple[IdNamespace, str, int | None, int | None], ExternalId] = {}
    for raw in (*([primary] if primary else []), *children):
        if not raw:
            continue
        external = parse_guid(raw)
        found.setdefault(
            (external.namespace, external.value, external.season, external.episode),
            external,
        )
    return sort_external_ids(found.values())
