"""Corruption functions and their registry (step 0.5).

Fifteen problem classes across `movies.py`, `tv.py`, and `audiobooks.py`. Two
invariants hold for every one of them: `apply_reverse(changes)` reproduces the
ground-truth item byte-for-byte, and no case ships without a
`DetectabilityWitness` that the scorer's own comparator accepts.
"""
