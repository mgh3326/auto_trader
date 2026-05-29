# ROB-352 Slice C — candidate_universe collector hygiene (design)

**Status:** approved 2026-05-29. Follows Slice A (#994) + Slice B (#996).

**Goal:** Fix two contract-hygiene defects in the snapshot-backed `candidate_universe` collector — duplicate symbols and silent truncation — without touching candidate *strategy*.

## Scope decision (locked)

ROB-352 Slice C is **thin contract-hygiene only**, confined to
`app/services/action_report/snapshot_backed/collectors/candidate_universe.py`.

**Explicitly delegated, NOT in this slice:**
- Candidate ranking / priority / microcap·liquidity·stale filters / `buy_review`·`watch_only`·`rejected`·`data_gap` classification → **ROB-346** (its verbatim scope).
- Held-vs-new-buy item classification → already implemented in `auto_emit.py` (`_held_kis_symbols` / `classify_held_symbol`, which has the portfolio snapshot); deeper work → ROB-346/ROB-350. The collector has no holdings context, so tagging held there would be redundant and misplaced.

No broker/order/watch/order-intent/trade-journal mutation. No scheduler. No new dependency. No migration.

## Change 1 — dedupe candidates by normalized symbol

Screener rows can contain the same instrument under symbol-format variants (the reported `BRK.B` duplicate: `BRK.B` / `BRK-B` / `BRK/B`). Before `build_candidate_evidence`, dedupe the fetched rows preserving first (highest-ranked, since rows arrive ordered `change_rate DESC`) occurrence.

- New module helper `_dedupe_rows(rows, *, key)` — order-preserving dedupe on `key(row)`.
- Equity branch (`_collect_equity`): `key=lambda r: to_db_symbol(r.symbol)` (from `app.core.symbol`), so `.`/`-`/`/` variants collapse.
- Crypto branch (`_collect_crypto`): `key=lambda r: r.symbol` (crypto tickers like `KRW-BTC` are not stock symbols; raw value).
- Ranks are assigned over the deduped set (existing `enumerate(..., start=1)` in `_build_candidate_result`), so `rank`/`candidate_rank` stay gap-free 1..n.

## Change 2 — surface the cap (no silent truncation)

`list_top_candidates(limit=...)` silently cuts the universe at `candidate_limit`. Make the truncation explicit by adding two fields to the payload and coverage:

- `universe_count` = `fresh_count + stale_count` (the partition's available row counts the collector already has).
- `capped` = `universe_count > candidate_limit` (bool).

`candidate_limit` and `candidate_count` are already emitted. A consumer now sees "showing N (limit L) of universe M" rather than an unannounced cut.

## Testing

Collector unit tests (`tests/services/action_report/test_candidate_universe_collector_evidence.py`, real-DB `db_session` fixture + fake repos, matching the existing style):
- **Dedup**: a fake equity repo returning `BRK.B`, `BRK-B` (variant), and a distinct symbol → candidates collapse the variant to one row; `candidate_count` reflects dedupe; ranks are 1..n contiguous.
- **Cap surfaced**: universe (`fresh+stale`) > `candidate_limit` → payload/coverage `capped is True` and `universe_count` correct; universe ≤ limit → `capped is False`.
- Existing collector tests stay green (new fields are additive; dedup is a no-op on already-unique fakes).

## Files

- Modify: `app/services/action_report/snapshot_backed/collectors/candidate_universe.py`
- Test: `tests/services/action_report/test_candidate_universe_collector_evidence.py`
