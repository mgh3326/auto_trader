"""KR, US and crypto one-day sessions for ``retro_funnel`` (synthetic values).

Each session routes the same SELECTs the funnel reads (candidates, forecasts,
proposals, rungs, the three live order ledgers, execution_ledger) so the
scope tests can compare the response with the statements that produced it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from tests.services.retro_v1.support import (
    FILL_AT,
    FakeSession,
    bar_row,
    forecast,
    kr_day_session,
    ledger,
    pick,
    proposal,
    rung,
)

DAY = date(2026, 1, 5)


class RecordingSession(FakeSession):
    """FakeSession that also keeps each statement's compiled bind parameters."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        super().__init__(routes)
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: Any = None) -> Any:
        compiled = statement.compile(dialect=_pg())
        self.executed.append((str(compiled), dict(compiled.params)))
        return await super().execute(statement, params)


def _pg() -> Any:
    from sqlalchemy.dialects import postgresql

    return postgresql.dialect()


def _live_order(
    *,
    account_scope: str,
    market: str,
    broker: str,
    symbol: str,
    currency: str,
    order_no: str,
    at: datetime = FILL_AT,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=200,
        trade_date=at,
        symbol=symbol,
        account_scope=account_scope,
        market=market,
        broker=broker,
        order_no=order_no,
        idempotency_key="idem-100",
        correlation_id=None,
        side="buy",
        quantity=Decimal("3"),
        price=Decimal("100"),
        currency=currency,
        status="accepted",
        response_code=None,
        response_message=None,
    )


def us_day_session(**extra_routes: Any) -> RecordingSession:
    """One US trading day: AAPL pick, rejected forecast, KIS US proposal → rung →
    order (live_order_ledger, kis_live scope) → fill (execution_ledger, NASD)."""
    routes: dict[str, Any] = {
        "FROM review.screener_pick_log": [
            pick(1, "AAPL", datetime(2026, 1, 5, 0, 30, tzinfo=UTC), market="us")
        ],
        "FROM review.trade_forecasts": [
            forecast(
                2,
                "MSFT",
                datetime(2026, 1, 5, 0, 40, tzinfo=UTC),
                instrument_type="equity_us",
            )
        ],
        "FROM review.order_proposals ": [
            proposal(
                10,
                "AAPL",
                datetime(2026, 1, 5, 1, 0, tzinfo=UTC),
                market="equity_us",
                account_mode="kis_live",
            )
        ],
        "FROM review.order_proposal_rungs": [rung(100, 10, idempotency_key="idem-100")],
        "FROM review.order_send_intents": [("idem-100", FILL_AT - timedelta(hours=1))],
        "FROM review.live_order_ledger": [
            _live_order(
                account_scope="kis_live",
                market="us",
                broker="kis",
                symbol="AAPL",
                currency="USD",
                order_no="0000123456",
            )
        ],
        "FROM review.execution_ledger": [
            ledger(
                1000,
                FILL_AT,
                broker="kis",
                instrument_type="equity_us",
                symbol="AAPL",
                raw_symbol="AAPL",
                currency="USD",
                venue="NASD",
            )
        ],
        "FROM public.us_candles_1d": [
            bar_row("AAPL", datetime(2026, 1, 6, tzinfo=UTC), "101")
        ],
    }
    routes.update(extra_routes)
    return RecordingSession(routes)


def crypto_day_session(**extra_routes: Any) -> RecordingSession:
    """One UTC-KST crypto day: KRW-BTC pick, rejected forecast, Upbit proposal →
    rung → order (live_order_ledger, upbit_live scope) → fill (execution_ledger)."""
    routes: dict[str, Any] = {
        "FROM review.screener_pick_log": [
            pick(
                1,
                "KRW-BTC",
                datetime(2026, 1, 5, 0, 30, tzinfo=UTC),
                market="crypto",
            )
        ],
        "FROM review.trade_forecasts": [
            forecast(
                2,
                "KRW-ETH",
                datetime(2026, 1, 5, 0, 40, tzinfo=UTC),
                instrument_type="crypto",
            )
        ],
        "FROM review.order_proposals ": [
            proposal(
                10,
                "KRW-BTC",
                datetime(2026, 1, 5, 1, 0, tzinfo=UTC),
                market="crypto",
                account_mode="upbit",
            )
        ],
        "FROM review.order_proposal_rungs": [
            rung(100, 10, idempotency_key="idem-100", broker_order_id="uuid-100")
        ],
        "FROM review.order_send_intents": [],
        "FROM review.live_order_ledger": [
            _live_order(
                account_scope="upbit_live",
                market="crypto",
                broker="upbit",
                symbol="KRW-BTC",
                currency="KRW",
                order_no="uuid-100",
            )
        ],
        "FROM review.execution_ledger": [
            ledger(
                1000,
                FILL_AT,
                broker="upbit",
                instrument_type="crypto",
                symbol="KRW-BTC",
                raw_symbol="KRW-BTC",
                currency="KRW",
                venue="upbit",
                broker_order_id="uuid-100",
                source="reconciler",
            )
        ],
        "FROM public.crypto_candles_1d": [
            bar_row("KRW-BTC", datetime(2026, 1, 6, tzinfo=UTC), "101")
        ],
    }
    routes.update(extra_routes)
    return RecordingSession(routes)


def kr_recording_session(**extra_routes: Any) -> RecordingSession:
    """``kr_day_session`` with statement recording (same routes, same data)."""
    base = kr_day_session(**extra_routes)
    return RecordingSession(base.routes)


MARKET_SESSIONS = {
    "kr": kr_recording_session,
    "us": us_day_session,
    "crypto": crypto_day_session,
}
