"""CLI entry-point contract for the manual KR market daily collector."""

from __future__ import annotations

import sys

import pytest

from app.core import cli
from app.services.research_candles import session_segment
from scripts import collect_kr_market_daily

pytestmark = pytest.mark.unit


def test_main_dry_run_bootstraps_and_fetches_without_database(
    monkeypatch, capsys
) -> None:
    events: list[str] = []

    class FakeClient:
        def __init__(self) -> None:
            events.append("client")

        async def fetch_breadth(self, market: str) -> dict[str, object]:
            events.append(f"breadth:{market}")
            return {
                "market": market,
                "up_count": 1,
                "down_count": 0,
                "flat_count": 0,
                "total_count": 1,
                "fetched_count": 1,
                "complete": True,
            }

        async def fetch_investors(self, market, bizdate):
            events.append(f"investors:{market}:{bizdate.isoformat()}")
            return []

        async def fetch_deposits(self):
            events.append("deposits")
            return []

    monkeypatch.setattr(
        collect_kr_market_daily,
        "NaverMarketDailyClient",
        FakeClient,
    )
    monkeypatch.setattr(
        cli,
        "setup_logging_and_sentry",
        lambda *, service_name: events.append(f"logging:{service_name}"),
    )
    monkeypatch.setattr(
        collect_kr_market_daily,
        "_today_kst",
        lambda: collect_kr_market_daily.date(2026, 11, 26),
    )
    monkeypatch.setattr(sys, "argv", ["collect_kr_market_daily", "--dry-run"])
    monkeypatch.setattr(
        session_segment,
        "session_segment_of",
        lambda *_: session_segment.KRX_AFTER,
    )

    assert collect_kr_market_daily.main() == 0
    assert events == [
        "logging:collect_kr_market_daily",
        "client",
        "breadth:KOSPI",
        "breadth:KOSDAQ",
        "investors:KOSPI:2026-11-26",
        "investors:KOSDAQ:2026-11-26",
        "deposits",
    ]
    assert '"inserted": null' in capsys.readouterr().out
