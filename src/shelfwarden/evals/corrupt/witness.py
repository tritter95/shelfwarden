"""Proof that a case is solvable at all, or the case is not emitted.

`implementation-plan.md` §3: *every corruption must prove its own detectability.*
An unsolvable case -- a summary nulled that was already empty, a foreign title
for a film with no alternate title, a swap into a record too ambiguous to
discriminate -- depresses the pass rate and hides real regressions, in the one
place nobody looks: a low score on a hard class reads as a model problem rather
than a harness problem.

**Three kinds, not one.** The witness `implementation-plan.md` specifies proves
one shape of solvability: a pointer resolves a value that differs from the
corrupted one and equals the ground truth. That covers the classes whose repair
restores a field, and it does not describe the other two shapes this step has to
emit:

* `VALUE` -- a field's true value is recoverable. Accepted when the policy is
  satisfied against the ground truth and **not** satisfied against the corrupted
  value. Inequality then equality, as specified.
* `RELATION` -- two or more ids are provably one thing. `duplicate_quality`,
  `author_name_variant`, and `multi_file_split` have a *set relation* as their
  ground truth -- *these entries are one work, and this one is the keeper* -- and
  no single resolved value is the answer. Accepted when the named comparator
  holds over the subjects **in the corrupted world**; a relation that only holds
  in the clean world proves nothing.
* `AMBIGUITY` -- at least `min_candidates` supported resolutions exist. This is
  what an `escalate` case needs, and a witness that resolved a unique value would
  prove the case is *not* ambiguous. Defined and unused: `anthology_omnibus` is
  curated rather than synthesized, so nothing emits one yet.

**The anti-circularity rule outranks all three.** A witness pointer must resolve
against the **corrupted** world, never against the ground truth. A witness citing
the pre-corruption record proves only that we knew the answer, which was never in
question -- it is the well-cited finding with an unbound referent that invariant 7
rejects, wearing a generator's badge. `LocalWitness` is constructed with the
corrupted family and has no access to the clean one, so the rule is structural
rather than remembered.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from shelfwarden.compare import Policy, Support
from shelfwarden.evals.screen import CheckSupport, item_evidence_id
from shelfwarden.models.evidence import Source, evidence_id
from shelfwarden.models.item import NormalizedItem, dump_item
from shelfwarden.pointer import JSONValue, PointerError, resolve

# The minimum number of distinct supported resolutions an AMBIGUITY witness must
# carry, matching the `min_candidates` floor step 0.6 puts on an escalate case.
# Silence is not escalation, and one candidate is not an ambiguity.
MIN_AMBIGUITY_CANDIDATES = 2


class WitnessKind(StrEnum):
    VALUE = "value"
    RELATION = "relation"
    AMBIGUITY = "ambiguity"


class WitnessTier(StrEnum):
    """Where the evidence came from, which is what decides whether a class can
    ship before step 1.1 lands `sources/`."""

    LOCAL = "local"
    AUTHORITY = "authority"


class DetectabilityWitness(BaseModel):
    """The evidence that makes a case solvable, and the verdict on it.

    `discriminates` is computed here from the comparators and never asserted by a
    caller -- the same rule that keeps `confidence` out of the model's hands, one
    layer down. A witness that does not discriminate is still returned, carrying
    `detail`, so the rejection can say what was missing rather than that
    something was.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: WitnessKind
    tier: WitnessTier
    source: Source
    evidence_id: str
    comparator: str
    # RELATION only: which relation is claimed (`same_work`, `same_author`,
    # `same_book`). A relation witness that cannot name its relation is a set of
    # ids with a similarity score attached.
    relation: str | None = None
    pointers: tuple[str, ...]
    resolved: tuple[JSONValue, ...] = ()
    subjects: tuple[str, ...] = ()
    support: tuple[CheckSupport, ...] = ()
    discriminates: bool = False
    detail: str | None = None


def _support(support: Support) -> CheckSupport:
    # `CheckSupport` is the pydantic mirror of `compare.Support`; it lives in
    # `screen.py` because 0.45 introduced it there, and it is reused rather than
    # duplicated so a screen check and a witness record the same shape.
    return CheckSupport(
        strength=support.strength, rule=support.rule, score=support.score, matched=support.matched
    )


def family_evidence_id(export_id: str, items: Sequence[NormalizedItem]) -> str:
    """Cite a set of library records as one retrieval.

    A single-subject witness uses `screen.item_evidence_id` instead, so that a
    witness and a screen check citing the same record produce the same id -- which
    is what lets step 0.8 ask whether the agent cited *the record the generator
    did* rather than merely some record.
    """
    ordered = sorted(items, key=lambda item: str(item.item_id))
    return evidence_id(
        Source.LIBRARY,
        "export",
        {"export_id": export_id, "item_ids": [str(item.item_id) for item in ordered]},
        [dump_item(item) for item in ordered],
    )


class LocalWitness:
    """Evidence taken from the corrupted world itself.

    Local does not mean weak. The realistic detector for a misfiled episode is
    the filename that still says `S01E02`; for a manufactured duplicate it is the
    twin sitting beside it. What local *does* mean is that no external source is
    needed, which is why eleven of the fifteen classes can ship before step 1.1.
    """

    tier = WitnessTier.LOCAL
    source = Source.LIBRARY

    def __init__(self, export_id: str, corrupted: Mapping[str, NormalizedItem]) -> None:
        self._export_id = export_id
        self._corrupted = dict(corrupted)

    @classmethod
    def over(cls, export_id: str, items: Sequence[NormalizedItem]) -> "LocalWitness":
        """Build a witness that can see the corrupted items and nothing else."""
        return cls(export_id, {str(item.item_id): item for item in items})

    def _resolves(self, subject_id: str, pointer: str) -> tuple[bool, str | None]:
        item = self._corrupted.get(subject_id)
        if item is None:
            return False, f"{subject_id} is not in the corrupted family"
        try:
            resolve(dump_item(item), pointer)
        except PointerError as exc:
            return False, str(exc)
        return True, None

    def value(
        self,
        *,
        subject_id: str,
        pointer: str,
        comparator: str,
        resolved: JSONValue,
        against_truth: Support,
        against_corrupted: Support,
        policy: Policy,
    ) -> DetectabilityWitness:
        """A VALUE witness: the field's true value is recoverable from the corruption.

        `against_truth` compares what the evidence yields with the ground-truth
        value; `against_corrupted` compares it with what the item now says. Both
        are required, and the second is the half that is easy to forget: evidence
        agreeing with the truth proves nothing if it also agrees with the
        corruption, because then it does not tell the two apart.
        """
        ok, problem = self._resolves(subject_id, pointer)
        supports = (_support(against_truth), _support(against_corrupted))
        if not ok:
            return DetectabilityWitness(
                kind=WitnessKind.VALUE,
                tier=self.tier,
                source=self.source,
                evidence_id="",
                comparator=comparator,
                pointers=(pointer,),
                subjects=(subject_id,),
                support=supports,
                detail=f"pointer does not resolve against the corrupted item: {problem}",
            )

        supports_truth = policy.satisfied_by(against_truth)
        contradicts = not policy.satisfied_by(against_corrupted)
        detail = None
        if not supports_truth:
            detail = (
                f"evidence does not support the ground truth "
                f"({against_truth.strength}/{against_truth.rule})"
            )
        elif not contradicts:
            detail = (
                "evidence supports the corrupted value too "
                f"({against_corrupted.strength}/{against_corrupted.rule}); it does not "
                "tell the two apart"
            )
        return DetectabilityWitness(
            kind=WitnessKind.VALUE,
            tier=self.tier,
            source=self.source,
            evidence_id=item_evidence_id(self._export_id, self._corrupted[subject_id]),
            comparator=comparator,
            pointers=(pointer,),
            resolved=(resolved,),
            subjects=(subject_id,),
            support=supports,
            discriminates=supports_truth and contradicts,
            detail=detail,
        )

    def relation(
        self,
        *,
        subject_ids: Sequence[str],
        relation: str,
        pointers: Sequence[str],
        comparator: str,
        supports: Sequence[Support],
        policy: Policy,
    ) -> DetectabilityWitness:
        """A RELATION witness: these ids are provably one thing, in the corrupted world."""
        subjects = tuple(subject_ids)
        recorded = tuple(_support(support) for support in supports)
        detail: str | None = None
        for subject_id, pointer in zip(subjects, pointers, strict=False):
            ok, problem = self._resolves(subject_id, pointer)
            if not ok:
                detail = f"pointer does not resolve against the corrupted family: {problem}"
                break

        if detail is None and len(subjects) < MIN_AMBIGUITY_CANDIDATES:
            detail = f"a relation needs at least two subjects, got {len(subjects)}"
        if detail is None and not supports:
            detail = "no comparison was made"
        if detail is None:
            weak = [s for s in supports if not policy.satisfied_by(s)]
            if weak:
                detail = (
                    f"{len(weak)} of {len(supports)} comparison(s) below the "
                    f"{policy.name} policy; the relation does not hold after corruption"
                )

        cited = [self._corrupted[s] for s in subjects if s in self._corrupted]
        resolved: tuple[JSONValue, ...] = ()
        if detail is None:
            resolved = tuple(
                resolve(dump_item(self._corrupted[subject_id]), pointer)
                for subject_id, pointer in zip(subjects, pointers, strict=False)
            )
        return DetectabilityWitness(
            kind=WitnessKind.RELATION,
            tier=self.tier,
            source=self.source,
            evidence_id=family_evidence_id(self._export_id, cited) if cited else "",
            comparator=comparator,
            relation=relation,
            pointers=tuple(pointers),
            resolved=resolved,
            subjects=subjects,
            support=recorded,
            discriminates=detail is None,
            detail=detail,
        )


__all__ = [
    "MIN_AMBIGUITY_CANDIDATES",
    "DetectabilityWitness",
    "LocalWitness",
    "WitnessKind",
    "WitnessTier",
    "family_evidence_id",
]
