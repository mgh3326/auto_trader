"""ROB-755 Task 3: execution_ledger_fill_events_list_recent MCP 도구 테스트.

live DB 없이 monkeypatch로 검증한다 (CLI test 패턴과 동일).
"""

from __future__ import annotations

import types
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.mcp_server.tooling import execution_ledger_events as tool_mod
from app.mcp_server.tooling.execution_ledger_events import (
    execution_ledger_fill_events_list_recent_impl,
)

EXPECTED_FILL_KEYS: set[str] = {
    "ledger_id",
    "event_key",
    "broker",
    "account_mode",
    "venue",
    "instrument_type",
    "market",
    "symbol",
    "raw_symbol",
    "side",
    "filled_qty",
    "filled_price",
    "filled_notional",
    "currency",
    "broker_order_id",
    "fill_seq",
    "correlation_id",
    "source",
    "filled_at",
    "trade_day_kst",
    "created_at",
}


def _mk_fill_row(
    *,
    ledger_id: int = 123,
    broker: str = "upbit",
    account_mode: str = "live",
    venue: str = "upbit_krw",
    instrument_type: str = "crypto",
    symbol: str = "BTC",
    raw_symbol: str = "KRW-BTC",
    side: str = "sell",
    filled_qty: str = "0.01",
    filled_price: str = "100000000",
    filled_notional: str = "1000000",
    currency: str = "KRW",
    broker_order_id: str = "broker-uuid-1",
    fill_seq: int = 0,
    correlation_id: str | None = "corr-uuid-1",
    source: str = "websocket",
    filled_at: datetime | None = None,
    created_at: datetime | None = None,
    raw_payload_json: dict[str, Any] | None = None,
) -> types.SimpleNamespace:
    """ExecutionLedger ORM row를 모방한 SimpleNamespace 픽스처."""
    if raw_payload_json is None:
        # 보안 검증용 sentinel: _sanitize이 절대 stdout/JSON에 흘려보내면 안 된다.
        raw_payload_json = {"secret": "DO-NOT-EMIT"}
    return types.SimpleNamespace(
        id=ledger_id,
        broker=broker,
        account_mode=account_mode,
        venue=venue,
        instrument_type=instrument_type,
        symbol=symbol,
        raw_symbol=raw_symbol,
        side=side,
        filled_qty=Decimal(filled_qty),
        filled_price=Decimal(filled_price),
        filled_notional=Decimal(filled_notional),
        currency=currency,
        broker_order_id=broker_order_id,
        fill_seq=fill_seq,
        correlation_id=correlation_id,
        source=source,
        filled_at=filled_at or datetime(2026, 7, 7, 0, 0, 0, tzinfo=UTC),
        created_at=created_at or datetime(2026, 7, 7, 0, 0, 1, tzinfo=UTC),
        raw_payload_json=raw_payload_json,
    )


class _FakeSessionCtx:
    """`async with AsyncSessionLocal() as db:` 호환 최소 스텁."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def __aenter__(self) -> Any:
        return object()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _FakeRepo:
    """ExecutionLedgerRepository.list_recent_fills_for_triage 호출 캡처."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def list_recent_fills_for_triage(self, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        return list(self.rows)


def _install_monkeypatched_session(
    monkeypatch: pytest.MonkeyPatch, rows: list[Any]
) -> _FakeRepo:
    fake_repo = _FakeRepo(rows)

    def fake_repo_factory(_db: Any) -> _FakeRepo:
        return fake_repo

    monkeypatch.setattr(
        "app.mcp_server.tooling.execution_ledger_events.AsyncSessionLocal",
        lambda: _FakeSessionCtx(fake_repo),
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.execution_ledger_events.ExecutionLedgerRepository",
        fake_repo_factory,
    )
    return fake_repo


pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


async def test_tool_returns_sanitized_fills_with_expected_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _mk_fill_row()
    fake_repo = _install_monkeypatched_session(monkeypatch, [row])

    out = await execution_ledger_fill_events_list_recent_impl(
        after_id=None,
        market=None,
        side=None,
        source="websocket",
        broker=None,
        account_mode=None,
        limit=50,
    )

    assert out["success"] is True
    assert out["count"] == 1
    assert len(out["fills"]) == 1
    fill = out["fills"][0]

    # 키 셋 정확 일치 + raw_payload_json 미노출 (보안 제약)
    assert set(fill) == EXPECTED_FILL_KEYS
    assert "raw_payload_json" not in fill

    # core sanitization sanity
    assert fill["ledger_id"] == 123
    assert fill["event_key"] == "execution_ledger:123"
    assert fill["instrument_type"] == "crypto"
    assert fill["market"] == "crypto"
    assert fill["filled_qty"] == "0.01"
    assert fill["correlation_id"] == "corr-uuid-1"
    assert fill["trade_day_kst"] == "20260707"

    # repo가 호출되었는지 + source 기본값 websocket이 그대로 전달되었는지
    assert len(fake_repo.calls) == 1
    call = fake_repo.calls[0]
    assert call["source"] == "websocket"
    assert call["limit"] == 50


async def test_tool_rejects_invalid_source_without_touching_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """source='invalid' → DB 도달 전에 invalid_source 반환 + repo 미호출."""
    repo_marker = _FakeRepo([])
    monkeypatch.setattr(
        "app.mcp_server.tooling.execution_ledger_events.AsyncSessionLocal",
        lambda: _FakeSessionCtx(repo_marker),
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.execution_ledger_events.ExecutionLedgerRepository",
        lambda _db: repo_marker,
    )

    out = await execution_ledger_fill_events_list_recent_impl(
        source="invalid",
    )

    assert out["success"] is False
    assert out["error"] == "invalid_source"
    # DB/Repo 호출 없음 — 잘못된 source는 즉시 차단
    assert repo_marker.calls == []


async def test_tool_returns_generic_error_and_logs_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unexpected DB/repo failures are logged server-side without leaking details."""

    class _RaisingRepo:
        async def list_recent_fills_for_triage(self, **_kwargs: Any) -> list[Any]:
            raise RuntimeError("postgresql://secret@example.internal/db")

    monkeypatch.setattr(
        "app.mcp_server.tooling.execution_ledger_events.AsyncSessionLocal",
        lambda: _FakeSessionCtx(_RaisingRepo()),
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.execution_ledger_events.ExecutionLedgerRepository",
        lambda _db: _RaisingRepo(),
    )

    with caplog.at_level("ERROR"):
        out = await execution_ledger_fill_events_list_recent_impl(source="websocket")

    assert out == {"success": False, "error": "internal_error"}
    assert "secret@example.internal" not in str(out)
    assert "execution_ledger_fill_events_list_recent failed" in caplog.text


async def test_fill_events_tool_accepts_toss_broker_with_reconciler_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-757: Toss REST poller writes broker='toss' + source='reconciler'.

    Passthrough verification: the MCP tool must forward both values to the repo
    unchanged so ROB-755 triage can query ``broker='toss', source='reconciler'``.
    """
    calls: list[dict[str, Any]] = []

    class _Repo:
        def __init__(self, db: object) -> None:
            pass

        async def list_recent_fills_for_triage(self, **kwargs: Any) -> list[Any]:
            calls.append(kwargs)
            return []

    fake_repo = _Repo(None)
    monkeypatch.setattr(
        tool_mod,
        "AsyncSessionLocal",
        lambda: _FakeSessionCtx(fake_repo),
    )
    monkeypatch.setattr(tool_mod, "ExecutionLedgerRepository", lambda _db: fake_repo)

    out = await execution_ledger_fill_events_list_recent_impl(
        source="reconciler",
        broker="toss",
        account_mode="live",
    )

    assert out == {"success": True, "count": 0, "fills": []}
    assert calls[0]["broker"] == "toss"
    assert calls[0]["source"] == "reconciler"
