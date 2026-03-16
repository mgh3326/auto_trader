from __future__ import annotations

import pytest


@pytest.mark.unit
class TestFmtPrice:
    """Price formatting: KRW uses 억/만/comma tiers, USD uses $-prefix."""

    # --- KRW ---
    def test_krw_below_10000_uses_comma(self) -> None:
        from app.services.n8n_formatting import fmt_price

        assert fmt_price(2470) == "2,470"

    def test_krw_exact_boundary_10000(self) -> None:
        from app.services.n8n_formatting import fmt_price

        assert fmt_price(10000) == "1.0만"

    def test_krw_10000_to_1m(self) -> None:
        from app.services.n8n_formatting import fmt_price

        assert fmt_price(16500) == "1.65만"
        assert fmt_price(70000) == "7.0만"
        assert fmt_price(999999) == "100.0만"

    def test_krw_1m_to_100m(self) -> None:
        from app.services.n8n_formatting import fmt_price

        assert fmt_price(1080000) == "108.0만"
        assert fmt_price(5_500_000) == "550.0만"
        assert fmt_price(99_999_999) == "10000.0만"

    def test_krw_100m_plus_uses_eok(self) -> None:
        from app.services.n8n_formatting import fmt_price

        assert fmt_price(108_000_000) == "1.08억"
        assert fmt_price(148_500_000) == "1.49억"
        assert fmt_price(1_500_000_000) == "15.0억"

    def test_krw_zero(self) -> None:
        from app.services.n8n_formatting import fmt_price

        assert fmt_price(0) == "0"

    def test_krw_small_decimal(self) -> None:
        from app.services.n8n_formatting import fmt_price

        assert fmt_price(999) == "999"

    # --- USD ---
    def test_usd_1000_plus_no_decimals(self) -> None:
        from app.services.n8n_formatting import fmt_price

        assert fmt_price(1234.0, "USD") == "$1,234"

    def test_usd_below_1000_two_decimals(self) -> None:
        from app.services.n8n_formatting import fmt_price

        assert fmt_price(12.5, "USD") == "$12.50"
        assert fmt_price(180.5, "USD") == "$180.50"

    def test_usd_zero(self) -> None:
        from app.services.n8n_formatting import fmt_price

        assert fmt_price(0, "USD") == "$0.00"

    # --- None handling ---
    def test_none_returns_dash(self) -> None:
        from app.services.n8n_formatting import fmt_price

        assert fmt_price(None) == "-"


@pytest.mark.unit
class TestFmtGap:
    """Gap percentage with sign prefix."""

    def test_positive(self) -> None:
        from app.services.n8n_formatting import fmt_gap

        assert fmt_gap(14.0) == "+14.0%"

    def test_negative(self) -> None:
        from app.services.n8n_formatting import fmt_gap

        assert fmt_gap(-3.2) == "-3.2%"

    def test_zero(self) -> None:
        from app.services.n8n_formatting import fmt_gap

        assert fmt_gap(0.0) == "0.0%"

    def test_none_returns_dash(self) -> None:
        from app.services.n8n_formatting import fmt_gap

        assert fmt_gap(None) == "-"


@pytest.mark.unit
class TestFmtAmount:
    """Amount formatting in KRW: uses 만 for >= 10,000."""

    def test_above_10000_uses_man(self) -> None:
        from app.services.n8n_formatting import fmt_amount

        assert fmt_amount(312000) == "31.2만"
        assert fmt_amount(6480000) == "648.0만"

    def test_large_amount(self) -> None:
        from app.services.n8n_formatting import fmt_amount

        assert fmt_amount(34603720) == "3,460.4만"

    def test_below_10000_uses_comma(self) -> None:
        from app.services.n8n_formatting import fmt_amount

        assert fmt_amount(5000) == "5,000"

    def test_zero(self) -> None:
        from app.services.n8n_formatting import fmt_amount

        assert fmt_amount(0) == "0"

    def test_none_returns_dash(self) -> None:
        from app.services.n8n_formatting import fmt_amount

        assert fmt_amount(None) == "-"


@pytest.mark.unit
class TestFmtAge:
    """Age formatting: days if >= 24h, otherwise hours."""

    def test_days(self) -> None:
        from app.services.n8n_formatting import fmt_age

        assert fmt_age(24) == "1일"
        assert fmt_age(72) == "3일"

    def test_hours(self) -> None:
        from app.services.n8n_formatting import fmt_age

        assert fmt_age(5) == "5시간"
        assert fmt_age(0) == "0시간"

    def test_23_hours(self) -> None:
        from app.services.n8n_formatting import fmt_age

        assert fmt_age(23) == "23시간"


@pytest.mark.unit
class TestBuildSummaryLine:
    """One-line order summary string."""

    def test_full_order(self) -> None:
        from app.services.n8n_formatting import build_summary_line

        order = {
            "symbol": "APT",
            "side": "buy",
            "order_price": 2470,
            "current_price": 2166,
            "gap_pct": -12.3,
            "amount_krw": 312000,
            "age_hours": 25,
            "currency": "KRW",
        }
        result = build_summary_line(order)
        assert result == "APT buy @2,470 (현재 2,166, -12.3%, 31.2만, 1일)"

    def test_missing_current_price(self) -> None:
        from app.services.n8n_formatting import build_summary_line

        order = {
            "symbol": "BTC",
            "side": "sell",
            "order_price": 148_500_000,
            "current_price": None,
            "gap_pct": None,
            "amount_krw": 297000,
            "age_hours": 6,
            "currency": "KRW",
        }
        result = build_summary_line(order)
        assert result == "BTC sell @1.49억 (현재 -, -, 29.7만, 6시간)"

    def test_usd_order(self) -> None:
        from app.services.n8n_formatting import build_summary_line

        order = {
            "symbol": "AAPL",
            "side": "buy",
            "order_price": 180.5,
            "current_price": 181.0,
            "gap_pct": 0.28,
            "amount_krw": 1_264_000,
            "age_hours": 3,
            "currency": "USD",
        }
        result = build_summary_line(order)
        assert result == "AAPL buy @$180.50 (현재 $181.00, +0.3%, 126.4만, 3시간)"


@pytest.mark.unit
class TestBuildSummaryTitle:
    """Summary title line for the response."""

    def test_title_format(self) -> None:
        from app.services.n8n_formatting import build_summary_title

        from datetime import datetime

        as_of = datetime.fromisoformat("2026-03-16T16:00:00+09:00")
        result = build_summary_title(total=13, buy_count=4, sell_count=9, as_of=as_of)
        assert result == "📋 미체결 리뷰 — 03/16 (13건, 매수 4 / 매도 9)"

    def test_title_zero_orders(self) -> None:
        from app.services.n8n_formatting import build_summary_title

        from datetime import datetime

        as_of = datetime.fromisoformat("2026-03-16T10:00:00+09:00")
        result = build_summary_title(total=0, buy_count=0, sell_count=0, as_of=as_of)
        assert result == "📋 미체결 리뷰 — 03/16 (0건, 매수 0 / 매도 0)"


@pytest.mark.unit
class TestSchemaFmtFields:
    """Verify _fmt fields exist on the Pydantic schemas."""

    def test_order_item_has_fmt_fields(self) -> None:
        from app.schemas.n8n import N8nPendingOrderItem

        item = N8nPendingOrderItem(
            order_id="test",
            symbol="BTC",
            raw_symbol="KRW-BTC",
            market="crypto",
            side="buy",
            status="pending",
            order_price=100.0,
            quantity=1.0,
            remaining_qty=1.0,
            created_at="2026-03-16T10:00:00+09:00",
            age_hours=1,
            age_days=0,
            currency="KRW",
            # _fmt fields default to None
        )
        assert item.order_price_fmt is None
        assert item.current_price_fmt is None
        assert item.gap_pct_fmt is None
        assert item.amount_fmt is None
        assert item.age_fmt is None
        assert item.summary_line is None

    def test_order_item_accepts_fmt_values(self) -> None:
        from app.schemas.n8n import N8nPendingOrderItem

        item = N8nPendingOrderItem(
            order_id="test",
            symbol="BTC",
            raw_symbol="KRW-BTC",
            market="crypto",
            side="buy",
            status="pending",
            order_price=2470.0,
            quantity=1.0,
            remaining_qty=1.0,
            created_at="2026-03-16T10:00:00+09:00",
            age_hours=25,
            age_days=1,
            currency="KRW",
            order_price_fmt="2,470",
            current_price_fmt="2,166",
            gap_pct_fmt="+14.0%",
            amount_fmt="31.2만",
            age_fmt="1일",
            summary_line="BTC buy @2,470 (현재 2,166, +14.0%, 31.2만, 1일)",
        )
        assert item.order_price_fmt == "2,470"
        assert item.summary_line.startswith("BTC buy")

    def test_summary_has_fmt_fields(self) -> None:
        from app.schemas.n8n import N8nPendingOrderSummary

        summary = N8nPendingOrderSummary(
            total=2,
            buy_count=1,
            sell_count=1,
            total_buy_krw=478400.0,
            total_sell_krw=34603720.0,
        )
        assert summary.total_buy_fmt is None
        assert summary.total_sell_fmt is None
        assert summary.title is None

    def test_summary_accepts_fmt_values(self) -> None:
        from app.schemas.n8n import N8nPendingOrderSummary

        summary = N8nPendingOrderSummary(
            total=13,
            buy_count=4,
            sell_count=9,
            total_buy_krw=478400.0,
            total_sell_krw=34603720.0,
            total_buy_fmt="47.8만",
            total_sell_fmt="3,460.4만",
            title="📋 미체결 리뷰 — 03/16 (13건, 매수 4 / 매도 9)",
        )
        assert summary.total_buy_fmt == "47.8만"
        assert summary.title.startswith("📋")
