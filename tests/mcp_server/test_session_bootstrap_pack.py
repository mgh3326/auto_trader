from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling import session_bootstrap_pack as pack
from app.mcp_server.tooling.session_bootstrap_registration import (
    register_session_bootstrap_tools,
)
from app.models.session_context import OperatorSessionContext
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
            "count": 2,
            "proposals": [{"state": "pending"}, {"state": "resting"}],
            "live_orders": {"count": 1, "orders": [{"id": "l1"}]},
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


@pytest.mark.asyncio
async def test_sections_preserve_source_values(source_stubs: dict[str, Any]) -> None:
    result = await pack._session_bootstrap_pack(
        "kr", None, False, registered_tool_names=_registered
    )

    assert result["success"] is True
    assert tuple(result["sections"]) == EXPECTED_DEFAULT_SECTIONS
    assert result["sections"]["briefing"] == _source_response("briefing")
    assert result["sections"]["holdings"] == _source_response("holdings")
    assert result["sections"]["cash"] == _source_response("cash")
    assert result["sections"]["pending_retros"] == _source_response("pending_retros")
    assert result["sections"]["due_forecasts"] == _source_response("due_forecasts")
    assert result["sections"]["recent_context"] == _source_response("recent_context")
    assert result["sections"]["policy"]["version"] == "v1"
    assert result["sections"]["policy"]["content_hash"] == "h1"
    assert source_stubs["resting"] == 1


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

    assert source["count"] >= 3
    assert json.dumps(result["sections"]["recent_context"], sort_keys=True) == (
        json.dumps(source, sort_keys=True)
    )


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
    assert await pack.session_bootstrap_pack_impl("kr", ["nope"]) == {
        "success": False,
        "error": "unknown_section",
        "unknown": ["nope"],
    }


@pytest.mark.asyncio
async def test_missing_resolver_denies_every_section() -> None:
    result = await pack.session_bootstrap_pack_impl("kr")

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
