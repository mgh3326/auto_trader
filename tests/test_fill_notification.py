"""Tests for fill notification normalization and formatting."""

import pytest

from app.core.config import settings
from app.services.fill_notification import (
    FillOrder,
    coerce_fill_order,
    normalize_kis_fill,
    normalize_upbit_fill,
)


class TestNormalizeUpbitFill:
    """Upbit 체결 데이터 정규화 테스트"""

    def test_normalize_basic(self) -> None:
        raw = {
            "code": "KRW-BTC",
            "ask_bid": "BID",
            "trade_price": 50_000_000,
            "trade_volume": 0.1,
            "trade_timestamp": 1_700_000_000_000,
        }
        order = normalize_upbit_fill(raw)

        assert order.symbol == "KRW-BTC"
        assert order.side == "bid"
        assert order.filled_price == 50_000_000
        assert order.filled_qty == pytest.approx(0.1)
        assert order.filled_amount == 5_000_000
        assert order.account == "upbit"
        assert order.market_type == "crypto"

    def test_normalize_with_order_metadata(self) -> None:
        raw = {
            "code": "KRW-ETH",
            "ask_bid": "ASK",
            "trade_price": 3_000_000,
            "trade_volume": 0.5,
            "order_price": 3_001_500,
            "uuid": "order-abc123",
            "order_type": "limit",
            "created_at": "2026-02-14T18:15:22",
        }
        order = normalize_upbit_fill(raw)

        assert order.side == "ask"
        assert order.order_id == "order-abc123"
        assert order.order_type == "limit"
        assert order.order_price == 3_001_500
        assert order.filled_at == "2026-02-14T18:15:22"


class TestNormalizeKisFill:
    """KIS 체결 데이터 정규화 테스트"""

    def test_normalize_domestic_fields(self) -> None:
        raw = {
            "pdno": "005930",
            "sll_buy_dvsn_cd": "02",
            "ccld_unpr": "70000",
            "ccld_qty": "10",
            "ord_no": "A123456789",
            "ord_tmd": "093001",
            "account": "kis",
        }
        order = normalize_kis_fill(raw)

        assert order.symbol == "005930"
        assert order.side == "bid"
        assert order.filled_price == 70_000
        assert order.filled_qty == 10
        assert order.filled_amount == 700_000
        assert order.order_id == "A123456789"
        assert order.account == "kis"
        assert order.market_type == "kr"

    def test_normalize_overseas_fields(self) -> None:
        raw = {
            "symbol": "AAPL",
            "sll_buy_dvsn_cd": "01",
            "ft_ccld_unpr3": "195.5",
            "ft_ccld_qty": "2",
            "ft_ccld_amt3": "391.0",
            "ord_no": "US-ORDER-1234",
            "filled_at": "2026-02-14T09:30:00-05:00",
        }
        order = normalize_kis_fill(raw)

        assert order.symbol == "AAPL"
        assert order.side == "ask"
        assert order.filled_price == pytest.approx(195.5)
        assert order.filled_qty == 2
        assert order.filled_amount == pytest.approx(391.0)
        assert order.order_id == "US-ORDER-1234"
        assert order.account == "kis"
        assert order.market_type == "us"

    def test_normalize_overseas_fields_preserve_explicit_currency(self) -> None:
        raw = {
            "symbol": "BAC",
            "side": "02",
            "filled_price": "47.9",
            "filled_qty": "23",
            "filled_amount": "1101.7",
            "filled_at": "2026-02-14T09:30:00-05:00",
            "market": "us",
            "currency": "USD",
        }

        order = normalize_kis_fill(raw)

        assert order.market_type == "us"
        assert order.currency == "USD"

    def test_normalize_overseas_fields_defaults_currency_to_usd_for_us_market(
        self,
    ) -> None:
        raw = {
            "symbol": "BAC",
            "side": "02",
            "filled_price": "47.9",
            "filled_qty": "23",
            "filled_amount": "1101.7",
            "filled_at": "2026-02-14T09:30:00-05:00",
            "market_type": "us",
        }

        order = normalize_kis_fill(raw)

        assert order.market_type == "us"
        assert order.currency == "USD"

    def test_normalize_kis_fill_prefers_explicit_market_field_over_symbol_inference(
        self,
    ) -> None:
        raw = {
            "pdno": "005930",
            "market": "NASDAQ",
            "sll_buy_dvsn_cd": "02",
            "ccld_unpr": "70000",
            "ccld_qty": "1",
        }

        order = normalize_kis_fill(raw)

        assert order.market_type == "us"

    def test_normalize_kis_fill_propagates_execution_status_to_fill_status(
        self,
    ) -> None:
        raw = {
            "symbol": "AAPL",
            "side": "02",
            "filled_price": "195.5",
            "filled_qty": "2",
            "execution_status": "partial",
        }

        order = normalize_kis_fill(raw)

        assert order.fill_status == "partial"

    def test_coerce_fill_order_prefers_explicit_fill_status(self) -> None:
        order = coerce_fill_order(
            {
                "symbol": "AAPL",
                "side": "02",
                "filled_price": 100,
                "filled_qty": 1,
                "filled_amount": 100,
                "filled_at": "2026-02-14T09:30:00",
                "account": "kis",
                "fill_status": "partial",
                "execution_status": "filled",
            }
        )

        assert order.fill_status == "partial"

    def test_coerce_fill_order_prefers_market_type_over_market_alias(self) -> None:
        order = coerce_fill_order(
            {
                "symbol": "005930",
                "side": "02",
                "filled_price": 100,
                "filled_qty": 1,
                "filled_amount": 100,
                "filled_at": "2026-02-14T09:30:00",
                "account": "kis",
                "market_type": "kr",
                "market": "NASDAQ",
            }
        )

        assert order.market_type == "kr"

    def test_coerce_fill_order_preserves_explicit_currency(self) -> None:
        order = coerce_fill_order(
            {
                "symbol": "BAC",
                "side": "02",
                "filled_price": 47.9,
                "filled_qty": 23,
                "filled_amount": 1101.7,
                "filled_at": "2026-02-14T09:30:00",
                "account": "kis",
                "market_type": "us",
                "currency": "USD",
            }
        )

        assert order.currency == "USD"

    @pytest.mark.parametrize(
        ("market", "expected_market_type"),
        [("kr", "kr"), ("us", "us"), ("NASDAQ", "us")],
    )
    def test_coerce_fill_order_normalizes_raw_market_aliases(
        self, market: str, expected_market_type: str
    ) -> None:
        order = coerce_fill_order(
            {
                "symbol": "005930" if expected_market_type == "kr" else "AAPL",
                "side": "02",
                "filled_price": 100,
                "filled_qty": 1,
                "filled_amount": 100,
                "filled_at": "2026-02-14T09:30:00",
                "account": "kis",
                "market": market,
            }
        )

        assert order.market_type == expected_market_type

    def test_coerce_fill_order_uses_upbit_account_market_fallback(self) -> None:
        order = coerce_fill_order(
            {
                "symbol": "KRW-BTC",
                "side": "BID",
                "filled_price": 100,
                "filled_qty": 1,
                "filled_amount": 100,
                "filled_at": "2026-02-14T09:30:00",
                "account": "upbit",
            }
        )

        assert order.market_type == "crypto"

    @pytest.mark.parametrize(
        ("symbol", "expected_market_type"),
        [("005930", "kr"), ("AAPL", "us")],
    )
    def test_coerce_fill_order_infers_safe_kis_market_from_symbol_shape(
        self, symbol: str, expected_market_type: str
    ) -> None:
        order = coerce_fill_order(
            {
                "symbol": symbol,
                "side": "02",
                "filled_price": 100,
                "filled_qty": 1,
                "filled_amount": 100,
                "filled_at": "2026-02-14T09:30:00",
                "account": "kis",
            }
        )

        assert order.market_type == expected_market_type

    def test_coerce_fill_order_keeps_unknown_market_for_crypto_like_symbol_without_hint(
        self,
    ) -> None:
        order = coerce_fill_order(
            {
                "symbol": "KRW-BTC",
                "side": "BID",
                "filled_price": 100,
                "filled_qty": 1,
                "filled_amount": 100,
                "filled_at": "2026-02-14T09:30:00",
                "account": "unknown",
            }
        )

        assert order.market_type is None

    def test_coerce_fill_order_missing_symbol_does_not_infer_us_market(self) -> None:
        order = coerce_fill_order(
            {
                "side": "02",
                "filled_price": 100,
                "filled_qty": 1,
                "filled_amount": 100,
                "filled_at": "2026-02-14T09:30:00",
                "account": "kis",
            }
        )

        assert order.symbol == "UNKNOWN"
        assert order.market_type is None

    @pytest.mark.parametrize("symbol", ["PROD", "ENV", "ORDER123"])
    def test_coerce_fill_order_does_not_infer_us_market_from_reserved_tokens(
        self, symbol: str
    ) -> None:
        order = coerce_fill_order(
            {
                "symbol": symbol,
                "side": "02",
                "filled_price": 100,
                "filled_qty": 1,
                "filled_amount": 100,
                "filled_at": "2026-02-14T09:30:00",
                "account": "kis",
            }
        )

        assert order.market_type is None
        assert order.currency is None

    def test_normalize_missing_fields_best_effort(self) -> None:
        order = normalize_kis_fill({})

        assert order.symbol == "UNKNOWN"
        assert order.side == "unknown"
        assert order.filled_price == 0
        assert order.filled_qty == 0
        assert order.filled_amount == 0
        assert order.account == "kis"
        assert order.market_type is None
        assert order.filled_at


# TestFormatFillMessage class removed as part of redesign


@pytest.mark.unit
class TestFillHelpers:
    def test_currency_aware_threshold(self):
        from app.services.fill_notification import (
            FillOrder,
            is_fill_notifiable,
        )

        krw = FillOrder(symbol="005930", side="bid", filled_price=1000,
                        filled_qty=49, filled_amount=49_000, filled_at="t",
                        account="kis", market_type="kr", currency="KRW")
        assert is_fill_notifiable(krw) is False
        krw2 = FillOrder(symbol="005930", side="bid", filled_price=1000,
                         filled_qty=50, filled_amount=50_000, filled_at="t",
                         account="kis", market_type="kr", currency="KRW")
        assert is_fill_notifiable(krw2) is True
        usd = FillOrder(symbol="AAPL", side="bid", filled_price=10,
                        filled_qty=6, filled_amount=60, filled_at="t",
                        account="kis", market_type="us", currency="USD")
        assert is_fill_notifiable(usd) is True

    def test_resolve_display_name_crypto(self):
        from app.services.fill_notification import (
            FillOrder,
            resolve_fill_display_name,
        )

        order = FillOrder(symbol="KRW-BTC", side="bid", filled_price=1, filled_qty=1,
                          filled_amount=1, filled_at="t", account="upbit", market_type="crypto")
        assert resolve_fill_display_name(order) == "BTC"

    def test_money_and_qty_fmt(self):
        from app.services.fill_notification import (
            format_fill_money,
            format_fill_quantity,
        )

        assert format_fill_money(68500, is_usd=False) == "68,500원"
        assert format_fill_money(12.5, is_usd=True) == "$12.50"
        assert format_fill_quantity(10.0) == "10"

    def test_enrichment_defaults(self):
        from app.services.fill_notification import FillEnrichment

        enr = FillEnrichment()
        assert enr.position_qty is None
        assert enr.is_approximate is True

