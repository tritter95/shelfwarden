"""One pointer grammar: RFC 6901, plus a wildcard that only a selector may use.

A leaf module beside `canonical.py` and `compare.py`. Pure functions over
already-serialized JSON documents -- no I/O, no clock, no model imports --
because the same grammar addresses three different documents on both sides of
the Phase 5 MCP seam:

| What it addresses | Written by | Read by | Step |
|---|---|---|---|
| a field of an item dump | `FieldChange.path` | `apply_reverse`, scorer | 0.5 / 0.8 |
| fields a repair may not touch | `must_not_change` | the scorer | 0.6 / 0.8 |
| a value in an evidence body | `witness.pointers` | generator, `agent/validate.py` | 0.5 / 1.4 |

That last row is why this file is top-level rather than `evals/pointer.py`: the
agent must not import the package holding the answer key, and the Phase 5
extraction must not have to carry `evals/` with it. An import contract enforces
the leaf property.

**Why one grammar rather than two.** The truth file carries a path into an item
(`corruption.changes[].path`) and a pointer into an evidence body
(`witness.pointers`) in the same document. Two grammars there is one resolver
call away from a silent bug in the scorer: the wrong resolver on the wrong
string returns a plausible answer rather than an error. `implementation-plan.md`
§3 illustrates the item paths as `guids` and `parts[*].file`; they are `/guids`
and `/parts/*/path` here -- the syntax per the paragraph above, and the field
name because the normalized model calls it `FilePart.path` and has no `file`.

**The wildcard is an extension, and it is checked rather than assumed.** RFC 6901
has no wildcard, and `must_not_change` needs one. `*` occupying a whole segment
matches every element of an array or every key of an object. It is legal in a
*selector* and illegal in a *pointer*: a change addresses exactly one location, a
constraint may address many. Verified across all seven item subtypes plus
`FilePart`, `ItemStub`, `ItemId`, and `ExternalId` -- 51 distinct field names --
that none contains `/`, `~`, or `*`, so the reservation costs nothing here. It is
still checked, because an evidence body is not this model: `select` raises where a
wildcard segment meets a mapping holding a literal `*` key, rather than quietly
reinterpreting someone's data. Such a document is simply not addressable by this
grammar -- recorded as a ceiling rather than papered over with an escape nobody
would ever type.
"""

from collections.abc import Iterator, Sequence
from typing import Any

# The value space of a parsed JSON document. Recursive, so the alias is a string
# forward reference to itself; `Any` is the leaf because a document that has been
# through `canonical_json` can hold anything JSON can express.
type JSONValue = Any

WILDCARD = "*"
SEPARATOR = "/"

# RFC 6901 §3: `~1` is `/` and `~0` is `~`. Unescaping is order-sensitive -- `~01`
# must decode to `~1` and not to `/` -- so `~1` is replaced first on the way in
# and `~` is escaped first on the way out.
_ESCAPES = ((("~1"), "/"), (("~0"), "~"))


class PointerError(Exception):
    """A pointer is malformed, or names nothing in the document it was given."""


def escape(token: str) -> str:
    """Encode one reference token."""
    return token.replace("~", "~0").replace("/", "~1")


def unescape(token: str) -> str:
    """Decode one reference token, `~1` before `~0`."""
    for escaped, plain in _ESCAPES:
        token = token.replace(escaped, plain)
    return token


def parse(pointer: str) -> tuple[str, ...]:
    """Split a pointer into decoded reference tokens.

    The empty pointer is the whole document and parses to no tokens. Anything
    else must start with `/`; a bare `parts/0` is a common typo that would
    otherwise resolve against a document whose first key happened to be
    `parts/0`, which is exactly the silent-wrong-answer shape the escaping rules
    exist to prevent.
    """
    if pointer == "":
        return ()
    if not pointer.startswith(SEPARATOR):
        raise PointerError(
            f"{pointer!r} is not a JSON pointer: it must be empty or start with '/'. "
            f"Did you mean {SEPARATOR + pointer!r}?"
        )
    return tuple(unescape(token) for token in pointer.split(SEPARATOR)[1:])


def build(tokens: Sequence[str]) -> str:
    """The inverse of `parse`."""
    return "".join(SEPARATOR + escape(token) for token in tokens)


def has_wildcard(pointer: str) -> bool:
    return WILDCARD in parse(pointer)


def _index(token: str, length: int, pointer: str) -> int:
    """Decode an array index token, strictly.

    RFC 6901 forbids leading zeros, and reserves `-` for the position after the
    last element -- a JSON Patch append target. Nothing here ever appends, so `-`
    is rejected rather than silently treated as an index.
    """
    if token == "-":
        raise PointerError(
            f"{pointer!r} uses '-', which names the position after the last element. "
            "Nothing in this project appends through a pointer; name an index."
        )
    if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
        raise PointerError(f"{pointer!r}: {token!r} is not an array index")
    index = int(token)
    if index >= length:
        raise PointerError(f"{pointer!r}: index {index} is out of range for {length} element(s)")
    return index


def resolve(document: JSONValue, pointer: str) -> JSONValue:
    """The single value a pointer names. Raises rather than returning a default.

    A missing location is an error, never `None`: a witness that resolved `None`
    from a pointer into a body that does not have that field would be a claim
    with no referent, and returning a default is how one gets recorded.
    """
    tokens = parse(pointer)
    if WILDCARD in tokens:
        raise PointerError(
            f"{pointer!r} contains a wildcard. A pointer addresses one location; "
            "use `select` for a selector."
        )
    current = document
    for depth, token in enumerate(tokens):
        if isinstance(current, dict):
            if token not in current:
                raise PointerError(f"{pointer!r}: no key {token!r} at {build(tokens[:depth])!r}")
            current = current[token]
        elif isinstance(current, list):
            current = current[_index(token, len(current), pointer)]
        else:
            raise PointerError(
                f"{pointer!r}: {build(tokens[:depth])!r} is a {type(current).__name__}, "
                f"which has no member {token!r}"
            )
    return current


def _walk(
    document: JSONValue, tokens: Sequence[str], prefix: list[str], selector: str
) -> Iterator[tuple[str, JSONValue]]:
    if not tokens:
        yield build(prefix), document
        return
    token, rest = tokens[0], tokens[1:]
    if token == WILDCARD:
        if isinstance(document, list):
            for index, value in enumerate(document):
                yield from _walk(value, rest, [*prefix, str(index)], selector)
        elif isinstance(document, dict):
            if WILDCARD in document:
                # The one place the extension could be ambiguous, checked rather
                # than assumed: this document has a member genuinely named `*`, so
                # a wildcard segment here could mean "every key" or "that key" and
                # the grammar cannot say which. Verified in step 0.5 that no field
                # of this model is named `*`, which is what makes reserving it
                # free -- but an evidence body is not this model.
                raise PointerError(
                    f"{selector!r}: the document has a literal {WILDCARD!r} key at "
                    f"{build(prefix)!r}, which this grammar cannot distinguish from "
                    "the wildcard. Such a document is not addressable here."
                )
            # Sorted, not insertion-ordered. A selector's results are recorded and
            # compared, and `canonical_json` sorts keys anyway -- inheriting field
            # declaration order here would make the two disagree for no reason.
            for key in sorted(document):
                yield from _walk(document[key], rest, [*prefix, key], selector)
        # A wildcard over a scalar matches nothing. That is not an error: it is
        # what `/parts/*/path` means on an item whose `parts` is empty.
        return
    if isinstance(document, dict):
        if token in document:
            yield from _walk(document[token], rest, [*prefix, token], selector)
        return
    if isinstance(document, list):
        try:
            index = _index(token, len(document), selector)
        except PointerError:
            return
        yield from _walk(document[index], rest, [*prefix, token], selector)


def select(document: JSONValue, selector: str) -> tuple[tuple[str, JSONValue], ...]:
    """Every `(pointer, value)` a selector matches, in document order.

    A selector without a wildcard matches at most one location, so a plain
    pointer is a selector -- one grammar with one extension rather than two
    languages. Unlike `resolve`, a selector that matches nothing returns an empty
    result rather than raising: "no part has a path" is an answer, and
    `must_not_change` on an item with no parts is vacuously satisfied.
    """
    tokens = parse(selector)
    return tuple(_walk(document, tokens, [], selector))


def set_at(document: JSONValue, pointer: str, value: JSONValue) -> None:
    """Replace the value a pointer names, in place.

    Deliberately cannot create: the parent and the final member must both already
    exist. A `NormalizedItem` dump carries every field including the unset ones,
    so a path that names nothing is a typo, and a setter that helpfully created
    it would put a key into the record that no model field will ever validate --
    discovered later as an `extra="forbid"` failure with no clue where it came
    from.
    """
    tokens = parse(pointer)
    if not tokens:
        raise PointerError("cannot replace the whole document through a pointer")
    if WILDCARD in tokens:
        raise PointerError(f"{pointer!r} contains a wildcard; a change addresses one location")
    parent = resolve(document, build(tokens[:-1]))
    token = tokens[-1]
    if isinstance(parent, dict):
        if token not in parent:
            raise PointerError(f"{pointer!r}: no key {token!r} to replace")
        parent[token] = value
        return
    if isinstance(parent, list):
        parent[_index(token, len(parent), pointer)] = value
        return
    raise PointerError(
        f"{pointer!r}: {build(tokens[:-1])!r} is a {type(parent).__name__} and has no members"
    )


__all__ = [
    "SEPARATOR",
    "WILDCARD",
    "JSONValue",
    "PointerError",
    "build",
    "escape",
    "has_wildcard",
    "parse",
    "resolve",
    "select",
    "set_at",
    "unescape",
]
