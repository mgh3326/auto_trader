# ROB-747 Minor Residuals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the three ROB-747 residuals: preserve KR index freshness metadata through secondary consumers, bound overseas inquiry token-refresh loops, and delete the unused KIS token-expiry helper.

**Architecture:** Keep the work as narrow pass-through and guard changes. The market index source already produces `quote_asof` and `data_state`; this plan wires those fields into Hermes market snapshots, `/invest` dashboard metrics, and market parity quote freshness without changing the source collector. The KIS inquiry fix reuses the existing overseas order token-refresh cap constant so read-only GET loops fail closed after one refresh attempt.

**Tech Stack:** Python 3.13, FastAPI/Pydantic v2, pytest/pytest-asyncio, React 19, TypeScript, Vitest.

## Global Constraints

- No database migration is expected.
- Keep `/invest` market dashboard and Hermes collectors read-only; do not call broker/order/watch mutation paths.
- Do not introduce new use of deprecated tick-size helper `app/mcp_server/tick_size.py:_get_tick_size`.
- Keep KIS changes scoped to `app/services/brokers/kis/overseas_orders.py` and dead-helper deletion in `app/services/brokers/kis/base.py`.
- Runtime command style is `uv run ...`; frontend checks run from `frontend/invest`.
- Preserve strict pytest markers; new tests use existing `unit` or surrounding file conventions.

---

## File Structure

- Modify `app/services/action_report/snapshot_backed/collectors/market.py`
  - Preserve index freshness metadata in the Hermes market snapshot payload.
- Modify `tests/services/action_report/snapshot_backed/test_collectors.py`
  - Add a collector regression for `quote_asof`, `data_state`, and lag metadata.
- Modify `app/schemas/invest_market_dashboard.py`
  - Add optional metric fields for index freshness metadata.
- Modify `app/services/invest_view_model/market_dashboard_service.py`
  - Map index row freshness metadata into `MarketDashboardMetric`.
  - Mark index metrics stale when `data_state == "stale"`.
  - Treat stale metrics as a partial section state.
- Modify `tests/test_invest_market_dashboard.py`
  - Add backend regression coverage for stale index metrics and response serialization.
- Modify `app/services/invest_view_model/market_parity_service.py`
  - Read `quote_asof` as the index quote timestamp and derive `ParityQuote.stale` from `data_state == "stale"`.
- Modify `tests/test_invest_market_parity_service.py`
  - Add default-provider coverage for `quote_asof` and `data_state`.
- Modify `frontend/invest/src/types/marketDashboard.ts`
  - Add optional camelCase freshness fields matching the API contract.
- Modify `frontend/invest/src/__tests__/DesktopMarketPage.test.tsx`
  - Assert stale index metrics render the existing stale chip.
- Modify `app/services/brokers/kis/overseas_orders.py`
  - Bound token-refresh `continue` loops in `inquire_overseas_orders` and `inquire_daily_order_overseas`.
- Modify `tests/test_kis_overseas_orders_retry.py`
  - Add repeated EGW tests for both read-only inquiry methods.
- Modify `app/services/brokers/kis/base.py`
  - Delete unused `_handle_token_expiry_and_retry`.

---

### Task 1: Wire Index Freshness Metadata Through Read-Only Consumers

**Files:**
- Modify: `app/services/action_report/snapshot_backed/collectors/market.py`
- Modify: `tests/services/action_report/snapshot_backed/test_collectors.py`
- Modify: `app/schemas/invest_market_dashboard.py`
- Modify: `app/services/invest_view_model/market_dashboard_service.py`
- Modify: `tests/test_invest_market_dashboard.py`
- Modify: `app/services/invest_view_model/market_parity_service.py`
- Modify: `tests/test_invest_market_parity_service.py`
- Modify: `frontend/invest/src/types/marketDashboard.ts`
- Modify: `frontend/invest/src/__tests__/DesktopMarketPage.test.tsx`

**Interfaces:**
- Consumes: index rows shaped like `{"symbol": str, "current": number, "change_pct": number, "quote_asof": str | None, "data_state": str | None, "data_state_reason": str | None, "quote_lag_seconds": int | None}`.
- Produces: Hermes payload index dicts containing existing `change_percent`, `name`, `current` plus optional `quote_asof`, `data_state`, `data_state_reason`, `quote_lag_seconds`, and `as_of`.
- Produces: `/invest/api/market` metric fields `dataState?: string | null`, `dataStateReason?: string | null`, `quoteAsOf?: datetime | null`, and `quoteLagSeconds?: int | null`.
- Produces: `DefaultMarketParityProvider.get_index_quote()` uses `quote_asof` before `as_of`/`timestamp` and sets `ParityQuote.stale` when `data_state == "stale"`.

- [ ] **Step 1: Write the failing Hermes collector test**

Add this test after `test_market_collector_kr_populates_kospi` in `tests/services/action_report/snapshot_backed/test_collectors.py`:

```python
@pytest.mark.asyncio
async def test_market_collector_preserves_index_freshness_metadata():
    async def fake_index_fn(symbols):
        return [
            {
                "symbol": "KOSPI",
                "name": "KOSPI",
                "current": 2700.0,
                "change_pct": -0.46,
                "quote_asof": "2026-07-06T09:05:00+09:00",
                "data_state": "stale",
                "data_state_reason": "kr_index_quote_lagging",
                "quote_lag_seconds": 300,
            }
        ]

    collector = MarketEventsSnapshotCollector(
        MagicMock(), query_service=_empty_events_query(), index_quote_fn=fake_index_fn
    )
    results = await collector.collect(_request(market="kr"))

    kospi = results[0].payload_json["indices"]["KOSPI"]
    assert kospi["change_percent"] == -0.46
    assert kospi["quote_asof"] == "2026-07-06T09:05:00+09:00"
    assert kospi["data_state"] == "stale"
    assert kospi["data_state_reason"] == "kr_index_quote_lagging"
    assert kospi["quote_lag_seconds"] == 300
```

- [ ] **Step 2: Run the Hermes collector test to verify it fails**

Run:

```bash
uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py::test_market_collector_preserves_index_freshness_metadata -q
```

Expected: FAIL because `_collect_indices` drops `quote_asof`, `data_state`, `data_state_reason`, and `quote_lag_seconds`.

- [ ] **Step 3: Preserve metadata in the Hermes collector**

In `app/services/action_report/snapshot_backed/collectors/market.py`, replace the current dict assignment inside `_collect_indices` with:

```python
            adapted = {
                "change_percent": change_percent,
                "name": row.get("name"),
                "current": row.get("current"),
            }
            for key in (
                "quote_asof",
                "data_state",
                "data_state_reason",
                "quote_lag_seconds",
                "as_of",
            ):
                value = row.get(key)
                if value is not None:
                    adapted[key] = value
            indices[str(symbol)] = adapted
```

Update the `_collect_indices` docstring line that says `{change_percent, name, current}` so it states that freshness metadata is passed through when present.

- [ ] **Step 4: Verify the Hermes collector test passes**

Run:

```bash
uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py::test_market_collector_preserves_index_freshness_metadata -q
```

Expected: PASS.

- [ ] **Step 5: Write the failing `/invest` dashboard backend test**

Add `from datetime import UTC, datetime` to `tests/test_invest_market_dashboard.py`, then add:

```python
class _StaleIndexProvider(_StubMarketProvider):
    async def get_indices(self) -> dict:
        return {
            "indices": [
                {
                    "symbol": "KOSPI",
                    "name": "KOSPI",
                    "current": 2875.25,
                    "change": -10.0,
                    "change_pct": -0.46,
                    "source": "naver",
                    "quote_asof": "2026-07-06T09:05:00+09:00",
                    "data_state": "stale",
                    "data_state_reason": "kr_index_quote_lagging",
                    "quote_lag_seconds": 300,
                }
            ]
        }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_market_dashboard_marks_data_state_stale_index() -> None:
    response = await build_market_dashboard(_StaleIndexProvider())

    kr_section = response.sections[0]
    metric = kr_section.metrics[0]
    assert kr_section.state == "partial"
    assert metric.stale is True
    assert metric.dataState == "stale"
    assert metric.dataStateReason == "kr_index_quote_lagging"
    assert metric.quoteLagSeconds == 300
    assert metric.quoteAsOf == datetime(2026, 7, 6, 9, 5, tzinfo=UTC).astimezone(
        metric.quoteAsOf.tzinfo
    )
```

Also extend `test_get_market_dashboard_returns_read_only_payload` after the `symbol` assertion:

```python
    assert "dataState" in body["sections"][0]["metrics"][0]
    assert "quoteAsOf" in body["sections"][0]["metrics"][0]
```

- [ ] **Step 6: Run the `/invest` dashboard backend test to verify it fails**

Run:

```bash
uv run pytest tests/test_invest_market_dashboard.py::test_build_market_dashboard_marks_data_state_stale_index -q
```

Expected: FAIL because `MarketDashboardMetric` has no freshness fields and `stale` ignores `data_state`.

- [ ] **Step 7: Add dashboard schema fields**

In `app/schemas/invest_market_dashboard.py`, add these fields to `MarketDashboardMetric` after `stale`:

```python
    dataState: str | None = None
    dataStateReason: str | None = None
    quoteAsOf: datetime | None = None
    quoteLagSeconds: int | None = None
```

- [ ] **Step 8: Map dashboard freshness fields in the service**

In `app/services/invest_view_model/market_dashboard_service.py`, add:

```python
def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
```

Add:

```python
def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
```

In `_metric_from_index`, compute freshness before constructing the model:

```python
    data_state = str(row.get("data_state")) if row.get("data_state") else None
    is_stale_state = data_state == "stale"
```

Then pass these fields into `MarketDashboardMetric`:

```python
        stale=warning is not None or row.get("current") is None or is_stale_state,
        warning=warning,
        dataState=data_state,
        dataStateReason=(
            str(row.get("data_state_reason")) if row.get("data_state_reason") else None
        ),
        quoteAsOf=_parse_datetime(row.get("quote_asof")),
        quoteLagSeconds=_as_int(row.get("quote_lag_seconds")),
```

Update `_section_state` so stale metrics make an otherwise usable section partial:

```python
    if warnings or any(m.stale for m in metrics) or len(usable) < len(metrics):
        return "partial" if usable else "error"
```

- [ ] **Step 9: Verify the `/invest` dashboard backend tests pass**

Run:

```bash
uv run pytest tests/test_invest_market_dashboard.py::test_build_market_dashboard_marks_data_state_stale_index tests/test_invest_market_dashboard.py::test_get_market_dashboard_returns_read_only_payload -q
```

Expected: PASS.

- [ ] **Step 10: Write the failing market parity provider test**

Add this test after `test_default_provider_index_quote_uses_current_only` in `tests/test_invest_market_parity_service.py`:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_provider_index_quote_uses_quote_asof_and_data_state(
    monkeypatch,
) -> None:
    import app.services.invest_view_model.market_parity_service as svc

    called = AsyncMock(
        return_value={
            "indices": [
                {
                    "symbol": "KOSPI",
                    "current": 2450.5,
                    "source": "naver",
                    "quote_asof": "2026-07-06T09:05:00+09:00",
                    "data_state": "stale",
                }
            ]
        }
    )
    monkeypatch.setattr(svc, "handle_get_market_index_current_only", called)

    quote = await svc.DefaultMarketParityProvider().get_index_quote("KOSPI")

    assert quote is not None
    assert quote.stale is True
    assert quote.as_of == datetime.fromisoformat("2026-07-06T09:05:00+09:00")
```

- [ ] **Step 11: Run the market parity provider test to verify it fails**

Run:

```bash
uv run pytest tests/test_invest_market_parity_service.py::test_default_provider_index_quote_uses_quote_asof_and_data_state -q
```

Expected: FAIL because the provider currently reads `row.get("stale")` and ignores `quote_asof`.

- [ ] **Step 12: Map market parity freshness correctly**

In `app/services/invest_view_model/market_parity_service.py`, change the `ParityQuote` construction in `DefaultMarketParityProvider.get_index_quote` to:

```python
        data_state = str(row.get("data_state")) if row.get("data_state") else None
        return ParityQuote(
            symbol=symbol,
            price=price,
            source=str(row.get("source") or "market_index"),
            as_of=_parse_datetime(
                row.get("quote_asof") or row.get("as_of") or row.get("timestamp")
            ),
            stale=data_state == "stale",
            warnings=tuple([str(row["error"])] if row.get("error") else []),
        )
```

- [ ] **Step 13: Verify the market parity provider test passes**

Run:

```bash
uv run pytest tests/test_invest_market_parity_service.py::test_default_provider_index_quote_uses_quote_asof_and_data_state -q
```

Expected: PASS.

- [ ] **Step 14: Update frontend type contract and stale chip regression**

In `frontend/invest/src/types/marketDashboard.ts`, add optional metric fields after `stale`:

```ts
  dataState?: string | null;
  dataStateReason?: string | null;
  quoteAsOf?: string | null;
  quoteLagSeconds?: number | null;
```

In `frontend/invest/src/__tests__/DesktopMarketPage.test.tsx`, update the KOSPI fixture metric in `MARKET_PAYLOAD`:

```ts
          stale: true,
          dataState: "stale",
          dataStateReason: "kr_index_quote_lagging",
          quoteAsOf: "2026-07-06T09:05:00+09:00",
          quoteLagSeconds: 300,
          warning: null,
```

Add this assertion to `renders market dashboard sections and read-only copy`:

```ts
  expect(screen.getByText("stale")).toBeInTheDocument();
```

- [ ] **Step 15: Run the frontend market dashboard test**

Run:

```bash
cd frontend/invest && npm test -- DesktopMarketPage.test.tsx
```

Expected: PASS.

- [ ] **Step 16: Run the Task 1 focused suite**

Run:

```bash
uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py::test_market_collector_preserves_index_freshness_metadata tests/test_invest_market_dashboard.py::test_build_market_dashboard_marks_data_state_stale_index tests/test_invest_market_parity_service.py::test_default_provider_index_quote_uses_quote_asof_and_data_state -q
cd frontend/invest && npm test -- DesktopMarketPage.test.tsx
```

Expected: both commands PASS.

- [ ] **Step 17: Commit Task 1**

```bash
git add app/services/action_report/snapshot_backed/collectors/market.py tests/services/action_report/snapshot_backed/test_collectors.py app/schemas/invest_market_dashboard.py app/services/invest_view_model/market_dashboard_service.py tests/test_invest_market_dashboard.py app/services/invest_view_model/market_parity_service.py tests/test_invest_market_parity_service.py frontend/invest/src/types/marketDashboard.ts frontend/invest/src/__tests__/DesktopMarketPage.test.tsx
git commit -m "fix(ROB-747): propagate index freshness metadata"
```

---

### Task 2: Bound Overseas Inquiry EGW Token Refresh Loops

**Files:**
- Modify: `app/services/brokers/kis/overseas_orders.py`
- Modify: `tests/test_kis_overseas_orders_retry.py`

**Interfaces:**
- Consumes: KIS error bodies with `msg_cd` `EGW00123` or `EGW00121`.
- Produces: exactly one token clear and one retry for each read-only inquiry call; repeated EGW raises `RuntimeError` instead of looping forever.
- Reuses: `_MAX_TOKEN_REFRESH_RESUBMITS = 1`.

- [ ] **Step 1: Extend the overseas retry test fixture**

In `tests/test_kis_overseas_orders_retry.py`, inside `_mock_overseas_orders`, add the token manager setup after `parent._ensure_token = AsyncMock()`:

```python
        token_manager = MagicMock()
        token_manager.clear_token = AsyncMock()
        parent._token_manager = token_manager
        parent._kis_url = lambda path: f"https://host{path}"
```

- [ ] **Step 2: Write failing repeated-EGW tests for both inquiry methods**

Add these tests to `TestOverseasOrdersTransientRetry`:

```python
    @pytest.mark.asyncio
    @pytest.mark.parametrize("error_code", ["EGW00123", "EGW00121"])
    async def test_pending_inquiry_token_expiry_repeated_is_bounded(
        self, _mock_overseas_orders, error_code
    ):
        instance, parent = _mock_overseas_orders
        parent._request_with_rate_limit = AsyncMock(
            return_value={
                "rt_cd": "1",
                "msg_cd": error_code,
                "msg1": "token expired",
            }
        )

        with pytest.raises(RuntimeError, match=error_code):
            await instance.inquire_overseas_orders("NASD")

        assert parent._request_with_rate_limit.call_count == 2
        assert parent._token_manager.clear_token.await_count == 1
        assert parent._ensure_token.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error_code", ["EGW00123", "EGW00121"])
    async def test_daily_inquiry_token_expiry_repeated_is_bounded(
        self, _mock_overseas_orders, error_code
    ):
        instance, parent = _mock_overseas_orders
        parent._request_with_rate_limit = AsyncMock(
            return_value={
                "rt_cd": "1",
                "msg_cd": error_code,
                "msg1": "token expired",
            }
        )

        with pytest.raises(RuntimeError, match=error_code):
            await instance.inquire_daily_order_overseas(
                start_date="20260317", end_date="20260317"
            )

        assert parent._request_with_rate_limit.call_count == 2
        assert parent._token_manager.clear_token.await_count == 1
        assert parent._ensure_token.await_count == 2
```

- [ ] **Step 3: Run the repeated-EGW tests to verify they fail**

Run:

```bash
uv run pytest tests/test_kis_overseas_orders_retry.py::TestOverseasOrdersTransientRetry::test_pending_inquiry_token_expiry_repeated_is_bounded tests/test_kis_overseas_orders_retry.py::TestOverseasOrdersTransientRetry::test_daily_inquiry_token_expiry_repeated_is_bounded -q
```

Expected: FAIL or hang before the implementation. If it hangs, stop it after confirming the loop and continue with Step 4.

- [ ] **Step 4: Add a small token-expiry predicate**

In `app/services/brokers/kis/overseas_orders.py`, below `_MAX_TOKEN_REFRESH_RESUBMITS = 1`, add:

```python
def _is_token_expiry(js: dict[str, Any]) -> bool:
    return js.get("msg_cd") in ["EGW00123", "EGW00121"]
```

- [ ] **Step 5: Bound `inquire_overseas_orders` token refresh**

In `inquire_overseas_orders`, add a counter next to the pagination state:

```python
        token_refresh_resubmits = 0
```

Replace the current EGW block with:

```python
                if _is_token_expiry(js):
                    if token_refresh_resubmits >= _MAX_TOKEN_REFRESH_RESUBMITS:
                        error_msg = f"{js.get('msg_cd')} {js.get('msg1')}"
                        logging.error(f"미체결 주문 조회 실패: {error_msg}")
                        raise RuntimeError(error_msg)
                    token_refresh_resubmits += 1
                    await self._parent._token_manager.clear_token()
                    await self._parent._ensure_token()
                    continue
```

- [ ] **Step 6: Bound `inquire_daily_order_overseas` token refresh**

In `inquire_daily_order_overseas`, add a counter next to `transient_retry_count`:

```python
        token_refresh_resubmits = 0
```

Replace the current EGW block with:

```python
                if _is_token_expiry(js):
                    if token_refresh_resubmits >= _MAX_TOKEN_REFRESH_RESUBMITS:
                        error_msg = f"{js.get('msg_cd')} {js.get('msg1')}"
                        logging.error(f"해외주식 체결조회 실패: {error_msg}")
                        raise RuntimeError(error_msg)
                    token_refresh_resubmits += 1
                    await self._parent._token_manager.clear_token()
                    await self._parent._ensure_token()
                    continue
```

- [ ] **Step 7: Verify repeated-EGW tests pass**

Run:

```bash
uv run pytest tests/test_kis_overseas_orders_retry.py::TestOverseasOrdersTransientRetry::test_pending_inquiry_token_expiry_repeated_is_bounded tests/test_kis_overseas_orders_retry.py::TestOverseasOrdersTransientRetry::test_daily_inquiry_token_expiry_repeated_is_bounded -q
```

Expected: PASS.

- [ ] **Step 8: Run surrounding overseas order tests**

Run:

```bash
uv run pytest tests/test_kis_overseas_orders_retry.py tests/test_kis_overseas_orders_mutation_guards.py tests/test_kis_overseas_pending_mock.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit Task 2**

```bash
git add app/services/brokers/kis/overseas_orders.py tests/test_kis_overseas_orders_retry.py
git commit -m "fix(ROB-747): bound overseas inquiry token refresh loops"
```

---

### Task 3: Delete Unused KIS Token-Expiry Helper

**Files:**
- Modify: `app/services/brokers/kis/base.py`

**Interfaces:**
- Removes: `KISBase._handle_token_expiry_and_retry`.
- Preserves: all existing KIS retry paths that are implemented inline in focused order and market-data modules.

- [ ] **Step 1: Confirm the helper has no call sites**

Run:

```bash
rg -n "_handle_token_expiry_and_retry" app tests
```

Expected before deletion:

```text
app/services/brokers/kis/base.py:692:    async def _handle_token_expiry_and_retry(
```

- [ ] **Step 2: Delete the helper**

Remove the entire `_handle_token_expiry_and_retry` method from `app/services/brokers/kis/base.py`.

- [ ] **Step 3: Confirm no references remain**

Run:

```bash
rg -n "_handle_token_expiry_and_retry" app tests
```

Expected: no output and exit code 1.

- [ ] **Step 4: Run a focused KIS base/order smoke**

Run:

```bash
uv run pytest tests/test_kis_base_rate_limit.py tests/test_kis_order_no_double_submit.py tests/test_kis_overseas_orders_retry.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add app/services/brokers/kis/base.py
git commit -m "chore(ROB-747): remove unused KIS token retry helper"
```

---

### Task 4: Final Verification

**Files:**
- Verify all files modified in Tasks 1-3.

**Interfaces:**
- Produces: one branch-ready diff with no migrations and no unrelated refactors.

- [ ] **Step 1: Run Python focused tests**

Run:

```bash
uv run pytest tests/services/action_report/snapshot_backed/test_collectors.py::test_market_collector_preserves_index_freshness_metadata tests/test_invest_market_dashboard.py tests/test_invest_market_parity_service.py::test_default_provider_index_quote_uses_quote_asof_and_data_state tests/test_kis_overseas_orders_retry.py tests/test_kis_overseas_orders_mutation_guards.py tests/test_kis_overseas_pending_mock.py -q
```

Expected: PASS.

- [ ] **Step 2: Run frontend focused tests**

Run:

```bash
cd frontend/invest && npm test -- DesktopMarketPage.test.tsx MarketStrip.test.tsx
```

Expected: PASS.

- [ ] **Step 3: Run lint on touched Python files**

Run:

```bash
uv run ruff check app/services/action_report/snapshot_backed/collectors/market.py app/schemas/invest_market_dashboard.py app/services/invest_view_model/market_dashboard_service.py app/services/invest_view_model/market_parity_service.py app/services/brokers/kis/overseas_orders.py app/services/brokers/kis/base.py tests/services/action_report/snapshot_backed/test_collectors.py tests/test_invest_market_dashboard.py tests/test_invest_market_parity_service.py tests/test_kis_overseas_orders_retry.py
```

Expected: PASS.

- [ ] **Step 4: Inspect diff**

Run:

```bash
git diff --stat
git diff -- app/services/action_report/snapshot_backed/collectors/market.py app/schemas/invest_market_dashboard.py app/services/invest_view_model/market_dashboard_service.py app/services/invest_view_model/market_parity_service.py app/services/brokers/kis/overseas_orders.py app/services/brokers/kis/base.py
```

Expected: only ROB-747 scoped changes, no migration files, no unrelated formatting churn.

- [ ] **Step 5: Final commit if previous task commits were not made**

If Tasks 1-3 were not committed individually, make one scoped commit:

```bash
git add app/services/action_report/snapshot_backed/collectors/market.py tests/services/action_report/snapshot_backed/test_collectors.py app/schemas/invest_market_dashboard.py app/services/invest_view_model/market_dashboard_service.py tests/test_invest_market_dashboard.py app/services/invest_view_model/market_parity_service.py tests/test_invest_market_parity_service.py frontend/invest/src/types/marketDashboard.ts frontend/invest/src/__tests__/DesktopMarketPage.test.tsx app/services/brokers/kis/overseas_orders.py tests/test_kis_overseas_orders_retry.py app/services/brokers/kis/base.py
git commit -m "fix(ROB-747): clear post-merge minor residuals"
```

---

## Self-Review

- Spec coverage: covers all three ROB-747 sections: Hermes and `/invest` index stale propagation, overseas inquiry EGW bounded retries, and dead helper deletion.
- Placeholder scan: no deferred implementation placeholders are present.
- Type consistency: source fields stay snake_case at provider boundaries and become camelCase only in `/invest` API/TypeScript contracts.
- Scope check: no DB migration, scheduled job change, broker mutation behavior change, or broad KIS refactor is included.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-06-rob-747-minor-residuals.md`. Two execution options:

**1. Subagent-Driven (recommended)** - dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** - execute tasks in this session with checkpoints after each task.
