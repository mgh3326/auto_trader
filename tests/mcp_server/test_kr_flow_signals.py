from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.mcp_server.tooling import kr_flow_signals
from app.services.brokers.toss.dto import (
    parse_credit_trades_page,
    parse_securities_lending_page,
    parse_short_selling_page,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "kr_flow"


def _fixture(name):
    return json.loads((FIXTURE_DIR / name).read_text())


@pytest.mark.asyncio
async def test_flow_tool_isolates_one_source_failure(monkeypatch):
    class FakeToss:
        async def stock_short_selling(self, symbol, *, count):
            raise RuntimeError("fixture failure")

        async def stock_securities_lending(self, symbol, *, count):
            return SimpleNamespace(
                records=[
                    {"date": date(2026, 11, 25), "balanceAmount": Decimal("123.40")}
                ]
            )

        async def stock_credit_trades(self, symbol, *, count):
            return SimpleNamespace(records=[])

        async def stock_program_trades(self, symbol, *, count):
            return SimpleNamespace(records=[{"date": date(2026, 11, 25)}])

        async def investor_trading_rankings(self, *, count):
            return {
                investor: SimpleNamespace(
                    ranked_at=datetime(2026, 11, 25, 15, 30, tzinfo=UTC),
                    buy=[{"rank": 1, "symbol": "005930", "amount": Decimal("100")}],
                    sell=[],
                )
                for investor in ("foreigner", "institution", "individual")
            }

        async def aclose(self):
            return None

    class FakeKis:
        async def inquire_vi_status(self, symbol, session_date):
            return [
                {
                    "mksc_shrn_iscd": symbol,
                    "bsop_date": session_date,
                    "cntg_vi_hour": "082700",
                    "vi_prc": Decimal("100"),
                }
            ]

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

    from app.services.brokers.toss.client import TossReadClient
    from app.services.kr_trading_flags import store

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        TossReadClient, "from_settings", classmethod(lambda cls: FakeToss())
    )
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", FakeSession)

    async def empty_records(db, symbols):
        return {}

    monkeypatch.setattr(store, "load_flag_records", empty_records)
    monkeypatch.setattr(kr_flow_signals, "today_kst", lambda: date(2026, 11, 26))
    monkeypatch.setattr("app.services.brokers.kis.client.KISClient", lambda: FakeKis())

    result = await kr_flow_signals.handle_get_kr_flow_signals("005930", 5)
    sources = result["reference"]
    assert sources["toss_short_selling"]["status"] == "error"
    assert sources["toss_short_selling"]["error_class"] == "RuntimeError"
    assert sources["toss_securities_lending"]["status"] == "ok"
    assert sources["toss_securities_lending"]["data"][0]["balanceAmount"] == "123.40"
    assert sources["toss_credit_trades"]["status"] == "empty"
    assert sources["toss_program_trades"]["status"] == "ok"
    assert sources["toss_investor_rankings"]["status"] == "ok"
    assert all(sources[name]["fetched_at"] for name in sources)
    assert result["risk"]["kis_vi_status"]["status"] == "ok"
    assert result["risk"]["kis_vi_status"]["data"][0]["vi_prc"] == "100"
    assert result["risk"]["kis_vi_status"]["as_of"] == "2026-11-26T08:27:00+09:00"
    assert result["risk"]["trading_flags"]["status"] == "empty"
    assert result["risk"]["trading_flags"]["data"]["management"]["state"] == "unknown"


@pytest.mark.parametrize(
    ("name", "parser"),
    [
        ("toss_short_selling.json", parse_short_selling_page),
        ("toss_securities_lending.json", parse_securities_lending_page),
        ("toss_credit_trades.json", parse_credit_trades_page),
    ],
)
def test_provider_as_of_handles_date_and_datetime_rows(name, parser):
    page = parser(_fixture(name))
    stamp = kr_flow_signals._as_of(page.records)
    assert stamp is not None
    assert "T" in stamp


@pytest.mark.asyncio
async def test_trading_flag_snapshot_keeps_missing_unknown_and_explicit_none(
    monkeypatch,
):
    from app.services.kr_trading_flags import store
    from app.services.kr_trading_flags.vocabulary import CATEGORY_MANAGEMENT

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    observed_at = datetime(2026, 11, 26, 0, 0, tzinfo=UTC)
    record = store.FlagRecord(
        category=CATEGORY_MANAGEMENT,
        state="none",
        raw_code="N",
        reason_code=None,
        effective_date=None,
        source="kis_master",
        observed_at=observed_at,
    )

    async def load_records(db, symbols):
        return {"005930": {CATEGORY_MANAGEMENT: record}}

    monkeypatch.setattr("app.core.db.AsyncSessionLocal", FakeSession)
    monkeypatch.setattr(store, "load_flag_records", load_records)
    snapshot = await kr_flow_signals._trading_flags_snapshot("005930")
    assert snapshot["data"][CATEGORY_MANAGEMENT]["state"] == "none"
    assert snapshot["data"]["market_alert"]["state"] == "unknown"


@pytest.mark.asyncio
async def test_toss_initialization_error_isolated_from_kis_and_flags(monkeypatch):
    from app.services.brokers.toss.client import TossReadClient
    from app.services.kr_trading_flags import store

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class FakeKis:
        async def inquire_vi_status(self, symbol, session_date):
            return []

        async def inquire_member(self, symbol):
            return {"buyers": [], "sellers": []}

    async def empty_records(db, symbols):
        return {}

    monkeypatch.setattr(
        TossReadClient,
        "from_settings",
        classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("disabled"))),
    )
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", FakeSession)
    monkeypatch.setattr(store, "load_flag_records", empty_records)
    monkeypatch.setattr("app.services.brokers.kis.client.KISClient", lambda: FakeKis())
    result = await kr_flow_signals.handle_get_kr_flow_signals("005930", 5)
    assert result["risk"]["kis_vi_status"]["status"] == "empty"
    assert result["reference"]["toss_short_selling"]["status"] == "error"
    assert result["reference"]["toss_short_selling"]["error_class"] == "RuntimeError"
    assert result["reference"]["toss_investor_rankings"]["status"] == "error"


@pytest.mark.asyncio
async def test_investor_rankings_returns_construction_error_in_reference(monkeypatch):
    from app.services.brokers.toss.client import TossReadClient

    monkeypatch.setattr(
        TossReadClient,
        "from_settings",
        classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("disabled"))),
    )
    result = await kr_flow_signals.handle_get_kr_investor_rankings()
    source = result["reference"]["toss_investor_rankings"]
    assert source["status"] == "error"
    assert source["error_class"] == "RuntimeError"
    assert source["fetched_at"]
