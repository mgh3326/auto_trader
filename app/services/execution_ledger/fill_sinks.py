"""Ledger sinks for the websocket monitor (fillwire P0, ``WS_LEDGER_SINK``).

``db`` (default) keeps the pre-existing behaviour exactly: the monitor commits
the normalized :class:`~app.schemas.execution_ledger.ExecutionLedgerUpsert`
through the repository itself and then runs the shared downstream work.

``http`` posts the *same* normalized upsert to the localhost ingest API so the
contract the Go ``fillwire`` daemon will use is exercised live. On that path
the API server owns the downstream work, so the monitor does not run it twice.

Loss policy (spec §3 P0: "체결 유실 0"):

1. a failed POST (transport error, timeout, non-2xx, malformed body, or a
   ``rejected`` item) parks the fill in a bounded FIFO retry queue and warns;
2. later deliveries drain that queue before posting anything new, so ordering
   is preserved;
3. once an entry has burned ``max_attempts`` posts — or the queue is at its
   cap and must make room — the fill is **not dropped**: it falls open to the
   direct DB sink (which also runs the downstream work) and bumps
   ``sink_fallback``;
4. shutdown flushes whatever is still queued through the same fallback.

The one residual loss boundary is a DB fallback that itself fails: there is
nowhere left to put the fill. That is never silent — it logs at ERROR and
increments ``sink_fallback_failed``, which the heartbeat exposes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.schemas.execution_ledger_ingest import IngestSource
from app.services.execution_ledger.fill_ingest import (
    DownstreamHooks,
    commit_fill,
    run_post_upsert_downstream,
)
from app.services.execution_ledger.repository import (
    ExecutionLedgerRepository,
    UpsertStatus,
)
from app.services.fill_notification import FillOrder

logger = logging.getLogger(__name__)

SINK_MODE_DB = "db"
SINK_MODE_HTTP = "http"
VALID_SINK_MODES = frozenset({SINK_MODE_DB, SINK_MODE_HTTP})

_ACCEPTED_ITEM_STATUSES = frozenset({"inserted", "updated", "unchanged"})

#: The only path this sink may post to, and the only hosts it may reach. The
#: request carries the ingest token, so an operator-supplied ``WS_LEDGER_SINK_URL``
#: must not be able to ship that token to another host, another path, or into
#: userinfo/query text. Checked when the sink is built *and* again immediately
#: before every send.
INGEST_URL_PATH = "/trading/api/execution-ledger/fills/ingest"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ALLOWED_URL_SCHEMES = frozenset({"http"})


class InvalidIngestUrl(ValueError):
    """Raised when ``WS_LEDGER_SINK_URL`` is not a loopback ingest URL."""


def assert_loopback_ingest_url(url: str) -> None:
    """Fail closed on any URL that could carry the token off this machine.

    The message names *what* was wrong, never the URL itself — a bad URL can
    contain credentials in its userinfo.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url or ""))
    except ValueError as exc:  # pragma: no cover - urlsplit is very permissive
        raise InvalidIngestUrl("unparseable") from exc

    if parts.scheme not in ALLOWED_URL_SCHEMES:
        raise InvalidIngestUrl("scheme_not_allowed")
    if parts.username is not None or parts.password is not None:
        raise InvalidIngestUrl("userinfo_not_allowed")
    if parts.query:
        raise InvalidIngestUrl("query_not_allowed")
    if parts.fragment:
        raise InvalidIngestUrl("fragment_not_allowed")
    hostname = (parts.hostname or "").strip().lower()
    if hostname not in LOOPBACK_HOSTS:
        raise InvalidIngestUrl("host_not_loopback")
    if parts.path != INGEST_URL_PATH:
        raise InvalidIngestUrl("path_not_ingest")


@dataclass(frozen=True)
class SinkContext:
    """Everything a sink needs beyond the normalized upsert itself.

    ``session_factory``/``repository_cls`` are carried explicitly so the
    monitor keeps writing through its own module-level names (its long-standing
    test patch points) rather than through this module's imports.
    """

    broker: str
    fill_order: FillOrder | None = None
    raw_event: dict[str, Any] | None = None
    correlation_id: str | None = None
    hooks: DownstreamHooks = field(default_factory=DownstreamHooks)
    session_factory: Callable[[], Any] | None = None
    repository_cls: type[ExecutionLedgerRepository] | None = None


class DirectDbFillSink:
    """Commit straight through the repository service layer."""

    mode = SINK_MODE_DB

    def __init__(
        self,
        *,
        owns_downstream: bool = False,
        commit: Callable[..., Awaitable[tuple[UpsertStatus, int]]] | None = None,
        downstream: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        #: ``True`` only when this sink is the HTTP sink's fallback: the
        #: monitor's own ``db`` path runs downstream itself, exactly as before.
        self._owns_downstream = owns_downstream
        self._commit = commit or commit_fill
        self._downstream = downstream or run_post_upsert_downstream
        self.rows_committed = 0

    async def deliver(
        self, fill: ExecutionLedgerUpsert, context: SinkContext
    ) -> UpsertStatus:
        status, row_id = await self._commit(
            fill,
            session_factory=context.session_factory,
            repository_cls=context.repository_cls,
        )
        self.rows_committed += 1
        logger.info(
            "Execution ledger websocket upsert committed: broker=%s symbol=%s "
            "order_id=%s fill_seq=%s status=%s row_id=%s",
            fill.broker,
            fill.symbol,
            fill.broker_order_id,
            fill.fill_seq,
            status,
            row_id,
        )
        if self._owns_downstream:
            await self._downstream(
                broker=context.broker,
                upsert_status=status,
                fill_order=context.fill_order,
                raw_event=context.raw_event,
                correlation_id=context.correlation_id,
                hooks=context.hooks,
            )
        return status

    async def flush(self) -> None:
        """No buffered state: a direct commit is already durable."""
        return None

    def stats(self) -> dict[str, Any]:
        return {"mode": self.mode, "rows_committed": self.rows_committed}


@dataclass
class _QueuedFill:
    fill: ExecutionLedgerUpsert
    context: SinkContext
    attempts: int = 0


class HttpFillIngestSink:
    """POST fills to the localhost execution-ledger ingest API."""

    mode = SINK_MODE_HTTP

    def __init__(
        self,
        *,
        url: str,
        token: str,
        header_name: str,
        timeout_seconds: float,
        max_queue: int,
        max_attempts: int,
        fallback: DirectDbFillSink,
        source: IngestSource = "websocket_monitor",
        source_run_id: uuid.UUID | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._url = url
        self._token = token
        self._header_name = header_name or ""
        self._timeout_seconds = float(timeout_seconds)
        self._max_queue = max(1, int(max_queue))
        self._max_attempts = max(1, int(max_attempts))
        self._fallback = fallback
        self._source = source
        # Transport run authority for every fill this process posts. Stable for
        # the life of the sink so a retry of the same fill carries the *same*
        # run id the first attempt did.
        self._source_run_id = source_run_id or uuid.uuid4()
        self._client_factory = client_factory
        self._queue: deque[_QueuedFill] = deque()
        self._lock = asyncio.Lock()
        self.http_success = 0
        self.http_failure = 0
        self.sink_fallback = 0
        self.sink_fallback_failed = 0
        self.url_blocked = 0
        # Build-time check. A rejected URL does not raise here: the sink stays
        # constructible and simply never opens a socket, failing every fill
        # open to the direct DB path instead.
        self._url_error: str | None = None
        try:
            assert_loopback_ingest_url(self._url)
        except InvalidIngestUrl as exc:
            self._url_error = str(exc)
            logger.error(
                "WS_LEDGER_SINK_URL rejected (%s): the execution-ledger HTTP sink "
                "will not send and every fill falls open to the direct DB path",
                self._url_error,
            )

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    @property
    def source_run_id(self) -> uuid.UUID:
        return self._source_run_id

    def _url_rejection(self) -> str | None:
        """Why the *current* URL may not be sent to, or ``None`` if it may."""
        try:
            assert_loopback_ingest_url(self._url)
        except InvalidIngestUrl as exc:
            return str(exc)
        return None

    async def deliver(
        self, fill: ExecutionLedgerUpsert, context: SinkContext
    ) -> UpsertStatus | None:
        async with self._lock:
            rejection = self._url_rejection()
            if rejection is not None:
                # Never reached the network. Fail open immediately — retrying a
                # URL that can never be allowed would only park fills — and
                # drain anything already queued the same way.
                self.url_blocked += 1
                logger.error(
                    "Execution ledger HTTP sink refusing to send (%s): broker=%s "
                    "order_id=%s — failing open to the direct DB path",
                    rejection,
                    fill.broker,
                    fill.broker_order_id,
                )
                while self._queue:
                    await self._fail_open(self._queue.popleft())
                await self._fail_open(_QueuedFill(fill, context))
                return None
            await self._drain_locked()
            if self._queue:
                # Something older is still stuck; queue behind it so the
                # ledger sees fills in the order the broker sent them.
                await self._enqueue_locked(_QueuedFill(fill, context))
                return None
            status = await self._post(fill, context)
            if status is not None:
                return status
            item = _QueuedFill(fill, context, attempts=1)
            if item.attempts >= self._max_attempts:
                # Already out of attempts: fail open now rather than parking an
                # exhausted entry that only the next delivery would notice.
                await self._fail_open(item)
                return None
            await self._enqueue_locked(item)
            return None

    async def flush(self) -> None:
        """Drain everything still queued so shutdown never loses a fill."""
        async with self._lock:
            await self._drain_locked()
            while self._queue:
                item = self._queue.popleft()
                await self._fail_open(item)

    def stats(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "queue_depth": self.queue_depth,
            "http_success": self.http_success,
            "http_failure": self.http_failure,
            "sink_fallback": self.sink_fallback,
            "sink_fallback_failed": self.sink_fallback_failed,
            "url_blocked": self.url_blocked,
        }

    async def _drain_locked(self) -> None:
        while self._queue:
            item = self._queue[0]
            status = await self._post(item.fill, item.context)
            if status is not None:
                self._queue.popleft()
                continue
            item.attempts += 1
            if item.attempts >= self._max_attempts:
                self._queue.popleft()
                await self._fail_open(item)
                continue
            # Still retryable: stop draining rather than hammering a sink that
            # just refused us, and let the next delivery (or flush) try again.
            break

    async def _enqueue_locked(self, item: _QueuedFill) -> None:
        while len(self._queue) >= self._max_queue:
            oldest = self._queue.popleft()
            logger.warning(
                "Execution ledger HTTP sink queue at cap (%s): failing the oldest "
                "fill open to the direct DB path broker=%s order_id=%s",
                self._max_queue,
                oldest.fill.broker,
                oldest.fill.broker_order_id,
            )
            await self._fail_open(oldest)
        self._queue.append(item)

    async def _fail_open(self, item: _QueuedFill) -> None:
        self.sink_fallback += 1
        try:
            await self._fallback.deliver(item.fill, item.context)
        except Exception as exc:  # noqa: BLE001 - loud, counted, never silent
            self.sink_fallback_failed += 1
            logger.error(
                "Execution ledger sink fallback to direct DB failed: broker=%s "
                "order_id=%s fill_seq=%s error=%s",
                item.fill.broker,
                item.fill.broker_order_id,
                item.fill.fill_seq,
                exc,
                exc_info=True,
            )
            return
        logger.warning(
            "Execution ledger HTTP sink fell open to the direct DB path: "
            "broker=%s order_id=%s fill_seq=%s attempts=%s sink_fallback=%s",
            item.fill.broker,
            item.fill.broker_order_id,
            item.fill.fill_seq,
            item.attempts,
            self.sink_fallback,
        )

    def _build_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        import httpx

        # ``follow_redirects`` is pinned off explicitly rather than relying on
        # the httpx default: a 3xx is the one way a validated request could
        # otherwise forward the ingest token to another host.
        return httpx.AsyncClient(timeout=self._timeout_seconds, follow_redirects=False)

    async def _post(
        self, fill: ExecutionLedgerUpsert, context: SinkContext
    ) -> UpsertStatus | None:
        """One POST attempt. Returns the item status, or ``None`` on failure.

        Never logs the token or the payload — only the broker identity of the
        fill and a bounded transport verdict.
        """
        payload = {
            "fills": [fill.model_dump(mode="json")],
            "source": self._source,
            "source_run_id": str(self._source_run_id),
        }
        headers = {self._header_name: self._token} if self._header_name else {}
        try:
            # Re-checked immediately before the send, not just at build time.
            assert_loopback_ingest_url(self._url)
        except InvalidIngestUrl as exc:
            self.url_blocked += 1
            logger.error(
                "Execution ledger HTTP sink refusing to send (%s): broker=%s "
                "order_id=%s",
                exc,
                fill.broker,
                fill.broker_order_id,
            )
            return None
        try:
            client = self._build_client()
            async with client as http:
                response = await http.post(
                    self._url,
                    json=payload,
                    headers=headers,
                    follow_redirects=False,
                )
            if response.status_code // 100 != 2:
                self.http_failure += 1
                logger.warning(
                    "Execution ledger HTTP sink refused: broker=%s order_id=%s "
                    "status_code=%s",
                    fill.broker,
                    fill.broker_order_id,
                    response.status_code,
                )
                return None
            body = response.json()
            results = body.get("results") if isinstance(body, dict) else None
            if not isinstance(results, list) or len(results) != 1:
                self.http_failure += 1
                logger.warning(
                    "Execution ledger HTTP sink returned a malformed body: "
                    "broker=%s order_id=%s",
                    fill.broker,
                    fill.broker_order_id,
                )
                return None
            item = results[0]
            status = item.get("status") if isinstance(item, dict) else None
            if status not in _ACCEPTED_ITEM_STATUSES:
                self.http_failure += 1
                logger.warning(
                    "Execution ledger HTTP sink rejected the fill: broker=%s "
                    "order_id=%s item_status=%s",
                    fill.broker,
                    fill.broker_order_id,
                    status,
                )
                return None
        except Exception as exc:  # noqa: BLE001 - transport failure is retryable
            self.http_failure += 1
            logger.warning(
                "Execution ledger HTTP sink transport error: broker=%s "
                "order_id=%s error=%s",
                fill.broker,
                fill.broker_order_id,
                exc.__class__.__name__,
            )
            return None
        self.http_success += 1
        logger.info(
            "Execution ledger HTTP sink accepted: broker=%s symbol=%s order_id=%s "
            "fill_seq=%s status=%s",
            fill.broker,
            fill.symbol,
            fill.broker_order_id,
            fill.fill_seq,
            status,
        )
        return status  # type: ignore[return-value]


def resolve_sink_mode(raw_mode: str | None) -> str:
    """Normalize ``WS_LEDGER_SINK``; anything unrecognized stays on ``db``."""
    mode = str(raw_mode or SINK_MODE_DB).strip().lower()
    if mode not in VALID_SINK_MODES:
        logger.warning(
            "Unknown WS_LEDGER_SINK value %r — falling back to the direct DB sink",
            raw_mode,
        )
        return SINK_MODE_DB
    return mode


def build_ledger_fill_sink(
    settings_obj: Any,
    *,
    client_factory: Callable[[], Any] | None = None,
) -> DirectDbFillSink | HttpFillIngestSink:
    """Build the configured sink. Default (and any bad value) is ``db``."""
    mode = resolve_sink_mode(getattr(settings_obj, "WS_LEDGER_SINK", SINK_MODE_DB))
    if mode == SINK_MODE_DB:
        return DirectDbFillSink()
    if not getattr(settings_obj, "EXECUTION_LEDGER_INGEST_TOKEN", ""):
        # Name the missing key, never a value; the sink would 401 on every
        # post and fall open to the DB anyway.
        logger.warning(
            "WS_LEDGER_SINK=http but EXECUTION_LEDGER_INGEST_TOKEN is unset — "
            "every post will be refused and fail open to the direct DB path"
        )
    return HttpFillIngestSink(
        url=settings_obj.WS_LEDGER_SINK_URL,
        token=settings_obj.EXECUTION_LEDGER_INGEST_TOKEN,
        header_name=settings_obj.EXECUTION_LEDGER_INGEST_TOKEN_HEADER,
        timeout_seconds=settings_obj.WS_LEDGER_SINK_TIMEOUT_SECONDS,
        max_queue=settings_obj.WS_LEDGER_SINK_MAX_QUEUE,
        max_attempts=settings_obj.WS_LEDGER_SINK_MAX_ATTEMPTS,
        fallback=DirectDbFillSink(owns_downstream=True),
        client_factory=client_factory,
    )
