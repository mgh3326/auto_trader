# ROB-377 PR1 — Crypto Market Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `get_market_index` return crypto market-regime data (total market cap + BTC dominance) via CoinGecko `/global`, and make the Hermes-bundle `market` dimension produce real data for crypto instead of failing closed.

**Architecture:** The `get_market_index` handler dispatches on `meta["source"]` (`naver`/`yfinance`). Add a third `coingecko` branch returning the same row shape (`{symbol, name, current, change, change_pct, source}`). Reuse the cached CoinGecko `/global` fetcher (`btc_dominance.fetch_btc_dominance`, extended additively). Fill the crypto cell of the two per-market index symbol tables so the existing collector/stage logic picks up `CRYPTO` with zero plumbing changes.

**Tech Stack:** Python 3.13, async httpx, pytest (`pytest.mark.asyncio`/`unit`), uv. No migration, no new dependency.

**Spec:** `docs/superpowers/specs/2026-05-30-rob-377-pr1-crypto-market-index-design.md`

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `app/services/external/btc_dominance.py` | CoinGecko `/global` fetch + 30m cache | Additive: also return `total_market_cap_usd`, `eth_dominance` |
| `app/mcp_server/tooling/fundamentals_sources_indices.py` | Index metadata + per-source fetchers | Add `CRYPTO`/`BTC.D` to `_INDEX_META`; add `_fetch_index_crypto_current` |
| `app/mcp_server/tooling/fundamentals/_market_index.py` | `get_market_index` handler | Add `coingecko` dispatch branch |
| `app/mcp_server/tooling/fundamentals_handlers.py` | MCP tool registration/description | Update `get_market_index` description to mention crypto |
| `app/services/action_report/snapshot_backed/collectors/market.py` | Per-market index symbol set (collector) | `_MARKET_TO_INDEX_SYMBOLS["crypto"] = ["CRYPTO"]` |
| `app/services/investment_stages/stages/market.py` | Per-market primary index (stage) | `_PRIMARY_INDEX_BY_MARKET["crypto"] = ("CRYPTO",)` |
| `tests/test_btc_dominance.py` | NEW — `fetch_btc_dominance` additive fields | Create |
| `tests/test_mcp_fundamentals_tools.py` | Handler + `_INDEX_META` tests | Extend |
| `tests/services/action_report/snapshot_backed/test_collectors.py` | Collector crypto indices | Extend |
| `tests/services/investment_stages/stages/test_market.py` | MarketStage crypto verdicts | Extend |

---

## Task 1: Extend `fetch_btc_dominance` with total market cap + ETH dominance (additive)

**Files:**
- Modify: `app/services/external/btc_dominance.py`
- Test: `tests/test_btc_dominance.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_btc_dominance.py`:

```python
"""ROB-377 PR1: fetch_btc_dominance additive fields (total mcap + ETH dominance)."""

from __future__ import annotations

import httpx
import pytest

from app.services.external import btc_dominance


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


def _patch_global(monkeypatch, payload):
    async def fake_get(self_cli, url, *args, **kwargs):
        return _FakeResponse(payload)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


@pytest.mark.asyncio
async def test_fetch_btc_dominance_includes_total_mcap_and_eth(monkeypatch):
    btc_dominance._clear_btc_dominance_cache()
    _patch_global(
        monkeypatch,
        {
            "data": {
                "market_cap_percentage": {"btc": 52.345, "eth": 17.21},
                "market_cap_change_percentage_24h_usd": 1.853,
                "total_market_cap": {"usd": 2_310_000_000_000.0},
            }
        },
    )

    result = await btc_dominance.fetch_btc_dominance()

    assert result is not None
    # existing keys unchanged
    assert result["btc_dominance"] == pytest.approx(52.35)
    assert result["total_market_cap_change_24h"] == pytest.approx(1.85)
    # new additive keys
    assert result["total_market_cap_usd"] == pytest.approx(2_310_000_000_000.0)
    assert result["eth_dominance"] == pytest.approx(17.21)


@pytest.mark.asyncio
async def test_fetch_btc_dominance_new_fields_none_when_absent(monkeypatch):
    btc_dominance._clear_btc_dominance_cache()
    _patch_global(
        monkeypatch,
        {
            "data": {
                "market_cap_percentage": {"btc": 50.0},
                "market_cap_change_percentage_24h_usd": -0.5,
            }
        },
    )

    result = await btc_dominance.fetch_btc_dominance()

    assert result is not None
    assert result["btc_dominance"] == pytest.approx(50.0)
    assert result["total_market_cap_usd"] is None
    assert result["eth_dominance"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_btc_dominance.py -v`
Expected: FAIL with `KeyError: 'total_market_cap_usd'`.

- [ ] **Step 3: Write minimal implementation**

In `app/services/external/btc_dominance.py`, inside the parse `try` block, after `market_cap_change = market_data.get("market_cap_change_percentage_24h_usd",)`, extend the `result` dict additively. Replace the existing `result = {...}` assignment with:

```python
        total_market_cap = (market_data.get("total_market_cap") or {}).get("usd")
        eth_dominance = market_cap_pct.get("eth")

        result = {
            "btc_dominance": round(float(btc_dominance), 2),
            "total_market_cap_change_24h": (
                round(float(market_cap_change), 2)
                if market_cap_change is not None
                else None
            ),
            "total_market_cap_usd": (
                float(total_market_cap) if total_market_cap is not None else None
            ),
            "eth_dominance": (
                round(float(eth_dominance), 2) if eth_dominance is not None else None
            ),
        }
```

Also update the docstring's return-shape block to list the two new keys.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_btc_dominance.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run regression on btc_dominance consumers**

Run: `uv run pytest tests/test_market_report.py tests/test_crypto_insight_external_adapters.py -q`
Expected: PASS (additive change must not break existing consumers).

- [ ] **Step 6: Commit**

```bash
git add app/services/external/btc_dominance.py tests/test_btc_dominance.py
git commit -m "feat(ROB-377): fetch_btc_dominance returns total mcap + ETH dominance (additive)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Add crypto symbols to `_INDEX_META` + crypto index fetcher

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals_sources_indices.py`
- Test: `tests/test_mcp_fundamentals_tools.py` (extend `TestIndexMeta` + add a fetcher test)

- [ ] **Step 1: Write the failing test**

In `tests/test_mcp_fundamentals_tools.py`, append to the `TestIndexMeta` class (after `test_aliases`):

```python
    def test_crypto_indices_have_coingecko_source(self):
        for sym in ("CRYPTO", "BTC.D"):
            meta = fundamentals_sources_indices._INDEX_META[sym]
            assert meta["source"] == "coingecko"
            assert "cg_metric" in meta

    def test_crypto_not_in_default_indices(self):
        # Crypto is fetched explicitly, never in the no-arg equity default list.
        for sym in ("CRYPTO", "BTC.D"):
            assert sym not in fundamentals_sources_indices._DEFAULT_INDICES
```

Then add a new test class at the end of the file for the fetcher:

```python
@pytest.mark.asyncio
class TestFetchIndexCryptoCurrent:
    """ROB-377 PR1: crypto market-regime index rows from CoinGecko /global."""

    async def _patch_global(self, monkeypatch, data):
        async def fake_fetch():
            return data

        monkeypatch.setattr(
            fundamentals_sources_indices, "fetch_btc_dominance", fake_fetch
        )

    async def test_crypto_total_market_cap_row(self, monkeypatch):
        await self._patch_global(
            monkeypatch,
            {
                "btc_dominance": 52.3,
                "total_market_cap_change_24h": 1.85,
                "total_market_cap_usd": 2.31e12,
                "eth_dominance": 17.2,
            },
        )
        row = await fundamentals_sources_indices._fetch_index_crypto_current(
            "total_market_cap", "암호화폐 총 시가총액", "CRYPTO"
        )
        assert row["symbol"] == "CRYPTO"
        assert row["current"] == pytest.approx(2.31e12)
        assert row["change_pct"] == pytest.approx(1.85)
        assert row["source"] == "coingecko"

    async def test_btc_dominance_row_has_no_change_pct(self, monkeypatch):
        await self._patch_global(
            monkeypatch,
            {
                "btc_dominance": 52.3,
                "total_market_cap_change_24h": 1.85,
                "total_market_cap_usd": 2.31e12,
                "eth_dominance": 17.2,
            },
        )
        row = await fundamentals_sources_indices._fetch_index_crypto_current(
            "btc_dominance", "BTC 도미넌스", "BTC.D"
        )
        assert row["symbol"] == "BTC.D"
        assert row["current"] == pytest.approx(52.3)
        assert row["change_pct"] is None

    async def test_raises_when_global_unavailable(self, monkeypatch):
        async def fake_fetch():
            return None

        monkeypatch.setattr(
            fundamentals_sources_indices, "fetch_btc_dominance", fake_fetch
        )
        with pytest.raises(Exception):
            await fundamentals_sources_indices._fetch_index_crypto_current(
                "total_market_cap", "암호화폐 총 시가총액", "CRYPTO"
            )
```

> Note: `fundamentals_sources_indices` is already imported in this test module (used by `TestIndexMeta`). `pytest` and `pytest.approx` are already imported.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest "tests/test_mcp_fundamentals_tools.py::TestIndexMeta::test_crypto_indices_have_coingecko_source" "tests/test_mcp_fundamentals_tools.py::TestFetchIndexCryptoCurrent" -v`
Expected: FAIL (`KeyError: 'CRYPTO'` and `AttributeError: ... has no attribute '_fetch_index_crypto_current'`).

- [ ] **Step 3: Write minimal implementation**

In `app/mcp_server/tooling/fundamentals_sources_indices.py`:

(a) Add the import near the top (after the existing imports):

```python
from app.services.external.btc_dominance import fetch_btc_dominance
```

(b) Add two entries to `_INDEX_META` (after the `VIX` line):

```python
    "CRYPTO": {
        "name": "암호화폐 총 시가총액",
        "source": "coingecko",
        "cg_metric": "total_market_cap",
    },
    "BTC.D": {
        "name": "BTC 도미넌스",
        "source": "coingecko",
        "cg_metric": "btc_dominance",
    },
```

(c) Add the fetcher function (after `_fetch_index_us_history`, before `__all__`):

```python
async def _fetch_index_crypto_current(
    cg_metric: str, name: str, symbol: str
) -> dict[str, Any]:
    """Crypto market-regime "index" row from CoinGecko /global (cached).

    Row shape matches the KR/US index rows so the snapshot collector and
    MarketStage consume it unchanged. ``total_market_cap`` carries a usable
    24h change_pct (the regime driver); ``btc_dominance`` reports the dominance
    level only (CoinGecko /global has no dominance 24h change) → change_pct is
    None, which the collector intentionally drops and MarketStage skips rather
    than fabricating a flat 0.0%. Raises on an unreachable /global so the
    handler maps it to an error payload (never fabricate values).
    """
    data = await fetch_btc_dominance()
    if not data:
        raise RuntimeError("CoinGecko /global unavailable")

    if cg_metric == "total_market_cap":
        current = data.get("total_market_cap_usd")
        change_pct = data.get("total_market_cap_change_24h")
    elif cg_metric == "btc_dominance":
        current = data.get("btc_dominance")
        change_pct = None
    else:
        raise ValueError(f"unknown cg_metric '{cg_metric}'")

    return {
        "symbol": symbol,
        "name": name,
        "current": current,
        "change": None,
        "change_pct": change_pct,
        "source": "coingecko",
    }
```

(d) Add `"_fetch_index_crypto_current"` to the `__all__` list.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest "tests/test_mcp_fundamentals_tools.py::TestIndexMeta" "tests/test_mcp_fundamentals_tools.py::TestFetchIndexCryptoCurrent" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/fundamentals_sources_indices.py tests/test_mcp_fundamentals_tools.py
git commit -m "feat(ROB-377): add CRYPTO/BTC.D index meta + CoinGecko /global fetcher

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Add `coingecko` branch to `handle_get_market_index` + update tool description

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals/_market_index.py`
- Modify: `app/mcp_server/tooling/fundamentals_handlers.py:226-229` (description)
- Test: `tests/test_mcp_fundamentals_tools.py` (extend `TestGetMarketIndex`)

- [ ] **Step 1: Write the failing test**

In `tests/test_mcp_fundamentals_tools.py`, add to the `TestGetMarketIndex` class:

```python
    def _patch_global(self, monkeypatch, data):
        from app.mcp_server.tooling import fundamentals_sources_indices

        async def fake_fetch():
            return data

        monkeypatch.setattr(
            fundamentals_sources_indices, "fetch_btc_dominance", fake_fetch
        )

    async def test_crypto_total_market_cap(self, monkeypatch):
        tools = build_tools()
        self._patch_global(
            monkeypatch,
            {
                "btc_dominance": 52.3,
                "total_market_cap_change_24h": 1.85,
                "total_market_cap_usd": 2.31e12,
                "eth_dominance": 17.2,
            },
        )

        result = await tools["get_market_index"](symbol="CRYPTO")

        assert len(result["indices"]) == 1
        idx = result["indices"][0]
        assert idx["symbol"] == "CRYPTO"
        assert idx["current"] == pytest.approx(2.31e12)
        assert idx["change_pct"] == pytest.approx(1.85)
        assert idx["source"] == "coingecko"
        assert result["history"] == []

    async def test_crypto_btc_dominance(self, monkeypatch):
        tools = build_tools()
        self._patch_global(
            monkeypatch,
            {
                "btc_dominance": 52.3,
                "total_market_cap_change_24h": 1.85,
                "total_market_cap_usd": 2.31e12,
                "eth_dominance": 17.2,
            },
        )

        result = await tools["get_market_index"](symbol="BTC.D")

        idx = result["indices"][0]
        assert idx["symbol"] == "BTC.D"
        assert idx["current"] == pytest.approx(52.3)
        assert idx["change_pct"] is None

    async def test_crypto_global_failure_returns_error_payload(self, monkeypatch):
        tools = build_tools()
        self._patch_global(monkeypatch, None)

        result = await tools["get_market_index"](symbol="CRYPTO")

        # fail-open: error payload, never fabricated values
        assert "indices" not in result or not result.get("indices")
        assert result.get("error") or result.get("source") == "coingecko"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest "tests/test_mcp_fundamentals_tools.py::TestGetMarketIndex::test_crypto_total_market_cap" "tests/test_mcp_fundamentals_tools.py::TestGetMarketIndex::test_crypto_btc_dominance" "tests/test_mcp_fundamentals_tools.py::TestGetMarketIndex::test_crypto_global_failure_returns_error_payload" -v`
Expected: FAIL (currently `ValueError: Unknown index symbol 'CRYPTO'` → not the awaited row shape).

- [ ] **Step 3: Write minimal implementation**

(a) In `app/mcp_server/tooling/fundamentals/_market_index.py`, import the new fetcher (add to the existing `from app.mcp_server.tooling.fundamentals_sources_indices import (...)` block):

```python
    _fetch_index_crypto_current,
```

(b) In the `if symbol:` block, replace the `if meta["source"] == "naver": ... else: ...` dispatch with a three-way dispatch:

```python
        try:
            if meta["source"] == "naver":
                current_data, history = await asyncio.gather(
                    _fetch_index_kr_current(meta["naver_code"], meta["name"]),
                    _fetch_index_kr_history(meta["naver_code"], capped_count, period),
                )
                return {"indices": [current_data], "history": history}
            if meta["source"] == "coingecko":
                current_data = await _fetch_index_crypto_current(
                    meta["cg_metric"], meta["name"], sym
                )
                return {"indices": [current_data], "history": []}
            current_data, history = await asyncio.gather(
                _fetch_index_us_current(meta["yf_ticker"], meta["name"], sym),
                _fetch_index_us_history(meta["yf_ticker"], capped_count, period),
            )
            return {"indices": [current_data], "history": history}
        except Exception as exc:
            return _error_payload(source=meta["source"], message=str(exc), symbol=sym)
```

(c) In `app/mcp_server/tooling/fundamentals_handlers.py`, update the `get_market_index` description (lines 226-229) to:

```python
        description=(
            "Get market index data. Supports KOSPI/KOSDAQ, major US indices "
            "(SPX/NASDAQ/DJI/VIX), and crypto market regime "
            "(CRYPTO=total market cap, BTC.D=BTC dominance via CoinGecko). "
            "Without symbol returns current major equity indices, with symbol "
            "adds OHLCV history (crypto has no history)."
        ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest "tests/test_mcp_fundamentals_tools.py::TestGetMarketIndex" -v`
Expected: PASS (existing KR/US tests + new crypto tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/fundamentals/_market_index.py app/mcp_server/tooling/fundamentals_handlers.py tests/test_mcp_fundamentals_tools.py
git commit -m "feat(ROB-377): get_market_index serves crypto regime via coingecko branch

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Fill crypto cell in collector + stage symbol tables (Hermes dimension survives)

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/market.py:44-48`
- Modify: `app/services/investment_stages/stages/market.py:21-25`
- Test: `tests/services/action_report/snapshot_backed/test_collectors.py` (extend)
- Test: `tests/services/investment_stages/stages/test_market.py` (extend)

- [ ] **Step 1: Write the failing tests**

(a) In `tests/services/action_report/snapshot_backed/test_collectors.py`, after `test_market_collector_us_populates_indices_dict`:

```python
@pytest.mark.asyncio
async def test_market_collector_crypto_populates_indices_dict():
    # ROB-377 PR1: crypto market dimension gets a CRYPTO (total mcap) index so
    # MarketStage no longer fails closed for crypto.
    captured: dict = {}

    async def fake_index_fn(symbols):
        captured["symbols"] = list(symbols)
        return [
            {
                "symbol": "CRYPTO",
                "name": "암호화폐 총 시가총액",
                "current": 2.31e12,
                "change_pct": 1.85,
            }
        ]

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="crypto"))
    payload = results[0].payload_json
    assert payload["indices"]["CRYPTO"]["change_percent"] == 1.85
    assert "CRYPTO" in captured["symbols"]
```

(b) In `tests/services/investment_stages/stages/test_market.py`, append:

```python
@pytest.mark.asyncio
async def test_market_stage_crypto_selects_crypto_bull():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [_snapshot({"indices": {"CRYPTO": {"change_percent": 2.0}}})]
        },
        bundle_metadata={},
        market="crypto",
    )
    payload = await MarketStage().run(ctx)
    assert payload.verdict == StageVerdict.BULL
    assert payload.cited_snapshots[0].payload_path == "$.indices.CRYPTO.change_percent"


@pytest.mark.asyncio
async def test_market_stage_crypto_selects_crypto_bear():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "market": [_snapshot({"indices": {"CRYPTO": {"change_percent": -2.0}}})]
        },
        bundle_metadata={},
        market="crypto",
    )
    payload = await MarketStage().run(ctx)
    assert payload.verdict == StageVerdict.BEAR


@pytest.mark.asyncio
async def test_market_stage_crypto_unavailable_when_no_index():
    # No CRYPTO index entry → still fail-closed (e.g. CoinGecko /global down).
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={"market": [_snapshot({"indices": {}})]},
        bundle_metadata={},
        market="crypto",
    )
    with pytest.raises(UnavailableStageError):
        await MarketStage().run(ctx)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest "tests/services/action_report/snapshot_backed/test_collectors.py::test_market_collector_crypto_populates_indices_dict" "tests/services/investment_stages/stages/test_market.py::test_market_stage_crypto_selects_crypto_bull" "tests/services/investment_stages/stages/test_market.py::test_market_stage_crypto_selects_crypto_bear" -v`
Expected: FAIL — collector requests no symbols for crypto (`_MARKET_TO_INDEX_SYMBOLS["crypto"]=[]` → `indices` empty); stage selects nothing for crypto (`_PRIMARY_INDEX_BY_MARKET["crypto"]=()` → `UnavailableStageError`).

- [ ] **Step 3: Write minimal implementation**

(a) In `app/services/action_report/snapshot_backed/collectors/market.py`, change the crypto cell:

```python
_MARKET_TO_INDEX_SYMBOLS: dict[str, list[str]] = {
    "kr": ["KOSPI", "KOSDAQ"],
    "us": ["SPX", "NASDAQ", "DJI"],
    "crypto": ["CRYPTO"],
}
```

Update the trailing comment on that block from "Crypto has no index dimension." to note crypto now uses the CoinGecko total-market-cap regime index (ROB-377).

(b) In `app/services/investment_stages/stages/market.py`, change the crypto cell:

```python
_PRIMARY_INDEX_BY_MARKET: dict[str, tuple[str, ...]] = {
    "kr": ("KOSPI",),
    "us": ("SPX", "NASDAQ", "DJI"),
    "crypto": ("CRYPTO",),
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest "tests/services/action_report/snapshot_backed/test_collectors.py::test_market_collector_crypto_populates_indices_dict" "tests/services/investment_stages/stages/test_market.py" -v`
Expected: PASS (new crypto tests + all existing KR/US stage tests still pass).

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/snapshot_backed/collectors/market.py app/services/investment_stages/stages/market.py tests/services/action_report/snapshot_backed/test_collectors.py tests/services/investment_stages/stages/test_market.py
git commit -m "feat(ROB-377): crypto market dimension uses CRYPTO total-mcap index

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Full-suite lint + type + targeted test gate

**Files:** none (verification only)

- [ ] **Step 1: Lint**

Run: `uv run ruff check app/ tests/`
Expected: no errors.

- [ ] **Step 2: Format check**

Run: `uv run ruff format --check app/ tests/`
Expected: no files would be reformatted. (If it lists files, run `uv run ruff format app/ tests/`, re-run the check, and amend the relevant commit.)

- [ ] **Step 3: Type check**

Run: `uv run ty check app/mcp_server/tooling/fundamentals_sources_indices.py app/mcp_server/tooling/fundamentals/_market_index.py app/services/external/btc_dominance.py app/services/action_report/snapshot_backed/collectors/market.py app/services/investment_stages/stages/market.py`
Expected: no new type errors.

- [ ] **Step 4: Targeted test run**

Run: `uv run pytest tests/test_btc_dominance.py "tests/test_mcp_fundamentals_tools.py::TestGetMarketIndex" "tests/test_mcp_fundamentals_tools.py::TestIndexMeta" "tests/test_mcp_fundamentals_tools.py::TestFetchIndexCryptoCurrent" tests/services/action_report/snapshot_backed/test_collectors.py tests/services/investment_stages/stages/test_market.py -q`
Expected: all PASS.

- [ ] **Step 5: Import-guard / broader regression (best-effort)**

Run: `uv run pytest tests/test_mcp_fundamentals_tools.py -q`
Expected: PASS (full fundamentals tool module unaffected).

> Note: the full `pytest -n auto` suite runs against a shared Postgres in CI and unrelated DB-integration tests can flake across xdist workers (a re-run clears them — not a regression). Treat only failures in the modules touched by this PR as blocking locally; rely on the CI Test workflow for the green-main gate before merge.

---

## Self-Review (completed during authoring)

- **Spec coverage:** AC ① (`get_market_index` returns crypto data) → Tasks 2+3. AC ② (Hermes market dimension real data for crypto) → Task 4. CoinGecko reuse → Task 1. Error/fail-open → Task 3 step 1 (failure test) + Task 4 unavailable test. Tool description → Task 3. ✓
- **Placeholder scan:** every code step has complete code; no TBD/TODO. ✓
- **Type consistency:** `_fetch_index_crypto_current(cg_metric, name, symbol)` signature identical across Tasks 2 and 3; return dict keys (`symbol/name/current/change/change_pct/source`) match the KR/US row shape the collector reads (`symbol` + `change_pct`). `fetch_btc_dominance` keys (`total_market_cap_usd`, `total_market_cap_change_24h`, `btc_dominance`, `eth_dominance`) consistent between Task 1 (producer) and Task 2 (consumer). ✓
- **Out of scope (documented):** OI/LSR (PR2), Upbit/altseason (PR3), on-chain/ETF/liquidations (parked); surfacing dominance *level* into the bundle (deferred follow-up). ✓
