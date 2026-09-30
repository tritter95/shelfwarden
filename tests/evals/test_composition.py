"""The composition: what the dataset is meant to hold, as absolute per-cell targets.

Step 0.6.4's gate is that shares which do not sum to 1 normalize, and that the
resolved integers sum to `--count`. Two properties matter more than the gate,
because each is what keeps a case's history in the CI baseline:

* **Raising `--count` never shrinks a cell.** Largest-remainder rounding, which
  the step plan first named, does: measured on the committed `composition.toml`,
  210 times between counts 0 and 1000, ten of them in cells the generator fills.
  A shrunk cell drops a case, so raising the count removed history rather than
  adding it. `resolve` is now sequential Webster apportionment.
* **The integers are a function of the shares**, not of the order the TOML
  declares them in. Apportionment breaks exact ties by position, so a
  declaration-order-dependent share would move cases on an edit that changed
  nothing.
"""

import functools
import random
import tomllib
from pathlib import Path

import pytest

from shelfwarden.evals.composition import (
    CLASS_BEARING,
    COMPOSITION_FILE,
    Cell,
    CompositionError,
    DeficitReason,
    deficits,
    load_composition,
    parse_composition,
    resolve,
)
from shelfwarden.evals.truth import Slice
from shelfwarden.models.finding import ProblemClass
from shelfwarden.models.item import MediaKind

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMITTED = (REPO_ROOT / COMPOSITION_FILE).read_bytes()


def _emit(slices, media):
    """A composition file from `[(name, share)]` and `[(kind, share, [(class, share)])]`."""
    lines = ["[slices]", *(f"{name} = {share!r}" for name, share in slices)]
    for kind, share, classes in media:
        lines += [f"[media.{kind}]", f"share = {share!r}", f"[media.{kind}.classes]"]
        lines += [f"{name} = {value!r}" for name, value in classes]
    return "\n".join(lines).encode()


def _shuffled(payload, seed):
    """The same shares, declared in a different order."""
    data = tomllib.loads(payload.decode())
    rng = random.Random(seed)
    slices = list(data["slices"].items())
    rng.shuffle(slices)
    media = []
    for kind, table in data["media"].items():
        classes = list(table["classes"].items())
        rng.shuffle(classes)
        media.append((kind, table["share"], classes))
    rng.shuffle(media)
    return _emit(slices, media)


def _targets(cells):
    return [(cell.slice, cell.media_kind, cell.problem_class, cell.intended) for cell in cells]


SMALL = _emit(
    [("synthetic", 2), ("real", 1), ("should_not_touch", 1), ("ambiguous", 0)],
    [
        ("movie", 3, [("wrong_match", 1), ("duplicate_quality", 3)]),
        ("show", 1, [("episode_wrong_season", 5)]),
    ],
)


# -- normalization ----------------------------------------------------------


class TestNormalization:
    def test_shares_that_do_not_sum_to_one_are_normalized(self):
        """Raising one number without rebalancing the others is a legitimate edit."""
        composition = parse_composition(SMALL)
        assert composition.slices == {
            Slice.SYNTHETIC: 0.5,
            Slice.REAL: 0.25,
            Slice.SHOULD_NOT_TOUCH: 0.25,
            Slice.AMBIGUOUS: 0.0,
        }
        assert composition.media[MediaKind.MOVIE].share == 0.75
        assert composition.media[MediaKind.SHOW].share == 0.25
        assert composition.media[MediaKind.MOVIE].classes == {
            ProblemClass.WRONG_MATCH: 0.25,
            ProblemClass.DUPLICATE_QUALITY: 0.75,
        }
        assert composition.media[MediaKind.SHOW].classes == {ProblemClass.EPISODE_WRONG_SEASON: 1.0}

    def test_the_committed_file_declares_all_fifteen_classes_in_every_medium(self):
        """A statement of design intent that should not churn when 1.1 lands. A class
        missing from a medium would be indistinguishable from one never considered."""
        data = tomllib.loads(COMMITTED.decode())
        for kind, table in data["media"].items():
            assert set(table["classes"]) == {str(pc) for pc in ProblemClass}, kind

    def test_the_composition_id_is_a_digest_of_the_bytes(self):
        """A diagnostic beside the lineage, never the baseline key."""
        assert parse_composition(SMALL).composition_id == parse_composition(SMALL).composition_id
        assert (
            parse_composition(SMALL).composition_id != parse_composition(COMMITTED).composition_id
        )
        assert parse_composition(SMALL).composition_id.startswith("comp-")


# -- resolution -------------------------------------------------------------


# Every count a human is likely to type, and past the first four points at which
# largest-remainder rounding shrank a cell the generator fills (172, 467, 515, 518).
SWEEP = range(0, 601)


@functools.cache
def _sweep():
    """The committed composition resolved at every count in `SWEEP`, once."""
    composition = parse_composition(COMMITTED)
    return tuple(resolve(composition, count) for count in SWEEP)


class TestResolution:
    def test_the_resolved_integers_sum_to_the_count(self):
        """The step's gate."""
        for count, cells in zip(SWEEP, _sweep(), strict=True):
            assert sum(cell.intended for cell in cells) == count

    def test_raising_the_count_never_shrinks_a_cell(self):
        """House monotonicity. A cell that loses a target loses a case, and that
        case's history, on an edit that asked for *more*. Largest-remainder rounding
        fails this on the committed file first at --count 46."""
        for count, previous, current in zip(SWEEP[1:], _sweep(), _sweep()[1:], strict=False):
            pairs = zip(previous, current, strict=True)
            grew = [now.intended - before.intended for before, now in pairs]
            assert min(grew) >= 0, f"a cell shrank going to --count {count}"
            assert sum(grew) == 1

    def test_every_target_stays_close_to_its_exact_share(self):
        """Monotone is not enough on its own: handing every case to one cell is
        monotone too. The targets must still be the shares, rounded.

        The bound is 1.5 rather than 1 because Webster, like every divisor method,
        can miss a cell's exact quota. Measured on the committed file between 0 and
        1000 it does so once, by 0.005: `synthetic | show | episode_wrong_season` at
        --count 38 has an exact share of 1.995 and receives 3. A broken
        apportionment misses by tens."""
        for count, cells in zip(SWEEP, _sweep(), strict=True):
            for cell in cells:
                assert abs(cell.intended - cell.share * count) < 1.5, (count, cell)

    def test_the_targets_do_not_depend_on_declaration_order(self):
        reference = parse_composition(COMMITTED)
        for seed in range(8):
            shuffled = parse_composition(_shuffled(COMMITTED, seed))
            for count in (0, 1, 7, 50, 199, 200, 201, 523):
                assert _targets(resolve(shuffled, count)) == _targets(resolve(reference, count))

    def test_should_not_touch_resolves_to_media_cells_only(self):
        """Such a case is about an item, not a class, so per-class shares are never
        read for it -- editing them must not move its targets."""
        cells = resolve(parse_composition(SMALL), 100)
        untouched = [cell for cell in cells if cell.slice is Slice.SHOULD_NOT_TOUCH]
        assert [cell.media_kind for cell in untouched] == [MediaKind.MOVIE, MediaKind.SHOW]
        assert all(cell.problem_class is None for cell in untouched)

        reclassed = _emit(
            [("synthetic", 2), ("real", 1), ("should_not_touch", 1), ("ambiguous", 0)],
            [
                ("movie", 3, [("wrong_match", 9), ("duplicate_quality", 1)]),
                ("show", 1, [("episode_wrong_season", 5), ("absolute_vs_seasonal", 5)]),
            ],
        )
        moved = resolve(parse_composition(reclassed), 100)
        assert [c.intended for c in moved if c.slice is Slice.SHOULD_NOT_TOUCH] == [
            c.intended for c in untouched
        ]

    def test_every_class_bearing_slice_is_split_by_class(self):
        cells = resolve(parse_composition(SMALL), 100)
        for cell in cells:
            assert (cell.problem_class is None) is (cell.slice not in CLASS_BEARING)

    def test_a_zero_share_cell_is_kept_rather_than_dropped(self):
        """ "We asked for none" and "we never considered it" must stay distinguishable."""
        cells = resolve(parse_composition(SMALL), 100)
        ambiguous = [cell for cell in cells if cell.slice is Slice.AMBIGUOUS]
        assert ambiguous
        assert all(cell.intended == 0 for cell in ambiguous)

    def test_a_negative_count_is_refused(self):
        with pytest.raises(CompositionError, match="must not be negative"):
            resolve(parse_composition(SMALL), -1)


# -- deficit rows -----------------------------------------------------------


def _cell(intended, problem_class=ProblemClass.WRONG_MATCH):
    return Cell(
        slice=Slice.SYNTHETIC,
        media_kind=MediaKind.MOVIE,
        problem_class=problem_class,
        share=0.1,
        intended=intended,
    )


class TestDeficits:
    def test_a_cell_asked_for_nothing_produces_no_row(self):
        assert deficits([_cell(0)], {}, {}) == ()

    def test_a_filled_cell_produces_no_row(self):
        key = (Slice.SYNTHETIC, MediaKind.MOVIE, ProblemClass.WRONG_MATCH)
        assert deficits([_cell(3)], {key: 3}, {}) == ()

    def test_a_short_cell_carries_its_recorded_reason(self):
        key = (Slice.SYNTHETIC, MediaKind.MOVIE, ProblemClass.WRONG_MATCH)
        (row,) = deficits([_cell(3)], {key: 1}, {key: (DeficitReason.CAPPED, "cap of 3")})
        assert (row.intended, row.achievable, row.reason, row.detail) == (
            3,
            1,
            DeficitReason.CAPPED,
            "cap of 3",
        )

    def test_a_short_cell_with_no_recorded_reason_says_the_generator_is_at_fault(self):
        """House rule 12: a shortfall nobody explained is reported, and the report says
        it is a gap in the generator rather than a fact about the library."""
        (row,) = deficits([_cell(2)], {}, {})
        assert row.reason is DeficitReason.NO_CANDIDATES
        assert "gap in the generator" in row.detail

    def test_there_are_five_reasons(self):
        """The plan named four. `not_curated` is the fifth: folding an empty
        `real.toml` into `no_candidates` would say the library has no such items
        when the truth is that nobody has labelled any yet."""
        assert {str(reason) for reason in DeficitReason} == {
            "not_implemented",
            "no_candidates",
            "rejected",
            "capped",
            "not_curated",
        }


# -- refusals ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (b"not = [toml", "not readable TOML"),
        (b"[slices]\nsynthetic = 1\n[media.movie]\nshare = 1\n[extra]\n", "unexpected top-level"),
        (b"[slices]\nsynthetic = 1\n", "needs both"),
        (b"[slices]\nbogus = 1\n[media.movie]\nshare = 1\n[media.movie.classes]\n", "Known slices"),
        (b"[slices]\nsynthetic = 1\n[media.film]\nshare = 1\n", "Known media kinds"),
        (b"[slices]\nsynthetic = 1\n[media.movie]\n[media.movie.classes]\n", "declares no `share`"),
        (
            b"[slices]\nsynthetic = 1\n[media.movie]\nshare = 1\n",
            "declares no \\[media.movie.classes\\]",
        ),
        (
            b"[slices]\nsynthetic = 1\n[media.movie]\nshare = 1\n"
            b"[media.movie.classes]\nwrongmatch = 1\n",
            "Known problem classes",
        ),
        (
            b"[slices]\nsynthetic = 0\n[media.movie]\nshare = 1\n"
            b"[media.movie.classes]\nwrong_match = 1\n",
            "no proportion can be derived",
        ),
        (
            b"[slices]\nsynthetic = 1\nreal = -1\n[media.movie]\nshare = 1\n"
            b"[media.movie.classes]\nwrong_match = 1\n",
            "set it to 0 to exclude it",
        ),
    ],
    ids=[
        "unparseable",
        "extra-table",
        "no-media",
        "unknown-slice",
        "unknown-medium",
        "no-share",
        "no-classes",
        "unknown-class",
        "zero-total",
        "negative-share",
    ],
)
def test_a_malformed_composition_names_its_fix(payload, match):
    """Every refusal is correctable, so every message names the next action."""
    with pytest.raises(CompositionError, match=match):
        parse_composition(payload)


def test_a_missing_composition_file_says_where_it_belongs(tmp_path):
    with pytest.raises(CompositionError, match="--composition"):
        load_composition(tmp_path / "composition.toml")
