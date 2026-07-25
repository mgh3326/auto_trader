"""ROB-755: list_recent_fill_events CLI 단위 테스트 (live DB 없이 monkeypatch)."""

from __future__ import annotations

import asyncio
import json
import types
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from scripts import list_recent_fill_events as cli

pytestmark = pytest.mark.unit

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
    """ExecutionLedger ORM row를 모방한 SimpleNamespace 픽스처.

    기본 raw_payload_json은 보안 검증용 sentinel dict (호출자가 명시적으로 ``None``을
    넘기지 않은 한 항상 secret 값을 포함한다 → 출력 누출 검증).
    """
    if raw_payload_json is None:
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
        return object()  # db 객체는 repo가 캡처하므로 무시됨

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _FakeRepo:
    """ExecutionLedgerRepository의 list_recent_fills_for_triage 호출 캡처."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def list_recent_fills_for_triage(self, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        return list(self.rows)


def _install_monkeypatched_session(
    monkeypatch: pytest.MonkeyPatch, rows: list[Any]
) -> _FakeRepo:
    """AsyncSessionLocal을 픽스처로 교체하고 (db, repo) 양쪽을 캡처."""
    fake_repo = _FakeRepo(rows)

    # collect() 내부에서 `ExecutionLedgerRepository(db)`로 인스턴스화하므로
    # AsyncSessionLocal context-manager를 통해 db를 노출하고, repo 생성을
    # monkeypatch로 가로채서 fake_repo가 주입되도록 한다.
    def fake_repo_factory(_db: Any) -> _FakeRepo:
        return fake_repo

    monkeypatch.setattr(
        cli,
        "AsyncSessionLocal",
        lambda: _FakeSessionCtx(fake_repo),
    )
    monkeypatch.setattr(
        cli,
        "ExecutionLedgerRepository",
        fake_repo_factory,
    )
    return fake_repo


def test_collect_accepts_toss_broker_and_reconciler_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-757: Toss REST poller writes broker='toss' + source='reconciler'.

    Passthrough verification: the CLI must forward both values to the repo
    unchanged so ROB-755 triage can query ``--source reconciler --broker toss``.
    """
    captured: dict[str, object] = {}

    class _Repo:
        def __init__(self, db: object) -> None:
            pass

        async def list_recent_fills_for_triage(self, **kwargs: object) -> list[object]:
            captured.update(kwargs)
            return []

    fake_repo = _Repo(None)
    monkeypatch.setattr(
        cli,
        "AsyncSessionLocal",
        lambda: _FakeSessionCtx(fake_repo),
    )
    monkeypatch.setattr(cli, "ExecutionLedgerRepository", lambda _db: fake_repo)

    out = asyncio.run(
        cli.collect(
            after_id=None,
            market="kr",
            side=None,
            source="reconciler",
            broker="toss",
            account_mode="live",
            limit=50,
        )
    )

    assert out == {"success": True, "count": 0, "fills": []}
    assert captured["source"] == "reconciler"
    assert captured["broker"] == "toss"


@pytest.mark.asyncio
async def test_collect_emits_exact_shape_for_crypto_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _mk_fill_row()
    fake_repo = _install_monkeypatched_session(monkeypatch, [row])

    out = await cli.collect(
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

    # 키 셋이 정확히 일치 (raw_payload_json 미포함이 핵심 보안 제약)
    assert set(fill) == EXPECTED_FILL_KEYS
    assert "raw_payload_json" not in fill

    # core sanitization
    assert fill["ledger_id"] == 123
    assert fill["event_key"] == "execution_ledger:123"
    assert fill["instrument_type"] == "crypto"
    assert fill["market"] == "crypto"  # derived, not from input --market
    assert fill["filled_qty"] == "0.01"
    assert fill["filled_price"] == "100000000"
    assert fill["filled_notional"] == "1000000"
    assert fill["correlation_id"] == "corr-uuid-1"
    assert fill["filled_at"] == "2026-07-07T00:00:00+00:00"
    assert fill["trade_day_kst"] == "20260707"
    assert fill["created_at"] == "2026-07-07T00:00:01+00:00"

    # JSON 직렬화 가능 + secret 값 누출 없음
    blob = json.dumps(out)
    assert "DO-NOT-EMIT" not in blob
    assert "raw_payload_json" not in blob

    # repo가 호출되었는지 확인 + source 기본값 전달
    assert len(fake_repo.calls) == 1
    call = fake_repo.calls[0]
    assert call["source"] == "websocket"
    assert call["limit"] == 50


@pytest.mark.asyncio
async def test_collect_derives_market_for_equity_kr_and_equity_us(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kr_row = _mk_fill_row(
        ledger_id=1,
        instrument_type="equity_kr",
        symbol="005930",
        raw_symbol="005930",
        currency="KRW",
        broker="kis",
        venue="KRX",
        filled_price="70000",
        filled_notional="7000000",
        correlation_id=None,
    )
    us_row = _mk_fill_row(
        ledger_id=2,
        instrument_type="equity_us",
        symbol="AAPL",
        raw_symbol="AAPL",
        currency="USD",
        broker="kis",
        venue="NASDAQ",
        filled_qty="5",
        filled_price="200",
        filled_notional="1000",
        correlation_id=None,
    )
    _install_monkeypatched_session(monkeypatch, [kr_row, us_row])

    out = await cli.collect(
        after_id=None,
        market=None,
        side=None,
        source="websocket",
        broker=None,
        account_mode=None,
        limit=50,
    )

    by_id = {f["ledger_id"]: f for f in out["fills"]}
    assert by_id[1]["market"] == "kr"
    assert by_id[1]["correlation_id"] is None
    assert by_id[2]["market"] == "us"


@pytest.mark.asyncio
async def test_collect_passes_source_none_when_input_is_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--source all → repo에는 source=None (모든 소스 조회) 전달."""
    row = _mk_fill_row(ledger_id=5)
    fake_repo = _install_monkeypatched_session(monkeypatch, [row])

    out = await cli.collect(
        after_id=None,
        market=None,
        side=None,
        source="all",  # CLI 의미: 모든 source
        broker=None,
        account_mode=None,
        limit=10,
    )

    assert out["success"] is True
    assert out["count"] == 1
    assert fake_repo.calls[0]["source"] is None  # "all" → None 매핑 확인
    assert fake_repo.calls[0]["limit"] == 10


def test_main_invalid_source_emits_error_json_and_returns_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.main(["--source", "invalid", "--limit", "5"])
    assert rc == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["success"] is False
    assert "error" in payload
    # 잘못된 source는 DB에 닿기 전에 거부되어야 한다 → repo 호출 없음.
    # (monkeypatch 미적용이지만 asyncio.run이 실행되지 않는 것만 확인해도 OK)


def test_main_invalid_market_emits_error_json_and_returns_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.main(["--market", "crypto1", "--limit", "5"])
    assert rc == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["success"] is False
    assert "invalid --market" in payload["error"]


def test_main_happy_path_returns_zero_and_valid_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    row = _mk_fill_row(ledger_id=99)
    fake_repo = _install_monkeypatched_session(monkeypatch, [row])

    rc = cli.main(["--source", "websocket", "--limit", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["success"] is True
    assert payload["count"] == 1
    assert payload["fills"][0]["ledger_id"] == 99
    # websocket 명시 → repo에는 "websocket"
    assert fake_repo.calls[0]["source"] == "websocket"
    assert fake_repo.calls[0]["limit"] == 5


def test_main_uses_fill_triage_env_defaults_for_toss_reconciler(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    row = _mk_fill_row(ledger_id=100, broker="toss", source="reconciler")
    fake_repo = _install_monkeypatched_session(monkeypatch, [row])
    monkeypatch.setenv("FILL_TRIAGE_MARKET", "kr")
    monkeypatch.setenv("FILL_TRIAGE_SOURCE", "reconciler")
    monkeypatch.setenv("FILL_TRIAGE_BROKER", "toss")
    monkeypatch.setenv("FILL_TRIAGE_ACCOUNT_MODE", "live")

    rc = cli.main(["--limit", "5"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    call = fake_repo.calls[0]
    assert call["market"] == "kr"
    assert call["source"] == "reconciler"
    assert call["broker"] == "toss"
    assert call["account_mode"] == "live"


def test_main_source_all_passes_none_to_repo(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    row = _mk_fill_row()
    fake_repo = _install_monkeypatched_session(monkeypatch, [row])

    rc = cli.main(["--source", "all", "--limit", "7"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["success"] is True
    assert fake_repo.calls[0]["source"] is None
    assert fake_repo.calls[0]["limit"] == 7
