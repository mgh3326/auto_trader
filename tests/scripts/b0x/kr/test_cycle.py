"""End-to-end kis_mock cycle behaviour — no network, no venue, no live KIS call.

``_FakeKrClient`` stands in for ``ReadOnlyKISMockDomesticClient``; every test
here passes it explicitly via ``client=`` so ``run_kr_cycle`` never
constructs a real one.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from scripts.b0x.kr.cycle import OUTSIDE_RTH_REASON, run_kr_cycle
from scripts.b0x.kr.mock import KrMockSubmissionNotWired
from scripts.b0x.labels import TRUST_LABELS
from scripts.b0x.ledger import ObservationLedger
from tests.scripts.b0x._table_fixtures import (
    make_payload,
    make_row,
    write_stale_marker,
    write_table,
)

pytestmark = pytest.mark.unit

# 2026-08-10 is a Monday; 02:00 UTC = 11:00 KST, inside the XKRX regular
# session (verified against exchange_calendars directly while writing this).
IN_SESSION_NOW = dt.datetime(2026, 8, 10, 2, 0, tzinfo=dt.UTC)
# 2026-08-08 is a Saturday — outside any session, regardless of time of day.
WEEKEND_NOW = dt.datetime(2026, 8, 8, 2, 0, tzinfo=dt.UTC)


@pytest.fixture
def table_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "policy-tables"
    write_table(
        directory,
        make_payload(
            market="kr",
            rows=[
                make_row(
                    symbol="005930",
                    previous_close="97000",
                    buy_l1="94090",
                    sell_r1="101850",
                    sell_r2="106700",
                ),
                make_row(symbol="000660", previous_close="200000", buy_l1="194000"),
            ],
            generated_at=IN_SESSION_NOW - dt.timedelta(hours=1),
        ),
        market="kr",
    )
    return directory


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "observations"


class _FakeKrClient:
    """Flat kis_mock account: cash only, no holdings."""

    def __init__(
        self,
        *,
        orderable_cash: str = "5000000",
        stocks: list[dict[str, Any]] | None = None,
    ) -> None:
        self._cash = {
            "dnca_tot_amt": float(orderable_cash),
            "stck_cash_ord_psbl_amt": float(orderable_cash),
        }
        self._stocks = stocks or []
        self.closed = False

    async def inquire_cash_balance(self) -> dict[str, Any]:
        return self._cash

    async def fetch_my_stocks(self) -> list[dict[str, Any]]:
        return self._stocks

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_outside_regular_session_derives_zero_orders(
    table_dir: Path, out_dir: Path
) -> None:
    outcome = await run_kr_cycle(
        now=WEEKEND_NOW, table_dir=table_dir, out_dir=out_dir, client=_FakeKrClient()
    )
    assert outcome.zero_order_reason == OUTSIDE_RTH_REASON
    assert outcome.order_count == 0
    assert outcome.record["orders"] == []
    assert outcome.record["submitted"] == []
    # The RTH gate must be checked before any table/account I/O — no table
    # fields should appear in the record at all.
    assert "policy_table_hash" not in outcome.record


@pytest.mark.asyncio
async def test_stale_table_derives_zero_orders_even_inside_session(
    table_dir: Path, out_dir: Path
) -> None:
    write_stale_marker(table_dir, market="kr")
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW, table_dir=table_dir, out_dir=out_dir, client=_FakeKrClient()
    )
    assert outcome.zero_order_reason == "stale_marker_present"
    assert outcome.order_count == 0


@pytest.mark.asyncio
async def test_dry_run_plans_but_never_dispatches(
    table_dir: Path, out_dir: Path
) -> None:
    client = _FakeKrClient(orderable_cash="5000000")
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=client,
    )
    assert outcome.zero_order_reason is None
    assert outcome.order_count > 0
    assert outcome.record["planned"], "expected planned buy legs for both symbols"
    assert outcome.record["submitted"] == []
    assert outcome.record["submission_skipped"] == "confirm=False — preview only"
    # A caller-supplied client is caller-owned: the cycle must not close it.
    assert client.closed is False

    # 3/3 fixed trust labels, in order, are present in the rendered artifact.
    artifact_text = outcome.artifact_path.read_text(encoding="utf-8")
    for label in TRUST_LABELS:
        assert label in artifact_text


@pytest.mark.asyncio
async def test_owns_client_path_constructs_and_closes_its_own_client(
    table_dir: Path, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no ``client=`` is supplied, the cycle builds and closes its own.

    Monkeypatches ``scripts.b0x.kr.mock``'s real read-only facade class so
    this still makes zero network calls.
    """

    from scripts.b0x.kr import mock as kr_mock_module

    fake = _FakeKrClient(orderable_cash="5000000")
    monkeypatch.setattr(kr_mock_module, "ReadOnlyKISMockDomesticClient", lambda: fake)

    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW, table_dir=table_dir, out_dir=out_dir, confirm=False
    )
    assert outcome.zero_order_reason is None
    assert fake.closed is True


@pytest.mark.asyncio
async def test_confirm_true_fails_closed_because_submission_is_unwired(
    table_dir: Path, out_dir: Path
) -> None:
    """``confirm=True`` must not silently no-op — it must fail loudly.

    This is the load-bearing assertion for the module docstring's claim that
    a cycle can never mistake "not yet built" for "submitted and confirmed
    zero orders": with real orders planned and confirm=True, the cycle must
    raise rather than return a clean-looking zero-submission record.
    """

    client = _FakeKrClient(orderable_cash="5000000")
    with pytest.raises(KrMockSubmissionNotWired):
        await run_kr_cycle(
            now=IN_SESSION_NOW,
            table_dir=table_dir,
            out_dir=out_dir,
            confirm=True,
            client=client,
        )


@pytest.mark.asyncio
async def test_kill_switch_trips_on_nav_ratio_and_blocks_all_new_orders(
    table_dir: Path, out_dir: Path
) -> None:
    """A same-cycle NAV of 1,000,000 with a persisted -30,000 realized loss
    is a 3% drawdown — over the KR envelope's 2.5% NAV kill.
    """

    ledger = ObservationLedger(lane="kis_mock", root=out_dir)
    ledger.ensure()
    from scripts.b0x.ledger import store_json_state

    store_json_state(
        ledger.lane_dir / "attributed_book.json",
        {
            "utc_day": IN_SESSION_NOW.date().isoformat(),
            "positions": {},
            "new_entry_symbols_today": [],
            "realized_pnl_today": "-30000",
        },
    )
    client = _FakeKrClient(orderable_cash="1000000")
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=client,
    )
    assert outcome.derivation is not None
    assert outcome.derivation.kill_switch.tripped
    assert outcome.record["planned"] == []
    assert outcome.record["submitted"] == []
    assert all(
        skip["reason"] == "kill_switch_active" for skip in outcome.record["skipped"]
    )


@pytest.mark.asyncio
async def test_determinism_same_inputs_same_derivation_hash(
    table_dir: Path, out_dir: Path
) -> None:
    """Same table + same account state -> byte-identical derivation_hash.

    Two independent cycles against unchanged fixtures (fresh client each
    time, same balances) must derive the same hash — contract §2-1.
    """

    hashes = []
    for _ in range(2):
        outcome = await run_kr_cycle(
            now=IN_SESSION_NOW,
            table_dir=table_dir,
            out_dir=out_dir,
            confirm=False,
            client=_FakeKrClient(orderable_cash="5000000"),
        )
        assert outcome.derivation is not None
        hashes.append(outcome.derivation.derivation_hash())
    assert len(set(hashes)) == 1, f"derivation hash diverged across runs: {hashes}"
