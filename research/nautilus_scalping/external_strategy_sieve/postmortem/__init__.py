"""ROB-384 — crypto strategy failure-mode postmortem + closure decision.

A read-only synthesis layer over the negative results of ROB-320 / 342 / 353 /
382 / 383. It re-parses existing counts-only artifacts (never raw market data),
assigns a deterministic failure-mode taxonomy, recomputes a fee grid, and emits
a per-candidate table + a single A/B/C closure decision.

No new strategy survey, hyperopt, parameter sweep, or campaign. No broker /
order / watch / order-intent / Demo / live / scheduler side effects. Pure
stdlib so it runs under ``uv run --no-project``.
"""
