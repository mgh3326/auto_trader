# Intraday harness v2

Contract version: `intraday-harness-v2.0.0`.

This package supports only bar-close signals, strictly-next-bar open market
fills, all-or-none execution, fail-closed missing/incomplete bars, and
separate fee/slippage accounting. Limit, partial, stop/target ordering, and
extended-hours semantics are intentionally absent.

`contract.py` verifies the SHA-256 of `engine.py`, the public `__init__.py`
entry point, and itself at import time. The self-pin uses one hash value and
one placeholder marker; verification normalizes the value to the marker and
requires each to occur exactly once. Updating execution semantics therefore
requires an explicit contract freeze update. This is tamper-evident protection
against ordinary source drift, not an unbreakable Python sandbox: a coordinated
edit can retarget the pins, but it changes `CONTRACT_HASH` and remains visible
in review. Python cannot make deletion of the `verify_contract()` call sites
structurally impossible; that residual is detectable only by code review/diff.

`slippage` is an unsigned accounting cost. Consumers that model execution
direction must apply the BUY/SELL sign themselves; `fill_price` is unchanged.
Incomplete fills have quantity zero. `filled_count` and `filled_notional`
describe actual fills, while `signal_count` and `NO_SIGNALS` distinguish an
empty signal stream from a completed run. A missing next bar on a later
calendar date is reported as `NEXT_BAR_SESSION_GAP`; same-date missing data is
`NEXT_BAR_MISSING`.
