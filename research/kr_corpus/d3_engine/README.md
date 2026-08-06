# D3 event-driven portfolio engine

This package is the deterministic, research-only D3 engine for arms B0/C1/C2/C3.
It is isolated from D2 Stage-B and from broker, order, account, database, scheduler,
environment, and in-process LLM surfaces.

The local acceptance command consumes the immutable external golden artifact as a
read-only oracle. It never regenerates expected values and never imports the
provenance-only `krx_tick_size_frozen.py` file.

Production research callers provide the complete, ascending XKRX session axis in
`PortfolioRunInput.market_sessions`; this makes missing symbol bars reset indicator
continuity and makes T+2 settlement session-based. Both `original_valid_bar` and
`clamp_admit_v1` data-view labels are supported. Candidate arms derive their
counterfactual-demand accounting from a deterministic B0 shadow run on the same
input, so policy rejections cannot disappear from the cash-exhaustion axis.

```bash
uv run python -m research.kr_corpus.d3_engine.acceptance
uv run pytest tests/research/kr_corpus/d3_engine -v
```

The acceptance smoke runs only synthetic single-session checks for all four arms.
It does not run the primary 16 physical exploration jobs and does not access any
2025+ holdout or calibration input.
