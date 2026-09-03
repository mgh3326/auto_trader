"""Low-cardinality timing headers and Sentry names for ``/invest/api``."""

from __future__ import annotations

import time
from datetime import datetime

import sentry_sdk
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_INVEST_API_PREFIX = "/invest/api/"


def _duration_ms(span: object) -> float | None:
    """Return a finished Sentry span's duration without depending on SDK internals."""
    start = getattr(span, "start_timestamp", None)
    end = getattr(span, "timestamp", None)
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return max(0.0, (end - start).total_seconds() * 1000)


def _span_metrics(span: object | None) -> tuple[float, float, str | None]:
    """Summarize finished child spans attached to the current transaction.

    The recorder is intentionally read-only.  It is the SDK's only per-request
    collection that includes both SQLAlchemy's ``db.*`` and HTTPX's
    ``http.client`` spans before a response header is emitted.
    """
    transaction = getattr(span, "containing_transaction", None) or span
    recorder = getattr(transaction, "_span_recorder", None)
    spans = getattr(recorder, "spans", ())
    if not isinstance(spans, (list, tuple)):
        return 0.0, 0.0, None

    db_ms = 0.0
    ext_ms = 0.0
    cache_seen = False
    cache_hit = False
    for child in spans:
        duration = _duration_ms(child)
        if duration is None:
            continue
        op = getattr(child, "op", "")
        if not isinstance(op, str):
            continue
        if op.startswith("db."):
            db_ms += duration
        elif op == "http.client":
            ext_ms += duration
        elif op.startswith("cache."):
            cache_seen = True
            data = getattr(child, "_data", {})
            tags = getattr(child, "_tags", {})
            if isinstance(data, dict) and data.get("cache.hit") is True:
                cache_hit = True
            if isinstance(tags, dict) and tags.get("cache.hit") in {True, "true"}:
                cache_hit = True

    return db_ms, ext_ms, ("hit" if cache_hit else "miss") if cache_seen else None


def _route_transaction_name(scope: Scope) -> str | None:
    route = scope.get("route")
    path = getattr(route, "path", None)
    method = scope.get("method")
    if not isinstance(path, str) or not path.startswith(_INVEST_API_PREFIX):
        return None
    if not isinstance(method, str):
        return None
    return f"{method.upper()} {path}"


def _server_timing_header(
    *, total_ms: float, db_ms: float, ext_ms: float, cache: str | None
) -> str:
    values = [
        f"total;dur={total_ms:.1f}",
        f"db;dur={db_ms:.1f}",
        f"ext;dur={ext_ms:.1f}",
    ]
    if cache is not None:
        values.append(f"cache;desc={cache}")
    return ", ".join(values)


class InvestTimingMiddleware:
    """Attach ``Server-Timing`` only to the authenticated invest API surface."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(
            _INVEST_API_PREFIX
        ):
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        response_started = False

        async def send_timed(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start" and not response_started:
                response_started = True
                name = _route_transaction_name(scope)
                if name is not None:
                    sentry_sdk.set_transaction_name(name, source="route")
                db_ms, ext_ms, cache = _span_metrics(sentry_sdk.get_current_span())
                MutableHeaders(scope=message)["Server-Timing"] = _server_timing_header(
                    total_ms=(time.perf_counter() - started) * 1000,
                    db_ms=db_ms,
                    ext_ms=ext_ms,
                    cache=cache,
                )
            await send(message)

        await self.app(scope, receive, send_timed)
