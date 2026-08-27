"""External metadata sources — TMDB, TVDB, Audnexus, Open Library (step 1.1).

`base.py` owns the shared HTTP client and the per-source throttle, cache, and retry
policy; each source module translates responses into `EvidenceRecord`s. Rate limits
are enforced in code, never left to a prompt.

With `library/`, this package is the Phase 5 MCP extraction boundary and must not
import from `agent/` — enforced by an import contract in pyproject.toml.
"""
