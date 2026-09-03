"""A2 — the capture CLI fails closed when the provider sends no authority clock.

Offline: the DB session and the Toss client are replaced with fakes, so this
exercises the real decision path (extract → append or fail closed) with no
database, no network, and no Toss credentials.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from app.services import krb1_metadata_authority
from app.services.krb1_metadata_authority import (
    PROVIDER_AUTHORITY_CLOCK_ABSENT,
    evaluate_metadata_authority,
    load_latest_metadata_snapshot,
)
from scripts import krb1_p0_metadata_snapshot_capture as capture

pytestmark = pytest.mark.unit

KST = dt.timezone(dt.timedelta(hours=9))
AS_OF = dt.date(2026, 7, 29)
DECISION_AT = dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)
BEFORE_DECISION = dt.datetime(2026, 7, 29, 17, 0, tzinfo=KST)

UNIVERSE_ROWS = [
    {
        "symbol": "005930",
        "exchange": "KOSPI",
        "security_type": "STOCK",
        "is_common_share": True,
        "listing_status": "ACTIVE",
        "list_date": dt.date(1975, 6, 11),
        "krx_trading_suspended": False,
    },
    {
        "symbol": "100001",
        "exchange": "KOSDAQ",
        "security_type": "STOCK",
        "is_common_share": True,
        "listing_status": "ACTIVE",
        "list_date": dt.date(2001, 1, 2),
        "krx_trading_suspended": False,
    },
]

CLOCKLESS_PAYLOAD = [{"symbol": "005930", "securityType": "STOCK"}]
CLOCKED_PAYLOAD = {
    "publishedAt": "2026-07-29T16:20:00+09:00",
    "effectiveSession": "2026-07-29",
    "result": CLOCKLESS_PAYLOAD,
}


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.statements: list[str] = []
        self.rolled_back = False

    async def execute(self, statement: object, params: object = None) -> _FakeResult:
        text = str(statement)
        self.statements.append(text)
        if "kr_symbol_universe" in text:
            return _FakeResult(self._rows)
        return _FakeResult([])

    async def rollback(self) -> None:
        self.rolled_back = True

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeToss:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.calls = 0

    async def stocks_raw(self, symbols: list[str]) -> object:
        self.calls += 1
        return self._payload


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    def _wire(payload: object) -> _FakeToss:
        session = _FakeSession(UNIVERSE_ROWS)
        client = _FakeToss(payload)
        monkeypatch.setattr(capture, "AsyncSessionLocal", lambda: session)
        monkeypatch.setattr(
            capture.TossReadClient, "from_settings", classmethod(lambda cls: client)
        )
        return client

    return _wire


def test_capture_fails_closed_when_the_provider_sends_no_clock(
    wired, tmp_path: Path
) -> None:
    """🔴 A2: no provider clock, no snapshot — and no retrieval-clock fallback."""
    wired(CLOCKLESS_PAYLOAD)

    result = asyncio.run(
        capture.run(
            as_of_session=AS_OF,
            decision_at=DECISION_AT,
            store_dir=tmp_path,
            now=BEFORE_DECISION,
            clock=lambda: BEFORE_DECISION,
        )
    )

    assert result["status"] == "fail_closed"
    markets = result["markets"]
    assert isinstance(markets, dict)
    for market in ("KOSPI", "KOSDAQ"):
        outcome = markets[market]
        assert outcome["status"] == "fail_closed"
        assert outcome["reason"] == PROVIDER_AUTHORITY_CLOCK_ABSENT
        assert outcome["appended"] is False
    assert not (tmp_path / capture.SNAPSHOT_FILENAME).exists()


def test_capture_records_the_provider_clock_once_a_contract_is_declared(
    wired, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attributability: with a declared provider contract the capture succeeds."""
    monkeypatch.setattr(
        krb1_metadata_authority,
        "PROVIDER_PUBLISHED_AT_FIELDS",
        frozenset({"publishedAt"}),
    )
    monkeypatch.setattr(
        krb1_metadata_authority,
        "PROVIDER_EFFECTIVE_SESSION_FIELDS",
        frozenset({"effectiveSession"}),
    )
    wired(CLOCKED_PAYLOAD)
    # The declared contract is what makes the write boundary accept this clock;
    # a locally named one is refused by snapshot_row (F4a).

    result = asyncio.run(
        capture.run(
            as_of_session=AS_OF,
            decision_at=DECISION_AT,
            store_dir=tmp_path,
            now=BEFORE_DECISION,
            clock=lambda: BEFORE_DECISION,
        )
    )

    assert result["status"] == "captured", result
    markets = result["markets"]
    assert isinstance(markets, dict)
    assert markets["KOSPI"]["provider_clock_fields"] == {
        "published_at": "publishedAt",
        "effective_session": "effectiveSession",
    }
    snapshot = load_latest_metadata_snapshot(
        tmp_path / capture.SNAPSHOT_FILENAME, market="KOSPI"
    )
    assert snapshot is not None
    assert snapshot.provider_clock is not None
    assert snapshot.provider_clock.published_at_field == "publishedAt"
    gate = evaluate_metadata_authority(
        snapshot=snapshot,
        market="KOSPI",
        rows=tuple(
            capture.SymbolMetadata(
                symbol=str(row["symbol"]),
                exchange=str(row["exchange"]),
                security_type=str(row["security_type"]),
                is_common_share=bool(row["is_common_share"]),
                listing_status=str(row["listing_status"]),
                list_date=row["list_date"],  # type: ignore[arg-type]
                krx_trading_suspended=bool(row["krx_trading_suspended"]),
            )
            for row in UNIVERSE_ROWS
            if row["exchange"] == "KOSPI"
        ),
        as_of_session=AS_OF,
        decision_at=DECISION_AT,
    )
    assert gate.status == "proven", gate.reason


def test_capture_refuses_to_start_after_the_decision_clock(
    wired, tmp_path: Path
) -> None:
    wired(CLOCKED_PAYLOAD)

    result = asyncio.run(
        capture.run(
            as_of_session=AS_OF,
            decision_at=DECISION_AT,
            store_dir=tmp_path,
            now=dt.datetime(2026, 7, 29, 19, 0, tzinfo=KST),
            clock=lambda: dt.datetime(2026, 7, 29, 19, 0, tzinfo=KST),
        )
    )

    assert result["status"] == "fail_closed"
    assert result["reason"] == "metadata_capture_started_after_decision_at"
    assert not (tmp_path / capture.SNAPSHOT_FILENAME).exists()


def test_capture_reads_the_universe_in_a_read_only_transaction(
    wired, tmp_path: Path
) -> None:
    wired(CLOCKLESS_PAYLOAD)

    asyncio.run(
        capture.run(
            as_of_session=AS_OF,
            decision_at=DECISION_AT,
            store_dir=tmp_path,
            now=BEFORE_DECISION,
            clock=lambda: BEFORE_DECISION,
        )
    )

    # The fake session records what the CLI actually executed.
    session = capture.AsyncSessionLocal()
    assert isinstance(session, _FakeSession)
    assert any(
        "REPEATABLE READ READ ONLY" in statement for statement in session.statements
    )
    assert session.rolled_back is True
