from __future__ import annotations

import inspect
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling import session_bootstrap_pack as pack
from app.mcp_server.tooling.session_bootstrap_registration import (
    register_session_bootstrap_tools,
)
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.review import KISLiveOrderLedger, TradeForecast
from app.models.session_context import OperatorSessionContext
from app.services.order_proposals import OrderProposalsService
from tests.mcp_server._registration_recorder import RegistrationRecorder

EXPECTED_DEFAULT_SECTIONS = (
    "briefing",
    "holdings",
    "cash",
    "resting",
    "pending_retros",
    "due_forecasts",
    "policy",
    "recent_context",
)
EXPECTED_OPEN_PROPOSAL_STATES = (
    "proposed",
    "approved",
    "partially_submitted",
    "submitted",
)


def _registered() -> set[str]:
    return set(pack.SECTION_SOURCE_TOOLS.values())


def _source_response(section: str) -> dict[str, Any]:
    common = {"success": True, "section": section, "freshness_status": "fresh"}
    responses = {
        "briefing": {
            **common,
            "pending_orders": {"count": 1, "orders": [{"id": "l1"}]},
        },
        "holdings": {**common, "positions": [{"symbol": "A"}]},
        "cash": {**common, "accounts": [{"currency": "KRW", "balance": 10}]},
        "pending_retros": {**common, "count": 1, "pending": [{"id": "r1"}]},
        "due_forecasts": {**common, "results": [{"id": "f1"}]},
        "recent_context": {**common, "entries": [{"id": "c1"}]},
    }
    return responses[section]


@pytest.fixture
def source_stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = dict.fromkeys(pack.DEFAULT_SECTIONS, 0)
    calls["real_resting"] = pack._resting

    async def briefing(*, market: str) -> dict[str, Any]:
        calls["briefing"] += 1
        return _source_response("briefing")

    async def holdings(market: str) -> dict[str, Any]:
        calls["holdings"] += 1
        return _source_response("holdings")

    async def cash() -> dict[str, Any]:
        calls["cash"] += 1
        return _source_response("cash")

    async def proposal_list(*, lifecycle_state: str) -> dict[str, Any]:
        calls["resting"] += 1
        return {
            "success": True,
            "count": 1,
            "proposals": [{"state": lifecycle_state}],
        }

    async def resting(**kwargs: Any) -> dict[str, Any]:
        calls["resting"] += 1
        return {
            "success": True,
            "proposals": {
                "by_state": {
                    state: 1 if state == "proposed" else 0
                    for state in EXPECTED_OPEN_PROPOSAL_STATES
                },
                "items": [{"lifecycle_state": "proposed"}],
            },
            "ledger_open": [{"id": "l1"}],
        }

    async def retros(*, limit: int) -> dict[str, Any]:
        calls["pending_retros"] += 1
        assert limit == 20
        return _source_response("pending_retros")

    async def forecasts(*, dry_run: bool) -> dict[str, Any]:
        calls["due_forecasts"] += 1
        assert dry_run is True
        return _source_response("due_forecasts")

    async def policy(*, market: str, lane: str) -> dict[str, Any]:
        calls["policy"] += 1
        return {
            "success": True,
            "version": "v1",
            "content_hash": "h1",
            "lane": lane,
        }

    async def context(*, market: str, account_scope: str, limit: int) -> dict[str, Any]:
        calls["recent_context"] += 1
        assert limit == 10
        return _source_response("recent_context")

    monkeypatch.setattr(
        pack.operating_briefing, "get_operating_briefing_impl", briefing
    )
    monkeypatch.setattr(pack, "_holdings", holdings)
    monkeypatch.setattr(pack, "_cash", cash)
    monkeypatch.setattr(pack, "_resting", resting)
    monkeypatch.setattr(pack.order_proposal_tools, "order_proposal_list", proposal_list)
    monkeypatch.setattr(
        pack.trade_retrospective_tools, "trade_retrospective_pending", retros
    )
    monkeypatch.setattr(pack.forecast_tools, "forecast_resolve", forecasts)
    monkeypatch.setattr(pack.trading_policy_tools, "get_trading_policy", policy)
    monkeypatch.setattr(
        pack.session_context_tools, "session_context_get_recent", context
    )
    return calls


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True)


@pytest.mark.asyncio
async def test_default_include_uses_every_fixed_section(
    source_stubs: dict[str, Any],
) -> None:
    result = await pack._session_bootstrap_pack(
        "kr", None, False, registered_tool_names=_registered
    )

    assert tuple(result["sections"]) == EXPECTED_DEFAULT_SECTIONS
    assert source_stubs["cash"] == 1


@pytest.mark.asyncio
async def test_policy_preserves_each_real_lane_response() -> None:
    sources = {
        lane: await pack.trading_policy_tools.get_trading_policy(market="kr", lane=lane)
        for lane in ("buy", "sell", "discovery")
    }
    result = await pack._session_bootstrap_pack(
        "kr",
        ["policy"],
        False,
        registered_tool_names=lambda: {"get_trading_policy"},
    )

    for lane, source in sources.items():
        assert _json(result["sections"]["policy"]["policies"][lane]) == _json(source)


@pytest.mark.asyncio
async def test_briefing_real_source_uses_the_default_account_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_scopes: list[str] = []

    async def summary(**kwargs: Any) -> dict[str, Any]:
        return {"accounts": [], "errors": [], "kwargs": kwargs}

    async def pending(_db: object, *, market: str, account_scope: str) -> Any:
        assert market == "kr"
        observed_scopes.append(account_scope)
        return SimpleNamespace(orders=[], unavailable_reason=None, as_of=None)

    async def recent_context(*args: object, **kwargs: object) -> dict[str, Any]:
        return {"count": 0, "entries": []}

    async def artifacts(*args: object, **kwargs: object) -> dict[str, Any]:
        return {"count": 0, "artifacts": []}

    async def watches(*args: object, **kwargs: object) -> dict[str, Any]:
        return {"count": 0, "active_watches": []}

    monkeypatch.setattr(pack.operating_briefing, "_get_portfolio_summary_impl", summary)
    monkeypatch.setattr(
        pack.operating_briefing, "collect_pending_orders_snapshot", pending
    )
    monkeypatch.setattr(
        pack.operating_briefing, "_recent_session_context", recent_context
    )
    monkeypatch.setattr(
        pack.operating_briefing, "_recent_analysis_artifacts", artifacts
    )
    monkeypatch.setattr(pack.operating_briefing, "list_active_watches_impl", watches)
    monkeypatch.setattr(
        pack.operating_briefing,
        "load_negative_class_health",
        lambda *args, **kwargs: SimpleNamespace(to_dict=lambda: {}),
    )
    monkeypatch.setattr(
        pack.operating_briefing, "get_account_costs_setting", lambda: None
    )

    result = await pack._session_bootstrap_pack(
        "kr",
        ["briefing"],
        False,
        registered_tool_names=lambda: {"get_operating_briefing"},
    )

    assert result["success"] is True
    assert observed_scopes == [
        pack.operating_briefing._default_account_scope("kr", None)
    ]


@pytest.mark.asyncio
async def test_holdings_and_cash_match_real_registered_closures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the routing wrappers on both the pack and registered tools."""

    holdings_calls: list[dict[str, Any]] = []
    capital_calls: list[dict[str, Any]] = []

    async def holdings_source(**kwargs: Any) -> dict[str, Any]:
        holdings_calls.append(dict(kwargs))
        assert kwargs["include_current_price"] is True
        return {
            "success": True,
            "accounts": [
                {
                    "account": "paper",
                    "broker": "paper",
                    "positions": [
                        {
                            "symbol": f"00{index:04d}",
                            "quantity": index + 1,
                            "current_price": 70_000 + index,
                            "currency": "KRW",
                        }
                        for index in range(3)
                    ],
                }
            ],
            "errors": [],
        }

    async def capital_source(**kwargs: Any) -> dict[str, Any]:
        capital_calls.append(dict(kwargs))
        assert kwargs.get("include_manual", True) is True
        return {
            "success": True,
            "accounts": [
                {
                    "account": f"paper:{index}",
                    "currency": "KRW",
                    "balance": 1_000_000 + index,
                    "orderable": 900_000 + index,
                }
                for index in range(3)
            ],
            "errors": [],
        }

    monkeypatch.setattr(pack.portfolio_holdings, "_get_holdings_impl", holdings_source)
    monkeypatch.setattr(
        pack.portfolio_holdings, "_get_available_capital_impl", capital_source
    )
    recorder = RegistrationRecorder()
    pack.portfolio_holdings._register_portfolio_tools_impl(recorder)  # type: ignore[arg-type]

    source_holdings = await recorder.tools["get_holdings"](market="kr")
    source_cash = await recorder.tools["get_available_capital"]()
    result = await pack._session_bootstrap_pack(
        "kr",
        ["holdings", "cash"],
        False,
        registered_tool_names=lambda: {"get_holdings", "get_available_capital"},
    )

    assert _json(result["sections"]["holdings"]) == _json(source_holdings)
    assert _json(result["sections"]["cash"]) == _json(source_cash)
    assert len(holdings_calls) == len(capital_calls) == 2

    source_holdings_call, pack_holdings_call = holdings_calls
    source_cash_call, pack_cash_call = capital_calls
    # ``_get_holdings_impl`` and ``get_available_capital_impl`` supply these
    # defaults when the pack omits an optional keyword. Compare the effective
    # call values, not just the sparse spellings of equivalent default calls.
    assert {
        "account": pack_holdings_call.get("account"),
        "market": pack_holdings_call.get("market"),
        "include_current_price": pack_holdings_call.get("include_current_price"),
        "minimum_value": pack_holdings_call.get("minimum_value"),
        "account_name": pack_holdings_call.get("account_name"),
        "fresh_sellable": pack_holdings_call.get("fresh_sellable", False),
    } == {
        "account": source_holdings_call["account"],
        "market": source_holdings_call["market"],
        "include_current_price": source_holdings_call["include_current_price"],
        "minimum_value": source_holdings_call["minimum_value"],
        "account_name": source_holdings_call["account_name"],
        "fresh_sellable": source_holdings_call["fresh_sellable"],
    }
    assert {
        "account": pack_cash_call.get("account"),
        "include_manual": pack_cash_call.get("include_manual", True),
        "is_mock": pack_cash_call["is_mock"],
    } == {
        "account": source_cash_call["account"],
        "include_manual": source_cash_call["include_manual"],
        "is_mock": source_cash_call["is_mock"],
    }


@pytest.mark.asyncio
async def test_recent_context_preserves_actual_source_response_subset(
    db_session: AsyncSession,
) -> None:
    """Capture a real source response after seeding three distinct local rows."""

    await db_session.execute(
        text(
            'TRUNCATE TABLE review."operator_session_context" RESTART IDENTITY CASCADE'
        )
    )
    scope = pack.operating_briefing._default_account_scope("kr", None)
    db_session.add_all(
        [
            OperatorSessionContext(
                kst_date=pack.now_kst().date(),
                market="kr",
                account_scope=scope,
                entry_type="handoff_note",
                title=f"handoff-{index}",
                body=f"context body {index}",
                refs={
                    "symbols": [f"00{index:04d}"],
                    "filled_notional": str(70_000 + index),
                    "currency": "KRW",
                },
                created_by="codex",
            )
            for index in range(3)
        ]
    )
    await db_session.commit()

    source = await pack.session_context_tools.session_context_get_recent(
        market="kr", account_scope=scope, limit=10
    )
    result = await pack._session_bootstrap_pack(
        "kr",
        ["recent_context"],
        False,
        registered_tool_names=lambda: {"session_context_get_recent"},
    )

    assert source["success"] is True
    assert source["count"] >= 3
    assert json.dumps(result["sections"]["recent_context"], sort_keys=True) == (
        json.dumps(source, sort_keys=True)
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("investment_reports_cleanup_lock")
async def test_pending_retros_preserves_real_seeded_source_response(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(delete(KISLiveOrderLedger))
    db_session.add_all(
        [
            KISLiveOrderLedger(
                trade_date=pack.now_kst() - timedelta(days=index),
                symbol=f"00{index:04d}",
                instrument_type="equity_kr",
                side="buy",
                order_type="limit",
                quantity=index + 1,
                price=70_000 + index,
                amount=(index + 1) * (70_000 + index),
                currency="KRW",
                account_mode="kis_live",
                broker="kis",
                status="filled",
                lifecycle_state="filled",
                order_no=f"ROB1347-RETRO-{index}",
            )
            for index in range(3)
        ]
    )
    await db_session.commit()

    source = await pack.trade_retrospective_tools.trade_retrospective_pending(limit=20)
    result = await pack._session_bootstrap_pack(
        "kr",
        ["pending_retros"],
        False,
        registered_tool_names=lambda: {"trade_retrospective_pending"},
    )

    assert source["success"] is True, source
    assert source["returned"] >= 3
    assert _json(result["sections"]["pending_retros"]) == _json(source)


@pytest.mark.asyncio
@pytest.mark.usefixtures("investment_reports_cleanup_lock")
async def test_due_forecasts_preserve_real_seeded_source_response(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(delete(TradeForecast))
    await db_session.commit()
    for index in range(3):
        seeded = await pack.forecast_tools.forecast_save(
            created_by="codex",
            symbol=f"FCAST{index}",
            instrument_type="equity_kr",
            forecast_target={"kind": "no_resolvable_forecast"},
            probability=0.2 + (index / 10),
            review_date=f"2020-01-0{index + 1}",
            session_label=f"bootstrap-{index}",
        )
        assert seeded["success"] is True

    source = await pack.forecast_tools.forecast_resolve(dry_run=True)
    result = await pack._session_bootstrap_pack(
        "kr",
        ["due_forecasts"],
        False,
        registered_tool_names=lambda: {"forecast_resolve"},
    )

    assert source["due_count"] >= 3
    assert _json(result["sections"]["due_forecasts"]) == _json(source)


@pytest.mark.asyncio
async def test_resting_proposals_preserve_real_source_responses(
    db_session: AsyncSession,
) -> None:
    # Keep this fixture below the source tool's 50-row cap. Rungs cascade from
    # their parent in production, but clearing them first makes the test's
    # database isolation explicit for this shared ledger table.
    await db_session.execute(delete(OrderProposalRung))
    await db_session.execute(delete(OrderProposal))
    await db_session.commit()

    seed_symbols = {
        state: f"REST-R3-{index}"
        for index, state in enumerate(EXPECTED_OPEN_PROPOSAL_STATES)
    }
    proposal_ids: dict[str, uuid.UUID] = {}
    for index, state in enumerate(EXPECTED_OPEN_PROPOSAL_STATES):
        rungs = [
            {
                "rung_index": 0,
                "side": "buy",
                "quantity": str(index + 1),
                "limit_price": str(70_000 + index),
                "notional": None,
            }
        ]
        if state == "partially_submitted":
            rungs.append(
                {
                    "rung_index": 1,
                    "side": "buy",
                    "quantity": "9",
                    "limit_price": "70009",
                    "notional": None,
                }
            )
        seeded = await pack.order_proposal_tools.order_proposal_create(
            symbol=seed_symbols[state],
            market="equity_kr",
            account_mode="kis_live",
            side="buy",
            order_type="limit",
            proposer="operator:bootstrap",
            thesis=f"seeded {state} proposal {index}",
            strategy="ladder",
            rungs=rungs,
        )
        assert seeded["success"] is True
        proposal_ids[state] = uuid.UUID(seeded["proposal_id"])

    # Every visible group state comes from the production transition graph.
    service = OrderProposalsService(db_session)
    now = pack.now_kst()
    for state in ("approved", "partially_submitted", "submitted"):
        proposal_id = proposal_ids[state]
        await service.transition_rung(proposal_id, 0, new_state="revalidating")
        await service.transition_rung(proposal_id, 0, new_state="approved")
    await service.transition_rung(
        proposal_ids["partially_submitted"], 0, new_state="submitting"
    )
    await service.transition_rung(proposal_ids["submitted"], 0, new_state="submitting")
    await service.record_resting(
        proposal_ids["submitted"],
        0,
        broker_order_id="ROB1347-REST-SUBMITTED",
        correlation_id="rob1347-resting-submitted",
        idempotency_key="rob1347-resting-submitted-key",
        approval_hash_digest="rob1347-resting-submitted-digest",
        now=now,
    )
    await db_session.commit()

    sources = {
        state: await pack.order_proposal_tools.order_proposal_list(
            symbol=seed_symbols[state], lifecycle_state=state
        )
        for state in EXPECTED_OPEN_PROPOSAL_STATES
    }
    result = await pack._session_bootstrap_pack(
        "kr",
        ["resting"],
        False,
        registered_tool_names=lambda: {"order_proposal_list"},
    )

    assert all(source["count"] > 0 for source in sources.values())
    proposals = result["sections"]["resting"]["proposals"]
    assert set(proposals["by_state"]) == set(EXPECTED_OPEN_PROPOSAL_STATES)
    expected_seeded_items = {
        state: source["proposals"] for state, source in sources.items()
    }
    actual_seeded_items = {
        state: [
            item for item in proposals["items"] if item["symbol"] == seed_symbols[state]
        ]
        for state in EXPECTED_OPEN_PROPOSAL_STATES
    }
    assert all(expected_seeded_items.values())
    assert _json(actual_seeded_items) == _json(expected_seeded_items)
    seeded_by_state = {
        state: len(items) for state, items in actual_seeded_items.items()
    }
    assert seeded_by_state == {
        state: sources[state]["count"] for state in EXPECTED_OPEN_PROPOSAL_STATES
    }
    assert all(
        proposals["by_state"][state] >= seeded_by_state[state]
        for state in EXPECTED_OPEN_PROPOSAL_STATES
    )
    assert seeded_by_state["proposed"] == sources["proposed"]["count"]


@pytest.mark.asyncio
async def test_resting_rejects_unknown_group_lifecycle_state() -> None:
    with pytest.raises(ValueError, match="unsupported proposal lifecycle_state"):
        await pack._order_proposal_list_for_state("resting")


@pytest.mark.asyncio
@pytest.mark.parametrize("section", pack.DEFAULT_SECTIONS)
async def test_section_failure_is_isolated(
    monkeypatch: pytest.MonkeyPatch,
    source_stubs: dict[str, Any],
    section: str,
) -> None:
    original = pack._section_source

    async def raises_one(name: str, **kwargs: Any) -> dict[str, Any]:
        if name == section:
            raise RuntimeError("forced failure")
        return await original(name, **kwargs)

    monkeypatch.setattr(pack, "_section_source", raises_one)
    try:
        result = await pack._session_bootstrap_pack(
            "kr", None, False, registered_tool_names=_registered
        )
    except Exception as exc:  # mutation proof requires an assertion failure
        raise AssertionError(f"section isolation raised {exc!r}") from exc

    assert result["success"] is True
    assert result["sections"][section]["state"] == "missing"
    assert result["meta"]["sections"][section]["state"] == "missing"
    for other in set(pack.DEFAULT_SECTIONS) - {section}:
        assert result["meta"]["sections"][other]["state"] == "fresh"


@pytest.mark.asyncio
async def test_include_only_calls_requested_sections(
    source_stubs: dict[str, Any],
) -> None:
    result = await pack._session_bootstrap_pack(
        "kr", ["briefing", "cash", "policy"], False, registered_tool_names=_registered
    )

    assert set(result["sections"]) == {"briefing", "cash", "policy"}
    assert source_stubs["briefing"] == source_stubs["cash"] == 1
    assert source_stubs["policy"] == 3
    assert all(
        source_stubs[name] == 0
        for name in set(pack.DEFAULT_SECTIONS) - {"briefing", "cash", "policy"}
    )


@pytest.mark.asyncio
async def test_unknown_section_is_fail_closed() -> None:
    assert await pack._session_bootstrap_pack(
        "kr", ["nope"], False, registered_tool_names=_registered
    ) == {
        "success": False,
        "error": "unknown_section",
        "unknown": ["nope"],
    }


@pytest.mark.asyncio
async def test_missing_resolver_denies_every_section() -> None:
    result = await pack._session_bootstrap_pack(
        "kr", None, False, registered_tool_names=None
    )

    assert result["meta"]["resolver_unavailable"] is True
    for section, tool in pack.SECTION_SOURCE_TOOLS.items():
        assert result["sections"][section] == {
            "state": "denied_by_profile",
            "tool": tool,
        }


@pytest.mark.asyncio
async def test_resolver_failure_denies_every_section() -> None:
    def unavailable() -> set[str]:
        raise RuntimeError("inventory unavailable")

    result = await pack._session_bootstrap_pack(
        "kr", None, False, registered_tool_names=unavailable
    )

    assert result["meta"]["resolver_unavailable"] is True
    assert all(
        value["state"] == "denied_by_profile" for value in result["sections"].values()
    )


@pytest.mark.asyncio
async def test_compact_records_truncation(source_stubs: dict[str, Any]) -> None:
    async def oversized(market: str) -> dict[str, Any]:
        return {
            "success": True,
            "positions": [
                {"symbol": str(index), "detail": "x" * 3_000} for index in range(30)
            ],
        }

    # Use the same source call path, only replacing the lower-level holdings value.
    original = pack._holdings
    pack._holdings = oversized
    try:
        result = await pack._session_bootstrap_pack(
            "kr", ["holdings"], False, registered_tool_names=_registered
        )
    finally:
        pack._holdings = original

    assert result["compact"] is True
    assert result["meta"]["compact_downgraded"] is True
    assert len(result["sections"]["holdings"]["positions"]) == 20
    assert result["meta"]["sections"]["holdings"]["truncated_from"] == 30


@pytest.mark.asyncio
@pytest.mark.usefixtures("investment_reports_cleanup_lock")
async def test_compact_truncates_seeded_due_forecasts(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(delete(TradeForecast))
    await db_session.commit()
    for index in range(21):
        seeded = await pack.forecast_tools.forecast_save(
            created_by="codex",
            symbol=f"DUE{index:02d}",
            instrument_type="equity_kr",
            forecast_target={"kind": "no_resolvable_forecast"},
            probability=0.1 + (index / 100),
            review_date="2020-01-01",
            session_label=f"compact-{index}",
        )
        assert seeded["success"] is True

    result = await pack._session_bootstrap_pack(
        "kr",
        ["due_forecasts"],
        True,
        registered_tool_names=lambda: {"forecast_resolve"},
    )

    assert result["success"] is True
    assert len(result["sections"]["due_forecasts"]["results"]) == 20
    assert result["meta"]["sections"]["due_forecasts"]["truncated_from"] == 21


@pytest.mark.asyncio
async def test_briefing_and_resting_share_one_pending_order_snapshot(
    monkeypatch: pytest.MonkeyPatch, source_stubs: dict[str, Any]
) -> None:
    snapshot_calls = 0

    async def snapshot(*args: object, **kwargs: object) -> SimpleNamespace:
        nonlocal snapshot_calls
        snapshot_calls += 1
        return SimpleNamespace(orders=[{"id": "live-1"}], unavailable_reason=None)

    async def briefing(*, market: str) -> dict[str, Any]:
        await pack.collect_pending_orders_snapshot(
            None, market=market, account_scope="all"
        )
        return _source_response("briefing")

    monkeypatch.setattr(pack, "collect_pending_orders_snapshot", snapshot)
    monkeypatch.setattr(pack, "_resting", source_stubs["real_resting"])
    monkeypatch.setattr(
        pack.operating_briefing, "get_operating_briefing_impl", briefing
    )

    result = await pack._session_bootstrap_pack(
        "kr", None, False, registered_tool_names=_registered
    )

    assert result["success"] is True
    assert snapshot_calls == 1


@pytest.mark.asyncio
async def test_forecast_resolve_is_always_dry_run(
    monkeypatch: pytest.MonkeyPatch, source_stubs: dict[str, Any]
) -> None:
    calls: list[bool] = []

    async def forecasts(*, dry_run: bool) -> dict[str, Any]:
        calls.append(dry_run)
        return _source_response("due_forecasts")

    monkeypatch.setattr(pack.forecast_tools, "forecast_resolve", forecasts)
    result = await pack._session_bootstrap_pack(
        "kr", ["due_forecasts"], False, registered_tool_names=_registered
    )

    assert result["success"] is True
    assert calls == [True]


@pytest.mark.asyncio
async def test_logging_failure_is_isolated(
    monkeypatch: pytest.MonkeyPatch, source_stubs: dict[str, Any]
) -> None:
    monkeypatch.setattr(
        pack.logger,
        "info",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("log")),
    )
    monkeypatch.setattr(
        pack.logger,
        "exception",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("log")),
    )

    result = await pack._session_bootstrap_pack(
        "kr", ["cash"], False, registered_tool_names=_registered
    )

    assert result["success"] is True
    assert result["sections"]["cash"]["state"] == "missing"


@pytest.mark.asyncio
async def test_elapsed_measurement_failure_is_isolated(
    monkeypatch: pytest.MonkeyPatch, source_stubs: dict[str, Any]
) -> None:
    def raises(*args: object) -> int:
        raise RuntimeError("clock")

    monkeypatch.setattr(pack, "_elapsed_ms", raises)
    result = await pack._session_bootstrap_pack(
        "kr", ["cash"], False, registered_tool_names=_registered
    )

    assert result["success"] is True
    assert result["sections"]["cash"]["state"] == "missing"


def test_registration_keeps_public_signature() -> None:
    recorder = RegistrationRecorder()
    register_session_bootstrap_tools(recorder, registered_tool_names=_registered)  # type: ignore[arg-type]
    tool = recorder.tools["session_bootstrap_pack"]
    assert tool.__name__ == "session_bootstrap_pack"


@pytest.mark.asyncio
async def test_public_impl_requires_a_registration_resolver() -> None:
    signature = inspect.signature(pack.session_bootstrap_pack_impl)
    assert (
        signature.parameters["registered_tool_names"].default is inspect.Parameter.empty
    )

    result = await pack.session_bootstrap_pack_impl(
        "kr",
        ["policy"],
        registered_tool_names=lambda: {"get_trading_policy"},
    )

    assert result["meta"]["sections"]["policy"]["state"] in {"fresh", "stale"}
