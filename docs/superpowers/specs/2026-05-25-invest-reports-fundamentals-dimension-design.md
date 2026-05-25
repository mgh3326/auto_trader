# `/invest/reports` Fundamentals Dimension Evidence — Slice 4

- **Date**: 2026-05-25
- **Status**: Design approved, pending spec review
- **Branch**: `rob-311`
- **Linear**: [ROB-311](https://linear.app/mgh3326/issue/ROB-311) (parent: ROB-306; ROB-308 loop; ROB-310 News)

## Context

Slice 4 of the TradingAgents-style `/invest/reports` program. The dimension machinery is generic — `investment_dimension_reports` already supports `dimension="fundamentals"` (table + `/hermes/dimension-reports` ingest + read), and the context export carries `dimension_reports` (ROB-308). So Fundamentals needs **no new table, endpoint, migration, or schema** — only a deterministic per-symbol valuation evidence assembler + context wiring, mirroring `market_evidence` (ROB-306) / `news_evidence` (ROB-310).

DB-backed source: **`market_valuation_snapshots`** (`MarketValuationSnapshotsRepository`) carries per-symbol `per / pbr / roe / dividend_yield / market_cap / high_52w / low_52w` (source `naver_finance` for KR, `yahoo` for US), built by `scripts/build_market_valuation_snapshots.py`. Sector comes from `stock_info` (`StockInfoService.get_stock_info_by_symbol(...).sector`). **Both are empty/sparse — ingestion is manual/unscheduled** (operator gate, like research_reports / crypto). Build the path + fixture-test it; ingestion enablement deferred.

**Critical:** KIS/Yahoo `fetch_fundamental_info` is **live-API-only (never persisted)** and must NOT be called in the deterministic context-export path. The assembler reads `market_valuation_snapshots` (DB) only.

## Decisions locked (with the user)

- **Per-symbol** (per-company) — PER/PBR/ROE are company metrics; the report focuses on the symbols it acts on (held holdings ∪ candidate top movers).
- **Assembler-only + fixture-tested.** Ingestion enablement deferred (operator gate).
- Deterministic, **DB-only (no live broker calls)**; Hermes authors the Fundamentals report prose via existing `/hermes/dimension-reports` (dimension="fundamentals"). No in-process LLM (ROB-287). Read-only; no broker mutation.
- **Earnings (`market_events`) excluded from v1** (empty; follow-up). v1 = valuation + sector.

## Architecture / scope

### F1. `app/services/investment_dimensions/fundamentals_evidence.py` (deterministic, DB-only)

```python
async def build_fundamentals_evidence(
    valuation_repo: MarketValuationSnapshotsRepository,
    stock_info_service: StockInfoService,
    *, market: str, symbols: Set[str], now: dt.datetime | None = None,
) -> dict[str, Any]
```

For each requested symbol: latest `market_valuation_snapshots` row (PER/PBR/ROE/dividend_yield/market_cap/52w) + `stock_info` sector. Returns:

```python
{
  "market": "kr" | "us",
  "per_symbol": [{"symbol", "sector", "per", "pbr", "roe", "dividend_yield",
                  "market_cap", "high_52w", "low_52w"}],
  "covered_count": int,
  "freshness": {"status": "fresh|stale|unavailable", "latest_snapshot_date": str | None},
  "data_health": {"requested": int, "covered": int},
}
```

Soft-fail: zero coverage → `covered_count: 0`, `freshness.status: "unavailable"` (never raises). Numeric Decimals serialized to float/str JSON-safely. No prose.

### F1b. `MarketValuationSnapshotsRepository.latest_for_symbols(market, symbols)`

Add a read-only method (mirrors `coverage_counts` query style): for the given `market` + `symbols`, return the latest-`snapshot_date` row per symbol (across sources; prefer the newest). Returns `list[MarketValuationSnapshot]` (or a small DTO). Empty `symbols` → `[]`.

### F2. Context wiring (`app/services/investment_stages/hermes_context.py`)

Add a `dimension_evidence["fundamentals"]` block immediately after the `["news"]` block, `kr`/`us` only, best-effort `try/except` (mirror). `symbols` = portfolio holdings (already gathered for the market block's `held` set) ∪ market-dimension `top_movers` symbols (reuse the `market_evidence` result if available, else gather from `held` only). Construct `MarketValuationSnapshotsRepository` + `StockInfoService` from `self._session`.

### F3. Tests

- `fundamentals_evidence` assembler: fixture-seed `market_valuation_snapshots` (+ `stock_info` sector) → assert `per_symbol` rows / `covered_count` / `freshness`; empty → graceful `unavailable`; partial coverage (some symbols missing) → `data_health.requested > covered`.
- Context export: a kr/us run yields `dimension_evidence["fundamentals"]` with expected keys; soft-fail path.

## What's free (no implementation)

Fundamentals dimension **report** (prose, stance, confidence): Hermes writes it via existing `POST /hermes/dimension-reports` with `dimension="fundamentals"`. Persistence, read surface, and composition citation all work via the ROB-306/308 generic contract — untouched here.

## Non-goals / boundaries

- No new table / endpoint / migration / schema. **No live KIS/Yahoo fundamental fetch in the assembler** (DB only). No in-process LLM. No broker/order/watch/order-intent mutation.
- No `market_valuation_snapshots` ingestion enablement (separate operator gate; data deferred). No earnings/`market_events` in v1.
- Market-wide fundamentals not produced (per-symbol only).

## Testing strategy

Fixture-based only (no live ingestion, no broker API): seed `market_valuation_snapshots` + `stock_info` rows via repository/model in `db_session`. Confirm ROB-287 no-internal-LLM import guard still passes and that the assembler imports no broker client (only the valuation repo + stock_info service).

## Assumptions to verify during implementation

- `StockInfoService.get_stock_info_by_symbol` returns a row with `.sector` (nullable) — confirmed at `app/services/stock_info_service.py`.
- `latest_for_symbols` source-precedence: if a symbol has both `naver_finance` and `yahoo` rows on the same latest date, pick deterministically (e.g. by `computed_at` desc, then source asc) — lock in the plan.
- The context exporter can reach the market-dimension `top_movers` symbols (reuse the already-built `market_evidence` dict in the same `export` scope) without re-querying.

## Program order

Parent: ROB-306. Slice 4 (Fundamentals, valuation+sector). Next: Sentiment (#5), crypto Market evidence (after ROB-282), earnings-in-Fundamentals follow-up. Loop-validation tooling = ROB-309 (done).
