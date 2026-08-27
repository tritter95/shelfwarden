"""The corruption harness, the scorer, and everything that measures the agent.

Phase 0's deliverable and the thing every later claim is measured against:
`export.py` (0.4), `compare.py` + `screen.py` (0.45), `corrupt/` (0.5),
`truth.py` + `generate.py` (0.6), `score.py` + `report.py` (0.8).

`agent/tools/` may not import this package — a tool that can see the truth file is
not a tool, it is an oracle.
"""
