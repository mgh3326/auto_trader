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
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.schemas.execution_intent import parse_child_request
from app.services.execution_intent_gateway.decisions import MOCK_ACCOUNTS
from app.services.execution_intent_gateway.gateway import IntentGateway
from app.services.execution_intent_gateway.kis_mock_forwarder import KisMockForwarder
from app.services.execution_intent_gateway.query import (
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
from app.services.kis_mock_runner.control import PostgresKillSwitchStore

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


def get_intent_gateway() -> IntentGateway:
    store = SqlAlchemyGatewayStore(AsyncSessionLocal)
    return IntentGateway(
        store=store,
        forwarders={"kis_mock": KisMockForwarder(journal=store)},
        kill_switch=PostgresKillSwitchStore(),
    )


def get_gateway_reader() -> GatewayReader:
    return SqlAlchemyGatewayStore(AsyncSessionLocal)


def get_evidence_reader() -> LedgerEvidenceReader:
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
    return JSONResponse(
        {"intent_id": str(intent_id), **child_json(child), "result": child.result}
    )


@router.get("/intents/{intent_id}")
async def get_intent(
    intent_id: UUID,
    reader: Annotated[GatewayReader, Depends(get_gateway_reader)],
    evidence_reader: Annotated[LedgerEvidenceReader, Depends(get_evidence_reader)],
) -> JSONResponse:
    """The intent's recorded children and what E2's ``Query`` needs to reconcile."""
    try:
        journal = await reader.read_intent_journal(intent_id)
        if journal is None:
            raise HTTPException(status_code=404, detail="intent_not_found")
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
