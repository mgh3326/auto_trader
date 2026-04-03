"""Tests for fill notification normalization and formatting."""

import pytest

from app.services.fill_notification import (
    FillOrder,
    coerce_fill_order,
    format_fill_message,
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
        assert order.filled_qty == 0.1
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
        assert order.filled_price == 195.5
        assert order.filled_qty == 2
        assert order.filled_amount == 391.0
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


class TestFormatFillMessage:
    """체결 알림 메시지 포맷 테스트"""

    def test_format_case1_basic_required_fields_only(self) -> None:
        order = FillOrder(
            symbol="KRW-BTC",
            side="bid",
            filled_price=100_020_000,
            filled_qty=0.015,
            filled_amount=1_500_300,
            filled_at="2026-02-14T17:30:45",
            account="upbit",
        )
        message = format_fill_message(order)

        expected = (
            "🟢 체결 알림\n\n"
            "종목: KRW-BTC\n"
            "구분: 매수 체결\n"
            "체결가: 100,020,000원\n"
            "수량: 0.015\n"
            "금액: 1,500,300원\n"
            "시간: 2026-02-14T17:30:45\n\n"
            "계좌: upbit\n"
            "상세: https://mgh3326.duckdns.org/portfolio/positions/crypto/KRW-BTC"
        )
        assert message == expected

    def test_format_case2_with_order_price_and_order_id(self) -> None:
        order = FillOrder(
            symbol="KRW-BTC",
            side="bid",
            filled_price=100_020_000,
            filled_qty=0.015,
            filled_amount=1_500_300,
            filled_at="2026-02-14T17:30:45",
            account="upbit",
            order_price=100_000_000,
            order_id="a3f5d2e1-aaaa-bbbb-cccc",
            order_type="limit",
        )
        message = format_fill_message(order)

        expected = (
            "🟢 체결 알림\n\n"
            "종목: KRW-BTC\n"
            "구분: 매수 체결\n"
            "체결가: 100,020,000원 (+0.02%)\n"
            "수량: 0.015\n"
            "금액: 1,500,300원\n"
            "시간: 2026-02-14T17:30:45\n\n"
            "계좌: upbit\n"
            "주문: a3f5d2e1...\n"
            "상세: https://mgh3326.duckdns.org/portfolio/positions/crypto/KRW-BTC"
        )
        assert message == expected

    def test_format_case3_sell_with_negative_diff(self) -> None:
        message = format_fill_message(
            {
                "symbol": "KRW-ETH",
                "side": "ask",
                "filled_price": 3_010_000,
                "filled_qty": 0.5,
                "filled_amount": 1_505_000,
                "filled_at": "2026-02-14T18:15:22",
                "account": "upbit",
                "order_price": 3_011_500,
            }
        )

        expected = (
            "🔴 체결 알림\n\n"
            "종목: KRW-ETH\n"
            "구분: 매도 체결\n"
            "체결가: 3,010,000원 (-0.05%)\n"
            "수량: 0.5\n"
            "금액: 1,505,000원\n"
            "시간: 2026-02-14T18:15:22\n\n"
            "계좌: upbit\n"
            "상세: https://mgh3326.duckdns.org/portfolio/positions/crypto/KRW-ETH"
        )
        assert message == expected

    def test_format_partial_fill_displays_partial_status_text(self) -> None:
        order = FillOrder(
            symbol="AAPL",
            side="bid",
            filled_price=195.5,
            filled_qty=2,
            filled_amount=391,
            filled_at="2026-02-14T09:30:00-05:00",
            account="kis",
            fill_status="partial",
        )

        message = format_fill_message(order)

        assert "🟢 체결 알림" in message
        assert "구분: 매수 부분체결" in message

    def test_format_us_fill_uses_usd_currency_output(self) -> None:
        order = FillOrder(
            symbol="BAC",
            side="bid",
            filled_price=47.9,
            filled_qty=23,
            filled_amount=1101.7,
            filled_at="2026-02-14T09:30:00-05:00",
            account="kis",
            market_type="us",
            currency="USD",
        )

        message = format_fill_message(order)

        assert "체결가: $47.90" in message
        assert "금액: $1,101.70" in message

    def test_format_us_fill_normalizes_lowercase_currency_on_fill_order_input(
        self,
    ) -> None:
        order = FillOrder(
            symbol="BAC",
            side="bid",
            filled_price=47.9,
            filled_qty=23,
            filled_amount=1101.7,
            filled_at="2026-02-14T09:30:00-05:00",
            account="kis",
            market_type="us",
            currency="usd",
        )

        message = format_fill_message(order)

        assert "체결가: $47.90" in message
        assert "금액: $1,101.70" in message

    def test_format_fill_message_appends_position_detail_url(self) -> None:
        order = FillOrder(
            symbol="AAPL",
            side="bid",
            filled_price=150.0,
            filled_qty=10,
            filled_amount=1500.0,
            filled_at="2026-04-03T10:00:00",
            account="kis",
            market_type="us",
        )
        message = format_fill_message(order)
        assert (
            "상세: https://mgh3326.duckdns.org/portfolio/positions/us/AAPL" in message
        )
