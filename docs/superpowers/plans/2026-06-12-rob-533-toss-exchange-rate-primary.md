# ROB-533 Toss Exchange Rate Primary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the USD/KRW exchange-rate service internals so Toss Securities exchange-rate is the primary source when enabled, `open.er-api.com` remains the fallback, and consumers can opt in to both Toss `rate` and `midRate` without changing existing call signatures.

**Architecture:** Keep the change concentrated in `app/services/exchange_rate_service.py`. Existing scalar helpers keep returning `float`; after the product decision for ROB-533, that default scalar is Toss `midRate` when Toss succeeds. Add one opt-in detail helper that returns a typed quote object containing `rate`, `mid_rate`, source metadata, and validity window; cache detail quotes by provider-specific expiry so scalar callers and detail callers share the same fetch.

**Tech Stack:** Python 3.13, httpx, dataclasses, Decimal parsing, Toss read client from ROB-530, pytest, pytest-asyncio, Ruff, ty.

---

## Starting State And Scope

Current file: `app/services/exchange_rate_service.py`.

Current behavior:

- `get_usd_krw_rate() -> float` fetches `https://open.er-api.com/v6/latest/USD`.
- `_CACHE_TTL_SECONDS` is fixed at 300 seconds.
- `get_usd_krw_quote() -> float` is a scalar alias used by watch/stock-detail code.

ROB-533 behavior:

- If `settings.toss_api_enabled` is truthy, try Toss `GET /api/v1/exchange-rate?baseCurrency=USD&quoteCurrency=KRW`.
- On Toss disabled, missing configuration, client error, response error, or parse error, use the existing `open.er-api.com` source.
- Preserve existing consumer signatures: `get_usd_krw_rate()` and `get_usd_krw_quote()` still return `float`.
- Product decision: the default scalar value is `midRate` for Toss, not `rate`.
- Expose Toss `rate` for opt-in callers through a new helper, without migrating the eight known consumers in this issue.
- Use Toss `validUntil` for Toss cache expiry. Keep 300-second TTL for the `open.er-api.com` fallback.
- No DB migration. No router/MCP signature change. No direct consumer rewiring.

## File Structure

- Modify: `app/services/exchange_rate_service.py`
  - Owns the cache, provider selection, Toss parsing, fallback parsing, and public helpers.
- Create: `tests/services/test_exchange_rate_service.py`
  - Unit tests for Toss primary, default `midRate`, opt-in `rate`, fallback behavior, and cache expiry.

No `app/mcp_server/README.md` update is required because no MCP tool contract changes in this slice.

## Public Interface Decisions

Preserve:

```python
async def get_usd_krw_rate() -> float: ...

async def get_usd_krw_quote() -> float: ...
```

Add:

```python
@dataclass(frozen=True)
class UsdKrwExchangeRateQuote:
    rate: float
    mid_rate: float
    source: Literal["toss", "open_er_api"]
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    basis_point: float | None = None
    rate_change_type: str | None = None

    @property
    def default_rate(self) -> float:
        return self.mid_rate


async def get_usd_krw_rate_details() -> UsdKrwExchangeRateQuote: ...
```

Rationale:

- `get_usd_krw_quote()` already returns `float`; changing it to a structured object would break current call sites.
- `get_usd_krw_rate_details()` gives future consumers an explicit opt-in path for `rate` vs `mid_rate`.
- For fallback data, `rate == mid_rate` because `open.er-api.com` does not provide a separate buy rate.

## Task 1: Add Quote Object And Toss Parser

**Files:**
- Modify: `app/services/exchange_rate_service.py`
- Create: `tests/services/test_exchange_rate_service.py`

- [ ] **Step 1: Write failing parser/default tests**

Create `tests/services/test_exchange_rate_service.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services import exchange_rate_service as mod


@pytest.fixture(autouse=True)
def clear_exchange_rate_cache() -> None:
    mod._cache.clear()


def test_parse_toss_usd_krw_quote_uses_mid_rate_as_default() -> None:
    quote = mod._parse_toss_usd_krw_quote(
        {
            "baseCurrency": "USD",
            "quoteCurrency": "KRW",
            "rate": "1522.2",
            "midRate": "1522.05",
            "basisPoint": "15.2",
            "rateChangeType": "UP",
            "validFrom": "2026-06-12T09:30:00+09:00",
            "validUntil": "2026-06-12T09:31:00+09:00",
        }
    )

    assert quote.source == "toss"
    assert quote.rate == pytest.approx(1522.2)
    assert quote.mid_rate == pytest.approx(1522.05)
    assert quote.default_rate == pytest.approx(1522.05)
    assert quote.basis_point == pytest.approx(15.2)
    assert quote.rate_change_type == "UP"
    assert quote.valid_from == datetime(2026, 6, 12, 0, 30, tzinfo=UTC)
    assert quote.valid_until == datetime(2026, 6, 12, 0, 31, tzinfo=UTC)


def test_parse_open_er_api_quote_exposes_same_rate_and_mid_rate() -> None:
    quote = mod._parse_open_er_api_usd_krw_quote({"rates": {"KRW": 1498.7}})

    assert quote.source == "open_er_api"
    assert quote.rate == pytest.approx(1498.7)
    assert quote.mid_rate == pytest.approx(1498.7)
    assert quote.default_rate == pytest.approx(1498.7)
    assert quote.valid_from is None
    assert quote.valid_until is None
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/services/test_exchange_rate_service.py::test_parse_toss_usd_krw_quote_uses_mid_rate_as_default tests/services/test_exchange_rate_service.py::test_parse_open_er_api_quote_exposes_same_rate_and_mid_rate -q
```

Expected: FAIL because `UsdKrwExchangeRateQuote`, `_parse_toss_usd_krw_quote`, and `_parse_open_er_api_usd_krw_quote` do not exist.

- [ ] **Step 3: Add quote object and parsers**

In `app/services/exchange_rate_service.py`, update imports:

```python
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, TypedDict, cast
```

Add the public quote object after module constants:

```python
@dataclass(frozen=True)
class UsdKrwExchangeRateQuote:
    rate: float
    mid_rate: float
    source: Literal["toss", "open_er_api"]
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    basis_point: float | None = None
    rate_change_type: str | None = None

    @property
    def default_rate(self) -> float:
        return self.mid_rate
```

Add parsing helpers:

```python
def _parse_decimal_float(value: object) -> float:
    if isinstance(value, float):
        raise TypeError("Toss decimal values must be strings, not float")
    if value is None:
        raise TypeError("Decimal value is required")
    return float(Decimal(str(value)))


def _parse_optional_decimal_float(value: object) -> float | None:
    if value is None:
        return None
    return _parse_decimal_float(value)


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_toss_usd_krw_quote(raw: dict[str, Any]) -> UsdKrwExchangeRateQuote:
    if raw.get("baseCurrency") != "USD" or raw.get("quoteCurrency") != "KRW":
        raise ValueError("Toss exchange-rate response is not USD/KRW")
    return UsdKrwExchangeRateQuote(
        rate=_parse_decimal_float(raw["rate"]),
        mid_rate=_parse_decimal_float(raw["midRate"]),
        source="toss",
        valid_from=_parse_datetime(raw.get("validFrom")),
        valid_until=_parse_datetime(raw.get("validUntil")),
        basis_point=_parse_optional_decimal_float(raw.get("basisPoint")),
        rate_change_type=str(raw["rateChangeType"])
        if raw.get("rateChangeType") is not None
        else None,
    )


def _parse_open_er_api_usd_krw_quote(
    data: _ExchangeRatePayload,
) -> UsdKrwExchangeRateQuote:
    rate = float(data["rates"]["KRW"])
    return UsdKrwExchangeRateQuote(
        rate=rate,
        mid_rate=rate,
        source="open_er_api",
    )
```

- [ ] **Step 4: Run parser tests and verify pass**

Run:

```bash
uv run pytest tests/services/test_exchange_rate_service.py::test_parse_toss_usd_krw_quote_uses_mid_rate_as_default tests/services/test_exchange_rate_service.py::test_parse_open_er_api_quote_exposes_same_rate_and_mid_rate -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add app/services/exchange_rate_service.py tests/services/test_exchange_rate_service.py
git commit -m "feat(ROB-533): add USD/KRW quote details"
```

## Task 2: Add Toss Primary With Open ER API Fallback

**Files:**
- Modify: `app/services/exchange_rate_service.py`
- Modify: `tests/services/test_exchange_rate_service.py`

- [ ] **Step 1: Write failing provider-selection tests**

Append to `tests/services/test_exchange_rate_service.py`:

```python
@pytest.mark.asyncio
async def test_get_usd_krw_rate_details_uses_toss_when_enabled(monkeypatch) -> None:
    toss_quote = mod.UsdKrwExchangeRateQuote(
        rate=1522.2,
        mid_rate=1522.05,
        source="toss",
        valid_until=datetime(2026, 6, 12, 0, 31, tzinfo=UTC),
    )
    fallback_called = False

    async def fake_toss() -> mod.UsdKrwExchangeRateQuote:
        return toss_quote

    async def fake_fallback() -> mod.UsdKrwExchangeRateQuote:
        nonlocal fallback_called
        fallback_called = True
        return mod.UsdKrwExchangeRateQuote(
            rate=1498.7,
            mid_rate=1498.7,
            source="open_er_api",
        )

    monkeypatch.setattr(mod.settings, "toss_api_enabled", True)
    monkeypatch.setattr(mod, "_fetch_toss_usd_krw_quote", fake_toss)
    monkeypatch.setattr(mod, "_fetch_open_er_api_usd_krw_quote", fake_fallback)

    quote = await mod._fetch_usd_krw_rate_details()

    assert quote is toss_quote
    assert fallback_called is False


@pytest.mark.asyncio
async def test_get_usd_krw_rate_details_uses_fallback_when_toss_disabled(
    monkeypatch,
) -> None:
    async def fail_toss() -> mod.UsdKrwExchangeRateQuote:
        raise AssertionError("Toss should not be called when disabled")

    async def fake_fallback() -> mod.UsdKrwExchangeRateQuote:
        return mod.UsdKrwExchangeRateQuote(
            rate=1498.7,
            mid_rate=1498.7,
            source="open_er_api",
        )

    monkeypatch.setattr(mod.settings, "toss_api_enabled", False)
    monkeypatch.setattr(mod, "_fetch_toss_usd_krw_quote", fail_toss)
    monkeypatch.setattr(mod, "_fetch_open_er_api_usd_krw_quote", fake_fallback)

    quote = await mod._fetch_usd_krw_rate_details()

    assert quote.source == "open_er_api"
    assert quote.default_rate == pytest.approx(1498.7)


@pytest.mark.asyncio
async def test_get_usd_krw_rate_details_falls_back_when_toss_fails(
    monkeypatch,
) -> None:
    async def fail_toss() -> mod.UsdKrwExchangeRateQuote:
        raise RuntimeError("Toss is unavailable")

    async def fake_fallback() -> mod.UsdKrwExchangeRateQuote:
        return mod.UsdKrwExchangeRateQuote(
            rate=1498.7,
            mid_rate=1498.7,
            source="open_er_api",
        )

    monkeypatch.setattr(mod.settings, "toss_api_enabled", True)
    monkeypatch.setattr(mod, "_fetch_toss_usd_krw_quote", fail_toss)
    monkeypatch.setattr(mod, "_fetch_open_er_api_usd_krw_quote", fake_fallback)

    quote = await mod._fetch_usd_krw_rate_details()

    assert quote.source == "open_er_api"
    assert quote.default_rate == pytest.approx(1498.7)
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/services/test_exchange_rate_service.py::test_get_usd_krw_rate_details_uses_toss_when_enabled tests/services/test_exchange_rate_service.py::test_get_usd_krw_rate_details_uses_fallback_when_toss_disabled tests/services/test_exchange_rate_service.py::test_get_usd_krw_rate_details_falls_back_when_toss_fails -q
```

Expected: FAIL because `_fetch_usd_krw_rate_details`, `_fetch_toss_usd_krw_quote`, and `_fetch_open_er_api_usd_krw_quote` do not exist.

- [ ] **Step 3: Implement Toss fetch and fallback selection**

In `app/services/exchange_rate_service.py`, add imports:

```python
from app.core.config import settings
from app.services.brokers.toss.client import TossReadClient
```

Replace `_fetch_usd_krw_rate()` with these helpers:

```python
async def _fetch_toss_usd_krw_quote() -> UsdKrwExchangeRateQuote:
    client = TossReadClient.from_settings()
    try:
        raw = await client.exchange_rate(base_currency="USD", quote_currency="KRW")
    finally:
        await client.aclose()
    if not isinstance(raw, dict):
        raise TypeError("Toss exchange-rate response must be an object")
    quote = _parse_toss_usd_krw_quote(raw)
    logger.debug(
        "Fetched USD/KRW exchange rate from Toss: rate=%s mid_rate=%s valid_until=%s",
        quote.rate,
        quote.mid_rate,
        quote.valid_until,
    )
    return quote


async def _fetch_open_er_api_usd_krw_quote() -> UsdKrwExchangeRateQuote:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(_EXCHANGE_RATE_URL)
        _ = response.raise_for_status()
        data = cast(_ExchangeRatePayload, response.json())

    quote = _parse_open_er_api_usd_krw_quote(data)
    logger.debug("Fetched USD/KRW exchange rate from open.er-api.com: %s", quote.rate)
    return quote


async def _fetch_usd_krw_rate_details() -> UsdKrwExchangeRateQuote:
    if bool(getattr(settings, "toss_api_enabled", False)):
        try:
            return await _fetch_toss_usd_krw_quote()
        except Exception as exc:
            logger.warning(
                "Toss USD/KRW exchange-rate fetch failed; falling back to open.er-api.com: %s",
                exc,
            )
    return await _fetch_open_er_api_usd_krw_quote()
```

- [ ] **Step 4: Run provider-selection tests and verify pass**

Run:

```bash
uv run pytest tests/services/test_exchange_rate_service.py::test_get_usd_krw_rate_details_uses_toss_when_enabled tests/services/test_exchange_rate_service.py::test_get_usd_krw_rate_details_uses_fallback_when_toss_disabled tests/services/test_exchange_rate_service.py::test_get_usd_krw_rate_details_falls_back_when_toss_fails -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add app/services/exchange_rate_service.py tests/services/test_exchange_rate_service.py
git commit -m "feat(ROB-533): use Toss USD/KRW primary with fallback"
```

## Task 3: Cache Detail Quotes With Toss validUntil

**Files:**
- Modify: `app/services/exchange_rate_service.py`
- Modify: `tests/services/test_exchange_rate_service.py`

- [ ] **Step 1: Write failing cache tests**

Append to `tests/services/test_exchange_rate_service.py`:

```python
@pytest.mark.asyncio
async def test_cache_uses_toss_valid_until(monkeypatch) -> None:
    calls = 0
    now_utc = datetime(2026, 6, 12, 0, 30, 0, tzinfo=UTC)
    monotonic_now = 1000.0

    async def fake_fetch() -> mod.UsdKrwExchangeRateQuote:
        nonlocal calls
        calls += 1
        return mod.UsdKrwExchangeRateQuote(
            rate=1522.2 + calls,
            mid_rate=1522.05 + calls,
            source="toss",
            valid_until=datetime(2026, 6, 12, 0, 31, 0, tzinfo=UTC),
        )

    monkeypatch.setattr(mod, "_now_utc", lambda: now_utc)
    monkeypatch.setattr(mod.time, "monotonic", lambda: monotonic_now)
    monkeypatch.setattr(mod, "_fetch_usd_krw_rate_details", fake_fetch)

    first = await mod.get_usd_krw_rate_details()
    second = await mod.get_usd_krw_rate_details()

    assert first is second
    assert calls == 1

    monotonic_now = 1059.9
    third = await mod.get_usd_krw_rate_details()

    assert third is first
    assert calls == 1

    monotonic_now = 1060.1
    fourth = await mod.get_usd_krw_rate_details()

    assert fourth is not first
    assert fourth.mid_rate == pytest.approx(1524.05)
    assert calls == 2


@pytest.mark.asyncio
async def test_cache_uses_fixed_ttl_for_open_er_api(monkeypatch) -> None:
    calls = 0
    monotonic_now = 2000.0

    async def fake_fetch() -> mod.UsdKrwExchangeRateQuote:
        nonlocal calls
        calls += 1
        return mod.UsdKrwExchangeRateQuote(
            rate=1498.7 + calls,
            mid_rate=1498.7 + calls,
            source="open_er_api",
        )

    monkeypatch.setattr(mod.time, "monotonic", lambda: monotonic_now)
    monkeypatch.setattr(mod, "_fetch_usd_krw_rate_details", fake_fetch)

    first = await mod.get_usd_krw_rate_details()
    monotonic_now = 2299.9
    second = await mod.get_usd_krw_rate_details()

    assert second is first
    assert calls == 1

    monotonic_now = 2300.1
    third = await mod.get_usd_krw_rate_details()

    assert third is not first
    assert calls == 2
```

- [ ] **Step 2: Run cache tests and verify failure**

Run:

```bash
uv run pytest tests/services/test_exchange_rate_service.py::test_cache_uses_toss_valid_until tests/services/test_exchange_rate_service.py::test_cache_uses_fixed_ttl_for_open_er_api -q
```

Expected: FAIL because the cache still stores only floats and does not use `validUntil`.

- [ ] **Step 3: Replace cache internals**

In `app/services/exchange_rate_service.py`, replace cache constants and type:

```python
_EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"
_CACHE_KEY = "usd_krw"
_OPEN_ER_API_CACHE_TTL_SECONDS = 300.0
_MIN_TOSS_CACHE_TTL_SECONDS = 1.0
_cache: dict[str, dict[str, object]] = {}
```

Add clock and cache helpers:

```python
def _now_utc() -> datetime:
    return datetime.now(UTC)


def _quote_cache_ttl_seconds(quote: UsdKrwExchangeRateQuote) -> float:
    if quote.source == "toss" and quote.valid_until is not None:
        ttl = (quote.valid_until - _now_utc()).total_seconds()
        return max(ttl, _MIN_TOSS_CACHE_TTL_SECONDS)
    return _OPEN_ER_API_CACHE_TTL_SECONDS


def _get_cached_quote(now: float) -> UsdKrwExchangeRateQuote | None:
    cached = _cache.get(_CACHE_KEY)
    if cached and float(cached["expires_at"]) > now:
        quote = cached["quote"]
        if isinstance(quote, UsdKrwExchangeRateQuote):
            return quote
    return None


def _set_cached_quote(quote: UsdKrwExchangeRateQuote, now: float) -> None:
    _cache[_CACHE_KEY] = {
        "quote": quote,
        "expires_at": now + _quote_cache_ttl_seconds(quote),
    }
```

Replace `get_usd_krw_rate()` with the new detail-backed implementation:

```python
async def get_usd_krw_rate_details() -> UsdKrwExchangeRateQuote:
    now = time.monotonic()
    cached_quote = _get_cached_quote(now)
    if cached_quote is not None:
        return cached_quote

    async with _get_lock():
        now = time.monotonic()
        cached_quote = _get_cached_quote(now)
        if cached_quote is not None:
            return cached_quote

        quote = await _fetch_usd_krw_rate_details()
        _set_cached_quote(quote, now)
        return quote


async def get_usd_krw_rate() -> float:
    quote = await get_usd_krw_rate_details()
    return quote.default_rate
```

Keep `get_usd_krw_quote()` as a scalar alias:

```python
async def get_usd_krw_quote() -> float:
    """Return the default USD/KRW quote for existing scalar consumers."""
    return await get_usd_krw_rate()
```

- [ ] **Step 4: Run cache tests and verify pass**

Run:

```bash
uv run pytest tests/services/test_exchange_rate_service.py::test_cache_uses_toss_valid_until tests/services/test_exchange_rate_service.py::test_cache_uses_fixed_ttl_for_open_er_api -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add app/services/exchange_rate_service.py tests/services/test_exchange_rate_service.py
git commit -m "feat(ROB-533): cache Toss FX by validUntil"
```

## Task 4: Preserve Scalar Consumer Contract

**Files:**
- Modify: `tests/services/test_exchange_rate_service.py`

- [ ] **Step 1: Write scalar compatibility tests**

Append to `tests/services/test_exchange_rate_service.py`:

```python
@pytest.mark.asyncio
async def test_scalar_helpers_return_mid_rate_default(monkeypatch) -> None:
    async def fake_details() -> mod.UsdKrwExchangeRateQuote:
        return mod.UsdKrwExchangeRateQuote(
            rate=1522.2,
            mid_rate=1522.05,
            source="toss",
            valid_until=datetime(2026, 6, 12, 0, 31, tzinfo=UTC),
        )

    monkeypatch.setattr(mod, "_fetch_usd_krw_rate_details", fake_details)

    rate = await mod.get_usd_krw_rate()
    quote = await mod.get_usd_krw_quote()
    details = await mod.get_usd_krw_rate_details()

    assert rate == pytest.approx(1522.05)
    assert quote == pytest.approx(1522.05)
    assert details.rate == pytest.approx(1522.2)
    assert details.mid_rate == pytest.approx(1522.05)
```

- [ ] **Step 2: Run scalar compatibility test**

Run:

```bash
uv run pytest tests/services/test_exchange_rate_service.py::test_scalar_helpers_return_mid_rate_default -q
```

Expected: PASS after Task 3.

- [ ] **Step 3: Run targeted consumer regressions**

Run:

```bash
uv run pytest \
  tests/test_mcp_available_capital.py \
  tests/test_portfolio_overview_service.py::test_get_overview_includes_exchange_rate \
  tests/test_portfolio_overview_service.py::test_get_overview_exchange_rate_none_on_failure \
  tests/test_stock_detail_service.py \
  tests/test_screening_us.py \
  -q
```

Expected: PASS. These cover representative consumers that patch or call the scalar helper contract.

- [ ] **Step 4: Commit Task 4**

```bash
git add tests/services/test_exchange_rate_service.py
git commit -m "test(ROB-533): preserve scalar FX consumers"
```

## Task 5: Final Verification And Linear Update

**Files:**
- Modify: `app/services/exchange_rate_service.py`
- Modify: `tests/services/test_exchange_rate_service.py`
- Optional Linear comment/status update for `ROB-533`

- [ ] **Step 1: Run focused test suite**

Run:

```bash
uv run pytest tests/services/test_exchange_rate_service.py -q
```

Expected: PASS.

- [ ] **Step 2: Run Toss client regression tests**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_client.py tests/services/brokers/toss/test_config.py tests/services/brokers/toss/test_errors.py -q
```

Expected: PASS.

- [ ] **Step 3: Run lint on touched files**

Run:

```bash
uv run ruff check app/services/exchange_rate_service.py tests/services/test_exchange_rate_service.py
```

Expected: PASS.

- [ ] **Step 4: Run type check on touched files**

Run:

```bash
uv run ty check app/services/exchange_rate_service.py tests/services/test_exchange_rate_service.py
```

Expected: PASS, or document any pre-existing ty limitation if the project tool cannot target individual files.

- [ ] **Step 5: Inspect diff**

Run:

```bash
git diff -- app/services/exchange_rate_service.py tests/services/test_exchange_rate_service.py
```

Expected:

- No secrets or Toss credential values.
- No DB migration.
- No consumer call-site changes.
- `get_usd_krw_rate()` and `get_usd_krw_quote()` still return `float`.
- Toss scalar default uses `midRate`.
- Fallback source still uses `open.er-api.com`.

- [ ] **Step 6: Commit final verification fixes if needed**

If Task 5 required edits, commit them:

```bash
git add app/services/exchange_rate_service.py tests/services/test_exchange_rate_service.py
git commit -m "chore(ROB-533): verify Toss FX fallback integration"
```

If Task 5 did not require edits, do not create an empty commit.

- [ ] **Step 7: Add Linear implementation note**

Post this comment to `ROB-533` after implementation and verification:

```markdown
Implemented ROB-533 with Toss `/api/v1/exchange-rate` as primary when `TOSS_API_ENABLED` is true, preserving `open.er-api.com` fallback.

Decision applied: existing scalar helpers return Toss `midRate` by default. New detail helper exposes both `rate` and `mid_rate` for opt-in callers.

Verification:
- `uv run pytest tests/services/test_exchange_rate_service.py -q`
- `uv run pytest tests/services/brokers/toss/test_client.py tests/services/brokers/toss/test_config.py tests/services/brokers/toss/test_errors.py -q`
- `uv run ruff check app/services/exchange_rate_service.py tests/services/test_exchange_rate_service.py`
- `uv run ty check app/services/exchange_rate_service.py tests/services/test_exchange_rate_service.py`

Migration: 0.
Consumer signature changes: 0.
```

## Self-Review

Spec coverage:

- Toss primary when enabled: Task 2.
- Existing source fallback when Toss disabled or fails: Task 2.
- `validUntil` cache: Task 3.
- `midRate` exposure and `rate` opt-in: Tasks 1 and 4.
- Existing consumer signatures unchanged: Task 4.
- Regression tests and migration-free scope: Task 5.

Placeholder scan:

- No `TBD`, `TODO`, or unspecified implementation steps.
- All code-facing steps include concrete snippets or exact commands.

Type consistency:

- Public detail helper is consistently named `get_usd_krw_rate_details()`.
- Existing scalar helpers remain `get_usd_krw_rate()` and `get_usd_krw_quote()`.
- Quote object fields use `rate`, `mid_rate`, and `default_rate` consistently.
