"""The ordered guard chain (Phase 2).

schema → existence → budget → rate limit → loop detection → business rules. The
order is load-bearing: cheap structural rejections precede anything that costs a
network call or a token.
"""
