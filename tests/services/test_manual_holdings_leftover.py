from __future__ import annotations

import inspect
import io
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.models.manual_holdings import BrokerAccount, ManualHolding, MarketType
from app.models.review import TossLiveOrderLedger
from app.services.manual_holdings_leftover import (
    DELETE_EVIDENCE_REASONS,
    FILL_EVIDENCE_REASON,
    LeftoverManualRow,
    cleanup_toss_leftover_manual_rows,
    detect_manual_broker_conflicts,
    is_deletion_candidate,
    leftover_reasons,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


def test_leftover_reasons_require_fill_evidence() -> None:
    assert leftover_reasons(
        ticker="AMZN",
        is_mock=False,
        filled_sell_symbols={"AMZN"},
    ) == (FILL_EVIDENCE_REASON,)
    assert leftover_reasons(
        ticker="AMZN",
        is_mock=True,
        filled_sell_symbols={"AMZN"},
    ) == (FILL_EVIDENCE_REASON, "account_mode_mock_mismatch")
    # Scope membership is not evidence — empty reasons is reachable.
    assert (
        leftover_reasons(ticker="AMZN", is_mock=False, filled_sell_symbols=set()) == ()
    )
    assert leftover_reasons(
        ticker="GOOGL", is_mock=True, filled_sell_symbols=set()
    ) == ("account_mode_mock_mismatch",)


def test_allowlist_and_mock_label_are_not_deletion_evidence() -> None:
    assert is_deletion_candidate(()) is False
    assert is_deletion_candidate(("toss_us_allowlist",)) is False
    assert is_deletion_candidate(("account_mode_mock_mismatch",)) is False
    assert is_deletion_candidate((FILL_EVIDENCE_REASON,)) is True
    assert DELETE_EVIDENCE_REASONS == frozenset({FILL_EVIDENCE_REASON})


def test_mutant_scope_label_is_not_a_leftover_reason() -> None:
    source = inspect.getsource(leftover_reasons)
    assert "toss_us_allowlist" not in source
    assert "filled_sell_symbols" in source
    assert "FILL_EVIDENCE_REASON" in source
    gate = inspect.getsource(is_deletion_candidate)
    assert "DELETE_EVIDENCE_REASONS" in gate


def test_conflict_only_when_broker_fill_exists() -> None:
    rows = (
        LeftoverManualRow(
            holding_id=1,
            ticker="AMZN",
            quantity="1",
            broker_account_id=9,
            broker_type="toss",
            is_mock=False,
            reasons=(FILL_EVIDENCE_REASON,),
        ),
        LeftoverManualRow(
            holding_id=2,
            ticker="GOOGL",
            quantity="1",
            broker_account_id=9,
            broker_type="toss",
            is_mock=False,
            reasons=(),
        ),
    )
    conflicts = detect_manual_broker_conflicts(rows)
    assert [row.ticker for row in conflicts] == ["AMZN"]
    assert conflicts[0].reason == "manual_row_conflicts_with_broker_fill"


def test_commit_prints_targets_before_write() -> None:
    from scripts.cleanup_toss_manual_holdings import print_delete_targets

    rows = (
        LeftoverManualRow(
            holding_id=11,
            ticker="AMZN",
            quantity="2",
            broker_account_id=3,
            broker_type="toss",
            is_mock=False,
            reasons=(FILL_EVIDENCE_REASON,),
        ),
    )
    buffer = io.StringIO()
    import sys

    old = sys.stdout
    sys.stdout = buffer
    try:
        print_delete_targets(rows)
    finally:
        sys.stdout = old
    text = buffer.getvalue()
    assert text.startswith("=== DELETE TARGETS ===\n")
    assert "delete_target_count=1" in text
    payload = json.loads(
        text.split("=== DELETE TARGETS ===\n", 1)[1].split("\ndelete_target_count=")[0]
    )
    assert payload["delete_target_count"] == 1
    assert payload["rows"][0]["ticker"] == "AMZN"


@pytest.mark.asyncio
async def test_commit_without_confirm_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = (
        LeftoverManualRow(
            holding_id=11,
            ticker="AMZN",
            quantity="1",
            broker_account_id=3,
            broker_type="toss",
            is_mock=False,
            reasons=(FILL_EVIDENCE_REASON,),
        ),
    )

    async def _listed(_session: object) -> tuple[tuple, tuple]:
        return targets, ()

    monkeypatch.setattr(
        "app.services.manual_holdings_leftover.list_toss_leftover_manual_rows",
        _listed,
    )
    seen: list[tuple[str, ...]] = []

    def _reporter(rows: tuple[LeftoverManualRow, ...]) -> None:
        seen.append(tuple(row.ticker for row in rows))

    with pytest.raises(ValueError, match="confirm"):
        await cleanup_toss_leftover_manual_rows(
            object(),  # type: ignore[arg-type]
            commit=True,
            confirm=False,
            reporter=_reporter,
        )
    assert seen == [("AMZN",)]


async def _purge_leftover_scope(db_session) -> None:
    await db_session.execute(
        delete(TossLiveOrderLedger).where(
            TossLiveOrderLedger.symbol.in_(["AMZN", "GOOGL"])
        )
    )
    await db_session.execute(
        delete(ManualHolding).where(ManualHolding.ticker.in_(["AMZN", "GOOGL"]))
    )
    await db_session.commit()


async def _seed_toss_us_holding(
    db_session, user, *, ticker: str, is_mock: bool = False
) -> ManualHolding:
    account = BrokerAccount(
        user_id=user.id,
        broker_type="toss",
        account_name=f"toss-{ticker}-{uuid4().hex[:8]}",
        is_mock=is_mock,
        is_active=True,
    )
    db_session.add(account)
    await db_session.flush()
    holding = ManualHolding(
        broker_account_id=account.id,
        ticker=ticker,
        market_type=MarketType.US,
        quantity=Decimal("1"),
        avg_price=Decimal("100"),
    )
    db_session.add(holding)
    await db_session.flush()
    return holding


async def _seed_filled_sell(
    db_session, *, symbol: str, cid: str
) -> TossLiveOrderLedger:
    row = TossLiveOrderLedger(
        trade_date=datetime(2026, 8, 19, 18, 0, tzinfo=UTC),
        broker="toss",
        account_mode="toss_live",
        operation_kind="place",
        market="us",
        symbol=symbol,
        side="sell",
        order_type="market",
        client_order_id=f"{cid}-{uuid4().hex[:8]}",
        broker_order_id=f"ord-{cid}-{uuid4().hex[:8]}",
        status="filled",
        filled_qty=Decimal("1"),
    )
    db_session.add(row)
    await db_session.flush()
    return row


@pytest.mark.asyncio
async def test_delete_only_rows_with_filled_sell_evidence(db_session, user) -> None:
    await _purge_leftover_scope(db_session)
    amzn = await _seed_toss_us_holding(db_session, user, ticker="AMZN")
    googl = await _seed_toss_us_holding(db_session, user, ticker="GOOGL")
    await _seed_filled_sell(db_session, symbol="AMZN", cid="amzn-fill-1")
    await db_session.commit()

    dry = await cleanup_toss_leftover_manual_rows(
        db_session, commit=False, confirm=False
    )
    assert [row.ticker for row in dry.rows] == ["AMZN"]
    assert [row.ticker for row in dry.skipped_rows] == ["GOOGL"]
    assert dry.matched == 1
    assert dry.skipped_without_evidence == 1
    assert dry.deleted == 0

    committed = await cleanup_toss_leftover_manual_rows(
        db_session, commit=True, confirm=True
    )
    assert committed.deleted == 1
    remaining = list(
        (
            await db_session.scalars(
                select(ManualHolding).where(ManualHolding.id.in_([amzn.id, googl.id]))
            )
        ).all()
    )
    assert [row.ticker for row in remaining] == ["GOOGL"]


@pytest.mark.asyncio
async def test_mock_label_without_fill_is_not_deleted(db_session, user) -> None:
    await _purge_leftover_scope(db_session)
    holding = await _seed_toss_us_holding(db_session, user, ticker="AMZN", is_mock=True)
    await db_session.commit()
    result = await cleanup_toss_leftover_manual_rows(
        db_session, commit=True, confirm=True
    )
    assert result.deleted == 0
    assert result.skipped_without_evidence == 1
    still = await db_session.get(ManualHolding, holding.id)
    assert still is not None


def test_list_and_cleanup_keep_empty_reason_guard_in_source() -> None:
    source = (REPO / "app" / "services" / "manual_holdings_leftover.py").read_text()
    assert "if not reasons or not is_deletion_candidate(reasons):" in source
    assert "ticker in TOSS_LEFTOVER_TICKERS" not in inspect.getsource(leftover_reasons)
