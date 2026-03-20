# Upbit Ticker 404 Invalid Market Filter Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** `DailyScanner.check_price_crash()`가 유효하지 않은 KRW 마켓 코드를 `/v1/ticker`에 전달하지 않도록 보장해 `HTTP 404 Code not found` 재발을 막는다.

**Architecture:** 루트 원인은 보유 코인(`fetch_my_coins`)에서 합성한 `KRW-{currency}` 코드가 현재 거래 불가능한 마켓일 때 전체 티커 조회가 실패하는 점이다. 해결은 `check_price_crash`에서 티커 요청 대상을 `fetch_top_traded_coins("KRW")`로 확보한 실제 거래 가능 마켓 집합으로 제한하는 것이다. 즉, 스캐너 레이어에서 입력을 정규화해 업비트 `/v1/ticker` 호출이 항상 유효 코드를 받도록 만든다.

**Tech Stack:** Python 3.11+, asyncio, FastAPI service layer, pytest + pytest-asyncio + AsyncMock

---

### Task 1: Reproduce Bug With Failing Test

**Files:**
- Modify: `tests/test_daily_scan.py`
- Test: `tests/test_daily_scan.py`

**Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_check_price_crash_filters_invalid_holding_market(
    scanner_env,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, _openclaw, _redis, daily_scan = scanner_env

    monkeypatch.setattr(
        daily_scan,
        "fetch_top_traded_coins",
        AsyncMock(
            return_value=[
                {"market": "KRW-BTC"},
                {"market": "KRW-ETH"},
            ]
        ),
    )
    monkeypatch.setattr(
        daily_scan,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {"currency": "KRW"},
                {"currency": "BTC"},
                {"currency": "PCI"},  # 비거래(또는 상장폐지) 케이스
            ]
        ),
    )

    fetch_tickers = AsyncMock(return_value=[])
    monkeypatch.setattr(daily_scan, "fetch_multiple_tickers", fetch_tickers)

    await scanner.check_price_crash()

    fetch_tickers.assert_awaited_once_with(["KRW-BTC", "KRW-ETH"])
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_daily_scan.py::test_check_price_crash_filters_invalid_holding_market -v`  
Expected: FAIL (현재 구현은 `KRW-PCI`를 포함해 호출)

**Step 3: Commit failing test**

```bash
git add tests/test_daily_scan.py
git commit -m "test: reproduce invalid KRW market in crash scan ticker request"
```

### Task 2: Implement Minimal Market Filtering Fix

**Files:**
- Modify: `app/jobs/daily_scan.py`
- Test: `tests/test_daily_scan.py`

**Step 1: Write minimal implementation**

```python
tradable_krw_markets = {
    str(item.get("market"))
    for item in top_coins
    if str(item.get("market") or "").startswith("KRW-")
}

market_codes = set(tradable_krw_markets)

for coin in my_coins:
    currency = str(coin.get("currency") or "").upper()
    if not currency or currency == "KRW":
        continue
    candidate_market = f"KRW-{currency}"
    if candidate_market in tradable_krw_markets:
        market_codes.add(candidate_market)
```

**Step 2: (Optional but recommended) Add observability log for dropped holdings**

```python
invalid_holding_markets: list[str] = []
# ... loop 내에서 candidate_market not in tradable_krw_markets 이면 append
if invalid_holding_markets:
    logger.info(
        "Skipping non-tradable holding markets for crash scan: %s",
        sorted(set(invalid_holding_markets)),
    )
```

**Step 3: Run focused tests**

Run: `uv run pytest tests/test_daily_scan.py::test_check_price_crash_filters_invalid_holding_market -v`  
Expected: PASS

**Step 4: Run adjacent regression test**

Run: `uv run pytest tests/test_daily_scan.py::test_check_price_crash_threshold_applies -v`  
Expected: PASS (기존 급등/급락 임계치 동작 유지)

**Step 5: Commit implementation**

```bash
git add app/jobs/daily_scan.py tests/test_daily_scan.py
git commit -m "fix: filter invalid KRW holding markets before upbit ticker request"
```

### Task 3: Verify No Regression in Upbit Service Call Contract

**Files:**
- Test: `tests/test_upbit_service.py`

**Step 1: Confirm URL contract test still passes**

Run: `uv run pytest tests/test_upbit_service.py::test_fetch_multiple_tickers_keeps_comma_unescaped -v`  
Expected: PASS (`markets=...` 콤마 인코딩 규칙 불변)

**Step 2: Run combined regression subset**

Run: `uv run pytest tests/test_daily_scan.py tests/test_upbit_service.py -k "price_crash or fetch_multiple_tickers_keeps_comma_unescaped" -v`  
Expected: PASS

**Step 3: Final quality gate**

Run: `make test-unit`  
Expected: PASS (최소 실패 0, 기존 단위테스트 회귀 없음)

**Step 4: Commit verification artifacts (if any test code changed)**

```bash
git add tests/test_daily_scan.py app/jobs/daily_scan.py
git commit -m "test: verify crash scan ticker filtering and upbit request contract"
```

