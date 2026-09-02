"""The comparator library: when are two strings the same thing?

A leaf module beside `canonical.py`. Pure functions over strings and primitives
-- no I/O, no clock, no RNG -- because four consumers on both sides of the
Phase 5 MCP seam have to agree on the answer:

| Consumer | Module | Step |
|---|---|---|
| mechanical screen | `evals/screen.py` | 0.45 |
| detectability witness | `evals/corrupt/*` | 0.5 |
| scorer | `evals/score.py` | 0.8 |
| validator *support* + `bind()` | `agent/validate.py` | 1.4 |

That last row is why this file is top-level rather than `evals/compare.py`,
which is where implementation-plan.md §7 originally put it: the agent must not
import the package holding the answer key, and the Phase 5 extraction must not
have to carry `evals/` with it. An import contract enforces the leaf property.

**Three verified traps shape everything below**, each a default that fails
quietly rather than loudly:

1. **`SequenceMatcher.ratio()` is not symmetric.** Brute-forced over a
   three-letter alphabet: 9228 asymmetric pairs at lengths 1-5, with
   `ratio('ab', 'bacb')` returning 0.667 one way and 0.333 the other. So every
   comparator here takes `(observed, authority)` **in that order**, the
   parameter names say so, and a test pins a known-asymmetric pair. If the
   screen called with the library value first and the validator called with the
   authority value first, they would share a name and not a result.
2. **`autojunk=True` is the default and destroys long-text comparison.** It
   drops any element appearing in more than 1% of the second sequence, for
   sequences of length >= 200. Verified: `"a" * 300` against `"ab" * 150` scores
   0.0033 with the default and 0.5 without -- a 150x difference, switching on
   partway through a dataset as summaries get longer. `autojunk=False` is passed
   explicitly at every construction site.
3. **`casefold()` does not preserve NFC.** `'Å'` decomposed casefolds to a
   decomposed `'å'`; `'İ'` expands to `i` + U+0307. `FilePart.path` is
   deliberately *not* NFC-normalized (macOS hands out NFD and a path is an
   argument to a future filesystem call), so NFD text reaches these functions
   through exactly one door: the filename checks. `fold_text` therefore
   normalizes **last**, not first.

Note `fold_text` uses NFKC while `canonical.canonical_text` uses NFC. That
divergence is deliberate and load-bearing -- see `fold_text`. Do not "unify"
them.
"""

import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from shelfwarden.models.ids import ExternalId, IdNamespace

# Scores are rounded at construction. A raw float is a determinism hazard in
# canonical JSON, and `allow_nan=False` already turns the pathological case into
# a write-time error rather than an unreadable dataset.
SCORE_PRECISION = 4

# A release year and an air year differ by one constantly -- a December film
# reaching cinemas in January, a season premiere crossing New Year. One year is
# a FUZZY match, never a NORMALIZED one.
YEAR_TOLERANCE = 1


class SupportStrength(StrEnum):
    """How strongly a value supports a claim. Never a bool.

    `StrEnum` rather than `IntEnum` despite the ordering: these values are
    written into `screen.json` and `truth.json` and read by a human choosing
    `composition.toml` shares, and an `IntEnum` serializes to a bare integer
    that makes the dataset unreadable years later. `STRENGTH_RANK` carries the
    ordering explicitly, which also avoids `NONE == 0` being accidentally falsy.
    """

    EXACT = "exact"
    ALIAS = "alias"
    NORMALIZED = "normalized"
    FUZZY = "fuzzy"
    NONE = "none"


# ALIAS outranks NORMALIZED on purpose: an alias hit is an assertion *by the
# authority* that two names denote one work; our fold is an assertion by us.
STRENGTH_RANK: dict[SupportStrength, int] = {
    SupportStrength.NONE: 0,
    SupportStrength.FUZZY: 1,
    SupportStrength.NORMALIZED: 2,
    SupportStrength.ALIAS: 3,
    SupportStrength.EXACT: 4,
}


def at_least(strength: SupportStrength, minimum: SupportStrength) -> bool:
    """Rank comparison, spelled out rather than inherited from an enum's ordering."""
    return STRENGTH_RANK[strength] >= STRENGTH_RANK[minimum]


@dataclass(frozen=True, slots=True)
class Support:
    """One comparison's verdict, with the reason it reached it.

    `rule` names the fold rung or alias source that produced the answer, which
    is what makes step 1.4's false-rejection metric decompose per check rather
    than arriving as one undifferentiated number. `matched` names the alias that
    actually did the work -- an alias hit that cannot say which alias matched is
    a claim without a citation. `score` is a `ratio()` when `rule == "ratio"`
    and `None` otherwise, including for the non-ratio FUZZY rules.
    """

    strength: SupportStrength
    rule: str
    score: float | None = None
    matched: str | None = None

    def __post_init__(self) -> None:
        if self.score is not None:
            object.__setattr__(self, "score", round(self.score, SCORE_PRECISION))


@dataclass(frozen=True, slots=True)
class Policy:
    """Strength-to-decision, declared as data.

    Comparators return a strength; *policies* turn a strength into a decision.
    The screen and the validator share the comparators and deliberately do not
    share the thresholds, which is why this is a separate object rather than a
    constant inside `compare_title`:

    * the **screen** breaks ties toward `unguarded` -- a missed guard costs
      coverage, which is visible and countable;
    * the **validator** breaks ties toward *accepting* a finding -- a wrong
      rejection costs a true detection, which is not.

    Same functions, opposite tie-breaking. Do not refactor the asymmetry away.
    """

    name: str
    minimum: SupportStrength
    fuzzy_floor: float | None = None

    def satisfied_by(self, support: Support) -> bool:
        if not at_least(support.strength, self.minimum):
            return False
        if support.strength is SupportStrength.FUZZY and self.fuzzy_floor is not None:
            return support.score is not None and support.score >= self.fuzzy_floor
        return True


SCREEN_POLICY = Policy("screen", minimum=SupportStrength.NORMALIZED, fuzzy_floor=None)
# VALIDATOR_POLICY lands in step 1.4, where there is a false-rejection rate to
# tune it against. Deliberately absent rather than stubbed: a stub would be a
# number chosen with no evidence, and it would be copied.


# -- the fold ladder ------------------------------------------------------


def _collapse(value: str) -> str:
    return " ".join(value.split())


def fold_text(value: str) -> str:
    """NFKC, then casefold, then **NFC**, then collapse whitespace.

    Two orderings here are load-bearing.

    **NFC last.** The Unicode canonical caseless match is
    `NFC(casefold(NFD(x)))`; casefolding does not preserve NFC, so folding after
    normalizing leaves decomposed text decomposed and an NFD path never matches
    an NFC title. Verified: `'Å'` (NFD) casefolds to a decomposed `'å'`, and
    `'İstanbul'` casefolds to `i` + U+0307 + `stanbul`. Normalizing afterwards
    makes both agree with their composed forms.

    **NFKC rather than NFC.** Compatibility normalization folds `Ⅻ` to `XII`,
    `ﬁ` to `fi`, and fullwidth forms to ASCII -- all the same title. It is lossy
    in ways NFC is not, which is exactly why it belongs in a *comparison* fold
    and never in `canonical.canonical_text`, whose job is to preserve the value
    being hashed. The two functions must not be unified.

    Verified limit: `Æ` and `œ` survive NFKD, so `Cœur` and `Coeur` fall to
    FUZZY rather than NORMALIZED. A hand-maintained ligature table would rot;
    recording the ceiling is the honest answer.
    """
    folded = unicodedata.normalize("NFKC", value).casefold()
    return _collapse(unicodedata.normalize("NFC", folded))


def strip_punctuation(value: str) -> str:
    """Drop Unicode `P*`, replacing with a space so `Spider-Man` meets `Spider Man`."""
    return _collapse(
        "".join(" " if unicodedata.category(char).startswith("P") else char for char in value)
    )


# Anglocentric, and knowingly so: English, French, Spanish, and German. A
# Japanese or Korean title has no article to strip and is unaffected; a Swedish
# or Polish one may be. This is a NORMALIZED-tier loosening, so the failure mode
# is a missed guard rather than a wrong one. Revisit against a real census.
LEADING_ARTICLES: tuple[str, ...] = (
    "the ",
    "a ",
    "an ",
    "le ",
    "la ",
    "les ",
    "el ",
    "der ",
    "die ",
    "das ",
)


def strip_articles(value: str) -> str:
    """Drop one leading article. Runs on already-folded (lowercased) text."""
    for article in LEADING_ARTICLES:
        if value.startswith(article):
            return value[len(article) :]
    return value


def strip_diacritics(value: str) -> str:
    """NFKD, drop combining marks, recompose."""
    decomposed = unicodedata.normalize("NFKD", value)
    return unicodedata.normalize(
        "NFC", "".join(char for char in decomposed if not unicodedata.combining(char))
    )


# Ordered and cumulative: each rung is applied on top of the previous one, and
# the rung that first makes two strings equal is what `Support.rule` reports.
FOLD_LADDER: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("fold", fold_text),
    ("strip_punctuation", strip_punctuation),
    ("strip_articles", strip_articles),
    ("strip_diacritics", strip_diacritics),
)


def fold_rungs(value: str) -> tuple[tuple[str, str], ...]:
    """The value at every rung of the ladder, in order."""
    rungs: list[tuple[str, str]] = []
    current = value
    for name, step in FOLD_LADDER:
        current = step(current)
        rungs.append((name, current))
    return tuple(rungs)


def ladder_rule(observed: str, authority: str) -> str | None:
    """The first rung at which two strings become equal, or `None`."""
    for (name, left), (_, right) in zip(fold_rungs(observed), fold_rungs(authority), strict=True):
        if left == right:
            return name
    return None


# -- the comparators ------------------------------------------------------
#
# All of them take (observed, authority) in that order. See trap 1.


def ratio(observed: str, authority: str) -> float:
    """Similarity in [0, 1]. `autojunk=False` (trap 2); argument order matters (trap 1)."""
    return round(
        SequenceMatcher(None, observed, authority, autojunk=False).ratio(), SCORE_PRECISION
    )


def compare_title(
    observed: str | None, authority: str | None, *, aliases: Sequence[str] = ()
) -> Support:
    """Compare a library title against an authority's title and its aliases.

    The authority's own title is tried before its aliases, so `matched` is set
    only when an alias is what actually did the work. An alias hit outranks a
    normalized one because it is the authority asserting the identity rather
    than us inferring it -- which is what keeps `foreign_title_variant`, a class
    whose entire premise is that the local title differs from the canonical one,
    from being rejected by strict equality.
    """
    if not observed or not authority:
        return Support(SupportStrength.NONE, "empty")
    if observed == authority:
        return Support(SupportStrength.EXACT, "identity")
    for alias in aliases:
        if alias and observed == alias:
            return Support(SupportStrength.ALIAS, "alias_identity", matched=alias)
    rung = ladder_rule(observed, authority)
    if rung is not None:
        return Support(SupportStrength.NORMALIZED, rung)
    for alias in aliases:
        if not alias:
            continue
        rung = ladder_rule(observed, alias)
        if rung is not None:
            return Support(SupportStrength.ALIAS, f"alias_{rung}", matched=alias)
    score = ratio(observed, authority)
    if score <= 0.0:
        return Support(SupportStrength.NONE, "no_match", score=score)
    return Support(SupportStrength.FUZZY, "ratio", score=score)


def compare_year(observed: int | None, authority: int | None) -> tuple[Support, int | None]:
    """Compare years, returning the delta alongside the strength.

    The delta is returned rather than folded into the strength because
    `SubjectMatch` (implementation-plan.md §6) carries `year_delta` as its own
    field: "off by one" and "off by forty" are different facts about a match,
    and a remake pair is exactly the case where the second one is the finding.
    """
    if observed is None or authority is None:
        return Support(SupportStrength.NONE, "missing"), None
    delta = observed - authority
    if delta == 0:
        return Support(SupportStrength.EXACT, "identity"), 0
    if abs(delta) <= YEAR_TOLERANCE:
        return Support(SupportStrength.FUZZY, "within_tolerance"), delta
    return Support(SupportStrength.NONE, "year_mismatch"), delta


def compare_text_block(observed: str | None, authority: str | None) -> Support:
    """Compare summaries and other long text. This is where trap 2 bites."""
    if not observed or not authority:
        return Support(SupportStrength.NONE, "empty")
    if observed == authority:
        return Support(SupportStrength.EXACT, "identity")
    if fold_text(observed) == fold_text(authority):
        return Support(SupportStrength.NORMALIZED, "fold")
    score = ratio(observed, authority)
    if score <= 0.0:
        return Support(SupportStrength.NONE, "no_match", score=score)
    return Support(SupportStrength.FUZZY, "ratio", score=score)


def name_tokens(value: str) -> tuple[str, ...]:
    """A person's name as an order-independent token set."""
    return tuple(sorted(strip_punctuation(fold_text(value)).split()))


def compare_person_name(observed: str | None, authority: str | None) -> Support:
    """Compare author and narrator names.

    `"Sanderson, Brandon"` and `"Brandon Sanderson"` return **ALIAS**, not
    FUZZY. That is `author_name_variant`'s entire premise: an inversion is a
    structural equivalence, not a similarity score, and letting it land at FUZZY
    would make the class's own guard threshold-dependent -- the one shape spec
    §3 forbids.

    The fold rungs are tried before the token set so that a name differing only
    in whitespace reports the tighter rule that explains it.
    """
    if not observed or not authority:
        return Support(SupportStrength.NONE, "empty")
    if observed == authority:
        return Support(SupportStrength.EXACT, "identity")
    rung = ladder_rule(observed, authority)
    if rung is not None:
        return Support(SupportStrength.NORMALIZED, rung)
    if name_tokens(observed) == name_tokens(authority):
        return Support(SupportStrength.ALIAS, "token_set", matched=authority)
    score = ratio(observed, authority)
    if score <= 0.0:
        return Support(SupportStrength.NONE, "no_match", score=score)
    return Support(SupportStrength.FUZZY, "ratio", score=score)


def compare_episode_number(
    observed: tuple[int | None, int | None], authority: tuple[int | None, int | None]
) -> Support:
    """Compare `(season, episode)` pairs.

    No fuzzy rung, for the reason `compare_series_position` has none: an episode
    number is an identifier, and the similarity of `S01E02` and `S01E12` is not
    evidence of anything. Either side missing a component is `NONE`, never a
    partial match -- "the file says episode 2 and the metadata says nothing" is an
    absence, and treating it as agreement is how a guard comes to cover a case it
    cannot see.
    """
    if None in observed or None in authority:
        return Support(SupportStrength.NONE, "missing")
    if observed == authority:
        return Support(SupportStrength.EXACT, "identity")
    return Support(SupportStrength.NONE, "numbering_mismatch")


def normalize_position(value: str) -> str:
    """Strip leading zeros and a trailing `.0`. Never coerces to a number.

    Audnexus returns `"3.5"` for novellas (practices §5.2), so `int()` raises --
    and `float()` is worse, because it succeeds: `3.5 == 3.50` starts being true
    while `"3.5" != "3.50"`, and the two comparisons disagree silently. The
    normalization is declared here so the rule a `Support` names is a rule
    someone can read.
    """
    text = value.strip()
    if not text:
        return ""
    whole, dot, fraction = text.partition(".")
    whole = whole.lstrip("0") or "0"
    if dot:
        fraction = fraction.rstrip("0")
        if fraction:
            return f"{whole}.{fraction}"
    return whole


def compare_series_position(observed: str | None, authority: str | None) -> Support:
    """Compare series positions as strings.

    No fuzzy rung: a position is an identifier, and the similarity of `"3"` and
    `"13"` is not evidence of anything.
    """
    if not observed or not authority:
        return Support(SupportStrength.NONE, "missing")
    if observed == authority:
        return Support(SupportStrength.EXACT, "identity")
    if normalize_position(observed) == normalize_position(authority):
        return Support(SupportStrength.NORMALIZED, "normalized_position")
    return Support(SupportStrength.NONE, "position_mismatch")


# -- identifiers ----------------------------------------------------------

# `PLEX` is an address inside one server, `LOCAL` says the agent matched
# nothing, and `UNKNOWN` means we did not recognise the form -- so a shared
# UNKNOWN value proves two strings are equal, not that two records denote one
# work. None of the three can resolve against an external authority.
RESOLVABLE_NAMESPACES: frozenset[IdNamespace] = frozenset(IdNamespace) - {
    IdNamespace.UNKNOWN,
    IdNamespace.LOCAL,
    IdNamespace.PLEX,
}


def has_resolvable_id(guids: Iterable[ExternalId]) -> bool:
    """Is there any id here that an external source could be asked about?"""
    return any(external.namespace in RESOLVABLE_NAMESPACES for external in guids)


def id_overlap(
    observed: Iterable[ExternalId], authority: Iterable[ExternalId]
) -> frozenset[IdNamespace]:
    """Namespaces in which both sides carry the same id.

    Values are compared case-insensitively because ASINs arrive uppercase and
    IMDb ids lowercase, from sources that disagree about which. This is the
    `id_overlap` field of §6's `SubjectMatch`, and it is the one signal that
    dominates a title comparison: never reject on a title alone when the ids
    agree.
    """
    left = {
        (external.namespace, external.value.casefold())
        for external in observed
        if external.namespace in RESOLVABLE_NAMESPACES
    }
    right = {
        (external.namespace, external.value.casefold())
        for external in authority
        if external.namespace in RESOLVABLE_NAMESPACES
    }
    return frozenset(namespace for namespace, _ in left & right)


# -- filenames ------------------------------------------------------------

_SEASON_EPISODE = re.compile(r"(?<![a-z0-9])s(\d{1,3})[ ._-]*e(\d{1,4})(?!\d)", re.IGNORECASE)
_YEAR = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_SEPARATORS = re.compile(r"[.\s_]+")

# Recognised release tags, stripped from a parsed title and reported in
# `ParsedRelease.tags`. Deliberately a list of what has been seen rather than an
# attempt at completeness -- an unrecognised tag stays in the title, which makes
# a comparison fail loudly instead of a title being silently truncated. Extend
# it from the census, not from imagination.
RELEASE_TAGS: frozenset[str] = frozenset(
    {
        "2160p",
        "1080p",
        "1080i",
        "720p",
        "576p",
        "480p",
        "4k",
        "uhd",
        "hd",
        "sd",
        "bluray",
        "blu-ray",
        "bdrip",
        "brrip",
        "bdremux",
        "remux",
        "webrip",
        "web-dl",
        "webdl",
        "web",
        "hdtv",
        "pdtv",
        "dvdrip",
        "dvd",
        "dvd5",
        "dvd9",
        "hdrip",
        "cam",
        "x264",
        "x265",
        "h264",
        "h265",
        "hevc",
        "avc",
        "xvid",
        "divx",
        "10bit",
        "8bit",
        "aac",
        "aac2",
        "ac3",
        "eac3",
        "dd5",
        "ddp5",
        "dts",
        "dtshd",
        "truehd",
        "atmos",
        "flac",
        "mp3",
        "opus",
        "commentary",
        "proper",
        "repack",
        "extended",
        "uncut",
        "unrated",
        "remastered",
        "internal",
        "limited",
        "imax",
        "hdr",
        "hdr10",
        "dv",
        "sdr",
        "multi",
        "dual",
        "subbed",
        "dubbed",
        "retail",
    }
)


@dataclass(frozen=True, slots=True)
class ParsedRelease:
    """What a filename claims about the item it holds.

    Deliberately **not** NFC-normalized: `FilePart.path` is the one string this
    project does not normalize, and normalizing here would hide trap 3 rather
    than exercise it. Comparison folds; parsing does not.
    """

    source: str
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    tags: tuple[str, ...] = ()


def _tokens(text: str) -> list[str]:
    """Split on scene separators, then on hyphens that bracket a known tag."""
    parts: list[str] = []
    for raw in _SEPARATORS.split(text):
        token = raw.strip("()[]{}")
        if not token:
            continue
        if "-" in token and any(
            piece.casefold() in RELEASE_TAGS for piece in token.split("-") if piece
        ):
            parts.extend(piece for piece in token.split("-") if piece)
        else:
            parts.append(token)
    return parts


def parse_release_name(filename: str) -> ParsedRelease:
    """Pull a title, year, and season/episode out of a path or filename.

    Both separators are handled because a Plex server on Windows reports
    backslash paths to a client on anything else.

    The *last* year in the name wins: `Blade Runner 2049 (2017)` and
    `2001 A Space Odyssey (1968)` both have a year in the title, and in both the
    release year is the trailing one. Everything before the year (or before
    `SxxEyy`) is the title, which is what makes tag stripping a fallback rather
    than the main mechanism.
    """
    source = re.split(r"[\\/]", filename)[-1]
    stem = _EXTENSION.sub("", source)

    season: int | None = None
    episode: int | None = None
    year: int | None = None
    head = stem

    match = _SEASON_EPISODE.search(head)
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
        head = head[: match.start()]

    years = list(_YEAR.finditer(head))
    if years:
        last = years[-1]
        year = int(last.group(1))
        head = head[: last.start()]

    tags = tuple(sorted({token.casefold() for token in _tokens(stem)} & RELEASE_TAGS))
    title = _collapse(
        " ".join(token for token in _tokens(head) if token.casefold() not in RELEASE_TAGS)
    ).strip("- ")

    return ParsedRelease(
        source=source, title=title, year=year, season=season, episode=episode, tags=tags
    )


# A path segment that is structure rather than a name: `Season 1`, `CD2`,
# `Disc 03`, `Specials`. Such a segment supplies no title and no year, which is
# what lets `parse_release_path` walk past `.../Cowboy Bebop/Season 1/S01E02.mkv`
# and reach the show. Anglocentric and deliberately narrow: a film genuinely
# titled `Vol` would be skipped, and the cost of that is falling through to a
# farther parent -- a missed answer rather than a wrong one.
_STRUCTURAL_SEGMENT = re.compile(
    r"^(?:s(?:eason)?|d(?:isc|isk)|cd|part|pt|vol(?:ume)?|track|specials?|extras?)"
    r"[\s._-]*\d*$",
    re.IGNORECASE,
)


def is_structural_segment(name: str) -> bool:
    """Is this path segment a disc/season marker rather than a name?"""
    return bool(_STRUCTURAL_SEGMENT.match(name.strip()))


def _raw_segments(path: str) -> list[str]:
    return [segment for segment in re.split(r"[\\/]", path) if segment]


def path_segments(path: str) -> tuple[str, ...]:
    """A path split into comparable name segments, nearest-last.

    `parse_release_name` reads the **basename** and nothing else, which is right
    for what step 0.45 asked of it and blind to what step 0.5 needs: four
    corruption classes take their detectability witness from a *directory*. The
    shared parent that proves two albums are one book, the series folder that
    proves a stripped series membership, the `[Final Cut]` folder that proves
    which cut a file holds -- on `.../The Way of Kings/CD1.m4b` the basename
    parses to `CD1` and tells you nothing while the parent tells you everything.

    The final segment is returned **without its extension**, because every
    consumer of this function is comparing names rather than opening files.
    (`parse_release_path` deliberately does not use it for the basename: strip
    the extension here and `parse_release_name` strips a second one, turning
    `br.1080p.mkv` into `br` and losing the tag.)

    Deliberately not NFC-normalized, for the reason `ParsedRelease` records:
    `FilePart.path` is the one string this project does not normalize, and
    folding happens at comparison time so trap 3 is exercised rather than hidden.
    """
    segments = _raw_segments(path)
    if not segments:
        return ()
    segments[-1] = _EXTENSION.sub("", segments[-1])
    return tuple(segments)


def parse_release_path(path: str) -> ParsedRelease:
    """`parse_release_name`, falling back to the parent directories.

    The basename is authoritative where it answers. Where it does not -- because
    it is a bare stem, or a structural segment like `CD1` -- the parents are
    tried from nearest to farthest. Three rules, each earning its place:

    * **A structural segment supplies nothing.** `Season 1` is not a show title
      and `CD1` is not a book title, and taking either would be a wrong answer
      rather than a missing one.
    * **Title and year are taken together**, from whichever segment supplies the
      year. `Title (Year)` is one claim, not two, so `Amelie (2001)/movie.mkv`
      resolves to `Amelie` rather than to `movie` with a year bolted on.
    * **`source` names the segment that supplied the title**, so a reader can see
      where the answer came from instead of assuming it was the filename.

    `tags` is the union across every segment, sorted: an edition marker lives in
    the folder at least as often as in the file, and `alternate_cut` reads it.

    `parse_release_name` is deliberately left untouched -- 0.45's tests pin its
    behavior and the screen's byte output depends on it.
    """
    segments = _raw_segments(path)
    if not segments:
        return ParsedRelease(source="", title="")

    parsed = parse_release_name(segments[-1])
    structural = is_structural_segment(parsed.title)
    title = "" if structural else parsed.title
    year = parsed.year
    season, episode = parsed.season, parsed.episode
    source = parsed.source if title else ""
    tags = set(parsed.tags)

    for segment in reversed(segments[:-1]):
        if title and year is not None:
            break
        candidate = parse_release_name(segment)
        tags |= set(candidate.tags)
        if is_structural_segment(candidate.title):
            continue
        if season is None and candidate.season is not None:
            season, episode = candidate.season, candidate.episode
        if year is None and candidate.year is not None:
            year = candidate.year
            if candidate.title:
                title, source = candidate.title, segment
            continue
        if not title and candidate.title:
            title, source = candidate.title, segment

    return ParsedRelease(
        source=source,
        title=title,
        year=year,
        season=season,
        episode=episode,
        tags=tuple(sorted(tags)),
    )


# Where a library hides a qualifier inside a segment: `Blade Runner (1982)
# [Final Cut]`, `The Way of Kings - Part 2`. Splitting on these lets an edition
# or a series name be found without a substring match, which would report a hit
# with no fold rung behind it.
# The en dash is spelled by codepoint: a literal one is indistinguishable
# from a hyphen in most editors, and RUF001 flags it for exactly that reason.
_CHUNK = re.compile("[\\[\\]()]|\\s[-\u2013]\\s")


def _path_candidates(path: str) -> tuple[str, ...]:
    """Each segment of a path, plus the bracketed chunks inside it.

    Whole segments first, so a segment that matches exactly outranks a chunk of
    one. Chunks are what let `[Final Cut]` be found inside
    `Blade Runner (1982) [Final Cut]` -- comparing the whole segment against
    `Final Cut` fails, and a substring test would return a hit with no fold rung
    to justify it.
    """
    found: list[str] = []
    for segment in path_segments(path):
        found.append(segment)
        for chunk in _CHUNK.split(segment):
            chunk = chunk.strip()
            if chunk and chunk != segment:
                found.append(chunk)
    return tuple(found)


def find_in_path(path: str, authority: str | None) -> Support:
    """The strongest `Support` any segment of a path offers for a string.

    This is the comparator behind the directory-shaped witnesses: is the series
    name still somewhere in the path after it was stripped from the metadata, is
    the edition marker still in the folder after `edition_title` was cleared. It
    reuses `compare_title` per segment rather than substring matching, so a hit
    arrives with the fold rung that produced it and is subject to the same policy
    as every other comparison.

    It returns the best support it found and applies **no threshold** -- a path
    almost always offers some FUZZY noise (`media` against `Amelie` scores 0.36),
    and deciding whether that counts is a `Policy`'s job, not a comparator's. A
    caller that treats a bare non-NONE result as a hit has skipped the policy.

    `matched` names the winning segment -- the same role it plays for an alias,
    one level out: which observed string actually did the work.
    """
    if not authority:
        return Support(SupportStrength.NONE, "empty")
    best: Support | None = None
    for segment in _path_candidates(path):
        support = compare_title(segment, authority)
        if support.strength is SupportStrength.NONE:
            continue
        ranked = (STRENGTH_RANK[support.strength], support.score or 0.0)
        if best is None or ranked > (STRENGTH_RANK[best.strength], best.score or 0.0):
            best = Support(support.strength, support.rule, support.score, matched=segment)
    return best or Support(SupportStrength.NONE, "no_match")


__all__ = [
    "FOLD_LADDER",
    "LEADING_ARTICLES",
    "RELEASE_TAGS",
    "RESOLVABLE_NAMESPACES",
    "SCORE_PRECISION",
    "SCREEN_POLICY",
    "STRENGTH_RANK",
    "YEAR_TOLERANCE",
    "ParsedRelease",
    "Policy",
    "Support",
    "SupportStrength",
    "at_least",
    "compare_episode_number",
    "compare_person_name",
    "compare_series_position",
    "compare_text_block",
    "compare_title",
    "compare_year",
    "find_in_path",
    "fold_rungs",
    "fold_text",
    "has_resolvable_id",
    "id_overlap",
    "is_structural_segment",
    "ladder_rule",
    "name_tokens",
    "normalize_position",
    "parse_release_name",
    "parse_release_path",
    "path_segments",
    "ratio",
    "strip_articles",
    "strip_diacritics",
    "strip_punctuation",
]
