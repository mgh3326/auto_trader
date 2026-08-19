from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.market_close_digest.flags import improvement_flags
from app.services.market_close_digest.formatter import format_digest_message
from app.services.market_close_digest.oversell import (
    collect_oversell_blocks,
    is_oversell_block,
)
from app.services.market_close_digest.types import (
    DigestSnapshot,
    LedgerFill,
    OversellBlock,
    ProposalRow,
)
from app.telegram_contract import telegram_text_length
from tests.services.market_close_digest.fakes import (
    AC1_SESSION_DATE,
    ac1_fills,
    ac1_oversell_proposals,
)

pytestmark = pytest.mark.unit


def test_holiday_message_is_empty() -> None:
    snapshot = DigestSnapshot(
        market="us",
        session_date=date(2025, 7, 4),
        status="skipped_holiday",
    )
    assert format_digest_message(snapshot) == ""


def test_zero_fills_is_exactly_one_line() -> None:
    snapshot = DigestSnapshot(
        market="us",
        session_date=AC1_SESSION_DATE,
        status="zero_fills",
    )
    message = format_digest_message(snapshot)
    assert message == "US 마감 2026-08-19 · 체결 0건"
    assert "\n" not in message


def test_ac1_card_is_one_compact_message() -> None:
    fills = ac1_fills()
    oversell = collect_oversell_blocks(ac1_oversell_proposals())
    snapshot = DigestSnapshot(
        market="us",
        session_date=AC1_SESSION_DATE,
        status="ok",
        fills=fills,
        oversell_blocked=oversell,
        auto_approve_count=1,
        card_count=2,
        flags=("오버셀 차단 2건 — 매도수량>주문가능 (AMZN,GOOGL)",),
    )
    message = format_digest_message(snapshot)
    assert message.count("\n") < 12
    assert telegram_text_length(message) < 4096
    assert "매도 4" in message
    assert "매수 1" in message
    assert "차단 오버셀 2 (AMZN,GOOGL)" in message
    assert "장문" not in message


def test_improvement_flags_are_count_derived() -> None:
    snapshot = DigestSnapshot(
        market="us",
        session_date=AC1_SESSION_DATE,
        status="ok",
        oversell_blocked=(
            OversellBlock(symbol="AMZN", reason="exceeds orderable"),
            OversellBlock(symbol="GOOGL", reason="exceeds sellable"),
        ),
        auto_approve_count=0,
        card_count=2,
    )
    flags = improvement_flags(snapshot)
    assert flags == (
        "오버셀 차단 2건 — 매도수량>주문가능 (AMZN,GOOGL)",
        "자동승인 0 — 전건 카드 2",
    )


def test_buy_insufficient_balance_is_not_oversell() -> None:
    assert is_oversell_block(side="buy", reason="insufficient balance") is False
    assert (
        is_oversell_block(
            side="sell",
            reason="Requested sell quantity 2 exceeds orderable balance 1.",
        )
        is True
    )


def test_duplicate_oversell_reason_collapses() -> None:
    rows = ac1_oversell_proposals() + (
        ProposalRow(
            symbol="AMZN",
            side="sell",
            market="equity_us",
            auto_approved=False,
            card_kind=None,
            lifecycle_state="rejected",
            void_reason="Requested sell quantity 2 exceeds orderable balance 1.",
            created_at=ac1_oversell_proposals()[0].created_at,
        ),
    )
    blocks = collect_oversell_blocks(rows)
    assert [block.symbol for block in blocks] == ["AMZN", "GOOGL"]


def test_fill_without_pnl_does_not_invent_numbers() -> None:
    fill = LedgerFill(
        source="toss_live_order_ledger",
        broker="toss",
        symbol="AMD",
        side="buy",
        qty=Decimal("2"),
        price=Decimal("160"),
        notional=Decimal("320"),
        pnl=None,
        pnl_pct=None,
        pnl_currency="USD",
        filled_at=ac1_fills()[-1].filled_at,
    )
    snapshot = DigestSnapshot(
        market="us",
        session_date=AC1_SESSION_DATE,
        status="ok",
        fills=(fill,),
    )
    message = format_digest_message(snapshot)
    buy_line = next(line for line in message.splitlines() if line.startswith("매수 "))
    assert buy_line == "매수 AMD"
