"""Fail-open, loopback-only audit witness for KIS live domestic orders.

This module is deliberately observational: it neither builds nor gates KIS
requests.  The caller snapshots an already-final order immediately before the
existing broker call, while all witness I/O is owned by background tasks.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import sentry_sdk
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

logger = logging.getLogger(__name__)

_ENABLED_ENV = "KIS_LIVE_SHADOW_WITNESS_ENABLED"
_URL_ENV = "EDGE_WITNESS_URL"
_DEFAULT_URL = "http://127.0.0.1:8080"
_IO_TIMEOUT_SECONDS = 0.5
_ECHO_DEPENDENCY_TIMEOUT_SECONDS = 2.0
_SCHEMA = json.loads(
    Path(__file__).with_name("kis_live_shadow_witness_v1.schema.json").read_text()
)
_VALIDATOR = Draft202012Validator(_SCHEMA, format_checker=FormatChecker())


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _breadcrumb(category: str, data: Mapping[str, str] | None = None) -> None:
    try:
        sentry_sdk.add_breadcrumb(
            category="kis_live_shadow_witness", message=category, data=data
        )
    except Exception:
        # Observation must never change the broker outcome, even if Sentry's
        # own hook/transport is unavailable.
        pass


def _warning(event: str, **safe_data: str) -> None:
    try:
        logger.warning(event, extra=safe_data or None)
    except Exception:
        # A logging filter/handler may fail; still attempt the independent
        # breadcrumb channel below, without letting it affect an order.
        pass
    _breadcrumb(event, safe_data or None)


def report_observation_failure(event: str) -> None:
    """Safely record a witness-only failure at a live broker boundary."""
    _warning(event)


def _validate(section: str, value: Mapping[str, object]) -> bool:
    try:
        _VALIDATOR.validate({section: dict(value)})
    except ValidationError:
        return False
    return True


def _validated_base_url(value: str) -> str | None:
    """Accept only a plain HTTP loopback origin; never inherit proxy settings."""
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or port == 0
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    return value.rstrip("/")


def _enabled() -> bool:
    return os.getenv(_ENABLED_ENV, "false") == "true"


def witness_base_url() -> str | None:
    """Return the safe configured origin for an explicit reconcile invocation."""
    return _validated_base_url(os.getenv(_URL_ENV, _DEFAULT_URL))


class WitnessReconcileError(RuntimeError):
    """A reconcile query failed or returned an unsafe response shape."""


async def fetch_missing_echoes(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> list[dict[str, str]]:
    """Fetch validated missing-echo receipts without treating an error as empty."""
    base_url = witness_base_url()
    if base_url is None:
        raise WitnessReconcileError("invalid_witness_url")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_IO_TIMEOUT_SECONDS),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            response = await asyncio.wait_for(
                client.get(
                    f"{base_url}/v1/commands",
                    params={"scope": "kis_live", "missing_echo": "true"},
                ),
                timeout=_IO_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise WitnessReconcileError("query_failed") from exc
    if not isinstance(payload, dict) or "witnesses" not in payload:
        raise WitnessReconcileError("invalid_response")
    witnesses = payload["witnesses"]
    if witnesses is None:
        return []
    if not isinstance(witnesses, list):
        raise WitnessReconcileError("invalid_response")
    validated: list[dict[str, str]] = []
    for witness in witnesses:
        if not isinstance(witness, dict) or not _validate("receipt", witness):
            raise WitnessReconcileError("invalid_response")
        validated.append(
            {key: value for key, value in witness.items() if isinstance(value, str)}
        )
    return validated


@dataclass(slots=True)
class LiveShadowWitness:
    """Owns background witness tasks for one immutable broker intent."""

    base_url: str
    intent: Mapping[str, str]
    transport: httpx.AsyncBaseTransport | None = None
    _tasks: set[asyncio.Task[object]] = field(default_factory=set, init=False)
    _intent_task: asyncio.Task[object] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.intent = MappingProxyType(dict(self.intent))

    def _track(self, coroutine: Any) -> asyncio.Task[object] | None:
        try:
            task = asyncio.create_task(coroutine)
        except Exception:
            try:
                coroutine.close()
            except Exception:
                pass
            _warning("kis_live_witness_task_schedule_failed")
            return None
        self._tasks.add(task)

        def done(completed: asyncio.Task[object]) -> None:
            self._tasks.discard(completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                # Task bodies already reduce errors to safe observability events.
                _warning("kis_live_witness_task_failed")

        task.add_done_callback(done)
        return task

    def start(self) -> None:
        self._intent_task = self._track(self._post_intent())

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(_IO_TIMEOUT_SECONDS),
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        )

    async def _post_json(self, path: str, payload: Mapping[str, str]) -> object:
        client = await asyncio.to_thread(self._make_client)
        try:
            response = await asyncio.wait_for(
                client.post(f"{self.base_url}{path}", json=dict(payload)),
                timeout=_IO_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()
        finally:
            await client.aclose()

    async def _post_intent(self) -> object | None:
        try:
            if not await asyncio.to_thread(_validate, "intent", self.intent):
                _warning("kis_live_witness_intent_invalid")
                return None
            response = await self._post_json("/v1/commands", self.intent)
            if not isinstance(response, dict) or not await asyncio.to_thread(
                _validate, "receipt", response
            ):
                _warning("kis_live_witness_receipt_invalid")
                return None
            if response["command_id"] != self.intent["command_id"]:
                _warning("kis_live_witness_receipt_mismatch")
                return None
            receipt_data = {
                "witness_id": response["witness_id"],
                "command_id": response["command_id"],
            }
            logger.info("kis_live_witness_receipt_recorded", extra=receipt_data)
            _breadcrumb("kis_live_witness_receipt_recorded", receipt_data)
            return response
        except Exception:
            _warning("kis_live_witness_intent_failed")
            return None

    def capture_raw_echo(self, response: Mapping[str, object]) -> None:
        """Capture raw KIS fields before the domestic client normalizes them."""
        output = response.get("output")
        echo = {
            "ODNO": output.get("ODNO") if isinstance(output, dict) else None,
            "rt_cd": response.get("rt_cd"),
            "msg_cd": response.get("msg_cd"),
            "msg1": response.get("msg1"),
            "received_at": _utc_now(),
        }
        if not all(isinstance(value, str) for value in echo.values()):
            _warning("kis_live_witness_echo_invalid")
            return
        self._track(self._post_echo(echo))

    async def _post_echo(self, echo: Mapping[str, str]) -> None:
        if self._intent_task is None:
            _warning("kis_live_witness_echo_without_intent")
            return
        try:
            receipt = await asyncio.wait_for(
                asyncio.shield(self._intent_task),
                timeout=_ECHO_DEPENDENCY_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            _warning("kis_live_witness_echo_dependency_timeout")
            return
        except Exception:
            _warning("kis_live_witness_echo_dependency_failed")
            return
        if not isinstance(receipt, dict):
            _warning("kis_live_witness_echo_missing_receipt")
            return
        if not await asyncio.to_thread(_validate, "echo", echo):
            _warning("kis_live_witness_echo_invalid")
            return
        try:
            command_id = receipt["command_id"]
            await self._post_json(
                f"/v1/commands/{quote(command_id, safe='')}/echo", echo
            )
        except Exception:
            _warning("kis_live_witness_echo_failed")


_ACTIVE_WITNESS: contextvars.ContextVar[LiveShadowWitness | None] = (
    contextvars.ContextVar("kis_live_shadow_witness", default=None)
)


def start_kis_live_shadow_witness(
    *,
    command_id: str | None,
    side: str,
    stock_code: str,
    quantity: object,
    price: object,
    kis_order_code: str,
) -> LiveShadowWitness | None:
    """Build and schedule a valid witness intent without affecting an order."""
    if not _enabled():
        return None
    base_url = witness_base_url()
    if base_url is None:
        _warning("kis_live_witness_skipped", order_type=kis_order_code)
        return None
    if kis_order_code != "00":
        _warning("kis_live_witness_skipped", order_type=kis_order_code)
        return None
    intent = {
        "schema_version": "execution-command/v1",
        "command_id": command_id or "",
        "account_scope": "kis_live",
        "side": side,
        "stock_code": stock_code,
        "quantity": str(quantity),
        "price": str(price),
        "order_type": "limit",
        "issued_at": _utc_now(),
    }
    if not (stock_code.isascii() and stock_code.isdecimal() and len(stock_code) == 6):
        _warning("kis_live_witness_skipped", order_type=kis_order_code)
        return None
    witness = LiveShadowWitness(base_url=base_url, intent=intent)
    witness.start()
    return witness


def activate(
    witness: LiveShadowWitness | None,
) -> contextvars.Token[LiveShadowWitness | None]:
    return _ACTIVE_WITNESS.set(witness)


def deactivate(token: contextvars.Token[LiveShadowWitness | None]) -> None:
    _ACTIVE_WITNESS.reset(token)


def current() -> LiveShadowWitness | None:
    return _ACTIVE_WITNESS.get()
