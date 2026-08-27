"""Deciding whether an `artist` section holds audiobooks.

Plex has no audiobook library type -- sections are only movie, show, artist, or
photo -- so audiobooks live in a Music section by convention: Author→Artist,
Book→Album, Chapter→Track, usually via the legacy Audnexus agent. There is no
`item.type == 'audiobook'` to branch on, so this is a judgement.

Because it is a judgement, it reports its evidence. The verdict carries every
signal, what was observed, and the threshold it was judged against, so a later
argument about a misclassified section is settled by reading the record rather
than re-running the detection.

This module deliberately imports no plexapi: it takes plain samples, which keeps
the logic unit-testable and keeps the adapter boundary in one place.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from statistics import median

# An agent identifier containing any of these is decisive on its own -- the
# operator has told Plex what the section is.
AGENT_MARKERS: tuple[str, ...] = ("audnexus", "audiobook")

# Containers and extensions that indicate spoken-word packaging. Plex reports the
# container inconsistently for .m4b (sometimes "mp4"), so the file extension is
# checked as well as the container attribute.
AUDIOBOOK_CONTAINERS: frozenset[str] = frozenset({"m4b", "m4a"})
AUDIOBOOK_EXTENSIONS: frozenset[str] = frozenset({".m4b", ".m4a"})

# Share of sampled tracks that must be audiobook-packaged.
CONTAINER_SHARE_THRESHOLD = 0.5

# Median track length above which a "track" is more plausibly a chapter. Music
# tracks cluster around 3-5 minutes; audiobook chapters run far longer.
LONG_TRACK_MS = 10 * 60 * 1000


class Signal(StrEnum):
    AGENT_IDENTIFIER = "agent_identifier"
    CONTAINER_SHARE = "container_share"
    ALBUM_STRUCTURE = "album_structure"


@dataclass(frozen=True, slots=True)
class TrackSample:
    """One sampled track, reduced to the attributes detection actually uses."""

    album_id: str
    path: str
    container: str | None = None
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class SignalResult:
    signal: Signal
    fired: bool
    observed: str
    threshold: str


@dataclass(frozen=True, slots=True)
class AudiobookVerdict:
    is_audiobook: bool
    signals: tuple[SignalResult, ...]
    # How much was looked at, and out of how much. Detection samples rather than
    # walking a large section, and a truncation nobody can see reads as full
    # coverage -- the "no silent caps" house rule.
    sampled: int
    population: int

    @property
    def fired(self) -> tuple[Signal, ...]:
        return tuple(result.signal for result in self.signals if result.fired)

    def explain(self) -> str:
        verdict = "audiobooks" if self.is_audiobook else "not audiobooks"
        detail = "; ".join(
            f"{r.signal}={'fired' if r.fired else 'no'} ({r.observed} vs {r.threshold})"
            for r in self.signals
        )
        return f"{verdict} [sampled {self.sampled}/{self.population}] {detail}"


def _is_audiobook_packaged(sample: TrackSample) -> bool:
    if sample.container and sample.container.lower() in AUDIOBOOK_CONTAINERS:
        return True
    lowered = sample.path.lower()
    return any(lowered.endswith(extension) for extension in AUDIOBOOK_EXTENSIONS)


def classify_section(
    agent: str,
    samples: Sequence[TrackSample],
    population: int,
) -> AudiobookVerdict:
    """Decide whether a Music section holds audiobooks.

    The agent identifier is decisive alone. Failing that, **both** remaining
    signals must fire, because either one alone has a well-known false positive:
    a classical or live-recording library has long tracks, and spoken-word
    podcasts ship as .m4a. Requiring both is the conservative choice, and the
    signals are recorded either way so a wrong call can be argued with.
    """
    lowered_agent = agent.lower()
    agent_hit = any(marker in lowered_agent for marker in AGENT_MARKERS)
    agent_result = SignalResult(
        signal=Signal.AGENT_IDENTIFIER,
        fired=agent_hit,
        observed=agent or "<none>",
        threshold=f"contains any of {', '.join(AGENT_MARKERS)}",
    )

    if samples:
        packaged = sum(1 for sample in samples if _is_audiobook_packaged(sample))
        share = packaged / len(samples)
        durations = [s.duration_ms for s in samples if s.duration_ms is not None]
        median_duration = median(durations) if durations else 0
    else:
        share = 0.0
        median_duration = 0

    container_result = SignalResult(
        signal=Signal.CONTAINER_SHARE,
        fired=share >= CONTAINER_SHARE_THRESHOLD and bool(samples),
        observed=f"{share:.0%} of {len(samples)} sampled",
        threshold=f">= {CONTAINER_SHARE_THRESHOLD:.0%}",
    )
    structure_result = SignalResult(
        signal=Signal.ALBUM_STRUCTURE,
        fired=median_duration >= LONG_TRACK_MS,
        observed=f"median track {median_duration / 60000:.1f} min",
        threshold=f">= {LONG_TRACK_MS / 60000:.0f} min",
    )

    signals = (agent_result, container_result, structure_result)
    is_audiobook = agent_hit or (container_result.fired and structure_result.fired)

    return AudiobookVerdict(
        is_audiobook=is_audiobook,
        signals=signals,
        sampled=len(samples),
        population=population,
    )
