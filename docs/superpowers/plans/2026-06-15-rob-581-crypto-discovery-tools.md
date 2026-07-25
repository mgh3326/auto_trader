# ROB-581 Crypto Discovery Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add read-only crypto candidate discovery primitives so agents can discover Upbit alt candidates without manually probing a hand-written symbol universe.

**Architecture:** Reuse the existing Upbit public-data stack instead of adding a new provider. Extend the Upbit altseason service to optionally return BTC-outperforming constituents, then expose a dedicated crypto top-movers MCP wrapper over the existing `get_top_stocks(market="crypto")` ranking path with a new `relative_strength` ranking type. Keep `screen_stocks_snapshot(market="crypto")` as the snapshot-backed crypto screener and document it instead of adding a duplicate alias unless the owner explicitly asks for one.

**Tech Stack:** Python 3.13, FastMCP, httpx, Upbit public REST, pytest-asyncio, Ruff, ty.

---

## Owner Decisions Before Execution

Recommended defaults are selected below so implementation can proceed unless the owner changes them.

1. Dedicated tool exposure: **recommended yes**.
   - Add `get_crypto_top_movers(ranking_type="relative_strength", limit=20)`.
   - Keep `get_top_stocks(market="crypto", ...)` backward-compatible.

2. 7d change fields: **recommended phased**.
   - This PR returns 24h change and 24h vs-BTC relative strength from the official Upbit ticker.
   - Do not fetch 7d candles for the entire KRW universe in this PR because Upbit daily candles are per-market and would add many network calls to a read tool.
   - Add a follow-up issue for optional capped `include_7d=true` candle enrichment if 7d is required on the first response.

3. Crypto screener alias: **recommended no**.
   - `screen_stocks_snapshot(preset="crypto_high_volume", market="crypto", ...)` already exists.
   - Update MCP docs with crypto examples instead of creating `screen_crypto` in this PR.

## File Structure

| File | Responsibility |
| --- | --- |
| `app/services/external/upbit_index.py` | Compute altseason ratio and optional 24h BTC-outperforming constituent rows from Upbit public ticker data. |
| `app/mcp_server/tooling/fundamentals/_upbit_index.py` | Validate MCP handler parameters and pass altseason constituent options to the service. |
| `app/mcp_server/tooling/fundamentals_handlers.py` | Update `get_upbit_altseason` registration signature and public description. |
| `app/mcp_server/tooling/analysis_rankings.py` | Add crypto `relative_strength` ranking support over existing Upbit ticker rows. |
| `app/mcp_server/tooling/analysis_screening.py` | Include relative-strength fields in mapped crypto ranking rows. |
| `app/mcp_server/tooling/analysis_tool_handlers.py` | Allow `("crypto", "relative_strength")` and add `get_crypto_top_movers_impl`. |
| `app/mcp_server/tooling/analysis_registration.py` | Register `get_crypto_top_movers`. |
| `app/mcp_server/README.md` | Document altseason constituents, crypto top movers, and existing crypto snapshot screener examples. |
| `tests/test_upbit_index_service.py` | Service and handler contract tests for constituents. |
| `tests/test_mcp_top_stocks.py` | Ranking and dedicated tool tests for relative strength. |
| `tests/test_mcp_profiles.py` | Guard that the crypto profile keeps the new read-only discovery tool. |

---

### Task 1: Altseason Constituents

**Files:**
- Modify: `app/services/external/upbit_index.py`
- Modify: `app/mcp_server/tooling/fundamentals/_upbit_index.py`
- Modify: `app/mcp_server/tooling/fundamentals_handlers.py`
- Test: `tests/test_upbit_index_service.py`

- [x] **Step 1: Write failing service test for constituents**

Add this test after `test_altseason_ratio_and_breadth` in `tests/test_upbit_index_service.py`:

```python
@pytest.mark.asyncio
async def test_altseason_constituents_list_btc_outperformers(monkeypatch):
    upbit_index._clear_caches()
    mapping = {
        **_datalab_mapping(),
        "/market/all": _MARKET_ALL,
        "/ticker": [
            {
                "market": "KRW-BTC",
                "trade_price": 100_000_000,
                "signed_change_rate": 0.01,
                "acc_trade_volume_24h": 100.0,
                "acc_trade_price_24h": 10_000_000_000.0,
            },
            {
                "market": "KRW-ETH",
                "trade_price": 5_000_000,
                "signed_change_rate": 0.05,
                "acc_trade_volume_24h": 200.0,
                "acc_trade_price_24h": 20_000_000_000.0,
            },
            {
                "market": "KRW-XRP",
                "trade_price": 900,
                "signed_change_rate": -0.02,
                "acc_trade_volume_24h": 300.0,
                "acc_trade_price_24h": 30_000_000_000.0,
            },
        ],
    }
    monkeypatch.setattr(httpx.AsyncClient, "get", _route(mapping))

    payload = await upbit_index.fetch_upbit_altseason(
        include_constituents=True,
        constituents_limit=10,
    )

    breadth = payload["breadth"]
    assert breadth["alts_total"] == 2
    assert breadth["alts_beating_btc"] == 1
    assert breadth["constituents_count"] == 1
    assert breadth["constituents"] == [
        {
            "rank": 1,
            "symbol": "KRW-ETH",
            "coin": "ETH",
            "price": 5_000_000,
            "change_rate_24h": 0.05,
            "change_pct_24h": 5.0,
            "btc_change_rate_24h": 0.01,
            "relative_strength_vs_btc_24h": 0.04,
            "relative_strength_pct_vs_btc_24h": 4.0,
            "volume_24h": 200.0,
            "trade_amount_24h": 20_000_000_000.0,
        }
    ]
```

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_upbit_index_service.py::test_altseason_constituents_list_btc_outperformers -q
```

Expected: FAIL because `fetch_upbit_altseason()` does not accept `include_constituents`.

- [x] **Step 3: Add constituent builder and service parameters**

In `app/services/external/upbit_index.py`, replace `_fetch_krw_breadth_24h` with this signature and body shape. Keep existing constants and cache behavior:

```python
def _to_float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coin_from_market(market: str) -> str:
    if market.startswith("KRW-"):
        return market.removeprefix("KRW-")
    return market


def _build_altseason_constituents(
    *,
    ticker_rows: list[dict[str, Any]],
    btc_rate: float,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ticker in ticker_rows:
        market = str(ticker.get("market") or "").strip().upper()
        if market == "KRW-BTC" or not market.startswith("KRW-"):
            continue
        rate = _to_float_or_none(ticker.get("signed_change_rate"))
        if rate is None or rate <= btc_rate:
            continue
        relative = rate - btc_rate
        rows.append(
            {
                "symbol": market,
                "coin": _coin_from_market(market),
                "price": _to_float_or_none(ticker.get("trade_price")),
                "change_rate_24h": rate,
                "change_pct_24h": round(rate * 100, 4),
                "btc_change_rate_24h": btc_rate,
                "relative_strength_vs_btc_24h": round(relative, 8),
                "relative_strength_pct_vs_btc_24h": round(relative * 100, 4),
                "volume_24h": _to_float_or_none(ticker.get("acc_trade_volume_24h")),
                "trade_amount_24h": _to_float_or_none(ticker.get("acc_trade_price_24h")),
            }
        )
    rows.sort(
        key=lambda row: (
            row["relative_strength_vs_btc_24h"],
            row.get("trade_amount_24h") or 0.0,
            row["symbol"],
        ),
        reverse=True,
    )
    return [{**row, "rank": idx} for idx, row in enumerate(rows[:limit], 1)]


async def _fetch_krw_breadth_24h(
    *,
    include_constituents: bool = False,
    constituents_limit: int = 50,
) -> dict[str, Any] | None:
    try:
        markets = await _get_json(MARKET_ALL_URL)
        krw = [
            m["market"]
            for m in markets
            if isinstance(m, dict) and str(m.get("market", "")).startswith("KRW-")
        ]
        if "KRW-BTC" not in krw:
            return None
        tickers = await _get_json(TICKER_URL, params={"markets": ",".join(krw)})
    except Exception as exc:
        logger.warning("Failed to fetch Upbit KRW breadth: %s", exc)
        return None

    ticker_rows = [t for t in tickers if isinstance(t, dict) and t.get("market")]
    rate_by_market = {
        str(t["market"]).upper(): _to_float_or_none(t.get("signed_change_rate"))
        for t in ticker_rows
    }
    btc_rate = rate_by_market.get("KRW-BTC")
    if btc_rate is None:
        return None

    alt_rates = [
        rate
        for market, rate in rate_by_market.items()
        if market != "KRW-BTC" and rate is not None
    ]
    if not alt_rates:
        return None

    beating = sum(1 for rate in alt_rates if rate > btc_rate)
    result: dict[str, Any] = {
        "window": "24h",
        "method": "open_api_ticker_24h_derived",
        "alts_total": len(alt_rates),
        "alts_beating_btc": beating,
        "alts_beating_btc_pct": round(beating / len(alt_rates), 4),
        "btc_change_24h": btc_rate,
    }
    if include_constituents:
        constituents = _build_altseason_constituents(
            ticker_rows=ticker_rows,
            btc_rate=btc_rate,
            limit=constituents_limit,
        )
        result["constituents"] = constituents
        result["constituents_count"] = len(constituents)
    return result
```

Then update `fetch_upbit_altseason`:

```python
async def fetch_upbit_altseason(
    *,
    include_constituents: bool = False,
    constituents_limit: int = 50,
) -> dict[str, Any] | None:
    ...
    breadth = await _fetch_krw_breadth_24h(
        include_constituents=include_constituents,
        constituents_limit=max(1, min(int(constituents_limit), 200)),
    )
```

Cache note: the existing `_altseason_cache` cannot ignore `include_constituents`. Use a cache key or cache only the richer payload safely:

```python
_altseason_cache: dict[tuple[bool, int], dict[str, Any]] = {}
_altseason_cache_expires: dict[tuple[bool, int], datetime] = {}
```

Update `_clear_caches()` accordingly. Return `payload.copy()` as before.

- [x] **Step 4: Run service tests**

Run:

```bash
uv run pytest tests/test_upbit_index_service.py -q
```

Expected: PASS.

- [x] **Step 5: Update handler and registration tests**

Add handler validation tests in `tests/test_upbit_index_service.py`:

```python
@pytest.mark.asyncio
async def test_handle_get_upbit_altseason_passes_constituent_options(monkeypatch):
    async def fake_fetch(*, include_constituents: bool, constituents_limit: int):
        return {
            "source": "upbit_datalab+upbit_open_api",
            "provenance": "test",
            "as_of": "2026-06-15T00:00:00+09:00",
            "ubai_ubmi_ratio": 0.5,
            "breadth": {
                "window": "24h",
                "constituents": [],
                "constituents_count": 0,
            },
            "options": {
                "include_constituents": include_constituents,
                "constituents_limit": constituents_limit,
            },
        }

    monkeypatch.setattr(upbit_index, "fetch_upbit_altseason", fake_fetch)

    result = await handle_get_upbit_altseason(
        include_constituents=True,
        constituents_limit=500,
    )

    assert result["options"] == {
        "include_constituents": True,
        "constituents_limit": 200,
    }
```

Expected implementation in `app/mcp_server/tooling/fundamentals/_upbit_index.py`:

```python
async def handle_get_upbit_altseason(
    include_constituents: bool = False,
    constituents_limit: int = 50,
) -> dict[str, Any]:
    limit = max(1, min(int(constituents_limit), 200))
    try:
        payload = await upbit_index.fetch_upbit_altseason(
            include_constituents=include_constituents,
            constituents_limit=limit,
        )
        ...
```

Expected registration change in `app/mcp_server/tooling/fundamentals_handlers.py`:

```python
async def get_upbit_altseason(
    include_constituents: bool = False,
    constituents_limit: int = 50,
) -> dict[str, Any]:
    return await handle_get_upbit_altseason(
        include_constituents=include_constituents,
        constituents_limit=constituents_limit,
    )
```

- [x] **Step 6: Run handler tests**

Run:

```bash
uv run pytest tests/test_upbit_index_service.py -q
```

Expected: PASS.

- [x] **Step 7: Commit**

```bash
git add app/services/external/upbit_index.py app/mcp_server/tooling/fundamentals/_upbit_index.py app/mcp_server/tooling/fundamentals_handlers.py tests/test_upbit_index_service.py
git commit -m "feat: expose upbit altseason constituents"
```

---

### Task 2: Crypto Relative-Strength Rankings

**Files:**
- Modify: `app/mcp_server/tooling/analysis_rankings.py`
- Modify: `app/mcp_server/tooling/analysis_screening.py`
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py`
- Test: `tests/test_mcp_top_stocks.py`

- [x] **Step 1: Write failing relative-strength ranking test**

Add this test near the existing crypto ranking tests in `tests/test_mcp_top_stocks.py`:

```python
async def test_crypto_rankings_relative_strength_sort_excludes_btc(self, monkeypatch):
    tools = build_tools()

    async def mock_fetch_top_traded_coins():
        return [
            {
                "market": "KRW-BTC",
                "trade_price": "100000000",
                "signed_change_rate": "0.03",
                "acc_trade_volume_24h": "100",
                "acc_trade_price_24h": "10000000000",
            },
            {
                "market": "KRW-ETH",
                "trade_price": "5000000",
                "signed_change_rate": "0.05",
                "acc_trade_volume_24h": "80",
                "acc_trade_price_24h": "20000000000",
            },
            {
                "market": "KRW-XRP",
                "trade_price": "900",
                "signed_change_rate": "0.04",
                "acc_trade_volume_24h": "200",
                "acc_trade_price_24h": "30000000000",
            },
        ]

    monkeypatch.setattr(
        upbit_service,
        "fetch_top_traded_coins",
        mock_fetch_top_traded_coins,
    )

    result = await tools["get_top_stocks"](
        market="crypto",
        ranking_type="relative_strength",
        limit=5,
    )

    assert result["ranking_type"] == "relative_strength"
    assert [row["symbol"] for row in result["rankings"]] == ["KRW-ETH", "KRW-XRP"]
    assert result["rankings"][0]["relative_strength_vs_btc_24h"] == pytest.approx(0.02)
    assert result["rankings"][0]["relative_strength_pct_vs_btc_24h"] == pytest.approx(2.0)
```

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_mcp_top_stocks.py::TestMCPTopStocks::test_crypto_rankings_relative_strength_sort_excludes_btc -q
```

Expected: FAIL with unsupported ranking type.

- [x] **Step 3: Implement ranking support**

In `app/mcp_server/tooling/analysis_rankings.py`, add helper functions above `get_crypto_rankings_impl`:

```python
def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _with_crypto_relative_strength(coins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    btc_rate = None
    for coin in coins:
        if str(coin.get("market") or "").upper() == "KRW-BTC":
            btc_rate = _as_float(coin.get("signed_change_rate"))
            break
    if btc_rate is None:
        return []

    rows: list[dict[str, Any]] = []
    for coin in coins:
        market = str(coin.get("market") or "").upper()
        if market == "KRW-BTC":
            continue
        rate = _as_float(coin.get("signed_change_rate"))
        if rate is None:
            continue
        relative = rate - btc_rate
        rows.append(
            {
                **coin,
                "relative_strength_vs_btc_24h": round(relative, 8),
                "relative_strength_pct_vs_btc_24h": round(relative * 100, 4),
                "btc_change_rate_24h": btc_rate,
            }
        )
    rows.sort(
        key=lambda row: (
            row["relative_strength_vs_btc_24h"],
            _as_float(row.get("acc_trade_price_24h")) or 0.0,
            str(row.get("market") or ""),
        ),
        reverse=True,
    )
    return rows
```

Then update `get_crypto_rankings_impl`:

```python
    elif ranking_type == "relative_strength":
        sorted_coins = _with_crypto_relative_strength(coins)
```

In `app/mcp_server/tooling/analysis_screening.py`, extend `_map_crypto_row`:

```python
    relative_strength = _to_optional_float(row.get("relative_strength_vs_btc_24h"))
    relative_strength_pct = _to_optional_float(
        row.get("relative_strength_pct_vs_btc_24h")
    )
    btc_change_rate = _to_optional_float(row.get("btc_change_rate_24h"))

    mapped = {
        "rank": rank,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_rate": round(change_rate, 2),
        "volume": volume,
        "market_cap": market_cap,
        "trade_amount": trade_amount,
    }
    if relative_strength is not None:
        mapped["relative_strength_vs_btc_24h"] = relative_strength
    if relative_strength_pct is not None:
        mapped["relative_strength_pct_vs_btc_24h"] = relative_strength_pct
    if btc_change_rate is not None:
        mapped["btc_change_rate_24h"] = btc_change_rate
    return mapped
```

In `app/mcp_server/tooling/analysis_tool_handlers.py`, add the supported combination:

```python
        ("crypto", "relative_strength"),
```

- [x] **Step 4: Run crypto ranking tests**

Run:

```bash
uv run pytest tests/test_mcp_top_stocks.py::TestMCPTopStocks::test_crypto_rankings_relative_strength_sort_excludes_btc tests/test_mcp_top_stocks.py::TestMCPTopStocks::test_crypto_rankings_gainers_sort tests/test_mcp_top_stocks.py::TestMCPTopStocks::test_crypto_rankings_losers_sort -q
```

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/analysis_rankings.py app/mcp_server/tooling/analysis_screening.py app/mcp_server/tooling/analysis_tool_handlers.py tests/test_mcp_top_stocks.py
git commit -m "feat: rank crypto by relative strength"
```

---

### Task 3: Dedicated `get_crypto_top_movers` Tool

**Files:**
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py`
- Modify: `app/mcp_server/tooling/analysis_registration.py`
- Modify: `tests/test_mcp_top_stocks.py`
- Modify: `tests/test_mcp_profiles.py`

- [x] **Step 1: Write failing tool registration test**

Add this test near the crypto ranking tests in `tests/test_mcp_top_stocks.py`:

```python
async def test_get_crypto_top_movers_defaults_to_relative_strength(self, monkeypatch):
    tools = build_tools()
    assert "get_crypto_top_movers" in tools

    async def mock_fetch_top_traded_coins():
        return [
            {
                "market": "KRW-BTC",
                "trade_price": "100000000",
                "signed_change_rate": "0.01",
                "acc_trade_volume_24h": "100",
                "acc_trade_price_24h": "10000000000",
            },
            {
                "market": "KRW-SOL",
                "trade_price": "220000",
                "signed_change_rate": "0.04",
                "acc_trade_volume_24h": "90",
                "acc_trade_price_24h": "9000000000",
            },
        ]

    monkeypatch.setattr(
        upbit_service,
        "fetch_top_traded_coins",
        mock_fetch_top_traded_coins,
    )

    result = await tools["get_crypto_top_movers"](limit=10)

    assert result["market"] == "crypto"
    assert result["ranking_type"] == "relative_strength"
    assert result["rankings"][0]["symbol"] == "KRW-SOL"
```

Add a profile smoke test in `tests/test_mcp_profiles.py`:

```python
    def test_keeps_crypto_discovery_tool(self) -> None:
        mcp = _build_mcp(McpProfile.CRYPTO)
        assert "get_crypto_top_movers" in mcp.tools
```

- [x] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_mcp_top_stocks.py::TestMCPTopStocks::test_get_crypto_top_movers_defaults_to_relative_strength tests/test_mcp_profiles.py::TestCryptoProfile::test_keeps_crypto_discovery_tool -q
```

Expected: FAIL because the tool is not registered.

- [x] **Step 3: Implement handler wrapper**

In `app/mcp_server/tooling/analysis_tool_handlers.py`, add:

```python
async def get_crypto_top_movers_impl(
    ranking_type: str = "relative_strength",
    limit: int = 20,
) -> dict[str, Any]:
    normalized = (ranking_type or "relative_strength").strip().lower()
    aliases = {
        "relative": "relative_strength",
        "relative_strength_vs_btc": "relative_strength",
        "rs": "relative_strength",
        "value": "volume",
        "trade_amount": "volume",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"relative_strength", "volume", "gainers", "losers"}:
        return analysis_screening._error_payload(
            source="validation",
            message=(
                "Unsupported crypto ranking_type: "
                f"{ranking_type}; allowed: relative_strength, volume, gainers, losers"
            ),
            query=f"ranking_type={ranking_type}",
        )
    return await get_top_stocks_impl(
        market="crypto",
        ranking_type=normalized,
        limit=limit,
    )
```

In `app/mcp_server/tooling/analysis_registration.py`, import and register it:

```python
from app.mcp_server.tooling.analysis_tool_handlers import (
    get_crypto_top_movers_impl,
    ...
)
```

Add this registration after `get_top_stocks`:

```python
    @mcp.tool(
        name="get_crypto_top_movers",
        description=(
            "Read-only Upbit KRW crypto candidate discovery. "
            "ranking_type supports relative_strength (default, vs BTC 24h), "
            "volume, gainers, and losers. Returns the same ranking row shape as "
            "get_top_stocks(market='crypto') with relative-strength fields when "
            "ranking_type='relative_strength'."
        ),
    )
    async def get_crypto_top_movers(
        ranking_type: str = "relative_strength",
        limit: int = 20,
    ) -> dict[str, Any]:
        return await get_crypto_top_movers_impl(
            ranking_type=ranking_type,
            limit=limit,
        )
```

- [x] **Step 4: Run tests**

Run:

```bash
uv run pytest tests/test_mcp_top_stocks.py::TestMCPTopStocks::test_get_crypto_top_movers_defaults_to_relative_strength tests/test_mcp_profiles.py::TestCryptoProfile::test_keeps_crypto_discovery_tool -q
```

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/analysis_tool_handlers.py app/mcp_server/tooling/analysis_registration.py tests/test_mcp_top_stocks.py tests/test_mcp_profiles.py
git commit -m "feat: add crypto top movers mcp tool"
```

---

### Task 4: MCP Docs and Crypto Snapshot Examples

**Files:**
- Modify: `app/mcp_server/README.md`

- [x] **Step 1: Update tool list**

In `app/mcp_server/README.md`, update the tool list around `screen_stocks_snapshot` and `get_top_stocks` with:

```markdown
- `get_top_stocks(market="kr", ranking_type="volume", limit=20)` - Cross-market rankings. Crypto supports `volume`, `gainers`, `losers`, and `relative_strength`.
- `get_crypto_top_movers(ranking_type="relative_strength", limit=20)` - Crypto-only Upbit KRW discovery wrapper. Default ranking sorts non-BTC coins by 24h outperformance vs KRW-BTC.
- `get_upbit_altseason(include_constituents=false, constituents_limit=50)` - Upbit altseason ratio and 24h breadth. With constituents enabled, `breadth.constituents` lists KRW alts beating BTC with 24h change, vs-BTC relative strength, volume, and traded value.
```

Under the existing `screen_stocks_snapshot` entry, add:

```markdown
  - Crypto snapshot examples:
    - `screen_stocks_snapshot(preset="crypto_high_volume", market="crypto", limit=40)`
    - `screen_stocks_snapshot(preset="crypto_momentum", market="crypto", filters=[{"field":"trade_amount_24h","operator":"gte","value":10000000000}], limit=40)`
  - Use `get_crypto_top_movers` for live Upbit top movers; use `screen_stocks_snapshot(..., market="crypto")` for persisted snapshot-backed filtering.
```

- [x] **Step 2: Run README search sanity check**

Run:

```bash
rg -n "get_crypto_top_movers|get_upbit_altseason\\(|relative_strength|crypto_high_volume" app/mcp_server/README.md
```

Expected: all new terms appear, and the existing `screen_stocks_snapshot` docs remain present.

- [x] **Step 3: Commit**

```bash
git add app/mcp_server/README.md
git commit -m "docs: document crypto discovery tools"
```

---

### Task 5: Verification

**Files:**
- Verify only.

- [x] **Step 1: Run targeted test suite**

Run:

```bash
uv run pytest tests/test_upbit_index_service.py tests/test_mcp_top_stocks.py tests/test_mcp_profiles.py -q
```

Expected: PASS.

- [x] **Step 2: Run focused lint**

Run:

```bash
uv run ruff check app/services/external/upbit_index.py app/mcp_server/tooling/fundamentals/_upbit_index.py app/mcp_server/tooling/fundamentals_handlers.py app/mcp_server/tooling/analysis_rankings.py app/mcp_server/tooling/analysis_screening.py app/mcp_server/tooling/analysis_tool_handlers.py app/mcp_server/tooling/analysis_registration.py tests/test_upbit_index_service.py tests/test_mcp_top_stocks.py tests/test_mcp_profiles.py
```

Expected: PASS.

- [x] **Step 3: Run type check if local ty is configured**

Run:

```bash
uv run ty check app/services/external/upbit_index.py app/mcp_server/tooling/fundamentals/_upbit_index.py app/mcp_server/tooling/analysis_rankings.py app/mcp_server/tooling/analysis_tool_handlers.py app/mcp_server/tooling/analysis_registration.py
```

Expected: PASS. If `ty` does not support file-scoped invocation in this repo, run `make lint` and record the exact command/result in the handoff.

- [x] **Step 4: Manual MCP smoke**

Run:

```bash
uv run python - <<'PY'
import asyncio
from app.mcp_server.tooling.fundamentals._upbit_index import handle_get_upbit_altseason

async def main():
    payload = await handle_get_upbit_altseason(include_constituents=True, constituents_limit=5)
    print(payload.get("source"))
    print((payload.get("breadth") or {}).get("constituents_count"))

asyncio.run(main())
PY
```

Expected: no exception. If live Upbit is unavailable, the command may print an error payload; that is acceptable because the tool is fail-open.

- [x] **Step 5: Final review**

Confirm:

```bash
git status --short
```

Expected: clean except for intentional uncommitted changes if the user requested no commits.

Review checklist:
- Existing `get_top_stocks(market="crypto", ranking_type="volume|gainers|losers")` behavior is unchanged.
- `get_upbit_altseason()` without arguments preserves the old compact payload.
- New constituent fields are opt-in.
- `relative_strength` excludes BTC because BTC is the benchmark.
- No order, broker account, or mutation paths are touched.

---

## Scope Notes

- This is routine read-only MCP feature work, so it fits `keep_on_gpt54`.
- It is not a `high_risk_change`: no auth, permissions, DB migration, order execution, live approval, strategy policy, or deployment boundary changes.
- The only deferred part is 7d relative strength. Implementing it correctly likely needs a rate-limited daily-candle enrichment design and should be a separate ROB follow-up unless the owner prioritizes it now.
