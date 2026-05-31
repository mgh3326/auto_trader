# ROB-377 PR1 — Crypto market index via CoinGecko `/global`

**Date:** 2026-05-30
**Issue:** ROB-377 §A1 (parent ROB-369 Slice 4)
**Scope:** PR1 of a 3-PR slicing. This PR covers **A1** only (crypto market
regime: total market cap + BTC dominance, and surviving the Hermes bundle
`market` dimension for crypto). A2 (OI/LSR) is PR2; Upbit digital-asset
indices + altseason are PR3; on-chain/ETF/liquidations are parked.

## Problem

`get_market_index` supports only equity indices
(`KOSPI/KOSDAQ/SPX/SP500/NASDAQ/DJI/DOW/VIX`) — zero crypto coverage. As a
consequence, the deterministic Hermes-bundle `market` stage fails closed for
crypto: `_MARKET_TO_INDEX_SYMBOLS["crypto"] = []` (collector) and
`_PRIMARY_INDEX_BY_MARKET["crypto"] = ()` (stage), so `MarketStage.run()` raises
`UnavailableStageError("market index unavailable for crypto")`. Crypto reports
therefore have no market-regime signal via MCP and depend on out-of-band
(9222 Upbit) inspection.

## Goal (acceptance, ROB-377 AC ①②)

1. `get_market_index` accepts crypto pseudo-symbols and returns crypto market
   regime data (total market cap, BTC dominance).
2. The Hermes-bundle `market` dimension produces **real data** for crypto
   (not a fail-closed `UnavailableStageError`) whenever CoinGecko `/global` is
   reachable.

Out of scope for PR1: OI/LSR (PR2), altseason / Upbit indices (PR3),
on-chain / ETF / liquidations (parked — see ROB-355 for liquidation vendor gap).

## Approach

The `get_market_index` handler already dispatches on `meta["source"]`
(`"naver"` → KR, else → yfinance/US). Add a **third `"coingecko"` branch** that
returns rows in the **same shape** existing branches emit
(`{symbol, name, current, change, change_pct, source}`). Because the snapshot
collector (`_collect_indices`) and `MarketStage` already consume that shape via
`handle_get_market_index`, no collector/registry/stage *logic* changes are
needed — only the per-market symbol tables get their crypto cell filled.

Chosen tool shape (user-decided, one-way door): **extend `get_market_index`**
(not a new `get_crypto_market_index` tool), so the existing registry quote fn
(`_build_market_index_quote_fn`) feeds the crypto market dimension with zero
plumbing churn.

## Components & changes

### 1. Index metadata — `app/mcp_server/tooling/fundamentals_sources_indices.py`

Add two crypto entries to `_INDEX_META`:

```python
"CRYPTO": {"name": "암호화폐 총 시가총액", "source": "coingecko", "cg_metric": "total_market_cap"},
"BTC.D":  {"name": "BTC 도미넌스",        "source": "coingecko", "cg_metric": "btc_dominance"},
```

`_DEFAULT_INDICES` is **unchanged** (crypto symbols are not added to the
no-argument equity default list — crypto is fetched explicitly).

Add a crypto-index fetcher. To avoid a third raw `/global` caller and reuse the
existing 30-minute cache, route through
`app.services.external.btc_dominance.fetch_btc_dominance()`, after extending
that function **additively** (see §4). New helper:

```python
async def _fetch_index_crypto_current(cg_metric: str, name: str, symbol: str) -> dict[str, Any]:
    """Crypto market-regime "index" row from CoinGecko /global (cached).

    Row shape matches the KR/US index rows so the snapshot collector and
    MarketStage consume it unchanged. ``CRYPTO`` carries a usable change_pct
    (24h total-market-cap change). ``BTC.D`` reports the dominance level only
    (CoinGecko /global has no dominance 24h change) → change_pct=None, which the
    collector intentionally drops and MarketStage skips (no fabricated 0.0%).
    """
```

Returned fields per symbol:

| symbol  | current                | change_pct                                   | notes |
|---------|------------------------|----------------------------------------------|-------|
| CRYPTO  | total market cap (USD) | `market_cap_change_percentage_24h_usd` (%)   | regime driver |
| BTC.D   | BTC dominance (%)      | `None`                                       | level only |

On fetch failure (`fetch_btc_dominance()` returns `None`): raise so the handler
maps it to `_error_payload` — never fabricate values.

Export the new helper in `__all__`.

### 2. Handler — `app/mcp_server/tooling/fundamentals/_market_index.py`

Add a `meta["source"] == "coingecko"` branch alongside the existing
naver/yfinance branches:

```python
elif meta["source"] == "coingecko":
    current_data = await _fetch_index_crypto_current(meta["cg_metric"], meta["name"], sym)
    return {"indices": [current_data], "history": []}
```

- Crypto has no `/global` history → `history: []` (honest empty, documented).
- Existing single-symbol error handling (`_error_payload(source=meta["source"], ...)`)
  already wraps the branch — crypto failures surface as
  `_error_payload(source="coingecko", ...)`.
- The no-argument default-batch path is unchanged (still equity-only via
  `_DEFAULT_INDICES`).
- Symbol normalization `symbol.strip().upper()` is safe for `CRYPTO` / `BTC.D`.

### 3. Hermes plumbing — fill the crypto cell (logic unchanged)

- `app/services/action_report/snapshot_backed/collectors/market.py`:
  `_MARKET_TO_INDEX_SYMBOLS["crypto"] = ["CRYPTO"]`
- `app/services/investment_stages/stages/market.py`:
  `_PRIMARY_INDEX_BY_MARKET["crypto"] = ("CRYPTO",)`

Only `CRYPTO` goes into the collector's symbol list (it carries `change_pct`).
`BTC.D` is **not** added to the collector list for PR1 — the collector drops
change_pct-less rows, so adding it would be a no-op in the snapshot. `BTC.D`
remains directly queryable via the MCP tool. (Surfacing dominance *level* into
the bundle would require relaxing the collector to keep current-only rows — a
deferred follow-up, noted, not in PR1.)

Result: collector populates `indices = {"CRYPTO": {change_percent, name, current}}`;
`MarketStage` selects `CRYPTO` and emits BULL (≥ +0.5%) / BEAR (≤ −0.5%) /
NEUTRAL exactly as for equities.

### 4. CoinGecko `/global` reuse — `app/services/external/btc_dominance.py`

Extend `fetch_btc_dominance()` **additively** (existing keys unchanged) to also
return:

- `total_market_cap_usd`: `data.total_market_cap.usd` (float, nullable)
- `eth_dominance`: `data.market_cap_percentage.eth` (float, nullable)

Existing return keys `btc_dominance` and `total_market_cap_change_24h` are
preserved verbatim. Parse defensively (`round(float(...), 2)` guarded; `None`
when the field is absent — never fabricate). The 30-minute cache, lock, and KST
behavior are unchanged. This makes `btc_dominance.py` the single `/global`
consumer for total-cap + dominance regime data.

> Note: `crypto_insights.fetch_coingecko_global()` (a second `/global` caller)
> is **left untouched** in PR1 — it serves a different metrics-list consumer.
> Consolidating the two callers is out of scope here.

## Data flow

```
MCP get_market_index(symbol="CRYPTO")
  → handle_get_market_index  → _INDEX_META["CRYPTO"].source == "coingecko"
  → _fetch_index_crypto_current("total_market_cap", ...)
  → btc_dominance.fetch_btc_dominance()  → CoinGecko /global (cached 30m)
  → {symbol:"CRYPTO", current:<total_mcap_usd>, change_pct:<24h %>, source:"coingecko"}

Hermes bundle market dimension (crypto):
  registry _build_market_index_quote_fn → handle_get_market_index(symbol="CRYPTO", count=1)
  → collector _collect_indices → indices={"CRYPTO":{change_percent,...}}
  → MarketStage._select_index → BULL/BEAR/NEUTRAL
```

## Error handling & safety boundary

- **Read-only.** No broker / order / watch / order-intent mutation reachable.
- **Fail-open at the collector, fail-closed at the stage** — identical to the
  existing US path when yfinance `previous_close` is absent. If CoinGecko
  `/global` is unreachable, the crypto market dimension degrades to
  `UnavailableStageError` rather than fabricating a flat 0.0%.
- **No new external host.** `api.coingecko.com` is already in use
  (`btc_dominance.py`, `crypto_insights.py`).
- New external integration is **fail-open** (per ROB-377 safety boundary): a
  CoinGecko outage must not crash the bundle — only the crypto market dimension
  is reported unavailable; other dimensions are unaffected.

## Test plan (TDD)

Unit (pure / mocked, no live network):

1. `_INDEX_META` contains `CRYPTO` and `BTC.D` with `source == "coingecko"`.
2. `handle_get_market_index(symbol="CRYPTO")` (fetcher mocked) →
   `indices[0]` has `current == total_mcap`, `change_pct == 24h change`,
   `source == "coingecko"`, `history == []`.
3. `handle_get_market_index(symbol="BTC.D")` (fetcher mocked) →
   `current == dominance`, `change_pct is None`.
4. `handle_get_market_index(symbol="DOGE")` (unknown) → raises `ValueError`
   listing supported symbols (regression: unknown still errors).
5. CoinGecko failure (fetch returns `None`) →
   `handle_get_market_index(symbol="CRYPTO")` returns
   `_error_payload(source="coingecko", ...)` (fail-open, no fabrication).
6. `fetch_btc_dominance()` additive fields: `total_market_cap_usd` and
   `eth_dominance` present when payload has them; `None` when absent;
   existing keys unchanged. (mock httpx response)
7. Collector: crypto request + index_quote_fn →
   `payload["indices"]["CRYPTO"]["change_percent"]` set.
8. `MarketStage` crypto: snapshot with `CRYPTO` index → BULL/BEAR/NEUTRAL by
   threshold; snapshot without crypto index → `UnavailableStageError`
   (behavior unchanged when data absent).
9. Regression: existing KR/US `get_market_index` and MarketStage tests pass.

## Verification

Deterministic unit tests prove AC ①②. A live CoinGecko `/global` round-trip is
operator-verifiable but **not a merge gate** (consistent with the other
ROB-369 slices, where live smoke is operator-gated).

CI gate before merge (per repo convention): `ruff check app/ tests/` +
`ruff format --check app/ tests/` + `ty` + import guards + Test workflow green.

## Files touched

- `app/mcp_server/tooling/fundamentals_sources_indices.py` (meta + fetcher)
- `app/mcp_server/tooling/fundamentals/_market_index.py` (coingecko branch)
- `app/services/external/btc_dominance.py` (additive fields)
- `app/services/action_report/snapshot_backed/collectors/market.py` (crypto cell)
- `app/services/investment_stages/stages/market.py` (crypto cell)
- `app/mcp_server/__init__.py` (tool description mentions crypto symbols, if a
  description string exists for `get_market_index`)
- Tests: extend existing market-index / collector / market-stage test modules.

No migration. No new dependency.
