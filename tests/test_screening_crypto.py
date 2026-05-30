# tests/test_screening_crypto.py
"""Verify crypto screening functions are importable from the new location."""

import pytest


class _FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def iterrows(self):
        yield from enumerate(self._rows)


class _CooldownStub:
    async def filter_symbols_in_cooldown(self, symbols):
        return set()


class TestCryptoScreeningImports:
    def test_screen_crypto_via_tvscreener_importable(self):
        from app.mcp_server.tooling.screening.crypto import (
            _screen_crypto_via_tvscreener,
        )

        assert callable(_screen_crypto_via_tvscreener)

    def test_screen_crypto_with_fallback_importable(self):
        from app.mcp_server.tooling.screening.crypto import _screen_crypto_with_fallback

        assert callable(_screen_crypto_with_fallback)

    def test_crypto_market_cap_cache(self):
        from app.mcp_server.tooling.screening.crypto import _CRYPTO_MARKET_CAP_CACHE

        assert _CRYPTO_MARKET_CAP_CACHE is not None


class TestCryptoScreeningPhases:
    def test_build_crypto_filters_importable(self):
        from app.mcp_server.tooling.screening.crypto import _build_crypto_filters

        assert callable(_build_crypto_filters)

    def test_execute_crypto_query_importable(self):
        from app.mcp_server.tooling.screening.crypto import _execute_crypto_query

        assert callable(_execute_crypto_query)

    def test_normalize_crypto_results_importable(self):
        from app.mcp_server.tooling.screening.crypto import _normalize_crypto_results

        assert callable(_normalize_crypto_results)

    def test_is_upbit_krw_market_code_excludes_non_krw_quotes(self):
        from app.mcp_server.tooling.screening.crypto import _is_upbit_krw_market_code

        assert _is_upbit_krw_market_code("KRW-BTC")
        assert not _is_upbit_krw_market_code("USDT-OP")
        assert not _is_upbit_krw_market_code("BTC-PRL")

    def test_resolve_upbit_market_code_accepts_tvscreener_and_upbit_rows(self):
        from app.mcp_server.tooling.screening.crypto import (
            _resolve_upbit_market_code_from_row,
        )

        assert (
            _resolve_upbit_market_code_from_row({"symbol": "UPBIT:BTCKRW"}) == "KRW-BTC"
        )
        assert _resolve_upbit_market_code_from_row({"market": "KRW-ETH"}) == "KRW-ETH"
        assert _resolve_upbit_market_code_from_row({"symbol": "USDT-OP"}) == "USDT-OP"

    @pytest.mark.asyncio
    async def test_normalize_crypto_results_filters_non_krw_upbit_pairs(
        self, monkeypatch
    ):
        from app.mcp_server.tooling.screening import crypto

        async def fake_names(markets, db):
            return {
                "KRW-IMX": {
                    "korean_name": "이뮤터블엑스",
                    "english_name": "Immutable X",
                }
            }

        async def fake_warning_markets(quote_currency, db):
            return set()

        async def fake_tickers(markets):
            return [{"market": "KRW-IMX", "acc_trade_volume_24h": 1234}]

        async def fake_coingecko():
            return {
                "data": {},
                "cached": False,
                "age_seconds": None,
                "stale": False,
                "error": None,
            }

        monkeypatch.setattr(crypto, "get_upbit_market_display_names", fake_names)
        monkeypatch.setattr(crypto, "get_upbit_warning_markets", fake_warning_markets)
        monkeypatch.setattr(
            crypto.upbit_service, "fetch_multiple_tickers", fake_tickers
        )
        monkeypatch.setattr(crypto, "_run_crypto_coingecko_fetch", fake_coingecko)
        monkeypatch.setattr(
            crypto, "_get_crypto_trade_cooldown_service", lambda: _CooldownStub()
        )

        payload = await crypto._normalize_crypto_results(
            _FakeFrame(
                [
                    {
                        "symbol": "UPBIT:OPUSDT",
                        "name": "Optimism",
                        "price": 0.16,
                        "change_percent": 2.0,
                        "value_traded": 9000,
                    },
                    {
                        "symbol": "UPBIT:PRLBTC",
                        "name": "PRL",
                        "price": 0.0,
                        "change_percent": -0.2,
                        "value_traded": 8000,
                    },
                    {
                        "symbol": "UPBIT:IMXKRW",
                        "name": "Immutable X",
                        "price": 277,
                        "change_percent": 0.36,
                        "value_traded": 7000,
                    },
                ]
            ),
            market="crypto",
            max_rsi=None,
            min_market_cap=None,
            filters_applied={"sort_by": "trade_amount", "sort_order": "desc"},
            limit=20,
        )

        assert [row["symbol"] for row in payload["results"]] == ["KRW-IMX"]
        assert all(row["symbol"].startswith("KRW-") for row in payload["results"])
        warnings = payload.get("warnings", [])
        assert not any("KRW-BTC ticker not found" in warning for warning in warnings)
        # ROB-369 B4 — the BTC-default crash-filter warning is only emitted when
        # a crash candidate (≤ -30%) is present AND the KRW-BTC reference can't be
        # recovered. KRW-IMX here is +36% (no crash candidate), so the reference
        # is irrelevant and no warning is raised (previously it was noise).
        assert not any("BTC 기준 데이터가 없어" in warning for warning in warnings)
