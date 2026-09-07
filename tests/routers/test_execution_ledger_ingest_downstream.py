"""HTTP ingest drives the *same* shared downstream the websocket monitor does.

The router must not reimplement (or import) the monitor's post-upsert story:
it calls ``run_post_upsert_downstream`` after the ledger commit, and a
downstream failure never un-commits the fill.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.models.execution_ledger import ExecutionLedger
from app.routers import execution_ledger_ingest as ingest_module
from app.routers.execution_ledger_ingest import router as ingest_router
from tests.fixtures.execution_ledger_fill_frames import (
    kis_domestic_fill_frame,
    upbit_trade_frame,
)

_PATH = "/trading/api/execution-ledger/fills/ingest"
_ORDER_PREFIX = "fillwire-downstream-"


def _fill_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "broker": "upbit",
        "account_mode": "live",
        "venue": "upbit_krw",
        "instrument_type": "crypto",
        "symbol": "BTC",
        "raw_symbol": "KRW-BTC",
        "side": "buy",
        "broker_order_id": f"{_ORDER_PREFIX}{uuid.uuid4().hex[:12]}",
        "fill_seq": 0,
        "filled_qty": "0.0003",
        "filled_price": "92800000",
        "filled_at": datetime(2026, 9, 7, 3, 0, tzinfo=UTC).isoformat(),
        "currency": "KRW",
        "source": "websocket",
    }
    payload.update(overrides)
    return payload


@pytest_asyncio.fixture
async def client_and_calls(db_session, monkeypatch) -> AsyncIterator[tuple]:
    calls: list[dict[str, Any]] = []

    async def _spy(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(ingest_module, "run_post_upsert_downstream", _spy)

    app = FastAPI()
    app.include_router(ingest_router)

    async def _override_db():
        yield db_session

    from app.core.db import get_db

    app.dependency_overrides[get_db] = _override_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        yield client, calls
    await db_session.execute(
        delete(ExecutionLedger).where(
            ExecutionLedger.broker_order_id.like(f"{_ORDER_PREFIX}%")
        )
    )
    await db_session.commit()


def test_router_does_not_import_the_websocket_monitor() -> None:
    """No logic duplication and no router -> monitor import (spec §3 I3)."""
    source = Path("app/routers/execution_ledger_ingest.py").read_text()
    assert "websocket_monitor" not in source


@pytest.mark.integration
@pytest.mark.asyncio
async def test_downstream_runs_once_per_accepted_row_with_the_raw_frame(
    client_and_calls,
) -> None:
    client, calls = client_and_calls
    frame = upbit_trade_frame()
    good = _fill_payload(raw_payload_json=frame)
    invalid = _fill_payload(filled_price="0")

    await client.post(_PATH, json={"fills": [good, invalid], "source": "fillwire"})

    assert len(calls) == 1
    call = calls[0]
    assert call["broker"] == "upbit"
    assert call["upsert_status"] == "inserted"
    # The raw frame is preserved so Upbit rung projection keeps its cumulative
    # ``executed_volume`` evidence.
    assert call["raw_event"]["executed_volume"] == frame["executed_volume"]
    assert call["fill_order"].symbol == "KRW-BTC"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_downstream_still_notifies_without_a_raw_frame(
    client_and_calls,
) -> None:
    """``raw_payload_json`` is optional; canonical fields must still notify."""
    client, calls = client_and_calls
    payload = _fill_payload()
    assert "raw_payload_json" not in payload

    await client.post(_PATH, json={"fills": [payload], "source": "fillwire"})

    assert len(calls) == 1
    order = calls[0]["fill_order"]
    assert calls[0]["raw_event"] is None
    assert order is not None
    assert order.symbol == "KRW-BTC"
    assert order.side == "bid"
    assert order.filled_price == pytest.approx(92_800_000.0)
    assert order.filled_qty == pytest.approx(0.0003)
    assert order.filled_amount == pytest.approx(92_800_000.0 * 0.0003)
    assert order.market_type == "crypto"
    assert order.currency == "KRW"
    assert order.order_id == payload["broker_order_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kis_fill_without_raw_frame_rebuilds_a_sell_order(
    client_and_calls,
) -> None:
    client, calls = client_and_calls
    payload = _fill_payload(
        broker="kis",
        venue="krx",
        instrument_type="equity_kr",
        symbol="000660",
        raw_symbol="000660",
        side="sell",
        filled_qty="1",
        filled_price="1959000",
    )

    await client.post(_PATH, json={"fills": [payload], "source": "fillwire"})

    order = calls[0]["fill_order"]
    assert order.side == "ask"
    assert order.market_type == "kr"
    assert order.account == "kis"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kis_raw_frame_is_normalized_by_the_shared_normalizer(
    client_and_calls,
) -> None:
    client, calls = client_and_calls
    frame = kis_domestic_fill_frame()
    payload = _fill_payload(
        broker="kis",
        venue="krx",
        instrument_type="equity_kr",
        symbol="000660",
        raw_symbol="000660",
        side="sell",
        filled_qty="1",
        filled_price="1959000",
        raw_payload_json=frame,
        correlation_id=frame["correlation_id"],
    )

    await client.post(_PATH, json={"fills": [payload], "source": "fillwire"})

    call = calls[0]
    assert call["correlation_id"] == frame["correlation_id"]
    assert call["fill_order"].symbol == "000660"
    assert call["fill_order"].side == "ask"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_downstream_failure_leaves_the_committed_fill_alone(
    db_session, monkeypatch
) -> None:
    async def _boom(**_kwargs: Any) -> None:
        raise RuntimeError("notifier exploded")

    monkeypatch.setattr(ingest_module, "run_post_upsert_downstream", _boom)

    app = FastAPI()
    app.include_router(ingest_router)

    async def _override_db():
        yield db_session

    from app.core.db import get_db

    app.dependency_overrides[get_db] = _override_db

    payload = _fill_payload()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(_PATH, json={"fills": [payload], "source": "fillwire"})

    assert resp.status_code == 200
    assert resp.json()["results"][0]["status"] == "inserted"

    row = (
        await db_session.execute(
            select(ExecutionLedger).where(
                ExecutionLedger.broker_order_id == payload["broker_order_id"]
            )
        )
    ).scalar_one_or_none()
    assert row is not None, "the ledger commit is authoritative"

    await db_session.execute(
        delete(ExecutionLedger).where(
            ExecutionLedger.broker_order_id.like(f"{_ORDER_PREFIX}%")
        )
    )
    await db_session.commit()
