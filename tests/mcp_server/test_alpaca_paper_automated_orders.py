"""ROB-842 real public-MCP-handler integration tests for the automated boundary.

Drives the actual ``alpaca_paper_automated_preview_order`` /
``alpaca_paper_automated_submit_order`` handlers (session-factory + broker-factory
injected, gate armed) against the test DB with a counting fake broker. Proves:
- automated submit is default-disabled;
- the public handler produces exactly ONE broker submit for sequential AND
  parallel duplicate calls;
- caller cannot select origin or inject a client_order_id (no such params);
- packet authority (max_notional=10 vs notional=50) and market-data freshness
  fail-close at preview before any persistence/broker call.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling import alpaca_paper_automated_orders as auto_mod
from app.mcp_server.tooling.alpaca_paper_automated_orders import (
    alpaca_paper_automated_preview_order,
    alpaca_paper_automated_submit_order,
    reset_alpaca_paper_automated_factories,
    set_alpaca_paper_automated_factories,
)
from app.models.review import AlpacaPaperOrderLedger
from app.services.brokers.alpaca.schemas import Order

pytestmark = [pytest.mark.asyncio]

_CORR = "rob842-auto-it"


class CountingBroker:
    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.submit_calls: list[Any] = []
        self._delay_s = delay_s

    async def submit_order(self, request: Any) -> Order:
        self.submit_calls.append(request)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        return Order(
            id=f"paper-{len(self.submit_calls)}",
            client_order_id=getattr(request, "client_order_id", None),
            symbol=getattr(request, "symbol", "BTC/USD"),
            filled_qty=Decimal("0"),
            side=getattr(request, "side", "buy"),
            type=getattr(request, "type", "limit"),
            time_in_force=getattr(request, "time_in_force", "gtc"),
            status="accepted",
            limit_price=getattr(request, "limit_price", None),
        )

    async def get_order_by_client_order_id(self, client_order_id: str) -> Order | None:
        return None


@pytest_asyncio.fixture
async def broker(monkeypatch) -> CountingBroker:
    monkeypatch.setattr(settings, "alpaca_paper_automated_submit_enabled", True)
    b = CountingBroker(delay_s=0.02)
    set_alpaca_paper_automated_factories(
        session_factory=lambda: AsyncSessionLocal,
        broker_factory=lambda: b,
    )
    yield b
    reset_alpaca_paper_automated_factories()


@pytest_asyncio.fixture(autouse=True)
async def _clean_rows():
    async with AsyncSessionLocal() as db:
        stmt = delete(AlpacaPaperOrderLedger).where(
            AlpacaPaperOrderLedger.lifecycle_correlation_id.like(f"{_CORR}%")
        )
        await db.execute(stmt)
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(AlpacaPaperOrderLedger).where(
                AlpacaPaperOrderLedger.lifecycle_correlation_id.like(f"{_CORR}%")
            )
        )
        await db.commit()


def _preview_kwargs(corr: str, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "symbol": "BTC/USD",
        "side": "buy",
        "type": "limit",
        "correlation_id": corr,
        "snapshot_id": f"{corr}-snap",
        "signal_symbol": "KRW-BTC",
        "market_data_asof": datetime.now(UTC).isoformat(),
        "market_data_source": "upbit_ticker",
        "notional": Decimal("10"),
        "limit_price": Decimal("50000"),
        "time_in_force": "gtc",
        "asset_class": "crypto",
        "max_notional": Decimal("10"),
    }
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# Default-disabled
# ---------------------------------------------------------------------------
async def test_automated_preview_disabled_by_default() -> None:
    # No gate armed, factories default.
    result = await alpaca_paper_automated_preview_order(
        **_preview_kwargs(f"{_CORR}-off")
    )
    assert result["success"] is False
    assert result["reason_code"] == "automated_submit_disabled"


async def test_automated_submit_disabled_by_default() -> None:
    result = await alpaca_paper_automated_submit_order("any-token", confirm=True)
    assert result["success"] is False
    assert result["reason_code"] == "automated_submit_disabled"


# ---------------------------------------------------------------------------
# Public-handler exactly-one broker submit
# ---------------------------------------------------------------------------
async def test_public_handler_sequential_duplicate_submits_once(
    broker: CountingBroker,
) -> None:
    corr = f"{_CORR}-seq"
    preview = await alpaca_paper_automated_preview_order(**_preview_kwargs(corr))
    assert preview["success"] is True
    token = preview["approval_token"]

    first = await alpaca_paper_automated_submit_order(token, confirm=True)
    second = await alpaca_paper_automated_submit_order(token, confirm=True)

    assert first["status"] == "submitted"
    assert first["broker_called"] is True
    assert second["status"] in {"replayed", "recovered"}
    assert second["broker_called"] is False
    assert len(broker.submit_calls) == 1


async def test_public_handler_parallel_duplicate_submits_once(
    broker: CountingBroker,
) -> None:
    corr = f"{_CORR}-par"
    preview = await alpaca_paper_automated_preview_order(**_preview_kwargs(corr))
    token = preview["approval_token"]

    results = await asyncio.gather(
        alpaca_paper_automated_submit_order(token, confirm=True),
        alpaca_paper_automated_submit_order(token, confirm=True),
    )

    assert len(broker.submit_calls) == 1
    statuses = sorted(r["status"] for r in results)
    assert statuses.count("submitted") == 1
    other = [r for r in results if r["status"] != "submitted"][0]
    assert other["status"] in {"replayed", "recovered", "idempotency_in_progress"}
    assert other["broker_called"] is False


async def test_confirm_false_is_dry_run_no_post(broker: CountingBroker) -> None:
    corr = f"{_CORR}-dry"
    preview = await alpaca_paper_automated_preview_order(**_preview_kwargs(corr))
    token = preview["approval_token"]

    dry = await alpaca_paper_automated_submit_order(token, confirm=False)
    assert dry["submitted"] is False
    assert dry["blocked_reason"] == "confirmation_required"
    assert broker.submit_calls == []


async def test_submit_without_persisted_preview_rejected(
    broker: CountingBroker,
) -> None:
    result = await alpaca_paper_automated_submit_order(
        "rob842a-crypto-nonexistent", confirm=True
    )
    assert result["status"] == "rejected"
    assert result["reason_code"] == "no_preview_for_token"
    assert broker.submit_calls == []


# ---------------------------------------------------------------------------
# Packet authority fail-close at preview (blocker 3) — no persistence, no broker
# ---------------------------------------------------------------------------
async def test_preview_notional_exceeds_approved_ceiling_fails_close(
    broker: CountingBroker,
) -> None:
    corr = f"{_CORR}-overmax"
    result = await alpaca_paper_automated_preview_order(
        **_preview_kwargs(corr, notional=Decimal("50"), max_notional=Decimal("10"))
    )
    assert result["success"] is False
    assert result["reason_code"] == "notional_exceeds_max"
    # No preview row persisted → submit finds nothing.
    submit = await alpaca_paper_automated_submit_order(
        result["client_order_id"], confirm=True
    )
    assert submit["reason_code"] == "no_preview_for_token"
    assert broker.submit_calls == []


async def test_preview_missing_market_data_source_fails_close(
    broker: CountingBroker,
) -> None:
    result = await alpaca_paper_automated_preview_order(
        **_preview_kwargs(f"{_CORR}-nosrc", market_data_source="")
    )
    assert result["success"] is False
    assert result["reason_code"] == "missing_market_data_source"


async def test_preview_future_asof_fails_close(broker: CountingBroker) -> None:
    future = datetime(2999, 1, 1, tzinfo=UTC).isoformat()
    result = await alpaca_paper_automated_preview_order(
        **_preview_kwargs(f"{_CORR}-future", market_data_asof=future)
    )
    assert result["success"] is False
    assert result["reason_code"] == "future_source_timestamp"


# ---------------------------------------------------------------------------
# Trusted origin: no caller-selectable origin / client_order_id (blocker 2/4)
# ---------------------------------------------------------------------------
async def test_public_handlers_expose_no_origin_or_client_order_id() -> None:
    preview_params = set(
        inspect.signature(alpaca_paper_automated_preview_order).parameters
    )
    submit_params = set(
        inspect.signature(alpaca_paper_automated_submit_order).parameters
    )
    assert "origin" not in preview_params
    assert "client_order_id" not in preview_params
    assert "origin" not in submit_params
    assert "client_order_id" not in submit_params
    # Submit binds only to a server-issued token.
    assert submit_params == {"approval_token", "confirm"}


async def test_manual_submit_tool_has_no_origin_param() -> None:
    from app.mcp_server.tooling.alpaca_paper_orders import alpaca_paper_submit_order

    params = set(inspect.signature(alpaca_paper_submit_order).parameters)
    assert "origin" not in params


async def test_module_exposes_gate_and_factory_controls() -> None:
    assert callable(auto_mod.set_alpaca_paper_automated_factories)
    assert callable(auto_mod.reset_alpaca_paper_automated_factories)
    assert auto_mod.ALPACA_PAPER_AUTOMATED_TOOL_NAMES == {
        "alpaca_paper_automated_preview_order",
        "alpaca_paper_automated_submit_order",
    }
