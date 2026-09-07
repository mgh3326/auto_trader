"""``WS_LEDGER_SINK`` wiring in the websocket monitor (fillwire P0).

Drives the real-shaped KIS/Upbit frames the monitor already handles and proves
the ``db`` default is unchanged, that ``http`` produces the *same* normalized
upsert, and that downstream runs exactly once on either path.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services.execution_ledger.fill_sinks import (
    DirectDbFillSink,
    HttpFillIngestSink,
)
from tests.fixtures.execution_ledger_fill_frames import (
    kis_domestic_fill_frame,
    upbit_trade_frame,
)


@pytest.fixture
def ledger_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EXECUTION_LEDGER_COMMIT_ENABLED", True)
    monkeypatch.setattr(settings, "kis_ws_is_mock", False)


class _RecordingSink:
    mode = "db"

    def __init__(self, status: str | None = "inserted") -> None:
        self.fills: list[ExecutionLedgerUpsert] = []
        self.contexts: list[Any] = []
        self._status = status
        self.flushed = 0

    async def deliver(self, fill: ExecutionLedgerUpsert, context: Any):
        self.fills.append(fill)
        self.contexts.append(context)
        return self._status

    async def flush(self) -> None:
        self.flushed += 1

    def stats(self) -> dict[str, Any]:
        return {"mode": self.mode, "rows_committed": len(self.fills)}


class _Response:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return self._body


class _FakeTransport:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.requests: list[dict[str, Any]] = []

    def __call__(self):
        owner = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

            async def post(self, url, *, json, headers, follow_redirects):  # noqa: A002
                owner.requests.append(
                    {
                        "url": url,
                        "json": json,
                        "headers": headers,
                        "follow_redirects": follow_redirects,
                    }
                )
                if isinstance(owner.outcome, Exception):
                    raise owner.outcome
                return owner.outcome

        return _Client()


def _accepted() -> _Response:
    return _Response(
        200,
        {
            "source": "websocket_monitor",
            "source_run_id": None,
            "received": 1,
            "accepted": 1,
            "rejected": 0,
            "results": [{"status": "inserted", "row_id": 5, "reason": None}],
        },
    )


def _http_sink(transport: _FakeTransport, fallback: DirectDbFillSink):
    return HttpFillIngestSink(
        url="http://127.0.0.1:8000/trading/api/execution-ledger/fills/ingest",
        token="ledger-secret",
        header_name="X-Execution-Ledger-Ingest-Token",
        timeout_seconds=1.0,
        max_queue=10,
        max_attempts=1,
        fallback=fallback,
        client_factory=transport,
    )


@pytest.mark.unit
def test_default_sink_is_the_direct_db_path(ledger_settings: None) -> None:
    from websocket_monitor import UnifiedWebSocketMonitor

    monitor = UnifiedWebSocketMonitor()

    assert settings.WS_LEDGER_SINK == "db"
    assert isinstance(monitor._ledger_sink, DirectDbFillSink)
    assert monitor._ledger_sink_owns_downstream is False


@pytest.mark.unit
def test_http_mode_hands_downstream_to_the_api(
    ledger_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from websocket_monitor import UnifiedWebSocketMonitor

    monkeypatch.setattr(settings, "WS_LEDGER_SINK", "http", raising=False)
    monitor = UnifiedWebSocketMonitor()

    assert isinstance(monitor._ledger_sink, HttpFillIngestSink)
    assert monitor._ledger_sink_owns_downstream is True


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("frame", "broker"),
    [(kis_domestic_fill_frame(), "kis"), (upbit_trade_frame(), "upbit")],
)
async def test_db_and_http_sinks_receive_the_same_normalized_upsert(
    ledger_settings: None,
    monkeypatch: pytest.MonkeyPatch,
    frame: dict[str, Any],
    broker: str,
) -> None:
    from app.services.fill_notification import normalize_kis_fill, normalize_upbit_fill
    from websocket_monitor import UnifiedWebSocketMonitor

    normalize = normalize_kis_fill if broker == "kis" else normalize_upbit_fill
    order = normalize(frame)

    db_monitor = UnifiedWebSocketMonitor()
    recorder = _RecordingSink()
    db_monitor._ledger_sink = recorder
    await db_monitor._record_execution_ledger_fill(
        frame, order, broker=broker, correlation_id="corr-x"
    )

    transport = _FakeTransport(_accepted())
    http_monitor = UnifiedWebSocketMonitor()
    http_monitor._ledger_sink = _http_sink(transport, DirectDbFillSink())
    http_monitor._ledger_sink_owns_downstream = True
    await http_monitor._record_execution_ledger_fill(
        frame, order, broker=broker, correlation_id="corr-x"
    )

    assert len(recorder.fills) == 1
    posted = transport.requests[0]["json"]["fills"][0]
    assert posted == recorder.fills[0].model_dump(mode="json")
    assert posted["source"] == "websocket"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_db_mode_still_runs_downstream_locally(ledger_settings: None) -> None:
    from websocket_monitor import UnifiedWebSocketMonitor

    monitor = UnifiedWebSocketMonitor()
    monitor._ledger_sink = _RecordingSink()
    monitor._send_fill_notification = AsyncMock()

    await monitor._on_kis_execution(kis_domestic_fill_frame())

    monitor._send_fill_notification.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_mode_does_not_notify_twice(
    ledger_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API server ran downstream already; the monitor must not repeat it."""
    from websocket_monitor import UnifiedWebSocketMonitor

    monkeypatch.setattr(settings, "WS_LEDGER_SINK", "http", raising=False)
    transport = _FakeTransport(_accepted())
    monitor = UnifiedWebSocketMonitor()
    monitor._ledger_sink = _http_sink(transport, DirectDbFillSink())
    monitor._send_fill_notification = AsyncMock()
    monitor._project_upbit_proposal_fill = AsyncMock(return_value=True)

    await monitor._on_kis_execution(kis_domestic_fill_frame())
    await monitor._on_upbit_order(upbit_trade_frame())

    assert len(transport.requests) == 2
    monitor._send_fill_notification.assert_not_awaited()
    monitor._project_upbit_proposal_fill.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_mode_still_notifies_when_the_commit_gate_is_off(
    ledger_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate off means no sink is consulted, so the monitor still owns the alert."""
    from websocket_monitor import UnifiedWebSocketMonitor

    monkeypatch.setattr(settings, "WS_LEDGER_SINK", "http", raising=False)
    monkeypatch.setattr(settings, "EXECUTION_LEDGER_COMMIT_ENABLED", False)
    transport = _FakeTransport(_accepted())
    committed: list[ExecutionLedgerUpsert] = []

    async def _commit(fill, *, session_factory=None, repository_cls=None):
        committed.append(fill)
        return "inserted", 1

    monitor = UnifiedWebSocketMonitor()
    monitor._ledger_sink = _http_sink(
        transport, DirectDbFillSink(owns_downstream=True, commit=_commit)
    )
    monitor._send_fill_notification = AsyncMock()

    await monitor._on_kis_execution(kis_domestic_fill_frame())

    monitor._send_fill_notification.assert_awaited_once()
    assert transport.requests == [], "gate off must not POST"
    assert committed == [], "gate off must not write to the DB either"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_failure_falls_open_and_still_notifies_once(
    ledger_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from websocket_monitor import UnifiedWebSocketMonitor

    monkeypatch.setattr(settings, "WS_LEDGER_SINK", "http", raising=False)
    committed: list[ExecutionLedgerUpsert] = []
    notifications: list[tuple] = []

    async def _commit(fill, *, session_factory=None, repository_cls=None):
        committed.append(fill)
        return "inserted", 9

    async def _notify(*args: Any, **kwargs: Any) -> None:
        notifications.append((args, kwargs))

    fallback = DirectDbFillSink(owns_downstream=True, commit=_commit)
    transport = _FakeTransport(RuntimeError("connection reset"))
    monitor = UnifiedWebSocketMonitor()
    monitor._ledger_sink = _http_sink(transport, fallback)
    monitor._send_fill_notification = _notify  # type: ignore[method-assign]

    await monitor._on_kis_execution(kis_domestic_fill_frame())

    assert monitor._ledger_sink.sink_fallback == 1
    assert len(committed) == 1, "the fill reached the DB instead of being dropped"
    assert len(notifications) == 1, "exactly one notification, from the fallback"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_flushes_the_sink(
    ledger_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from websocket_monitor import UnifiedWebSocketMonitor

    monitor = UnifiedWebSocketMonitor()
    recorder = _RecordingSink()
    monitor._ledger_sink = recorder
    monkeypatch.setattr(monitor, "_write_heartbeat", lambda **_kwargs: None)

    await monitor.stop()

    assert recorder.flushed == 1


@pytest.mark.unit
def test_heartbeat_reports_the_sink_without_secrets(
    ledger_settings: None, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from websocket_monitor import UnifiedWebSocketMonitor

    monkeypatch.setattr(settings, "WS_LEDGER_SINK", "http", raising=False)
    monkeypatch.setattr(
        settings, "EXECUTION_LEDGER_INGEST_TOKEN", "ledger-secret", raising=False
    )
    monitor = UnifiedWebSocketMonitor()
    monitor._heartbeat_path = str(tmp_path / "heartbeat.json")

    monitor._write_heartbeat()

    data = json.loads((tmp_path / "heartbeat.json").read_text())
    assert data["ledger_sink"]["mode"] == "http"
    assert data["ledger_sink"]["sink_fallback"] == 0
    assert "ledger-secret" not in json.dumps(data)


@pytest.mark.unit
def test_sink_stats_failure_never_breaks_the_heartbeat(
    ledger_settings: None, tmp_path
) -> None:
    from websocket_monitor import UnifiedWebSocketMonitor

    class _BrokenSink(_RecordingSink):
        def stats(self) -> dict[str, Any]:
            raise RuntimeError("stats exploded")

    monitor = UnifiedWebSocketMonitor()
    monitor._ledger_sink = _BrokenSink()
    monitor._heartbeat_path = str(tmp_path / "heartbeat.json")

    monitor._write_heartbeat()

    data = json.loads((tmp_path / "heartbeat.json").read_text())
    assert data["ledger_sink"] == {"mode": "db"}
