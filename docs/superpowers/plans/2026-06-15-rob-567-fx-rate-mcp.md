# ROB-567 FX Rate MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `get_fx_rate(pair="USDKRW")` MCP tool that exposes the existing USD/KRW quote service without routing FX through `get_market_index`.

**Architecture:** Keep FX as a focused fundamentals read tool. A new handler module normalizes supported pair aliases, calls `app.services.exchange_rate_service.get_usd_krw_rate_details()`, and returns a JSON-safe spot-rate payload. Existing fundamentals registration exposes the tool on all MCP profiles; docs and tests make clear that trends, bank quotes, preferential rates, exchange execution, and US order total-cost integration are not part of ROB-567 P1.

**Tech Stack:** Python 3.13, FastMCP, pytest, Ruff, ty, existing `exchange_rate_service`.

---

## File Structure

| Path | Responsibility |
|---|---|
| `app/mcp_server/tooling/fundamentals/_fx_rates.py` | New pure handler for `get_fx_rate`, pair normalization, and JSON-safe quote shaping |
| `app/mcp_server/tooling/fundamentals_handlers.py` | Import/register `get_fx_rate` and include it in `FUNDAMENTALS_TOOL_NAMES` |
| `app/mcp_server/__init__.py` | Add `get_fx_rate` to the advertised tool-name list |
| `app/mcp_server/README.md` | Document the new tool and explicitly scope it to USD/KRW spot lookup |
| `tests/test_mcp_fundamentals_tools.py` | Handler, registration, alias, unsupported-pair, and FX-vs-index contract tests |
| `tests/test_mcp_tool_registration_boot.py` | Existing real FastMCP duplicate-name smoke test; rerun after registration |

## Scope Decisions

- Ship P1 only: `get_fx_rate(USDKRW)` read-only wrapper.
- Keep `USDKRW` out of `_INDEX_META`; `get_market_index` must continue to reject it.
- Defer trend, bank-specific, and preferential-rate modeling to P2/P3.
- Defer US order total-cost integration to ROB-565.
- No migration, scheduler, order mutation, or exchange execution code.

---

### Task 1: Add the FX Handler

**Files:**
- Create: `app/mcp_server/tooling/fundamentals/_fx_rates.py`
- Modify: `tests/test_mcp_fundamentals_tools.py`
- Test: `tests/test_mcp_fundamentals_tools.py`

- [x] **Step 1: Write failing handler tests**

In `tests/test_mcp_fundamentals_tools.py`, add this import near the other standard-library imports:

```python
from datetime import UTC, datetime
```

Add these imports near the other app imports:

```python
from app.mcp_server.tooling.fundamentals import _fx_rates as fundamentals_fx_rates
from app.services.exchange_rate_service import UsdKrwExchangeRateQuote
```

Add this test class before the `# get_market_index Tool` section:

```python
# ---------------------------------------------------------------------------
# get_fx_rate Tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetFxRateHandler:
    """Tests for the pure get_fx_rate handler."""

    async def test_get_fx_rate_returns_usdkrw_details(self, monkeypatch):
        async def fake_details() -> UsdKrwExchangeRateQuote:
            return UsdKrwExchangeRateQuote(
                rate=1505.7,
                mid_rate=1505.4,
                source="toss",
                valid_from=datetime(2026, 6, 15, 0, 0, tzinfo=UTC),
                valid_until=datetime(2026, 6, 15, 0, 1, tzinfo=UTC),
                basis_point=-12.5,
                rate_change_type="DOWN",
            )

        monkeypatch.setattr(
            fundamentals_fx_rates,
            "get_usd_krw_rate_details",
            fake_details,
        )

        result = await fundamentals_fx_rates.handle_get_fx_rate()

        assert result == {
            "pair": "USDKRW",
            "base_currency": "USD",
            "quote_currency": "KRW",
            "rate": 1505.7,
            "mid_rate": 1505.4,
            "default_rate": 1505.4,
            "source": "toss",
            "valid_from": "2026-06-15T00:00:00+00:00",
            "valid_until": "2026-06-15T00:01:00+00:00",
            "basis_point": -12.5,
            "rate_change_type": "DOWN",
        }

    async def test_get_fx_rate_normalizes_pair_aliases(self, monkeypatch):
        async def fake_details() -> UsdKrwExchangeRateQuote:
            return UsdKrwExchangeRateQuote(
                rate=1498.2,
                mid_rate=1498.2,
                source="open_er_api",
            )

        monkeypatch.setattr(
            fundamentals_fx_rates,
            "get_usd_krw_rate_details",
            fake_details,
        )

        for pair in ("USDKRW", "usdkrw", "USD/KRW", "USD_KRW", "USD-KRW"):
            result = await fundamentals_fx_rates.handle_get_fx_rate(pair=pair)
            assert result["pair"] == "USDKRW"
            assert result["base_currency"] == "USD"
            assert result["quote_currency"] == "KRW"
            assert result["default_rate"] == pytest.approx(1498.2)
            assert result["source"] == "open_er_api"
            assert result["valid_from"] is None
            assert result["valid_until"] is None
            assert result["basis_point"] is None
            assert result["rate_change_type"] is None

    async def test_get_fx_rate_rejects_unsupported_pair(self):
        with pytest.raises(ValueError, match="Unsupported FX pair 'EURKRW'"):
            await fundamentals_fx_rates.handle_get_fx_rate(pair="EURKRW")
```

- [x] **Step 2: Run tests to verify they fail for the missing module**

Run:

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py::TestGetFxRateHandler -q
```

Expected: collection fails with `ImportError` or `ModuleNotFoundError` because `app.mcp_server.tooling.fundamentals._fx_rates` does not exist yet.

- [x] **Step 3: Create the handler module**

Create `app/mcp_server/tooling/fundamentals/_fx_rates.py`:

```python
"""Handler for get_fx_rate tool."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.exchange_rate_service import get_usd_krw_rate_details


def _isoformat_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _normalize_fx_pair(pair: str | None) -> str:
    raw = (pair or "USDKRW").strip().upper()
    compact = (
        raw.replace("/", "")
        .replace("_", "")
        .replace("-", "")
        .replace(" ", "")
    )
    if compact != "USDKRW":
        raise ValueError(
            f"Unsupported FX pair '{raw}'. Supported: USDKRW"
        )
    return "USDKRW"


async def handle_get_fx_rate(pair: str | None = "USDKRW") -> dict[str, Any]:
    normalized_pair = _normalize_fx_pair(pair)
    quote = await get_usd_krw_rate_details()

    return {
        "pair": normalized_pair,
        "base_currency": "USD",
        "quote_currency": "KRW",
        "rate": quote.rate,
        "mid_rate": quote.mid_rate,
        "default_rate": quote.default_rate,
        "source": quote.source,
        "valid_from": _isoformat_or_none(quote.valid_from),
        "valid_until": _isoformat_or_none(quote.valid_until),
        "basis_point": quote.basis_point,
        "rate_change_type": quote.rate_change_type,
    }


__all__ = ["handle_get_fx_rate"]
```

- [x] **Step 4: Run handler tests to verify they pass**

Run:

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py::TestGetFxRateHandler -q
```

Expected: all `TestGetFxRateHandler` tests pass.

- [x] **Step 5: Commit handler slice**

Run:

```bash
git add app/mcp_server/tooling/fundamentals/_fx_rates.py tests/test_mcp_fundamentals_tools.py
git commit -m "feat(ROB-567): add USDKRW fx rate handler"
```

---

### Task 2: Register `get_fx_rate` on the MCP Surface

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals_handlers.py`
- Modify: `app/mcp_server/__init__.py`
- Modify: `tests/test_mcp_fundamentals_tools.py`
- Test: `tests/test_mcp_fundamentals_tools.py`
- Test: `tests/test_mcp_tool_registration_boot.py`

- [x] **Step 1: Write failing registration tests**

In `tests/test_mcp_fundamentals_tools.py`, add this test class after `TestGetFxRateHandler`:

```python
class TestGetFxRateToolRegistration:
    """Tests for get_fx_rate MCP registration."""

    @pytest.mark.parametrize("profile", list(McpProfile))
    def test_get_fx_rate_registered_on_all_profiles(self, profile: McpProfile):
        tools = _build_tools(profile=profile)

        assert "get_fx_rate" in tools

    @pytest.mark.asyncio
    async def test_registered_get_fx_rate_delegates_to_handler(self, monkeypatch):
        tools = build_tools()

        async def fake_details() -> UsdKrwExchangeRateQuote:
            return UsdKrwExchangeRateQuote(
                rate=1501.0,
                mid_rate=1500.5,
                source="toss",
                basis_point=3.0,
                rate_change_type="UP",
            )

        monkeypatch.setattr(
            fundamentals_fx_rates,
            "get_usd_krw_rate_details",
            fake_details,
        )

        result = await tools["get_fx_rate"](pair="USD/KRW")

        assert result["pair"] == "USDKRW"
        assert result["default_rate"] == pytest.approx(1500.5)
        assert result["source"] == "toss"
        assert result["basis_point"] == pytest.approx(3.0)
        assert result["rate_change_type"] == "UP"
```

- [x] **Step 2: Run registration tests to verify they fail**

Run:

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py::TestGetFxRateToolRegistration -q
```

Expected: tests fail because `get_fx_rate` is not registered in `fundamentals_handlers.py`.

- [x] **Step 3: Import the handler and add the tool name**

In `app/mcp_server/tooling/fundamentals_handlers.py`, add this import near the other fundamentals imports:

```python
from app.mcp_server.tooling.fundamentals._fx_rates import handle_get_fx_rate
```

Add `get_fx_rate` to `FUNDAMENTALS_TOOL_NAMES` near `get_market_index`:

```python
    "get_retail_sentiment",
    "get_fx_rate",
    "get_market_index",
```

- [x] **Step 4: Register the MCP tool**

In `_register_fundamentals_tools_impl()`, add this registration immediately before `get_market_index`:

```python
    @mcp.tool(
        name="get_fx_rate",
        description=(
            "Get the current USD/KRW FX spot quote for exchange-timing and "
            "US-market cash conversion decisions. P1 supports only USDKRW "
            "spot lookup through the existing exchange-rate service; use "
            "ROB-565/follow-ups for account-routing total cost, trend, bank, "
            "or preferential effective-rate modeling."
        ),
    )
    async def get_fx_rate(
        pair: str = "USDKRW",
    ) -> dict[str, Any]:
        return await handle_get_fx_rate(pair)
```

- [x] **Step 5: Advertise the tool name in package metadata**

In `app/mcp_server/__init__.py`, add `get_fx_rate` after `get_market_index` in `AVAILABLE_TOOL_NAMES`:

```python
    "get_market_index",
    "get_fx_rate",
    "get_support_resistance",
```

- [x] **Step 6: Run registration and boot tests**

Run:

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py::TestGetFxRateToolRegistration tests/test_mcp_tool_registration_boot.py -q
```

Expected: registration tests pass, and real FastMCP registration has no duplicate-name failures.

- [x] **Step 7: Commit registration slice**

Run:

```bash
git add app/mcp_server/tooling/fundamentals_handlers.py app/mcp_server/__init__.py tests/test_mcp_fundamentals_tools.py
git commit -m "feat(ROB-567): expose get_fx_rate MCP tool"
```

---

### Task 3: Preserve FX-vs-Index Separation and Update Docs

**Files:**
- Modify: `tests/test_mcp_fundamentals_tools.py`
- Modify: `app/mcp_server/README.md`
- Test: `tests/test_mcp_fundamentals_tools.py`

- [x] **Step 1: Write a regression test that USDKRW is not a market index**

In `tests/test_mcp_fundamentals_tools.py`, add this method to `TestGetMarketIndex`:

```python
    async def test_usdkrw_is_not_market_index(self):
        """FX pairs must stay on get_fx_rate, not get_market_index."""
        tools = build_tools()

        with pytest.raises(ValueError, match="Unknown index symbol 'USDKRW'"):
            await tools["get_market_index"](symbol="USDKRW")
```

- [x] **Step 2: Run the regression test**

Run:

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py::TestGetMarketIndex::test_usdkrw_is_not_market_index -q
```

Expected: pass without adding `USDKRW` to `_INDEX_META`.

- [x] **Step 3: Document `get_fx_rate` in the MCP README**

In `app/mcp_server/README.md`, add this bullet under `### Market Data Tools`, immediately after `get_quote(symbol, market=None)`:

```markdown
- `get_fx_rate(pair="USDKRW")`
  - Read-only spot FX quote for exchange-timing and US-market cash conversion decisions.
  - P1 supports USD/KRW only. Accepted spellings: `USDKRW`, `USD/KRW`, `USD_KRW`, `USD-KRW`.
  - Source is `app.services.exchange_rate_service.get_usd_krw_rate_details()`, which uses Toss when enabled and open.er-api as fallback.
  - Response fields: `pair`, `base_currency`, `quote_currency`, `rate`, `mid_rate`, `default_rate`, `source`, `valid_from`, `valid_until`, `basis_point`, `rate_change_type`.
  - `default_rate` mirrors the scalar exchange-rate behavior used by existing portfolio and cash consumers.
  - Unsupported pairs raise a tool argument error. FX pairs are not market indices; `get_market_index("USDKRW")` remains unsupported.
  - Trends, bank-specific quotes, preferential effective rates, exchange execution, and US-order total-cost routing are outside ROB-567 P1.
```

- [x] **Step 4: Run focused docs-adjacent tests**

Run:

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py::TestGetFxRateHandler tests/test_mcp_fundamentals_tools.py::TestGetFxRateToolRegistration tests/test_mcp_fundamentals_tools.py::TestGetMarketIndex::test_usdkrw_is_not_market_index -q
```

Expected: all focused tests pass.

- [x] **Step 5: Commit docs and separation test**

Run:

```bash
git add tests/test_mcp_fundamentals_tools.py app/mcp_server/README.md
git commit -m "docs(ROB-567): document fx rate MCP contract"
```

---

### Task 4: Final Verification and Linear Update

**Files:**
- Read: `git diff --stat HEAD~3..HEAD`
- Read: `git status --short`
- Update: Linear issue `ROB-567`

- [x] **Step 1: Run the focused test suite**

Run:

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py::TestGetFxRateHandler tests/test_mcp_fundamentals_tools.py::TestGetFxRateToolRegistration tests/test_mcp_fundamentals_tools.py::TestGetMarketIndex::test_usdkrw_is_not_market_index tests/test_mcp_tool_registration_boot.py -q
```

Expected: all selected tests pass.

- [x] **Step 2: Run lint**

Run:

```bash
uv run ruff check app/mcp_server/tooling/fundamentals/_fx_rates.py app/mcp_server/tooling/fundamentals_handlers.py app/mcp_server/__init__.py tests/test_mcp_fundamentals_tools.py
```

Expected: no Ruff violations.

- [x] **Step 3: Run type check for changed runtime files**

Run:

```bash
uv run ty check app/mcp_server/tooling/fundamentals/_fx_rates.py app/mcp_server/tooling/fundamentals_handlers.py app/mcp_server/__init__.py
```

Expected: no type errors in changed runtime files.

- [x] **Step 4: Inspect git state**

Run:

```bash
git status --short
git diff --stat HEAD~3..HEAD
```

Expected: working tree clean except for intentional uncommitted changes outside this plan, and the diff contains only the handler, registration, README, and focused tests.

- [x] **Step 5: Update Linear labels and status**

Use Linear to set:

- `labels`: `Feature`, `keep_on_gpt54`
- `state`: `In Review` after implementation and verification pass

Add this comment to `ROB-567`:

```markdown
ROB-567 P1 implemented as scoped:

- Added read-only `get_fx_rate(pair="USDKRW")` MCP tool.
- Reused existing `exchange_rate_service.get_usd_krw_rate_details()`; no new provider, DB, scheduler, or order/exchange mutation path.
- Kept `USDKRW` out of `get_market_index`; FX remains a separate domain.
- Deferred trend/bank/preferential-rate modeling to P2/P3 and US-order total-cost routing to ROB-565.

Verification:
- `uv run pytest tests/test_mcp_fundamentals_tools.py::TestGetFxRateHandler tests/test_mcp_fundamentals_tools.py::TestGetFxRateToolRegistration tests/test_mcp_fundamentals_tools.py::TestGetMarketIndex::test_usdkrw_is_not_market_index tests/test_mcp_tool_registration_boot.py -q`
- `uv run ruff check app/mcp_server/tooling/fundamentals/_fx_rates.py app/mcp_server/tooling/fundamentals_handlers.py app/mcp_server/__init__.py tests/test_mcp_fundamentals_tools.py`
- `uv run ty check app/mcp_server/tooling/fundamentals/_fx_rates.py app/mcp_server/tooling/fundamentals_handlers.py app/mcp_server/__init__.py`
```

Expected: ROB-567 carries routine-lane metadata and has a concise implementation note.

---

## Self-Review

- Spec coverage: P1 `get_fx_rate(USDKRW)` is covered by Tasks 1 and 2; FX-vs-index separation is covered by Task 3; docs and verification are covered by Tasks 3 and 4.
- Placeholder scan: no unresolved placeholder markers or vague implementation instructions remain.
- Type consistency: handler name is `handle_get_fx_rate`; public tool name is `get_fx_rate`; supported normalized pair is `USDKRW`; response fields match tests, docs, and design.
- Risk check: no migration, execution tool, scheduler, or live-trading boundary is touched, so this stays `keep_on_gpt54` and does not require `high_risk_change`.
