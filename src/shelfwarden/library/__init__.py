"""Library providers — the read/write seam (implementation-plan.md §2).

`base.py` defines the `LibraryProvider` protocol with **read methods only**; the
absence of a mutating method from the type is the structural half of spec §3.2.
`plex.py` (step 0.3) is the only module permitted to import `plexapi`, and
`snapshot.py` (step 0.7) serves the corrupted eval dataset through the identical
protocol.

Together with `sources/`, this package is the Phase 5 MCP extraction boundary: it
must not import from `agent/`. Both rules are enforced by import contracts in
pyproject.toml, not by convention.
"""
