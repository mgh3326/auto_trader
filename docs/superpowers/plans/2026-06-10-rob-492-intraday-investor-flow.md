# ROB-492 Intraday Investor Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only MCP tool that returns same-day provisional KIS foreign/institution investor-flow quantities for one Korean stock symbol.

**Architecture:** Add a thin KIS client wrapper around `/uapi/domestic-stock/v1/quotations/investor-trend-estimate`, then normalize the raw `output2[]` rows in a focused MCP fundamentals handler. Keep `get_investor_trends` unchanged because it is confirmed daily Naver history, not intraday provisional data.

**Tech Stack:** Python 3.13, FastMCP tool registration, KIS REST client, pytest, Ruff.

---

## Verified Provider Contract

KIS official API:

- Name: `종목별 외인기관 추정가집계[v1_국내주식-046]`
- URL: `/uapi/domestic-stock/v1/quotations/investor-trend-estimate`
- Real TR ID: `HHPTJ04160200`
- Mock trading: unsupported
- Request field: `MKSC_SHRN_ISCD`
- Response array: `output2[]`
- Relevant fields: `bsop_hour_gb`, `frgn_fake_ntby_qty`, `orgn_fake_ntby_qty`, `sum_fake_ntby_qty`

`bsop_hour_gb` maps to `1=09:30`, `2=10:00`, `3=11:20`, `4=13:20`, `5=14:30`. The endpoint returns quantity estimates only. Do not fabricate amounts or individual-investor values.

## File Structure

- Modify `app/services/brokers/kis/constants.py`: add URL/TR constants.
- Modify `app/services/brokers/kis/domestic_market_data.py`: add raw `investor_trend_estimate()` KIS call.
- Modify `app/services/brokers/kis/client.py`: expose the facade method.
- Create `app/mcp_server/tooling/fundamentals/_intraday_investor_flow.py`: validate symbol, call KIS, normalize rows, build MCP response.
- Modify `app/mcp_server/tooling/fundamentals_handlers.py`: import/register `get_intraday_investor_flow`.
- Modify `app/mcp_server/__init__.py`: add tool name to `AVAILABLE_TOOL_NAMES`.
- Modify `app/mcp_server/README.md`: document the new tool and provisional semantics.
- Modify `tests/test_kis_rankings.py`: cover KIS params and output normalization.
- Modify `tests/test_mcp_fundamentals_tools.py`: cover MCP contract and error paths.

---

### Task 1: KIS Raw Endpoint Wrapper

**Files:**
- Modify: `tests/test_kis_rankings.py`
- Modify: `app/services/brokers/kis/constants.py`
- Modify: `app/services/brokers/kis/domestic_market_data.py`
- Modify: `app/services/brokers/kis/client.py`

- [ ] **Step 1: Write failing KIS wrapper tests**

Add constants to the import list near the top of `tests/test_kis_rankings.py`:

```python
    INVESTOR_TREND_ESTIMATE_TR,
    INVESTOR_TREND_ESTIMATE_URL,
```

Add this test class after `TestKISRankingAPIParams`:

```python
@pytest.mark.asyncio
class TestKISInvestorTrendEstimateAPIParams:
    async def test_investor_trend_estimate_api_params(self, monkeypatch):
        captured_requests = []

        async def mock_get(self, url, headers, params, timeout):
            captured_requests.append({"url": url, "headers": headers, "params": params})
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "0",
                "msg_cd": "",
                "msg1": "",
                "output2": [
                    {
                        "bsop_hour_gb": "5",
                        "frgn_fake_ntby_qty": "-120000",
                        "orgn_fake_ntby_qty": "50000",
                        "sum_fake_ntby_qty": "-70000",
                    }
                ],
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        result = await KISClient().investor_trend_estimate("000660")

        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert (
            req["url"]
            == f"https://openapi.koreainvestment.com:9443{INVESTOR_TREND_ESTIMATE_URL}"
        )
        assert req["headers"]["tr_id"] == INVESTOR_TREND_ESTIMATE_TR
        assert req["headers"]["authorization"] == "Bearer test_token"
        assert req["params"] == {"MKSC_SHRN_ISCD": "000660"}
        assert result == [
            {
                "bsop_hour_gb": "5",
                "frgn_fake_ntby_qty": "-120000",
                "orgn_fake_ntby_qty": "50000",
                "sum_fake_ntby_qty": "-70000",
            }
        ]

    async def test_investor_trend_estimate_filters_malformed_output(self, monkeypatch):
        async def mock_get(self, url, headers, params, timeout):
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "0",
                "msg_cd": "",
                "msg1": "",
                "output2": [
                    {"bsop_hour_gb": "1", "frgn_fake_ntby_qty": "10"},
                    "not-a-row",
                    None,
                ],
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        result = await KISClient().investor_trend_estimate("660")

        assert result == [{"bsop_hour_gb": "1", "frgn_fake_ntby_qty": "10"}]
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_kis_rankings.py::TestKISInvestorTrendEstimateAPIParams -q
```

Expected: FAIL because `INVESTOR_TREND_ESTIMATE_TR`, `INVESTOR_TREND_ESTIMATE_URL`, and `KISClient.investor_trend_estimate` do not exist yet.

- [ ] **Step 3: Add KIS constants**

Add immediately after `INVESTOR_TRADING_TR` in `app/services/brokers/kis/constants.py`:

```python
INVESTOR_TREND_ESTIMATE_URL = (
    "/uapi/domestic-stock/v1/quotations/investor-trend-estimate"
)
INVESTOR_TREND_ESTIMATE_TR = "HHPTJ04160200"
```

- [ ] **Step 4: Add raw KIS wrapper**

Add this method in `app/services/brokers/kis/domestic_market_data.py` after `inquire_investor()`:

```python
    async def investor_trend_estimate(
        self,
        code: str,
    ) -> list[dict[str, Any]]:
        js = await self._request_with_token_retry(
            tr_id=constants.INVESTOR_TREND_ESTIMATE_TR,
            url=self._kis_url(constants.INVESTOR_TREND_ESTIMATE_URL),
            params={
                "MKSC_SHRN_ISCD": str(code).zfill(6),
            },
            api_name="investor_trend_estimate",
        )
        output = js.get("output2") or []
        if isinstance(output, dict):
            return [output]
        if not isinstance(output, list):
            return []
        return [row for row in output if isinstance(row, dict)]
```

Add this facade method in `app/services/brokers/kis/client.py` after `inquire_investor()`:

```python
    async def investor_trend_estimate(
        self, code: str
    ) -> list[dict[str, Any]]:
        return await self._market_data.investor_trend_estimate(code)
```

- [ ] **Step 5: Run tests to verify KIS wrapper passes**

Run:

```bash
uv run pytest tests/test_kis_rankings.py::TestKISInvestorTrendEstimateAPIParams -q
```

Expected: PASS.

- [ ] **Step 6: Commit KIS wrapper**

Run:

```bash
git add tests/test_kis_rankings.py app/services/brokers/kis/constants.py app/services/brokers/kis/domestic_market_data.py app/services/brokers/kis/client.py
git commit -m "feat(ROB-492): add KIS investor trend estimate wrapper"
```

---

### Task 2: MCP Intraday Investor Flow Handler

**Files:**
- Create: `app/mcp_server/tooling/fundamentals/_intraday_investor_flow.py`
- Modify: `tests/test_mcp_fundamentals_tools.py`

- [ ] **Step 1: Write failing MCP handler tests**

Add this import near the existing `app.mcp_server.tooling` imports in `tests/test_mcp_fundamentals_tools.py`:

```python
from app.mcp_server.tooling.fundamentals import (
    _intraday_investor_flow as intraday_investor_flow,
)
```

Add this test class after `TestGetInvestorTrends`:

```python
@pytest.mark.asyncio
class TestGetIntradayInvestorFlow:
    async def test_maps_latest_kis_intraday_estimate(self, monkeypatch):
        import datetime as _dt

        tools = build_tools()

        class MockKISClient:
            async def investor_trend_estimate(self, code):
                assert code == "000660"
                return [
                    {
                        "bsop_hour_gb": "1",
                        "frgn_fake_ntby_qty": "-10000",
                        "orgn_fake_ntby_qty": "",
                        "sum_fake_ntby_qty": "-10000",
                    },
                    {
                        "bsop_hour_gb": "5",
                        "frgn_fake_ntby_qty": "-120000",
                        "orgn_fake_ntby_qty": "50000",
                        "sum_fake_ntby_qty": "-70000",
                    },
                ]

        monkeypatch.setattr(intraday_investor_flow, "KISClient", MockKISClient)
        monkeypatch.setattr(
            intraday_investor_flow,
            "now_kst",
            lambda: _dt.datetime(2026, 6, 10, 15, 1, tzinfo=intraday_investor_flow.KST),
        )
        monkeypatch.setattr(
            intraday_investor_flow,
            "kr_market_data_state",
            lambda: "fresh",
        )

        result = await tools["get_intraday_investor_flow"]("000660")

        assert result["symbol"] == "000660"
        assert result["source"] == "kis"
        assert result["data_state"] == "intraday_provisional"
        assert result["market_session_state"] == "fresh"
        assert result["provisional"] is True
        assert result["as_of"] == "2026-06-10T14:30:00+09:00"
        assert result["as_of_time_kst"] == "14:30"
        assert result["foreign_net_qty"] == -120000
        assert result["institution_net_qty"] == 50000
        assert result["combined_net_qty"] == -70000
        assert len(result["rows"]) == 2
        assert result["rows"][0]["institution_net_qty"] is None

    async def test_returns_empty_success_when_kis_has_no_rows(self, monkeypatch):
        import datetime as _dt

        tools = build_tools()

        class MockKISClient:
            async def investor_trend_estimate(self, code):
                return []

        monkeypatch.setattr(intraday_investor_flow, "KISClient", MockKISClient)
        monkeypatch.setattr(
            intraday_investor_flow,
            "now_kst",
            lambda: _dt.datetime(2026, 6, 10, 8, 30, tzinfo=intraday_investor_flow.KST),
        )
        monkeypatch.setattr(
            intraday_investor_flow,
            "kr_market_data_state",
            lambda: "premarket_unavailable",
        )

        result = await tools["get_intraday_investor_flow"]("000660")

        assert result["rows"] == []
        assert result["as_of"] is None
        assert result["foreign_net_qty"] is None
        assert result["institution_net_qty"] is None
        assert result["combined_net_qty"] is None
        assert result["market_session_state"] == "premarket_unavailable"
        assert "No KIS provisional investor-flow rows" in result["note"]

    async def test_rejects_non_kr_symbol(self):
        tools = build_tools()

        with pytest.raises(ValueError, match="Korean stocks"):
            await tools["get_intraday_investor_flow"]("AAPL")

    async def test_upstream_error_returns_error_payload(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def investor_trend_estimate(self, code):
                raise RuntimeError("KIS down")

        monkeypatch.setattr(intraday_investor_flow, "KISClient", MockKISClient)

        result = await tools["get_intraday_investor_flow"]("000660")

        assert result["source"] == "kis"
        assert result["symbol"] == "000660"
        assert result["instrument_type"] == "equity_kr"
        assert "KIS down" in (result.get("error") or result.get("message") or "")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py::TestGetIntradayInvestorFlow -q
```

Expected: FAIL because the new handler module and registered tool do not exist.

- [ ] **Step 3: Create handler module**

Create `app/mcp_server/tooling/fundamentals/_intraday_investor_flow.py`:

```python
"""Intraday provisional KR investor-flow MCP handler."""

from __future__ import annotations

import datetime
from typing import Any

from app.core.timezone import KST, now_kst
from app.mcp_server.tooling.market_session import kr_market_data_state
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
)
from app.mcp_server.tooling.shared import (
    is_korean_equity_code as _is_korean_equity_code,
)
from app.services.brokers.kis.client import KISClient

DATA_STATE_INTRADAY_PROVISIONAL = "intraday_provisional"

_SLOT_TIMES: dict[str, str] = {
    "1": "09:30",
    "2": "10:00",
    "3": "11:20",
    "4": "13:20",
    "5": "14:30",
}

_PROVISIONAL_NOTE = (
    "KIS investor-trend-estimate is intraday provisional cumulative input, "
    "not a confirmed daily close figure."
)


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _slot_sort_key(row: dict[str, Any]) -> int:
    slot = str(row.get("slot") or "").strip()
    try:
        return int(slot)
    except ValueError:
        return -1


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    slot = str(row.get("bsop_hour_gb") or "").strip()
    return {
        "slot": slot or None,
        "as_of_time_kst": _SLOT_TIMES.get(slot),
        "foreign_net_qty": _to_int(row.get("frgn_fake_ntby_qty")),
        "institution_net_qty": _to_int(row.get("orgn_fake_ntby_qty")),
        "combined_net_qty": _to_int(row.get("sum_fake_ntby_qty")),
    }


def _as_of(slot_time: str | None) -> str | None:
    if slot_time is None:
        return None
    hour, minute = (int(part) for part in slot_time.split(":", maxsplit=1))
    dt = datetime.datetime.combine(
        now_kst().date(),
        datetime.time(hour=hour, minute=minute),
        tzinfo=KST,
    )
    return dt.isoformat()


async def handle_get_intraday_investor_flow(symbol: str) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    if not _is_korean_equity_code(symbol):
        raise ValueError(
            "Intraday investor flow is only available for Korean stocks "
            "(6-digit codes like '005930')"
        )

    try:
        raw_rows = await KISClient().investor_trend_estimate(symbol)
    except Exception as exc:
        return _error_payload(
            source="kis",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_kr",
        )

    rows = [_normalize_row(row) for row in raw_rows]
    rows.sort(key=_slot_sort_key)
    latest = rows[-1] if rows else None
    latest_time = latest.get("as_of_time_kst") if latest is not None else None

    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "source": "kis",
        "data_state": DATA_STATE_INTRADAY_PROVISIONAL,
        "market_session_state": kr_market_data_state(),
        "provisional": True,
        "as_of": _as_of(latest_time),
        "as_of_time_kst": latest_time,
        "foreign_net_qty": (
            latest.get("foreign_net_qty") if latest is not None else None
        ),
        "institution_net_qty": (
            latest.get("institution_net_qty") if latest is not None else None
        ),
        "combined_net_qty": (
            latest.get("combined_net_qty") if latest is not None else None
        ),
        "rows": rows,
        "note": (
            _PROVISIONAL_NOTE
            if rows
            else "No KIS provisional investor-flow rows were returned."
        ),
    }
```

- [ ] **Step 4: Run direct handler tests after registration task if needed**

If the test still fails only because the tool is not registered, continue to Task 3 before rerunning the MCP test class.

---

### Task 3: Register MCP Tool and Document Contract

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals_handlers.py`
- Modify: `app/mcp_server/__init__.py`
- Modify: `app/mcp_server/README.md`
- Test: `tests/test_mcp_fundamentals_tools.py`

- [ ] **Step 1: Register handler import and tool name**

In `app/mcp_server/tooling/fundamentals_handlers.py`, add this import near other fundamentals imports:

```python
from app.mcp_server.tooling.fundamentals._intraday_investor_flow import (
    handle_get_intraday_investor_flow,
)
```

Add `"get_intraday_investor_flow"` to `FUNDAMENTALS_TOOL_NAMES`.

Add this registration immediately after `get_investor_trends`:

```python
    @mcp.tool(
        name="get_intraday_investor_flow",
        description=(
            "Get same-day intraday provisional foreign/institution net-buy "
            "quantity estimates for a Korean stock. Returns KIS "
            "investor-trend-estimate rows with provisional/as_of metadata. "
            "Korean stocks only."
        ),
    )
    async def get_intraday_investor_flow(
        symbol: str,
    ) -> dict[str, Any]:
        return await handle_get_intraday_investor_flow(symbol)
```

In `app/mcp_server/__init__.py`, add `"get_intraday_investor_flow"` immediately after `"get_investor_trends"`.

- [ ] **Step 2: Add README entry**

In `app/mcp_server/README.md`, add this under the Market Data Tools list near `get_investment_opinions` and `get_short_interest`:

```markdown
- `get_intraday_investor_flow(symbol)`
  - KR-only read-only tool for same-day provisional foreign/institution flow by symbol.
  - Source: KIS `investor-trend-estimate` (`/uapi/domestic-stock/v1/quotations/investor-trend-estimate`, TR `HHPTJ04160200`).
  - Returns quantity estimates only: `foreign_net_qty`, `institution_net_qty`, `combined_net_qty`.
  - The response always marks successful data as `provisional: true` and `data_state: "intraday_provisional"`.
  - `as_of` is inferred from the latest returned KIS slot (`bsop_hour_gb`: 09:30, 10:00, 11:20, 13:20, 14:30) on the KST request date because the KIS payload does not include a date field.
  - This is not a confirmed daily close figure and should not be mixed with `get_investor_trends` day/week/month history.
```

- [ ] **Step 3: Run MCP tests**

Run:

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py::TestGetIntradayInvestorFlow -q
```

Expected: PASS.

- [ ] **Step 4: Run related existing fundamentals tests**

Run:

```bash
uv run pytest tests/test_mcp_fundamentals_tools.py::TestGetInvestorTrends tests/test_mcp_fundamentals_tools.py::TestGetIntradayInvestorFlow -q
```

Expected: PASS. Existing `get_investor_trends` behavior remains unchanged.

- [ ] **Step 5: Commit MCP surface**

Run:

```bash
git add app/mcp_server/tooling/fundamentals/_intraday_investor_flow.py app/mcp_server/tooling/fundamentals_handlers.py app/mcp_server/__init__.py app/mcp_server/README.md tests/test_mcp_fundamentals_tools.py
git commit -m "feat(ROB-492): expose intraday investor flow MCP tool"
```

---

### Task 4: Full Verification

**Files:**
- No source edits unless verification exposes a defect.

- [ ] **Step 1: Run targeted test set**

Run:

```bash
uv run pytest tests/test_kis_rankings.py::TestKISInvestorTrendEstimateAPIParams tests/test_mcp_fundamentals_tools.py::TestGetIntradayInvestorFlow tests/test_mcp_fundamentals_tools.py::TestGetInvestorTrends -q
```

Expected: PASS.

- [ ] **Step 2: Run lint on touched files**

Run:

```bash
uv run ruff check app/services/brokers/kis/constants.py app/services/brokers/kis/domestic_market_data.py app/services/brokers/kis/client.py app/mcp_server/tooling/fundamentals/_intraday_investor_flow.py app/mcp_server/tooling/fundamentals_handlers.py app/mcp_server/__init__.py tests/test_kis_rankings.py tests/test_mcp_fundamentals_tools.py
```

Expected: PASS.

- [ ] **Step 3: Run formatter check on touched files**

Run:

```bash
uv run ruff format --check app/services/brokers/kis/constants.py app/services/brokers/kis/domestic_market_data.py app/services/brokers/kis/client.py app/mcp_server/tooling/fundamentals/_intraday_investor_flow.py app/mcp_server/tooling/fundamentals_handlers.py app/mcp_server/__init__.py tests/test_kis_rankings.py tests/test_mcp_fundamentals_tools.py
```

Expected: PASS.

- [ ] **Step 4: Optional live smoke, only with real KIS credentials**

Run only when the environment has real KIS credentials and live market-data calls are allowed:

```bash
uv run python - <<'PY'
import asyncio
from app.mcp_server.tooling.fundamentals._intraday_investor_flow import (
    handle_get_intraday_investor_flow,
)

async def main():
    result = await handle_get_intraday_investor_flow("000660")
    print(result)

asyncio.run(main())
PY
```

Expected: payload has `source="kis"`, `data_state="intraday_provisional"`, `provisional is True`, and either normalized rows or a KIS error payload. Do not treat lack of live credentials as a test failure.

- [ ] **Step 5: Commit verification cleanup if changes were needed**

If verification required fixes, run:

```bash
git add app tests
git commit -m "fix(ROB-492): tighten intraday investor flow verification"
```

If no fixes were needed, skip this commit.

---

## Self-Review

- Spec coverage: ROB-492 asks for same-day intraday provisional foreign/institution net by symbol. This plan uses KIS `investor-trend-estimate`, which is the verified same-day provisional by-symbol endpoint.
- Public contract: `get_intraday_investor_flow` is separate from `get_investor_trends`, preserving historical confirmed trend semantics.
- Data honesty: the endpoint exposes quantities only. The MCP contract does not invent trade amounts or individual flow.
- Risk classification: read-only market-data/MCP feature, no DB migration, no scheduler, no live order approval boundary, no strategy policy change. No `high_risk_change` label required by the project guardrails.
