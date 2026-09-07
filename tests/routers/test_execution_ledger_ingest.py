"""fills ingest contract (fillwire P0): per-item reporting, isolation, idempotency.

DB-backed against the run-owned ``test_db``. No broker call, no network.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.models.execution_ledger import ExecutionLedger
from app.routers import execution_ledger_ingest as ingest_module
from app.routers.execution_ledger_ingest import router as ingest_router
from app.schemas.execution_ledger_ingest import MAX_INGEST_BATCH

_PATH = "/trading/api/execution-ledger/fills/ingest"
_ORDER_PREFIX = "fillwire-test-"


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
async def ingest_client(db_session, monkeypatch) -> AsyncIterator[AsyncClient]:
    """App with the ingest router only; auth middleware is not installed here.

    Token auth has its own dedicated contract test; this file exercises the
    handler behaviour behind that gate.
    """

    async def _no_downstream(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ingest_module, "run_post_upsert_downstream", _no_downstream)

    app = FastAPI()
    app.include_router(ingest_router)

    async def _override_db():
        yield db_session

    from app.core.db import get_db

    app.dependency_overrides[get_db] = _override_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        yield client
    await db_session.execute(
        delete(ExecutionLedger).where(
            ExecutionLedger.broker_order_id.like(f"{_ORDER_PREFIX}%")
        )
    )
    await db_session.commit()


async def _row_for(db_session, broker_order_id: str) -> ExecutionLedger | None:
    result = await db_session.execute(
        select(ExecutionLedger).where(
            ExecutionLedger.broker_order_id == broker_order_id
        )
    )
    return result.scalar_one_or_none()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_accepted_batch_writes_rows_and_reports_row_ids(
    ingest_client: AsyncClient, db_session
) -> None:
    first = _fill_payload()
    second = _fill_payload(side="sell")
    resp = await ingest_client.post(
        _PATH, json={"fills": [first, second], "source": "fillwire"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["received"] == 2
    assert body["accepted"] == 2
    assert body["rejected"] == 0
    assert [item["status"] for item in body["results"]] == ["inserted", "inserted"]
    for item in body["results"]:
        assert isinstance(item["row_id"], int)
        assert item["row_id"] > 0
        assert item["reason"] is None
        assert set(item) == {"status", "row_id", "reason"}

    row = await _row_for(db_session, first["broker_order_id"])
    assert row is not None
    assert row.id == body["results"][0]["row_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_batch_twice_is_unchanged_with_the_same_row_id(
    ingest_client: AsyncClient,
) -> None:
    payload = _fill_payload()
    body = {"fills": [payload], "source": "websocket_monitor"}

    first = (await ingest_client.post(_PATH, json=body)).json()
    second = (await ingest_client.post(_PATH, json=body)).json()

    assert first["results"][0]["status"] == "inserted"
    assert second["results"][0]["status"] == "unchanged"
    assert second["results"][0]["row_id"] == first["results"][0]["row_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_changed_values_for_the_same_key_report_updated(
    ingest_client: AsyncClient,
) -> None:
    payload = _fill_payload()
    await ingest_client.post(_PATH, json={"fills": [payload], "source": "fillwire"})
    changed = {**payload, "filled_price": "92900000"}
    second = (
        await ingest_client.post(_PATH, json={"fills": [changed], "source": "fillwire"})
    ).json()

    assert second["results"][0]["status"] == "updated"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_invalid_item_does_not_discard_the_batch(
    ingest_client: AsyncClient, db_session
) -> None:
    good_before = _fill_payload()
    invalid = _fill_payload(filled_qty="0")  # gt=0 on the upsert schema
    good_after = _fill_payload()

    resp = await ingest_client.post(
        _PATH,
        json={"fills": [good_before, invalid, good_after], "source": "fillwire"},
    )

    assert resp.status_code == 200
    body = resp.json()
    statuses = [item["status"] for item in body["results"]]
    # Order is preserved, so the caller can align results with its own batch.
    assert statuses == ["inserted", "rejected", "inserted"]
    assert body["accepted"] == 2
    assert body["rejected"] == 1
    rejected = body["results"][1]
    assert rejected["row_id"] is None
    assert "filled_qty" in rejected["reason"]

    assert await _row_for(db_session, good_before["broker_order_id"]) is not None
    assert await _row_for(db_session, good_after["broker_order_id"]) is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_db_failure_is_isolated_by_a_savepoint(
    ingest_client: AsyncClient, db_session, monkeypatch
) -> None:
    """A failing row must not roll back rows that already applied."""
    good_before = _fill_payload()
    boom = _fill_payload()
    good_after = _fill_payload()

    from app.services.execution_ledger.repository import ExecutionLedgerRepository

    original = ExecutionLedgerRepository.upsert_fill

    async def _flaky(self, fill):  # noqa: ANN001, ANN202
        if fill.broker_order_id == boom["broker_order_id"]:
            raise RuntimeError("simulated driver failure")
        return await original(self, fill)

    monkeypatch.setattr(ExecutionLedgerRepository, "upsert_fill", _flaky)

    resp = await ingest_client.post(
        _PATH, json={"fills": [good_before, boom, good_after], "source": "fillwire"}
    )

    body = resp.json()
    assert [item["status"] for item in body["results"]] == [
        "inserted",
        "rejected",
        "inserted",
    ]
    assert body["results"][1]["reason"] == "RuntimeError"
    assert await _row_for(db_session, good_before["broker_order_id"]) is not None
    assert await _row_for(db_session, good_after["broker_order_id"]) is not None
    assert await _row_for(db_session, boom["broker_order_id"]) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outer_source_run_id_is_the_transport_authority(
    ingest_client: AsyncClient, db_session
) -> None:
    """The envelope run id wins; an item cannot keep its own claim."""
    run_id = uuid.uuid4()
    item_run_id = uuid.uuid4()
    inherits = _fill_payload()
    claims_own = _fill_payload(source_run_id=str(item_run_id))

    await ingest_client.post(
        _PATH,
        json={
            "fills": [inherits, claims_own],
            "source": "fillwire",
            "source_run_id": str(run_id),
        },
    )

    inherited_row = await _row_for(db_session, inherits["broker_order_id"])
    claimed_row = await _row_for(db_session, claims_own["broker_order_id"])
    assert inherited_row is not None and claimed_row is not None
    assert inherited_row.source_run_id == run_id
    assert claimed_row.source_run_id == run_id
    assert claimed_row.source_run_id != item_run_id


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("claimed_source", ["reconciler", "manual_import", "websocket"])
async def test_row_source_is_forced_to_websocket(
    ingest_client: AsyncClient, db_session, claimed_source: str
) -> None:
    """A producer cannot claim reconciler/manual-import provenance over the wire."""
    payload = _fill_payload(source=claimed_source)
    await ingest_client.post(_PATH, json={"fills": [payload], "source": "fillwire"})

    row = await _row_for(db_session, payload["broker_order_id"])
    assert row is not None
    assert row.source == "websocket"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_raw_payload_is_redacted_server_side(
    ingest_client: AsyncClient, db_session
) -> None:
    payload = _fill_payload(raw_payload_json={"token": "super-secret", "symbol": "BTC"})
    await ingest_client.post(_PATH, json={"fills": [payload], "source": "fillwire"})

    row = await _row_for(db_session, payload["broker_order_id"])
    assert row is not None
    assert row.raw_payload_json["token"] == "[REDACTED]"
    assert row.raw_payload_json["symbol"] == "BTC"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rejection_reason_does_not_echo_payload_values(
    ingest_client: AsyncClient,
) -> None:
    payload = _fill_payload(
        broker="nasdaq-secret-broker-name", raw_payload_json={"token": "super-secret"}
    )
    body = (
        await ingest_client.post(_PATH, json={"fills": [payload], "source": "fillwire"})
    ).json()

    reason = body["results"][0]["reason"]
    assert body["results"][0]["status"] == "rejected"
    assert "super-secret" not in reason
    assert "nasdaq-secret-broker-name" not in reason
    assert len(reason) <= 300


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, MAX_INGEST_BATCH + 1])
async def test_batch_bounds_are_envelope_errors(
    ingest_client: AsyncClient, size: int
) -> None:
    resp = await ingest_client.post(
        _PATH,
        json={"fills": [_fill_payload() for _ in range(size)], "source": "fillwire"},
    )
    assert resp.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_max_batch_size_is_accepted(ingest_client: AsyncClient) -> None:
    fills = [_fill_payload() for _ in range(MAX_INGEST_BATCH)]
    resp = await ingest_client.post(_PATH, json={"fills": fills, "source": "fillwire"})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == MAX_INGEST_BATCH


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_envelope_source_is_rejected(
    ingest_client: AsyncClient,
) -> None:
    resp = await ingest_client.post(
        _PATH, json={"fills": [_fill_payload()], "source": "some-other-daemon"}
    )
    assert resp.status_code == 422
