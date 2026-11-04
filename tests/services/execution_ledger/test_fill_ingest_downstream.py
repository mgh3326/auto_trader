"""Shared post-upsert orchestration (fillwire P0).

The websocket monitor and the HTTP ingest route both run this function, so the
notification-suppression semantics the monitor grew (KIS duplicate suppression,
Upbit small-fill proposal-rung recovery) are asserted here once.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.execution_ledger.fill_ingest import (
    DownstreamHooks,
    run_post_upsert_downstream,
)
from app.services.fill_notification import FillOrder


def _order(*, amount: float, market_type: str = "crypto") -> FillOrder:
    return FillOrder(
        symbol="KRW-BTC" if market_type == "crypto" else "000660",
        side="bid",
        filled_price=92_800_000.0,
        filled_qty=amount / 92_800_000.0,
        filled_amount=amount,
        filled_at="2026-09-07T12:00:00+09:00",
        account="upbit" if market_type == "crypto" else "kis",
        order_id="order-1",
        market_type=market_type,
        currency="KRW",
    )


class _Hooks:
    def __init__(self, *, projected: bool = False) -> None:
        self.projected = projected
        self.projection_calls: list[dict[str, Any]] = []
        self.notify_calls: list[tuple[tuple, dict]] = []

    async def project(self, order_data: dict[str, Any]) -> bool:
        self.projection_calls.append(order_data)
        return self.projected

    async def notify(self, *args: Any, **kwargs: Any) -> None:
        self.notify_calls.append((args, kwargs))

    def as_hooks(self) -> DownstreamHooks:
        return DownstreamHooks(
            project_upbit_proposal_fill=self.project,
            send_fill_notification=self.notify,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_kis_fill_notifies_with_the_correlation_id() -> None:
    hooks = _Hooks()

    outcome = await run_post_upsert_downstream(
        broker="kis",
        upsert_status="inserted",
        fill_order=_order(amount=1_959_000, market_type="kr"),
        raw_event={"symbol": "000660"},
        correlation_id="corr-1",
        hooks=hooks.as_hooks(),
    )

    assert outcome.notification_attempted is True
    assert hooks.projection_calls == [], "no Upbit projection for a KIS fill"
    assert hooks.notify_calls[0][1] == {"correlation_id": "corr-1"}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["updated", "unchanged"])
async def test_duplicate_kis_row_suppresses_the_notification(status: str) -> None:
    hooks = _Hooks()

    outcome = await run_post_upsert_downstream(
        broker="kis",
        upsert_status=status,
        fill_order=_order(amount=1_959_000, market_type="kr"),
        raw_event=None,
        hooks=hooks.as_hooks(),
    )

    assert outcome.duplicate is True
    assert outcome.notification_attempted is False
    assert hooks.notify_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_ledger_row_still_notifies() -> None:
    """``None`` means the commit gate is off — not duplicate evidence."""
    hooks = _Hooks()

    outcome = await run_post_upsert_downstream(
        broker="kis",
        upsert_status=None,
        fill_order=_order(amount=1_959_000, market_type="kr"),
        raw_event=None,
        hooks=hooks.as_hooks(),
    )

    assert outcome.duplicate is False
    assert outcome.notification_attempted is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upbit_projects_then_notifies_with_the_rung_flag() -> None:
    hooks = _Hooks(projected=True)
    frame = {"state": "trade", "uuid": "u-1", "executed_volume": "0.0003"}

    await run_post_upsert_downstream(
        broker="upbit",
        upsert_status="inserted",
        fill_order=_order(amount=27_840),
        raw_event=frame,
        hooks=hooks.as_hooks(),
    )

    assert hooks.projection_calls == [frame]
    assert hooks.notify_calls[0][1] == {"proposal_rung_fill": True}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upbit_projection_is_skipped_when_no_row_was_written() -> None:
    hooks = _Hooks(projected=True)

    await run_post_upsert_downstream(
        broker="upbit",
        upsert_status=None,
        fill_order=_order(amount=27_840),
        raw_event={"state": "trade", "uuid": "u-1", "executed_volume": "0.0003"},
        hooks=hooks.as_hooks(),
    )

    assert hooks.projection_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_upbit_row_recovers_a_suppressed_small_alert() -> None:
    """A rung that only projected on the second delivery still gets announced."""
    hooks = _Hooks(projected=True)

    outcome = await run_post_upsert_downstream(
        broker="upbit",
        upsert_status="unchanged",
        fill_order=_order(amount=27_840),  # below the 50,000 KRW threshold
        raw_event={"state": "trade", "uuid": "u-1", "executed_volume": "0.0003"},
        hooks=hooks.as_hooks(),
    )

    assert outcome.duplicate is True
    assert outcome.notification_attempted is True
    assert hooks.notify_calls[0][1] == {"proposal_rung_fill": True}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_upbit_large_fill_stays_suppressed() -> None:
    """The large fill already alerted on its first delivery."""
    hooks = _Hooks(projected=True)

    outcome = await run_post_upsert_downstream(
        broker="upbit",
        upsert_status="unchanged",
        fill_order=_order(amount=1_000_000),
        raw_event={"state": "trade", "uuid": "u-1", "executed_volume": "0.01"},
        hooks=hooks.as_hooks(),
    )

    assert outcome.notification_attempted is False
    assert hooks.notify_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_upbit_without_projection_stays_suppressed() -> None:
    hooks = _Hooks(projected=False)

    outcome = await run_post_upsert_downstream(
        broker="upbit",
        upsert_status="unchanged",
        fill_order=_order(amount=27_840),
        raw_event={"state": "trade", "uuid": "u-1", "executed_volume": "0.0003"},
        hooks=hooks.as_hooks(),
    )

    assert outcome.notification_attempted is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_order_context_reports_no_notification() -> None:
    hooks = _Hooks()

    outcome = await run_post_upsert_downstream(
        broker="kis",
        upsert_status="inserted",
        fill_order=None,
        raw_event=None,
        hooks=hooks.as_hooks(),
    )

    assert outcome.notification_attempted is False
    assert hooks.notify_calls == []


class _RungRepo:
    """Fake OrderProposalRepository: one rung (2 of 3) of a limit-buy proposal."""

    def __init__(self, session: object) -> None:
        self._session = session

    async def find_rung_by_evidence(self, **_: Any) -> tuple[object, object]:
        return ("00000000-0000-0000-0000-00000000beef", _RUNG)

    async def get_group_by_proposal_id(self, _proposal_id: object, **_: Any) -> Any:
        return _PROPOSAL

    async def list_rungs(self, _proposal_pk: int) -> list[object]:
        return [_rung_row(0), _RUNG, _rung_row(2)]


class _NoopSession:
    async def __aenter__(self) -> _NoopSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _rung_row(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        rung_index=index, limit_price=Decimal("68000"), quantity=Decimal("10")
    )


_RUNG = _rung_row(1)
_PROPOSAL = SimpleNamespace(
    id=7,
    order_type="limit",
    strategy="rsi_reversion",
    thesis="눌림 후 반등 확인\n세부 근거",
    source_asof=None,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fill_alert_carries_intent_and_slippage_through_downstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real send_fill_notification looks up the rung and renders its intent."""
    from unittest.mock import AsyncMock

    from app.monitoring.trade_notifier.formatters_telegram import (
        format_fill_notification_telegram,
    )
    from app.services.execution_ledger import fill_ingest, fill_intent_lookup

    rendered: list[str] = []

    class _Notifier:
        async def notify_fill(
            self,
            order: FillOrder,
            *,
            enrichment: object = None,
            detail_url: str | None = None,
            intent: object = None,
        ) -> bool:
            rendered.append(
                format_fill_notification_telegram(
                    order,
                    display_name="삼성전자",
                    detail_url=detail_url,
                    enrichment=None,
                    intent=intent,  # type: ignore[arg-type]
                )
            )
            return True

    monkeypatch.setattr(fill_ingest, "get_trade_notifier", lambda: _Notifier())
    monkeypatch.setattr(
        fill_ingest, "fetch_fill_enrichment", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(fill_intent_lookup, "AsyncSessionLocal", _NoopSession)
    monkeypatch.setattr(fill_intent_lookup, "OrderProposalRepository", _RungRepo)

    order = FillOrder(
        symbol="005930",
        side="bid",
        filled_price=67_500.0,
        filled_qty=10.0,
        filled_amount=675_000.0,
        filled_at="2026-11-04T10:00:00",
        account="kis",
        order_id="0001234567",
        market_type="kr",
        currency="KRW",
    )
    outcome = await run_post_upsert_downstream(
        broker="kis",
        upsert_status="inserted",
        fill_order=order,
        raw_event=None,
        correlation_id="corr-2834",
        hooks=DownstreamHooks(on_fill_committed=AsyncMock(return_value=None)),
    )

    assert outcome.notification_attempted is True
    assert len(rendered) == 1
    assert "의도: rsi\\_reversion · 눌림 후 반등 확인 · 래더 2/3" in rendered[0]
    assert "슬리피지: 유리 -73.53bp · 총 -5,000원" in rendered[0]
