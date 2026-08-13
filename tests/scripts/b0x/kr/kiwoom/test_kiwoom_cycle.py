"""Cycle orchestration — the gates in the order they actually fire.

Every test here drives :func:`scripts.b0x.kr.kiwoom_cycle.run_kiwoom_cycle`
with a fake account and a synthetic policy table, and asserts on **what
reached the venue**, not only on the record. A record that says "blocked"
while an order went out is the failure mode these tests exist to catch, so
``account.buy_calls`` is checked alongside every zero-order reason.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.b0x.kr import attribution as kr_attribution
from scripts.b0x.kr import kiwoom as kiwoom_lane
from scripts.b0x.kr import kiwoom_attribution as kiwoom_attr
from scripts.b0x.kr import kiwoom_cycle
from scripts.policy_table.core.schema import compute_policy_table_hash
from tests.scripts.b0x._table_fixtures import make_payload, make_row, write_table
from tests.scripts.b0x.kr.kiwoom.conftest import FakeAccount, position, resting

pytestmark = pytest.mark.unit

IN_SESSION = dt.datetime(2026, 8, 12, 3, 0, tzinfo=dt.UTC)  # 12:00 KST, Wednesday
OUT_OF_SESSION = dt.datetime(2026, 8, 12, 7, 30, tzinfo=dt.UTC)  # 16:30 KST


def _write_table(
    table_dir: Path,
    *,
    generated_at: dt.datetime,
    symbols: tuple[str, ...] = ("005930",),
    halted: list[str] | None = None,
    buy_l1: str | None = "70000",
    sell_r1: str | None = None,
) -> None:
    """Write a real, hash-valid ``policy_table.v1`` for the KR market.

    Built through ``tests.scripts.b0x._table_fixtures`` so the fixture passes
    the same integrity check a generated table does — a hand-stamped hash
    would test a door the lane does not actually use.
    """

    payload = make_payload(
        rows=[
            make_row(
                symbol=symbol,
                previous_close="72000.00",
                buy_l1=buy_l1,
                sell_r1=sell_r1,
            )
            for symbol in symbols
        ],
        generated_at=generated_at,
        market="kr",
    )
    payload["universe"]["halted_suspect"] = list(halted or [])
    payload["stamps"]["policy_table_hash"] = compute_policy_table_hash(
        {key: value for key, value in payload.items() if key != "stamps"}
    )
    write_table(table_dir, payload, market="kr")


@pytest.fixture
def cycle_now(request: pytest.FixtureRequest) -> dt.datetime:
    """A KRX-RTH test clock; parametrized callers exercise another trading day."""

    return getattr(request, "param", IN_SESSION)


@pytest.fixture
def frozen_cycle_clock(
    monkeypatch: pytest.MonkeyPatch, cycle_now: dt.datetime
) -> dt.datetime:
    """Bind the cycle's wall clock to the same instant passed as ``now``."""

    class FrozenDateTime:
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:  # noqa: ANN102
            return (
                cycle_now.astimezone(tz)
                if tz is not None
                else cycle_now.replace(tzinfo=None)
            )

    monkeypatch.setattr(
        kiwoom_cycle,
        "dt",
        SimpleNamespace(datetime=FrozenDateTime, UTC=dt.UTC),
    )
    return cycle_now


@pytest.fixture
def table_dir(tmp_path: Path, cycle_now: dt.datetime) -> Path:
    directory = tmp_path / "policy-tables"
    _write_table(directory, generated_at=cycle_now - dt.timedelta(hours=4))
    return directory


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "artifacts"


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arm only the confirm gates a unit test can honestly satisfy."""

    monkeypatch.setattr(kiwoom_lane, "assert_kiwoom_lane_enabled", lambda: None)
    monkeypatch.setattr(
        kiwoom_lane,
        "account_identity_summary",
        lambda: {"fingerprint": "sha256:test-account", "product_suffix": "28"},
    )


async def _run(
    *,
    account,
    out_dir,
    table_dir,
    confirm=False,
    interim_ordering=False,
    now=IN_SESSION,  # noqa: ANN001
):  # noqa: ANN202
    return await kiwoom_cycle.run_kiwoom_cycle(
        now=now,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=confirm,
        interim_ordering=interim_ordering,
        account=account,
    )


# ---------------------------------------------------------------------------
# Preview path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_plans_without_touching_the_order_surface(
    table_dir, out_dir
) -> None:  # noqa: ANN001
    account = FakeAccount()
    outcome = await _run(account=account, out_dir=out_dir, table_dir=table_dir)

    assert outcome.zero_order_reason is None
    assert outcome.record["planned"], "preview should still plan"
    assert outcome.record["submission_skipped"].startswith("confirm=False")
    assert account.buy_calls == []
    assert account.cancel_calls == []
    assert outcome.artifact_path is not None and outcome.artifact_path.exists()


@pytest.mark.asyncio
async def test_outside_rth_is_a_zero_order_cycle(table_dir, out_dir) -> None:  # noqa: ANN001
    account = FakeAccount()
    outcome = await _run(
        account=account, out_dir=out_dir, table_dir=table_dir, now=OUT_OF_SESSION
    )
    assert outcome.zero_order_reason == kiwoom_cycle.OUTSIDE_RTH_REASON
    assert account.buy_calls == []


@pytest.mark.asyncio
async def test_missing_table_is_a_zero_order_cycle(tmp_path, out_dir) -> None:  # noqa: ANN001
    account = FakeAccount()
    outcome = await _run(account=account, out_dir=out_dir, table_dir=tmp_path / "empty")
    assert outcome.zero_order_reason == "table_missing"
    assert account.buy_calls == []


@pytest.mark.asyncio
async def test_record_carries_the_account_map_and_status_label(
    table_dir, out_dir
) -> None:  # noqa: ANN001
    outcome = await _run(account=FakeAccount(), out_dir=out_dir, table_dir=table_dir)
    record = outcome.record

    assert record["cycle_status"] == "OBSERVATION_DERIVATION_ONLY"
    assert record["account_map"]["commit"] == kiwoom_cycle.KR_ACCOUNT_MAP_COMMIT
    assert record["account_map"]["gate_values"]["account_lanes.kiwoom_mock"] == "KR-B1"
    assert (
        "kiwoom_mock"
        in record["account_map"]["gate_values"]["b0x_adapter_orders_20260808.surfaces"]
    )
    # 🔴 The lane must never claim the kis ledger as its pending source.
    assert "kt00009" in record["own_pending_source"]
    assert "원장 예외 미사용" in record["own_pending_source"]
    assert any("COEXISTING_ACCOUNT_LANE" in label for label in record["labels"]), (
        "the coexistence caveat must be on every artifact"
    )


@pytest.mark.asyncio
async def test_interim_ordering_requires_confirm_and_keeps_preview_safe(
    table_dir, out_dir
) -> None:  # noqa: ANN001
    account = FakeAccount()
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        interim_ordering=True,
    )

    assert outcome.zero_order_reason == "interim_ordering_requires_confirm"
    assert outcome.record["cycle_status"] == kiwoom_cycle.PREVIEW_STATUS
    assert account.buy_calls == []
    assert account.sell_calls == []
    assert account.cancel_calls == []


# ---------------------------------------------------------------------------
# Confirm preflight — each reason, each with zero venue calls.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("cycle_now", (IN_SESSION, IN_SESSION + dt.timedelta(days=1)))
async def test_confirm_blocks_on_same_day_foreign_orders(
    table_dir, out_dir, armed, frozen_cycle_clock
) -> None:  # noqa: ANN001, ARG001
    """🔴 The empirical KR-B1-is-active gate."""

    account = FakeAccount(
        order_detail={
            kiwoom_attr.kst_order_date(frozen_cycle_clock): [
                {"order_id": "0000009999", "symbol": "005380", "filled_quantity": 1}
            ]
        }
    )
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        now=frozen_cycle_clock,
    )
    assert outcome.zero_order_reason == "preflight_not_clean"
    assert (
        "CONTAMINATED_foreign_same_day_orders_kr_b1_active_suspect"
        in outcome.record["preflight"]["reasons"]
    )
    assert outcome.record["preflight"]["kr_b1_inactive_gate"]["passed"] is False
    assert account.buy_calls == []


@pytest.mark.asyncio
async def test_confirm_blocks_when_kr_b1_foreign_trace_read_fails(
    table_dir, out_dir, armed, monkeypatch
) -> None:  # noqa: ANN001, ARG001
    """🔴 Read failure is not a clean KR-B1-inactive observation."""

    async def readable_empty_attribution(**_kwargs):  # noqa: ANN003, ANN202
        return kr_attribution.OwnFillAttribution(lots=())

    monkeypatch.setattr(kiwoom_attr, "read_own_attribution", readable_empty_attribution)

    class ForeignTraceUnreadableAccount(FakeAccount):
        async def read_order_detail(self, *, order_date=None, symbol=None):  # noqa: ANN001, ANN201, ARG002
            raise RuntimeError("kt00007 foreign trace timeout")

    account = ForeignTraceUnreadableAccount()
    outcome = await _run(
        account=account, out_dir=out_dir, table_dir=table_dir, confirm=True
    )

    assert outcome.zero_order_reason == "preflight_not_clean"
    assert "foreign_same_day_trace_unreadable" in outcome.record["preflight"]["reasons"]
    assert outcome.record["preflight"]["kr_b1_inactive_gate"] == {
        "required": True,
        "source": "kt00007 same-day rows not authored by b0xkw journal",
        "foreign_trace_readable": False,
        "foreign_trace_count": None,
        "passed": False,
        "fail_closed": (
            "foreign trace read failure is not clean; any unreadable answer or "
            "non-zero foreign trace produces zero orders"
        ),
        "residual_toctou": "preflight_once_before_submission",
    }
    assert account.buy_calls == []
    assert account.sell_calls == []


@pytest.mark.asyncio
async def test_confirm_blocks_when_the_account_already_has_resting_orders(
    table_dir, out_dir, armed
) -> None:  # noqa: ANN001, ARG001
    account = FakeAccount(resting=[(resting("777", "005930"),)])
    outcome = await _run(
        account=account, out_dir=out_dir, table_dir=table_dir, confirm=True
    )
    assert outcome.zero_order_reason == "preflight_not_clean"
    assert "account_has_resting_orders" in outcome.record["preflight"]["reasons"]
    assert account.buy_calls == []


@pytest.mark.asyncio
async def test_confirm_blocks_when_pending_is_unreadable(
    table_dir, out_dir, armed
) -> None:  # noqa: ANN001, ARG001
    account = FakeAccount(resting_error=RuntimeError("kt00009 down"))
    outcome = await _run(
        account=account, out_dir=out_dir, table_dir=table_dir, confirm=True
    )
    assert outcome.zero_order_reason == "preflight_not_clean"
    assert "broker_pending_unreadable" in outcome.record["preflight"]["reasons"]
    assert account.buy_calls == []


@pytest.mark.asyncio
async def test_confirm_blocks_when_attribution_is_unreadable(
    table_dir, out_dir, armed, tmp_path
) -> None:  # noqa: ANN001, ARG001
    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text("{oops\n", encoding="utf-8")
    account = FakeAccount(positions=(position("005930", 10),))

    outcome = await kiwoom_cycle.run_kiwoom_cycle(
        now=IN_SESSION,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        account=account,
        journal=kiwoom_attr.OwnOrderJournal(path=corrupt),
    )
    assert outcome.zero_order_reason == "preflight_not_clean"
    assert "attribution_unreadable" in outcome.record["preflight"]["reasons"]
    assert account.buy_calls == []


@pytest.mark.asyncio
async def test_confirm_blocks_when_cash_is_not_positive(
    table_dir, out_dir, armed
) -> None:  # noqa: ANN001, ARG001
    account = FakeAccount(cash=Decimal("0"))
    outcome = await _run(
        account=account, out_dir=out_dir, table_dir=table_dir, confirm=True
    )
    assert outcome.zero_order_reason == "preflight_not_clean"
    assert "cash_not_positive" in outcome.record["preflight"]["reasons"]
    assert account.buy_calls == []


@pytest.mark.asyncio
async def test_confirm_without_the_env_gate_is_zero_order(
    table_dir, out_dir, monkeypatch
) -> None:  # noqa: ANN001
    """Default-off is real: the unarmed lane cannot dispatch."""

    monkeypatch.delenv("B0X_KR_KIWOOM_ENABLED", raising=False)
    account = FakeAccount()
    outcome = await _run(
        account=account, out_dir=out_dir, table_dir=table_dir, confirm=True
    )
    assert outcome.zero_order_reason == "confirm_gate_not_armed"
    assert account.buy_calls == []


# ---------------------------------------------------------------------------
# Confirm happy path — one order, always cancelled.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_submits_exactly_one_order_and_cancels_it(
    table_dir, out_dir, armed
) -> None:  # noqa: ANN001, ARG001
    _write_table(
        table_dir,
        generated_at=IN_SESSION - dt.timedelta(hours=4),
        symbols=("005930", "000660"),
    )
    # Derivation consumes rows in lexicographic symbol order, so the bounded
    # lever's single submission is 000660 — asserted explicitly rather than
    # assumed, because "which one of several" is exactly what the limit decides.
    account = FakeAccount(
        resting=[
            (),  # preflight: clean account
            (),  # pre-dispatch re-read
            (resting("0000123456", "000660", price=70_000, remaining=4),),  # resting
            (),  # reconcile: gone
        ]
    )

    outcome = await _run(
        account=account, out_dir=out_dir, table_dir=table_dir, confirm=True
    )

    assert outcome.exit_code == 0
    assert len(account.buy_calls) == 1, "the bounded lever must submit exactly one"
    assert account.buy_calls[0]["symbol"] == "000660"
    assert len(account.cancel_calls) == 1
    trips = outcome.record["round_trip"]
    assert len(trips) == 1
    assert trips[0]["cancel_confirmed"] is True
    assert trips[0]["round_trip_complete"] is True
    assert trips[0]["correlation_id"].startswith("b0xkw-")
    assert outcome.record["submission_stopped"].startswith(
        "acceptance_submission_limit="
    )
    assert outcome.record["round_trip_policy"] == kiwoom_cycle.ROUND_TRIP_MANDATORY_NOTE
    assert outcome.record["cycle_status"] == kiwoom_cycle.ACCEPTANCE_ONLY_STATUS
    assert outcome.record["cycle_status_label"] == (
        kiwoom_cycle.ACCEPTANCE_ONLY_STATUS_LABEL
    )
    assert outcome.record["day_orders"] == []


@pytest.mark.asyncio
async def test_interim_ordering_submits_every_envelope_derived_day_order_without_cancel(
    table_dir, out_dir, armed
) -> None:  # noqa: ANN001, ARG001
    """DAY retention replaces neither the derivation caps nor acceptance mode."""

    _write_table(
        table_dir,
        generated_at=IN_SESSION - dt.timedelta(hours=4),
        symbols=("005930", "000660", "035420", "051910"),
    )

    class DistinctOrderNumbers(FakeAccount):
        async def place_limit_buy(self, *, symbol, quantity, price):  # noqa: ANN001, ANN201
            payload = await super().place_limit_buy(
                symbol=symbol, quantity=quantity, price=price
            )
            payload["ord_no"] = f"{len(self.buy_calls):010d}"
            return payload

    account = DistinctOrderNumbers()
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        interim_ordering=True,
    )

    assert outcome.exit_code == 0
    assert outcome.record["cycle_status"] == "INTERIM_ORDERING"
    assert outcome.record["cycle_status_label"] == (
        kiwoom_cycle.INTERIM_ORDERING_STATUS_LABEL
    )
    assert outcome.record["interim_ordering_constraints"] == (
        kiwoom_cycle.INTERIM_ORDERING_CONSTRAINTS
    )
    assert kiwoom_cycle.INTERIM_ORDERING_STATUS_LABEL in outcome.record["labels"]
    for constraint in kiwoom_cycle.INTERIM_ORDERING_CONSTRAINTS.values():
        assert constraint.split(" — ")[0] in kiwoom_cycle.INTERIM_ORDERING_STATUS_LABEL

    # Four candidates reach derivation, but the locked daily-new cap admits
    # exactly three. INTERIM_ORDERING sends all three, not the old one-order
    # acceptance limit, and none reaches cancellation.
    assert outcome.derivation is not None
    assert len(outcome.derivation.orders) == 3
    assert any(
        skipped["reason"] == "daily_new_entry_cap_reached"
        for skipped in outcome.record["skipped"]
    )
    assert len(account.buy_calls) == len(outcome.derivation.orders)
    assert account.sell_calls == []
    assert account.cancel_calls == []
    assert outcome.record["round_trip"] == []
    assert len(outcome.record["day_orders"]) == 3
    assert outcome.record["submitted"] == outcome.record["day_orders"]
    assert all(
        order["time_in_force"] == "DAY" for order in outcome.record["day_orders"]
    )
    assert all(
        order["automatic_cancel"] is False for order in outcome.record["day_orders"]
    )
    assert all(
        order["fill_status"] == "unverified" for order in outcome.record["day_orders"]
    )
    assert all(
        Decimal(order["notional_krw"])
        <= Decimal(outcome.record["envelope"]["per_order_notional"])
        for order in outcome.record["day_orders"]
    )
    assert "acceptance_submission_limit" not in outcome.record
    assert outcome.record["day_order_policy"] == kiwoom_cycle.DAY_ORDER_RETAINED_NOTE


@pytest.mark.asyncio
async def test_interim_buy_only_sell_gate_blocks_derived_sell_with_audit_reason(
    table_dir, out_dir, armed, monkeypatch
) -> None:  # noqa: ANN001, ARG001
    """§50차 2항: a valid derived sell is visible but cannot reach Kiwoom."""

    _write_table(
        table_dir,
        generated_at=IN_SESSION - dt.timedelta(hours=4),
        buy_l1=None,
        sell_r1="80000.00",
    )
    own_attribution = kr_attribution.OwnFillAttribution(
        lots=(
            kr_attribution.AttributedLot(
                symbol="005930",
                quantity=Decimal("10"),
                average_price=Decimal("70000"),
                buy_fill_rows=1,
                sell_rows=0,
            ),
        )
    )

    async def _read_own_attribution(**_kwargs):  # noqa: ANN003, ANN202
        return own_attribution

    monkeypatch.setattr(kiwoom_attr, "read_own_attribution", _read_own_attribution)
    account = FakeAccount(positions=(position("005930", 10, average_price=70_000),))
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        interim_ordering=True,
    )

    assert outcome.exit_code == 0
    assert outcome.derivation is not None
    assert [order.side for order in outcome.derivation.orders] == ["sell"]
    assert [order["side"] for order in outcome.record["planned"]] == ["sell"]
    # Keep this assertion before the artifact checks: disabling the gate must
    # prove that the sell reached the fake venue, rather than only changing a
    # descriptive field.
    assert account.sell_calls == []
    assert account.buy_calls == []
    assert outcome.record["day_orders"] == []
    assert outcome.record["submitted"] == []
    assert outcome.record["interim_buy_only_sell_gate"] == {
        "enabled": True,
        "reason_code": "interim_buy_only_sell_gate",
        "scope": "submission_stage_sell_legs",
        "cli_disable_available": False,
        "release": "explicit_operator_approval_or_b_track_merge",
    }
    blocked = outcome.record["submission_blocked"]
    assert [(item["side"], item["leg"], item["reason"]) for item in blocked] == [
        ("sell", "sell_r1", "interim_buy_only_sell_gate")
    ]
    assert blocked[0]["detail"] == kiwoom_cycle.INTERIM_BUY_ONLY_SELL_GATE_DETAIL
    assert kiwoom_cycle.INTERIM_BUY_ONLY_SELL_GATE_ENABLED is True
    assert outcome.record["interim_ordering_constraints"]["buy_only"] == (
        "매수 전용(매도 게이트 ON)"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cycle_now", (IN_SESSION, IN_SESSION + dt.timedelta(days=1)))
async def test_confirm_writes_the_order_number_to_the_journal(
    table_dir, out_dir, armed, tmp_path, frozen_cycle_clock
) -> None:  # noqa: ANN001, ARG001
    journal = kiwoom_attr.OwnOrderJournal(path=tmp_path / "journal.jsonl")
    account = FakeAccount(
        resting=[
            (),
            (),
            (resting("0000123456", "005930", price=70_000, remaining=4),),
            (),
        ]
    )
    await kiwoom_cycle.run_kiwoom_cycle(
        now=frozen_cycle_clock,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        account=account,
        journal=journal,
    )
    records = journal.read_all()
    assert [record.order_no for record in records] == ["0000123456"]
    assert records[0].correlation_id.startswith("b0xkw-")
    assert records[0].order_date == kiwoom_attr.kst_order_date(frozen_cycle_clock)


@pytest.mark.asyncio
async def test_unconfirmed_cancel_exits_non_zero(table_dir, out_dir, armed) -> None:  # noqa: ANN001, ARG001
    """🔴 mutant ⑤ at cycle level — a stuck order is not a clean run."""

    account = FakeAccount(
        resting=[
            (),
            (),
            (resting("0000123456", "005930", price=70_000, remaining=4),),
            (resting("0000123456", "005930", price=70_000, remaining=4),),
        ]
    )
    outcome = await _run(
        account=account, out_dir=out_dir, table_dir=table_dir, confirm=True
    )
    assert outcome.exit_code == 2
    assert outcome.record["submission_stopped"] == "round_trip_incomplete"
    assert outcome.record["round_trip"] == []
    assert outcome.record["round_trip_failures"]


@pytest.mark.asyncio
async def test_halted_suspect_symbol_never_reaches_the_venue(
    table_dir, out_dir, armed
) -> None:  # noqa: ANN001, ARG001
    """The table already drops these; this is the second, independent line."""

    _write_table(
        table_dir,
        generated_at=IN_SESSION - dt.timedelta(hours=4),
        halted=["005930"],
    )
    account = FakeAccount()
    outcome = await _run(
        account=account, out_dir=out_dir, table_dir=table_dir, confirm=True
    )
    assert outcome.record["halted_suspect"]["symbols"] == ["005930"]
    assert outcome.record["submission_stopped"] == "halted_suspect_symbol"
    assert account.buy_calls == []


@pytest.mark.asyncio
async def test_stale_table_is_a_zero_order_cycle(table_dir, out_dir, armed) -> None:  # noqa: ANN001, ARG001
    """MAX_TABLE_AGE for KR is 36h and this lane does not get its own value."""

    _write_table(table_dir, generated_at=IN_SESSION - dt.timedelta(hours=48))
    account = FakeAccount()
    outcome = await _run(
        account=account, out_dir=out_dir, table_dir=table_dir, confirm=True
    )
    assert outcome.zero_order_reason == "stale_by_age"
    assert account.buy_calls == []


@pytest.mark.asyncio
async def test_second_process_is_refused_by_the_writer_lock(table_dir, out_dir) -> None:  # noqa: ANN001
    from scripts.b0x.ledger import WriterLockUnavailable, writer_lock

    out_dir.mkdir(parents=True, exist_ok=True)
    with writer_lock(lane=kiwoom_cycle.LANE, root=out_dir):
        with pytest.raises(WriterLockUnavailable):
            await _run(account=FakeAccount(), out_dir=out_dir, table_dir=table_dir)
