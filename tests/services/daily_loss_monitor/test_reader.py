"""hk 7119 — the ledger reader's fail-closed shapes (fake session, no DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.daily_loss_monitor.accounts import ACCOUNTS
from app.services.daily_loss_monitor.reader import load_account_fills

NOW = datetime(2026, 11, 27, 3, 0, tzinfo=UTC)  # 12:00 KST
DAY = datetime(2026, 11, 26, 15, 0, tzinfo=UTC)  # 00:00 KST
FRESH_RUN = NOW - timedelta(minutes=5)


def row(**kw):
    base = dict(  # noqa: C408
        broker="kis",
        account_mode="live",
        instrument_type="equity_kr",
        symbol="005930",
        side="buy",
        broker_order_id="0001",
        fill_seq=0,
        filled_qty=Decimal("10"),
        filled_price=Decimal("100"),
        fee_amount=Decimal("0"),
        fee_currency="KRW",
        fee_basis="broker_reported",
        tax_amount=None,
        filled_at_basis="broker_fill_time",
        filled_at=DAY - timedelta(days=2),
        currency="KRW",
        source="reconciler",
        raw_payload_json=None,
        updated_at=None,
    )
    base.update(kw)
    if base["updated_at"] is None:
        base["updated_at"] = base["filled_at"] + timedelta(minutes=30)
    return SimpleNamespace(**base)


class _Session:
    def __init__(self, rows, finished=FRESH_RUN):
        self.rows, self.finished = rows, finished

    async def execute(self, statement):
        rows, finished = self.rows, self.finished

        class _R:
            def scalars(self):
                return SimpleNamespace(all=lambda: rows)

            def scalar_one_or_none(self):
                return finished

        return _R()


async def load(rows, account="kis_live_kr", finished=FRESH_RUN):
    return await load_account_fills(
        _Session(rows, finished), ACCOUNTS[account], day_start=DAY, now=NOW
    )


@pytest.mark.asyncio
async def test_stale_or_missing_reconcile_is_unproven() -> None:
    assert "reconcile_unproven" in (await load([], finished=None)).reasons
    stale = await load([], finished=NOW - timedelta(hours=2))
    assert "reconcile_unproven" in stale.reasons
    assert (await load([])).reasons == ()


@pytest.mark.asyncio
async def test_latest_seed_supersedes_earlier_rows() -> None:
    cut = DAY - timedelta(days=1)
    fills = await load(
        [
            row(broker_order_id="0001", filled_at=cut - timedelta(days=3)),
            row(
                source="manual_import",
                broker_order_id="SEED-20261125-x",
                filled_qty=Decimal("10"),
                filled_at=cut,
            ),
        ]
    )
    assert fills.net_quantity == {"005930": Decimal("10")}
    assert [f.quantity for f in fills.history] == [Decimal("10")]


@pytest.mark.asyncio
async def test_seed_within_the_day_is_unmeasured() -> None:
    fills = await load(
        [
            row(
                source="manual_import",
                broker_order_id="SEED-x",
                filled_at=DAY + timedelta(hours=1),
            ),
        ]
    )
    assert "seed_within_day" in fills.reasons


@pytest.mark.asyncio
async def test_partial_reconcile_does_not_cover_a_later_websocket_fill() -> None:
    today = DAY + timedelta(hours=1)
    fills = await load(
        [
            row(broker_order_id="0007", filled_qty=Decimal("5"), filled_at=today),
            row(
                source="websocket",
                broker_order_id="7",
                filled_qty=Decimal("5"),
                fill_seq=1,
                filled_at=today,
            ),
            row(
                source="websocket",
                broker_order_id="7",
                filled_qty=Decimal("3"),
                fill_seq=2,
                filled_at=today + timedelta(minutes=5),
            ),
        ]
    )
    assert fills.provisional_unreconciled


@pytest.mark.asyncio
async def test_full_reconcile_covers_the_websocket_rows() -> None:
    today = DAY + timedelta(hours=1)
    fills = await load(
        [
            row(broker_order_id="0007", filled_qty=Decimal("8"), filled_at=today),
            row(
                source="websocket",
                broker_order_id="7",
                filled_qty=Decimal("8"),
                fill_seq=1,
                filled_at=today,
            ),
        ]
    )
    assert not fills.provisional_unreconciled


@pytest.mark.asyncio
async def test_old_order_number_does_not_cover_todays_fill() -> None:
    fills = await load(
        [
            row(broker_order_id="0007", filled_at=DAY - timedelta(days=3)),
            row(
                source="websocket",
                broker_order_id="7",
                fill_seq=1,
                filled_at=DAY + timedelta(hours=1),
            ),
        ]
    )
    assert fills.provisional_unreconciled


@pytest.mark.asyncio
async def test_unreconciled_websocket_fill_before_midnight_still_counts() -> None:
    fills = await load(
        [
            row(
                source="websocket",
                broker_order_id="9",
                fill_seq=1,
                filled_at=DAY - timedelta(hours=3),
            ),
        ]
    )
    assert fills.provisional_unreconciled


@pytest.mark.asyncio
async def test_two_cumulative_rows_of_one_order_are_unproven() -> None:
    fills = await load(
        [
            row(broker_order_id="0001", fill_seq=11),
            row(broker_order_id="0001", fill_seq=12, filled_qty=Decimal("12")),
        ]
    )
    assert "order_evidence_unproven" in fills.reasons


@pytest.mark.asyncio
async def test_toss_cumulative_average_chain_is_rebuilt() -> None:
    def toss(prev, cum, avg, seq):
        return row(
            broker="toss",
            broker_order_id="T1",
            fill_seq=seq,
            filled_qty=Decimal(cum - prev),
            filled_price=Decimal(avg),
            raw_payload_json={
                "broker_cumulative_filled_qty": str(cum),
                "previous_filled_qty": str(prev),
            },
            filled_at=DAY + timedelta(hours=1, minutes=seq),
        )

    fills = await load(
        [toss(0, 100, 10000, 1), toss(100, 200, 15000, 2)], account="toss_live_kr"
    )
    [fill] = fills.today
    assert fill.quantity == 200 and fill.price == 15000  # notional 3,000,000
    broken = await load(
        [toss(0, 100, 10000, 1), toss(150, 200, 15000, 2)], account="toss_live_kr"
    )
    assert "order_evidence_unproven" in broken.reasons


@pytest.mark.asyncio
async def test_order_time_near_midnight_is_unproven_for_us() -> None:
    fills = await load(
        [
            row(
                instrument_type="equity_us",
                currency="USD",
                symbol="AAPL",
                filled_at_basis="broker_order_time",
                filled_at=DAY - timedelta(minutes=5),
            )
        ],
        account="kis_live_us",
    )
    assert "fill_day_unproven" in fills.reasons
    kr = await load(
        [row(filled_at_basis="broker_order_time", filled_at=DAY - timedelta(minutes=5))]
    )
    assert "fill_day_unproven" not in kr.reasons


@pytest.mark.asyncio
async def test_symbols_are_normalized() -> None:
    us = await load(
        [
            row(
                instrument_type="equity_us",
                currency="USD",
                symbol="BRK/B",
                fee_currency="USD",
            )
        ],
        account="kis_live_us",
    )
    assert set(us.net_quantity) == {"BRK.B"}
    crypto = await load(
        [
            row(
                broker="upbit",
                instrument_type="crypto",
                symbol="BTC",
                broker_order_id="u1",
            )
        ],
        account="upbit_live",
    )
    assert set(crypto.net_quantity) == {"KRW-BTC"}


@pytest.mark.asyncio
async def test_multi_row_toss_order_without_bookkeeping_is_unproven() -> None:
    rows = [
        row(
            broker="toss",
            broker_order_id="T2",
            fill_seq=1,
            filled_at=DAY + timedelta(hours=1),
        ),
        row(
            broker="toss",
            broker_order_id="T2",
            fill_seq=2,
            filled_qty=Decimal("10"),
            filled_price=Decimal("750"),
            filled_at=DAY + timedelta(hours=2),
        ),
    ]
    fills = await load(rows, account="toss_live_kr")
    assert "order_evidence_unproven" in fills.reasons
    single = await load(rows[:1], account="toss_live_kr")
    assert "order_evidence_unproven" not in single.reasons


@pytest.mark.asyncio
async def test_booking_written_before_a_later_websocket_fill_does_not_cover_it() -> (
    None
):
    today = DAY + timedelta(hours=1)
    fills = await load(
        [
            row(
                broker_order_id="0007",
                filled_qty=Decimal("5"),
                filled_at=today,
                updated_at=today + timedelta(minutes=1),
            ),
            row(
                source="websocket",
                broker_order_id="7",
                filled_qty=Decimal("3"),
                fill_seq=2,
                filled_at=today + timedelta(minutes=10),
            ),
        ]
    )
    assert fills.provisional_unreconciled


@pytest.mark.asyncio
async def test_upbit_trades_keep_their_own_time_and_price() -> None:
    def trade(seq, price, at):
        return row(
            broker="upbit",
            instrument_type="crypto",
            symbol="BTC",
            broker_order_id="u-1",
            fill_seq=seq,
            filled_qty=Decimal("1"),
            filled_price=Decimal(price),
            filled_at=at,
        )

    fills = await load(
        [
            trade(1, "1000000", DAY - timedelta(minutes=30)),
            trade(2, "3000000", DAY + timedelta(minutes=30)),
        ],
        account="upbit_live",
    )
    assert [(f.quantity, f.price) for f in fills.history] == [(1, 1000000)]
    assert [(f.quantity, f.price) for f in fills.today] == [(1, 3000000)]


@pytest.mark.asyncio
async def test_upbit_order_time_row_is_unproven_however_old() -> None:
    fills = await load(
        [
            row(
                broker="upbit",
                instrument_type="crypto",
                symbol="BTC",
                broker_order_id="u-2",
                fill_seq=0,
                filled_at_basis="broker_order_time",
                filled_at=DAY - timedelta(days=2),
            )
        ],
        account="upbit_live",
    )
    assert "fill_day_unproven" in fills.reasons


@pytest.mark.asyncio
async def test_recorded_fee_without_currency_in_a_group_is_unknown() -> None:
    fills = await load(
        [
            row(
                broker="upbit",
                instrument_type="crypto",
                symbol="BTC",
                broker_order_id="u-3",
                fill_seq=1,
                fee_currency=None,
            ),
            row(
                broker="upbit",
                instrument_type="crypto",
                symbol="BTC",
                broker_order_id="u-3",
                fill_seq=2,
                fee_basis="not_provided",
            ),
        ],
        account="upbit_live",
    )
    assert "fee_currency_unknown" in fills.reasons


class _WindowSession(_Session):
    """Records the reconcile query to prove the window predicates exist."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return await super().execute(statement)


@pytest.mark.asyncio
async def test_reconcile_check_requires_a_window_covering_the_day() -> None:
    session = _WindowSession([])
    await load_account_fills(session, ACCOUNTS["kis_live_kr"], day_start=DAY, now=NOW)
    sql = str(session.statements[-1])
    assert "window_start <=" in sql and "window_end >=" in sql
