"""Tests for fill notification normalization and formatting."""

from app.services.fill_notification import (
    FillOrder,
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

    def test_normalize_missing_fields_best_effort(self) -> None:
        order = normalize_kis_fill({})

        assert order.symbol == "UNKNOWN"
        assert order.side == "unknown"
        assert order.filled_price == 0
        assert order.filled_qty == 0
        assert order.filled_amount == 0
        assert order.account == "kis"
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
            "계좌: upbit"
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
            "주문: a3f5d2e1..."
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
            "계좌: upbit"
        )
        assert message == expected
