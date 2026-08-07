# D3 event-driven portfolio engine

This package is the deterministic, research-only D3 engine for arms B0/C1/C2/C3.
It is isolated from D2 Stage-B and from broker, order, account, database, scheduler,
environment, and in-process LLM surfaces.

The local acceptance command consumes the immutable external 33-case golden
artifact as a read-only oracle. It never regenerates expected values and never
imports the provenance-only `krx_tick_size_frozen.py` file. The additive R1c
correction suite executes three 180+ session arm-neutral clock cases and four
mutants without changing any of the frozen 33 cases or 23 prior mutants.

Production research callers provide the complete, ascending XKRX session axis in
`PortfolioRunInput.market_sessions`; this makes missing symbol bars reset indicator
continuity and makes T+2 settlement session-based. Both `original_valid_bar` and
`clamp_admit_v1` data-view labels are supported. Candidate arms derive their
counterfactual-demand accounting from a deterministic B0 shadow run on the same
input, so policy rejections cannot disappear from the cash-exhaustion axis.

Delisting/corporate-action ingestion must set
`CorporateAction.data_ends_before_exploration_end=True` when the authoritative
event ledger establishes that a nonzero position's last valid bar precedes the
exploration end. A bare missing symbol bar is intentionally not treated as a
delisting because it can also mean a trading halt; without that upstream event
annotation the engine cannot distinguish an unresolved terminal exposure from a
temporary omission. The engine then marks the run
`INCONCLUSIVE_UNRESOLVED_TERMINAL` and prevents an ordinary OK result.

```bash
uv run python -m research.kr_corpus.d3_engine.acceptance
uv run pytest tests/research/kr_corpus/d3_engine -v
uv run python scripts/run_d3_primary.py
```

The acceptance smoke runs a natural, non-vacuous indicator signal and exact L1/L2
fills for all four arms, plus isolated engine contract probes. It does not run the
primary 16 physical correction jobs and does not access any 2025+ holdout,
calibration, or prospective input. The correction harness writes to
`~/work/herdr-artifacts/d3-r1c-correction-replay-v1`, verifies the old/new
bit-exact contract, and preserves the original `d3-primary-run-v1` tree with a
`superseded-by.json` sidecar only after all acceptance gates pass.
