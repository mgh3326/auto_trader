from __future__ import annotations

import ast
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select

from app.core.timezone import now_kst
from app.mcp_server.tooling import proposal_revalidate as revalidate
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.services.order_proposals.state_machine import GROUP_STATES
from app.services.order_proposals.void_authorization import SERVER_LOSS_GUARD_SOURCE
from app.services.trading_policy_service import policy_version_stamp
from tests.mcp_server._registration_recorder import collect_profile_tools

pytestmark = pytest.mark.unit


_FULL_CAPABILITIES = {"order_proposal_list", "order_proposal_void"}
_LIST_CAPABILITIES = {"order_proposal_list"}
_SEEDED_SYMBOLS = frozenset(
    {"RVKEEP01", "RVDEAD02", "RVGUARD03", "RVFILLED04", "RVSTALE05"}
)


async def _seed_revalidation_rows(db_session) -> dict[str, str]:
    """Seed isolated proposal and rung rows with real lifecycle vocabulary."""

    seeded_pks = select(OrderProposal.id).where(
        OrderProposal.symbol.in_(_SEEDED_SYMBOLS)
    )
    await db_session.execute(
        delete(OrderProposalRung).where(OrderProposalRung.proposal_pk.in_(seeded_pks))
    )
    await db_session.execute(
        delete(OrderProposal).where(OrderProposal.symbol.in_(_SEEDED_SYMBOLS))
    )
    await db_session.commit()

    now = now_kst()
    current_policy = policy_version_stamp()
    labels = {
        "keep": {
            "symbol": "RVKEEP01",
            "lifecycle_state": "proposed",
            "price": "101",
            "anchor": "100",
            "band_bps": "100",
            "rung_state": "pending_approval",
            "policy": current_policy,
        },
        "dead_anchor": {
            "symbol": "RVDEAD02",
            "lifecycle_state": "approved",
            "price": "102",
            "anchor": "100",
            "band_bps": "100",
            "rung_state": "pending_approval",
            "policy": current_policy,
        },
        "guard_blocked": {
            "symbol": "RVGUARD03",
            "lifecycle_state": "partially_submitted",
            "price": "99.5",
            "anchor": "100",
            "band_bps": "100",
            "rung_state": "resting",
            "policy": current_policy,
        },
        "filled_or_expired": {
            "symbol": "RVFILLED04",
            "lifecycle_state": "submitted",
            "price": "101",
            "anchor": "100",
            "band_bps": "100",
            "rung_state": "filled",
            "policy": {"version": "older", "content_hash": "older-hash"},
        },
        "stale_policy": {
            "symbol": "RVSTALE05",
            "lifecycle_state": "submitted",
            "price": "100",
            "anchor": "100",
            "band_bps": "100",
            "rung_state": "pending_approval",
            "policy": {"version": "older", "content_hash": "older-hash"},
        },
    }
    seeded: dict[str, str] = {}
    for index, (expected_label, values) in enumerate(labels.items()):
        proposal_id = uuid.uuid4()
        source_asof: dict[str, Any] = {
            "policy": values["policy"],
            "proposal_revalidate": {
                "anchor": {
                    "price": values["anchor"],
                    "band_bps": values["band_bps"],
                }
            },
        }
        if expected_label == "guard_blocked":
            source_asof["loss_guard_verdict"] = {
                "violated": True,
                "source": SERVER_LOSS_GUARD_SOURCE,
                "observed_at": now.isoformat(),
                "rung_index": 0,
                "error": "loss_sell_blocked",
            }
        group = OrderProposal(
            proposal_id=proposal_id,
            root_proposal_id=proposal_id,
            revision=index + 1,
            symbol=values["symbol"],
            market="equity_kr",
            account_mode="kis_live",
            side="buy",
            order_type="limit",
            proposer="mcp-revalidation-test",
            lifecycle_state=values["lifecycle_state"],
            payload_hash=f"proposal-hash-{index}",
            source_asof=source_asof,
            valid_until=now + timedelta(days=1),
        )
        db_session.add(group)
        await db_session.flush()
        db_session.add(
            OrderProposalRung(
                proposal_pk=group.id,
                rung_index=0,
                side="buy",
                quantity=Decimal(str(index + 1)),
                limit_price=Decimal(values["anchor"]),
                notional=Decimal(str((index + 1) * 100)),
                state=values["rung_state"],
                idempotency_key=f"rv-key-{index}",
                correlation_id=f"rv-correlation-{index}",
            )
        )
        seeded[expected_label] = str(proposal_id)
    await db_session.commit()
    return seeded


def _quote_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    prices = {
        "RVKEEP01": "101",
        "RVDEAD02": "102",
        "RVGUARD03": "99.5",
        "RVFILLED04": "101",
        "RVSTALE05": "100",
    }

    async def get_quote(
        symbol: str | int,
        market: str | None = None,
        include_extended_hours: bool = False,
    ) -> dict[str, Any]:
        del include_extended_hours
        return {
            "symbol": str(symbol),
            "instrument_type": "equity_kr",
            "price": prices.get(str(symbol), "100"),
            "source": "test-quote",
            "market": market,
        }

    monkeypatch.setattr(revalidate.market_data_quotes, "_get_quote_impl", get_quote)


async def _run(
    *,
    proposal_ids: list[str] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    capabilities: set[str] = _FULL_CAPABILITIES,
) -> dict[str, Any]:
    return await revalidate.proposal_revalidate_impl(
        "kr",
        proposal_ids,
        dry_run,
        confirm,
        registered_tool_names=lambda: capabilities,
    )


async def _proposal_counts(db_session) -> tuple[int, int]:
    seeded_pks = select(OrderProposal.id).where(
        OrderProposal.symbol.in_(_SEEDED_SYMBOLS)
    )
    return (
        await db_session.scalar(
            select(func.count())
            .select_from(OrderProposal)
            .where(OrderProposal.symbol.in_(_SEEDED_SYMBOLS))
        )
        or 0,
        await db_session.scalar(
            select(func.count())
            .select_from(OrderProposalRung)
            .where(OrderProposalRung.proposal_pk.in_(seeded_pks))
        )
        or 0,
    )


@pytest.mark.asyncio
async def test_proposal_revalidate_labels_priority_and_numeric_evidence(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed_revalidation_rows(db_session)
    _quote_stub(monkeypatch)

    result = await _run(proposal_ids=list(seeded.values()))

    assert result["success"] is True
    assert result["policy"] == policy_version_stamp()
    assert result["label_priority"] == list(revalidate.LABEL_PRIORITY)
    assert result["count"] == 5
    by_label = {
        item["label"]: item
        for item in result["results"]
        if item["proposal_id"] in seeded.values()
    }
    assert by_label
    assert set(by_label) == set(seeded)
    assert len(by_label) == 5
    assert all(item["void"] in revalidate.VOID_VALUES for item in by_label.values())

    keep = by_label["keep"]
    assert keep["proposal_id"] == seeded["keep"]
    assert keep["evidence"] == {
        "current_price": "101",
        "anchor": "100",
        "distance_bps": "100",
        "band_bps": "100",
    }

    dead_anchor = by_label["dead_anchor"]
    assert dead_anchor["proposal_id"] == seeded["dead_anchor"]
    assert dead_anchor["evidence"] == {
        "current_price": "102",
        "anchor": "100",
        "distance_bps": "200",
        "band_bps": "100",
    }

    guard_blocked = by_label["guard_blocked"]
    assert guard_blocked["proposal_id"] == seeded["guard_blocked"]
    assert guard_blocked["evidence"]["rule"] == "loss_sell_guard"
    assert guard_blocked["evidence"]["value"] == "loss_sell_blocked"
    assert guard_blocked["evidence"]["current_price"] == "99.5"
    assert guard_blocked["evidence"]["anchor"] == "100"

    filled = by_label["filled_or_expired"]
    assert filled["proposal_id"] == seeded["filled_or_expired"]
    assert filled["void"] == "skipped_dry_run"
    assert filled["evidence"]["lifecycle_state"] == "submitted"
    assert filled["evidence"]["rung_states"] == ["filled"]
    assert filled["evidence"]["valid_until"] is not None

    stale_policy = by_label["stale_policy"]
    assert stale_policy["proposal_id"] == seeded["stale_policy"]
    assert stale_policy["evidence"]["proposal_policy"] == {
        "version": "older",
        "content_hash": "older-hash",
    }
    assert stale_policy["evidence"]["current_policy"] == policy_version_stamp()
    # The filled row also has a stale policy stamp. The explicit priority makes
    # terminal ledger evidence win deterministically.
    assert filled["label"] == revalidate.LABEL_PRIORITY[0]


@pytest.mark.asyncio
async def test_proposal_revalidate_default_population_includes_all_seeded_rows(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed_revalidation_rows(db_session)
    _quote_stub(monkeypatch)

    result = await _run()
    selected = [
        item for item in result["results"] if item["proposal_id"] in seeded.values()
    ]

    assert result["success"] is True
    assert len(selected) == 5
    assert {item["proposal_id"] for item in selected} == set(seeded.values())


@pytest.mark.asyncio
async def test_proposal_revalidate_dry_run_has_no_writes_and_no_broker_client(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed_revalidation_rows(db_session)
    _quote_stub(monkeypatch)

    class BrokerClientSpy:
        calls = 0

        def __init__(self) -> None:
            type(self).calls += 1

    monkeypatch.setattr(revalidate.market_data_quotes, "KISClient", BrokerClientSpy)
    before = await _proposal_counts(db_session)
    result = await _run(proposal_ids=list(seeded.values()))
    after = await _proposal_counts(db_session)

    assert result["count"] == 5
    assert before == (5, 5)
    assert after == before
    assert BrokerClientSpy.calls == 0


def test_proposal_revalidate_has_no_unrelated_mutation_references() -> None:
    root = Path(__file__).parents[2]
    module_paths = (
        root / "app/mcp_server/tooling/proposal_revalidate.py",
        root / "app/mcp_server/tooling/proposal_revalidate_registration.py",
    )
    forbidden = ("order_proposal_create", "supersede", "modify")
    for path in module_paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        mutation_calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"add", "commit", "delete", "execute", "flush"}
        }
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert not any(
            module.startswith("app.services.brokers") for module in imported_modules
        )
        assert not mutation_calls
        assert all(token not in source for token in forbidden)


@pytest.mark.asyncio
async def test_proposal_revalidate_confirm_and_void_target_are_fail_closed(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed_revalidation_rows(db_session)
    _quote_stub(monkeypatch)

    unconfirmed = await _run(
        proposal_ids=list(seeded.values()), dry_run=False, confirm=False
    )
    assert unconfirmed == {"success": False, "error": "confirm_required"}

    calls: list[str] = []
    original_void = revalidate.order_proposal_tools.order_proposal_void

    async def recording_void(proposal_id: str, reason: str) -> dict[str, Any]:
        calls.append(proposal_id)
        return await original_void(proposal_id, reason)

    monkeypatch.setattr(
        revalidate.order_proposal_tools, "order_proposal_void", recording_void
    )
    result = await _run(proposal_ids=list(seeded.values()), dry_run=False, confirm=True)

    assert result["count"] == 5
    assert calls == [seeded["filled_or_expired"]]
    by_id = {item["proposal_id"]: item for item in result["results"]}
    assert by_id[seeded["filled_or_expired"]]["void"] == "refused"
    assert by_id[seeded["filled_or_expired"]]["error"] == "void_not_authorized"
    for label in ("keep", "dead_anchor", "guard_blocked", "stale_policy"):
        assert by_id[seeded[label]]["void"] == "not_applicable"


@pytest.mark.asyncio
async def test_proposal_revalidate_reports_void_refusal(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed_revalidation_rows(db_session)
    _quote_stub(monkeypatch)

    result = await _run(
        proposal_ids=[seeded["filled_or_expired"]], dry_run=False, confirm=True
    )

    assert result["count"] == 1
    assert result["results"]
    refusal = result["results"][0]
    assert refusal["proposal_id"] == seeded["filled_or_expired"]
    assert refusal["label"] == "filled_or_expired"
    assert refusal["void"] == "refused"
    assert refusal["error"] == "void_not_authorized"
    assert refusal["evidence"]["rung_states"] == ["filled"]


@pytest.mark.asyncio
async def test_proposal_revalidate_profile_gates_and_resolver_failure(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_revalidation_rows(db_session)
    _quote_stub(monkeypatch)

    missing_list = await revalidate.proposal_revalidate_impl(
        "kr",
        registered_tool_names=lambda: set(),
    )
    missing_void = await _run(
        dry_run=False, confirm=True, capabilities=_LIST_CAPABILITIES
    )

    def broken_resolver() -> set[str]:
        raise RuntimeError("registration unavailable")

    resolver_failure = await revalidate.proposal_revalidate_impl(
        "kr", registered_tool_names=broken_resolver
    )

    assert missing_list == {"state": "denied_by_profile", "tool": "order_proposal_list"}
    assert missing_void == {"state": "denied_by_profile", "tool": "order_proposal_void"}
    assert resolver_failure == {
        "state": "denied_by_profile",
        "tool": "order_proposal_list",
    }


def test_proposal_revalidate_profile_registration_follows_list_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = collect_profile_tools(monkeypatch, gates_enabled=True)
    list_profiles = {
        profile
        for profile, names in inventory.items()
        if "order_proposal_list" in names
    }
    revalidate_profiles = {
        profile
        for profile, names in inventory.items()
        if "proposal_revalidate" in names
    }
    void_profiles = {
        profile
        for profile, names in inventory.items()
        if "order_proposal_void" in names
    }

    assert list_profiles
    assert revalidate_profiles == list_profiles
    assert len(revalidate_profiles) == 9
    assert void_profiles <= list_profiles


@pytest.mark.asyncio
async def test_proposal_revalidate_rejects_non_group_lifecycle_vocabulary() -> None:
    assert "pending" not in GROUP_STATES
    with pytest.raises(ValueError, match="unsupported proposal lifecycle_state"):
        await revalidate._list_for_state("pending")
