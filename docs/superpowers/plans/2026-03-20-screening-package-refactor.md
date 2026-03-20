# Screening Package Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `analysis_screen_core.py` (3,693 lines) into a `screening/` package organized by market (KR/US/Crypto) and responsibility (common, enrichment, entrypoint).

**Architecture:** Pure move-refactor with zero behavior changes. Each market gets its own module. Shared utilities and the unified entrypoint live in dedicated modules. Backward-compatible re-exports keep existing imports working during transition. Tests that use `monkeypatch.setattr` on the old module must be updated to patch at the new module path.

**Tech Stack:** Python 3.13, pytest-asyncio, Ruff, ty

---

## File Structure

```
app/mcp_server/tooling/screening/
    __init__.py           # Re-exports public API (screen_stocks_unified, normalize_screen_request)
    common.py             # ~300 lines: constants, timeout utils, data converters, normalization, filters, response builder, MarketCapCache
    enrichment.py         # ~200 lines: equity enrichment pipeline (Naver/Yahoo wrappers)
    tvscreener_support.py # ~220 lines: TvScreener capability checks, row mapping, response adaptation
    kr.py                 # ~950 lines: _screen_kr, _screen_kr_via_tvscreener, _screen_kr_with_fallback
    us.py                 # ~950 lines: _screen_us, _screen_us_via_tvscreener, _screen_us_with_fallback
    crypto.py             # ~850 lines: _enrich_crypto_indicators, _screen_crypto, _screen_crypto_via_tvscreener, _screen_crypto_with_fallback, _CRYPTO_MARKET_CAP_CACHE
    entrypoint.py         # ~100 lines: screen_stocks_unified

app/mcp_server/tooling/
    analysis_screen_core.py  # Shrunk to compat shim (~10 lines): re-exports everything from screening.*
```

Existing files stay untouched:
- `analysis_screen_crypto.py` (finalize_crypto_screen) — stays as-is (already separate, imported by crypto.py)
- `analysis_crypto_score.py` — stays as-is (already separate)

### Function-to-File Mapping

| Target File | Functions (current line range in analysis_screen_core.py) |
|---|---|
| **common.py** | `_timeout_seconds` (90), `TimeoutBehavior` (94), `_with_timeout` (102), `_to_optional_float` (156), `_to_optional_int` (168), `_rank_priority` (183), `is_safe_drop` (189), `_extract_market_symbol` (199), `_compute_rsi_bucket` (209), `_strip_exchange_prefix` (216), `_get_first_present` (223), `_get_tvscreener_attr` (231), `_extract_kr_stock_code` (239), `_kr_market_codes` (243), `_clean_text` (251), `_normalize_screen_market` (668), `_normalize_asset_type` (675), `_normalize_sort_by` (682), `_normalize_sort_order` (689), `_normalize_optional_text` (696), `_normalize_sector_value` (703), `_normalize_sector_compare_key` (707), `_canonicalize_us_sector_label` (716), `_normalize_min_analyst_buy` (730), `_normalize_min_dividend_value` (738), `_normalize_dividend_yield_threshold` (856), `normalize_screen_request` (760), `_validate_screen_filters` (1073), `_apply_basic_filters` (1115), `_sort_and_limit` (1165), `_empty_rsi_enrichment_diagnostics` (1197), `_build_screen_response` (1315), `MarketCapCache` (559), constants (63-87) |
| **enrichment.py** | `_apply_equity_enrichment_defaults` (265), `_compute_target_upside_pct` (287), `_row_has_complete_screen_enrichment` (295), `_filter_supported_keyword_args` (306), `_screen_row_symbol` (330), `_is_equity_stock_row` (339), `_decorate_screen_rows_with_equity_enrichment` (349), `_decorate_screen_response_with_equity_enrichment` (426), `_apply_post_enrichment_filters` (481), `_pick_display_name` (512), `_resolve_crypto_display_name` (519), `_tradingview_symbol_name` (538), `_is_market_warning` (542), `_sort_crypto_by_rsi_bucket` (549), `_SCREEN_ENRICHMENT_FIELDS` (67) |
| **tvscreener_support.py** | `_required_tvscreener_stock_capabilities` (866), `_get_tvscreener_stock_capability_snapshot` (899), `_can_use_tvscreener_stock_path` (937), `_map_tvscreener_stock_row` (969), `_adapt_tvscreener_stock_response` (1042) |
| **kr.py** | `_screen_kr` (1361), `_screen_kr_via_tvscreener` (2191), `_screen_kr_with_fallback` (3462) |
| **us.py** | `_screen_us` (1670), `_screen_us_via_tvscreener` (2420), `_screen_us_with_fallback` (3544) |
| **crypto.py** | `_run_crypto_indicator_enrichment` (1208), `_run_crypto_coingecko_fetch` (1275), `_finalize_rsi_enrichment_diagnostics` (1291), `_enrich_crypto_indicators` (1899), `_screen_crypto` (2801), `_screen_crypto_via_tvscreener` (3003), `_screen_crypto_with_fallback` (3623), `_CRYPTO_MARKET_CAP_CACHE` (665) |
| **entrypoint.py** | `screen_stocks_unified` (3270) |

### Key placement decisions (from review)

1. **`_normalize_dividend_yield_threshold` → common.py** (not tvscreener_support.py): Called by `_normalize_min_dividend_value` in common.py. Putting it in tvscreener_support.py would create circular import.
2. **`_empty_rsi_enrichment_diagnostics` → common.py** (not crypto.py): Called by `_build_screen_response` (common.py) and `_screen_kr` (kr.py). Putting it in crypto.py would create circular import.
3. **`MarketCapCache` → common.py**, **`_CRYPTO_MARKET_CAP_CACHE` singleton → crypto.py**: MarketCapCache is a generic class. The singleton is crypto-specific and only used in crypto.py functions.

### External Consumers That Need Updated Imports (or compat shim)

| Consumer File | Current Import | After Refactor |
|---|---|---|
| `analysis_screening.py:22-35` | `from analysis_screen_core import ...` | Shim handles (Task 10 migrates) |
| `analysis_tool_handlers.py:19` | `from analysis_screen_core import normalize_screen_request` | Shim handles (Task 10 migrates) |
| `app/services/screener_service.py:14` | `from analysis_screen_core import normalize_screen_request` | Shim handles (Task 10 migrates) |
| `tests/test_analysis_screen_core.py:5` | `from analysis_screen_core import _clean_text, ...` | Shim re-exports (no change needed) |
| `tests/test_tvscreener_stocks.py:10` | `from analysis_screen_core import ...` + `patch("...analysis_screen_core.TvScreenerService")` etc. | **Must update** (Task 8.5) — string-based patches of third-party imports |
| `tests/test_tvscreener_crypto.py:10` | `from analysis_screen_core import ...` + `patch("...analysis_screen_core.upbit_service")` etc. | **Must update** (Task 8.5) — string-based patches of third-party imports |
| `tests/test_crypto_composite_score.py` | `monkeypatch.setattr(analysis_screen_core, "_fetch_ohlcv_for_indicators", ...)` etc. | **Must update** (Task 8.5) |
| `tests/_mcp_screen_stocks_support.py` | `from ... import analysis_screen_core` + monkeypatch | **Must update** (Task 8.5) |
| `tests/_mcp_tooling_support.py` | `monkeypatch.setattr(analysis_screen_core, "get_upbit_warning_markets", ...)` etc. | **Must update** (Task 8.5) |
| `tests/test_mcp_screen_stocks_filters_and_rsi.py` | monkeypatch on `analysis_screen_core` | **Must update** (Task 8.5) |
| `tests/test_mcp_screen_stocks_crypto.py` | `analysis_screen_core._CRYPTO_MARKET_CAP_CACHE` | **Must update** (Task 8.5) |
| `tests/test_mcp_recommend_flow.py` | `analysis_screen_core.yf.screen` | **Must update** (Task 8.5) |

### Monkeypatch Strategy

Tests that use `monkeypatch.setattr(analysis_screen_core, "func_name", ...)` will break because:
1. The shim re-exports names but patching the shim doesn't affect the actual module where the function lives.
2. Attributes like `yf`, `asyncio`, `_calculate_rsi` won't exist on the shim module.

**Fix:** Update monkeypatch targets to the **new** module path. For example:
- `monkeypatch.setattr(analysis_screen_core, "_screen_kr", ...)` → `monkeypatch.setattr("app.mcp_server.tooling.screening.kr._screen_kr", ...)`
- `monkeypatch.setattr(analysis_screen_core, "_calculate_rsi", ...)` → `monkeypatch.setattr("app.mcp_server.tooling.screening.kr._calculate_rsi", ...)` (patch at point of use)
- `analysis_screen_core._CRYPTO_MARKET_CAP_CACHE` → `from app.mcp_server.tooling.screening.crypto import _CRYPTO_MARKET_CAP_CACHE`
- `analysis_screen_core.yf` → `monkeypatch.setattr("app.mcp_server.tooling.screening.us.yf", ...)`

This is handled in Task 8.5 (after the shim is in place).

---

## Tasks

### Task 1: Create `screening/common.py` with shared utilities

**Files:**
- Create: `app/mcp_server/tooling/screening/__init__.py`
- Create: `app/mcp_server/tooling/screening/common.py`
- Create: `tests/test_screening_common.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_screening_common.py
"""Verify screening.common re-exports match the original module."""
import pytest


class TestCommonReExports:
    """Ensure every util from common.py is importable."""

    def test_timeout_seconds(self):
        from app.mcp_server.tooling.screening.common import _timeout_seconds
        assert _timeout_seconds("tvscreener") == 30.0

    def test_to_optional_float_valid(self):
        from app.mcp_server.tooling.screening.common import _to_optional_float
        assert _to_optional_float("3.14") == pytest.approx(3.14)

    def test_to_optional_float_none(self):
        from app.mcp_server.tooling.screening.common import _to_optional_float
        assert _to_optional_float(None) is None

    def test_to_optional_int_valid(self):
        from app.mcp_server.tooling.screening.common import _to_optional_int
        assert _to_optional_int("42") == 42

    def test_clean_text(self):
        from app.mcp_server.tooling.screening.common import _clean_text
        assert _clean_text("  hello  ") == "hello"

    def test_normalize_screen_request_returns_dict(self):
        from app.mcp_server.tooling.screening.common import normalize_screen_request
        result = normalize_screen_request(market="kr")
        assert isinstance(result, dict)
        assert result["market"] == "kr"

    def test_validate_screen_filters_empty(self):
        from app.mcp_server.tooling.screening.common import _validate_screen_filters
        # Should not raise for no filters
        _validate_screen_filters(
            market="kr", asset_type=None,
            min_market_cap=None, max_per=None,
            min_dividend_yield=None, max_rsi=None,
            sort_by=None,
        )

    def test_build_screen_response(self):
        from app.mcp_server.tooling.screening.common import _build_screen_response
        resp = _build_screen_response(
            results=[], total_count=0, filters_applied={},
            market="kr",
        )
        assert resp["market"] == "kr"
        assert resp["results"] == []

    def test_normalize_dividend_yield_threshold(self):
        from app.mcp_server.tooling.screening.common import _normalize_dividend_yield_threshold
        result = _normalize_dividend_yield_threshold(3.0)
        assert isinstance(result, (float, int, type(None)))

    def test_empty_rsi_enrichment_diagnostics(self):
        from app.mcp_server.tooling.screening.common import _empty_rsi_enrichment_diagnostics
        d = _empty_rsi_enrichment_diagnostics()
        assert isinstance(d, dict)
        assert "attempted" in d

    def test_market_cap_cache_class(self):
        from app.mcp_server.tooling.screening.common import MarketCapCache
        cache = MarketCapCache(ttl=10)
        assert cache is not None

    def test_constants_exist(self):
        from app.mcp_server.tooling.screening.common import (
            DROP_THRESHOLD,
            DEFAULT_TIMEOUTS,
            CRYPTO_TOP_BY_VOLUME,
        )
        assert DROP_THRESHOLD == -0.30
        assert "tvscreener" in DEFAULT_TIMEOUTS
        assert CRYPTO_TOP_BY_VOLUME == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screening_common.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.mcp_server.tooling.screening'`

- [ ] **Step 3: Create `screening/__init__.py` (empty for now)**

```python
# app/mcp_server/tooling/screening/__init__.py
"""Stock screening package — split from analysis_screen_core.py."""
```

- [ ] **Step 4: Extract common utilities into `screening/common.py`**

Copy (not move yet) the following functions/classes from `analysis_screen_core.py` lines 63-102, 156-251, 668-855, 1073-1360 into `screening/common.py`. Keep the same imports they need. The file should contain:

- Constants: `DROP_THRESHOLD`, `MARKET_PANIC`, `CRYPTO_TOP_BY_VOLUME`, `COINGECKO_MARKETS_URL`, `DEFAULT_TIMEOUTS`
- Timeout: `_timeout_seconds`, `TimeoutBehavior`, `_with_timeout`
- Converters: `_to_optional_float`, `_to_optional_int`, `_rank_priority`, `is_safe_drop`, `_extract_market_symbol`, `_compute_rsi_bucket`, `_strip_exchange_prefix`, `_get_first_present`, `_get_tvscreener_attr`, `_extract_kr_stock_code`, `_kr_market_codes`, `_clean_text`
- Normalization: `_normalize_screen_market`, `_normalize_asset_type`, `_normalize_sort_by`, `_normalize_sort_order`, `_normalize_optional_text`, `_normalize_sector_value`, `_normalize_sector_compare_key`, `_canonicalize_us_sector_label`, `_normalize_min_analyst_buy`, `_normalize_min_dividend_value`, `_normalize_dividend_yield_threshold`, `normalize_screen_request`
- Filtering: `_validate_screen_filters`, `_apply_basic_filters`, `_sort_and_limit`
- Response: `_build_screen_response`, `_empty_rsi_enrichment_diagnostics`
- Cache: `MarketCapCache`

Imports needed (copy from original):
```python
from __future__ import annotations
import asyncio
import logging
import math
from typing import Any
from app.mcp_server.tooling.shared import error_payload as _error_payload
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_screening_common.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/screening/__init__.py app/mcp_server/tooling/screening/common.py tests/test_screening_common.py
git commit -m "refactor: extract screening/common.py with shared utils and normalization"
```

---

### Task 2: Create `screening/enrichment.py` with equity enrichment pipeline

**Files:**
- Create: `app/mcp_server/tooling/screening/enrichment.py`
- Create: `tests/test_screening_enrichment.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_screening_enrichment.py
"""Verify screening.enrichment functions are importable and work."""
import pytest


class TestEnrichmentImports:
    def test_apply_equity_enrichment_defaults(self):
        from app.mcp_server.tooling.screening.enrichment import _apply_equity_enrichment_defaults
        row: dict = {}
        _apply_equity_enrichment_defaults(row)
        assert "sector" in row

    def test_compute_target_upside_pct(self):
        from app.mcp_server.tooling.screening.enrichment import _compute_target_upside_pct
        result = _compute_target_upside_pct(100.0, 120.0)
        assert result == pytest.approx(20.0)

    def test_row_has_complete_screen_enrichment_empty(self):
        from app.mcp_server.tooling.screening.enrichment import _row_has_complete_screen_enrichment
        assert _row_has_complete_screen_enrichment({}) is False

    def test_screen_enrichment_fields_constant(self):
        from app.mcp_server.tooling.screening.enrichment import _SCREEN_ENRICHMENT_FIELDS
        assert "sector" in _SCREEN_ENRICHMENT_FIELDS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screening_enrichment.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Extract enrichment functions into `screening/enrichment.py`**

Copy from `analysis_screen_core.py` lines 67-74, 265-549 into `screening/enrichment.py`:

- `_SCREEN_ENRICHMENT_FIELDS`
- `_apply_equity_enrichment_defaults`, `_compute_target_upside_pct`, `_row_has_complete_screen_enrichment`
- `_filter_supported_keyword_args`, `_screen_row_symbol`, `_is_equity_stock_row`
- `_decorate_screen_rows_with_equity_enrichment`, `_decorate_screen_response_with_equity_enrichment`
- `_apply_post_enrichment_filters`
- `_pick_display_name`, `_resolve_crypto_display_name`, `_tradingview_symbol_name`
- `_is_market_warning`, `_sort_crypto_by_rsi_bucket`

Imports needed:
```python
from __future__ import annotations
import asyncio
import inspect
import logging
import math
from typing import Any

from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_screen_enrichment_kr,
    _fetch_screen_enrichment_us,
)
from app.mcp_server.tooling.screening.common import (
    _clean_text,
    _compute_rsi_bucket,
    _extract_market_symbol,
    _get_first_present,
    _get_tvscreener_attr,
    _normalize_sector_compare_key,
    _rank_priority,
    _sort_and_limit,
    _strip_exchange_prefix,
    _to_optional_float,
    _with_timeout,
)
from app.services.upbit_symbol_universe_service import get_upbit_market_display_names
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_screening_enrichment.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/screening/enrichment.py tests/test_screening_enrichment.py
git commit -m "refactor: extract screening/enrichment.py with equity enrichment pipeline"
```

---

### Task 3: Create `screening/tvscreener_support.py`

**Files:**
- Create: `app/mcp_server/tooling/screening/tvscreener_support.py`
- Create: `tests/test_screening_tvscreener_support.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_screening_tvscreener_support.py
"""Verify tvscreener_support functions are importable."""

class TestTvScreenerSupportImports:
    def test_required_capabilities(self):
        from app.mcp_server.tooling.screening.tvscreener_support import _required_tvscreener_stock_capabilities
        caps = _required_tvscreener_stock_capabilities()
        assert isinstance(caps, (list, set, tuple))

```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screening_tvscreener_support.py -v`
Expected: FAIL

- [ ] **Step 3: Extract TvScreener support into `screening/tvscreener_support.py`**

Copy from `analysis_screen_core.py` lines 866-1071:

- `_required_tvscreener_stock_capabilities`
- `_get_tvscreener_stock_capability_snapshot`
- `_can_use_tvscreener_stock_path`
- `_map_tvscreener_stock_row`
- `_adapt_tvscreener_stock_response`

(**Note:** `_normalize_dividend_yield_threshold` stays in common.py to avoid circular imports.)

Imports needed:
```python
from __future__ import annotations
import logging
from typing import Any

from app.mcp_server.tooling.screening.common import (
    _build_screen_response,
    _clean_text,
    _get_first_present,
    _get_tvscreener_attr,
    _timeout_seconds,
    _to_optional_float,
    _to_optional_int,
)
from app.mcp_server.tooling.screening.enrichment import (
    _apply_equity_enrichment_defaults,
    _compute_target_upside_pct,
)
from app.services.tvscreener_service import (
    TvScreenerCapabilitySnapshot,
    TvScreenerService,
    _import_tvscreener,
)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_screening_tvscreener_support.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/screening/tvscreener_support.py tests/test_screening_tvscreener_support.py
git commit -m "refactor: extract screening/tvscreener_support.py"
```

---

### Task 4: Create `screening/kr.py` — KR market screening

**Files:**
- Create: `app/mcp_server/tooling/screening/kr.py`
- Create: `tests/test_screening_kr.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_screening_kr.py
"""Verify KR screening functions are importable from the new location."""

class TestKrScreeningImports:
    def test_screen_kr_importable(self):
        from app.mcp_server.tooling.screening.kr import _screen_kr
        assert callable(_screen_kr)

    def test_screen_kr_via_tvscreener_importable(self):
        from app.mcp_server.tooling.screening.kr import _screen_kr_via_tvscreener
        assert callable(_screen_kr_via_tvscreener)

    def test_screen_kr_with_fallback_importable(self):
        from app.mcp_server.tooling.screening.kr import _screen_kr_with_fallback
        assert callable(_screen_kr_with_fallback)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screening_kr.py -v`
Expected: FAIL

- [ ] **Step 3: Extract KR screening into `screening/kr.py`**

Copy from `analysis_screen_core.py`:
- `_screen_kr` (line 1361-~1669)
- `_screen_kr_via_tvscreener` (line 2191-~2419)
- `_screen_kr_with_fallback` (line 3462-~3543)

Imports needed — reference the original file's imports and pull from:
- `screening.common` — filters, normalization, response builders, converters
- `screening.enrichment` — equity enrichment decorators
- `screening.tvscreener_support` — capability checks, row mapping
- `app.services.krx` — `classify_etf_category`, `fetch_etf_all_cached`, `fetch_stock_all_cached`, `fetch_valuation_all_cached`
- `app.services.tvscreener_service` — `TvScreenerService`, error types
- `app.mcp_server.tooling.market_data_indicators` — RSI calculation

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_screening_kr.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/screening/kr.py tests/test_screening_kr.py
git commit -m "refactor: extract screening/kr.py with KR market screening"
```

---

### Task 5: Create `screening/us.py` — US market screening

**Files:**
- Create: `app/mcp_server/tooling/screening/us.py`
- Create: `tests/test_screening_us.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_screening_us.py
"""Verify US screening functions are importable from the new location."""

class TestUsScreeningImports:
    def test_screen_us_importable(self):
        from app.mcp_server.tooling.screening.us import _screen_us
        assert callable(_screen_us)

    def test_screen_us_via_tvscreener_importable(self):
        from app.mcp_server.tooling.screening.us import _screen_us_via_tvscreener
        assert callable(_screen_us_via_tvscreener)

    def test_screen_us_with_fallback_importable(self):
        from app.mcp_server.tooling.screening.us import _screen_us_with_fallback
        assert callable(_screen_us_with_fallback)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screening_us.py -v`
Expected: FAIL

- [ ] **Step 3: Extract US screening into `screening/us.py`**

Copy from `analysis_screen_core.py`:
- `_screen_us` (line 1670-~1898)
- `_screen_us_via_tvscreener` (line 2420-~2800)
- `_screen_us_with_fallback` (line 3544-~3622)

Imports needed:
- `screening.common` — filters, normalization, converters
- `screening.enrichment` — equity enrichment decorators
- `screening.tvscreener_support` — capability checks, row mapping
- `yfinance`, `httpx`, `sentry_sdk`
- `app.monitoring.build_yfinance_tracing_session`
- `app.services.tvscreener_service` — `TvScreenerService`, error types
- `app.mcp_server.tooling.market_data_indicators` — RSI calculation

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_screening_us.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/screening/us.py tests/test_screening_us.py
git commit -m "refactor: extract screening/us.py with US market screening"
```

---

### Task 6: Create `screening/crypto.py` — Crypto screening

**Files:**
- Create: `app/mcp_server/tooling/screening/crypto.py`
- Create: `tests/test_screening_crypto.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_screening_crypto.py
"""Verify crypto screening functions are importable from the new location."""

class TestCryptoScreeningImports:
    def test_screen_crypto_importable(self):
        from app.mcp_server.tooling.screening.crypto import _screen_crypto
        assert callable(_screen_crypto)

    def test_screen_crypto_via_tvscreener_importable(self):
        from app.mcp_server.tooling.screening.crypto import _screen_crypto_via_tvscreener
        assert callable(_screen_crypto_via_tvscreener)

    def test_screen_crypto_with_fallback_importable(self):
        from app.mcp_server.tooling.screening.crypto import _screen_crypto_with_fallback
        assert callable(_screen_crypto_with_fallback)

    def test_enrich_crypto_indicators_importable(self):
        from app.mcp_server.tooling.screening.crypto import _enrich_crypto_indicators
        assert callable(_enrich_crypto_indicators)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screening_crypto.py -v`
Expected: FAIL

- [ ] **Step 3: Extract crypto screening into `screening/crypto.py`**

Copy from `analysis_screen_core.py` (note: `_empty_rsi_enrichment_diagnostics` is in common.py, import it from there):
- `_run_crypto_indicator_enrichment` (line 1208)
- `_run_crypto_coingecko_fetch` (line 1275)
- `_finalize_rsi_enrichment_diagnostics` (line 1291)
- `_enrich_crypto_indicators` (line 1899)
- `_screen_crypto` (line 2801)
- `_screen_crypto_via_tvscreener` (line 3003)
- `_screen_crypto_with_fallback` (line 3623)

Imports needed:
- `screening.common` — converters, filters, timeout, constants (`COINGECKO_MARKETS_URL`, `CRYPTO_TOP_BY_VOLUME`, `DROP_THRESHOLD`, `MARKET_PANIC`)
- `screening.enrichment` — `_resolve_crypto_display_name`, `_is_market_warning`, `_sort_crypto_by_rsi_bucket`
- `app.mcp_server.tooling.analysis_crypto_score` — `calculate_crypto_metrics_from_ohlcv`
- `app.mcp_server.tooling.analysis_screen_crypto` — `finalize_crypto_screen`
- `app.mcp_server.tooling.market_data_indicators` — RSI/OHLCV functions
- `app.services.brokers.upbit.client` — Upbit API
- `app.services.upbit_symbol_universe_service` — warning/display names
- `app.utils.symbol_mapping` — symbol conversion
- `app.services.tvscreener_service` — TvScreener
- `httpx`, `sentry_sdk`, `asyncio`, `time`, `datetime`

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_screening_crypto.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/screening/crypto.py tests/test_screening_crypto.py
git commit -m "refactor: extract screening/crypto.py with crypto screening"
```

---

### Task 7: Create `screening/entrypoint.py` with `screen_stocks_unified`

**Files:**
- Create: `app/mcp_server/tooling/screening/entrypoint.py`
- Create: `tests/test_screening_entrypoint.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_screening_entrypoint.py
"""Verify entrypoint functions are importable from the new location."""

class TestEntrypointImports:
    def test_screen_stocks_unified_importable(self):
        from app.mcp_server.tooling.screening.entrypoint import screen_stocks_unified
        assert callable(screen_stocks_unified)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screening_entrypoint.py -v`
Expected: FAIL

- [ ] **Step 3: Extract entrypoint into `screening/entrypoint.py`**

Copy from `analysis_screen_core.py`:
- `screen_stocks_unified` (line 3270-3461)

(**Note:** `MarketCapCache` is in common.py. `_CRYPTO_MARKET_CAP_CACHE` singleton is in crypto.py.)

Imports needed:
```python
from __future__ import annotations
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.screening.common import (
    _build_screen_response,
    _normalize_screen_market,
    _validate_screen_filters,
    normalize_screen_request,
)
from app.mcp_server.tooling.screening.enrichment import (
    _decorate_screen_response_with_equity_enrichment,
)
from app.mcp_server.tooling.screening.kr import _screen_kr_with_fallback
from app.mcp_server.tooling.screening.us import _screen_us_with_fallback
from app.mcp_server.tooling.screening.crypto import _screen_crypto_with_fallback
from app.mcp_server.tooling.shared import error_payload as _error_payload
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_screening_entrypoint.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/screening/entrypoint.py tests/test_screening_entrypoint.py
git commit -m "refactor: extract screening/entrypoint.py with screen_stocks_unified"
```

---

### Task 8: Wire up `screening/__init__.py` and backward-compat shim

**Files:**
- Modify: `app/mcp_server/tooling/screening/__init__.py`
- Modify: `app/mcp_server/tooling/analysis_screen_core.py` (replace with shim)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_screening_compat.py
"""Verify backward compatibility — old import paths still work."""

class TestBackwardCompat:
    """All existing imports from analysis_screen_core must still resolve."""

    def test_screen_stocks_unified_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import screen_stocks_unified
        assert callable(screen_stocks_unified)

    def test_normalize_screen_request_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import normalize_screen_request
        assert callable(normalize_screen_request)

    def test_screen_kr_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _screen_kr
        assert callable(_screen_kr)

    def test_screen_us_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _screen_us
        assert callable(_screen_us)

    def test_screen_crypto_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _screen_crypto
        assert callable(_screen_crypto)

    def test_build_screen_response_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _build_screen_response
        assert callable(_build_screen_response)

    def test_clean_text_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _clean_text
        assert callable(_clean_text)

    def test_to_optional_float_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _to_optional_float
        assert callable(_to_optional_float)

    def test_to_optional_int_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _to_optional_int
        assert callable(_to_optional_int)

    def test_enrich_crypto_indicators_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _enrich_crypto_indicators
        assert callable(_enrich_crypto_indicators)

    def test_screen_kr_via_tvscreener_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _screen_kr_via_tvscreener
        assert callable(_screen_kr_via_tvscreener)

    def test_screen_us_via_tvscreener_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _screen_us_via_tvscreener
        assert callable(_screen_us_via_tvscreener)

    def test_screen_crypto_via_tvscreener_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _screen_crypto_via_tvscreener
        assert callable(_screen_crypto_via_tvscreener)

    def test_validate_screen_filters_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _validate_screen_filters
        assert callable(_validate_screen_filters)

    def test_new_package_init_exports(self):
        from app.mcp_server.tooling.screening import screen_stocks_unified, normalize_screen_request
        assert callable(screen_stocks_unified)
        assert callable(normalize_screen_request)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_screening_compat.py -v`
Expected: FAIL (new `__init__.py` doesn't export everything yet)

- [ ] **Step 3: Populate `screening/__init__.py` with public API**

```python
# app/mcp_server/tooling/screening/__init__.py
"""Stock screening package — split from analysis_screen_core.py."""
from app.mcp_server.tooling.screening.common import normalize_screen_request
from app.mcp_server.tooling.screening.entrypoint import screen_stocks_unified

__all__ = ["normalize_screen_request", "screen_stocks_unified"]
```

- [ ] **Step 4: Replace `analysis_screen_core.py` with backward-compat shim**

Back up the original file first (or rely on git), then replace with:

```python
"""Backward-compatibility shim — use app.mcp_server.tooling.screening instead."""
from __future__ import annotations

# --- common ---
from app.mcp_server.tooling.screening.common import (  # noqa: F401
    COINGECKO_MARKETS_URL,
    CRYPTO_TOP_BY_VOLUME,
    DEFAULT_TIMEOUTS,
    DROP_THRESHOLD,
    MARKET_PANIC,
    MarketCapCache,
    TimeoutBehavior,
    _apply_basic_filters,
    _build_screen_response,
    _canonicalize_us_sector_label,
    _clean_text,
    _compute_rsi_bucket,
    _empty_rsi_enrichment_diagnostics,
    _extract_kr_stock_code,
    _extract_market_symbol,
    _get_first_present,
    _get_tvscreener_attr,
    _kr_market_codes,
    _normalize_asset_type,
    _normalize_dividend_yield_threshold,
    _normalize_min_analyst_buy,
    _normalize_min_dividend_value,
    _normalize_optional_text,
    _normalize_screen_market,
    _normalize_sector_compare_key,
    _normalize_sector_value,
    _normalize_sort_by,
    _normalize_sort_order,
    _rank_priority,
    _sort_and_limit,
    _strip_exchange_prefix,
    _timeout_seconds,
    _to_optional_float,
    _to_optional_int,
    _validate_screen_filters,
    _with_timeout,
    is_safe_drop,
    normalize_screen_request,
)

# --- enrichment ---
from app.mcp_server.tooling.screening.enrichment import (  # noqa: F401
    _SCREEN_ENRICHMENT_FIELDS,
    _apply_equity_enrichment_defaults,
    _apply_post_enrichment_filters,
    _compute_target_upside_pct,
    _decorate_screen_response_with_equity_enrichment,
    _decorate_screen_rows_with_equity_enrichment,
    _filter_supported_keyword_args,
    _is_equity_stock_row,
    _is_market_warning,
    _pick_display_name,
    _resolve_crypto_display_name,
    _row_has_complete_screen_enrichment,
    _screen_row_symbol,
    _sort_crypto_by_rsi_bucket,
    _tradingview_symbol_name,
)

# --- tvscreener_support ---
from app.mcp_server.tooling.screening.tvscreener_support import (  # noqa: F401
    _adapt_tvscreener_stock_response,
    _can_use_tvscreener_stock_path,
    _get_tvscreener_stock_capability_snapshot,
    _map_tvscreener_stock_row,
    _required_tvscreener_stock_capabilities,
)

# --- kr ---
from app.mcp_server.tooling.screening.kr import (  # noqa: F401
    _screen_kr,
    _screen_kr_via_tvscreener,
    _screen_kr_with_fallback,
)

# --- us ---
from app.mcp_server.tooling.screening.us import (  # noqa: F401
    _screen_us,
    _screen_us_via_tvscreener,
    _screen_us_with_fallback,
)

# --- crypto ---
from app.mcp_server.tooling.screening.crypto import (  # noqa: F401
    _CRYPTO_MARKET_CAP_CACHE,
    _enrich_crypto_indicators,
    _finalize_rsi_enrichment_diagnostics,
    _run_crypto_coingecko_fetch,
    _run_crypto_indicator_enrichment,
    _screen_crypto,
    _screen_crypto_via_tvscreener,
    _screen_crypto_with_fallback,
)

# --- entrypoint ---
from app.mcp_server.tooling.screening.entrypoint import (  # noqa: F401
    screen_stocks_unified,
)
```

- [ ] **Step 5: Run compat tests**

Run: `uv run pytest tests/test_screening_compat.py -v`
Expected: ALL PASS

- [ ] **Step 6: Run ALL existing screening tests (some will fail due to monkeypatch)**

Run: `uv run pytest tests/test_analysis_screen_core.py tests/test_tvscreener_stocks.py tests/test_tvscreener_crypto.py tests/test_mcp_screen_stocks_kr.py tests/test_mcp_screen_stocks_crypto.py tests/test_mcp_screen_stocks_filters_and_rsi.py tests/test_screener_service.py -v`
Expected: Some tests may fail due to monkeypatch targets. Note which tests fail — they are fixed in Task 8.5.

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/screening/__init__.py app/mcp_server/tooling/analysis_screen_core.py tests/test_screening_compat.py
git commit -m "refactor: replace analysis_screen_core.py with backward-compat shim"
```

---

### Task 8.5: Fix monkeypatch targets in existing tests

**Files:**
- Modify: `tests/_mcp_screen_stocks_support.py`
- Modify: `tests/_mcp_tooling_support.py`
- Modify: `tests/test_mcp_screen_stocks_filters_and_rsi.py`
- Modify: `tests/test_mcp_screen_stocks_crypto.py`
- Modify: `tests/test_mcp_recommend_flow.py`
- Modify: `tests/test_tvscreener_stocks.py`
- Modify: `tests/test_tvscreener_crypto.py`
- Modify: `tests/test_crypto_composite_score.py`
- Modify: `tests/_mcp_recommend_support.py` (if needed)

After the shim replaces the monolithic file, `monkeypatch.setattr(analysis_screen_core, ...)` no longer patches the actual code. The shim re-exports functions defined in the screening package, but does **NOT** re-export third-party imports (`TvScreenerService`, `upbit_service`, `yf`, `_import_tvscreener`, etc.). Any `monkeypatch.setattr` or `patch()` targeting these on the shim module will fail with `AttributeError`.

Each test must patch at the **new** module path where the name is actually imported/used.

- [ ] **Step 1: Identify all monkeypatch/patch targets**

Search for both patterns across all test files:
- `monkeypatch.setattr.*analysis_screen_core`
- `patch("app.mcp_server.tooling.analysis_screen_core.`

For each occurrence, determine the correct new module path:

| Old patch target | New patch target |
|---|---|
| `analysis_screen_core._screen_kr` | `screening.kr._screen_kr` |
| `analysis_screen_core._screen_us` | `screening.us._screen_us` |
| `analysis_screen_core._calculate_rsi` | Patch at point of use: `screening.kr._calculate_rsi` / `screening.us._calculate_rsi` |
| `analysis_screen_core.asyncio` | Patch at point of use: `screening.kr.asyncio` / `screening.us.asyncio` / `screening.crypto.asyncio` |
| `analysis_screen_core.yf` | `screening.us.yf` |
| `analysis_screen_core._CRYPTO_MARKET_CAP_CACHE` | `screening.crypto._CRYPTO_MARKET_CAP_CACHE` |
| `analysis_screen_core.TvScreenerService` | `screening.kr.TvScreenerService` / `screening.us.TvScreenerService` / `screening.crypto.TvScreenerService` (patch at point of use) |
| `analysis_screen_core._import_tvscreener` | `screening.tvscreener_support._import_tvscreener` / `screening.kr._import_tvscreener` etc. |
| `analysis_screen_core.upbit_service` | `screening.crypto.upbit_service` |
| `analysis_screen_core.get_upbit_warning_markets` | `screening.crypto.get_upbit_warning_markets` |
| `analysis_screen_core.get_upbit_market_display_names` | `screening.crypto.get_upbit_market_display_names` / `screening.enrichment.get_upbit_market_display_names` |
| `analysis_screen_core.compute_crypto_realtime_rsi_map` | `screening.crypto.compute_crypto_realtime_rsi_map` |
| `analysis_screen_core._fetch_ohlcv_for_indicators` | `screening.crypto._fetch_ohlcv_for_indicators` |
| `analysis_screen_core.fetch_stock_all_cached` | `screening.kr.fetch_stock_all_cached` |
| `analysis_screen_core.fetch_valuation_all_cached` | `screening.kr.fetch_valuation_all_cached` |

- [ ] **Step 2: Update each test file**

For each test file, update the import and monkeypatch calls. Example pattern:

```python
# Before:
from app.mcp_server.tooling import analysis_screen_core
monkeypatch.setattr(analysis_screen_core, "_screen_kr", mock_fn)

# After:
from app.mcp_server.tooling.screening import kr as screening_kr
monkeypatch.setattr(screening_kr, "_screen_kr", mock_fn)
```

For `_CRYPTO_MARKET_CAP_CACHE`:
```python
# Before:
analysis_screen_core._CRYPTO_MARKET_CAP_CACHE.clear()

# After:
from app.mcp_server.tooling.screening.crypto import _CRYPTO_MARKET_CAP_CACHE
_CRYPTO_MARKET_CAP_CACHE.clear()
```

For string-based `patch()` (e.g. in tvscreener test files):
```python
# Before:
@patch("app.mcp_server.tooling.analysis_screen_core.TvScreenerService")
@patch("app.mcp_server.tooling.analysis_screen_core.fetch_stock_all_cached")

# After:
@patch("app.mcp_server.tooling.screening.kr.TvScreenerService")
@patch("app.mcp_server.tooling.screening.kr.fetch_stock_all_cached")
```

For `test_crypto_composite_score.py`:
```python
# Before:
monkeypatch.setattr(analysis_screen_core, "_fetch_ohlcv_for_indicators", mock)

# After:
from app.mcp_server.tooling.screening import crypto as screening_crypto
monkeypatch.setattr(screening_crypto, "_fetch_ohlcv_for_indicators", mock)
```

- [ ] **Step 3: Run all screening tests**

Run: `uv run pytest tests/test_analysis_screen_core.py tests/test_tvscreener_stocks.py tests/test_tvscreener_crypto.py tests/test_mcp_screen_stocks_kr.py tests/test_mcp_screen_stocks_crypto.py tests/test_mcp_screen_stocks_filters_and_rsi.py tests/test_screener_service.py tests/test_mcp_recommend_flow.py tests/test_crypto_composite_score.py -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "fix: update monkeypatch targets to new screening module paths"
```

---

### Task 9: Lint and type-check verification

**Files:** None created — verification only.

- [ ] **Step 1: Run Ruff linter**

Run: `make lint`
Expected: No errors. Fix any import-order or unused-import issues.

- [ ] **Step 2: Run type checker**

Run: `make typecheck`
Expected: No new errors. Fix any typing issues in the new modules.

- [ ] **Step 3: Run full test suite**

Run: `make test`
Expected: ALL PASS

- [ ] **Step 4: Commit any lint/type fixes**

```bash
git add -u
git commit -m "fix: resolve lint and type issues from screening refactor"
```

---

### Task 10: (Optional) Migrate direct consumers to new import paths

**Files:**
- Modify: `app/mcp_server/tooling/analysis_screening.py`
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py`
- Modify: `app/services/screener_service.py`

This task is optional — the compat shim makes it non-urgent. Do it when convenient.

- [ ] **Step 1: Update `analysis_screening.py` imports**

Replace:
```python
from app.mcp_server.tooling.analysis_screen_core import (
    _build_screen_response as build_screen_response,
    _normalize_asset_type,
    _normalize_screen_market,
    _normalize_sort_by,
    _normalize_sort_order,
    _screen_crypto,
    _screen_kr,
    _validate_screen_filters,
    normalize_screen_request,
    screen_stocks_unified,
)
```

With:
```python
from app.mcp_server.tooling.screening.common import (
    _build_screen_response as build_screen_response,
    _normalize_asset_type,
    _normalize_screen_market,
    _normalize_sort_by,
    _normalize_sort_order,
    _validate_screen_filters,
    normalize_screen_request,
)
from app.mcp_server.tooling.screening.crypto import _screen_crypto
from app.mcp_server.tooling.screening.entrypoint import screen_stocks_unified
from app.mcp_server.tooling.screening.kr import _screen_kr
```

- [ ] **Step 2: Update `analysis_tool_handlers.py` import**

Replace:
```python
from app.mcp_server.tooling.analysis_screen_core import normalize_screen_request
```
With:
```python
from app.mcp_server.tooling.screening.common import normalize_screen_request
```

- [ ] **Step 3: Update `screener_service.py` import**

Replace:
```python
from app.mcp_server.tooling.analysis_screen_core import normalize_screen_request
```
With:
```python
from app.mcp_server.tooling.screening.common import normalize_screen_request
```

- [ ] **Step 4: Run tests**

Run: `make test`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/analysis_screening.py app/mcp_server/tooling/analysis_tool_handlers.py app/services/screener_service.py
git commit -m "refactor: migrate consumers to screening package imports"
```
