from __future__ import annotations

from decimal import Decimal
from typing import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import WatchOrderIntentLedger
from app.services.watch_intent_policy import IntentPolicy
from app.services.watch_order_intent_service import (
    IntentEmissionResult,
    WatchOrderIntentService,
)


def _intent_policy(**overrides: object) -> IntentPolicy:
    base = dict(
        action="create_order_intent",
        side="buy",
        quantity=1,
        notional_krw=None,
        limit_price=None,
        max_notional_krw=Decimal("1500000"),
    )
    base.update(overrides)
    return IntentPolicy(**base)  # type: ignore[arg-type]


def _watch(market: str = "kr", symbol: str = "005930") -> dict:
    return {
        "market": market,
        "target_kind": "asset",
        "symbol": symbol,
        "condition_type": "price_below",
        "threshold": Decimal("70000"),
        "threshold_key": "70000",
    }


class FakeFx:
    def __init__(self, value: Decimal | None = Decimal("1400")) -> None:
        self.value = value
        self.calls = 0

    async def get_quote(self) -> Decimal | None:
        self.calls += 1
        return self.value


@pytest.mark.asyncio
async def test_emit_intent_kr_success_writes_previewed_row(
    db_session: AsyncSession,
) -> None:
    service = WatchOrderIntentService(db_session, fx_provider=FakeFx())
    result = await service.emit_intent(
        watch=_watch(),
        policy=_intent_policy(),
        triggered_value=Decimal("68000"),
        kst_date="2026-05-04",
        correlation_id="corr-1",
    )
    assert isinstance(result, IntentEmissionResult)
    assert result.status == "previewed"
    rows = (
        await db_session.execute(select(WatchOrderIntentLedger))
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.lifecycle_state == "previewed"
    assert row.market == "kr"
    assert row.symbol == "005930"
    assert row.side == "buy"
    assert row.account_mode == "kis_mock"
    assert row.execution_source == "watch"
    assert row.idempotency_key == (
        "kr:asset:005930:price_below:70000:create_order_intent:buy:2026-05-04"
    )
    assert row.kst_date == "2026-05-04"
    assert row.preview_line["lifecycle_state"] == "previewed"


@pytest.mark.asyncio
async def test_emit_intent_dedupe_returns_dedupe_hit(
    db_session: AsyncSession,
) -> None:
    service = WatchOrderIntentService(db_session, fx_provider=FakeFx())
    first = await service.emit_intent(
        watch=_watch(),
        policy=_intent_policy(),
        triggered_value=Decimal("68000"),
        kst_date="2026-05-04",
        correlation_id="corr-first",
    )
    assert first.status == "previewed"

    second = await service.emit_intent(
        watch=_watch(),
        policy=_intent_policy(),
        triggered_value=Decimal("67000"),
        kst_date="2026-05-04",
        correlation_id="corr-second",
    )
    assert second.status == "dedupe_hit"
    assert second.correlation_id == "corr-first"
    assert second.idempotency_key == first.idempotency_key

    rows = (
        await db_session.execute(select(WatchOrderIntentLedger))
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_emit_intent_failed_does_not_block_subsequent_previewed(
    db_session: AsyncSession,
) -> None:
    fx = FakeFx(value=None)
    service = WatchOrderIntentService(db_session, fx_provider=fx)
    failed = await service.emit_intent(
        watch=_watch(market="us", symbol="AAPL"),
        policy=_intent_policy(quantity=1, max_notional_krw=Decimal("3000000")),
        triggered_value=Decimal("181"),
        kst_date="2026-05-04",
        correlation_id="corr-fail",
    )
    assert failed.status == "failed"
    assert failed.blocked_by == "fx_unavailable"

    fx.value = Decimal("1400")
    succeeded = await service.emit_intent(
        watch=_watch(market="us", symbol="AAPL"),
        policy=_intent_policy(quantity=1, max_notional_krw=Decimal("3000000")),
        triggered_value=Decimal("181"),
        kst_date="2026-05-04",
        correlation_id="corr-succeed",
    )
    assert succeeded.status == "previewed"

    rows = (
        await db_session.execute(
            select(WatchOrderIntentLedger).order_by(WatchOrderIntentLedger.created_at)
        )
    ).scalars().all()
    assert [r.lifecycle_state for r in rows] == ["failed", "previewed"]


@pytest.mark.asyncio
async def test_emit_intent_cap_blocked_records_failed_row(
    db_session: AsyncSession,
) -> None:
    service = WatchOrderIntentService(db_session, fx_provider=FakeFx())
    result = await service.emit_intent(
        watch=_watch(),
        policy=_intent_policy(quantity=100, max_notional_krw=Decimal("100000")),
        triggered_value=Decimal("68000"),
        kst_date="2026-05-04",
        correlation_id="corr-cap",
    )
    assert result.status == "failed"
    assert result.blocked_by == "max_notional_krw_cap"

    row = (
        await db_session.execute(select(WatchOrderIntentLedger))
    ).scalars().one()
    assert row.lifecycle_state == "failed"
    assert row.blocked_by == "max_notional_krw_cap"
    assert row.notional_krw_evaluated is not None
