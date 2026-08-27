"""Tracing (Phase 2).

`otel.py` is the single module that owns `gen_ai.*` attribute naming, so the
in-flight OTel GenAI semantic-convention rename is absorbed in one place. The
`trace` dependency group stays optional: the agent must run without a tracing
stack installed.
"""
