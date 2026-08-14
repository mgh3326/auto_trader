"""End-to-end kis_mock cycle behaviour — no network, no venue, no live KIS call.

``_FakeKrClient`` stands in for ``ReadOnlyKISMockDomesticClient``; every test
here passes it explicitly via ``client=`` so ``run_kr_cycle`` never
constructs a real one.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts.b0x import contract as common_contract
from scripts.b0x.kr import cycle as kr_cycle
from scripts.b0x.kr import kiwoom_cycle
from scripts.b0x.kr.cycle import (
    KILL_TRIPPED_CANCEL_UNSUPPORTED,
    KR_ACCOUNT_MAP_COMMIT,
    KR_CONTRACT_VERSION,
    KR_STATUS_LABEL,
    MANUAL_CONFIRM_SUBMISSION_LIMIT,
    OUTSIDE_RTH_REASON,
    run_kr_cycle,
)
from scripts.b0x.kr.cycle import LANE as KR_LANE
from scripts.b0x.labels import SHARED_ACCOUNT_HISTORY, TRUST_LABELS
from tests.scripts.b0x._table_fixtures import (
    make_payload,
    make_row,
    write_stale_marker,
    write_table,
)
from tests.scripts.b0x.kr._attribution import (
    no_attribution,
    unreadable_attribution,
)
from tests.scripts.b0x.kr._pending import (
    exploding_pending,
    foreign_traces,
    readable_pending,
    unreadable_pending,
)

pytestmark = pytest.mark.unit

# 2026-08-10 is a Monday; 02:00 UTC = 11:00 KST, inside the XKRX regular
# session (verified against exchange_calendars directly while writing this).
IN_SESSION_NOW = dt.datetime(2026, 8, 10, 2, 0, tzinfo=dt.UTC)
# 2026-08-08 is a Saturday — outside any session, regardless of time of day.
WEEKEND_NOW = dt.datetime(2026, 8, 8, 2, 0, tzinfo=dt.UTC)


def test_kr_contracts_keep_their_lane_clauses_at_current_versions() -> None:
    scheduler_clause = common_contract.CONTRACT_CLAUSES["§8 v1.7"]

    assert kr_cycle.KR_CONTRACT_VERSION == "v1.7"
    assert kr_cycle.KR_CONTRACT_CLAUSES["§8 v1.7"] == scheduler_clause
    assert "§8 v1.6" in kr_cycle.KR_CONTRACT_CLAUSES

    # v1.8 = the kiwoom-lane ladder batch-submit amendment (§69차); it layers
    # on top of the v1.7 scheduler clause and the v1.6 lane clauses, so all
    # three must remain present.
    assert kiwoom_cycle.KR_CONTRACT_VERSION == "v1.8"
    assert "§8 v1.8 (v1.5 ① KR amendment)" in kiwoom_cycle.KR_CONTRACT_CLAUSES
    assert kiwoom_cycle.KR_CONTRACT_CLAUSES["§8 v1.7"] == scheduler_clause
    assert "§8 v1.6" in kiwoom_cycle.KR_CONTRACT_CLAUSES
    assert "§39차 ①②③④⑤" in kiwoom_cycle.KR_CONTRACT_CLAUSES


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
        # Faithful to AccountClient: the broker's own row is echoed under
        # ``raw``, which is what distinguishes an absent cash field from a
        # zero balance.
        self._cash["raw"] = {
            "dnca_tot_amt": str(orderable_cash),
            "stck_cash_ord_psbl_amt": str(orderable_cash),
        }
        self._stocks = stocks or []
        self.closed = False

    async def inquire_cash_balance(self) -> dict[str, Any]:
        return self._cash

    async def fetch_my_stocks(self) -> list[dict[str, Any]]:
        return self._stocks

    async def close(self) -> None:
        self.closed = True


#: Contract v1.6 ① made 자기 미체결 readable from the submission ledger, so the
#: ordinary case for a lane that has not traded today is a readable **empty**
#: answer. Tests that want the fail-closed tri-state ask for it explicitly with
#: ``unreadable_pending()`` / ``exploding_pending(...)`` — the substitution is
#: never implicit, and (per ``conftest.py``) never accidental.
EMPTY_PENDING = readable_pending()

#: 🔴 §36차 2항 기본값 — 「원장이 답했고 이 레인 소유는 없다」. 계좌 보유가
#: 있어도 그것은 legacy 이며, 매도/물타기 후보가 되어서는 안 된다. 자기 귀속을
#: 증명해야 하는 테스트는 ``attributed(...)`` 를 명시적으로 주입한다.
NO_ATTRIBUTION = no_attribution()


@pytest.mark.asyncio
async def test_outside_regular_session_derives_zero_orders(
    table_dir: Path, out_dir: Path
) -> None:
    outcome = await run_kr_cycle(
        now=WEEKEND_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        client=_FakeKrClient(),
        attribution_reader=NO_ATTRIBUTION,
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
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        client=_FakeKrClient(),
        attribution_reader=NO_ATTRIBUTION,
    )
    assert outcome.zero_order_reason == "stale_marker_present"
    assert outcome.order_count == 0


@pytest.mark.asyncio
async def test_table_older_than_36h_derives_zero_orders_with_reason(
    tmp_path: Path, out_dir: Path
) -> None:
    """Contract v1.1 §2-2: MAX_TABLE_AGE[kr] = 36h — not the pre-amendment 8h.

    A table generated 37h before ``now`` must zero the cycle out with
    ``stale_by_age`` and a detail string, not silently replay it.
    """

    directory = tmp_path / "policy-tables"
    write_table(
        directory,
        make_payload(
            market="kr",
            rows=[make_row(symbol="005930", previous_close="97000", buy_l1="94090")],
            generated_at=IN_SESSION_NOW - dt.timedelta(hours=37),
        ),
        market="kr",
    )
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=directory,
        out_dir=out_dir,
        client=_FakeKrClient(),
        attribution_reader=NO_ATTRIBUTION,
    )
    assert outcome.zero_order_reason == "stale_by_age"
    assert outcome.order_count == 0
    # timedelta(hours=36) stringifies as "1 day, 12:00:00", not "36:00:00".
    assert "max_age=1 day, 12:00:00" in outcome.record["zero_order_detail"]


@pytest.mark.asyncio
async def test_table_just_under_36h_still_derives_orders(
    tmp_path: Path, out_dir: Path
) -> None:
    """Boundary check the other side: 35h59m must NOT be zeroed by age."""

    directory = tmp_path / "policy-tables"
    write_table(
        directory,
        make_payload(
            market="kr",
            rows=[make_row(symbol="005930", previous_close="97000", buy_l1="94090")],
            generated_at=IN_SESSION_NOW - dt.timedelta(hours=35, minutes=59),
        ),
        market="kr",
    )
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=directory,
        out_dir=out_dir,
        client=_FakeKrClient(orderable_cash="5000000"),
        pending_reader=EMPTY_PENDING,
        attribution_reader=NO_ATTRIBUTION,
    )
    assert outcome.zero_order_reason is None
    # Under contract v1.6 ① a lane that has not traded today reads a readable
    # empty pending book, so this really does derive — before v1.6 the
    # unreadable fail-close stopped every row here regardless of table age,
    # which made the boundary check vacuous. A 37h table would instead have set
    # zero_order_reason=stale_by_age above.
    assert outcome.order_count > 0
    assert outcome.record["broker_truth"]["own_pending_readable"] is True


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
        pending_reader=EMPTY_PENDING,
        attribution_reader=NO_ATTRIBUTION,
    )
    assert outcome.zero_order_reason is None
    assert outcome.record["planned"], "a readable-empty book must still plan"
    assert outcome.record["submitted"] == []
    assert outcome.record["submission_skipped"] == "confirm=False — preview only"
    # A caller-supplied client is caller-owned: the cycle must not close it.
    assert client.closed is False

    # 3/3 fixed trust labels, in order, are present in the rendered artifact.
    artifact_text = outcome.artifact_path.read_text(encoding="utf-8")
    for label in TRUST_LABELS:
        assert label in artifact_text
    assert KR_STATUS_LABEL in outcome.record["labels"]
    assert KR_STATUS_LABEL in artifact_text
    assert SHARED_ACCOUNT_HISTORY not in outcome.record["labels"]
    assert "SHARED_ACCOUNT_HISTORY" not in artifact_text
    assert outcome.record["cycle_status"] == "OBSERVATION_DERIVATION_ONLY"
    assert outcome.record["contract"]["version"] == KR_CONTRACT_VERSION
    assert outcome.record["account_map"]["commit"] == KR_ACCOUNT_MAP_COMMIT
    assert outcome.record["account_map"]["gate_values"] == {
        "account_lanes.kis_mock": "B0-X-KR",
        "exclusive_lane": "B0-X-KR",
        "active_ordering_strategy": "B0-X-adapter-single-writer",
        "surface": "kis_mock",
    }


@pytest.mark.asyncio
async def test_kr_pending_is_unreadable_and_fails_closed(
    table_dir: Path, out_dir: Path
) -> None:
    """계약 v1.5 ① / v1.6 ④ — 「조회 불가」 is still not 「미체결 없음」.

    v1.6 ① gave this lane a *source* for 자기 미체결; it did not delete the
    unreadable state. When the source cannot answer — this test drives the
    kis_mock broker-surface sentinel directly — every candidate row is refused
    with ``own_pending_unreadable``, the refusal carries *why*, and the record
    says plainly that pending is unreadable rather than reporting an empty
    book. ``test_a_failed_ledger_read_falls_back_to_unreadable`` covers the
    other way into this state (the ledger query itself faulting).
    """

    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=_FakeKrClient(orderable_cash="5000000"),
        pending_reader=unreadable_pending(),
        attribution_reader=NO_ATTRIBUTION,
    )

    assert outcome.record["broker_truth"]["own_pending_readable"] is False
    assert (
        outcome.record["broker_truth"]["own_pending"]["reason"]
        == "kis_mock_pending_inquiry_unsupported"
    )
    assert outcome.record["fresh_truth"]["own_pending_readable"] is False

    # Zero orders, zero plans — and the whole row refused, not one leg.
    assert outcome.order_count == 0
    assert outcome.record["orders"] == []
    assert outcome.record["planned"] == []
    skipped = outcome.record["skipped"]
    assert {skip["symbol"] for skip in skipped} == {"005930", "000660"}
    assert all(skip["reason"] == "own_pending_unreadable" for skip in skipped)
    assert all(skip["leg"] == "*" for skip in skipped)
    assert all("TTTC8036R" in skip["detail"] for skip in skipped)


@pytest.mark.asyncio
async def test_a_failed_ledger_read_falls_back_to_unreadable(
    table_dir: Path, out_dir: Path
) -> None:
    """계약 v1.6 ④ — 원장 조회 실패 → 다시 unreadable → 전 심볼 차단.

    The v1.6 exception is conditional on the ledger *answering*. A fault must
    not degrade to "nothing is resting"; it must land back on exactly the
    tri-state X-E1 built, refusing every symbol.
    """

    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=_FakeKrClient(orderable_cash="5000000"),
        pending_reader=exploding_pending(OSError("connection refused")),
        attribution_reader=NO_ATTRIBUTION,
    )

    truth = outcome.record["broker_truth"]
    assert truth["own_pending_readable"] is False
    assert truth["own_pending"]["reason"] == "kis_mock_ledger_pending_unreadable"
    # The record must say the *ledger* failed, not merely repeat the broker
    # surface fact — and it must not leak the exception message (a DB error can
    # carry a DSN with credentials), only its type name.
    assert "OSError" in truth["own_pending"]["detail"]
    assert "connection refused" not in truth["own_pending"]["detail"]

    assert outcome.order_count == 0
    assert outcome.record["planned"] == []
    skipped = outcome.record["skipped"]
    assert {skip["symbol"] for skip in skipped} == {"005930", "000660"}
    assert all(skip["reason"] == "own_pending_unreadable" for skip in skipped)


@pytest.mark.asyncio
async def test_kr_records_that_realized_pnl_has_no_source(
    table_dir: Path, out_dir: Path
) -> None:
    """A structural absence must not read as a measured zero (v1.5 ①)."""

    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=_FakeKrClient(orderable_cash="5000000"),
        pending_reader=EMPTY_PENDING,
        attribution_reader=NO_ATTRIBUTION,
    )
    assert "Not a measured zero" in outcome.record["realized_pnl_source"]
    assert outcome.derivation is not None
    assert outcome.derivation.kill_switch.tripped is False


@pytest.mark.asyncio
async def test_kr_dry_run_reacts_when_shared_history_scope_expands(
    table_dir: Path, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KR wiring must become observable if an operator expands the scope."""

    from scripts.b0x import labels as labels_module

    monkeypatch.setattr(
        labels_module,
        "SHARED_HISTORY_ACCOUNTS",
        labels_module.SHARED_HISTORY_ACCOUNTS | {KR_LANE},
    )
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=_FakeKrClient(orderable_cash="5000000"),
        pending_reader=EMPTY_PENDING,
        attribution_reader=NO_ATTRIBUTION,
    )

    assert SHARED_ACCOUNT_HISTORY in outcome.record["labels"]
    assert outcome.artifact_path is not None
    assert f"> {SHARED_ACCOUNT_HISTORY}" in outcome.artifact_path.read_text(
        encoding="utf-8"
    )


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
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        pending_reader=EMPTY_PENDING,
        attribution_reader=NO_ATTRIBUTION,
    )
    assert outcome.zero_order_reason is None
    assert fake.closed is True


class _FakeBroker:
    """Stands in for ``KisMockBroker`` — no HTTP, no reservation DB write.

    Records every call it received so a test can assert the cycle threaded
    the right symbol/side/price/quantity/correlation_id through, without
    touching a real kis_mock account (contract §7: 실주문 0 this round).
    """

    def __init__(self) -> None:
        self.buy_calls: list[dict[str, Any]] = []
        self.sell_calls: list[dict[str, Any]] = []

    async def submit_buy(self, **kwargs: Any) -> dict[str, Any]:
        self.buy_calls.append(kwargs)
        return {"success": True, "odno": f"FAKE-BUY-{len(self.buy_calls)}"}

    async def submit_exit_sell(self, **kwargs: Any) -> dict[str, Any]:
        self.sell_calls.append(kwargs)
        return {"success": True, "odno": f"FAKE-SELL-{len(self.sell_calls)}"}


@pytest.mark.asyncio
async def test_confirm_true_dispatches_nothing_while_pending_is_unreadable(
    table_dir: Path, out_dir: Path, armed_confirm: list[str]
) -> None:
    """The v1.5 ① fail-close reaches the dispatch path, not just derivation."""

    broker = _FakeBroker()
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        client=_FakeKrClient(orderable_cash="5000000"),
        broker=broker,
        pending_reader=unreadable_pending(),
        foreign_trace_reader=foreign_traces(),
        attribution_reader=NO_ATTRIBUTION,
    )
    assert outcome.zero_order_reason == "preflight_not_clean"
    assert outcome.record["preflight"]["passed"] is False
    assert "ledger_pending_unreadable" in outcome.record["preflight"]["reasons"]
    assert outcome.record["planned"] == []
    assert outcome.record["submitted"] == []
    assert broker.buy_calls == [] and broker.sell_calls == []
    assert armed_confirm == ["acquired", "released"]


@pytest.mark.asyncio
async def test_confirm_true_routes_through_the_injected_broker_when_pending_reads(
    table_dir: Path, out_dir: Path, armed_confirm: list[str]
) -> None:
    """``confirm=True`` routes every planned order through the wired
    ``KisMockBroker`` integration point (contract v1.3 ③) — proven here with
    a fake broker so the assertion never touches a real kis_mock account.

    Before v1.6 this test had to reach past ``broker_state`` to fake a readable
    book, because the lane had no readable source at all. It now uses the real
    ``pending_reader`` seam with an empty ledger — the same code path
    production takes on a day this lane has not yet traded.
    ``unwired_submit_order``/``KrMockSubmissionNotWired`` is proven still
    intact (kept, not deleted) by ``test_unwired_submit_order_always_raises``
    in ``tests/scripts/b0x/kr/test_mock.py``.
    """

    client = _FakeKrClient(orderable_cash="5000000")
    broker = _FakeBroker()
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        client=client,
        broker=broker,
        pending_reader=EMPTY_PENDING,
        foreign_trace_reader=foreign_traces(),
        attribution_reader=NO_ATTRIBUTION,
    )

    assert outcome.zero_order_reason is None
    assert outcome.record["preflight"]["passed"] is True
    assert outcome.record["preflight"]["reasons"] == []
    assert outcome.record["preflight"]["account"] == {
        "fingerprint": "sha256:test-account",
        "product_suffix": "01",
    }
    assert outcome.record["preflight"]["cash"] == {"present": True}
    assert outcome.record["preflight"]["positions"] == {
        "non_dust_symbols": [],
        "count": 0,
        "own_attributed_symbols": [],
        "own_attributed_count": 0,
        "legacy_symbols": [],
        "legacy_count": 0,
    }
    assert (
        outcome.record["preflight"]["open_orders"]["native_broker"]["available"]
        is False
    )
    assert outcome.record["preflight"]["writer_lease"] == {
        "acquired": True,
        "surface": "b0x_adapter",
    }
    planned = outcome.record["planned"]
    assert planned, "fixture table must derive at least one planned order"
    assert len(broker.buy_calls) == MANUAL_CONFIRM_SUBMISSION_LIMIT
    assert not broker.sell_calls  # flat account, fixture table has no sells
    submitted = outcome.record["submitted"]
    assert len(submitted) == MANUAL_CONFIRM_SUBMISSION_LIMIT
    assert all(row.get("success") is True for row in submitted)
    for order, call in zip(
        planned[:MANUAL_CONFIRM_SUBMISSION_LIMIT], broker.buy_calls, strict=True
    ):
        assert call["symbol"] == order["symbol"]
        assert call["correlation_id"] == order["client_order_id"]
        assert call["confirm"] is True
    assert outcome.record["submission_stopped"] == "post_submit_dedup_unproven"
    assert outcome.record["post_submit_dedup"] == [
        {
            "symbol": planned[0]["symbol"],
            "correlation_id": planned[0]["client_order_id"],
            "observed": False,
        }
    ]
    assert armed_confirm == ["acquired", "released"]


@pytest.mark.asyncio
async def test_confirm_preflight_contamination_stops_zero_orders(
    table_dir: Path, out_dir: Path, armed_confirm: list[str]
) -> None:
    """NW-B4: any other writer's trace is contamination, never a cleanup cue."""

    broker = _FakeBroker()
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        client=_FakeKrClient(orderable_cash="5000000"),
        broker=broker,
        pending_reader=EMPTY_PENDING,
        foreign_trace_reader=foreign_traces(
            "005930", order_trace_count=1, signal_trace_count=1
        ),
        attribution_reader=NO_ATTRIBUTION,
    )

    assert outcome.zero_order_reason == "preflight_not_clean"
    assert outcome.record["preflight"]["passed"] is False
    assert (
        "CONTAMINATED_foreign_correlation_trace"
        in outcome.record["preflight"]["reasons"]
    )
    assert outcome.record["submitted"] == []
    assert broker.buy_calls == [] and broker.sell_calls == []
    assert armed_confirm == ["acquired", "released"]


@pytest.mark.asyncio
async def test_confirm_rechecks_v1_6_dedup_at_the_mutation_boundary(
    table_dir: Path, out_dir: Path, armed_confirm: list[str]
) -> None:
    """A trace that appears after preflight still blocks before broker submit."""

    reads = 0

    async def _pending_after_preflight(
        *, now: dt.datetime, correlation_prefix: str
    ) -> tuple[str, ...]:
        del now
        assert correlation_prefix == "b0xk-"
        nonlocal reads
        reads += 1
        return () if reads == 1 else ("000660", "005930")

    broker = _FakeBroker()
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        client=_FakeKrClient(orderable_cash="5000000"),
        broker=broker,
        pending_reader=_pending_after_preflight,
        foreign_trace_reader=foreign_traces(),
        attribution_reader=NO_ATTRIBUTION,
    )

    assert outcome.zero_order_reason is None
    assert outcome.record["preflight"]["passed"] is True
    assert outcome.record["submitted"] == []
    assert outcome.record["submission_stopped"] == "v1_6_dedup_blocked"
    assert outcome.record["submission_dedup_blocked"] == [
        {
            "symbol": outcome.record["planned"][0]["symbol"],
            "correlation_id": outcome.record["planned"][0]["client_order_id"],
            "reason": "v1_6_pending_recheck_blocked",
        }
    ]
    assert broker.buy_calls == [] and broker.sell_calls == []
    assert reads == 2
    assert armed_confirm == ["acquired", "released"]


@pytest.mark.asyncio
async def test_legacy_holdings_do_not_block_confirm_preflight(
    table_dir: Path, out_dir: Path, armed_confirm: list[str]
) -> None:
    """🔴 §36차 2항 (mutant ④) — flat 요구는 사라졌고 귀속 게이트가 대신한다.

    이 계좌에는 B0-X 가 만들지 않은 legacy 보유가 있다. 예전 게이트는
    ``unexpected_positions`` 로 **영구히** 제출을 막았고, 그 상태로는 계약
    v1.5 ③ 의 매도 파생도 도달 불가였다. 이제 legacy 는 이름이 적히고 공존하되,
    파생 입력에서는 빠진다.
    """

    broker = _FakeBroker()
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        client=_FakeKrClient(
            orderable_cash="5000000",
            stocks=[
                {
                    "pdno": "005930",
                    "hldg_qty": "1",
                    "pchs_avg_pric": "70000",
                    "evlu_amt": "70000",
                }
            ],
        ),
        broker=broker,
        pending_reader=EMPTY_PENDING,
        foreign_trace_reader=foreign_traces(),
        attribution_reader=NO_ATTRIBUTION,
    )

    preflight = outcome.record["preflight"]
    assert preflight["reasons"] == []
    assert preflight["passed"] is True
    assert outcome.zero_order_reason != "preflight_not_clean"
    # 🔴 조용히 지워지지 않는다: 계좌 진실과 귀속 분리가 나란히 기록된다.
    assert preflight["positions"] == {
        "non_dust_symbols": ["005930"],
        "count": 1,
        "own_attributed_symbols": [],
        "own_attributed_count": 0,
        "legacy_symbols": ["005930"],
        "legacy_count": 1,
    }
    assert preflight["attribution"]["readable"] is True
    assert preflight["attribution"]["cap_basis"] == "attributed_own_positions"
    assert armed_confirm == ["acquired", "released"]
    # 🔴 그리고 legacy 는 매도 후보가 되지 않는다 (mutant ①).
    assert [order["symbol"] for order in outcome.record["orders"]] == [
        order["symbol"] for order in outcome.record["orders"] if order["side"] == "buy"
    ]
    assert broker.sell_calls == []


@pytest.mark.asyncio
async def test_confirm_preflight_stops_when_attribution_is_unreadable(
    table_dir: Path, out_dir: Path, armed_confirm: list[str]
) -> None:
    """🔴 「보유가 있다」가 아니라 「누구 것인지 모른다」가 fail-closed 사유다."""

    broker = _FakeBroker()
    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=True,
        client=_FakeKrClient(
            orderable_cash="5000000",
            stocks=[
                {
                    "pdno": "005930",
                    "hldg_qty": "1",
                    "pchs_avg_pric": "70000",
                    "evlu_amt": "70000",
                }
            ],
        ),
        broker=broker,
        pending_reader=EMPTY_PENDING,
        foreign_trace_reader=foreign_traces(),
        attribution_reader=unreadable_attribution(),
    )

    assert outcome.zero_order_reason == "preflight_not_clean"
    assert outcome.record["preflight"]["reasons"] == ["attribution_unreadable"]
    assert outcome.record["preflight"]["attribution"]["readable"] is False
    assert outcome.record["submitted"] == []
    assert broker.buy_calls == [] and broker.sell_calls == []
    assert armed_confirm == ["acquired", "released"]


@pytest.mark.asyncio
async def test_kill_switch_trips_on_nav_ratio_and_blocks_all_new_orders(
    table_dir: Path, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-cycle NAV of 1,000,000 with a -30,000 realized loss is a 3%
    drawdown — over the KR envelope's 2.5% NAV kill.

    The loss used to be read from ``attributed_book.json``; contract v1.5 ①
    deleted that path and left the KR lane with no realized-P&L source at all
    (``test_kr_records_that_realized_pnl_has_no_source``). The kill *wiring*
    still has to work for the day one exists, so the loss is injected at the
    state boundary here rather than through a file the lane no longer reads.
    """

    from scripts.b0x.kr import cycle as kr_cycle_module

    real_broker_state = kr_cycle_module.broker_state

    def _with_loss(**kwargs: Any):
        state = real_broker_state(**kwargs)
        return replace(state, realized_pnl_today=Decimal("-30000"))

    monkeypatch.setattr(kr_cycle_module, "broker_state", _with_loss)

    outcome = await run_kr_cycle(
        now=IN_SESSION_NOW,
        table_dir=table_dir,
        out_dir=out_dir,
        confirm=False,
        client=_FakeKrClient(orderable_cash="1000000"),
        pending_reader=EMPTY_PENDING,
        attribution_reader=NO_ATTRIBUTION,
    )
    assert outcome.derivation is not None
    assert outcome.derivation.kill_switch.tripped
    assert outcome.record["planned"] == []
    assert outcome.record["submitted"] == []
    assert outcome.record["cancelled"] == []
    assert outcome.record["cancel_status"] == KILL_TRIPPED_CANCEL_UNSUPPORTED
    assert outcome.record["cancel_attempted"] is False
    assert outcome.record["cancel_confirmed"] is False
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
            pending_reader=EMPTY_PENDING,
            attribution_reader=NO_ATTRIBUTION,
        )
        assert outcome.derivation is not None
        hashes.append(outcome.derivation.derivation_hash())
    assert len(set(hashes)) == 1, f"derivation hash diverged across runs: {hashes}"
