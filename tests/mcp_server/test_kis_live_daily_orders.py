"""hk 3717 — ``kis_live_get_daily_orders``: read-only KIS KR orders for one KST date."""

from __future__ import annotations

import ast
import datetime as dt
import inspect
from pathlib import Path
from typing import Any, cast

import pytest

import app.mcp_server.tooling.kis_live_daily_orders as mod
from app.core.timezone import KST, today_kst
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.kis_live_daily_orders import (
    InferenceRungFact,
    LedgerFact,
    attach_system_status,
    dedupe_rows,
    derive_broker_status,
    kis_live_get_daily_orders_impl,
    normalize_row,
)
from app.mcp_server.tooling.orders_kis_variants import register_kis_live_order_tools
from app.mcp_server.tooling.route_request_lanes import (
    HARNESS_DENIED_TOOL_BASIS,
    HARNESS_DENIED_TOOLS,
)
from tests._mcp_tooling_support import DummyMCP
from tests.mcp_server._registration_recorder import collect_profile_tools

pytestmark = pytest.mark.unit

TOOL = "kis_live_get_daily_orders"
DAY = "2026-11-02"
DAY_DATE = dt.date(2026, 11, 2)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _row(odno: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "odno": odno,
        "orgn_odno": "",
        "sll_buy_dvsn_cd": "02",
        "sll_buy_dvsn_cd_name": "현금매수",
        "pdno": "005930",
        "prdt_name": "삼성전자",
        "ord_qty": "10",
        "ord_unpr": "70000",
        "tot_ccld_qty": "0",
        "tot_ccld_amt": "0",
        "rmn_qty": "10",
        "rjct_qty": "0",
        "cncl_yn": "",
        "excg_id_dvsn_cd": "SOR",
        "ord_dt": "20261102",
        "ord_tmd": "093000",
    }
    base.update(over)
    return base


class FakeKis:
    instances: list[FakeKis] = []

    def __init__(
        self, rows: list[dict[str, Any]] | None = None, exc: Exception | None = None
    ):
        self.rows = rows or []
        self.exc = exc
        self.calls: list[dict[str, Any]] = []
        FakeKis.instances.append(self)

    async def inquire_daily_order_domestic(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.rows


def _factory(client: FakeKis) -> Any:
    return lambda: client


def _loader(
    ledger: list[LedgerFact] | None = None,
    rungs: list[InferenceRungFact] | None = None,
) -> Any:
    async def load(_ids: list[str]) -> tuple[list[LedgerFact], list[InferenceRungFact]]:
        return ledger or [], rungs or []

    return load


def _sent(day: dt.date, hour: int = 9) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, 0, tzinfo=KST)


# --- broker call shape ------------------------------------------------------


@pytest.mark.asyncio
async def test_all_symbol_call_uses_blank_code_and_the_date_as_both_bounds() -> None:
    client = FakeKis([_row("1")])
    out = await kis_live_get_daily_orders_impl(
        trade_date=DAY, client_factory=_factory(client), system_facts_loader=_loader()
    )
    assert out["success"] is True
    (call,) = client.calls
    assert call["stock_code"] == ""
    assert call["start_date"] == call["end_date"] == "20261102"
    assert call["side"] == "00"
    assert call["max_pages"] == 100
    # Exhaustive reconcile defaults, not the ROB-903 display fast path.
    assert "stop_when_no_new_rows" not in call
    assert "inter_page_delay" not in call


@pytest.mark.parametrize(
    ("side", "code"), [("buy", "02"), ("sell", "01"), ("all", "00")]
)
@pytest.mark.asyncio
async def test_side_filter_maps_to_the_broker_code(side: str, code: str) -> None:
    client = FakeKis([])
    await kis_live_get_daily_orders_impl(
        trade_date=DAY,
        side=side,
        client_factory=_factory(client),
        system_facts_loader=_loader(),
    )
    assert client.calls[0]["side"] == code


# --- input validation -------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "2026-11-02/2026-11-03",
        "20261102",
        "2026-13-01",
        "2026-02-30",
        "",
        "2026-11-2",
        "２０２６-11-02",
    ],
)
@pytest.mark.asyncio
async def test_malformed_date_is_rejected_before_any_broker_call(bad: str) -> None:
    client = FakeKis([_row("1")])
    out = await kis_live_get_daily_orders_impl(
        trade_date=bad, client_factory=_factory(client), system_facts_loader=_loader()
    )
    assert out["success"] is False
    assert out["error_code"] == "invalid_input"
    assert client.calls == []


@pytest.mark.asyncio
async def test_future_date_is_rejected_before_any_broker_call() -> None:
    client = FakeKis([_row("1")])
    tomorrow = (today_kst() + dt.timedelta(days=1)).isoformat()
    out = await kis_live_get_daily_orders_impl(
        trade_date=tomorrow,
        client_factory=_factory(client),
        system_facts_loader=_loader(),
    )
    assert out["success"] is False
    assert out["error_code"] == "invalid_input"
    assert client.calls == []


@pytest.mark.asyncio
async def test_bad_side_is_rejected_before_any_broker_call() -> None:
    client = FakeKis([])
    out = await kis_live_get_daily_orders_impl(
        trade_date=DAY,
        side="both",
        client_factory=_factory(client),
        system_facts_loader=_loader(),
    )
    assert out["error_code"] == "invalid_input"
    assert client.calls == []


# --- rows -------------------------------------------------------------------


def test_duplicate_rows_across_pages_are_deduped() -> None:
    rows = [_row("1"), _row("2"), _row("1"), _row("2"), _row("3")]
    assert [r["odno"] for r in dedupe_rows(rows)] == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_duplicates_are_deduped_in_the_response() -> None:
    client = FakeKis([_row("1"), _row("1"), _row("2")])
    out = await kis_live_get_daily_orders_impl(
        trade_date=DAY, client_factory=_factory(client), system_facts_loader=_loader()
    )
    assert out["count"] == 2
    assert [o["order_id"] for o in out["orders"]] == ["1", "2"]


@pytest.mark.asyncio
async def test_max_pages_hit_reports_incomplete_with_page_count() -> None:
    client = FakeKis(
        exc=RuntimeError(
            "KIS domestic daily order history truncated at max_pages=100 "
            "for 20261102~20261102"
        )
    )
    out = await kis_live_get_daily_orders_impl(
        trade_date=DAY, client_factory=_factory(client), system_facts_loader=_loader()
    )
    assert out["success"] is True
    assert out["complete"] is False
    assert out["pages"] == 100
    assert out["error_class"] == "RuntimeError"
    assert out["orders"] == []


@pytest.mark.asyncio
async def test_complete_true_when_paging_ended_normally() -> None:
    client = FakeKis([_row("1")])
    out = await kis_live_get_daily_orders_impl(
        trade_date=DAY, client_factory=_factory(client), system_facts_loader=_loader()
    )
    assert out["complete"] is True


@pytest.mark.asyncio
async def test_broker_error_uses_closed_shape_without_the_raw_message() -> None:
    client = FakeKis(exc=RuntimeError("EGW00201 secret-ish raw broker text"))
    out = await kis_live_get_daily_orders_impl(
        trade_date=DAY, client_factory=_factory(client), system_facts_loader=_loader()
    )
    assert out["success"] is False
    assert out["error_class"] == "RuntimeError"
    assert out["error_code"] == "kis_daily_orders_lookup_failed"
    assert "secret-ish" not in repr(out)


# --- broker_status ----------------------------------------------------------


def _status(**over: Any) -> str:
    return normalize_row(_row("9", **over))["broker_status"]


def test_filled_requires_total_equal_to_order_quantity() -> None:
    assert _status(tot_ccld_qty="10", rmn_qty="0") == "filled"
    assert _status(tot_ccld_qty="9", rmn_qty="1") != "filled"
    assert _status(ord_qty="0", tot_ccld_qty="0", rmn_qty="0") == "unknown"
    assert _status(tot_ccld_qty="", rmn_qty="0") == "unknown"
    assert _status(tot_ccld_qty="11", rmn_qty="0") == "unknown"


def test_partial_working_and_rejected_eod_map_from_broker_fields() -> None:
    assert _status(tot_ccld_qty="4", rmn_qty="6") == "partially_filled"
    assert _status(tot_ccld_qty="0", rmn_qty="10") == "working"
    assert (
        _status(tot_ccld_qty="0", rmn_qty="0", rjct_qty="10")
        == "rejected_or_expired_eod"
    )
    assert _status(tot_ccld_qty="0", rmn_qty="0", rjct_qty="0") == "unknown"


def test_cancel_confirmation_row_is_its_own_row_linked_to_the_original() -> None:
    row = normalize_row(
        _row(
            "0000000777",
            orgn_odno="0000000555",
            sll_buy_dvsn_cd_name="매수취소",
            cncl_yn="Y",
            rmn_qty="0",
        )
    )
    assert row["is_cancel_confirmation"] is True
    assert row["original_order_id"] == "0000000555"
    assert row["order_id"] == "0000000777"
    assert row["broker_status"] == "cancelled"
    # A normal row has no link (zero padding alone is not a link).
    plain = normalize_row(_row("1", orgn_odno="0000000000"))
    assert plain["is_cancel_confirmation"] is False
    assert plain["original_order_id"] is None


def test_modification_confirmation_row_is_not_reported_cancelled() -> None:
    # KIS links modification confirmations through orgn_odno too.
    modify = normalize_row(
        _row(
            "0000000888",
            orgn_odno="0000000555",
            sll_buy_dvsn_cd_name="매수정정",
            rmn_qty="10",
        )
    )
    assert modify["original_order_id"] == "0000000555"
    assert modify["is_cancel_confirmation"] is False
    assert modify["cancelled"] is False
    assert modify["broker_status"] == "working"
    filled = normalize_row(
        _row(
            "0000000889",
            orgn_odno="0000000555",
            sll_buy_dvsn_cd_name="매수정정",
            tot_ccld_qty="10",
            rmn_qty="0",
        )
    )
    assert filled["broker_status"] == "filled"


def test_row_fields_and_basis() -> None:
    row = normalize_row(
        _row("1", tot_ccld_qty="10", tot_ccld_amt="700000", rmn_qty="0")
    )
    assert row["symbol"] == "005930"
    assert row["name"] == "삼성전자"
    assert row["side"] == "buy"
    assert row["order_qty"] == 10
    assert row["order_price"] == 70000
    assert row["filled_qty"] == 10
    assert row["filled_amount"] == 700000
    assert row["remaining_qty"] == 0
    assert row["rejected_qty"] == 0
    assert row["exchange"] == "SOR"
    assert (row["order_date"], row["order_time"]) == ("20261102", "093000")
    assert row["broker_status_basis"] == "broker_fields"
    assert "filled_at" not in row and "fill_time" not in row
    assert normalize_row(_row("2", sll_buy_dvsn_cd="01"))["side"] == "sell"


def test_derive_broker_status_is_pure_on_decimals() -> None:
    from decimal import Decimal as D

    assert (
        derive_broker_status(
            ord_qty=D(5),
            tot_ccld_qty=D(5),
            rmn_qty=D(0),
            rjct_qty=D(0),
            cancelled=False,
            is_cancel_confirmation=False,
        )
        == "filled"
    )


# --- system_status join -----------------------------------------------------


def _ledger(day: dt.date = DAY_DATE, **over: Any) -> LedgerFact:
    return LedgerFact(
        "0000000123",
        "accepted",
        "accepted",
        _sent(day),
        over.get("idempotency_key", "idem-1"),
        over.get("correlation_id", "corr-1"),
    )


def _rung(**over: Any) -> InferenceRungFact:
    return InferenceRungFact(
        over.get("broker_order_id", "123"),
        over.get("idempotency_key", "idem-1"),
        over.get("correlation_id", "corr-1"),
    )


def _join(
    ledger: list[LedgerFact], rungs: list[InferenceRungFact] | None = None
) -> dict[str, Any]:
    row = normalize_row(_row("0000000123"))
    attach_system_status(
        [row], trade_date=DAY_DATE, ledger=ledger, inference_rungs=rungs or []
    )
    return row


def test_ledger_row_on_the_same_date_attaches_with_ledger_basis() -> None:
    row = _join([_ledger()])
    assert row["system_status"] == {"status": "accepted", "lifecycle_state": "accepted"}
    assert row["system_status_basis"] == "ledger"


def test_no_ledger_row_means_null_status_and_basis() -> None:
    row = _join([])
    assert row["system_status"] is None
    assert row["system_status_basis"] is None


def test_inference_marked_rung_makes_the_basis_inference() -> None:
    # Leading-zero normalization: rung stores "123", broker/ledger "0000000123".
    row = _join([_ledger()], [_rung()])
    assert row["system_status_basis"] == "inference"
    assert row["system_status"] is not None
    assert (
        _join([_ledger()], [_rung(idempotency_key=None)])["system_status_basis"]
        == "inference"
    )  # tied by correlation id alone


def test_marker_rung_of_a_different_order_number_is_not_inference() -> None:
    row = _join([_ledger()], [_rung(broker_order_id="124")])
    assert row["system_status_basis"] == "ledger"


def test_marker_rung_of_a_reused_number_from_another_order_is_not_inference() -> None:
    row = _join(
        [_ledger()],
        [_rung(idempotency_key="idem-old", correlation_id="corr-old")],
    )
    assert row["system_status_basis"] == "ledger"


def test_same_order_number_with_the_marker_on_a_different_date_is_not_inference() -> (
    None
):
    other_day = DAY_DATE - dt.timedelta(days=1)
    row = _join([_ledger(day=other_day)], [_rung()])
    assert row["system_status"] is None
    assert row["system_status_basis"] is None


def test_same_order_number_on_a_different_date_is_not_joined() -> None:
    other_day = DAY_DATE - dt.timedelta(days=1)
    row = _join([_ledger(day=other_day)])
    assert row["system_status"] is None
    assert row["system_status_basis"] is None


def test_kst_date_boundary_uses_kst_not_utc() -> None:
    # 00:30 KST on DAY is the previous day in UTC; it still belongs to DAY.
    sent = dt.datetime(2026, 11, 2, 0, 30, tzinfo=KST).astimezone(dt.UTC)
    row = _join([LedgerFact("0000000123", "accepted", "accepted", sent)])
    assert row["system_status"] is not None


@pytest.mark.asyncio
async def test_ledger_read_failure_leaves_system_status_null_without_failing() -> None:
    async def boom(_ids: list[str]) -> Any:
        raise ConnectionError("db down")

    client = FakeKis([_row("1")])
    out = await kis_live_get_daily_orders_impl(
        trade_date=DAY, client_factory=_factory(client), system_facts_loader=boom
    )
    assert out["success"] is True
    (row,) = out["orders"]
    assert row["system_status"] is None
    assert row["system_status_error"] == "ConnectionError"
    assert out["system_status_error"] == "ConnectionError"
    assert row["broker_status"] == "working"


@pytest.mark.asyncio
async def test_end_to_end_attaches_ledger_status() -> None:
    client = FakeKis([_row("0000000123")])
    out = await kis_live_get_daily_orders_impl(
        trade_date=DAY,
        client_factory=_factory(client),
        system_facts_loader=_loader(
            [LedgerFact("0000000123", "expired", "cancelled", _sent(DAY_DATE))]
        ),
    )
    assert out["orders"][0]["system_status"] == {
        "status": "expired",
        "lifecycle_state": "cancelled",
    }


# --- registration / permissions --------------------------------------------


def test_registered_only_in_the_default_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = collect_profile_tools(monkeypatch, gates_enabled=True)
    holders = {name for name, tools in profiles.items() if TOOL in tools}
    assert holders == {McpProfile.DEFAULT.value}


def test_harness_denied_under_the_678_basis() -> None:
    assert TOOL in HARNESS_DENIED_TOOLS
    assert HARNESS_DENIED_TOOL_BASIS == "live_session_harness_denied_678"
    assert "kis_live_get_order_history" in HARNESS_DENIED_TOOLS


def test_no_lane_allowlist_names_the_tool() -> None:
    for path in (REPO_ROOT / "config/mcp_lane_allowlists").glob("*.txt"):
        names = {
            line.split("\t")[0]
            for line in path.read_text().splitlines()
            if line and not line.startswith("#")
        }
        assert TOOL not in names, path.name


@pytest.mark.asyncio
async def test_registered_tool_delegates_with_kis_live_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeKis([_row("1")])
    monkeypatch.setattr(mod, "KISClient", lambda: client)

    async def no_facts(_ids: list[str]) -> tuple[list[Any], list[Any]]:
        return [], []

    monkeypatch.setattr(mod, "_load_system_facts", no_facts)
    mcp = DummyMCP()
    register_kis_live_order_tools(cast(Any, mcp))
    out = await mcp.tools[TOOL](trade_date=DAY, side="buy")
    assert out["account_mode"] == "kis_live"
    assert out["count"] == 1
    assert client.calls[0]["side"] == "02"


def test_order_history_tool_signature_is_unchanged() -> None:
    mcp = DummyMCP()
    register_kis_live_order_tools(cast(Any, mcp))
    params = inspect.signature(mcp.tools["kis_live_get_order_history"]).parameters
    assert list(params) == [
        "symbol",
        "status",
        "order_id",
        "market",
        "side",
        "days",
        "limit",
        "account_mode",
        "account_type",
    ]
    assert params["limit"].default == 50 and params["days"].default is None


# --- read-only import boundary ---------------------------------------------

_FORBIDDEN = (
    "place_order",
    "modify_order",
    "cancel_order",
    "order_execution",
    "orders_modify_cancel",
    "kis_live_ledger",
    "KISLiveOrderLedgerService",
    "OrderProposalsService",
    "pg_insert",
    "insert",
    "update",
    "delete",
)


def test_module_imports_no_order_mutation_or_ledger_writer() -> None:
    tree = ast.parse(Path(mod.__file__).read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    for name in imported:
        for bad in _FORBIDDEN:
            assert bad not in name, f"{name!r} pulls in {bad!r}"
    called = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert not called & {"add_all", "delete", "commit", "flush", "merge", "rollback"}
