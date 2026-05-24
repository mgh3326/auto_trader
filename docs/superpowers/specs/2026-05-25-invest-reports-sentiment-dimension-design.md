# `/invest/reports` Sentiment Dimension Evidence — Slice 5

- **Date**: 2026-05-25
- **Status**: Design approved, pending spec review
- **Branch**: `rob-312`
- **Linear**: [ROB-312](https://linear.app/mgh3326/issue/ROB-312) (parent: ROB-306; ROB-308 loop; ROB-310 News; ROB-311 Fundamentals)

## Context

Slice 5 of the TradingAgents-style `/invest/reports` program. The dimension machinery is generic — `investment_dimension_reports` already supports `dimension="sentiment"` (table + `/hermes/dimension-reports` ingest + read), and the context export carries `dimension_reports`. So Sentiment needs **no new table, endpoint, migration, or schema** — only a deterministic per-symbol evidence assembler + context wiring, mirroring `fundamentals_evidence` (ROB-311).

**Sentiment-source finding:** the repo has no social feed, no DB-persisted Fear&Greed, no stored analyst ratings. The only **distinct, DB-backed, populated, non-LLM** sentiment proxy is **`investor_flow_snapshots`** (ROB-205): KR foreign/institution net buy/sell flows, `double_buy`/`double_sell`, consecutive-buy streaks. (RSI/momentum would double-count Market; `NewsAnalysisResult` is LLM-derived; Fear&Greed is live-API-only — all rejected.) So Sentiment = **KR investor-flow consensus**, genuinely distinct from Market (price breadth) and News (citations). Unlike News/Fundamentals/crypto (empty pending ingestion), `investor_flow_snapshots` is **already populated** (ROB-205; used by the screener `double_buy` preset), so Sentiment can surface real data.

## Decisions locked (with the user)

- **KR-only** — investor-flow data is KR-only (`market IN ('kr')`). For US/crypto the assembler returns `unavailable` (graceful, empty `per_symbol`). Intended, documented limitation; multi-market sentiment awaits a social/analyst source.
- **Per-symbol** (Fundamentals pattern) — symbols = holdings ∪ market top movers.
- Deterministic, **DB-only (no live calls)**; Hermes authors the prose via existing `/hermes/dimension-reports` (dimension="sentiment"). No in-process LLM (ROB-287). Read-only; no broker mutation.

## Architecture / scope

### S1. `app/services/investment_dimensions/sentiment_evidence.py` (deterministic, DB-only)

```python
async def build_sentiment_evidence(
    flow_repo: InvestorFlowSnapshotsRepository,
    *, market: str, symbols: Set[str], now: dt.datetime | None = None,
) -> dict[str, Any]
```

- Non-KR market → return `unavailable` immediately (`per_symbol: []`, `data_health.requested` reflects input, reason captured in freshness status). No query.
- KR → `flow_repo.latest_by_symbols(market="kr", symbols=symbols)` (existing method — newest snapshot per symbol). Per symbol map: `foreign_net`, `institution_net`, `double_buy`, `double_sell`, `foreign_consecutive_buy_days`, `institution_consecutive_buy_days`.

Returns:

```python
{
  "market": market,
  "per_symbol": [{"symbol", "foreign_net", "institution_net", "double_buy",
                  "double_sell", "foreign_consecutive_buy_days",
                  "institution_consecutive_buy_days"}],
  "covered_count": int,
  "freshness": {"status": "fresh|stale|unavailable", "latest_snapshot_date": str | None},
  "data_health": {"requested": int, "covered": int},
}
```

Soft-fail: empty (or non-KR) → `covered_count: 0`, `freshness.status: "unavailable"`, never raises. Freshness `fresh` when latest `snapshot_date` within a window (`FRESH_WINDOW_DAYS = 5`, KR trading-day-ish), else `stale`.

### S2. Context wiring (`app/services/investment_stages/hermes_context.py`)

Add a `dimension_evidence["sentiment"]` block immediately after the `["fundamentals"]` block, inside the `kr`/`us` guard, best-effort `try/except`. `symbols` = portfolio holdings ∪ market-dimension `top_movers` (reuse the exact set the Fundamentals block builds — extract that gathering into a small local or recompute identically). Construct `InvestorFlowSnapshotsRepository` from `self._session`. For a `us` bundle the assembler returns `unavailable` (no KR flow data) — that's expected and surfaced, not an error.

### S3. Tests

- `sentiment_evidence` assembler: fixture-seed `investor_flow_snapshots` (KR) → assert `per_symbol` (net flows, double_buy, streaks) / `covered_count` / `freshness`; empty → graceful `unavailable`; **non-KR market (`us`) → `unavailable` with empty `per_symbol`** (and no query attempted); partial coverage → `data_health.requested > covered`.
- Context export: a `kr` run with seeded flows yields `dimension_evidence["sentiment"]` with populated `per_symbol`; a `us` run yields `unavailable`; soft-fail path.

## What's free (no implementation)

Sentiment dimension **report** (prose, stance, confidence): Hermes writes it via existing `POST /hermes/dimension-reports` with `dimension="sentiment"`. Persistence, read surface, and composition citation all work via the ROB-306/308 generic contract.

## Non-goals / boundaries

- No new table / endpoint / migration / schema. No live calls. No in-process LLM. No broker/order/watch/order-intent mutation.
- **KR-only by design** — US/crypto sentiment deferred (no distinct DB source). No RSI/momentum (double-counts Market). No LLM news-sentiment, no Fear&Greed.
- Reuse existing `InvestorFlowSnapshotsRepository.latest_by_symbols(*, market, symbols, as_of=None)` — no new repo method.

## Testing strategy

Fixture-based only: seed `investor_flow_snapshots` rows via model in `db_session`. Confirm ROB-287 no-internal-LLM import guard still passes and the assembler imports no broker client / LLM (only the investor-flow repo).

## Assumptions to verify during implementation

- `InvestorFlowSnapshotsRepository.latest_by_symbols(*, market, symbols, as_of=None) -> list[InvestorFlowSnapshot]` — confirmed at `app/services/investor_flow_snapshots/repository.py` (newest snapshot per symbol, KR-normalized).
- The context exporter's Fundamentals block already builds `holdings ∪ top_movers`; reuse the same set for the Sentiment block (hoist into one local computed once, or replicate identically) to avoid drift.
- `InvestorFlowSnapshot` columns: `foreign_net`, `institution_net`, `double_buy`, `double_sell`, `foreign_consecutive_buy_days`, `institution_consecutive_buy_days`, `snapshot_date`, `market` (kr) — confirmed at `app/models/investor_flow_snapshot.py`.

## Program order

Parent: ROB-306. Slice 5 (Sentiment, KR investor-flow). Remaining: crypto Market evidence (after ROB-282), earnings-in-Fundamentals follow-up, multi-market Sentiment (needs social/analyst source). Loop-validation tooling = ROB-309 (done).
