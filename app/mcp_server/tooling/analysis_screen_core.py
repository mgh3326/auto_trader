"""Backward-compatibility shim — re-exports from the screening package.

All logic has been moved to ``app.mcp_server.tooling.screening.*``.
This module exists only so that existing ``from analysis_screen_core import X``
statements continue to work.
"""

from __future__ import annotations

# --- common -------------------------------------------------------------------
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
    _finalize_rsi_enrichment_diagnostics,
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
    is_safe_drop,
    normalize_screen_request,
)

# --- crypto -------------------------------------------------------------------
from app.mcp_server.tooling.screening.crypto import (  # noqa: F401
    _CRYPTO_MARKET_CAP_CACHE,
    _run_crypto_coingecko_fetch,
    _run_crypto_indicator_enrichment,
    _screen_crypto_via_tvscreener,
    _screen_crypto_with_fallback,
)

# --- enrichment ---------------------------------------------------------------
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

# --- entrypoint ---------------------------------------------------------------
from app.mcp_server.tooling.screening.entrypoint import (  # noqa: F401
    screen_stocks_unified,
)

# --- kr -----------------------------------------------------------------------
from app.mcp_server.tooling.screening.kr import (  # noqa: F401
    _screen_kr,
    _screen_kr_via_tvscreener,
    _screen_kr_with_fallback,
)

# --- tvscreener support -------------------------------------------------------
from app.mcp_server.tooling.screening.tvscreener_support import (  # noqa: F401
    _adapt_tvscreener_stock_response,
    _can_use_tvscreener_stock_path,
    _get_tvscreener_stock_capability_snapshot,
    _map_tvscreener_stock_row,
    _required_tvscreener_stock_capabilities,
)

# --- us -----------------------------------------------------------------------
from app.mcp_server.tooling.screening.us import (  # noqa: F401
    _screen_us,
    _screen_us_via_tvscreener,
    _screen_us_with_fallback,
)
