# Sonar Pass 1 Security And Correctness Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the pass 1 runtime Sonar backlog by making websocket TLS verification secure-by-default, bounding KIS daily-candle lookbacks before they drive paging loops, and fixing only the runtime bug candidates that survive direct code inspection and regression testing.

**Architecture:** Keep the changes narrow and compatibility-preserving. TLS hardening is limited to the Upbit websocket clients that actually use `wss://` today, KIS daily-lookback normalization preserves the repo's existing silent cap-to-200 behavior, and suspicious bug candidates are handled with targeted tests first so false positives are explicitly dispositioned instead of being rewritten blindly.

**Tech Stack:** Python 3.13+, FastAPI, websockets, asyncio, pandas, pytest, Ruff, ty

---

### Task 1: Make Upbit websocket TLS verification secure-by-default

**Files:**
- Modify: `app/services/upbit_websocket.py`
- Modify: `app/services/upbit_market_websocket.py`
- Modify: `upbit_websocket_monitor.py`
- Modify: `websocket_monitor.py`
- Test: `tests/test_upbit_websocket_service.py`
- Test: create `tests/test_upbit_market_websocket.py` or extend an existing websocket router/client test

**Step 1: Add failing TLS-behavior tests**

Extend the websocket unit tests with coverage for the secure default and the explicit insecure opt-in.

```python
def test_upbit_ssl_context_verifies_by_default() -> None:
    client = UpbitMyOrderWebSocket()
    ssl_context = client._create_ssl_context()

    assert ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert ssl_context.check_hostname is True


def test_upbit_ssl_context_allows_explicit_insecure_mode() -> None:
    client = UpbitMyOrderWebSocket(verify_ssl=False)
    ssl_context = client._create_ssl_context()

    assert ssl_context.verify_mode == ssl.CERT_NONE
    assert ssl_context.check_hostname is False


def test_public_upbit_ssl_context_verifies_by_default() -> None:
    client = UpbitPublicWebSocketClient()
    ssl_context = client._create_ssl_context()

    assert ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert ssl_context.check_hostname is True
```

**Step 2: Run the websocket slices to confirm RED**

Run: `uv run pytest --no-cov tests/test_upbit_websocket_service.py tests/test_upbit_market_websocket.py -k "ssl_context or verify" -q`
Expected: FAIL because the Upbit websocket clients still default to insecure verification.

**Step 3: Implement the minimal TLS changes**

- Change `verify_ssl` defaults to `True` in `UpbitMyOrderWebSocket`, `UpbitOrderAnalysisService`, and `UpbitPublicWebSocketClient`.
- Keep the insecure path available only through an explicit `verify_ssl=False` call and log it as an override.
- Remove the runtime default bypasses in `upbit_websocket_monitor.py` and `websocket_monitor.py`; if a runtime opt-out is needed, thread it through an explicit flag or environment read instead of hardcoding `False`.
- Do not change `app/services/kis_websocket.py` in pass 1 unless a supported `wss://` KIS runtime path is proven first.

**Step 4: Re-run the websocket slices**

Run: `uv run pytest --no-cov tests/test_upbit_websocket_service.py tests/test_upbit_market_websocket.py -k "ssl_context or verify" -q`
Expected: PASS.

**Step 5: Run the broader websocket safety slice**

Run: `uv run pytest --no-cov tests/test_upbit_websocket_service.py tests/test_upbit_market_websocket.py tests/test_websocket_monitor.py -q`
Expected: PASS.

**Step 6: Commit**

```bash
git add app/services/upbit_websocket.py app/services/upbit_market_websocket.py upbit_websocket_monitor.py websocket_monitor.py tests/test_upbit_websocket_service.py tests/test_upbit_market_websocket.py tests/test_websocket_monitor.py
git commit -m "fix: secure websocket TLS defaults"
```

---

### Task 2: Bound KIS daily-candle lookbacks before they hit the paging loop

**Files:**
- Modify: `app/services/brokers/kis/constants.py`
- Modify: `app/services/brokers/kis/market_data.py`
- Modify: `app/services/brokers/kis/client.py`
- Modify: `app/routers/trading.py`
- Test: `tests/test_services_kis_market_data.py`
- Test: `tests/test_trading_orderbook_router.py` or create `tests/test_trading_ohlcv_router.py`

**Step 1: Add failing service tests for invalid and oversized lookbacks**

Add service-level tests that lock the intended contract while preserving the repo's existing 200-candle clamp.

```python
@pytest.mark.asyncio
async def test_kis_inquire_daily_itemchartprice_rejects_non_positive_n(monkeypatch) -> None:
    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())

    with pytest.raises(ValueError, match="n must be greater than or equal to 1"):
        await client.inquire_daily_itemchartprice("005930", market="UN", n=0)


@pytest.mark.asyncio
async def test_kis_inquire_daily_itemchartprice_clamps_oversized_n(monkeypatch) -> None:
    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(return_value={"rt_cd": "0", "output2": []})
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    await client.inquire_daily_itemchartprice("005930", market="UN", n=9999)

    assert request_mock.await_count == 1
    await_args = request_mock.await_args
    assert await_args is not None
    assert await_args.kwargs["params"]["FID_INPUT_DATE_2"]
```

**Step 2: Add a failing router regression**

Add a direct router test that shows invalid `days` is rejected with `HTTPException(400)` and oversized `days` is clamped to 200 before the KIS call.

```python
@pytest.mark.asyncio
async def test_get_ohlcv_clamps_requested_days(monkeypatch) -> None:
    captured: dict[str, int] = {}

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code: str, market: str = "UN", n: int = 200, period: str = "D"):
            captured["n"] = n
            return pd.DataFrame([
                {
                    "date": pd.Timestamp("2026-03-10"),
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                }
            ])

    monkeypatch.setattr(trading, "KISClient", DummyKISClient)

    await trading.get_ohlcv(
        ticker="005930",
        days=9999,
        market_type=MarketType.KR,
        current_user=MagicMock(),
        db=AsyncMock(),
    )

    assert captured["n"] == 200
```

**Step 3: Run the targeted KIS slices to confirm RED**

Run: `uv run pytest --no-cov tests/test_services_kis_market_data.py tests/test_trading_orderbook_router.py -k "daily_itemchartprice or get_ohlcv" -q`
Expected: FAIL because no shared clamp/validation exists yet.

**Step 4: Implement a shared normalization helper**

- Reuse the existing 200-candle contract from the market-data layer (`min(count, 200)`) instead of inventing a new cap.
- Add a helper in `app/services/brokers/kis/market_data.py` that:
  - raises `ValueError` for `n < 1`
  - clamps `n` down to `200`
- Call the helper at the start of `MarketDataClient.inquire_daily_itemchartprice()`.
- Reuse the same helper or constant in `app/routers/trading.py` so the router boundary does not forward unbounded values, and map invalid `days` to `HTTPException(status_code=400, ...)` instead of leaking them into the generic 500 handler.
- Keep the external route and response shape unchanged.

**Step 5: Re-run the KIS slices**

Run: `uv run pytest --no-cov tests/test_services_kis_market_data.py tests/test_trading_orderbook_router.py -k "daily_itemchartprice or get_ohlcv" -q`
Expected: PASS.

**Step 6: Run the adjacent market-data smoke slice**

Run: `uv run pytest --no-cov tests/test_services_kis_market_data.py tests/test_trading_orderbook_router.py tests/test_market_data_service.py -q`
Expected: PASS.

**Step 7: Commit**

```bash
git add app/services/brokers/kis/constants.py app/services/brokers/kis/market_data.py app/services/brokers/kis/client.py app/routers/trading.py tests/test_services_kis_market_data.py tests/test_trading_orderbook_router.py tests/test_market_data_service.py
git commit -m "fix: bound KIS daily candle lookbacks"
```

---

### Task 3: Triage and fix only the verified runtime bug candidates

**Files:**
- Modify: `websocket_monitor.py`
- Modify: `app/analysis/prompt.py`
- Modify: `app/auth/admin_router.py` (only if direct runtime test proves a behavior bug)
- Modify: `app/mcp_server/tooling/analysis_screening.py` (only if direct runtime test proves a behavior bug)
- Test: `tests/test_websocket_monitor.py`
- Test: create `tests/test_admin_router.py` if an admin behavior bug is real
- Test: create `tests/test_analysis_prompt.py` if prompt runtime behavior changes
- Test: extend `tests/test_mcp_screen_stocks.py` if MCP validation behavior changes

**Step 1: Write failing tests for the verified bug paths, one-by-one**

Start with an explicit inspection / disposition step, then write tests only for the candidates that prove to be real runtime behavior bugs.

Inspection checklist before writing production code:

- `websocket_monitor.py`: confirm whether cancellation is actually swallowed or already re-raised.
- `app/analysis/prompt.py`: confirm whether the Sonar hit changes prompt output or is only dead/duplicated formatting structure.
- `app/auth/admin_router.py`: confirm whether any endpoint behavior is wrong, or whether the `= None` signatures are only cosmetic.
- `app/mcp_server/tooling/analysis_screening.py`: confirm whether a real user-facing validation bug exists beyond the current test coverage in `tests/test_mcp_screen_stocks.py`.

Only proceed to code changes for candidates that produce a failing behavior test first.

```python
@pytest.mark.asyncio
async def test_start_reraises_cancelled_error_after_cleanup(mock_settings: None) -> None:
    from websocket_monitor import UnifiedWebSocketMonitor

    monitor = UnifiedWebSocketMonitor(mode="upbit")
    gate = asyncio.Event()

    async def wait_forever() -> None:
        gate.set()
        await asyncio.Future()

    monitor._start_upbit = wait_forever  # type: ignore[method-assign]

    task = asyncio.create_task(monitor.start())
    await gate.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
```

For the remaining candidates, only add tests after direct inspection shows a real user-visible behavior problem. If a candidate is only a misleading smell, record that disposition in the implementation notes and do not change runtime code in pass 1.

**Step 2: Run the targeted bug slice to confirm RED**

Run: `uv run pytest --no-cov tests/test_websocket_monitor.py -k "cancelled" -q`
Expected: FAIL if cancellation is currently swallowed; PASS means the candidate is already correct and should be dispositioned, not patched.

**Step 3: Apply the minimal verified fixes**

- If the cancellation test fails, update `websocket_monitor.py` so cancellation cleanup still allows `CancelledError` to propagate.
- If prompt formatting dead code is confirmed, reduce it to a behavior-neutral cleanup with a focused regression.
- If the admin or MCP candidate does not produce a failing behavior test, do not change it in pass 1; record it as reviewed/deferred.

**Step 4: Re-run the bug slice and the neighboring suites**

Run: `uv run pytest --no-cov tests/test_websocket_monitor.py tests/test_mcp_screen_stocks.py tests/test_analysis.py -q`
Expected: PASS for touched areas, with unchanged behavior elsewhere.

**Step 5: Commit**

```bash
git add websocket_monitor.py app/analysis/prompt.py app/auth/admin_router.py app/mcp_server/tooling/analysis_screening.py tests/test_websocket_monitor.py tests/test_mcp_screen_stocks.py tests/test_analysis.py tests/test_admin_router.py
git commit -m "fix: close verified pass1 runtime bug paths"
```

Only stage the optional admin/MCP/prompt files that actually changed after a failing behavior test. If no real admin bug is proven, do not create or stage `tests/test_admin_router.py` in pass 1.

---

### Task 4: Verify the full pass 1 slice and capture reviewed dispositions

**Files:**
- Modify: `docs/plans/2026-03-10-sonar-pass1-security-correctness-implementation-plan.md`
- Modify: any touched runtime/tests from Tasks 1-3

**Step 1: Run diagnostics and the full targeted pass 1 suite**

Run: `uv run pytest --no-cov tests/test_upbit_websocket_service.py tests/test_kis_websocket.py tests/test_websocket_monitor.py tests/test_services_kis_market_data.py tests/test_trading_orderbook_router.py -q`
Expected: PASS.

**Step 2: Run lint/type checks for the touched files**

Run: `make lint`
Expected: PASS.

Run: `uv run ty check app/services/upbit_websocket.py app/services/upbit_market_websocket.py app/services/kis_websocket.py app/services/brokers/kis/market_data.py app/routers/trading.py websocket_monitor.py`
Expected: PASS.

**Step 3: Record any reviewed-but-deferred candidates**

Append a short “Reviewed / deferred in pass 1” section to this plan file if any Sonar candidates turned out to be non-runtime or already-correct behavior after test-first verification.

**Step 4: Commit**

```bash
git add docs/plans/2026-03-10-sonar-pass1-security-correctness-implementation-plan.md app/services/upbit_websocket.py app/services/upbit_market_websocket.py app/services/kis_websocket.py app/services/brokers/kis/market_data.py app/routers/trading.py websocket_monitor.py tests/test_upbit_websocket_service.py tests/test_kis_websocket.py tests/test_websocket_monitor.py tests/test_services_kis_market_data.py tests/test_trading_orderbook_router.py
git commit -m "fix: complete sonar pass1 security and correctness slice"
```

---

## Reviewed / Deferred In Pass 1

- `websocket_monitor.py` cancellation propagation was reviewed with a dedicated regression test and already re-raises `CancelledError`; no production change was needed for this pass.
- `app/services/kis_websocket.py` TLS handling was not changed in pass 1 because the current runtime path builds `ws://` URLs rather than `wss://`, so the SSL context is not active in production today.
- `app/auth/admin_router.py` `= None` dependency defaults were reviewed as mechanical/type-hint smells, not demonstrated runtime bugs; defer them to the mechanical cleanup pass.
- `app/mcp_server/tooling/analysis_screening.py` was reviewed and did not produce a failing runtime behavior beyond current coverage in `tests/test_mcp_screen_stocks.py`; defer any cleanup there until a concrete failing case exists.
