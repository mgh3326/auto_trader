from __future__ import annotations

import pytest

from app.services.n8n_formatting import (
    fmt_date_with_weekday,
    fmt_pnl,
    fmt_value,
)


@pytest.mark.unit
class TestDailyBriefFormatting:
    def test_fmt_date_with_weekday(self):
        from datetime import datetime

        from app.core.timezone import KST

        dt = datetime(2026, 3, 17, 8, 30, tzinfo=KST)
        assert fmt_date_with_weekday(dt) == "03/17 (화)"

    def test_fmt_date_with_weekday_sunday(self):
        from datetime import datetime

        from app.core.timezone import KST

        dt = datetime(2026, 3, 15, 8, 30, tzinfo=KST)
        assert fmt_date_with_weekday(dt) == "03/15 (일)"

    def test_fmt_value_krw_man(self):
        assert fmt_value(15_000_000, "KRW") == "1,500만"

    def test_fmt_value_krw_eok(self):
        assert fmt_value(150_000_000, "KRW") == "1.5억"

    def test_fmt_value_usd(self):
        assert fmt_value(42_000, "USD") == "$42,000"

    def test_fmt_value_none(self):
        assert fmt_value(None, "KRW") == "-"

    def test_fmt_pnl_negative(self):
        assert fmt_pnl(-5.2) == "-5.2%"

    def test_fmt_pnl_positive(self):
        assert fmt_pnl(3.1) == "+3.1%"

    def test_fmt_pnl_none(self):
        assert fmt_pnl(None) == "-"


@pytest.mark.unit
class TestBuildBriefText:
    def test_contains_header(self):
        from app.schemas.n8n import N8nMarketOverview
        from app.services.n8n_daily_brief_service import _build_brief_text

        text = _build_brief_text(
            date_fmt="03/17 (화)",
            market_overview=N8nMarketOverview(
                fear_greed=None,
                btc_dominance=None,
                total_market_cap_change_24h=None,
                economic_events_today=[],
            ),
            pending_by_market={},
            portfolio_by_market={},
            yesterday_fills={"total": 0, "fills": []},
        )

        assert "📋 Daily Trading Brief — 03/17 (화)" in text
        assert "💼 미체결 주문" in text
        assert "📊 포트폴리오" in text

    def test_includes_pending_counts(self):
        from app.schemas.n8n import N8nMarketOverview
        from app.services.n8n_daily_brief_service import _build_brief_text

        text = _build_brief_text(
            date_fmt="03/17 (화)",
            market_overview=N8nMarketOverview(
                fear_greed=None,
                btc_dominance=None,
                total_market_cap_change_24h=None,
                economic_events_today=[],
            ),
            pending_by_market={
                "crypto": {
                    "total": 11,
                    "buy_count": 4,
                    "sell_count": 7,
                    "near_fill_count": 2,
                    "needs_attention_count": 5,
                    "orders": [],
                },
            },
            portfolio_by_market={},
            yesterday_fills={"total": 0, "fills": []},
        )

        assert "[크립토] 11건 (매수 4 / 매도 7)" in text
        assert "체결 임박 2건 ⚡" in text

    def test_includes_fills(self):
        from app.schemas.n8n import N8nMarketOverview
        from app.services.n8n_daily_brief_service import _build_brief_text

        text = _build_brief_text(
            date_fmt="03/17 (화)",
            market_overview=N8nMarketOverview(
                fear_greed=None,
                btc_dominance=None,
                total_market_cap_change_24h=None,
                economic_events_today=[],
            ),
            pending_by_market={},
            portfolio_by_market={},
            yesterday_fills={
                "total": 1,
                "fills": [
                    {
                        "symbol": "ETH",
                        "side": "sell",
                        "price_fmt": "3.35M",
                        "amount_fmt": "402만",
                        "time": "14:23",
                    }
                ],
            },
        )

        assert "✅ 전일 체결" in text
        assert "ETH sell" in text
