"""LLM provider adapters (steps 1.2 and 1.8).

`base.py` defines the thin `LLMProvider` protocol — `Proposal`, `ToolSpec`,
`Usage`, and the provider-opaque `ProviderCarryover`. `openai.py` and
`anthropic.py` are the only modules permitted to import their respective SDKs,
enforced by import contracts in pyproject.toml.

Raw responses are stored verbatim here; that is what makes Phase 2 deterministic
replay a config change rather than a rewrite.
"""
