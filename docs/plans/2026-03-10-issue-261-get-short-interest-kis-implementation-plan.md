# Issue #261 get_short_interest KIS Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the `get_short_interest` backend with the KIS daily short-sale API while keeping the public MCP tool name, KR-only validation, and response keys stable.

**Architecture:** Add a new KIS market-data client method that returns normalized `output1`/`output2`, then add `app.services.market_data.service.get_short_interest()` to map KIS rows into the existing MCP payload shape. Repoint the MCP handler to the new service, remove the legacy Naver/KRX/`pykrx` short-interest path, and update the focused test suites plus dependency metadata.

**Tech Stack:** Python 3.13, FastAPI/FastMCP, KIS REST client, pandas, pytest, uv lock.

---

## Verified Constraints From Search

- Official KIS endpoint: `GET /uapi/domestic-stock/v1/quotations/daily-short-sale`
- Official TR ID: `FHPST04830000`
- Required request params: `FID_COND_MRKT_DIV_CODE`, `FID_INPUT_ISCD`, `FID_INPUT_DATE_1`, `FID_INPUT_DATE_2`
- Official response shape: `output1` is a single dict; `output2` is a list of per-day dict rows
- `output2` already includes direct total fields: `acml_vol` and `acml_tr_pbmn`
- `output2` includes direct short-sale ratio fields: `ssts_vol_rlim` and `ssts_tr_pbmn_rlim`
- The daily short-sale response does **not** include a stock name field; `name` needs a separate fallback lookup
- All KIS values arrive as strings and must be cast explicitly
- `short_balance` is not available from this endpoint and should stay optional/omitted

## Spec Corrections To Carry Into Implementation

- Treat `name` from KIS summary as unavailable; use a lightweight fallback lookup every time unless a local cache/table lookup is added.
- Map `total_volume` from `acml_vol` and `total_amount` from `acml_tr_pbmn` directly when present; only derive totals as a defensive fallback if KIS omits them.
- Use `ssts_vol_rlim` as the primary source for `short_ratio`; do not prefer the amount ratio unless the volume ratio is missing.
- Pass explicit start/end dates to KIS instead of relying on blank date defaults.

### Task 1: Add failing KIS client tests for the daily short-sale endpoint

**Files:**
- Modify: `tests/test_services_kis_market_data.py`
- Reference: `app/services/brokers/kis/market_data.py`

**Step 1: Write the failing success-path test**

Add a test that mocks `_request_with_rate_limit()` and expects:
- URL ends with `/daily-short-sale`
- `tr_id == "FHPST04830000"`
- params include `FID_COND_MRKT_DIV_CODE`, `FID_INPUT_ISCD`, `FID_INPUT_DATE_1`, `FID_INPUT_DATE_2`
- return value is `(output1_dict, output2_list)`

```python
@pytest.mark.asyncio
async def test_kis_inquire_short_selling_parses_output1_and_output2(monkeypatch):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output1": {"stck_prpr": "70000", "acml_vol": "2000000"},
            "output2": [
                {
                    "stck_bsop_date": "20260310",
                    "ssts_cntg_qty": "100000",
                    "ssts_vol_rlim": "5.0",
                    "ssts_tr_pbmn": "1000000000",
                    "acml_vol": "2000000",
                    "acml_tr_pbmn": "20000000000",
                }
            ],
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    output1, output2 = await client.inquire_short_selling(
        code="005930",
        start_date="20260301",
        end_date="20260310",
    )

    assert output1["stck_prpr"] == "70000"
    assert output2[0]["ssts_cntg_qty"] == "100000"
```

**Step 2: Run the single test to verify RED**

Run: `uv run pytest tests/test_services_kis_market_data.py -q -k short_selling_parses_output1_and_output2`

Expected: FAIL with missing `inquire_short_selling`

**Step 3: Add failing edge-case tests**

Add tests for:
- token retry on `EGW00121` and `EGW00123`
- empty `output2` returns `[]` without crashing
- malformed `output1` (non-dict) raises `RuntimeError`
- malformed `output2` (non-list / non-dict row) raises `RuntimeError`

**Step 4: Run the focused KIS test subset to verify RED**

Run: `uv run pytest tests/test_services_kis_market_data.py -q -k short_selling`

Expected: FAIL only because the feature does not exist yet

### Task 2: Implement the KIS client method and pass-through

**Files:**
- Modify: `app/services/brokers/kis/constants.py`
- Modify: `app/services/brokers/kis/market_data.py`
- Modify: `app/services/brokers/kis/client.py`

**Step 1: Add constants**

Add near the other domestic quotation constants:

```python
DOMESTIC_DAILY_SHORT_SALE_URL = "/uapi/domestic-stock/v1/quotations/daily-short-sale"
DOMESTIC_DAILY_SHORT_SALE_TR = "FHPST04830000"
```

**Step 2: Add payload validators in `market_data.py`**

Add narrow validators similar to `_validate_daily_itemchartprice_chunk()`:

```python
def _validate_short_selling_output1(output1: Any) -> dict[str, Any]:
    if not isinstance(output1, dict):
        raise RuntimeError("Malformed KIS short selling payload: expected dict in output1")
    return output1


def _validate_short_selling_output2(output2: Any) -> list[dict[str, Any]]:
    if output2 in (None, ""):
        return []
    if not isinstance(output2, list):
        raise RuntimeError("Malformed KIS short selling payload: expected list in output2")
    validated: list[dict[str, Any]] = []
    for index, row in enumerate(output2):
        if not isinstance(row, dict):
            raise RuntimeError(
                f"Malformed KIS short selling payload at row {index}: expected object"
            )
        validated.append(row)
    return validated
```

**Step 3: Implement `MarketDataClient.inquire_short_selling()`**

Follow the existing KIS request pattern:

```python
async def inquire_short_selling(
    self,
    code: str,
    start_date: str,
    end_date: str,
    market: str = "J",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    await self._parent._ensure_token()
    hdr = self._parent._hdr_base | {
        "authorization": f"Bearer {self._settings.kis_access_token}",
        "tr_id": constants.DOMESTIC_DAILY_SHORT_SALE_TR,
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": market,
        "FID_INPUT_ISCD": code.zfill(6),
        "FID_INPUT_DATE_1": start_date,
        "FID_INPUT_DATE_2": end_date,
    }
    js = await self._parent._request_with_rate_limit(
        "GET",
        f"{constants.BASE}{constants.DOMESTIC_DAILY_SHORT_SALE_URL}",
        headers=hdr,
        params=params,
        timeout=5,
        api_name="inquire_short_selling",
        tr_id=constants.DOMESTIC_DAILY_SHORT_SALE_TR,
    )
    if js.get("rt_cd") != "0":
        if js.get("msg_cd") in {"EGW00121", "EGW00123"}:
            await self._parent._token_manager.clear_token()
            await self._parent._ensure_token()
            return await self.inquire_short_selling(code, start_date, end_date, market)
        raise RuntimeError(f"{js.get('msg_cd')} {js.get('msg1')}")

    output1 = _validate_short_selling_output1(js.get("output1"))
    output2 = _validate_short_selling_output2(js.get("output2"))
    return output1, output2
```

**Step 4: Add the KIS facade pass-through in `client.py`**

```python
async def inquire_short_selling(
    self,
    code: str,
    start_date: str,
    end_date: str,
    market: str = "J",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return await self._market_data.inquire_short_selling(
        code, start_date, end_date, market
    )
```

**Step 5: Run the KIS client tests to verify GREEN**

Run: `uv run pytest tests/test_services_kis_market_data.py -q -k short_selling`

Expected: PASS

### Task 3: Add failing market-data service tests for short-interest mapping

**Files:**
- Modify: `tests/test_market_data_service.py`
- Reference: `app/services/market_data/service.py`

**Step 1: Add a normalization/date-range test**

Write a test that calls `market_data_service.get_short_interest("5930", days=20)` and expects:
- symbol normalized to `005930`
- KIS called with `start_date = today - 40 days` and `end_date = today`
- KIS market default is `J`

Use `freezegun`-style time control if already available; otherwise monkeypatch `market_data_service.dt.date.today` with a lightweight test stub.

**Step 2: Add a row-mapping test**

Mock KIS rows that include:
- `stck_bsop_date`
- `ssts_cntg_qty`
- `ssts_tr_pbmn`
- `ssts_vol_rlim`
- `acml_vol`
- `acml_tr_pbmn`

Assert the service returns:

```python
{
    "symbol": "005930",
    "name": "삼성전자",
    "short_data": [
        {
            "date": "2026-03-10",
            "short_volume": 100000,
            "short_amount": 1000000000,
            "short_ratio": 5.0,
            "total_volume": 2000000,
            "total_amount": 20000000000,
        }
    ],
    "avg_short_ratio": 5.0,
}
```

**Step 3: Add edge-case tests**

Cover:
- rows sorted descending by date, then truncated to `days`
- `avg_short_ratio` ignores `None`
- no-data returns `short_data=[]` and `avg_short_ratio=None`
- `name` fallback uses `fetch_fundamental_info()` when KIS short-sale response has no name
- if fallback name lookup fails, `name` is `None`

**Step 4: Run the market-data short-interest tests to verify RED**

Run: `uv run pytest tests/test_market_data_service.py -q -k short_interest`

Expected: FAIL with missing service method

### Task 4: Implement `market_data_service.get_short_interest()` and export it

**Files:**
- Modify: `app/services/market_data/service.py`
- Modify: `app/services/market_data/__init__.py`

**Step 1: Add small parsing helpers**

Reuse the existing `_to_optional_int()` pattern and add an optional float parser if needed:

```python
def _to_optional_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(str(value))
    except (TypeError, ValueError):
        return None
```

**Step 2: Implement the service method**

```python
async def get_short_interest(symbol: str, days: int = 20) -> dict[str, Any]:
    resolved_symbol = _normalize_symbol(symbol, "equity_kr")
    capped_days = min(max(int(days), 1), 60)
    end_date = dt.date.today()
    start_date = end_date - dt.timedelta(days=capped_days * 2)

    kis = KISClient()
    try:
        output1, rows = await kis.inquire_short_selling(
            code=resolved_symbol,
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
            market="J",
        )
    except Exception as exc:
        raise _map_error(exc) from exc

    mapped_rows = []
    for row in rows:
        mapped_rows.append(
            {
                "date": pd.to_datetime(row["stck_bsop_date"], format="%Y%m%d").strftime("%Y-%m-%d"),
                "short_volume": _to_optional_int(row.get("ssts_cntg_qty")),
                "short_amount": _to_optional_int(row.get("ssts_tr_pbmn")),
                "short_ratio": _to_optional_float(row.get("ssts_vol_rlim")),
                "total_volume": _to_optional_int(row.get("acml_vol")),
                "total_amount": _to_optional_int(row.get("acml_tr_pbmn")),
            }
        )

    mapped_rows.sort(key=lambda row: row["date"], reverse=True)
    short_data = mapped_rows[:capped_days]
    valid_ratios = [row["short_ratio"] for row in short_data if row["short_ratio"] is not None]
    avg_short_ratio = round(sum(valid_ratios) / len(valid_ratios), 2) if valid_ratios else None

    name = None
    try:
        fundamental = await kis.fetch_fundamental_info(resolved_symbol, market="UN")
        raw_name = fundamental.get("종목명")
        name = str(raw_name).strip() if raw_name else None
    except Exception:
        name = None

    return {
        "symbol": resolved_symbol,
        "name": name,
        "short_data": short_data,
        "avg_short_ratio": avg_short_ratio,
    }
```

Do **not** add `short_balance` here.

**Step 3: Export it**

Update `service.py` `__all__` and `app/services/market_data/__init__.py` to include `get_short_interest`.

**Step 4: Run the market-data tests to verify GREEN**

Run: `uv run pytest tests/test_market_data_service.py -q -k short_interest`

Expected: PASS

### Task 5: Repoint the MCP tool and update MCP contract tests

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals_handlers.py`
- Modify: `tests/test_mcp_fundamentals_tools.py`

**Step 1: Update the handler import**

Add:

```python
from app.services import market_data as market_data_service
```

Keep `naver_finance` for the other fundamentals tools.

**Step 2: Repoint `get_short_interest()`**

Change:

```python
return await naver_finance.fetch_short_interest(symbol, capped_days)
```

to:

```python
return await market_data_service.get_short_interest(symbol, capped_days)
```

and change the error payload source from `"krx"` to `"kis"`.

**Step 3: Update tests to monkeypatch the new target**

In `tests/test_mcp_fundamentals_tools.py`:
- add/import `app.services.market_data as market_data_service`
- replace short-interest monkeypatches from `naver_finance.fetch_short_interest` to `market_data_service.get_short_interest`
- update the error expectation to `result["source"] == "kis"`
- keep KR-only validation and `days == 60` cap assertions unchanged

**Step 4: Run the MCP short-interest tests to verify GREEN**

Run: `uv run pytest tests/test_mcp_fundamentals_tools.py -q -k short_interest`

Expected: PASS

### Task 6: Remove the legacy Naver short-interest path and `pykrx`

**Files:**
- Modify: `app/services/naver_finance.py`
- Modify: `tests/test_naver_finance.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Step 1: Delete only the short-interest helpers from `naver_finance.py`**

Remove:
- `_fetch_short_data_from_krx`
- `_fetch_short_data_from_pykrx`
- `_fetch_daily_volumes`
- `fetch_short_interest`
- stale module docstring mention of `pykrx`

Leave disclosures, news, valuation, investor trends, and other Naver functionality untouched.

**Step 2: Clean the Naver tests**

Delete or rewrite only the short-interest-specific tests in `tests/test_naver_finance.py` (`TestFetchShortInterest`). Keep the rest of the file intact.

**Step 3: Remove the dependency**

Delete `"pykrx>=1.0.0,<2.0.0"` from `pyproject.toml`.

**Step 4: Regenerate the lockfile**

Run: `uv lock`

Expected: `pykrx` removed from `uv.lock`

**Step 5: Run the Naver-focused tests to verify GREEN**

Run: `uv run pytest tests/test_naver_finance.py -q`

Expected: PASS with the short-interest tests removed or replaced

### Task 7: Run the required verification set and inspect diffs

**Files:**
- Verify only; no new file creation expected beyond the plan file

**Step 1: Run the focused issue test suite**

Run:

```bash
uv run pytest tests/test_services_kis_market_data.py tests/test_market_data_service.py tests/test_mcp_fundamentals_tools.py -q
```

Expected: PASS

**Step 2: Run the Naver suite**

Run:

```bash
uv run pytest tests/test_naver_finance.py -q
```

Expected: PASS

**Step 3: Run lints/types on touched files if repo fast enough**

Run:

```bash
make lint
```

Expected: PASS

If `make lint` is too broad for the branch, run the repo-standard focused commands instead.

**Step 4: Inspect the lockfile cleanup**

Run:

```bash
git diff -- pyproject.toml uv.lock
```

Expected: only `pykrx` removal and resolver fallout

**Step 5: Perform the benchmark follow-up from Issue #260**

Re-run the same external benchmark procedure used for Issue #260 and capture before/after timing for `get_short_interest`. Do not broaden this into `get_disclosures` work.

### Task 8: Optional follow-up cleanup (do not block Issue #261)

**Files:**
- Optional: `blog/blog_11_mcp_server.md`

**Step 1: Record stale docs only if requested**

Search evidence shows `blog/blog_11_mcp_server.md` still describes Naver/short-interest behavior. Do **not** expand Issue #261 scope automatically; note it for a separate cleanup if the branch wants doc parity.
