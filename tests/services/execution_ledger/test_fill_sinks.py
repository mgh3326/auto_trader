"""``WS_LEDGER_SINK`` sinks (fillwire P0): equivalence, retry, fail-open, flush.

No real HTTP and no real DB: the transport client and the commit function are
both injected.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services.execution_ledger.fill_ingest import DownstreamHooks
from app.services.execution_ledger.fill_sinks import (
    DirectDbFillSink,
    HttpFillIngestSink,
    InvalidIngestUrl,
    SinkContext,
    assert_loopback_ingest_url,
    build_ledger_fill_sink,
    resolve_sink_mode,
)


def _fill(**overrides: Any) -> ExecutionLedgerUpsert:
    data: dict[str, Any] = {
        "broker": "upbit",
        "account_mode": "live",
        "venue": "upbit_krw",
        "instrument_type": "crypto",
        "symbol": "BTC",
        "raw_symbol": "KRW-BTC",
        "side": "buy",
        "broker_order_id": "sink-order-1",
        "fill_seq": 0,
        "filled_qty": Decimal("0.0003"),
        "filled_price": Decimal("92800000"),
        "filled_at": datetime(2026, 9, 7, 3, 0, tzinfo=UTC),
        "currency": "KRW",
        "source": "websocket",
    }
    data.update(overrides)
    return ExecutionLedgerUpsert(**data)


def _context(**overrides: Any) -> SinkContext:
    base: dict[str, Any] = {
        "broker": "upbit",
        "correlation_id": "corr-1",
        "hooks": DownstreamHooks(),
    }
    base.update(overrides)
    return SinkContext(**base)


class _Response:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


class _FakeClient:
    """One-shot ``httpx.AsyncClient`` stand-in with a scripted response queue."""

    def __init__(self, owner: _FakeTransport) -> None:
        self._owner = owner

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        json: Any,
        headers: dict[str, str],
        follow_redirects: bool,
    ) -> _Response:
        self._owner.requests.append(
            {
                "url": url,
                "json": json,
                "headers": headers,
                "follow_redirects": follow_redirects,
            }
        )
        outcome = self._owner.next_outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeTransport:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict[str, Any]] = []

    def next_outcome(self) -> Any:
        if len(self.outcomes) > 1:
            return self.outcomes.pop(0)
        return self.outcomes[0]

    def __call__(self) -> _FakeClient:
        return _FakeClient(self)


def _ok(status: str = "inserted", row_id: int = 11) -> _Response:
    return _Response(
        200,
        {
            "source": "websocket_monitor",
            "source_run_id": None,
            "received": 1,
            "accepted": 1,
            "rejected": 0,
            "results": [{"status": status, "row_id": row_id, "reason": None}],
        },
    )


class _RecordingCommit:
    def __init__(self, status: str = "inserted", fail: bool = False) -> None:
        self.calls: list[ExecutionLedgerUpsert] = []
        self._status = status
        self._fail = fail

    async def __call__(self, fill, *, session_factory=None, repository_cls=None):
        self.calls.append(fill)
        if self._fail:
            raise RuntimeError("db is down")
        return self._status, 42


class _RecordingDownstream:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _http_sink(
    outcomes: list[Any],
    *,
    commit: _RecordingCommit | None = None,
    downstream: _RecordingDownstream | None = None,
    max_queue: int = 10,
    max_attempts: int = 3,
    url: str = "http://127.0.0.1:8000/trading/api/execution-ledger/fills/ingest",
) -> tuple[HttpFillIngestSink, _FakeTransport, _RecordingCommit, _RecordingDownstream]:
    commit = commit or _RecordingCommit()
    downstream = downstream or _RecordingDownstream()
    transport = _FakeTransport(outcomes)
    sink = HttpFillIngestSink(
        url=url,
        token="ledger-secret",
        header_name="X-Execution-Ledger-Ingest-Token",
        timeout_seconds=1.0,
        max_queue=max_queue,
        max_attempts=max_attempts,
        fallback=DirectDbFillSink(
            owns_downstream=True, commit=commit, downstream=downstream
        ),
        client_factory=transport,
    )
    return sink, transport, commit, downstream


# --- URL trust boundary ----------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/trading/api/execution-ledger/fills/ingest",
        "http://localhost:8000/trading/api/execution-ledger/fills/ingest",
        "http://[::1]:8000/trading/api/execution-ledger/fills/ingest",
        "http://LOCALHOST:9999/trading/api/execution-ledger/fills/ingest",
    ],
)
def test_loopback_ingest_urls_are_allowed(url: str) -> None:
    assert_loopback_ingest_url(url)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "reason"),
    [
        # another host would receive the ingest token
        (
            "http://evil.example.com/trading/api/execution-ledger/fills/ingest",
            "host_not_loopback",
        ),
        (
            "http://127.0.0.1.evil.example.com/trading/api/execution-ledger/fills/ingest",
            "host_not_loopback",
        ),
        (
            "http://10.0.0.5:8000/trading/api/execution-ledger/fills/ingest",
            "host_not_loopback",
        ),
        # userinfo is a credential channel of its own
        (
            "http://user:pw@127.0.0.1:8000/trading/api/execution-ledger/fills/ingest",
            "userinfo_not_allowed",
        ),
        # a different path is a different (possibly logging) handler
        ("http://127.0.0.1:8000/anything-else", "path_not_ingest"),
        ("http://127.0.0.1:8000/", "path_not_ingest"),
        # query/fragment can smuggle text alongside the token
        (
            "http://127.0.0.1:8000/trading/api/execution-ledger/fills/ingest?x=1",
            "query_not_allowed",
        ),
        (
            "http://127.0.0.1:8000/trading/api/execution-ledger/fills/ingest#f",
            "fragment_not_allowed",
        ),
        # non-http schemes
        (
            "https://127.0.0.1:8000/trading/api/execution-ledger/fills/ingest",
            "scheme_not_allowed",
        ),
        ("file:///etc/passwd", "scheme_not_allowed"),
        ("", "scheme_not_allowed"),
    ],
)
def test_non_loopback_ingest_urls_are_rejected(url: str, reason: str) -> None:
    with pytest.raises(InvalidIngestUrl) as excinfo:
        assert_loopback_ingest_url(url)
    assert str(excinfo.value) == reason


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_url_never_opens_a_socket_and_fails_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("ERROR")
    sink, transport, commit, downstream = _http_sink(
        [_ok()], url="http://evil.example.com/trading/api/execution-ledger/fills/ingest"
    )

    status = await sink.deliver(_fill(), _context())

    assert status is None
    assert transport.requests == [], "no request may leave for a non-loopback host"
    assert sink.url_blocked == 1
    assert sink.stats()["url_blocked"] == 1
    assert commit.calls, "the fill still reaches the DB"
    assert downstream.calls, "the fallback owns downstream"
    assert "host_not_loopback" in caplog.text
    assert "ledger-secret" not in caplog.text
    assert "evil.example.com" not in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_url_mutated_after_construction_is_blocked_before_send() -> None:
    """The URL is re-validated immediately before every send, not only at build."""
    sink, transport, commit, _d = _http_sink([_ok()])
    sink._url = "http://evil.example.com/trading/api/execution-ledger/fills/ingest"

    assert await sink.deliver(_fill(), _context()) is None

    assert transport.requests == []
    assert sink.url_blocked == 1
    assert commit.calls, "blocked sends still fail open to the DB"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redirects_are_never_followed() -> None:
    redirect = _Response(302, {})
    redirect.headers = {"location": "http://evil.example.com/collect"}  # type: ignore[attr-defined]
    sink, transport, _c, _d = _http_sink([redirect])

    status = await sink.deliver(_fill(), _context())

    assert status is None, "a 3xx is a failure, not an accepted fill"
    assert len(transport.requests) == 1, "the redirect target is never requested"
    assert transport.requests[0]["follow_redirects"] is False
    assert sink.http_failure == 1


@pytest.mark.unit
def test_default_http_client_pins_redirects_off() -> None:
    sink, _t, _c, _d = _http_sink([_ok()])
    sink._client_factory = None

    client = sink._build_client()
    try:
        assert client.follow_redirects is False
    finally:
        pass


# --- mode resolution -------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, "db"), ("", "db"), ("db", "db"), ("HTTP", "http"), ("kafka", "db")],
)
def test_resolve_sink_mode_defaults_to_db(raw: str | None, expected: str) -> None:
    assert resolve_sink_mode(raw) == expected


@pytest.mark.unit
def test_build_ledger_fill_sink_defaults_to_direct_db() -> None:
    from app.core.config import settings

    sink = build_ledger_fill_sink(settings)
    assert settings.WS_LEDGER_SINK == "db", "the shipped default must stay db"
    assert isinstance(sink, DirectDbFillSink)
    assert sink.mode == "db"


@pytest.mark.unit
def test_build_ledger_fill_sink_http_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "WS_LEDGER_SINK", "http", raising=False)
    sink = build_ledger_fill_sink(settings)
    assert isinstance(sink, HttpFillIngestSink)


# --- direct DB sink --------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_direct_db_sink_does_not_run_downstream_by_default() -> None:
    commit = _RecordingCommit()
    downstream = _RecordingDownstream()
    sink = DirectDbFillSink(commit=commit, downstream=downstream)

    status = await sink.deliver(_fill(), _context())

    assert status == "inserted"
    assert commit.calls[0].broker_order_id == "sink-order-1"
    assert downstream.calls == [], "the monitor owns downstream on the db path"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_db_sink_owns_downstream() -> None:
    commit = _RecordingCommit()
    downstream = _RecordingDownstream()
    sink = DirectDbFillSink(owns_downstream=True, commit=commit, downstream=downstream)

    await sink.deliver(_fill(), _context(broker="upbit"))

    assert len(downstream.calls) == 1
    assert downstream.calls[0]["upsert_status"] == "inserted"


# --- HTTP sink happy path --------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_sink_posts_the_same_normalized_upsert() -> None:
    sink, transport, commit, downstream = _http_sink([_ok()])
    fill = _fill()

    status = await sink.deliver(fill, _context())

    assert status == "inserted"
    assert commit.calls == [], "no local write when the API accepted it"
    assert downstream.calls == [], "the API server owns downstream on the http path"
    request = transport.requests[0]
    assert request["json"]["fills"] == [fill.model_dump(mode="json")]
    assert request["json"]["source"] == "websocket_monitor"
    assert request["headers"]["X-Execution-Ledger-Ingest-Token"] == "ledger-secret"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_sink_sends_a_process_stable_source_run_id() -> None:
    sink, transport, _commit, _downstream = _http_sink([_ok()])

    await sink.deliver(_fill(), _context())
    await sink.deliver(_fill(broker_order_id="sink-order-2"), _context())

    run_ids = {request["json"]["source_run_id"] for request in transport.requests}
    assert len(run_ids) == 1
    assert uuid.UUID(run_ids.pop()) == sink.source_run_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_keeps_the_same_source_run_id() -> None:
    sink, transport, _commit, _downstream = _http_sink(
        [RuntimeError("connection reset"), _ok()]
    )

    await sink.deliver(_fill(), _context())  # fails -> queued
    await sink.deliver(_fill(broker_order_id="sink-order-2"), _context())  # drains

    run_ids = {request["json"]["source_run_id"] for request in transport.requests}
    assert len(run_ids) == 1
    assert uuid.UUID(run_ids.pop()) == sink.source_run_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_and_db_sinks_produce_the_same_row_status() -> None:
    http_sink, _t, _c, _d = _http_sink([_ok("unchanged", row_id=7)])
    db_commit = _RecordingCommit(status="unchanged")
    db_sink = DirectDbFillSink(commit=db_commit)

    assert await http_sink.deliver(_fill(), _context()) == "unchanged"
    assert await db_sink.deliver(_fill(), _context()) == "unchanged"


# --- HTTP sink retry / fail-open ------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transport_failure_queues_instead_of_dropping() -> None:
    sink, _t, commit, _d = _http_sink([RuntimeError("connection reset")])

    status = await sink.deliver(_fill(), _context())

    assert status is None
    assert sink.queue_depth == 1
    assert commit.calls == [], "not yet exhausted, so no fallback"
    assert sink.sink_fallback == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_queued_fill_is_retried_and_succeeds() -> None:
    sink, transport, commit, _d = _http_sink([RuntimeError("boom"), _ok()])

    first = _fill()
    assert await sink.deliver(first, _context()) is None
    # A later delivery drains the queue first, preserving broker order.
    await sink.deliver(_fill(broker_order_id="sink-order-2"), _context())

    posted_ids = [
        request["json"]["fills"][0]["broker_order_id"] for request in transport.requests
    ]
    assert posted_ids == ["sink-order-1", "sink-order-1", "sink-order-2"]
    assert sink.queue_depth == 0
    assert commit.calls == []
    assert sink.sink_fallback == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_exhaustion_fails_open_to_the_direct_db_path() -> None:
    sink, _t, commit, downstream = _http_sink(
        [RuntimeError("still down")], max_attempts=3
    )
    fill = _fill()

    assert await sink.deliver(fill, _context()) is None  # attempt 1
    assert await sink.deliver(_fill(broker_order_id="x2"), _context()) is None  # 2
    assert await sink.deliver(_fill(broker_order_id="x3"), _context()) is None  # 3

    assert sink.sink_fallback >= 1
    assert commit.calls, "the exhausted fill must reach the DB, not be dropped"
    assert commit.calls[0].broker_order_id == fill.broker_order_id
    assert downstream.calls, "the fallback owns downstream"
    assert sink.sink_fallback_failed == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_queue_cap_fails_the_oldest_open_instead_of_dropping_it() -> None:
    sink, _t, commit, _d = _http_sink(
        [RuntimeError("down")], max_queue=1, max_attempts=99
    )

    await sink.deliver(_fill(broker_order_id="old"), _context())
    assert sink.queue_depth == 1
    await sink.deliver(_fill(broker_order_id="new"), _context())

    assert sink.queue_depth == 1
    assert sink.sink_fallback == 1
    assert [call.broker_order_id for call in commit.calls] == ["old"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_2xx_is_a_failure() -> None:
    sink, _t, _c, _d = _http_sink([_Response(401, {"detail": "Invalid token"})])

    assert await sink.deliver(_fill(), _context()) is None
    assert sink.http_failure == 1
    assert sink.queue_depth == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_body_is_a_failure() -> None:
    sink, _t, _c, _d = _http_sink([_Response(200, {"results": []})])

    assert await sink.deliver(_fill(), _context()) is None
    assert sink.http_failure == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rejected_item_is_a_failure() -> None:
    sink, _t, _c, _d = _http_sink([_ok("rejected", row_id=0)])

    assert await sink.deliver(_fill(), _context()) is None
    assert sink.http_failure == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_flush_drains_the_queue_through_the_db_fallback() -> None:
    sink, _t, commit, downstream = _http_sink([RuntimeError("down")], max_attempts=99)
    await sink.deliver(_fill(), _context())
    assert sink.queue_depth == 1

    await sink.flush()

    assert sink.queue_depth == 0
    assert [call.broker_order_id for call in commit.calls] == ["sink-order-1"]
    assert downstream.calls, "shutdown fallback still runs downstream"
    assert sink.sink_fallback == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_db_failure_is_counted_not_hidden(
    caplog: pytest.LogCaptureFixture,
) -> None:
    commit = _RecordingCommit(fail=True)
    sink, _t, _c, _d = _http_sink(
        [RuntimeError("down")], commit=commit, max_attempts=99
    )
    caplog.set_level("ERROR")

    await sink.deliver(_fill(), _context())
    await sink.flush()

    assert sink.sink_fallback == 1
    assert sink.sink_fallback_failed == 1
    assert "sink fallback to direct DB failed" in caplog.text


# --- observability ---------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stats_expose_mode_and_counters_without_secrets() -> None:
    sink, _t, _c, _d = _http_sink([RuntimeError("down")], max_attempts=99)
    await sink.deliver(_fill(), _context())

    stats = sink.stats()

    assert stats["mode"] == "http"
    assert stats["queue_depth"] == 1
    assert stats["http_failure"] == 1
    assert stats["sink_fallback"] == 0
    assert stats["sink_fallback_failed"] == 0
    assert "ledger-secret" not in json.dumps(stats)
    assert "token" not in json.dumps(stats).lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failure_logs_never_contain_the_token_or_the_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink, _t, _c, _d = _http_sink([_Response(401, {"detail": "Invalid token"})])
    caplog.set_level("WARNING")

    await sink.deliver(_fill(raw_payload_json={"secret": "hunter2"}), _context())

    assert "ledger-secret" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "status_code=401" in caplog.text
