# ROB-563 Slow Invest Traces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Reduce slow successful `/invest` Sentry traces by exposing inner provider timings and overlapping independent read-only calls without changing response schemas or trading boundaries.

**Architecture:** Keep the API/router layer unchanged and improve the read-model services in place. Add narrow Sentry spans around provider phases, then parallelize independent readers with `asyncio.gather` while preserving manual-vs-Toss filtering semantics. Market dashboard provider calls stay independently degraded but stop stacking their 6s timeout budgets sequentially.

**Tech Stack:** Python 3.13, FastAPI service layer, asyncio, sentry_sdk, Pydantic schemas, pytest, uv, Ruff.

---

## Scope Check

This plan covers one Sentry-backed performance slice across related `/invest` read-model endpoints:

- `/invest/api/home`
- `/invest/api/account-panel`
- `/invest/api/market`

All changes are read-only orchestration or observability changes. This plan does not introduce account-balance caching, stale read models, order execution changes, live order approval changes, schema changes, or a special optimization for `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=false`. The flag is expected to stay true and current live mutation behavior must remain intact.

Linear: `ROB-563`

## Execution Status

Implemented on branch `rob-sentry` in these commits:

- `7d332447 test: capture invest home reader concurrency`
- `0fc44555 perf: parallelize invest home readers`
- `7740d4c1 chore: add invest reader phase spans`
- `33aec865 chore: add toss portfolio phase spans`
- `56a5a409 perf: parallelize invest market dashboard providers`

Final verification completed on 2026-06-15 KST:

- `uv run pytest tests/test_invest_home_service.py tests/test_invest_home_readers.py tests/test_invest_market_dashboard.py -q` -> `68 passed, 2 warnings`
- `uv run ruff check app/services/invest_home_service.py app/services/invest_home_readers.py app/services/toss_portfolio_service.py app/services/invest_view_model/market_dashboard_service.py tests/test_invest_home_service.py tests/test_invest_home_readers.py tests/test_invest_market_dashboard.py` -> passed
- `uv run ruff format --check app/services/invest_home_service.py app/services/invest_home_readers.py app/services/toss_portfolio_service.py app/services/invest_view_model/market_dashboard_service.py tests/test_invest_home_service.py tests/test_invest_home_readers.py tests/test_invest_market_dashboard.py` -> passed
- `make test-unit` -> `11782 passed, 10 skipped, 384 deselected, 49 warnings`

## File Structure

- Modify `app/services/invest_home_service.py`
  - Add a shared `_fetch_reader_result` helper that wraps reader calls with the existing Sentry span tags and exception-to-warning behavior.
  - Run KIS, Upbit, and Toss API readers concurrently for both `get_home()` and `build_account_panel_view()`.
  - Keep manual holdings after Toss API so `_filter_manual_holdings_for_toss_api()` continues to remove Toss manual duplicates.
  - Leave paper reader behavior sequential and unchanged.

- Modify `app/services/invest_home_readers.py`
  - Add inner Sentry spans for KIS phases: domestic balance, integrated margin, overseas balance, FX, and overseas margin fallback.
  - Add inner Sentry spans for Toss API reader snapshot and FX conversion.
  - Add inner Sentry spans for manual holdings load, KR quote fetch, US quote fetch, and FX conversion.

- Modify `app/services/toss_portfolio_service.py`
  - Add inner Sentry spans for Toss holdings, sellable quantity fanout, and buying power fanout.
  - Preserve current sellable quantity behavior; do not gate it off for `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=false`.

- Modify `app/services/invest_view_model/market_dashboard_service.py`
  - Wrap `_capture()` calls in Sentry spans.
  - Execute market index, fear/greed, and kimchi premium captures concurrently.
  - Preserve current per-provider timeout and partial degradation semantics.

- Modify `tests/test_invest_home_service.py`
  - Add concurrency tests for `get_home()` and `build_account_panel_view()`.
  - Keep existing Toss API manual-filter fallback tests as contract coverage.

- Modify `tests/test_invest_home_readers.py`
  - Add span emission tests for KIS, manual, and Toss API reader internals.
  - Keep mutation-enabled Toss behavior tests unchanged.

- Modify `tests/test_invest_market_dashboard.py`
  - Add provider concurrency test.
  - Add provider span emission test.

---

### Task 1: Add Failing Reader Concurrency Tests

**Files:**
- Modify: `tests/test_invest_home_service.py`
- Test: `tests/test_invest_home_service.py`

- [x] **Step 1: Add asyncio import**

Modify the import block at the top of `tests/test_invest_home_service.py`:

```python
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
```

- [x] **Step 2: Add get_home concurrency test**

Append this test after `test_get_home_default_skips_paper_spans` and before the `ROB-532 Toss API fallback & preference tests` section:

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_home_runs_primary_readers_concurrently() -> None:
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    active = 0
    peak_active = 0

    class _ConcurrentReader:
        async def fetch(self, *, user_id: int) -> _SourceFetchResult:
            nonlocal active, peak_active
            assert user_id == 1
            active += 1
            peak_active = max(peak_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return _SourceFetchResult(accounts=[], holdings=[])

    class _ManualReader:
        async def fetch(self, *, user_id: int) -> _SourceFetchResult:
            assert user_id == 1
            return _SourceFetchResult(accounts=[], holdings=[])

    service = InvestHomeService(
        kis_reader=_ConcurrentReader(),
        upbit_reader=_ConcurrentReader(),
        manual_reader=_ManualReader(),
        toss_api_reader=_ConcurrentReader(),
    )

    await service.get_home(user_id=1)

    assert peak_active == 3
```

- [x] **Step 3: Run the new get_home test and verify it fails**

Run:

```bash
uv run pytest tests/test_invest_home_service.py::test_get_home_runs_primary_readers_concurrently -q
```

Expected: `FAIL` with `assert 1 == 3`.

- [x] **Step 4: Add account-panel concurrency test**

Append this test immediately after `test_get_home_runs_primary_readers_concurrently`:

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_account_panel_view_runs_primary_readers_concurrently() -> None:
    from app.services.invest_home_service import InvestHomeService, _SourceFetchResult

    active = 0
    peak_active = 0

    class _ConcurrentReader:
        async def fetch(self, *, user_id: int) -> _SourceFetchResult:
            nonlocal active, peak_active
            assert user_id == 1
            active += 1
            peak_active = max(peak_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return _SourceFetchResult(accounts=[], holdings=[])

    class _ManualReader:
        async def fetch(self, *, user_id: int) -> _SourceFetchResult:
            assert user_id == 1
            return _SourceFetchResult(accounts=[], holdings=[])

    service = InvestHomeService(
        kis_reader=_ConcurrentReader(),
        upbit_reader=_ConcurrentReader(),
        manual_reader=_ManualReader(),
        toss_api_reader=_ConcurrentReader(),
    )

    await service.build_account_panel_view(user_id=1)

    assert peak_active == 3
```

- [x] **Step 5: Run both new tests and verify they fail**

Run:

```bash
uv run pytest \
  tests/test_invest_home_service.py::test_get_home_runs_primary_readers_concurrently \
  tests/test_invest_home_service.py::test_account_panel_view_runs_primary_readers_concurrently \
  -q
```

Expected: both tests fail with `assert 1 == 3`.

- [x] **Step 6: Commit failing tests**

```bash
git add tests/test_invest_home_service.py
git commit -m "test: capture invest home reader concurrency"
```

---

### Task 2: Parallelize Primary Home Readers

**Files:**
- Modify: `app/services/invest_home_service.py`
- Test: `tests/test_invest_home_service.py`

- [x] **Step 1: Add asyncio and callable imports**

Modify the imports in `app/services/invest_home_service.py`:

```python
import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
```

- [x] **Step 2: Add shared reader fetch helper**

Insert this helper immediately after `_AccountPanelView`:

```python
async def _fetch_reader_result(
    fetcher: Callable[..., Awaitable[_SourceFetchResult]],
    *,
    span_name: str,
    source: str,
    user_id: int,
    include_paper: bool,
    paper_sources: frozenset[str] | None,
) -> _SourceFetchResult:
    with sentry_sdk.start_span(op="invest.home.reader", name=span_name) as span:
        span.set_tag("source", source)
        span.set_tag("include_paper", include_paper)
        if paper_sources is not None:
            span.set_tag("paper_sources", ",".join(sorted(paper_sources)))
        try:
            return await fetcher(user_id=user_id)
        except Exception as exc:
            logger.warning(
                "[invest_home] %s fetch failed: %s",
                source,
                exc,
                exc_info=True,
            )
            return _SourceFetchResult(
                accounts=[],
                holdings=[],
                warning=InvestHomeWarning(
                    source=source,
                    message=str(exc) or type(exc).__name__,
                ),
            )
```

- [x] **Step 3: Replace get_home live/Toss/manual section**

In `InvestHomeService.get_home()`, replace the current KIS/Upbit loop, Toss API block, and manual block with this code. Keep the existing `if include_paper:` block immediately after it unchanged.

```python
        live_sources = ["kis", "upbit"]
        live_tasks = [
            _fetch_reader_result(
                self._kis.fetch,
                span_name="invest.home.kis",
                source="kis",
                user_id=user_id,
                include_paper=include_paper,
                paper_sources=paper_sources,
            ),
            _fetch_reader_result(
                self._upbit.fetch,
                span_name="invest.home.upbit",
                source="upbit",
                user_id=user_id,
                include_paper=include_paper,
                paper_sources=paper_sources,
            ),
        ]
        if self._toss_api is not None:
            live_sources.append("toss_api")
            live_tasks.append(
                _fetch_reader_result(
                    self._toss_api.fetch,
                    span_name="invest.home.toss_api",
                    source="toss_api",
                    user_id=user_id,
                    include_paper=include_paper,
                    paper_sources=paper_sources,
                )
            )

        live_results = await asyncio.gather(*live_tasks)
        toss_api_holdings: list[Holding] = []

        for source, result in zip(live_sources, live_results, strict=True):
            if result.warning is not None:
                warnings.append(result.warning)

            if source == "toss_api":
                if result.holdings or result.accounts:
                    accounts.extend(result.accounts)
                    holdings.extend(result.holdings)
                    toss_api_holdings = list(result.holdings)
                continue

            accounts.extend(result.accounts)
            holdings.extend(result.holdings)
            hidden_holdings.extend(result.hidden_holdings)
            hidden_counts.upbitInactive += result.hidden_counts.upbitInactive
            hidden_counts.upbitDust += result.hidden_counts.upbitDust

        manual_result = await _fetch_reader_result(
            self._manual.fetch,
            span_name="invest.home.manual",
            source="toss_manual",
            user_id=user_id,
            include_paper=include_paper,
            paper_sources=paper_sources,
        )
        manual_holdings = _filter_manual_holdings_for_toss_api(
            manual_result.holdings, toss_api_holdings
        )
        accounts.extend(manual_result.accounts)
        holdings.extend(manual_holdings)
        if manual_result.warning is not None:
            warnings.append(manual_result.warning)
        toss_account = build_manual_account_from_holdings(manual_holdings)
        if toss_account is not None:
            accounts.append(toss_account)
```

- [x] **Step 4: Replace account-panel live/Toss/manual section**

Inside `InvestHomeService.build_account_panel_view()`, replace the current KIS/Upbit loop, Toss API block, and manual block inside the outer `invest.account_panel.build` span with this code. Keep the existing `if include_paper:` block immediately after it unchanged.

```python
            live_sources = ["kis", "upbit"]
            live_tasks = [
                _fetch_reader_result(
                    self._kis.fetch,
                    span_name="invest.home.kis",
                    source="kis",
                    user_id=user_id,
                    include_paper=include_paper,
                    paper_sources=paper_sources,
                ),
                _fetch_reader_result(
                    self._upbit.fetch,
                    span_name="invest.home.upbit",
                    source="upbit",
                    user_id=user_id,
                    include_paper=include_paper,
                    paper_sources=paper_sources,
                ),
            ]
            if self._toss_api is not None:
                live_sources.append("toss_api")
                live_tasks.append(
                    _fetch_reader_result(
                        self._toss_api.fetch,
                        span_name="invest.home.toss_api",
                        source="toss_api",
                        user_id=user_id,
                        include_paper=include_paper,
                        paper_sources=paper_sources,
                    )
                )

            live_results = await asyncio.gather(*live_tasks)
            toss_api_holdings: list[Holding] = []

            for source, result in zip(live_sources, live_results, strict=True):
                if result.warning is not None:
                    warnings.append(result.warning)

                if source == "toss_api":
                    if result.holdings or result.accounts:
                        accounts.extend(result.accounts)
                        holdings.extend(result.holdings)
                        toss_api_holdings = list(result.holdings)
                    continue

                accounts.extend(result.accounts)
                holdings.extend(result.holdings)

            manual_result = await _fetch_reader_result(
                self._manual.fetch,
                span_name="invest.home.manual",
                source="toss_manual",
                user_id=user_id,
                include_paper=include_paper,
                paper_sources=paper_sources,
            )
            manual_holdings = _filter_manual_holdings_for_toss_api(
                manual_result.holdings, toss_api_holdings
            )
            accounts.extend(manual_result.accounts)
            holdings.extend(manual_holdings)
            if manual_result.warning is not None:
                warnings.append(manual_result.warning)
            toss_account = build_manual_account_from_holdings(manual_holdings)
            if toss_account is not None:
                accounts.append(toss_account)
```

- [x] **Step 5: Run targeted home service tests**

Run:

```bash
uv run pytest \
  tests/test_invest_home_service.py::test_get_home_runs_primary_readers_concurrently \
  tests/test_invest_home_service.py::test_account_panel_view_runs_primary_readers_concurrently \
  tests/test_invest_home_service.py::test_get_home_creates_reader_spans \
  tests/test_invest_home_service.py::test_get_home_uses_toss_api_instead_of_manual_when_toss_api_has_holdings \
  tests/test_invest_home_service.py::test_get_home_falls_back_to_manual_when_toss_api_returns_warning_only \
  tests/test_invest_home_service.py::test_paper_reader_exception_does_not_break_account_panel_view \
  -q
```

Expected: all selected tests pass.

- [x] **Step 6: Run the full home service file**

Run:

```bash
uv run pytest tests/test_invest_home_service.py -q
```

Expected: pass.

- [x] **Step 7: Commit home reader parallelization**

```bash
git add app/services/invest_home_service.py tests/test_invest_home_service.py
git commit -m "perf: parallelize invest home readers"
```

---

### Task 3: Add KIS and Manual Reader Phase Spans

**Files:**
- Modify: `app/services/invest_home_readers.py`
- Modify: `tests/test_invest_home_readers.py`
- Test: `tests/test_invest_home_readers.py`

- [x] **Step 1: Add KIS phase span test**

Append this test after `test_kis_reader_excludes_cash_from_value_and_converts_usd`:

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_reader_emits_provider_phase_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[tuple[str, str, dict[str, Any]]] = []

    class _Span:
        def __init__(self) -> None:
            self.data: dict[str, Any] = {}

        def set_data(self, key: str, value: Any) -> None:
            self.data[key] = value

        def set_tag(self, key: str, value: Any) -> None:
            self.data[key] = value

    class _SpanContext:
        def __init__(self, op: str, name: str) -> None:
            self.op = op
            self.name = name
            self.span = _Span()

        def __enter__(self) -> _Span:
            started.append((self.op, self.name, self.span.data))
            return self.span

        def __exit__(self, *exc: object) -> bool:
            return False

    def _start_span(*, op: str, name: str, **kwargs: Any) -> _SpanContext:
        return _SpanContext(op, name)

    monkeypatch.setattr(readers.sentry_sdk, "start_span", _start_span)
    monkeypatch.setattr(readers, "SafeKISClient", _FakeKISClient)

    async def _fx() -> float:
        return 1_300.0

    monkeypatch.setattr(readers, "get_usd_krw_rate", _fx)

    await readers.KISHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    names = [name for _, name, _ in started]
    assert "invest.home.kis.domestic_balance" in names
    assert "invest.home.kis.integrated_margin" in names
    assert "invest.home.kis.overseas_balance" in names
    assert "invest.home.kis.fx" in names
```

- [x] **Step 2: Run KIS phase span test and verify it fails**

Run:

```bash
uv run pytest tests/test_invest_home_readers.py::test_kis_reader_emits_provider_phase_spans -q
```

Expected: `FAIL` because the phase spans do not exist.

- [x] **Step 3: Instrument KISHomeReader.fetch**

In `KISHomeReader.fetch()`, replace the provider call section from `# 1. Domestic` through `usd_krw_rate = await get_usd_krw_rate()` with this code:

```python
            with sentry_sdk.start_span(
                op="invest.home.kis.phase",
                name="invest.home.kis.domestic_balance",
            ) as span:
                stocks_kr = await self._client.account.fetch_my_stocks(
                    is_overseas=False
                )
                span.set_data("holding_count", len(stocks_kr))

            with sentry_sdk.start_span(
                op="invest.home.kis.phase",
                name="invest.home.kis.integrated_margin",
            ) as span:
                margin = await self._client.account.inquire_integrated_margin()
                span.set_data("field_count", len(margin))

            domestic_cash = extract_domestic_cash_summary_from_integrated_margin(margin)

            with sentry_sdk.start_span(
                op="invest.home.kis.phase",
                name="invest.home.kis.overseas_balance",
            ) as span:
                stocks_us = await self._client.account.fetch_my_overseas_stocks(
                    exchange_code="NASD"
                )
                span.set_data("holding_count", len(stocks_us))
                span.set_tag("exchange_code", "NASD")

            fx_warning: InvestHomeWarning | None = None
            usd_krw_rate: float | None = None
            try:
                with sentry_sdk.start_span(
                    op="invest.home.kis.phase",
                    name="invest.home.kis.fx",
                ) as span:
                    usd_krw_rate = await get_usd_krw_rate()
                    span.set_tag("success", True)
            except Exception as exc:
                logger.warning("USD/KRW FX fetch failed: %s", exc, exc_info=True)
                fx_warning = InvestHomeWarning(
                    source="kis",
                    message="USD 보유 평가금액 환산을 위한 환율 조회에 실패했습니다.",
                )
```

Then replace the overseas margin fallback call with this span-wrapped version:

```python
                try:
                    with sentry_sdk.start_span(
                        op="invest.home.kis.phase",
                        name="invest.home.kis.overseas_margin_fallback",
                    ) as span:
                        overseas_margin = (
                            await self._client.account.inquire_overseas_margin()
                        )
                        span.set_data("row_count", len(overseas_margin))
                    us_margin = next(
                        (
                            m
                            for m in overseas_margin
                            if m.get("crcy_cd") == "USD"
                            and m.get("natn_name") in ["미국", "US", "USA"]
                        ),
                        None,
                    )
                    if us_margin:
                        usd_balance = us_margin.get("frcr_dncl_amt1")
                        usd_buying_power = us_margin.get("frcr_gnrl_ord_psbl_amt")
                except Exception as exc:
                    logger.warning("KIS overseas margin fallback failed: %s", exc)
```

- [x] **Step 4: Add manual reader phase span test**

Append this test near the existing `ManualHomeReader` tests:

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_emits_load_quote_and_fx_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[str] = []

    class _Span:
        def set_data(self, key: str, value: Any) -> None:
            return None

        def set_tag(self, key: str, value: Any) -> None:
            return None

    class _SpanContext:
        def __init__(self, name: str) -> None:
            self.name = name

        def __enter__(self) -> _Span:
            started.append(self.name)
            return _Span()

        def __exit__(self, *exc: object) -> bool:
            return False

    def _start_span(*, op: str, name: str, **kwargs: Any) -> _SpanContext:
        return _SpanContext(name)

    class _BrokerAccount:
        broker_type = "toss"

    class _ManualHolding:
        id = 1
        broker_account_id = 10
        broker_account = _BrokerAccount()
        ticker = "005930"
        display_name = "삼성전자"
        market_type = MarketType.KR
        quantity = 2
        avg_price = 70_000

    class _ManualHoldingsService:
        def __init__(self, db: object) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[_ManualHolding]:
            assert user_id == 1
            return [_ManualHolding()]

    class _QuoteService:
        async def fetch_kr_prices(self, tickers: list[str]) -> dict[str, float | None]:
            assert tickers == ["005930"]
            return {"005930": 72_000.0}

        async def fetch_us_prices(self, tickers: list[str]) -> dict[str, float | None]:
            assert tickers == []
            return {}

    monkeypatch.setattr(readers.sentry_sdk, "start_span", _start_span)
    monkeypatch.setattr(readers, "ManualHoldingsService", _ManualHoldingsService)

    result = await readers.ManualHomeReader(
        db=object(), quote_service=_QuoteService()
    ).fetch(user_id=1)  # type: ignore[arg-type]

    assert result.warning is None
    assert "invest.home.manual.load_holdings" in started
    assert "invest.home.manual.fetch_kr_prices" in started
    assert "invest.home.manual.fetch_us_prices" in started
```

- [x] **Step 5: Run manual span test and verify it fails**

Run:

```bash
uv run pytest tests/test_invest_home_readers.py::test_manual_reader_emits_load_quote_and_fx_spans -q
```

Expected: `FAIL` because the manual phase spans do not exist.

- [x] **Step 6: Instrument ManualHomeReader.fetch**

In `ManualHomeReader.fetch()`, replace the raw holdings and quote-fetch block with this code:

```python
            with sentry_sdk.start_span(
                op="invest.home.manual.phase",
                name="invest.home.manual.load_holdings",
            ) as span:
                raw_holdings = await self._service.get_holdings_by_user(user_id)
                span.set_data("raw_holding_count", len(raw_holdings))

            toss_holdings = [
                h
                for h in raw_holdings
                if str(getattr(h.broker_account, "broker_type", "")).lower() == "toss"
            ]

            kr_tickers = [
                h.ticker for h in toss_holdings if h.market_type == MarketType.KR
            ]
            us_tickers = [
                h.ticker for h in toss_holdings if h.market_type == MarketType.US
            ]

            kr_prices: dict[str, float | None] = {}
            us_prices: dict[str, float | None] = {}
            usd_krw_rate: float | None = None

            if self._quote_service:
                with sentry_sdk.start_span(
                    op="invest.home.manual.phase",
                    name="invest.home.manual.fetch_kr_prices",
                ) as span:
                    span.set_data("ticker_count", len(kr_tickers))
                    kr_prices = await self._quote_service.fetch_kr_prices(kr_tickers)
                    span.set_data("price_count", len(kr_prices))

                with sentry_sdk.start_span(
                    op="invest.home.manual.phase",
                    name="invest.home.manual.fetch_us_prices",
                ) as span:
                    span.set_data("ticker_count", len(us_tickers))
                    us_prices = await self._quote_service.fetch_us_prices(us_tickers)
                    span.set_data("price_count", len(us_prices))

                if us_tickers:
                    try:
                        with sentry_sdk.start_span(
                            op="invest.home.manual.phase",
                            name="invest.home.manual.fx",
                        ) as span:
                            usd_krw_rate = await get_usd_krw_rate()
                            span.set_tag("success", True)
                    except Exception:
                        logger.warning("FX fetch failed for ManualHomeReader")
```

- [x] **Step 7: Run KIS and manual span tests**

Run:

```bash
uv run pytest \
  tests/test_invest_home_readers.py::test_kis_reader_emits_provider_phase_spans \
  tests/test_invest_home_readers.py::test_manual_reader_emits_load_quote_and_fx_spans \
  -q
```

Expected: pass.

- [x] **Step 8: Commit KIS/manual observability**

```bash
git add app/services/invest_home_readers.py tests/test_invest_home_readers.py
git commit -m "chore: add invest reader phase spans"
```

---

### Task 4: Add Toss Portfolio Phase Spans

**Files:**
- Modify: `app/services/toss_portfolio_service.py`
- Modify: `app/services/invest_home_readers.py`
- Modify: `tests/test_invest_home_readers.py`
- Test: `tests/test_invest_home_readers.py`

- [x] **Step 1: Add Toss portfolio span test**

Append this test near the Toss API reader tests in `tests/test_invest_home_readers.py`:

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_toss_portfolio_snapshot_emits_phase_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal

    from app.services import toss_portfolio_service as toss_service

    started: list[str] = []

    class _Span:
        def set_data(self, key: str, value: Any) -> None:
            return None

        def set_tag(self, key: str, value: Any) -> None:
            return None

    class _SpanContext:
        def __init__(self, name: str) -> None:
            self.name = name

        def __enter__(self) -> _Span:
            started.append(self.name)
            return _Span()

        def __exit__(self, *exc: object) -> bool:
            return False

    def _start_span(*, op: str, name: str, **kwargs: Any) -> _SpanContext:
        return _SpanContext(name)

    class _Client:
        async def holdings(self) -> SimpleNamespace:
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        symbol="005930",
                        name="삼성전자",
                        market_country="KR",
                        quantity=Decimal("2"),
                        average_purchase_price=Decimal("70000"),
                        last_price=Decimal("72000"),
                        market_value={"amount": Decimal("144000")},
                        profit_loss={"amount": Decimal("4000"), "rate": Decimal("0.0285")},
                    )
                ]
            )

        async def sellable_quantity(self, *, symbol: str) -> SimpleNamespace:
            assert symbol == "005930"
            return SimpleNamespace(sellable_quantity=Decimal("1"))

        async def buying_power(self, *, currency: str) -> SimpleNamespace:
            return SimpleNamespace(currency=currency, cash_buying_power=Decimal("1000"))

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(toss_service.sentry_sdk, "start_span", _start_span)

    snapshot = await toss_service.fetch_toss_portfolio_snapshot(client=_Client())

    assert snapshot.positions[0].symbol == "005930"
    assert "invest.home.toss_api.holdings" in started
    assert "invest.home.toss_api.sellable_quantity" in started
    assert "invest.home.toss_api.buying_power" in started
```

- [x] **Step 2: Run Toss portfolio span test and verify it fails**

Run:

```bash
uv run pytest tests/test_invest_home_readers.py::test_toss_portfolio_snapshot_emits_phase_spans -q
```

Expected: `FAIL` because `toss_portfolio_service` has no `sentry_sdk` import and no phase spans.

- [x] **Step 3: Import sentry_sdk in toss_portfolio_service**

Modify `app/services/toss_portfolio_service.py` imports:

```python
import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import sentry_sdk
```

- [x] **Step 4: Instrument fetch_toss_cash_snapshot**

Replace the `buying_power_results = await asyncio.gather(...)` block in `fetch_toss_cash_snapshot()` with:

```python
        with sentry_sdk.start_span(
            op="invest.home.toss_api.phase",
            name="invest.home.toss_api.buying_power",
        ) as span:
            span.set_data("currency_count", 2)
            buying_power_results = await asyncio.gather(
                active_client.buying_power(currency="KRW"),
                active_client.buying_power(currency="USD"),
                return_exceptions=True,
            )
            span.set_data(
                "error_count",
                sum(
                    1
                    for result in buying_power_results
                    if isinstance(result, BaseException)
                ),
            )
```

- [x] **Step 5: Instrument fetch_toss_portfolio_snapshot**

Replace the holdings and sellable fanout calls in `fetch_toss_portfolio_snapshot()` with:

```python
        with sentry_sdk.start_span(
            op="invest.home.toss_api.phase",
            name="invest.home.toss_api.holdings",
        ) as span:
            holdings = await active_client.holdings()
            span.set_data("position_count", len(holdings.items))

        errors: list[dict[str, Any]] = []

        with sentry_sdk.start_span(
            op="invest.home.toss_api.phase",
            name="invest.home.toss_api.sellable_quantity",
        ) as span:
            span.set_data("position_count", len(holdings.items))
            sellable_results = await asyncio.gather(
                *[
                    active_client.sellable_quantity(symbol=item.symbol)
                    for item in holdings.items
                ],
                return_exceptions=True,
            )
            span.set_data(
                "error_count",
                sum(
                    1
                    for result in sellable_results
                    if isinstance(result, BaseException)
                ),
            )
```

- [x] **Step 6: Add TossApiHomeReader snapshot and FX spans**

In `TossApiHomeReader.fetch()`, replace the snapshot fetch and USD FX block with:

```python
            with sentry_sdk.start_span(
                op="invest.home.toss_api.phase",
                name="invest.home.toss_api.snapshot",
            ) as span:
                snapshot = await fetch_toss_portfolio_snapshot()
                span.set_data("position_count", len(snapshot.positions))
                span.set_data("error_count", len(snapshot.errors))
```

Then replace the `usd_krw_rate = await get_usd_krw_rate()` call with:

```python
                    with sentry_sdk.start_span(
                        op="invest.home.toss_api.phase",
                        name="invest.home.toss_api.fx",
                    ) as span:
                        usd_krw_rate = await get_usd_krw_rate()
                        span.set_tag("success", True)
```

- [x] **Step 7: Run Toss-focused tests**

Run:

```bash
uv run pytest \
  tests/test_invest_home_readers.py::test_toss_portfolio_snapshot_emits_phase_spans \
  tests/test_invest_home_readers.py::test_toss_api_home_reader_maps_read_only_holdings_and_cash \
  tests/test_invest_home_readers.py::test_toss_api_home_reader_tradeable_when_mutations_enabled \
  tests/test_invest_home_readers.py::test_toss_api_home_reader_converts_us_holdings_to_krw \
  -q
```

Expected: pass.

- [x] **Step 8: Commit Toss observability**

```bash
git add app/services/toss_portfolio_service.py app/services/invest_home_readers.py tests/test_invest_home_readers.py
git commit -m "chore: add toss portfolio phase spans"
```

---

### Task 5: Parallelize and Instrument Market Dashboard Providers

**Files:**
- Modify: `app/services/invest_view_model/market_dashboard_service.py`
- Modify: `tests/test_invest_market_dashboard.py`
- Test: `tests/test_invest_market_dashboard.py`

- [x] **Step 1: Add asyncio import to market dashboard tests**

Modify `tests/test_invest_market_dashboard.py` imports:

```python
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
```

- [x] **Step 2: Add provider concurrency test**

Append this test after `test_build_market_dashboard_degrades_to_partial_on_provider_error`:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_market_dashboard_captures_providers_concurrently() -> None:
    class _ConcurrentProvider(_StubMarketProvider):
        def __init__(self) -> None:
            self.active = 0
            self.peak_active = 0

        async def _enter(self) -> None:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1

        async def get_indices(self) -> dict:
            await self._enter()
            return await super().get_indices()

        async def get_fear_greed(self) -> dict:
            await self._enter()
            return await super().get_fear_greed()

        async def get_kimchi_premium(self) -> dict:
            await self._enter()
            return await super().get_kimchi_premium()

    provider = _ConcurrentProvider()

    response = await build_market_dashboard(provider)

    assert response.state == "fresh"
    assert provider.peak_active == 3
```

- [x] **Step 3: Run provider concurrency test and verify it fails**

Run:

```bash
uv run pytest tests/test_invest_market_dashboard.py::test_build_market_dashboard_captures_providers_concurrently -q
```

Expected: `FAIL` with `assert 1 == 3`.

- [x] **Step 4: Add market provider span test**

Append this test after the provider concurrency test:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_market_dashboard_emits_provider_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.invest_view_model import market_dashboard_service as service

    started: list[tuple[str, str]] = []

    class _Span:
        def set_data(self, key: str, value: object) -> None:
            return None

        def set_tag(self, key: str, value: object) -> None:
            return None

    class _SpanContext:
        def __init__(self, op: str, name: str) -> None:
            self.op = op
            self.name = name

        def __enter__(self) -> _Span:
            started.append((self.op, self.name))
            return _Span()

        def __exit__(self, *exc: object) -> bool:
            return False

    def _start_span(*, op: str, name: str, **kwargs: object) -> _SpanContext:
        return _SpanContext(op, name)

    monkeypatch.setattr(service.sentry_sdk, "start_span", _start_span)

    await build_market_dashboard(_StubMarketProvider())

    assert ("invest.market.provider", "invest.market.market_index") in started
    assert ("invest.market.provider", "invest.market.fear_greed") in started
    assert ("invest.market.provider", "invest.market.kimchi_premium") in started
```

- [x] **Step 5: Run provider span test and verify it fails**

Run:

```bash
uv run pytest tests/test_invest_market_dashboard.py::test_build_market_dashboard_emits_provider_spans -q
```

Expected: `FAIL` because `market_dashboard_service` has no `sentry_sdk` import and `_capture()` has no span.

- [x] **Step 6: Import sentry_sdk in market dashboard service**

Modify imports in `app/services/invest_view_model/market_dashboard_service.py`:

```python
import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import sentry_sdk
```

- [x] **Step 7: Wrap _capture with provider span**

Replace `_capture()` with:

```python
async def _capture(
    label: str, call: Callable[[], Awaitable[Any]]
) -> tuple[Any | None, str | None]:
    try:
        with sentry_sdk.start_span(
            op="invest.market.provider",
            name=f"invest.market.{label}",
        ) as span:
            span.set_tag("provider", label)
            result = await asyncio.wait_for(call(), timeout=6)
            if isinstance(result, dict):
                span.set_data("payload_keys", sorted(str(key) for key in result.keys()))
            elif isinstance(result, list):
                span.set_data("payload_length", len(result))
            return result, None
    except Exception as exc:  # provider failures should not break /invest shell
        return None, f"{label}: {exc}"
```

- [x] **Step 8: Parallelize build_market_dashboard captures**

Replace the three sequential `_capture()` awaits in `build_market_dashboard()` with:

```python
    (
        (indices, index_warning),
        (fear_greed, fear_greed_warning),
        (kimchi, kimchi_warning),
    ) = await asyncio.gather(
        _capture("market_index", provider.get_indices),
        _capture("fear_greed", provider.get_fear_greed),
        _capture("kimchi_premium", provider.get_kimchi_premium),
    )
```

- [x] **Step 9: Run market dashboard tests**

Run:

```bash
uv run pytest tests/test_invest_market_dashboard.py -q
```

Expected: pass.

- [x] **Step 10: Commit market dashboard optimization**

```bash
git add app/services/invest_view_model/market_dashboard_service.py tests/test_invest_market_dashboard.py
git commit -m "perf: parallelize invest market dashboard providers"
```

---

### Task 6: Final Verification and ROB-563 Update

**Files:**
- Verify: `app/services/invest_home_service.py`
- Verify: `app/services/invest_home_readers.py`
- Verify: `app/services/toss_portfolio_service.py`
- Verify: `app/services/invest_view_model/market_dashboard_service.py`
- Verify: `tests/test_invest_home_service.py`
- Verify: `tests/test_invest_home_readers.py`
- Verify: `tests/test_invest_market_dashboard.py`

- [x] **Step 1: Run focused unit tests**

Run:

```bash
uv run pytest \
  tests/test_invest_home_service.py \
  tests/test_invest_home_readers.py \
  tests/test_invest_market_dashboard.py \
  -q
```

Expected: pass.

- [x] **Step 2: Run lint on changed files**

Run:

```bash
uv run ruff check \
  app/services/invest_home_service.py \
  app/services/invest_home_readers.py \
  app/services/toss_portfolio_service.py \
  app/services/invest_view_model/market_dashboard_service.py \
  tests/test_invest_home_service.py \
  tests/test_invest_home_readers.py \
  tests/test_invest_market_dashboard.py
```

Expected: pass with `All checks passed!`.

- [x] **Step 3: Run formatter check on changed files**

Run:

```bash
uv run ruff format --check \
  app/services/invest_home_service.py \
  app/services/invest_home_readers.py \
  app/services/toss_portfolio_service.py \
  app/services/invest_view_model/market_dashboard_service.py \
  tests/test_invest_home_service.py \
  tests/test_invest_home_readers.py \
  tests/test_invest_market_dashboard.py
```

Expected: pass with unchanged formatting.

- [x] **Step 4: Run the project unit gate when focused tests pass**

Run:

```bash
make test-unit
```

Expected: pass.

- [x] **Step 5: Inspect git diff for forbidden boundaries**

Run:

```bash
git diff -- app/services/invest_home_service.py app/services/invest_home_readers.py app/services/toss_portfolio_service.py app/services/invest_view_model/market_dashboard_service.py
```

Expected:

- No router behavior changes.
- No schema changes.
- No DB write/backfill/update/delete path.
- No broker order submit/cancel/modify/place-order import.
- No change to Toss live mutation enabled semantics.
- No account-balance cache or stale read-model behavior.

- [x] **Step 6: Post Linear implementation note**

Add a comment to `ROB-563` with this body:

```md
Phase 1 implementation completed.

Changed:
- Added inner Sentry spans for KIS, Toss API, Toss portfolio snapshot, manual holdings, and market dashboard provider phases.
- Parallelized independent `/invest/api/home` and `/invest/api/account-panel` primary readers while keeping manual filtering after Toss API.
- Parallelized independent `/invest/api/market` provider captures.
- Preserved response schemas, paper reader behavior, Toss live mutation semantics, and read-only boundaries.

Verification:
- `uv run pytest tests/test_invest_home_service.py tests/test_invest_home_readers.py tests/test_invest_market_dashboard.py -q`
- `uv run ruff check app/services/invest_home_service.py app/services/invest_home_readers.py app/services/toss_portfolio_service.py app/services/invest_view_model/market_dashboard_service.py tests/test_invest_home_service.py tests/test_invest_home_readers.py tests/test_invest_market_dashboard.py`
- `uv run ruff format --check app/services/invest_home_service.py app/services/invest_home_readers.py app/services/toss_portfolio_service.py app/services/invest_view_model/market_dashboard_service.py tests/test_invest_home_service.py tests/test_invest_home_readers.py tests/test_invest_market_dashboard.py`
- `make test-unit`

Follow-up after deploy:
- Inspect new Sentry traces for remaining KIS domestic balance tail latency.
- Inspect Toss inner spans to see whether holdings, sellable quantity, buying power, or FX owns the next p95.
- Inspect market root self time after provider spans land.
```

- [x] **Step 7: Commit verification note if any docs changed**

If only code/tests changed, skip this commit. If a verification markdown artifact was added under `docs/superpowers/verification/`, commit it:

```bash
git add docs/superpowers/verification
git commit -m "docs: record rob-563 verification"
```

## Self-Review

- Spec coverage: The plan covers Sentry trace observability, `/home` and `/account-panel` reader parallelization, `/market` provider parallelization, response contract preservation, warning/degradation preservation, and tests.
- Out of scope: Account-balance caching, stale snapshots, live order approval changes, response schema changes, and false-flag Toss optimization are intentionally excluded.
- Type consistency: Helper names, test names, span operation names, and source labels match the code snippets in the tasks.
- Execution risk: The only concurrency introduced overlaps already independent read-only calls. Manual holdings remain after Toss API so duplicate filtering keeps its current inputs.
