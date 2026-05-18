"""ROB-265 Plan 4 — Hermes notification client tests."""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from app.services.hermes_client import (
    HermesNotificationClient,
    ReviewTriggerPayload,
)


def _base_payload(**overrides) -> ReviewTriggerPayload:
    payload: dict = {
        "event_uuid": uuid.uuid4(),
        "alert_uuid": uuid.uuid4(),
        "source_report_uuid": uuid.uuid4(),
        "source_item_uuid": uuid.uuid4(),
        "correlation_id": "corr-test-1",
        "kst_date": "2026-05-18",
        "market": "kr",
        "target_kind": "asset",
        "symbol": "005930",
        "metric": "rsi",
        "operator": "below",
        "threshold": Decimal("30"),
        "threshold_key": "30",
        "intent": "trend_recovery_review",
        "action_mode": "notify_only",
        "current_value": Decimal("28.5"),
        "scanner_snapshot": {"rsi_14": 28.5, "close": 68000},
        "outcome": "notified",
    }
    payload.update(overrides)
    return ReviewTriggerPayload(**payload)


def test_payload_rejects_extra_fields() -> None:
    """Locked design: the payload contract is closed (extra='forbid')."""
    with pytest.raises(ValidationError):
        ReviewTriggerPayload(
            event_uuid=uuid.uuid4(),
            alert_uuid=uuid.uuid4(),
            source_report_uuid=uuid.uuid4(),
            source_item_uuid=uuid.uuid4(),
            correlation_id="x",
            kst_date="2026-05-18",
            market="kr",
            target_kind="asset",
            symbol="005930",
            metric="rsi",
            operator="below",
            threshold=Decimal("30"),
            threshold_key="30",
            intent="trend_recovery_review",
            action_mode="notify_only",
            current_value=None,
            scanner_snapshot={},
            outcome="notified",
            # not in the schema — must be rejected
            stray_field="x",  # type: ignore[call-arg]
        )


@pytest.mark.asyncio
async def test_disabled_client_skips_delivery() -> None:
    client = HermesNotificationClient(
        webhook_url="http://nowhere.local/hook", token="t", enabled=False
    )
    result = await client.send_review_trigger(_base_payload())
    assert result.status == "skipped"
    await client.close()


@pytest.mark.asyncio
async def test_enabled_client_success_path() -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(202)

    transport = httpx.MockTransport(_handler)
    client = HermesNotificationClient(
        webhook_url="http://hermes.test/hook",
        token="bearer-abc",
        enabled=True,
        transport=transport,
    )
    payload = _base_payload()
    result = await client.send_review_trigger(payload)
    await client.close()

    assert result.status == "success"
    assert result.http_status == 202
    assert captured["url"] == "http://hermes.test/hook"
    assert captured["headers"]["authorization"] == "Bearer bearer-abc"
    # Payload includes every required immutable-snapshot field.
    assert f'"event_uuid":"{payload.event_uuid}"' in captured["body"]
    assert f'"alert_uuid":"{payload.alert_uuid}"' in captured["body"]
    assert '"market":"kr"' in captured["body"]
    assert '"action_mode":"notify_only"' in captured["body"]
    assert '"outcome":"notified"' in captured["body"]


@pytest.mark.asyncio
async def test_enabled_client_4xx_returns_failed() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(403, text="forbidden")
    )
    client = HermesNotificationClient(
        webhook_url="http://hermes.test/hook",
        enabled=True,
        transport=transport,
    )
    result = await client.send_review_trigger(_base_payload())
    await client.close()
    assert result.status == "failed"
    assert result.http_status == 403


@pytest.mark.asyncio
async def test_enabled_client_network_error_returns_failed() -> None:
    def _raise(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(_raise)
    client = HermesNotificationClient(
        webhook_url="http://hermes.test/hook",
        enabled=True,
        transport=transport,
    )
    result = await client.send_review_trigger(_base_payload())
    await client.close()
    assert result.status == "failed"
    assert result.reason == "request_failed"


@pytest.mark.asyncio
async def test_enabled_client_without_token_omits_auth_header() -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200)

    transport = httpx.MockTransport(_handler)
    client = HermesNotificationClient(
        webhook_url="http://hermes.test/hook",
        token="",
        enabled=True,
        transport=transport,
    )
    await client.send_review_trigger(_base_payload())
    await client.close()
    assert "authorization" not in captured["headers"]
