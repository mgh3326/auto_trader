# US market_valuation Finnhub fallback (field-fill) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When yahoo `.info` leaves US valuation fields null (operator's "ROE rows 0") or fails (crumb/session), backfill the missing valuation fields from Finnhub's `company_basic_financials` metric endpoint — keeping `source='yahoo'`, recording per-field provenance, with zero migration.

**Architecture:** A new service-layer module `finnhub_fallback.py` owns the Finnhub metric fetch + unit conversions + the gap-fill merge. `default_valuation_fetcher`'s US branch wraps the yahoo gather in try/except and calls the fallback to fill gaps. The fallback is gated by a default-off settings flag and is inert without `FINNHUB_API_KEY`. Reporting (finnhub backfill counts + non-null coverage) is aggregated in the job layer from each payload's `raw_payload['_field_provenance']`.

**Tech Stack:** Python 3.13, asyncio, pydantic-settings, `finnhub-python` (optional dep), pytest (`@pytest.mark.unit`/`asyncio`/`integration`), `uv run`.

**Spec:** `docs/superpowers/specs/2026-06-09-rob-434-us-market-valuation-finnhub-fallback-design.md`

**Key code facts (verified against current code):**
- `app/services/market_valuation_snapshots/builder.py`: `default_valuation_fetcher(symbol, market, *, include_high_date=False)` (US branch at lines 54-82); `_payload_from_raw` (90-111) resolves each column from priority raw keys via an `or`-chain; `_source_for_market("us")` → `"yahoo"` (hardcoded); `build_valuation_snapshots_for_market` skips a symbol whose every metric is None and catches fetch exceptions (`_one`, 136-168).
- `app/services/brokers/yahoo/client.py::fetch_fundamental_info` returns keys `PER/PBR/EPS/BPS/Dividend Yield/ROE/yearHigh/yearLow/marketCap` (ROE already percent via `_roe_to_percent`). Crumb errors re-raise after one retry.
- `app/jobs/market_valuation_snapshots.py`: `run_market_valuation_snapshot_build` calls the builder **without** a `fetcher` (so the trigger flows through `default_valuation_fetcher`); aggregates `payloads` into `MarketValuationSnapshotBuildResult` (frozen dataclass, line 62) which the CLI's `_print_result` consumes.
- `app/services/finnhub_news.py`: `_get_finnhub_client()` (env-first `FINNHUB_API_KEY` → `settings.finnhub_api_key`, lazy `finnhub` import) — the reuse target; **no `app.mcp_server` import**.
- `app/core/config.py`: pydantic-settings `Settings` with snake_case fields; `finnhub_api_key: str | None = None` (line 323); feature flags like `invest_screener_snapshots_commit_enabled: bool = False`.

**Boundaries:** migration 0; broker/order/watch mutation 0; KR/`naver_finance` + fundamentals table untouched; fail-closed preserved; secrets never printed.

---

## File Structure

- **Create** `app/services/market_valuation_snapshots/finnhub_fallback.py` — Finnhub metric fetch + unit conversions + gap-fill merge + gate. Single responsibility: "given a partial yahoo raw dict, fill missing valuation fields from Finnhub when gated on."
- **Modify** `app/core/config.py` — add one settings flag.
- **Modify** `app/services/market_valuation_snapshots/builder.py` — extract a shared `_FIELD_SOURCE_KEYS` + `_resolve_raw_value` (DRY; reused by the fallback's gap detection), and wire the fallback into the US branch of `default_valuation_fetcher`.
- **Modify** `app/jobs/market_valuation_snapshots.py` — additive `finnhub_backfill` + `field_nonnull_coverage` aggregates on the result dataclass.
- **Modify** `scripts/build_market_valuation_snapshots.py` — print the two new aggregates in `_print_result`.
- **Create** `tests/test_market_valuation_finnhub_fallback_rob434.py` — all unit tests + one integration test.
- **Modify** `docs/runbooks/invest-screener-snapshots.md` — operator note on the flag.

---

## Task 1: Add the settings flag

**Files:**
- Modify: `app/core/config.py` (near line 323, beside `finnhub_api_key`)
- Test: `tests/test_market_valuation_finnhub_fallback_rob434.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_market_valuation_finnhub_fallback_rob434.py`:

```python
"""ROB-434: US market_valuation Finnhub fallback (field-fill).

When yahoo .info leaves valuation fields null (or fails), backfill the missing
fields from Finnhub company_basic_financials. source stays 'yahoo'; per-field
provenance in raw_payload['_field_provenance']; default-off, inert without key.
"""

from __future__ import annotations

import datetime as dt

import pytest


@pytest.mark.unit
def test_settings_flag_defaults_off() -> None:
    from app.core.config import settings

    assert settings.market_valuation_finnhub_fallback_enabled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_market_valuation_finnhub_fallback_rob434.py::test_settings_flag_defaults_off -v`
Expected: FAIL with `AttributeError` (no `market_valuation_finnhub_fallback_enabled`).

- [ ] **Step 3: Add the setting**

In `app/core/config.py`, immediately after line 323 (`finnhub_api_key: str | None = None`):

```python
    # ROB-434 — US market_valuation Finnhub fallback (field-fill). When ON and
    # FINNHUB_API_KEY is set, default_valuation_fetcher backfills valuation fields
    # yahoo .info left null (operator "ROE rows 0") from company_basic_financials.
    # Default False → inert until an operator enables it. No key → also inert.
    market_valuation_finnhub_fallback_enabled: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_market_valuation_finnhub_fallback_rob434.py::test_settings_flag_defaults_off -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/test_market_valuation_finnhub_fallback_rob434.py
git commit -m "feat(ROB-434): add market_valuation_finnhub_fallback_enabled flag (default off)

Co-authored-by: Hermes <hermes@example.invalid>
Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Extract shared field-key resolver in builder (DRY, no behavior change)

The fallback's gap detection must use the *same* per-field key priority that `_payload_from_raw` uses, or the two drift. Extract it once.

**Files:**
- Modify: `app/services/market_valuation_snapshots/builder.py` (lines 90-111)
- Test: `tests/test_market_valuation_finnhub_fallback_rob434.py`

- [ ] **Step 1: Write the failing test**

Append to the test file:

```python
@pytest.mark.unit
def test_resolve_raw_value_priority_and_truthiness() -> None:
    from app.services.market_valuation_snapshots.builder import _resolve_raw_value

    # canonical lowercase key wins over the yahoo key
    assert _resolve_raw_value({"roe": 22.0, "ROE": 9.9}, "roe") == 22.0
    # falls back to the yahoo key when canonical absent
    assert _resolve_raw_value({"marketCap": 1234}, "market_cap") == 1234
    # 0/None/absent → None (truthiness, matches _payload_from_raw's or-chain)
    assert _resolve_raw_value({"per": 0.0, "PER": 0}, "per") is None
    assert _resolve_raw_value({}, "high_52w_date") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_market_valuation_finnhub_fallback_rob434.py::test_resolve_raw_value_priority_and_truthiness -v`
Expected: FAIL with `ImportError` (`_resolve_raw_value` not defined).

- [ ] **Step 3: Refactor `_payload_from_raw` to use a shared resolver**

In `app/services/market_valuation_snapshots/builder.py`, replace the `_payload_from_raw` function (lines 90-111) with:

```python
# ROB-434: single source of truth for per-column raw-key priority. Used by
# _payload_from_raw AND finnhub_fallback's gap detection so they never drift.
# Mirrors _payload_from_raw's original or-chains exactly.
_FIELD_SOURCE_KEYS: dict[str, tuple[str, ...]] = {
    "per": ("per", "PER", "trailingPE"),
    "pbr": ("pbr", "PBR", "priceToBook"),
    "roe": ("roe", "ROE"),
    "dividend_yield": (
        "dividend_yield",
        "Dividend Yield",
        "trailingAnnualDividendYield",
    ),
    "market_cap": ("market_cap", "marketCap"),
    "high_52w": ("high_52w", "yearHigh"),
    "low_52w": ("low_52w", "yearLow"),
    "high_52w_date": ("high_52w_date",),
}


def _resolve_raw_value(raw: dict[str, Any], field: str) -> Any:
    """First truthy value among the field's priority keys (matches the original
    or-chain: 0/None are treated as absent)."""
    for key in _FIELD_SOURCE_KEYS[field]:
        value = raw.get(key)
        if value:
            return value
    return None


def _payload_from_raw(
    *, market: str, symbol: str, snapshot_date: dt.date, raw: dict[str, Any]
) -> MarketValuationSnapshotUpsert:
    return MarketValuationSnapshotUpsert(
        market=market,
        symbol=symbol,
        snapshot_date=snapshot_date,
        source=_source_for_market(market),
        per=_to_decimal(_resolve_raw_value(raw, "per")),
        pbr=_to_decimal(_resolve_raw_value(raw, "pbr")),
        roe=_to_decimal(_resolve_raw_value(raw, "roe")),
        dividend_yield=_to_decimal(_resolve_raw_value(raw, "dividend_yield")),
        market_cap=_to_decimal(_resolve_raw_value(raw, "market_cap")),
        high_52w=_to_decimal(_resolve_raw_value(raw, "high_52w")),
        low_52w=_to_decimal(_resolve_raw_value(raw, "low_52w")),
        high_52w_date=_to_date(_resolve_raw_value(raw, "high_52w_date")),
        raw_payload=redact_sensitive_payload(dict(raw)),
    )
```

- [ ] **Step 4: Run tests to verify pass + no regression**

Run: `uv run pytest tests/test_market_valuation_finnhub_fallback_rob434.py::test_resolve_raw_value_priority_and_truthiness tests/test_invest_coverage_valuation.py tests/test_yahoo_roe_rob440.py -v`
Expected: PASS (new test + all existing valuation/ROE tests — proves the refactor preserved behavior).

- [ ] **Step 5: Commit**

```bash
git add app/services/market_valuation_snapshots/builder.py tests/test_market_valuation_finnhub_fallback_rob434.py
git commit -m "refactor(ROB-434): extract _FIELD_SOURCE_KEYS/_resolve_raw_value in valuation builder

Co-authored-by: Hermes <hermes@example.invalid>
Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Finnhub metric mapping (unit conversions) — `finnhub_fallback.py`

The riskiest code: the unit traps. Pure function, no IO — test exhaustively first.

**Files:**
- Create: `app/services/market_valuation_snapshots/finnhub_fallback.py`
- Test: `tests/test_market_valuation_finnhub_fallback_rob434.py`

- [ ] **Step 1: Write the failing test**

Append to the test file:

```python
@pytest.mark.unit
def test_map_finnhub_metrics_unit_traps() -> None:
    from app.services.market_valuation_snapshots.finnhub_fallback import (
        _map_finnhub_metrics,
    )

    out = _map_finnhub_metrics(
        {
            "roeTTM": 22.0,  # already percent → NOT ×100
            "peTTM": 8.0,
            "pbAnnual": 0.9,
            "dividendYieldIndicatedAnnual": 3.0,  # percent → ÷100 ratio
            "marketCapitalization": 1500.0,  # millions → ×1e6 absolute
            "52WeekHigh": 110.0,
            "52WeekLow": 80.0,
            "52WeekHighDate": "2026-03-14",
        }
    )
    assert out["roe"] == 22.0  # critical: not 2200
    assert out["per"] == 8.0
    assert out["pbr"] == 0.9
    assert out["dividend_yield"] == 0.03
    assert out["market_cap"] == 1_500_000_000.0
    assert out["high_52w"] == 110.0
    assert out["low_52w"] == 80.0
    assert out["high_52w_date"] == "2026-03-14"  # iso str (JSON-safe, parsed later)


@pytest.mark.unit
def test_map_finnhub_metrics_fail_closed_on_missing_and_nonfinite() -> None:
    from app.services.market_valuation_snapshots.finnhub_fallback import (
        _map_finnhub_metrics,
    )

    out = _map_finnhub_metrics(
        {"roeTTM": None, "peTTM": "n/a", "marketCapitalization": float("inf")}
    )
    assert out == {}  # nothing fabricated; non-finite/None/unparseable dropped


@pytest.mark.unit
def test_map_finnhub_metrics_bad_date_dropped() -> None:
    from app.services.market_valuation_snapshots.finnhub_fallback import (
        _map_finnhub_metrics,
    )

    assert "high_52w_date" not in _map_finnhub_metrics({"52WeekHighDate": ""})
    assert "high_52w_date" not in _map_finnhub_metrics({"52WeekHighDate": None})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_market_valuation_finnhub_fallback_rob434.py -k map_finnhub_metrics -v`
Expected: FAIL with `ModuleNotFoundError` (module not created yet).

- [ ] **Step 3: Create the module with the mapping**

Create `app/services/market_valuation_snapshots/finnhub_fallback.py`:

```python
"""ROB-434: US market_valuation Finnhub fallback (field-fill).

When yahoo .info leaves valuation fields null (operator "ROE rows 0") or the
yahoo call fails (crumb/session), backfill the missing valuation fields from
Finnhub's company_basic_financials metric endpoint. Keeps source='yahoo';
records per-field provenance in raw['_field_provenance']. Default-off settings
gate; inert without FINNHUB_API_KEY. Fail-closed: any Finnhub error leaves raw
unchanged (no fabrication).

Service-layer only — does NOT import app.mcp_server (reuses the finnhub_news
client factory). Single consumer is default_valuation_fetcher.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from app.services.market_valuation_snapshots.builder import _resolve_raw_value

logger = logging.getLogger(__name__)


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _map_finnhub_metrics(metric: dict[str, Any]) -> dict[str, Any]:
    """Finnhub company_basic_financials['metric'] → canonical valuation fields.

    Unit traps (the whole reason this is a dedicated, exhaustively-tested fn):
    - roeTTM is ALREADY percent (yahoo returnOnEquity is a fraction ×100) → no ×100.
    - dividendYieldIndicatedAnnual is percent → ÷100 to the stored ratio (guard ≤0.25).
    - marketCapitalization is in MILLIONS → ×1e6 to absolute USD (guard ≥$100M).
    Missing / non-finite / unparseable → field omitted (fail-closed, never fabricated).
    """
    out: dict[str, Any] = {}
    roe = _to_float(metric.get("roeTTM"))
    if roe is not None:
        out["roe"] = roe  # already percent — do NOT ×100
    per = _to_float(metric.get("peTTM"))
    if per is not None:
        out["per"] = per
    pbr = _to_float(metric.get("pbAnnual"))
    if pbr is not None:
        out["pbr"] = pbr
    dividend_yield = _to_float(metric.get("dividendYieldIndicatedAnnual"))
    if dividend_yield is not None:
        out["dividend_yield"] = dividend_yield / 100.0  # percent → ratio
    market_cap_millions = _to_float(metric.get("marketCapitalization"))
    if market_cap_millions is not None:
        out["market_cap"] = market_cap_millions * 1_000_000.0  # millions → absolute
    high_52w = _to_float(metric.get("52WeekHigh"))
    if high_52w is not None:
        out["high_52w"] = high_52w
    low_52w = _to_float(metric.get("52WeekLow"))
    if low_52w is not None:
        out["low_52w"] = low_52w
    high_date = metric.get("52WeekHighDate")
    if isinstance(high_date, str) and high_date.strip():
        # iso string keeps raw_payload JSON-safe; _payload_from_raw parses to date.
        out["high_52w_date"] = high_date.strip()[:10]
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_market_valuation_finnhub_fallback_rob434.py -k map_finnhub_metrics -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/market_valuation_snapshots/finnhub_fallback.py tests/test_market_valuation_finnhub_fallback_rob434.py
git commit -m "feat(ROB-434): Finnhub metric → valuation field mapping with unit conversions

Co-authored-by: Hermes <hermes@example.invalid>
Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Fetch + gap-fill merge + gate — `finnhub_fallback.py`

**Files:**
- Modify: `app/services/market_valuation_snapshots/finnhub_fallback.py`
- Test: `tests/test_market_valuation_finnhub_fallback_rob434.py`

- [ ] **Step 1: Write the failing test**

Append to the test file:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_fallback_disabled_is_noop(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: False)
    calls = {"n": 0}

    async def _never(symbol):  # noqa: ANN001
        calls["n"] += 1
        return {"roe": 22.0}

    monkeypatch.setattr(fb, "fetch_valuation_finnhub", _never)
    raw = {"PER": 8.0}  # roe missing
    out = await fb.apply_valuation_fallback("AAPL", raw, yahoo_failed=False)
    assert out == {"PER": 8.0}  # untouched
    assert calls["n"] == 0  # finnhub never called


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_fallback_fills_only_missing_fields(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: True)

    async def _metrics(symbol):  # noqa: ANN001
        return {"roe": 18.0, "per": 99.0, "market_cap": 2_000_000_000.0}

    monkeypatch.setattr(fb, "fetch_valuation_finnhub", _metrics)
    # yahoo gave PER (8.0) but no ROE/market_cap → fill ROE + market_cap, keep PER
    raw = {"PER": 8.0}
    out = await fb.apply_valuation_fallback("AAPL", raw, yahoo_failed=False)
    assert out["roe"] == 18.0
    assert out["market_cap"] == 2_000_000_000.0
    assert "per" not in out  # PER already present (yahoo) → NOT overwritten
    assert out["_field_provenance"] == {"roe": "finnhub", "market_cap": "finnhub"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_fallback_total_yahoo_failure_fills_all(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: True)

    async def _metrics(symbol):  # noqa: ANN001
        return {"roe": 18.0, "per": 8.0, "market_cap": 2_000_000_000.0}

    monkeypatch.setattr(fb, "fetch_valuation_finnhub", _metrics)
    out = await fb.apply_valuation_fallback("AAPL", {}, yahoo_failed=True)
    assert out["roe"] == 18.0 and out["per"] == 8.0
    assert out["_field_provenance"]["per"] == "finnhub"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_fallback_finnhub_error_is_fail_closed(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: True)

    async def _boom(symbol):  # noqa: ANN001
        raise RuntimeError("finnhub rate limited")

    monkeypatch.setattr(fb, "fetch_valuation_finnhub", _boom)
    raw = {"PER": 8.0}
    out = await fb.apply_valuation_fallback("AAPL", raw, yahoo_failed=False)
    assert out == {"PER": 8.0}  # unchanged, no crash


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_fallback_no_gap_skips_finnhub(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: True)
    calls = {"n": 0}

    async def _metrics(symbol):  # noqa: ANN001
        calls["n"] += 1
        return {"roe": 1.0}

    monkeypatch.setattr(fb, "fetch_valuation_finnhub", _metrics)
    # every field present → no finnhub call
    raw = {
        "ROE": 15.0, "PER": 8.0, "PBR": 0.9, "Dividend Yield": 0.02,
        "marketCap": 3e9, "yearHigh": 100.0, "yearLow": 80.0,
        "high_52w_date": "2026-01-01",
    }
    out = await fb.apply_valuation_fallback("AAPL", raw, yahoo_failed=False)
    assert calls["n"] == 0
    assert "_field_provenance" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_market_valuation_finnhub_fallback_rob434.py -k apply_fallback -v`
Expected: FAIL with `AttributeError` (`apply_valuation_fallback` / `_finnhub_fallback_enabled` / `fetch_valuation_finnhub` not defined).

- [ ] **Step 3: Implement fetch + merge + gate**

Append to `app/services/market_valuation_snapshots/finnhub_fallback.py`:

```python
def _finnhub_fallback_enabled() -> bool:
    try:
        from app.core.config import settings
    except Exception:  # noqa: BLE001
        return False
    return bool(getattr(settings, "market_valuation_finnhub_fallback_enabled", False))


def _has_missing_fields(raw: dict[str, Any]) -> bool:
    return any(_resolve_raw_value(raw, field) is None for field in _FIELD_SOURCE_KEYS)


async def fetch_valuation_finnhub(symbol: str) -> dict[str, Any]:
    """Finnhub company_basic_financials metric → canonical valuation dict.

    Raises ImportError (finnhub lib missing) / ValueError (no key) / API errors —
    the caller (apply_valuation_fallback) catches and fail-closes.
    """
    from app.services.finnhub_news import _get_finnhub_client

    client = _get_finnhub_client()

    def _fetch_sync() -> dict[str, Any]:
        data = client.company_basic_financials(symbol.upper(), "all")
        return (data or {}).get("metric", {}) or {}

    metric = await asyncio.to_thread(_fetch_sync)
    return _map_finnhub_metrics(metric)


async def apply_valuation_fallback(
    symbol: str, raw: dict[str, Any], *, yahoo_failed: bool
) -> dict[str, Any]:
    """Backfill missing valuation fields in ``raw`` from Finnhub when gated on.

    No-op unless the settings flag is on AND there is a gap (or yahoo failed).
    Fills only fields ``raw`` lacks; records provenance in raw['_field_provenance'].
    source stays 'yahoo' (caller never changes it). Fail-closed on any Finnhub error.
    """
    if not _finnhub_fallback_enabled():
        return raw
    if not (yahoo_failed or _has_missing_fields(raw)):
        return raw
    try:
        metrics = await fetch_valuation_finnhub(symbol)
    except Exception as exc:  # noqa: BLE001 — no key / lib / API / rate-limit
        logger.warning("finnhub valuation fallback failed symbol=%s: %s", symbol, exc)
        return raw
    filled: list[str] = []
    for field, value in metrics.items():
        if value is None:
            continue
        if _resolve_raw_value(raw, field) is None:
            raw[field] = value
            filled.append(field)
    if filled:
        provenance = raw.setdefault("_field_provenance", {})
        for field in filled:
            provenance[field] = "finnhub"
    return raw
```

Also add the import needed by `_has_missing_fields` at the top of the file (it already imports `_resolve_raw_value`; add `_FIELD_SOURCE_KEYS`):

```python
from app.services.market_valuation_snapshots.builder import (
    _FIELD_SOURCE_KEYS,
    _resolve_raw_value,
)
```

(Replace the single-name import added in Task 3 Step 3 with this two-name import.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_market_valuation_finnhub_fallback_rob434.py -k "apply_fallback or map_finnhub" -v`
Expected: PASS (all mapping + apply tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/market_valuation_snapshots/finnhub_fallback.py tests/test_market_valuation_finnhub_fallback_rob434.py
git commit -m "feat(ROB-434): Finnhub valuation fetch + gap-fill merge + default-off gate

Co-authored-by: Hermes <hermes@example.invalid>
Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Wire the fallback into `default_valuation_fetcher` (US branch)

**Files:**
- Modify: `app/services/market_valuation_snapshots/builder.py` (US branch, lines 54-82)
- Test: `tests/test_market_valuation_finnhub_fallback_rob434.py`

- [ ] **Step 1: Write the failing test**

Append to the test file:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetcher_backfills_roe_when_yahoo_null(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import builder
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    async def _fast(sym):  # noqa: ANN001
        return {"symbol": sym}

    async def _fund(sym):  # noqa: ANN001
        return {"PER": 8.0, "ROE": None, "marketCap": 3_000_000_000}  # ROE missing

    monkeypatch.setattr("app.services.brokers.yahoo.client.fetch_fast_info", _fast)
    monkeypatch.setattr("app.services.brokers.yahoo.client.fetch_fundamental_info", _fund)
    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: True)

    async def _metrics(symbol):  # noqa: ANN001
        return {"roe": 18.0}

    monkeypatch.setattr(fb, "fetch_valuation_finnhub", _metrics)

    raw = await builder.default_valuation_fetcher("AAPL", "us")
    assert raw["roe"] == 18.0  # backfilled
    assert raw["PER"] == 8.0  # yahoo preserved
    assert raw["_field_provenance"] == {"roe": "finnhub"}
    # source unchanged downstream:
    payload = builder._payload_from_raw(
        market="us", symbol="AAPL", snapshot_date=dt.date(2026, 6, 9), raw=raw
    )
    assert payload.source == "yahoo"
    assert payload.roe == __import__("decimal").Decimal("18.0")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetcher_recovers_total_yahoo_failure(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import builder
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    async def _fast(sym):  # noqa: ANN001
        return {"symbol": sym}

    async def _boom(sym):  # noqa: ANN001
        raise RuntimeError("Invalid Crumb / Session is closed")

    monkeypatch.setattr("app.services.brokers.yahoo.client.fetch_fast_info", _fast)
    monkeypatch.setattr("app.services.brokers.yahoo.client.fetch_fundamental_info", _boom)
    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: True)

    async def _metrics(symbol):  # noqa: ANN001
        return {"roe": 18.0, "per": 8.0, "market_cap": 2_000_000_000.0}

    monkeypatch.setattr(fb, "fetch_valuation_finnhub", _metrics)

    raw = await builder.default_valuation_fetcher("AAPL", "us")  # no raise
    assert raw["roe"] == 18.0 and raw["per"] == 8.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetcher_total_failure_reraises_when_disabled(monkeypatch) -> None:
    from app.services.market_valuation_snapshots import builder
    from app.services.market_valuation_snapshots import finnhub_fallback as fb

    async def _fast(sym):  # noqa: ANN001
        return {"symbol": sym}

    async def _boom(sym):  # noqa: ANN001
        raise RuntimeError("Invalid Crumb")

    monkeypatch.setattr("app.services.brokers.yahoo.client.fetch_fast_info", _fast)
    monkeypatch.setattr("app.services.brokers.yahoo.client.fetch_fundamental_info", _boom)
    monkeypatch.setattr(fb, "_finnhub_fallback_enabled", lambda: False)  # disabled

    with pytest.raises(RuntimeError, match="Invalid Crumb"):
        await builder.default_valuation_fetcher("AAPL", "us")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_market_valuation_finnhub_fallback_rob434.py -k "fetcher_" -v`
Expected: FAIL — `test_fetcher_backfills_roe_when_yahoo_null` fails (no `roe`/`_field_provenance` because the fallback isn't wired); `test_fetcher_recovers_total_yahoo_failure` fails (raises instead of recovering).

- [ ] **Step 3: Wire the fallback into the US branch**

In `app/services/market_valuation_snapshots/builder.py`, replace the US branch of `default_valuation_fetcher` (currently lines 54-82, from `if market == "us":` through `return {... "high_52w_date": ...}`) with:

```python
    if market == "us":
        from app.services.brokers.yahoo.client import (
            fetch_52w_high_date,
            fetch_fast_info,
            fetch_fundamental_info,
        )
        from app.services.market_valuation_snapshots.finnhub_fallback import (
            apply_valuation_fallback,
        )

        raw: dict[str, Any] = {}
        yahoo_failed = False
        yahoo_exc: Exception | None = None
        try:
            # ROB-440 PR4: the 52w-high DATE needs a heavy OHLC fetch (1y daily) — a
            # 3rd yahoo call/symbol that over-loads yfinance at universe scale. Opt-in.
            if not include_high_date:
                fast_info, fundamentals = await asyncio.gather(
                    fetch_fast_info(symbol), fetch_fundamental_info(symbol)
                )
                raw = {**fast_info, **fundamentals}
            else:
                fast_info, fundamentals, high_52w_date = await asyncio.gather(
                    fetch_fast_info(symbol),
                    fetch_fundamental_info(symbol),
                    fetch_52w_high_date(symbol),
                )
                raw = {
                    **fast_info,
                    **fundamentals,
                    "high_52w_date": high_52w_date.isoformat()
                    if high_52w_date
                    else None,
                }
        except Exception as exc:  # noqa: BLE001 — try Finnhub before giving up
            raw, yahoo_failed, yahoo_exc = {}, True, exc

        # ROB-434: backfill yahoo's null/missing valuation fields from Finnhub when
        # gated on. No-op when disabled / no key / no gap. source stays 'yahoo'.
        raw = await apply_valuation_fallback(symbol, raw, yahoo_failed=yahoo_failed)

        # Nothing recovered from a total yahoo failure → preserve today's skip+warn.
        if yahoo_failed and not raw and yahoo_exc is not None:
            raise yahoo_exc
        return raw
```

- [ ] **Step 4: Run tests to verify pass + no regression**

Run: `uv run pytest tests/test_market_valuation_finnhub_fallback_rob434.py tests/test_invest_coverage_valuation.py::test_default_valuation_fetcher_high_date_opt_in -v`
Expected: PASS (new fetcher tests + the existing opt-in test — proves the disabled/no-key default path is unchanged; note the existing test has no flag set, so the fallback is a no-op there).

- [ ] **Step 5: Commit**

```bash
git add app/services/market_valuation_snapshots/builder.py tests/test_market_valuation_finnhub_fallback_rob434.py
git commit -m "feat(ROB-434): wire Finnhub fallback into default_valuation_fetcher US branch

Co-authored-by: Hermes <hermes@example.invalid>
Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Operator smoke reporting (finnhub backfill counts + non-null coverage)

**Files:**
- Modify: `app/jobs/market_valuation_snapshots.py` (result dataclass + `run_market_valuation_snapshot_build` loop)
- Modify: `scripts/build_market_valuation_snapshots.py` (`_print_result`)
- Test: `tests/test_market_valuation_finnhub_fallback_rob434.py`

- [ ] **Step 1: Write the failing test**

Append to the test file:

```python
@pytest.mark.unit
def test_aggregate_reporting_from_payloads() -> None:
    from decimal import Decimal

    from app.jobs.market_valuation_snapshots import _aggregate_report
    from app.services.market_valuation_snapshots.repository import (
        MarketValuationSnapshotUpsert,
    )

    p1 = MarketValuationSnapshotUpsert(
        market="us", symbol="AAA", snapshot_date=dt.date(2026, 6, 9), source="yahoo",
        per=Decimal("8"), roe=Decimal("18"), market_cap=Decimal("3e9"),
        raw_payload={"_field_provenance": {"roe": "finnhub"}},
    )
    p2 = MarketValuationSnapshotUpsert(
        market="us", symbol="BBB", snapshot_date=dt.date(2026, 6, 9), source="yahoo",
        per=Decimal("9"), roe=None, market_cap=Decimal("5e9"),
        raw_payload={},
    )
    backfill, coverage = _aggregate_report([p1, p2])
    assert backfill == {"roe": 1}  # only p1's roe was finnhub
    assert coverage["per"] == 2
    assert coverage["roe"] == 1  # p2 roe is None
    assert coverage["market_cap"] == 2
    assert coverage["pbr"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_market_valuation_finnhub_fallback_rob434.py::test_aggregate_reporting_from_payloads -v`
Expected: FAIL with `ImportError` (`_aggregate_report` not defined).

- [ ] **Step 3: Add aggregation + result fields**

In `app/jobs/market_valuation_snapshots.py`:

(a) Add two fields to `MarketValuationSnapshotBuildResult` (after line 74, `warnings`):

```python
    finnhub_backfill: dict[str, int] = field(default_factory=dict)
    field_nonnull_coverage: dict[str, int] = field(default_factory=dict)
```

(b) Add a module-level helper + constant (after `_sample`, ~line 214):

```python
_COVERAGE_FIELDS: tuple[str, ...] = (
    "per",
    "pbr",
    "roe",
    "dividend_yield",
    "market_cap",
    "high_52w",
    "low_52w",
    "high_52w_date",
)


def _aggregate_report(
    payloads: list[MarketValuationSnapshotUpsert],
) -> tuple[dict[str, int], dict[str, int]]:
    """ROB-434: per-field Finnhub-backfill counts + per-field non-null coverage,
    derived from each payload's raw_payload['_field_provenance'] and column values.
    Operator smoke (acceptance #5): provider attribution + coverage, works in dry-run."""
    backfill: Counter[str] = Counter()
    coverage: Counter[str] = Counter({f: 0 for f in _COVERAGE_FIELDS})
    for payload in payloads:
        provenance = (payload.raw_payload or {}).get("_field_provenance", {})
        for field_name, src in provenance.items():
            if src == "finnhub":
                backfill[field_name] += 1
        for field_name in _COVERAGE_FIELDS:
            if getattr(payload, field_name) is not None:
                coverage[field_name] += 1
    return dict(backfill), dict(coverage)
```

(c) In `run_market_valuation_snapshot_build`, accumulate across batches. After the line `total_built = 0` (line 255), add:

```python
    finnhub_backfill: Counter[str] = Counter()
    coverage: Counter[str] = Counter({f: 0 for f in _COVERAGE_FIELDS})
```

Inside the batch loop, after `samples.extend(...)` (line 271), add:

```python
        batch_backfill, batch_coverage = _aggregate_report(payloads)
        finnhub_backfill.update(batch_backfill)
        coverage.update(batch_coverage)
```

In the final `return MarketValuationSnapshotBuildResult(...)` (line 275), add two kwargs:

```python
        finnhub_backfill=dict(finnhub_backfill),
        field_nonnull_coverage=dict(coverage),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_market_valuation_finnhub_fallback_rob434.py::test_aggregate_reporting_from_payloads -v`
Expected: PASS.

- [ ] **Step 5: Print the aggregates in the CLI**

In `scripts/build_market_valuation_snapshots.py::_print_result`, after the `snapshot distribution` block (after line 103, before the `samples` block), add:

```python
    finnhub_backfill = getattr(result, "finnhub_backfill", {})
    if finnhub_backfill:
        print("finnhub backfill (fields filled where yahoo was null):")
        for field_name, count in sorted(finnhub_backfill.items()):
            print(f"  {field_name}: {count}")
    coverage = getattr(result, "field_nonnull_coverage", {})
    if coverage:
        print("non-null field coverage:")
        for field_name, count in sorted(coverage.items()):
            print(f"  {field_name}: {count}/{result.snapshots_built}")
```

- [ ] **Step 6: Verify CLI smoke output (dry-run, no network needed for the print path)**

Run: `uv run python -c "import ast,sys; ast.parse(open('scripts/build_market_valuation_snapshots.py').read()); print('syntax ok')"`
Expected: `syntax ok`. (Full CLI dry-run hits the network; the print path is covered by the unit test on `_aggregate_report`.)

- [ ] **Step 7: Commit**

```bash
git add app/jobs/market_valuation_snapshots.py scripts/build_market_valuation_snapshots.py tests/test_market_valuation_finnhub_fallback_rob434.py
git commit -m "feat(ROB-434): operator smoke — finnhub backfill counts + non-null coverage

Co-authored-by: Hermes <hermes@example.invalid>
Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Integration — backfilled row upserts, provenance survives, passes quality guard

**Files:**
- Test: `tests/test_market_valuation_finnhub_fallback_rob434.py`

- [ ] **Step 1: Write the failing test**

Append to the test file:

```python
import sqlalchemy as sa  # noqa: E402  (top-of-file import is fine too)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfilled_row_upserts_and_passes_quality_guard(db_session) -> None:
    from decimal import Decimal

    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.invest_view_model.us_quality_guards import (
        apply_us_valuation_quality_guards,
    )
    from app.services.market_valuation_snapshots.builder import (
        build_valuation_snapshots_for_market,
    )
    from app.services.market_valuation_snapshots.repository import (
        MarketValuationSnapshotsRepository,
    )

    snapshot_date = dt.date(2026, 6, 9)
    sym = "ZZQ434"

    # Simulate the merged raw a yahoo-partial + finnhub-backfill produced:
    async def fake_fetcher(symbol: str, market: str) -> dict[str, object]:
        assert market == "us"
        return {
            "PER": "8",  # yahoo
            "roe": 18.0,  # finnhub-filled (≤300 guard)
            "market_cap": 3_000_000_000.0,  # finnhub-filled (≥$100M guard)
            "_field_provenance": {"roe": "finnhub", "market_cap": "finnhub"},
        }

    result = await build_valuation_snapshots_for_market(
        market="us", symbols=[sym], snapshot_date=snapshot_date, fetcher=fake_fetcher
    )
    assert len(result.payloads) == 1
    payload = result.payloads[0]
    assert payload.source == "yahoo"
    assert payload.roe == Decimal("18.0")
    assert payload.raw_payload["_field_provenance"] == {
        "roe": "finnhub",
        "market_cap": "finnhub",
    }

    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(MarketValuationSnapshot.symbol == sym)
    )
    await db_session.commit()
    assert await MarketValuationSnapshotsRepository(db_session).upsert(result.payloads) == 1
    await db_session.commit()

    # The backfilled row survives the read-time quality guard (mcap≥$100M, roe≤300%).
    stmt = apply_us_valuation_quality_guards(
        sa.select(MarketValuationSnapshot.symbol).where(
            MarketValuationSnapshot.symbol == sym,
            MarketValuationSnapshot.snapshot_date == snapshot_date,
        ),
        uses_roe=True,
    )
    rows = (await db_session.execute(stmt)).all()
    assert len(rows) == 1  # passes the guard

    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(MarketValuationSnapshot.symbol == sym)
    )
    await db_session.commit()
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `uv run pytest tests/test_market_valuation_finnhub_fallback_rob434.py::test_backfilled_row_upserts_and_passes_quality_guard -v`
Expected: PASS if a test DB is available. If `apply_us_valuation_quality_guards`'s exact signature differs (verify with `grep -n "def apply_us_valuation_quality_guards" app/services/invest_view_model/us_quality_guards.py`), adjust the call (e.g. it may guard a `cand_stmt` against `MarketValuationSnapshot` columns) and re-run.

> Note: if the local test DB is unavailable, this `@pytest.mark.integration` test is skipped/excluded by `-m "not integration"`; the unit coverage already proves the logic. Do not block the PR on DB availability, but DO run it once if the DB is up.

- [ ] **Step 3: (only if Step 2 failed on signature/guard mismatch) fix the test call**

Read `app/services/invest_view_model/us_quality_guards.py` and match the real `apply_us_valuation_quality_guards` signature (statement arg + `uses_roe`/`uses_dividend` kwargs) and the column it filters on. Update the test's `stmt = apply_us_valuation_quality_guards(...)` accordingly.

- [ ] **Step 4: Commit**

```bash
git add tests/test_market_valuation_finnhub_fallback_rob434.py
git commit -m "test(ROB-434): integration — backfilled row upserts + passes US quality guard

Co-authored-by: Hermes <hermes@example.invalid>
Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Docs + full-suite gate

**Files:**
- Modify: `docs/runbooks/invest-screener-snapshots.md`
- (verify) all touched files

- [ ] **Step 1: Add an operator note to the runbook**

In `docs/runbooks/invest-screener-snapshots.md`, add a subsection under the US valuation activation section (find it: `grep -n "valuation" docs/runbooks/invest-screener-snapshots.md`):

```markdown
### US valuation Finnhub fallback (ROB-434, default-off)

`market_valuation_snapshots` US builds can backfill valuation fields that yahoo
`.info` left null (operator "ROE rows 0" / Invalid Crumb) from Finnhub
`company_basic_financials`. **Disabled by default.** To enable for a build:

1. Set `FINNHUB_API_KEY` (operator secret manager — never commit).
2. Set `MARKET_VALUATION_FINNHUB_FALLBACK_ENABLED=true`.
3. Run `uv run python -m scripts.build_market_valuation_snapshots --market us --common-stocks-only --concurrency 4` (dry-run first). The summary prints `finnhub backfill` per-field counts and `non-null field coverage`.

Without the key or flag the build is byte-identical to today (yahoo-only,
fail-closed). `source` stays `yahoo`; per-field provenance is in
`raw_payload._field_provenance`. Finnhub free tier ~60/min — keep `--concurrency`
modest. Fallback fires only on a per-symbol gap, never per-symbol unconditionally.
```

- [ ] **Step 2: Run lint + typecheck on touched files**

Run:
```bash
uv run ruff check app/services/market_valuation_snapshots/finnhub_fallback.py app/services/market_valuation_snapshots/builder.py app/jobs/market_valuation_snapshots.py app/core/config.py scripts/build_market_valuation_snapshots.py tests/test_market_valuation_finnhub_fallback_rob434.py
uv run ruff format --check app/services/market_valuation_snapshots/finnhub_fallback.py tests/test_market_valuation_finnhub_fallback_rob434.py
uv run ty check app/
```
Expected: clean (no errors). Fix any reported issues, re-run. (Reminder from project memory: CI lints **both** `app/` and `tests/` — run ruff on the test file too.)

- [ ] **Step 3: Run the full relevant suite**

Run:
```bash
uv run pytest tests/test_market_valuation_finnhub_fallback_rob434.py tests/test_invest_coverage_valuation.py tests/test_yahoo_roe_rob440.py -v -m "not integration"
```
Expected: all PASS. Then, if a DB is available, run once including integration:
```bash
uv run pytest tests/test_market_valuation_finnhub_fallback_rob434.py -v
```

- [ ] **Step 4: Commit docs + push + open PR**

```bash
git add docs/runbooks/invest-screener-snapshots.md
git commit -m "docs(ROB-434): runbook note for US valuation Finnhub fallback (default-off)

Co-authored-by: Hermes <hermes@example.invalid>
Co-authored-by: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
git push -u origin rob-434-finnhub-valuation-fallback
gh pr create --base main --title "feat(ROB-434): US valuation Finnhub fallback (field-fill)" --body "$(cat <<'EOF'
## What
When yahoo `.info` leaves US `market_valuation_snapshots` fields null (operator "ROE rows 0" / Invalid Crumb) or fails, backfill the missing valuation fields from Finnhub `company_basic_financials`. Keeps `source='yahoo'`, per-field provenance in `raw_payload._field_provenance`. **Migration 0.**

## Design / Plan
- Spec: `docs/superpowers/specs/2026-06-09-rob-434-us-market-valuation-finnhub-fallback-design.md`
- Plan: `docs/superpowers/plans/2026-06-09-rob-434-us-market-valuation-finnhub-fallback.md`

## Decisions
- Scope: valuation table only. Provenance: field-fill, no migration. Backfill: all missing valuation fields. Toggle: `MARKET_VALUATION_FINNHUB_FALLBACK_ENABLED` default-off, inert without `FINNHUB_API_KEY`.
- Unit traps nailed in tests: roeTTM already %, marketCapitalization millions→absolute, dividendYield %→ratio.

## Safety
- Default-off + no-key inert → byte-identical to today. Fail-closed preserved. broker/order/watch mutation 0. KR + fundamentals table untouched. Read-time quality guard unchanged (backfilled row must pass it — integration-tested).

## Operator follow-up (out of PR)
Set the key + flag, run dry-run build, review `finnhub backfill` / `non-null coverage` summary, then `--commit`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**1. Spec coverage** (each spec section → task):
- 컴포넌트 1 (finnhub_fallback helper) → Tasks 3, 4. ✅
- 컴포넌트 2 (trigger in default_valuation_fetcher) → Task 5. ✅
- 컴포넌트 3 (field mapping + unit traps) → Task 3 (+ asserted in Task 5). ✅
- 컴포넌트 4 (settings flag) → Task 1. ✅
- 컴포넌트 5 (smoke reporting) → Task 6. ✅
- Provenance / fail-closed → Tasks 3,4,5,7 (provenance survives + guard pass in Task 7). ✅
- Boundaries (migration 0, KR untouched, no-key inert) → Tasks 1,2 (no-regression), 5 (disabled re-raise), 8 (lint/suite). ✅
- Docs/runbook → Task 8. ✅
- Out-of-scope (fundamentals table, separate finnhub source row, operator backfill) → not implemented, noted in PR body. ✅

**2. Placeholder scan:** No TBD/TODO; every code step shows full code. Task 7 Step 3 is a conditional fix-up (verify-then-adjust), not a placeholder — it carries the exact grep + what to match. ✅

**3. Type consistency:** `_resolve_raw_value(raw, field)` defined in Task 2, imported/used in Tasks 3-4. `_FIELD_SOURCE_KEYS` defined Task 2, imported Task 4. `apply_valuation_fallback(symbol, raw, *, yahoo_failed)` defined Task 4, called Task 5 with the same signature. `_aggregate_report(payloads) -> (backfill, coverage)` defined + used Task 6. `_map_finnhub_metrics` / `fetch_valuation_finnhub` names consistent across Tasks 3-5. `MarketValuationSnapshotBuildResult` new fields `finnhub_backfill`/`field_nonnull_coverage` consistent across job + CLI (Task 6). ✅

**Known coupling (documented):** `_FIELD_SOURCE_KEYS` is the single source of truth for raw-key priority; both `_payload_from_raw` and the fallback's gap detection use it via `_resolve_raw_value` (no drift). The fallback writes canonical lowercase keys, which `_resolve_raw_value` ranks first.
