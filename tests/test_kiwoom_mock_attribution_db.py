"""hk 4723 (3797 T-P6) — DB proof: the signal row commits before the reservation.

CI only. The pytest-owned throwaway database is the only target; every row this
test creates is deleted in a ``finally`` block so the shared schema stays as
bootstrapped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from app.core.db import AsyncSessionLocal, engine
from app.services.kiwoom_mock_attribution import KiwoomMockSignalRecorder
from app.services.kiwoom_mock_order_ledger_service import KiwoomMockPlaceRecorder
from tests._run_owned_database import validate_run_owned_database_url

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

FROZEN_NOW = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)  # 2026-11-16 10:00 KST
ACK = {"return_code": 0, "return_msg": "정상", "ord_no": "0000111222"}


class _AckOrderClient:
    def __init__(self, _client: Any) -> None:
        pass

    async def place_buy_order(self, **_kwargs: Any) -> dict[str, Any]:
        return ACK


class _ProbePlaceRecorder(KiwoomMockPlaceRecorder):
    """Observes the signal table at the exact moment the reservation is made."""

    def __init__(self, session_factory: Any, *, strategy: str) -> None:
        super().__init__(session_factory)
        self._strategy = strategy
        self.signal_rows_at_reserve: int | None = None

    async def reserve(self, *, idempotency_key: str, symbol: str, side: str):
        async with self._session_factory() as db:
            self.signal_rows_at_reserve = int(
                (
                    await db.execute(
                        text(
                            "SELECT count(*) FROM review.kiwoom_mock_signal_ledger "
                            "WHERE strategy = :strategy"
                        ),
                        {"strategy": self._strategy},
                    )
                ).scalar_one()
            )
        return await super().reserve(
            idempotency_key=idempotency_key, symbol=symbol, side=side
        )


@pytest.mark.asyncio
async def test_signal_row_is_committed_before_the_reservation_and_the_send(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod
    from tests.test_mcp_kiwoom_order_variants import _patch_preflight_success

    strategy = f"db-test-{uuid.uuid4().hex[:12]}"
    # A price this test has never used keeps the deterministic client order id
    # unique across runs, so a leftover reservation cannot collide.
    price = 100_000 + uuid.uuid4().int % 800_000

    probe = _ProbePlaceRecorder(AsyncSessionLocal, strategy=strategy)
    monkeypatch.setattr(mod, "_new_place_recorder", lambda: probe)
    monkeypatch.setattr(
        mod,
        "_new_signal_recorder",
        lambda: KiwoomMockSignalRecorder(AsyncSessionLocal),
    )
    monkeypatch.setattr(mod, "_new_kiwoom_mock_client", lambda: object())
    monkeypatch.setattr(mod, "KiwoomDomesticOrderClient", _AckOrderClient)
    monkeypatch.setattr(mod, "_utc_now", lambda: FROZEN_NOW)
    _patch_preflight_success(monkeypatch, mod)

    client_order_id: str | None = None
    correlation_id: str | None = None
    try:
        response = await mod._kiwoom_mock_place_order_impl(
            symbol="005930",
            side="buy",
            quantity=1,
            price=price,
            exchange="KRX",
            strategy=strategy,
            dry_run=False,
            rung=None,
        )
        client_order_id = response["client_order_id"]
        correlation_id = response["correlation_id"]

        assert response["success"] is True
        # 1. the signal row was already committed when the reservation ran
        assert probe.signal_rows_at_reserve == 1

        async with AsyncSessionLocal() as db:
            signal = (
                await db.execute(
                    text(
                        "SELECT strategy, outcome_state, decision, account_mode "
                        "FROM review.kiwoom_mock_signal_ledger "
                        "WHERE correlation_id = :correlation_id"
                    ),
                    {"correlation_id": correlation_id},
                )
            ).one()
            reservations = (
                await db.execute(
                    text(
                        "SELECT count(*) FROM review.order_send_intents "
                        "WHERE account_scope = 'kiwoom_mock' "
                        "AND idempotency_key = :key"
                    ),
                    {"key": client_order_id},
                )
            ).scalar_one()
            ledger_status = (
                await db.execute(
                    text(
                        "SELECT status FROM review.kiwoom_mock_order_ledger "
                        "WHERE client_order_id = :key"
                    ),
                    {"key": client_order_id},
                )
            ).scalar_one()

        # 2. the row is attributed, and its outcome was marked after the send
        assert tuple(signal) == (strategy, "submitted", "order", "kiwoom_mock")
        # 3. the reservation and the ledger row exist exactly once
        assert reservations == 1
        assert ledger_status == "accepted"
    finally:
        async with AsyncSessionLocal() as db:
            if correlation_id is not None:
                await db.execute(
                    text(
                        "DELETE FROM review.kiwoom_mock_signal_ledger "
                        "WHERE correlation_id = :correlation_id"
                    ),
                    {"correlation_id": correlation_id},
                )
            else:
                await db.execute(
                    text(
                        "DELETE FROM review.kiwoom_mock_signal_ledger "
                        "WHERE strategy = :strategy"
                    ),
                    {"strategy": strategy},
                )
            if client_order_id is not None:
                await db.execute(
                    text(
                        "DELETE FROM review.kiwoom_mock_order_ledger "
                        "WHERE client_order_id = :key"
                    ),
                    {"key": client_order_id},
                )
                await db.execute(
                    text(
                        "DELETE FROM review.order_send_intents "
                        "WHERE account_scope = 'kiwoom_mock' "
                        "AND idempotency_key = :key"
                    ),
                    {"key": client_order_id},
                )
            await db.commit()
