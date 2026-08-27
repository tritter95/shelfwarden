"""Tool implementations and the phase-keyed registry (step 1.3).

`registry.py` keys tools by `RunPhase` — the structural read/write seam behind
spec §3.2. `mutating.py` arrives in Phase 3 and is never imported by the read-only
registry.

This package is the Phase 5 MCP extraction boundary: it must not import
`agent.loop`, `agent.provider`, or `evals`, enforced by the import contract "the
tool layer is MCP-extractable" in pyproject.toml.
"""
