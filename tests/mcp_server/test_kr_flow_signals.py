from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.mcp_server.tooling import kr_flow_signals


@pytest.mark.asyncio
async def test_flow_tool_isolates_one_source_failure(monkeypatch):
    class FakeToss:
        async def stock_short_selling(self, symbol, *, count):
            raise RuntimeError("fixture failure")

        async def stock_securities_lending(self, symbol, *, count):
            return SimpleNamespace(records=[{"date": date(2026, 11, 25), "balanceAmount": Decimal("123.40")}])

        async def stock_credit_trades(self, symbol, *, count):
            return SimpleNamespace(records=[])

        async def stock_program_trades(self, symbol, *, count):
            return SimpleNamespace(records=[{"date": date(2026, 11, 25)}])

        async def aclose(self):
            return None

    class FakeKis:
        async def inquire_vi_status(self, symbol, session_date):
            return [{"stck_shrn_iscd": symbol, "vi_prpr": Decimal("100")}]

        async def inquire_member(self, symbol):
            return {
                "buyers": [
                    {
                        "rank": 1,
                        "member_code": "1",
                        "member_name": "buyer",
                        "volume": Decimal("4"),
                        "trading_amount": Decimal("400"),
                    }
                ],
                "sellers": [],
            }

        async def aclose(self):
            return None

    from app.services.brokers.toss.client import TossReadClient

    monkeypatch.setattr(TossReadClient, "from_settings", classmethod(lambda cls: FakeToss()))
    monkeypatch.setattr(kr_flow_signals, "today_kst", lambda: date(2026, 11, 26))
    monkeypatch.setattr(
        "app.services.brokers.kis.client.KISClient", lambda: FakeKis()
    )

    result = await kr_flow_signals.handle_get_kr_flow_signals("005930", 5)
    sources = result["sources"]
    assert sources["toss_short_selling"]["status"] == "error"
    assert sources["toss_short_selling"]["error_class"] == "RuntimeError"
    assert sources["toss_securities_lending"]["status"] == "ok"
    assert sources["toss_securities_lending"]["data"][0]["balanceAmount"] == "123.40"
    assert sources["toss_credit_trades"]["status"] == "empty"
    assert sources["toss_program_trades"]["status"] == "ok"
    assert sources["kis_vi_status"]["status"] == "ok"
    assert sources["kis_broker_window"]["status"] == "ok"
