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
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.brokers.kiwoom import constants as kiwoom_constants
from app.services.brokers.kiwoom.client import KiwoomMockClient
from app.services.mock_integration.coordination import (
    CoordinationScope,
    MutationCertainty,
)
from scripts.b0x.kr import attribution as kr_attribution
from scripts.b0x.kr import kiwoom as kiwoom_lane
from scripts.b0x.kr import kiwoom_attribution as kiwoom_attr
from scripts.b0x.kr import kiwoom_cycle
from scripts.b0x.kr import kiwoom_ordering as ordering_support
from scripts.policy_table.core.schema import compute_policy_table_hash
from tests.scripts.b0x._table_fixtures import make_payload, make_row, write_table
from tests.scripts.b0x.kr.kiwoom.conftest import FakeAccount, position, resting
from tests.services.mock_integration.test_kiwoom_coordination_adapter import (
    bound_kiwoom_entry,
    build_offline_adapter,
    offline_coordination_factory,
)

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
    buy_l2: str | None = None,
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
                buy_l2=buy_l2,
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


def _attach_test_mock_client(account) -> None:  # noqa: ANN001
    if getattr(account, "_client", None) is None:
        account._client = KiwoomMockClient(
            base_url=kiwoom_constants.MOCK_BASE_URL,
            app_key="unit-test",
            app_secret="unit-test",
            account_no="unit-test",
        )


async def _run(
    *,
    account,
    out_dir,
    table_dir,
    confirm=False,
    ordering=False,
    now=IN_SESSION,  # noqa: ANN001
    **kwargs,
):  # noqa: ANN202
    if confirm and "coordination_factory" not in kwargs:
        offline_entry = bound_kiwoom_entry()
        kwargs["coordination_factory"] = lambda: offline_coordination_factory(
            entry=offline_entry
        )
        kwargs["coordination_entry"] = offline_entry
        _attach_test_mock_client(account)
    elif kwargs.get("coordination_factory") is not None:
        _attach_test_mock_client(account)
    return await kiwoom_cycle.run_kiwoom_cycle(
        now=now,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=confirm,
        ordering=ordering,
        account=account,
        **kwargs,
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
    assert record["contract"]["version"] == "v1.8"
    assert record["contract"]["file_sha256_reference_only"] == (
        "7d2729bc4197dd167d40e3e881b64f30a778b1b1a7158acf81fd0c7d38d008c0"
    )
    v18 = record["contract"]["clauses"]["§8 v1.8 (v1.5 ① KR amendment)"]
    assert "동일 cycle_id·동일 symbol·BUY 다단" in v18
    assert "다음 cycle의 기존 자기 미체결 차단" in v18
    assert "crypto/US와 공용 게이트는 불변" in v18
    assert "partial_failure + exit 2" in v18
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
    assert all(
        "LADDER_MULTI_RUNG_OBSERVATION_LIMIT" not in label for label in record["labels"]
    )
    assert outcome.artifact_path is not None
    artifact = outcome.artifact_path.read_text(encoding="utf-8")
    assert "LADDER_MULTI_RUNG_OBSERVATION_LIMIT" not in artifact


@pytest.mark.asyncio
async def test_ordering_requires_confirm_and_keeps_preview_safe(
    table_dir, out_dir
) -> None:  # noqa: ANN001
    account = FakeAccount()
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        ordering=True,
    )

    assert outcome.zero_order_reason == "ordering_requires_confirm"
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
async def test_acceptance_buy_cancel_reconcile_share_one_authority_scope(
    table_dir, out_dir, armed
) -> None:  # noqa: ANN001, ARG001
    entry = bound_kiwoom_entry()
    adapter = build_offline_adapter(entry=entry)
    account = FakeAccount(
        resting=[
            (),
            (),
            (resting("0000123456", "005930", price=70_000, remaining=4),),
            (),
        ]
    )
    _attach_test_mock_client(account)
    outcome = await kiwoom_cycle.run_kiwoom_cycle(
        now=IN_SESSION,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        account=account,
        coordination_factory=lambda: adapter,
        coordination_entry=entry,
        authority_risk_notifier=lambda _risk: pytest.fail(
            "clean cancellation must not alert"
        ),
    )

    assert outcome.exit_code == 0
    assert adapter.ports.connection_factory.calls == 1  # type: ignore[attr-defined]
    assert len(adapter.ports.authority_evidence.attempts) == 1  # type: ignore[union-attr]
    assert len(adapter.ports.authority_evidence.terminals) == 1  # type: ignore[union-attr]
    assert adapter.fence_rechecks == [
        f"post:{outcome.record['planned'][0]['order_key']}",
        "cancel:0000123456",
    ]
    assert adapter.ordered_events.index("jsonl_appended:0000123456") < (
        adapter.ordered_events.index("acceptance_callback_complete")
    )
    assert adapter.ordered_events.index("jsonl_appended:0000123457") < (
        adapter.ordered_events.index("acceptance_callback_complete")
    )
    assert adapter.ordered_events.index("acceptance_callback_complete") < (
        adapter.ordered_events.index("j2b_composite_evidence_persisted")
    )
    assert outcome.record["authority_cessation"]["status"] == "RELEASE_VERIFIED"
    assert outcome.record["G3_OVERALL"] == "RELEASE_VERIFIED"


class _TestOrderingLease:
    def __init__(self) -> None:
        self.held = False
        self.checks = 0

    def acquire(self) -> None:
        self.held = True

    def assert_held(self) -> None:
        self.checks += 1
        if not self.held:
            raise RuntimeError("lease lost")

    def release(self) -> None:
        self.held = False

    def canonical(self) -> dict[str, object]:
        return {"acquired": self.held, "test_lease": True}


class _DynamicOrderingAccount(FakeAccount):
    """Fake kt00007 whose rows are updated by the fake broker mutations."""

    def __init__(self, *, rows=None, **kwargs) -> None:  # noqa: ANN001
        super().__init__(**kwargs)
        self.rows: list[dict[str, object]] = list(rows or [])
        self._next_order_no = 1

    async def read_order_detail(self, *, order_date=None, symbol=None):  # noqa: ANN001, ANN201, ARG002
        self.detail_calls.append({"order_date": order_date, "symbol": symbol})
        return [dict(row) for row in self.rows]

    async def place_limit_buy(self, *, symbol, quantity, price):  # noqa: ANN001, ANN201
        await super().place_limit_buy(symbol=symbol, quantity=quantity, price=price)
        return self._append_order(
            symbol=symbol, side="buy", quantity=quantity, price=price
        )

    async def place_limit_sell(self, *, symbol, quantity, price):  # noqa: ANN001, ANN201
        await super().place_limit_sell(symbol=symbol, quantity=quantity, price=price)
        return self._append_order(
            symbol=symbol, side="sell", quantity=quantity, price=price
        )

    def _append_order(self, *, symbol, side, quantity, price):  # noqa: ANN001, ANN202
        order_no = f"{self._next_order_no:010d}"
        self._next_order_no += 1
        partial = len(self.rows) == 0
        filled = 1 if partial else 0
        self.rows.append(
            {
                "order_id": order_no,
                "symbol": symbol,
                "status": "partial" if partial else "open",
                "ordered_quantity": quantity,
                "filled_quantity": filled,
                "remaining_quantity": quantity - filled,
                "unfilled_quantity": quantity - filled,
                "ordered_price": price,
                "average_price": price + 100 if filled else 0,
            }
        )
        return {"return_code": 0, "ord_no": order_no}


def _own_pending_record(
    *, now: dt.datetime, order_no: str = "0000000042", quantity: int = 3
) -> kiwoom_attr.OwnOrderRecord:
    return kiwoom_attr.OwnOrderRecord(
        at=now.isoformat(),
        order_no=order_no,
        correlation_id="b0xkw-prior-buy",
        symbol="005930",
        side="buy",
        price=70_000,
        quantity=quantity,
        order_date=kiwoom_attr.kst_order_date(now),
    )


def _own_pending_row(
    *, order_no: str = "0000000042", quantity: int = 3
) -> dict[str, object]:
    return {
        "order_id": order_no,
        "symbol": "005930",
        "status": "open",
        "ordered_quantity": quantity,
        "filled_quantity": 0,
        "remaining_quantity": quantity,
        "unfilled_quantity": quantity,
        "ordered_price": 70_000,
        "average_price": 0,
    }


@pytest.mark.asyncio
async def test_ordering_submits_table_derived_day_orders_with_readback_fidelity(
    table_dir, out_dir, armed, frozen_cycle_clock
) -> None:  # noqa: ANN001, ARG001
    """ORDERING has a distinct lifecycle, not an acceptance status rename."""

    _write_table(
        table_dir,
        generated_at=IN_SESSION - dt.timedelta(hours=4),
        symbols=("005930", "000660", "035420", "051910"),
    )
    lease = _TestOrderingLease()
    account = _DynamicOrderingAccount()
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        lease_factory=lambda *_: lease,
    )

    assert outcome.exit_code == 0
    assert outcome.record["cycle_status"] == kiwoom_cycle.ORDERING_STATUS
    assert outcome.record["cycle_status_label"] == kiwoom_cycle.ORDERING_STATUS_LABEL
    assert outcome.record["ordering_requirements"] == kiwoom_cycle.ORDERING_REQUIREMENTS
    assert outcome.derivation is not None
    assert len(outcome.derivation.orders) == 3
    assert len(account.buy_calls) == len(outcome.derivation.orders)
    assert account.sell_calls == []
    assert account.cancel_calls == []
    assert outcome.record["round_trip"] == []
    assert len(outcome.record["day_orders"]) == 3
    assert all(
        order["time_in_force"] == "DAY" for order in outcome.record["day_orders"]
    )
    assert all(
        order["automatic_cancel"] is False for order in outcome.record["day_orders"]
    )
    assert all("broker_readback" in order for order in outcome.record["day_orders"])
    first_readback = outcome.record["day_orders"][0]["broker_readback"]
    assert first_readback["partial"] is True
    assert first_readback["complete"] is False
    assert first_readback["remaining_quantity"] > 0
    assert first_readback["fill_vwap"] is not None
    assert first_readback["slippage_krw"] == "100"
    events = outcome.record["fidelity_events"]
    assert {event["event"] for event in events} >= {
        "table_price_to_intended_limit",
        "broker_ack",
        "broker_readback_reconcile",
    }
    persisted_events = ordering_support.OrderingEventJournal.for_lane(
        root=out_dir, lane=kiwoom_cycle.LANE
    ).read_all()
    assert tuple(event["at"] for event in persisted_events) == tuple(
        event["at"] for event in events
    )
    assert all(event["at"] and event["event"] for event in persisted_events)
    assert lease.checks >= len(outcome.record["day_orders"]) + 1


@pytest.mark.asyncio
async def test_ordering_batches_same_cycle_buy_ladder_under_one_gate(
    table_dir, out_dir, armed, frozen_cycle_clock, monkeypatch
) -> None:  # noqa: ANN001, ARG001
    _write_table(
        table_dir,
        generated_at=IN_SESSION - dt.timedelta(hours=4),
        buy_l2="68000",
    )
    gate_symbols: list[str] = []
    real_assert = kiwoom_lane.assert_resubmit_allowed

    def counted_assert(truth, *, symbol, lane):  # noqa: ANN001, ANN202
        gate_symbols.append(symbol)
        return real_assert(truth, symbol=symbol, lane=lane)

    monkeypatch.setattr(kiwoom_lane, "assert_resubmit_allowed", counted_assert)
    account = _DynamicOrderingAccount()
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        lease_factory=lambda *_: _TestOrderingLease(),
    )

    assert outcome.exit_code == 0
    assert gate_symbols == ["005930"]
    assert len(account.buy_calls) == 2
    assert {order["cycle_id"] for order in outcome.record["planned"]} == {
        outcome.record["cycle_id"]
    }
    batch = outcome.record["same_cycle_buy_batches"][0]
    assert batch["cycle_id"] == outcome.record["cycle_id"]
    assert batch["status"] == "complete"
    assert batch["gate_checks"] == 1
    assert batch["mutation_boundary_checks"] == 2
    assert batch["accepted_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_error",
    [
        kiwoom_lane.KiwoomBrokerRejected(
            api="kt10000", return_code=1, return_msg="second rung rejected"
        ),
        OSError("second rung transport failed"),
    ],
    ids=("broker-rejected", "transport-error"),
)
async def test_ordering_partial_batch_failure_is_not_success_and_retains_ack(
    table_dir, out_dir, armed, frozen_cycle_clock, second_error
) -> None:  # noqa: ANN001, ARG001
    _write_table(
        table_dir,
        generated_at=IN_SESSION - dt.timedelta(hours=4),
        buy_l2="68000",
    )

    class RejectsSecondRung(_DynamicOrderingAccount):
        async def place_limit_buy(self, *, symbol, quantity, price):  # noqa: ANN001, ANN201
            if self.buy_calls:
                await FakeAccount.place_limit_buy(
                    self, symbol=symbol, quantity=quantity, price=price
                )
                raise second_error
            return await super().place_limit_buy(
                symbol=symbol, quantity=quantity, price=price
            )

    account = RejectsSecondRung()
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        lease_factory=lambda *_: _TestOrderingLease(),
    )

    assert outcome.exit_code == 2
    assert outcome.record["batch_submission_status"] == "partial_failure"
    assert outcome.record["submission_stopped"] == "day_order_submission_unverified"
    batch = outcome.record["same_cycle_buy_batches"][0]
    assert batch["status"] == "partial_failure"
    assert batch["accepted_count"] == 1
    assert len(batch["remaining_order_keys"]) == 1
    assert batch["compensating_cancel_attempted"] is False
    assert len(outcome.record["day_orders"]) == 1
    assert outcome.record["day_orders"][0]["broker_readback"]["partial"] is True
    assert len(account.buy_calls) == 2
    assert account.cancel_calls == []
    assert outcome.artifact_path is not None
    artifact = outcome.artifact_path.read_text(encoding="utf-8")
    assert "PARTIAL_BATCH_FAILURE" in artifact
    assert "성공이 아니다" in artifact
    assert batch["accepted_order_keys"][0] in artifact
    assert batch["remaining_order_keys"][0] in artifact
    assert "| false | day_order_submission_unverified:" in artifact


@pytest.mark.asyncio
async def test_ordering_rechecks_foreign_trace_before_first_batch_leg(
    table_dir, out_dir, armed, frozen_cycle_clock
) -> None:  # noqa: ANN001, ARG001
    """A foreign order appearing after preflight must stop the batch before L1."""

    _write_table(
        table_dir,
        generated_at=IN_SESSION - dt.timedelta(hours=4),
        buy_l2="68000",
    )

    class ForeignWriterBeforeBatchGate(_DynamicOrderingAccount):
        async def read_order_detail(  # noqa: ANN201
            self,
            *,
            order_date=None,
            symbol=None,  # noqa: ANN001
        ):
            rows = await super().read_order_detail(order_date=order_date, symbol=symbol)
            if len(self.detail_calls) == 1:
                self.rows.append(
                    {
                        "order_id": "foreign-writer-other-symbol",
                        "symbol": "000660",
                        "status": "open",
                        "ordered_quantity": 1,
                        "filled_quantity": 0,
                        "remaining_quantity": 1,
                        "unfilled_quantity": 1,
                        "ordered_price": 100_000,
                        "average_price": 0,
                    }
                )
            return rows

    account = ForeignWriterBeforeBatchGate()
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        lease_factory=lambda *_: _TestOrderingLease(),
    )

    assert outcome.exit_code == 0
    assert account.buy_calls == []
    assert outcome.record["batch_submission_status"] == (
        "failed_before_acknowledgement"
    )
    batch = outcome.record["same_cycle_buy_batches"][0]
    assert batch["status"] == "failed_before_acknowledgement"
    assert batch["accepted_order_keys"] == []
    assert batch["failure"] == {
        "reason": "mutation_boundary_not_clean",
        "detail": "foreign_same_day_orders_present",
    }
    assert outcome.record["mutation_boundaries"][-1]["action"].startswith("batch_gate:")
    assert outcome.record["mutation_boundaries"][-1]["foreign_same_day_orders"][
        "symbols"
    ] == ["000660"]


@pytest.mark.asyncio
async def test_ordering_rechecks_foreign_trace_between_batch_legs(
    table_dir, out_dir, armed, frozen_cycle_clock
) -> None:  # noqa: ANN001, ARG001
    _write_table(
        table_dir,
        generated_at=IN_SESSION - dt.timedelta(hours=4),
        buy_l2="68000",
    )

    class ForeignWriterInterleaves(_DynamicOrderingAccount):
        async def place_limit_buy(self, *, symbol, quantity, price):  # noqa: ANN001, ANN201
            result = await super().place_limit_buy(
                symbol=symbol, quantity=quantity, price=price
            )
            if len(self.buy_calls) == 1:
                self.rows.append(
                    {
                        "order_id": "foreign-between-legs",
                        "symbol": symbol,
                        "status": "open",
                        "ordered_quantity": 1,
                        "filled_quantity": 0,
                        "remaining_quantity": 1,
                        "unfilled_quantity": 1,
                        "ordered_price": price,
                        "average_price": 0,
                    }
                )
            return result

    account = ForeignWriterInterleaves()
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        lease_factory=lambda *_: _TestOrderingLease(),
    )

    assert outcome.exit_code == 2
    assert len(account.buy_calls) == 1
    assert account.cancel_calls == []
    batch = outcome.record["same_cycle_buy_batches"][0]
    assert batch["status"] == "partial_failure"
    assert batch["failure"]["reason"] == "mutation_boundary_not_clean"
    assert batch["failure"]["detail"] == "foreign_same_day_orders_present"
    assert (
        outcome.record["mutation_boundaries"][-1]["foreign_same_day_orders"]["count"]
        == 1
    )


@pytest.mark.asyncio
async def test_next_cycle_still_blocks_the_same_symbol_batch(
    table_dir, out_dir, armed, frozen_cycle_clock
) -> None:  # noqa: ANN001, ARG001
    _write_table(
        table_dir,
        generated_at=IN_SESSION - dt.timedelta(hours=4),
        buy_l2="68000",
    )
    account = _DynamicOrderingAccount()

    def readable_zero(**_kwargs):  # noqa: ANN003, ANN202
        return kiwoom_attr.RealizedPnlInput(value=Decimal("0"), source="test")

    first = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        lease_factory=lambda *_: _TestOrderingLease(),
        realized_pnl_reader=readable_zero,
    )
    second = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock + dt.timedelta(minutes=1),
        lease_factory=lambda *_: _TestOrderingLease(),
        realized_pnl_reader=readable_zero,
    )

    assert len(first.record["day_orders"]) == 2
    assert len(account.buy_calls) == 2, "cycle 2 must not stack another ladder"
    assert first.record["cycle_id"] != second.record["cycle_id"]
    assert second.record["orders"] == []
    assert second.record["submitted"] == []
    assert second.record["same_cycle_buy_batches"] == []
    assert [skip["reason"] for skip in second.record["skipped"]] == [
        "own_pending_order_exists"
    ]


@pytest.mark.asyncio
async def test_ladder_batches_preserve_the_1800000_cycle_cap_and_daily_symbol_cap(
    table_dir, out_dir, armed, frozen_cycle_clock
) -> None:  # noqa: ANN001, ARG001
    _write_table(
        table_dir,
        generated_at=IN_SESSION - dt.timedelta(hours=4),
        symbols=("005930", "000660", "035420", "051910"),
        buy_l2="68000",
    )
    account = _DynamicOrderingAccount()
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        lease_factory=lambda *_: _TestOrderingLease(),
    )

    assert outcome.exit_code == 0
    assert len(outcome.record["day_orders"]) == 6
    assert len({order["symbol"] for order in outcome.record["day_orders"]}) == 3
    assert all(
        order["notional_krw"] <= 300_000 for order in outcome.record["day_orders"]
    )
    assert (
        sum(order["notional_krw"] for order in outcome.record["day_orders"])
        <= 1_800_000
    )
    for symbol in {order["symbol"] for order in outcome.record["day_orders"]}:
        assert (
            sum(
                order["notional_krw"]
                for order in outcome.record["day_orders"]
                if order["symbol"] == symbol
            )
            <= 1_500_000
        )
    assert [
        batch["gate_checks"] for batch in outcome.record["same_cycle_buy_batches"]
    ] == [
        1,
        1,
        1,
    ]
    assert any(
        skip["symbol"] == "051910" and skip["reason"] == "daily_new_entry_cap_reached"
        for skip in outcome.record["skipped"]
    )


@pytest.mark.asyncio
async def test_ordering_stops_after_ack_when_its_readback_row_is_absent(
    table_dir, out_dir, armed, frozen_cycle_clock
) -> None:  # noqa: ANN001, ARG001
    """E2: one unproved ACK stops the rest of the DAY submission sequence."""

    class AckWithoutReadback(_DynamicOrderingAccount):
        async def place_limit_buy(self, *, symbol, quantity, price):  # noqa: ANN001, ANN201
            await FakeAccount.place_limit_buy(
                self, symbol=symbol, quantity=quantity, price=price
            )
            return {"return_code": 0, "ord_no": "0000000001"}

    _write_table(
        table_dir,
        generated_at=IN_SESSION - dt.timedelta(hours=4),
        symbols=("005930", "000660", "035420", "051910"),
    )
    account = AckWithoutReadback()
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        lease_factory=lambda *_: _TestOrderingLease(),
    )

    assert outcome.exit_code == 2
    assert outcome.record["submission_stopped"] == "broker_readback_unavailable"
    assert len(account.buy_calls) == 1
    assert len(outcome.record["day_orders"]) == 1
    assert "readback_failure" in outcome.record["day_orders"][0]


@pytest.mark.asyncio
async def test_ordering_blocks_when_fidelity_journal_is_unreadable(
    table_dir, out_dir, armed, frozen_cycle_clock
) -> None:  # noqa: ANN001, ARG001
    """E9: corrupt lifecycle evidence closes ORDERING before the venue."""

    fidelity = ordering_support.OrderingEventJournal.for_lane(
        root=out_dir, lane=kiwoom_cycle.LANE
    )
    fidelity.path.parent.mkdir(parents=True)
    fidelity.path.write_text("{not json\n", encoding="utf-8")
    account = _DynamicOrderingAccount()

    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        lease_factory=lambda *_: _TestOrderingLease(),
    )

    assert outcome.zero_order_reason == "ordering_fidelity_journal_unreadable"
    assert account.buy_calls == []
    assert account.sell_calls == []
    assert fidelity.path.read_text(encoding="utf-8") == "{not json\n"


@pytest.mark.asyncio
async def test_ordering_allows_a_sell_only_when_the_broker_fill_is_attributed(
    table_dir, out_dir, armed, frozen_cycle_clock, tmp_path
) -> None:  # noqa: ANN001, ARG001
    _write_table(
        table_dir,
        generated_at=IN_SESSION - dt.timedelta(hours=4),
        buy_l1=None,
        sell_r1="80000.00",
    )
    journal = kiwoom_attr.OwnOrderJournal(path=tmp_path / "own-orders.jsonl")
    journal.append(
        kiwoom_attr.OwnOrderRecord(
            at=frozen_cycle_clock.isoformat(),
            order_no="0000000042",
            correlation_id="b0xkw-prior-buy",
            symbol="005930",
            side="buy",
            price=70_000,
            quantity=10,
            order_date=kiwoom_attr.kst_order_date(frozen_cycle_clock),
        )
    )
    account = _DynamicOrderingAccount(
        positions=(position("005930", 10, average_price=70_000),),
        rows=[
            {
                "order_id": "0000000042",
                "symbol": "005930",
                "status": "filled",
                "ordered_quantity": 10,
                "filled_quantity": 10,
                "remaining_quantity": 0,
                "unfilled_quantity": 0,
                "ordered_price": 70_000,
                "average_price": 70_000,
            }
        ],
    )
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        journal=journal,
        lease_factory=lambda *_: _TestOrderingLease(),
        realized_pnl_reader=lambda **_kwargs: kiwoom_attr.RealizedPnlInput(
            value=Decimal("0"), source="test"
        ),
    )

    assert outcome.exit_code == 0
    assert account.buy_calls == []
    assert len(account.sell_calls) == 1
    assert outcome.record["day_orders"][0]["side"] == "sell"


@pytest.mark.asyncio
async def test_ordering_blocks_when_realized_pnl_cannot_be_proven(
    table_dir, out_dir, armed, frozen_cycle_clock, tmp_path
) -> None:  # noqa: ANN001, ARG001
    journal = kiwoom_attr.OwnOrderJournal(path=tmp_path / "own-orders.jsonl")
    journal.append(
        kiwoom_attr.OwnOrderRecord(
            at=frozen_cycle_clock.isoformat(),
            order_no="0000000042",
            correlation_id="b0xkw-prior-buy",
            symbol="005930",
            side="buy",
            price=70_000,
            quantity=1,
            order_date=kiwoom_attr.kst_order_date(frozen_cycle_clock),
        )
    )
    account = _DynamicOrderingAccount(
        rows=[
            {
                "order_id": "0000000042",
                "symbol": "005930",
                "status": "filled",
                "ordered_quantity": 1,
                "filled_quantity": 1,
                "remaining_quantity": 0,
                "unfilled_quantity": 0,
                "ordered_price": 70_000,
                "average_price": 70_000,
            }
        ]
    )
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        journal=journal,
        lease_factory=lambda *_: _TestOrderingLease(),
    )

    assert outcome.zero_order_reason == kiwoom_cycle.REALIZED_PNL_UNAVAILABLE_REASON
    assert outcome.record["realized_pnl_input"]["readable"] is False
    assert account.buy_calls == []
    assert account.sell_calls == []
    assert account.cancel_calls == []


@pytest.mark.asyncio
async def test_ordering_rechecks_foreign_trace_at_the_submit_boundary(
    table_dir, out_dir, armed, frozen_cycle_clock
) -> None:  # noqa: ANN001, ARG001
    class ForeignAfterPreflight(_DynamicOrderingAccount):
        def __init__(self) -> None:
            super().__init__()
            self.boundary_reads = 0

        async def read_order_detail(self, *, order_date=None, symbol=None):  # noqa: ANN001, ANN201, ARG002
            self.detail_calls.append({"order_date": order_date, "symbol": symbol})
            self.boundary_reads += 1
            if self.boundary_reads == 1:
                return []
            return [
                {
                    "order_id": "foreign-1",
                    "symbol": "000660",
                    "status": "open",
                    "ordered_quantity": 1,
                    "filled_quantity": 0,
                    "remaining_quantity": 1,
                    "unfilled_quantity": 1,
                    "ordered_price": 70_000,
                    "average_price": 0,
                }
            ]

    account = ForeignAfterPreflight()
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        lease_factory=lambda *_: _TestOrderingLease(),
    )

    assert outcome.zero_order_reason == "mutation_boundary_not_clean"
    assert len(outcome.record["mutation_boundaries"]) >= 2
    assert (
        outcome.record["mutation_boundaries"][-1]["foreign_same_day_orders"]["count"]
        == 1
    )
    assert account.buy_calls == []
    assert account.sell_calls == []


@pytest.mark.asyncio
async def test_ordering_flock_without_j3a_grant_makes_zero_transport(
    table_dir, out_dir, armed, frozen_cycle_clock
) -> None:  # noqa: ANN001, ARG001
    account = _DynamicOrderingAccount()
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        lease_factory=lambda *_: _TestOrderingLease(),
        coordination_factory=None,
    )

    assert account.buy_calls == []
    assert account.sell_calls == []
    assert account.cancel_calls == []
    assert outcome.record["coordination"]["authorizes_send"] is False
    assert outcome.record["coordination"]["local_flock_authorizes_send"] is False
    assert outcome.record.get("lane_lifecycle_status") == (
        ordering_support.KIWOOM_LIFECYCLE_STATUS
    )


@pytest.mark.asyncio
async def test_ordering_blocks_when_its_writer_lease_is_lost_before_submit(
    table_dir, out_dir, armed, frozen_cycle_clock
) -> None:  # noqa: ANN001, ARG001
    class LostBeforeSubmit(_TestOrderingLease):
        def assert_held(self) -> None:
            self.checks += 1
            if self.checks >= 2:
                raise RuntimeError("lease disappeared")

    account = _DynamicOrderingAccount()
    lease = LostBeforeSubmit()
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        lease_factory=lambda *_: lease,
    )

    assert outcome.zero_order_reason == "mutation_boundary_not_clean"
    assert outcome.record["mutation_boundaries"][-1]["blocking_reason"] == (
        "writer_lease_lost"
    )
    assert account.buy_calls == []
    assert account.sell_calls == []


@pytest.mark.asyncio
async def test_ordering_rechecks_own_fill_quantity_before_a_sell(
    table_dir, out_dir, armed, frozen_cycle_clock, tmp_path
) -> None:  # noqa: ANN001, ARG001
    _write_table(
        table_dir,
        generated_at=IN_SESSION - dt.timedelta(hours=4),
        buy_l1=None,
        sell_r1="80000.00",
    )
    journal = kiwoom_attr.OwnOrderJournal(path=tmp_path / "own-orders.jsonl")
    journal.append(
        kiwoom_attr.OwnOrderRecord(
            at=frozen_cycle_clock.isoformat(),
            order_no="0000000042",
            correlation_id="b0xkw-prior-buy",
            symbol="005930",
            side="buy",
            price=70_000,
            quantity=10,
            order_date=kiwoom_attr.kst_order_date(frozen_cycle_clock),
        )
    )
    full_fill = {
        "order_id": "0000000042",
        "symbol": "005930",
        "status": "filled",
        "ordered_quantity": 10,
        "filled_quantity": 10,
        "remaining_quantity": 0,
        "unfilled_quantity": 0,
        "ordered_price": 70_000,
        "average_price": 70_000,
    }
    missing_fill = {**full_fill, "filled_quantity": 0, "average_price": 0}

    class FillDisappearsBeforeSell(_DynamicOrderingAccount):
        def __init__(self) -> None:
            super().__init__(positions=(position("005930", 10),))
            self.answers = [[full_fill], [full_fill], [missing_fill]]
            self.answer_index = 0

        async def read_order_detail(self, *, order_date=None, symbol=None):  # noqa: ANN001, ANN201, ARG002
            self.detail_calls.append({"order_date": order_date, "symbol": symbol})
            answer = self.answers[min(self.answer_index, len(self.answers) - 1)]
            self.answer_index += 1
            return [dict(row) for row in answer]

    account = FillDisappearsBeforeSell()
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        journal=journal,
        lease_factory=lambda *_: _TestOrderingLease(),
        realized_pnl_reader=lambda **_kwargs: kiwoom_attr.RealizedPnlInput(
            value=Decimal("0"), source="test"
        ),
    )

    assert account.sell_calls == []
    assert outcome.record["submission_blocked"][0]["reason"] == (
        "own_fill_sell_gate_blocked"
    )


@pytest.mark.asyncio
async def test_ordering_stops_the_cycle_when_fresh_sell_attribution_is_unreadable(
    table_dir, out_dir, armed, frozen_cycle_clock, monkeypatch
) -> None:  # noqa: ANN001, ARG001
    """A failed sell-side evidence read cannot be bypassed by later buys."""

    _write_table(
        table_dir,
        generated_at=IN_SESSION - dt.timedelta(hours=4),
        buy_l1=None,
        sell_r1="80000.00",
    )
    calls = 0

    async def _attribution(**_kwargs):  # noqa: ANN003, ANN202
        nonlocal calls
        calls += 1
        if calls == 1:
            return kr_attribution.OwnFillAttribution(
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
        return kr_attribution.attribution_unreadable("OSError")

    monkeypatch.setattr(kiwoom_attr, "read_own_attribution", _attribution)
    account = _DynamicOrderingAccount(positions=(position("005930", 10),))
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        lease_factory=lambda *_: _TestOrderingLease(),
        realized_pnl_reader=lambda **_kwargs: kiwoom_attr.RealizedPnlInput(
            value=Decimal("0"), source="test"
        ),
    )

    assert outcome.zero_order_reason == "fresh_sell_attribution_unavailable"
    assert account.buy_calls == []
    assert account.sell_calls == []


@pytest.mark.asyncio
async def test_ordering_kill_cancels_own_pending_and_proves_it_gone(
    table_dir, out_dir, armed, frozen_cycle_clock, tmp_path
) -> None:  # noqa: ANN001, ARG001
    journal = kiwoom_attr.OwnOrderJournal(path=tmp_path / "own-orders.jsonl")
    journal.append(
        kiwoom_attr.OwnOrderRecord(
            at=frozen_cycle_clock.isoformat(),
            order_no="0000000042",
            correlation_id="b0xkw-prior-buy",
            symbol="005930",
            side="buy",
            price=70_000,
            quantity=3,
            order_date=kiwoom_attr.kst_order_date(frozen_cycle_clock),
        )
    )

    class KillAccount(_DynamicOrderingAccount):
        async def cancel(self, *, original_order_no, symbol, cancel_quantity):  # noqa: ANN001, ANN201
            await super().cancel(
                original_order_no=original_order_no,
                symbol=symbol,
                cancel_quantity=cancel_quantity,
            )
            self.rows = [
                row for row in self.rows if row["order_id"] != original_order_no
            ]
            return {"return_code": 0, "ord_no": "0000000043"}

    account = KillAccount(
        rows=[
            {
                "order_id": "0000000042",
                "symbol": "005930",
                "status": "open",
                "ordered_quantity": 3,
                "filled_quantity": 0,
                "remaining_quantity": 3,
                "unfilled_quantity": 3,
                "ordered_price": 70_000,
                "average_price": 0,
            }
        ]
    )
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        journal=journal,
        lease_factory=lambda *_: _TestOrderingLease(),
        realized_pnl_reader=lambda **_kwargs: kiwoom_attr.RealizedPnlInput(
            value=Decimal("-300000"), source="test_kill"
        ),
    )

    assert outcome.record["kill_switch"]["allow_new_orders"] is False
    assert account.buy_calls == []
    assert account.sell_calls == []
    assert len(account.cancel_calls) == 1
    assert outcome.record["kill_cancellation"]["confirmed"] is True
    assert any(
        event["event"] == "cancel_ack" for event in outcome.record["fidelity_events"]
    )


@pytest.mark.asyncio
async def test_ordering_kill_rejects_cancel_ack_when_own_order_still_rests(
    table_dir, out_dir, armed, frozen_cycle_clock, tmp_path
) -> None:  # noqa: ANN001, ARG001
    """E4: a cancel ACK is not proof until its per-order broker re-read clears."""

    journal = kiwoom_attr.OwnOrderJournal(path=tmp_path / "own-orders.jsonl")
    journal.append(_own_pending_record(now=frozen_cycle_clock))
    account = _DynamicOrderingAccount(rows=[_own_pending_row()])

    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        journal=journal,
        lease_factory=lambda *_: _TestOrderingLease(),
        realized_pnl_reader=lambda **_kwargs: kiwoom_attr.RealizedPnlInput(
            value=Decimal("-300000"), source="test_kill"
        ),
    )

    assert outcome.exit_code == 2
    assert outcome.zero_order_reason == "kill_cancel_not_confirmed"
    assert len(account.cancel_calls) == 1
    assert outcome.record["kill_cancellation"]["confirmed"] is False
    assert any(
        event["action"].startswith("kill_cancel_reconcile:")
        for event in outcome.record["fidelity_events"]
        if event["event"] == "mutation_boundary"
    )


@pytest.mark.asyncio
async def test_ordering_kill_rejects_own_pending_reappearing_at_final_reconcile(
    table_dir, out_dir, armed, frozen_cycle_clock, monkeypatch, tmp_path
) -> None:  # noqa: ANN001, ARG001
    """E1: all per-order rechecks passing is insufficient without final proof."""

    async def empty_attribution(**_kwargs):  # noqa: ANN003, ANN202
        return kr_attribution.OwnFillAttribution(lots=())

    monkeypatch.setattr(kiwoom_attr, "read_own_attribution", empty_attribution)
    journal = kiwoom_attr.OwnOrderJournal(path=tmp_path / "own-orders.jsonl")
    journal.append(_own_pending_record(now=frozen_cycle_clock))
    row = _own_pending_row()

    class ReappearsOnFinalReconcile(_DynamicOrderingAccount):
        def __init__(self) -> None:
            super().__init__(rows=[row])
            self.boundary_reads = 0

        async def read_order_detail(self, *, order_date=None, symbol=None):  # noqa: ANN001, ANN201, ARG002
            self.detail_calls.append({"order_date": order_date, "symbol": symbol})
            self.boundary_reads += 1
            if self.boundary_reads == 4:
                return []
            return [dict(row)]

    account = ReappearsOnFinalReconcile()
    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        ordering=True,
        now=frozen_cycle_clock,
        journal=journal,
        lease_factory=lambda *_: _TestOrderingLease(),
        realized_pnl_reader=lambda **_kwargs: kiwoom_attr.RealizedPnlInput(
            value=Decimal("-300000"), source="test_kill"
        ),
    )

    assert outcome.exit_code == 2
    assert outcome.zero_order_reason == "kill_own_pending_remains"
    assert len(account.cancel_calls) == 1
    assert account.boundary_reads == 5
    assert outcome.record["kill_cancellation"]["confirmed"] is False


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
    offline_entry = bound_kiwoom_entry()
    _attach_test_mock_client(account)
    await kiwoom_cycle.run_kiwoom_cycle(
        now=frozen_cycle_clock,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        account=account,
        journal=journal,
        coordination_factory=lambda: offline_coordination_factory(entry=offline_entry),
        coordination_entry=offline_entry,
    )
    records = journal.read_all()
    assert [record.order_no for record in records] == [
        "0000123456",
        "0000123457",
    ]
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
    assert len(outcome.record["round_trip"]) == 1
    assert outcome.record["round_trip"][0]["failure"] == "cancel_unconfirmed"
    assert outcome.record["round_trip_failures"]


def _lose_authority_only_at_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    events: list[str] = []
    original = CoordinationScope.assert_owned

    async def _assert(self: CoordinationScope) -> None:
        events.append("assert_owned")
        if len(events) == 2:
            raise RuntimeError("simulated authority loss before cancel")
        await original(self)

    monkeypatch.setattr(CoordinationScope, "assert_owned", _assert)
    return events


@pytest.mark.asyncio
async def test_authority_blocked_cancel_records_cycle_risk_before_fake_alert(
    table_dir, out_dir, armed, monkeypatch
) -> None:  # noqa: ANN001, ARG001
    """AC7: no cancel outside scope, and the live-order risk is never silent."""

    assertions = _lose_authority_only_at_cancel(monkeypatch)
    account = FakeAccount(
        resting=[
            (),
            (),
            (resting("0000123456", "005930", price=70_000, remaining=4),),
        ]
    )
    entry = bound_kiwoom_entry()
    adapter = build_offline_adapter(entry=entry)
    alerts: list[dict[str, Any]] = []

    async def fake_alert(risk: dict[str, Any]) -> None:
        immediate = (out_dir / kiwoom_cycle.LANE / "cycles.jsonl").read_text(
            encoding="utf-8"
        )
        assert kiwoom_lane.MANDATORY_CANCEL_BLOCKED_BY_AUTHORITY in immediate
        alerts.append(risk)

    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        coordination_factory=lambda: adapter,
        coordination_entry=entry,
        authority_risk_notifier=fake_alert,
    )

    assert assertions == ["assert_owned", "assert_owned"]
    assert len(account.buy_calls) == 1
    assert account.cancel_calls == []
    assert outcome.exit_code == 2
    risk = outcome.record["live_order_risk"]
    assert risk == {
        "present": True,
        "status": kiwoom_lane.MANDATORY_CANCEL_BLOCKED_BY_AUTHORITY,
        "order_id": "0000123456",
        "symbol": "005930",
        "quantity": 4,
        "side": "buy",
        "at": risk["at"],
        "operator_notification": "sent",
    }
    assert alerts and alerts[0]["operator_notification"] == "pending"
    assert outcome.record["round_trip"][0]["cancel_attempted"] is False
    assert adapter.last_result is not None
    assert adapter.last_result.certainty is MutationCertainty.UNCERTAIN
    assert adapter.last_result.evidence.broker_order_id == risk["order_id"]
    assert adapter.last_result.claim.row_id > 0


@pytest.mark.asyncio
async def test_authority_risk_flag_survives_fake_alert_failure(
    table_dir, out_dir, armed, monkeypatch
) -> None:  # noqa: ANN001, ARG001
    """AC7: alert failure cannot roll back the already-appended risk snapshot."""

    _lose_authority_only_at_cancel(monkeypatch)
    account = FakeAccount(
        resting=[
            (),
            (),
            (resting("0000123456", "005930", price=70_000, remaining=4),),
        ]
    )

    async def failing_fake_alert(_risk: dict[str, Any]) -> None:
        raise RuntimeError("fake notification failure")

    outcome = await _run(
        account=account,
        out_dir=out_dir,
        table_dir=table_dir,
        confirm=True,
        authority_risk_notifier=failing_fake_alert,
    )

    assert account.cancel_calls == []
    assert outcome.exit_code == 2
    assert outcome.record["live_order_risk"]["present"] is True
    assert outcome.record["live_order_risk"]["operator_notification"] == "failed"
    assert (
        outcome.record["round_trip"][0]["risk_notification_error_type"]
        == "RuntimeError"
    )
    cycle_lines = (out_dir / kiwoom_cycle.LANE / "cycles.jsonl").read_text(
        encoding="utf-8"
    )
    assert "live_order_risk_immediate" in cycle_lines


@pytest.mark.asyncio
async def test_authority_risk_default_alert_reuses_existing_telegram_notifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7: the default alert is the existing notifier, never a new transport."""

    from app.monitoring import trade_notifier

    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(
        trade_notifier,
        "get_trade_notifier",
        lambda: SimpleNamespace(notify_agent_message=notify),
    )
    risk = {
        "status": kiwoom_lane.MANDATORY_CANCEL_BLOCKED_BY_AUTHORITY,
        "order_id": "0000123456",
        "symbol": "005930",
        "quantity": 4,
        "side": "buy",
        "at": IN_SESSION.isoformat(),
    }

    await kiwoom_cycle._notify_mandatory_cancel_risk(risk)

    notify.assert_awaited_once()
    _, kwargs = notify.await_args
    assert kwargs["skip_discord"] is True
    assert kwargs["mirror_telegram"] is True
    assert kwargs["correlation_id"] == risk["order_id"]


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
