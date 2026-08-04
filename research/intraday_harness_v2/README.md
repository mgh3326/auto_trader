# Intraday harness v2

Contract version: `intraday-harness-v2.0.0`.

This package supports only bar-close signals, strictly-next-bar open market
fills, all-or-none execution, fail-closed missing/incomplete bars, and
separate fee/slippage accounting. Limit, partial, stop/target ordering, and
extended-hours semantics are intentionally absent.

`contract.py` verifies the SHA-256 of `engine.py` at import time. Updating
execution semantics therefore requires an explicit contract freeze update.
