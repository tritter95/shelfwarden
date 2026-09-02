"""`evidence_id` is a content address, so its stability is the whole contract."""

import subprocess
import sys

from shelfwarden.models.evidence import DIGEST_PREFIX, Source, evidence_id


def _id(**overrides):
    payload = {
        "source": Source.LIBRARY,
        "endpoint": "export",
        "params": {"export_id": "exp-abc", "item_id": "plex:1:101"},
        "body": {"title": "Solaris", "year": 1972},
    }
    payload.update(overrides)
    return evidence_id(**payload)


def test_it_is_prefixed_and_hex():
    value = _id()
    assert value.startswith(DIGEST_PREFIX)
    assert len(value) == len(DIGEST_PREFIX) + 64


def test_the_same_retrieval_hashes_the_same():
    assert _id() == _id()


def test_key_order_in_params_and_body_does_not_change_the_id():
    """`canonical_json` sorts keys, which is what makes this a content address
    rather than a hash of one dict's construction order."""
    assert _id(body={"year": 1972, "title": "Solaris"}) == _id()


def test_every_component_changes_the_id():
    assert _id(source=Source.TMDB) != _id()
    assert _id(endpoint="search") != _id()
    assert _id(params={"export_id": "exp-abc", "item_id": "plex:1:102"}) != _id()
    assert _id(body={"title": "Solaris", "year": 2002}) != _id()


def test_the_join_is_unambiguous_across_a_pipe_in_a_component():
    """A raw pipe join would make these two retrievals collide. Each component is
    canonical-JSON encoded before the join precisely so they cannot."""
    assert _id(endpoint="a|b", params={"k": "c"}) != _id(endpoint="a", params={"k": "b|c"})


def test_ids_are_stable_across_processes():
    """Hash randomization must not reach it: a content address that depends on
    PYTHONHASHSEED is not an address."""
    program = (
        "from shelfwarden.models.evidence import Source, evidence_id;"
        "print(evidence_id(Source.LIBRARY, 'export', {'a': 1, 'b': 2}, {'t': 'x'}))"
    )
    digests = []
    for seed in ("0", "1"):
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": seed},
            check=False,
        )
        assert result.returncode == 0, result.stderr
        digests.append(result.stdout.strip())
    assert digests[0] == digests[1]


def test_the_library_is_a_source_like_any_other():
    """implementation-plan.md §6: a library read is evidence too. That is what
    lets an internally-derived claim carry a citation instead of inventing a
    second citation type."""
    assert Source.LIBRARY in set(Source)
    assert _id(source=Source.LIBRARY).startswith(DIGEST_PREFIX)
