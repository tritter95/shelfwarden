"""Canonical data models — the vocabulary every other package speaks.

`item.py` (roadmap step 0.2) holds `NormalizedItem` and its subtypes; `finding.py`
and `evidence.py` (step 1.4) hold the claim union and the evidence record.

Nothing here may import a provider SDK or `plexapi`: these types are what the
adapters translate *into*, which is the point of confining those libraries.
"""
