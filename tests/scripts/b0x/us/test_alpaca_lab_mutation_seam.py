"""ROB-1267 J5B §6.2 — contaminated / foreign / unlinked residue is a pre-submit stop.

The contract in `docs/contracts/rob-1267-us-alpaca-lab-recovery.md` §6.2 claims
that the lab lane's mutation seam is never reached while the account carries
residue the lane cannot attribute to itself.  These tests state that as a
**call-count** contract: the injected submit/cancel callbacks are invoked
exactly zero times.

Three shapes of residue are covered, because only the first is obvious:

* a genuinely foreign open order (no `b0xu-` correlation anywhere),
* an *unlinked* position — one that does have `b0xu-` executions, but whose
  broker quantity does not reconcile against them, and
* a correlated position whose fill quantity is unreadable.

The last two are the ones that could plausibly have been recorded as a mere
note.  `_attribute_positions` puts every attribution failure into the foreign
set as well, which is what makes the contamination flag cover them; the
structural test below pins that relationship directly.

Everything is offline: fake readers, injected callbacks, no broker, no ledger,
no network.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.mcp_server.tooling.market_session import US_SESSION_REGULAR
from scripts.b0x.broker_truth import BrokerTruth
from scripts.b0x.us import alpaca
from scripts.b0x.us import cycle as us_cycle
from scripts.policy_table.core.schema import compute_policy_table_hash
from tests.scripts.b0x._table_fixtures import make_payload, make_row, write_table

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 10, 15, 0, tzinfo=dt.UTC)


def _table_dir(tmp_path: Path) -> Path:
    payload = make_payload(
        market="us",
        rows=[
            make_row(
                symbol="AAPL",
                previous_close="100",
                buy_l1="97",
                sell_r1="105",
                sell_r2="110",
            )
        ],
        generated_at=NOW - dt.timedelta(hours=1),
    )
    payload["config"] = {
        **payload["config"],
        "quote_currency": "USD",
        "new_entry_notional_usd_min": "150",
        "new_entry_notional_usd_max": "450",
        "new_entry_notional_usd": "300",
    }
    payload["sizing"] = {}
    payload["stamps"]["policy_table_hash"] = compute_policy_table_hash(
        {key: value for key, value in payload.items() if key != "stamps"}
    )
    directory = tmp_path / "policy-tables"
    write_table(directory, payload, market="us")
    return directory


def _readers(
    *,
    positions: list[dict[str, Any]] | None = None,
    orders: list[dict[str, Any]] | None = None,
    ledger: list[dict[str, Any]] | None = None,
) -> alpaca.LabReaders:
    async def account(**_: Any) -> dict[str, Any]:
        return {
            "success": True,
            "account_mode": alpaca.LANE,
            "account": {"cash": "5000", "portfolio_value": "5000"},
        }

    def _page(key: str, values: list[dict[str, Any]] | None) -> dict[str, Any]:
        items = values or []
        return {
            "success": True,
            "account_mode": alpaca.LANE,
            "count": len(items),
            key: items,
        }

    async def list_positions(**_: Any) -> dict[str, Any]:
        return _page("positions", positions)

    async def list_orders(**_: Any) -> dict[str, Any]:
        return _page("orders", orders)

    async def list_ledger(**_: Any) -> dict[str, Any]:
        return _page("items", ledger)

    return alpaca.LabReaders(
        get_account=account,
        list_positions=list_positions,
        list_orders=list_orders,
        list_recent_ledger=list_ledger,
    )


def _position(symbol: str, qty: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "qty": qty,
        "qty_available": qty,
        "avg_entry_price": "20",
    }


def _execution(
    *,
    symbol: str,
    filled_qty: str | None,
    broker_order_id: str = "own-1",
    side: str = "buy",
    age: dt.timedelta = dt.timedelta(days=3),
) -> dict[str, Any]:
    """A b0xu execution row.

    ``age`` defaults to a *prior* UTC day on purpose.  A same-day execution
    trips the realized-P&L fail-closed first (covered separately below), which
    would hide the contamination gate these cases are here to exercise.
    """
    return {
        "lifecycle_correlation_id": f"{alpaca.B0XU_CORRELATION_PREFIX}{symbol.lower()}",
        "account_mode": alpaca.LANE,
        "record_kind": "execution",
        "client_order_id": f"coid-{symbol.lower()}",
        "broker_order_id": broker_order_id,
        "execution_symbol": symbol,
        "side": side,
        "filled_qty": filled_qty,
        "filled_avg_price": "20",
        "created_at": (NOW - age).isoformat(),
    }


class _CountingSeam:
    """A submitter/canceler that must never be called."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"args": args, **kwargs})
        return {"submitted": True}


# ---------------------------------------------------------------------------
# residue shapes → zero seam calls
# ---------------------------------------------------------------------------
_RESIDUE_CASES = {
    # An open order with no b0xu correlation at all.
    "foreign_open_order": {
        "positions": [],
        "orders": [{"id": "someone-elses", "symbol": "UBER"}],
        "ledger": [],
    },
    # A position that IS b0xu-correlated, but whose broker quantity does not
    # equal the signed b0xu fills.  Correlated, yet unattributable.
    "unlinked_quantity_mismatch": {
        "positions": [_position("AAPL", "5")],
        "orders": [],
        "ledger": [_execution(symbol="AAPL", filled_qty="2")],
    },
    # A b0xu-correlated position whose fill quantity cannot be read back.
    "unlinked_unreadable_fill": {
        "positions": [_position("AAPL", "2")],
        "orders": [],
        "ledger": [_execution(symbol="AAPL", filled_qty=None)],
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_RESIDUE_CASES), ids=sorted(_RESIDUE_CASES))
async def test_residue_blocks_confirmed_submit_with_zero_seam_calls(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(us_cycle, "us_market_session", lambda now: US_SESSION_REGULAR)
    submitter = _CountingSeam()
    canceler = _CountingSeam()

    outcome = await us_cycle.run_us_cycle(
        now=NOW,
        table_dir=_table_dir(tmp_path),
        out_dir=tmp_path / "observations",
        confirm=True,
        readers=_readers(**_RESIDUE_CASES[case]),  # type: ignore[arg-type]
        submitter=submitter,
        canceler=canceler,
    )

    assert outcome.record["contaminated"] is True
    assert outcome.record["submitted"] == []
    assert "contaminated lab account state" in outcome.record["submission_skipped"]
    assert submitter.calls == [], f"{case}: submit seam was called under residue"
    assert canceler.calls == [], f"{case}: cancel seam was called under residue"


@pytest.mark.asyncio
async def test_contamination_gate_precedes_the_confirm_and_submitter_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Residue is reported as residue, not mislabelled as a missing seam."""
    monkeypatch.setattr(us_cycle, "us_market_session", lambda now: US_SESSION_REGULAR)
    submitter = _CountingSeam()

    outcome = await us_cycle.run_us_cycle(
        now=NOW,
        table_dir=_table_dir(tmp_path),
        out_dir=tmp_path / "observations",
        confirm=True,
        readers=_readers(**_RESIDUE_CASES["foreign_open_order"]),  # type: ignore[arg-type]
        submitter=submitter,
    )
    skipped = outcome.record["submission_skipped"]
    assert "contaminated lab account state" in skipped
    assert "confirm=False" not in skipped
    assert "no approved injected" not in skipped
    assert submitter.calls == []


@pytest.mark.asyncio
async def test_same_day_unattributable_execution_stops_even_earlier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contamination gate is not the only stop, and none of them submits.

    When the unattributable execution is from the *current* UTC day, the cycle
    fails closed on the realized-P&L read model before it ever reaches the
    submission gates.  Recorded here so the ordering is pinned rather than
    accidental — and so the seam call count is asserted on that path too.
    """
    monkeypatch.setattr(us_cycle, "us_market_session", lambda now: US_SESSION_REGULAR)
    submitter = _CountingSeam()
    canceler = _CountingSeam()

    outcome = await us_cycle.run_us_cycle(
        now=NOW,
        table_dir=_table_dir(tmp_path),
        out_dir=tmp_path / "observations",
        confirm=True,
        readers=_readers(
            positions=[_position("AAPL", "5")],
            ledger=[
                _execution(symbol="AAPL", filled_qty="2", age=dt.timedelta(hours=2))
            ],
        ),
        submitter=submitter,
        canceler=canceler,
    )

    assert outcome.zero_order_reason == us_cycle.REALIZED_PNL_UNAVAILABLE_REASON
    assert outcome.record["submitted"] == []
    assert outcome.record["fresh_truth"]["foreign_position_symbols"] == ["AAPL"]
    assert submitter.calls == []
    assert canceler.calls == []


# ---------------------------------------------------------------------------
# the structural reason the above holds
# ---------------------------------------------------------------------------
def _raw(symbol: str, qty: str) -> alpaca.RawPosition:
    return alpaca.RawPosition(
        symbol=symbol,
        quantity=Decimal(qty),
        quantity_available=Decimal(qty),
        average_price=Decimal("20"),
    )


def _ledger_execution(
    symbol: str, filled_qty: str | None, side: str = "buy"
) -> alpaca.LedgerExecution:
    return alpaca.LedgerExecution(
        correlation_id=f"{alpaca.B0XU_CORRELATION_PREFIX}{symbol.lower()}",
        client_order_id=f"coid-{symbol.lower()}",
        broker_order_id="own-1",
        symbol=symbol,
        side=side,
        filled_qty=Decimal(filled_qty) if filled_qty is not None else None,
        filled_avg_price=Decimal("20"),
        created_at=NOW - dt.timedelta(hours=2),
    )


@pytest.mark.parametrize(
    "executions",
    [
        pytest.param((), id="no_correlation"),
        pytest.param((_ledger_execution("AAPL", None),), id="unreadable_fill"),
        pytest.param((_ledger_execution("AAPL", "2"),), id="quantity_mismatch"),
        pytest.param(
            (
                _ledger_execution("AAPL", "5"),
                _ledger_execution("AAPL", "5", side="sell"),
            ),
            id="net_zero_signed_quantity",
        ),
    ],
)
def test_every_linkage_failure_is_also_a_contamination(
    executions: tuple[alpaca.LedgerExecution, ...],
) -> None:
    """A linkage failure may never be recorded as a note only.

    If a failure could be appended to the notes without also entering the
    foreign set, the contamination flag would miss it and a confirmed cycle
    would submit against an account it cannot account for.
    """
    owned, foreign, failures, _sources, _readable = alpaca._attribute_positions(
        (_raw("AAPL", "5"),), executions
    )
    assert failures, "fixture did not produce a linkage failure"
    assert owned == ()
    failed_symbols = {failure.split(":", 1)[0] for failure in failures}
    assert failed_symbols <= set(foreign), (
        f"linkage failure(s) {sorted(failed_symbols)} absent from the foreign "
        f"set {sorted(foreign)}: contamination would not be raised"
    )


# ---------------------------------------------------------------------------
# the seam itself stays unwired, and an unconfirmed submit does not call it
# ---------------------------------------------------------------------------
def _planned() -> alpaca.PlannedOrder:
    return alpaca.PlannedOrder(
        order_key="rob1267-seam",
        lifecycle_correlation_id=f"{alpaca.B0XU_CORRELATION_PREFIX}rob1267-seam",
        symbol="AAPL",
        side="buy",
        leg="buy_l1",
        price=Decimal("100"),
        quantity=Decimal("3"),
        notional=Decimal("300"),
    )


def _empty_truth() -> alpaca.FreshTruth:
    return alpaca.FreshTruth(
        cash=Decimal("1"),
        nav=Decimal("1"),
        positions=(),
        open_orders=(),
        own_open_orders=(alpaca.RawOpenOrder(broker_order_id="own-1", symbol="AAPL"),),
        foreign_open_orders=(),
        own_positions=(),
        foreign_position_symbols=(),
        position_linkage_failures=(),
        sell_source_client_order_ids={},
        realized_pnl_today=Decimal("0"),
        cumulative_deployment_readable=True,
    )


@pytest.mark.asyncio
async def test_unconfirmed_submit_does_not_call_a_present_seam(
    tmp_path: Path,
) -> None:
    """confirm is not a formality: a seam that exists is still not called."""
    from scripts.b0x.table_source import PolicyTable, load_policy_table

    submitter = _CountingSeam()
    table = load_policy_table(market="us", now=NOW, table_dir=_table_dir(tmp_path))
    assert isinstance(table, PolicyTable)

    result = await alpaca.submit_planned_order(
        planned=_planned(),
        table=table,
        confirm=False,
        broker_truth=BrokerTruth(position_symbols=(), own_pending=()),
        submitter=submitter,
    )

    assert result["submitted"] is False
    assert result["reason_code"] == "confirmation_required"
    assert result["account_mode"] == alpaca.LANE
    assert submitter.calls == []


@pytest.mark.asyncio
async def test_unconfirmed_cancel_does_not_call_a_present_seam() -> None:
    canceler = _CountingSeam()
    assert (
        await alpaca.cancel_own_open_orders(
            fresh=_empty_truth(), confirm=False, canceler=canceler
        )
        == []
    )
    assert canceler.calls == []
