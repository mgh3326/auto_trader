"""ROB-1461 — HTTP binding for the E1 intent gateway (fillwire ROB-1457).

This router is served only by the loopback process in
``app/execution_intent_gateway_server.py``. ``app.main`` does not include it, so
the public API cannot reach it.

The gate order is: route setting (404), machine token (403 or 401), then the
request. A session cookie cannot stand in for the token, because that process has
no session middleware. The confirm header is the per-action confirmation: it must
equal the body's idempotency key, and anything else makes E1 answer
``confirm_required``.

There is no way to create an intent here. A body with any field outside E1's child
request is refused, so a caller cannot name an account.
"""

from __future__ import annotations

import dataclasses
import hmac
import json
import logging
import os
from datetime import UTC, datetime
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.schemas.execution_intent import (
    UPBIT_LIVE_ACCOUNT,
    misshaped_submit_identity,
    parse_child_request,
)
from app.services.execution_intent_gateway.account_control import (
    PostgresAccountControlStore,
)
from app.services.execution_intent_gateway.decisions import MOCK_ACCOUNTS, Refusal
from app.services.execution_intent_gateway.gateway import IntentGateway
from app.services.execution_intent_gateway.kis_mock_forwarder import KisMockForwarder
from app.services.execution_intent_gateway.query import (
    IntentFillsReader,
    LedgerEvidenceReader,
    SqlLedgerEvidenceReader,
    build_reconciliation,
    child_json,
    reconciliation_json,
)
from app.services.execution_intent_gateway.store import (
    GatewayReader,
    SqlAlchemyGatewayStore,
)
from app.services.execution_intent_gateway.upbit_account_reads import (
    upbit_avg_buy_price,
    upbit_cooldown_active,
)
from app.services.execution_intent_gateway.upbit_decisions import UPBIT_ACCOUNTS
from app.services.execution_intent_gateway.upbit_gateway import UpbitGatewayPath
from app.services.execution_intent_gateway.upbit_live_forwarder import (
    UpbitLiveForwarder,
)
from app.services.execution_intent_gateway.upbit_query import (
    build_upbit_reconciliation,
    filled_by_uuid,
    upbit_child_json,
    upbit_reconciliation_json,
)
from app.services.execution_intent_gateway.upbit_shadow_forwarder import (
    UpbitShadowForwarder,
)
from app.services.execution_intent_gateway.upbit_store import UpbitGatewayStore
from app.services.kis_mock_runner.control import PostgresKillSwitchStore
from app.services.strategy_sleeves.account_binding import configured_upbit_account_ref

logger = logging.getLogger(__name__)

CONFIRM_HEADER: Final = "X-Execution-Intent-Confirm"
#: E1's largest child request is far below this. fillwire bounds its answer too.
_MAX_BODY_BYTES: Final = 4_096
_JSON_MEDIA_TYPE: Final = "application/json"


def require_route_enabled() -> None:
    """Route off looks like no route at all, before the token or any DB access."""
    if settings.EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED is not True:
        raise HTTPException(status_code=404)


def require_gateway_token(request: Request) -> None:
    expected = settings.EXECUTION_INTENT_GATEWAY_TOKEN
    header = settings.EXECUTION_INTENT_GATEWAY_TOKEN_HEADER.strip()
    if not expected or not header:
        raise HTTPException(status_code=403, detail="gateway_token_not_configured")
    supplied = request.headers.get(header, "")
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid_gateway_token")


router = APIRouter(
    prefix="/execution-intents",
    tags=["execution-intent-gateway"],
    dependencies=[Depends(require_route_enabled), Depends(require_gateway_token)],
)


def get_upbit_store() -> UpbitGatewayStore:
    return UpbitGatewayStore(AsyncSessionLocal)


def get_upbit_path() -> UpbitGatewayPath:
    """hk 5441: the ``upbit_live`` account. Shadow by construction unless the
    intent is live **and** ``EXECUTION_INTENT_GATEWAY_UPBIT_LIVE_ENABLED`` is
    exactly ``true``; the forwarder checks that again before the order service.
    """
    store = get_upbit_store()
    kill_switch = PostgresAccountControlStore(UPBIT_LIVE_ACCOUNT, AsyncSessionLocal)
    return UpbitGatewayPath(
        store=store,
        kill_switch=kill_switch,
        shadow_forwarder=UpbitShadowForwarder(),
        live_forwarder=UpbitLiveForwarder(
            facts=store,
            kill_switch=kill_switch,
            environment=os.environ,
            clock=lambda: datetime.now(UTC),
            account_ref=configured_upbit_account_ref,
        ),
        avg_price_reader=upbit_avg_buy_price,
        cooldown_reader=upbit_cooldown_active,
    )


def get_intent_gateway() -> IntentGateway:
    store = SqlAlchemyGatewayStore(AsyncSessionLocal)
    return IntentGateway(
        store=store,
        forwarders={"kis_mock": KisMockForwarder(journal=store)},
        kill_switch=PostgresKillSwitchStore(),
        upbit=get_upbit_path(),
    )


def get_gateway_reader() -> GatewayReader:
    return SqlAlchemyGatewayStore(AsyncSessionLocal)


def get_evidence_reader() -> LedgerEvidenceReader:
    return SqlLedgerEvidenceReader(AsyncSessionLocal)


def get_fills_reader() -> IntentFillsReader:
    return SqlLedgerEvidenceReader(AsyncSessionLocal)


async def _read_bounded_json(request: Request) -> Any:
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != _JSON_MEDIA_TYPE:
        raise HTTPException(status_code=415, detail="json_required")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="body_too_large")
    try:
        return json.loads(bytes(body))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_child_request") from exc


async def _misshaped_upbit_submit(
    request: Request, gateway: IntentGateway, payload: Any
) -> JSONResponse | None:
    """Design 5438 §3.3: common fields, then the intent, then the account's shape.

    A submit with the kis_mock field set is validated against kis_mock limits,
    so one meant for an ``upbit_live`` intent can be rejected before its account
    is known. The executor holds on any non-200, so that request gets the
    definite refusal the contract names instead.
    """
    identity = misshaped_submit_identity(payload)
    if identity is None:
        return None
    confirm = hmac.compare_digest(
        request.headers.get(CONFIRM_HEADER, "").encode(),
        identity.idempotency_key.encode(),
    )
    try:
        outcome = await gateway.refuse_misshaped_upbit_child(
            identity.intent_id, confirm=confirm
        )
    except Exception as exc:  # noqa: BLE001 - fillwire treats any non-200 as unknown
        logger.error("intent gateway handle raised: error_class=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="gateway_unavailable") from exc
    return None if outcome is None else JSONResponse(dataclasses.asdict(outcome))


@router.post("/children")
async def post_child(
    request: Request,
    gateway: Annotated[IntentGateway, Depends(get_intent_gateway)],
) -> JSONResponse:
    """One child request, answered with E1's outcome. See the fillwire binding."""
    payload = await _read_bounded_json(request)
    try:
        child = parse_child_request(payload)
    except ValidationError as exc:
        refused = await _misshaped_upbit_submit(request, gateway, payload)
        if refused is not None:
            return refused
        # The validation error can echo the body, so it never reaches the answer.
        raise HTTPException(status_code=422, detail="invalid_child_request") from exc
    confirm = hmac.compare_digest(
        request.headers.get(CONFIRM_HEADER, "").encode(),
        child.idempotency_key.encode(),
    )
    try:
        outcome = await gateway.handle(child, confirm=confirm)
    except Exception as exc:  # noqa: BLE001 - fillwire treats any non-200 as unknown
        logger.error("intent gateway handle raised: error_class=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="gateway_unavailable") from exc
    if outcome.reason == Refusal.AMEND_UNSUPPORTED_FOR_ACCOUNT.value:
        # Design 5438 §3.3: an upbit_live order is replaced by a cancel and a new
        # submit under a new key. The request is not valid for the account.
        raise HTTPException(status_code=422, detail=outcome.reason)
    return JSONResponse(dataclasses.asdict(outcome))


@router.get("/children/{idempotency_key}")
async def get_child(
    idempotency_key: str,
    reader: Annotated[GatewayReader, Depends(get_gateway_reader)],
) -> JSONResponse:
    """One recorded child by its key, with its journaled broker result."""
    try:
        found = await reader.read_child(idempotency_key)
    except Exception as exc:  # noqa: BLE001 - never a guess at the recorded state
        logger.error("intent query raised: error_class=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="query_unavailable") from exc
    if found is None:
        raise HTTPException(status_code=404, detail="child_not_found")
    intent_id, child = found
    row = (
        child_json(child) if child.quantity_decimal is None else upbit_child_json(child)
    )
    return JSONResponse({"intent_id": str(intent_id), **row, "result": child.result})


@router.get("/intents")
async def list_intents(
    store: Annotated[UpbitGatewayStore, Depends(get_upbit_store)],
    account: Annotated[str, Query()],
    open: Annotated[bool, Query()] = True,  # noqa: A002 - the wire name (design R12)
) -> JSONResponse:
    """Unexpired intents of one account, for the executor's poll (design §5 R12).

    Read only: it cannot create or change an intent. Only ``upbit_live`` is
    listed, and only with ``open=true``; the fields carry no account number and
    no mandate terms.
    """
    if account not in UPBIT_ACCOUNTS or open is not True:
        raise HTTPException(status_code=422, detail="unsupported_intent_listing")
    try:
        intents = await store.list_open_intents(account, now=datetime.now(UTC))
    except Exception as exc:  # noqa: BLE001 - never an empty list for a failed read
        logger.error("intent list query raised: error_class=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="query_unavailable") from exc
    return JSONResponse({"account": account, "intents": intents})


@router.get("/intents/{intent_id}")
async def get_intent(
    intent_id: UUID,
    reader: Annotated[GatewayReader, Depends(get_gateway_reader)],
    evidence_reader: Annotated[LedgerEvidenceReader, Depends(get_evidence_reader)],
    fills_reader: Annotated[IntentFillsReader, Depends(get_fills_reader)],
    upbit_store: Annotated[UpbitGatewayStore, Depends(get_upbit_store)],
) -> JSONResponse:
    """The intent's recorded children and what E2's ``Query`` needs to reconcile."""
    try:
        journal = await reader.read_intent_journal(intent_id)
        if journal is None:
            raise HTTPException(status_code=404, detail="intent_not_found")
        if journal.intent.account in UPBIT_ACCOUNTS:
            fills = await fills_reader.read_fills(journal.intent, journal.children)
            upbit = build_upbit_reconciliation(
                journal.children,
                await upbit_store.read_intent_orders(journal.intent),
                filled_by_uuid(fills),
            )
            return JSONResponse(
                upbit_reconciliation_json(intent_id, journal.children, upbit)
            )
        if journal.intent.account not in MOCK_ACCOUNTS:
            raise HTTPException(status_code=404, detail="intent_not_found")
        evidence = await evidence_reader.read(journal.intent, journal.children)
        reconciliation = build_reconciliation(journal.children, evidence)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - never a guess at the recorded state
        logger.error("intent query raised: error_class=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="query_unavailable") from exc
    return JSONResponse(
        reconciliation_json(intent_id, journal.children, reconciliation)
    )


@router.get("/intents/{intent_id}/fills")
async def get_intent_fills(
    intent_id: UUID,
    reader: Annotated[GatewayReader, Depends(get_gateway_reader)],
    fills_reader: Annotated[IntentFillsReader, Depends(get_fills_reader)],
) -> JSONResponse:
    """Committed fills scoped exclusively to the intent's journaled orders."""
    try:
        journal = await reader.read_intent_journal(intent_id)
        if journal is None or journal.intent.account not in (
            MOCK_ACCOUNTS | UPBIT_ACCOUNTS
        ):
            raise HTTPException(status_code=404, detail="intent_not_found")
        orders = await fills_reader.read_fills(journal.intent, journal.children)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - never substitute an empty fill set
        logger.error("intent fills query raised: error_class=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="query_unavailable") from exc
    return JSONResponse({"intent_id": str(intent_id), "orders": orders})
