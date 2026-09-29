"""Task #963 — get_holdings(include_ledger_lots) opt-in behavior.

* default output (flag omitted or False) is byte-identical to the pre-change
  implementation (golden JSON generated before the parameter existed);
* the opt-in only ADDS ``ledger_lots`` keys, and only on KIS live KR positions;
* the block makes no broker call and can never fail get_holdings.
"""

from __future__ import annotations

import inspect
import json
from decimal import Decimal
from typing import Any

import pytest

from app.mcp_server.tooling import portfolio_holdings, portfolio_ledger_lots
from app.services.execution_ledger import kis_lots
from tests.mcp_server.get_holdings_golden_support import (
    GOLDEN_PATH,
    call_get_holdings,
    canonical_json,
    install_fake_collect,
)

pytestmark = pytest.mark.unit

CALL: dict[str, Any] = {"include_current_price": False, "minimum_value": 0}


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _install_fake_loader(
    monkeypatch: pytest.MonkeyPatch, *, seen: list[Any] | None = None
) -> None:
    async def fake_loader(db: Any, refs: Any, **_kwargs: Any) -> dict[str, Any]:
        if seen is not None:
            seen.extend(refs)
        return {
            ref.symbol: {"ledger_state": "known", "marker": f"blk-{ref.symbol}"}
            for ref in refs
        }

    monkeypatch.setattr(portfolio_ledger_lots, "AsyncSessionLocal", _FakeSession)
    monkeypatch.setattr(
        portfolio_ledger_lots, "load_kis_live_kr_lot_blocks", fake_loader
    )


def _strip_ledger_lots(payload: dict[str, Any]) -> dict[str, Any]:
    stripped = json.loads(json.dumps(payload))
    stripped.pop("ledger_lots", None)
    for group in stripped["accounts"]:
        for position in group["positions"]:
            position.pop("ledger_lots", None)
    return stripped


def _positions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for g in payload["accounts"] for p in g["positions"]]


# ---------------------------------------------------------------------------
# default output is byte-identical (golden)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_default_output_is_byte_identical_to_pre_change_golden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    golden = GOLDEN_PATH.read_text(encoding="utf-8")
    install_fake_collect(monkeypatch)
    omitted = canonical_json(await call_get_holdings(**CALL))
    install_fake_collect(monkeypatch)
    explicit_false = canonical_json(
        await call_get_holdings(**CALL, include_ledger_lots=False)
    )
    assert omitted == golden
    assert explicit_false == golden
    assert "ledger_lots" not in golden


@pytest.mark.asyncio
async def test_default_path_never_touches_the_ledger_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("ledger block must not run without the opt-in flag")

    install_fake_collect(monkeypatch)
    monkeypatch.setattr(portfolio_holdings, "attach_ledger_lots", boom)
    result = await call_get_holdings(**CALL)
    assert "ledger_lots" not in result


# ---------------------------------------------------------------------------
# opt-in is additive and KIS-live-KR only
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_opt_in_only_adds_keys_to_the_golden_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_collect(monkeypatch)
    _install_fake_loader(monkeypatch)
    result = await call_get_holdings(**CALL, include_ledger_lots=True)
    assert canonical_json(_strip_ledger_lots(result)) == GOLDEN_PATH.read_text(
        encoding="utf-8"
    )
    assert result["ledger_lots"]["requested"] is True
    assert result["ledger_lots"]["applied"] is True
    assert result["ledger_lots"]["external_orders_verifiable"] is False
    assert result["ledger_lots"]["cost_method"] == "fifo_remaining_lots_from_ledger"


@pytest.mark.asyncio
async def test_block_is_attached_to_kis_live_kr_positions_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []
    install_fake_collect(monkeypatch)
    _install_fake_loader(monkeypatch, seen=seen)
    result = await call_get_holdings(**CALL, include_ledger_lots=True)

    # The loader received the broker quantity/price already in hand.
    assert [(ref.symbol, ref.reference_quantity) for ref in seen] == [
        ("196170", Decimal("12")),
        ("171090", Decimal("40")),
    ]
    assert seen[0].current_price == Decimal("90000")

    tagged = {
        (g["broker"], p["market"], p["symbol"])
        for g in result["accounts"]
        for p in g["positions"]
        if "ledger_lots" in p
    }
    assert tagged == {("kis", "kr", "196170"), ("kis", "kr", "171090")}
    assert result["ledger_lots"]["positions_covered"] == 2
    # Toss KR position with the same symbol as a KIS one is untouched.
    toss = [
        p for g in result["accounts"] if g["broker"] == "toss" for p in g["positions"]
    ]
    assert toss and all("ledger_lots" not in p for p in toss)


@pytest.mark.asyncio
async def test_no_kis_kr_positions_means_no_query_and_zero_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.mcp_server import get_holdings_golden_support as support

    seen: list[Any] = []
    install_fake_collect(
        monkeypatch,
        [
            support.kis_us_position("AAPL"),
            support.toss_api_kr_position("171090", 5.0, 26000.0),
        ],
    )
    _install_fake_loader(monkeypatch, seen=seen)
    result = await call_get_holdings(**CALL, include_ledger_lots=True)
    assert seen == []
    assert result["ledger_lots"]["applied"] is True
    assert result["ledger_lots"]["positions_covered"] == 0
    assert all("ledger_lots" not in p for p in _positions(result))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["kis_mock", "db_simulated"])
async def test_non_live_routing_is_not_applied(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(portfolio_holdings, "validate_kis_mock_config", lambda: [])
    install_fake_collect(monkeypatch)
    _install_fake_loader(monkeypatch, seen=seen)
    result = await call_get_holdings(
        **CALL, account_mode=mode, include_ledger_lots=True
    )
    assert seen == []
    assert result["ledger_lots"] == {
        "requested": True,
        "scope": "kis_live_kr_positions",
        "cost_method": "fifo_remaining_lots_from_ledger",
        "external_orders_verifiable": False,
        "applied": False,
        "positions_covered": 0,
        "reason": "kis_live_kr_only",
    }
    assert all("ledger_lots" not in p for p in _positions(result))


# ---------------------------------------------------------------------------
# the block can never fail get_holdings
# ---------------------------------------------------------------------------
def _assert_all_kis_kr_unknown(result: dict[str, Any]) -> None:
    tagged = [
        p
        for p in _positions(result)
        if p.get("market") == "kr" and p["source"] == "kis_api"
    ]
    assert tagged
    for position in tagged:
        block = position["ledger_lots"]
        assert block["ledger_state"] == "unknown"
        assert block["unknown_reasons"] == ["ledger_read_failed"]
        assert block["lots"] is None
        assert block["open_buy_evidence"]["blocking"] is True


@pytest.mark.asyncio
async def test_loader_failure_yields_unknown_blocks_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("db down")

    install_fake_collect(monkeypatch)
    monkeypatch.setattr(portfolio_ledger_lots, "AsyncSessionLocal", _FakeSession)
    monkeypatch.setattr(portfolio_ledger_lots, "load_kis_live_kr_lot_blocks", boom)
    result = await call_get_holdings(**CALL, include_ledger_lots=True)
    _assert_all_kis_kr_unknown(result)
    assert result["total_positions"] == 4
    assert result["errors"] == []
    assert _strip_ledger_lots(result) == json.loads(GOLDEN_PATH.read_text("utf-8"))


@pytest.mark.asyncio
async def test_session_creation_failure_yields_unknown_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_session() -> None:
        raise RuntimeError("no pool")

    install_fake_collect(monkeypatch)
    monkeypatch.setattr(portfolio_ledger_lots, "AsyncSessionLocal", broken_session)
    result = await call_get_holdings(**CALL, include_ledger_lots=True)
    _assert_all_kis_kr_unknown(result)


@pytest.mark.asyncio
async def test_unexpected_attach_failure_is_contained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom_refs(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("bad ref")

    install_fake_collect(monkeypatch)
    _install_fake_loader(monkeypatch)
    monkeypatch.setattr(portfolio_ledger_lots, "PositionRef", boom_refs)
    result = await call_get_holdings(**CALL, include_ledger_lots=True)
    assert result["ledger_lots"]["applied"] is False
    assert result["ledger_lots"]["reason"] == "ledger_read_failed"
    _assert_all_kis_kr_unknown(result)


@pytest.mark.asyncio
async def test_missing_blocks_for_a_symbol_degrade_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def partial_loader(db: Any, refs: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"196170": {"ledger_state": "known", "marker": "only-one"}}

    install_fake_collect(monkeypatch)
    monkeypatch.setattr(portfolio_ledger_lots, "AsyncSessionLocal", _FakeSession)
    monkeypatch.setattr(
        portfolio_ledger_lots, "load_kis_live_kr_lot_blocks", partial_loader
    )
    result = await call_get_holdings(**CALL, include_ledger_lots=True)
    by_symbol = {
        p["symbol"]: p["ledger_lots"] for p in _positions(result) if "ledger_lots" in p
    }
    assert by_symbol["196170"]["marker"] == "only-one"
    assert by_symbol["171090"]["unknown_reasons"] == ["ledger_read_failed"]


# ---------------------------------------------------------------------------
# no broker surface
# ---------------------------------------------------------------------------
def test_ledger_block_modules_import_no_broker_client() -> None:
    for module in (portfolio_ledger_lots, kis_lots):
        source = inspect.getsource(module)
        assert "app.services.brokers" not in source, module.__name__
        assert "KISClient" not in source, module.__name__


def test_tool_signature_has_the_opt_in_default_false() -> None:
    from tests._mcp_tooling_support import DummyMCP

    mcp = DummyMCP()
    portfolio_holdings._register_portfolio_tools_impl(mcp)
    parameter = inspect.signature(mcp.tools["get_holdings"]).parameters[
        "include_ledger_lots"
    ]
    assert parameter.default is False
    # Appended last: existing positional callers are unaffected.
    assert (
        list(inspect.signature(mcp.tools["get_holdings"]).parameters)[-1]
        == "include_ledger_lots"
    )
