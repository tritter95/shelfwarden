"""The determinism traps the canonical serializer exists to close.

Each test pins a behaviour that a well-meaning simplification would reintroduce.
"""

import json
import unicodedata

import pytest

from shelfwarden.canonical import canonical_json, canonical_text


def test_keys_are_sorted_and_separators_are_compact():
    assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_key_order_does_not_change_the_bytes():
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


def test_non_ascii_is_written_as_utf8_not_escaped():
    assert canonical_json({"t": "Amélie"}) == '{"t":"Amélie"}'.encode()


def test_nfd_and_nfc_are_different_bytes_before_normalization():
    """The reason canonical_text exists. macOS filesystems hand out NFD."""
    nfc = unicodedata.normalize("NFC", "Amélie")
    nfd = unicodedata.normalize("NFD", "Amélie")
    assert nfc != nfd
    assert canonical_json({"t": nfc}) != canonical_json({"t": nfd})


def test_canonical_text_collapses_both_forms():
    nfd = unicodedata.normalize("NFD", "Amélie")
    assert canonical_text(nfd) == "Amélie"
    assert canonical_json({"t": canonical_text(nfd)}) == canonical_json({"t": "Amélie"})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_infinity_are_rejected_rather_than_written(value):
    """json.dumps emits bare NaN/Infinity by default, which is not JSON: Python
    reads it back, every other parser rejects the dataset."""
    with pytest.raises(ValueError):
        canonical_json({"r": value})

    # The behaviour being guarded against, so the test fails loudly if the flag is
    # ever dropped rather than passing for the wrong reason. These three literals
    # are what json.dumps emits by default, and none of them is valid JSON.
    assert json.dumps({"r": value}).removeprefix('{"r": ').removesuffix("}") in {
        "NaN",
        "Infinity",
        "-Infinity",
    }


def test_int_and_float_of_equal_value_are_not_interchangeable():
    """A rating that is sometimes 8 and sometimes 8.0 breaks byte-identity without
    changing value, so field types have to be stable."""
    assert canonical_json({"r": 8}) != canonical_json({"r": 8.0})


def test_round_trips_through_json_loads():
    payload = {"t": "Amélie", "n": [1, 2.5, None, True], "d": {"z": "é"}}
    assert json.loads(canonical_json(payload).decode()) == payload
