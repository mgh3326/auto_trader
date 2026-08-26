from __future__ import annotations

import ast
import inspect
import textwrap
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from itertools import combinations
from types import SimpleNamespace
from typing import Any

import pytest

from app.mcp_server.tooling.support_reserve_net_consumer_tool import (
    RESERVE_NET_JOIN_KEY_CENSUS,
    RESERVE_NET_JOIN_KEY_PROTECTION_PARTITIONS,
    support_reserve_net_consume_impl,
)
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.service import (
    WatchToOrderScope,
    WatchToOrderScopeInspection,
)
from app.services.support_reserve_net_consumer import SupportReserveNetConsumer


def _candidate(
    symbol: str = "WIRE-A",
    *,
    sector: str = "software",
    broker_account_id: str = "acct-exact-1",
    beneficial_owner_id: str = "owner-1",
) -> dict[str, Any]:
    return {
        "normalized_symbol": symbol,
        "market": "equity_kr",
        "account_mode": "kis_live",
        "broker_account_id": broker_account_id,
        "beneficial_owner_id": beneficial_owner_id,
        "intent": "new",
        "current_price": "100",
        "support_price": "96",
        "support_strength": "moderate",
        "independent_support_families": ["fib", "bb_lower"],
        "honest_upside_pct": "45",
        "regular_gate_failure": "RSI_ONLY",
        "discount_below_support_pct": "5",
        "proposed_limit_price": "91.2",
        "price_tick": "0.1",
        "quantity": "1000",
        "required_cash": "100000",
        "sector_cluster": sector,
        "post_fill_sector_concentration_pct": "5",
        "post_fill_sector_increase": "0.01",
        "thesis": "fresh support evidence from independent source families",
    }


def _request(*candidates: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidates": list(candidates or (_candidate(),)),
        "cash_snapshots": [
            {
                "account_mode": "kis_live",
                "broker_account_id": "acct-exact-1",
                "currency": "KRW",
                "fresh_broker_orderable_cash": "500000",
                "net_orderable_cash": "500000",
                "all_pending_buy_required_cash": "0",
                "reserve_net_armed_required_cash": "0",
                "is_fresh": True,
                "same_account_currency_pending_accounted": True,
            }
        ],
        "reserve_net_attributions": [],
        "self_unfilled_orders": [],
        "sector_exposures": [],
        "self_unfilled_order_read_complete": True,
        "sector_exposure_complete": True,
        "submissions_frozen": False,
    }


def _attribution(
    symbol: str = "WIRE-HISTORY",
    *,
    beneficial_owner_id: str = "owner-1",
    strategy: str = "buy.unrelated",
    state: str = "filled",
    sector: str = "hardware",
) -> dict[str, Any]:
    return {
        "normalized_symbol": symbol,
        "market": "equity_kr",
        "beneficial_owner_id": beneficial_owner_id,
        "account_mode": "kis_live",
        "broker_account_id": "acct-exact-1",
        "state": state,
        "strategy": strategy,
        "sector_cluster": sector,
    }


def _self_unfilled_order(
    symbol: str = "WIRE-SELL",
    *,
    beneficial_owner_id: str = "owner-1",
    side: str = "sell",
) -> dict[str, Any]:
    return {
        "normalized_symbol": symbol,
        "market": "equity_kr",
        "beneficial_owner_id": beneficial_owner_id,
        "account_mode": "kis_live",
        "broker_account_id": "acct-exact-1",
        "side": side,
    }


def _sector_exposure(
    symbol: str = "WIRE-HARDWARE",
    *,
    beneficial_owner_id: str = "owner-1",
    sector: str = "hardware",
) -> dict[str, Any]:
    return {
        "normalized_symbol": symbol,
        "market": "equity_kr",
        "beneficial_owner_id": beneficial_owner_id,
        "sector_cluster": sector,
    }


def _request_with_all_owner_record_groups() -> dict[str, Any]:
    payload = _request(
        _candidate(),
        _candidate("WIRE-B", sector="hardware"),
    )
    payload["reserve_net_attributions"] = [
        _attribution(),
        _attribution("WIRE-HISTORY-2", sector="finance"),
    ]
    payload["self_unfilled_orders"] = [
        _self_unfilled_order(),
        _self_unfilled_order("WIRE-SELL-2"),
    ]
    payload["sector_exposures"] = [
        _sector_exposure(),
        _sector_exposure("WIRE-FINANCE", sector="finance"),
    ]
    return payload


def _consumer_join_key_fields() -> set[str]:
    """Discover fields used by every request-fed index builder in ``plan``."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(SupportReserveNetConsumer)))
    class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    plan = methods["plan"]

    def is_request_collection(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "request"
        )

    builder_names: set[str] = set()
    for call in ast.walk(plan):
        if not isinstance(call, ast.Call):
            continue
        arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
        if not (
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
            and any(is_request_collection(argument) for argument in arguments)
        ):
            continue
        builder_names.add(call.func.attr)

    fields: set[str] = set()
    for builder_name in builder_names:
        method = methods[builder_name]
        record_names: set[str] = set()
        for node in ast.walk(method):
            target: ast.expr | None = None
            if isinstance(node, (ast.For, ast.comprehension)):
                target = node.target
            if isinstance(target, ast.Name):
                record_names.add(target.id)
            elif isinstance(target, (ast.Tuple, ast.List)):
                record_names.update(
                    item.id for item in target.elts if isinstance(item, ast.Name)
                )
        fields.update(
            node.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in record_names
        )
    return fields


def test_consumer_join_key_census_is_disjoint_total_and_source_derived() -> None:
    discovered = _consumer_join_key_fields()
    protected = tuple(RESERVE_NET_JOIN_KEY_PROTECTION_PARTITIONS.values())

    for left, right in combinations(protected, 2):
        assert left.isdisjoint(right)
    assert frozenset().union(*protected) == RESERVE_NET_JOIN_KEY_CENSUS
    assert discovered == set(RESERVE_NET_JOIN_KEY_CENSUS)


class _FakeSession(AbstractAsyncContextManager):
    def __init__(self) -> None:
        self.commit_calls = 0
        self.rollback_calls = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1


class _LegacyProbeService:
    def __init__(self) -> None:
        self.inspected_account_ids: list[str | None] = []
        self.create_calls = 0

    async def inspect_watch_to_order_scope(self, **kwargs: Any) -> Any:
        account_id = kwargs["broker_account_id"]
        self.inspected_account_ids.append(account_id)
        scope = WatchToOrderScope(**kwargs)
        return WatchToOrderScopeInspection(
            scope=scope,
            lock_acquired=True,
            active_groups=(object(),) if account_id is None else (),  # type: ignore[arg-type]
        )

    async def create_proposal_in_watch_to_order_scope(
        self, inspection: Any, **kwargs: Any
    ) -> Any:
        self.create_calls += 1
        raise AssertionError("Probe B must stop before companion create")


@pytest.mark.asyncio
async def test_caller_invokes_consume_and_probe_b_never_creates(
    monkeypatch,
) -> None:
    session = _FakeSession()
    service = _LegacyProbeService()
    consume_calls = 0
    original_consume = SupportReserveNetConsumer.consume

    async def record_consume(self, request, *, proposal_creator=None):
        nonlocal consume_calls
        consume_calls += 1
        return await original_consume(
            self,
            request,
            proposal_creator=proposal_creator,
        )

    monkeypatch.setattr(SupportReserveNetConsumer, "consume", record_consume)

    result = await support_reserve_net_consume_impl(
        _request(),
        session_factory=lambda: session,
        service_factory=lambda _: service,
    )

    assert consume_calls == 1
    assert result["success"] is True
    assert result["proposal_creation_status"] == (
        "legacy_unscoped_active_proposal_exists"
    )
    assert result["proposal_count"] == 0
    assert result["proposals_created"] == []
    assert service.inspected_account_ids == [None]
    assert service.create_calls == 0
    assert session.commit_calls == 0
    assert session.rollback_calls == 1


@pytest.mark.parametrize("account_id", [None, "", " acct-exact-1 "])
@pytest.mark.asyncio
async def test_unresolved_account_id_opens_no_session_and_creates_zero(
    account_id: str | None,
) -> None:
    payload = _request()
    payload["candidates"][0]["broker_account_id"] = account_id
    session_factory_calls = 0

    def forbidden_session_factory() -> _FakeSession:
        nonlocal session_factory_calls
        session_factory_calls += 1
        raise AssertionError("unresolved account ID must stop before DB/seam")

    result = await support_reserve_net_consume_impl(
        payload,
        session_factory=forbidden_session_factory,
    )

    assert result["success"] is False
    assert result["error"] in {
        "invalid_reserve_net_request",
        "broker_account_id_normalization_unavailable",
    }
    assert result["proposal_count"] == 0
    assert result["proposals_created"] == []
    assert session_factory_calls == 0


@pytest.mark.asyncio
async def test_missing_account_id_opens_no_session_and_creates_zero() -> None:
    payload = _request()
    payload["candidates"][0].pop("broker_account_id")
    session_factory_calls = 0

    def forbidden_session_factory() -> _FakeSession:
        nonlocal session_factory_calls
        session_factory_calls += 1
        raise AssertionError("missing account ID must stop before DB/seam")

    result = await support_reserve_net_consume_impl(
        payload,
        session_factory=forbidden_session_factory,
    )

    assert result["success"] is False
    assert result["error"] == "invalid_reserve_net_request"
    assert result["proposal_count"] == 0
    assert result["proposals_created"] == []
    assert session_factory_calls == 0


@pytest.mark.asyncio
async def test_session_open_failure_returns_fixed_zero_create_boundary() -> None:
    class SessionOpenFailure(AbstractAsyncContextManager):
        async def __aenter__(self):
            raise RuntimeError("injected session-open failure")

        async def __aexit__(self, *args: Any) -> None:
            return None

    result = await support_reserve_net_consume_impl(
        _request(),
        session_factory=SessionOpenFailure,
    )

    assert result == {
        "success": False,
        "error": "proposal_session_unavailable",
        "proposal_count": 0,
        "proposals_created": [],
    }


@pytest.mark.asyncio
async def test_account_alias_mismatch_is_not_guessed() -> None:
    payload = _request(_candidate(broker_account_id="ACCT-EXACT-1"))
    session_factory_calls = 0

    def forbidden_session_factory() -> _FakeSession:
        nonlocal session_factory_calls
        session_factory_calls += 1
        raise AssertionError("ambiguous account IDs must stop before DB/seam")

    result = await support_reserve_net_consume_impl(
        payload,
        session_factory=forbidden_session_factory,
    )

    assert result == {
        "success": False,
        "error": "broker_account_id_normalization_unavailable",
        "proposal_creation_status": "not_attempted_account_id_unavailable",
        "proposal_count": 0,
        "proposals_created": [],
    }
    assert session_factory_calls == 0


@pytest.mark.asyncio
async def test_stale_cash_and_missing_seam_both_fail_closed() -> None:
    stale = _request()
    stale["cash_snapshots"][0]["is_fresh"] = False
    stale_session = _FakeSession()

    class IncompleteService:
        pass

    stale_result = await support_reserve_net_consume_impl(
        stale,
        session_factory=lambda: stale_session,
        service_factory=lambda _: IncompleteService(),
    )
    assert stale_result["proposal_count"] == 0
    assert stale_result["plan"]["rejected"][0]["code"] == ("cash_snapshot_unavailable")

    seam_session = _FakeSession()
    seam_result = await support_reserve_net_consume_impl(
        _request(),
        session_factory=lambda: seam_session,
        service_factory=lambda _: IncompleteService(),
    )
    assert seam_result["proposal_count"] == 0
    assert seam_result["proposal_creation_status"] == (
        "atomic_self_open_order_read_seam_unavailable"
    )
    assert seam_result["plan"]["proposal_creation_permitted"] is False
    assert stale_session.rollback_calls == 1
    assert seam_session.rollback_calls == 1


@pytest.mark.asyncio
async def test_sector_cap_excess_is_returned_as_advisory_in_mcp_plan() -> None:
    """The response remains observable even when the candidate is selected."""
    payload = _request()
    payload["candidates"][0]["post_fill_sector_concentration_pct"] = "10.01"
    session = _FakeSession()

    class IncompleteService:
        pass

    result = await support_reserve_net_consume_impl(
        payload,
        session_factory=lambda: session,
        service_factory=lambda _: IncompleteService(),
    )

    assert result["success"] is True
    assert result["plan"]["selected"][0]["normalized_symbol"] == "WIRE-A"
    assert result["plan"]["sector_cluster_cap_advisories"] == [
        {
            "normalized_symbol": "WIRE-A",
            "intent": "new",
            "sector_cluster": "software",
            "post_fill_sector_concentration_pct": "10.01",
            "sector_cluster_cap_pct": "10",
            "post_fill_sector_increase": "0.01",
            "code": "sector_cluster_cap_exceeded",
        }
    ]
    assert session.rollback_calls == 1


@pytest.mark.asyncio
async def test_submissions_frozen_creates_zero_without_seam_use() -> None:
    payload = _request()
    payload["submissions_frozen"] = True
    session = _FakeSession()

    class SeamMustNotRun:
        async def inspect_watch_to_order_scope(self, **kwargs: Any) -> Any:
            raise AssertionError("freeze must stop before seam inspection")

        async def create_proposal_in_watch_to_order_scope(
            self, inspection: Any, **kwargs: Any
        ) -> Any:
            raise AssertionError("freeze must stop before proposal creation")

    result = await support_reserve_net_consume_impl(
        payload,
        session_factory=lambda: session,
        service_factory=lambda _: SeamMustNotRun(),
    )

    assert result["proposal_count"] == 0
    assert result["proposal_creation_status"] == (
        "not_attempted_no_selected_candidates"
    )
    assert session.commit_calls == 0
    assert session.rollback_calls == 1


@pytest.mark.asyncio
async def test_missing_submissions_frozen_opens_no_session_and_creates_zero() -> None:
    payload = _request()
    payload.pop("submissions_frozen")
    consumer_factory_calls = 0
    session_factory_calls = 0

    def forbidden_consumer_factory() -> SupportReserveNetConsumer:
        nonlocal consumer_factory_calls
        consumer_factory_calls += 1
        raise AssertionError("missing freeze evidence must stop before consumer")

    def forbidden_session_factory() -> _FakeSession:
        nonlocal session_factory_calls
        session_factory_calls += 1
        raise AssertionError("missing freeze evidence must stop before DB/seam")

    result = await support_reserve_net_consume_impl(
        payload,
        consumer_factory=forbidden_consumer_factory,
        session_factory=forbidden_session_factory,
    )

    assert result == {
        "success": False,
        "error": "submissions_frozen_evidence_required",
        "proposal_creation_status": (
            "not_attempted_submissions_frozen_evidence_unavailable"
        ),
        "proposal_count": 0,
        "proposals_created": [],
    }
    assert consumer_factory_calls == 0
    assert session_factory_calls == 0


@pytest.mark.parametrize(
    "record_group",
    [
        "candidates",
        "reserve_net_attributions",
        "self_unfilled_orders",
        "sector_exposures",
    ],
)
@pytest.mark.parametrize(
    ("canonical_owner_id", "drifted_owner_id"),
    [
        pytest.param("owner-1", "OWNER-1", id="case-upper"),
        pytest.param("owner-1", " owner-1", id="leading-space"),
        pytest.param("owner-1", "owner-1 ", id="trailing-space"),
        pytest.param("owner-1", "owner_1", id="separator-underscore"),
        pytest.param("owner-1", "", id="empty"),
        pytest.param("owner-1", "   ", id="whitespace-only"),
        pytest.param("owner-1", "\xa0owner-1", id="leading-nbsp"),
        pytest.param("owner-1", "\u200bowner-1", id="leading-zero-width-space"),
        pytest.param("owner-1", "\towner-1", id="leading-tab"),
        pytest.param("owner-1", "owner-1\n", id="trailing-newline"),
        pytest.param("owner-1", "owner.1", id="separator-dot"),
        pytest.param("owner-1", "owner 1", id="separator-space"),
        pytest.param("owner-1", "owner-01", id="alnum-alias"),
        pytest.param("owner-1", "owner‑1", id="nonbreaking-hyphen"),
        pytest.param("owner-1", "Owner-1", id="case-title"),
        pytest.param("owner-1", "ownér-1", id="accented-alias"),
        pytest.param("ownér-1", "owne\u0301r-1", id="unicode-nfc-nfd"),
    ],
)
@pytest.mark.asyncio
async def test_owner_id_drift_in_each_record_group_stops_before_db_and_seam(
    record_group: str,
    canonical_owner_id: str,
    drifted_owner_id: str,
) -> None:
    payload = _request_with_all_owner_record_groups()
    for owner_record_group in (
        "candidates",
        "reserve_net_attributions",
        "self_unfilled_orders",
        "sector_exposures",
    ):
        for record in payload[owner_record_group]:
            record["beneficial_owner_id"] = canonical_owner_id
    payload[record_group][1]["beneficial_owner_id"] = drifted_owner_id
    consumer_factory_calls = 0
    session_factory_calls = 0
    service_factory_calls = 0

    def forbidden_consumer_factory() -> SupportReserveNetConsumer:
        nonlocal consumer_factory_calls
        consumer_factory_calls += 1
        raise AssertionError("owner drift must stop before consumer")

    def forbidden_session_factory() -> _FakeSession:
        nonlocal session_factory_calls
        session_factory_calls += 1
        raise AssertionError("owner drift must stop before DB")

    def forbidden_service_factory(_: Any) -> Any:
        nonlocal service_factory_calls
        service_factory_calls += 1
        raise AssertionError("owner drift must stop before seam service")

    result = await support_reserve_net_consume_impl(
        payload,
        consumer_factory=forbidden_consumer_factory,
        session_factory=forbidden_session_factory,
        service_factory=forbidden_service_factory,
    )

    assert result == {
        "success": False,
        "error": "beneficial_owner_id_normalization_unavailable",
        "proposal_creation_status": ("not_attempted_beneficial_owner_id_unavailable"),
        "proposal_count": 0,
        "proposals_created": [],
    }
    assert consumer_factory_calls == 0
    assert session_factory_calls == 0
    assert service_factory_calls == 0


def _request_with_join_key_drift(
    join_key: str,
    drifted_value: str,
) -> dict[str, Any]:
    payload = _request()
    if join_key == "side":
        payload["self_unfilled_orders"] = [
            _self_unfilled_order("WIRE-OTHER", side="sell"),
            _self_unfilled_order("WIRE-A", side=drifted_value),
        ]
    elif join_key == "strategy":
        payload["reserve_net_attributions"] = [
            _attribution("WIRE-OTHER", strategy="buy.unrelated"),
            _attribution(
                "WIRE-A",
                strategy=drifted_value,
                state="armed",
            ),
        ]
    else:
        assert join_key == "sector_cluster"
        payload["sector_exposures"] = [
            _sector_exposure("WIRE-HARDWARE", sector="hardware"),
            _sector_exposure("WIRE-SECTOR-BLOCK", sector=drifted_value),
        ]
    return payload


@pytest.mark.parametrize(
    ("join_key", "drifted_value", "expected_error"),
    [
        pytest.param(
            "side",
            "BUY",
            "self_unfilled_side_contract_unavailable",
            id="side-case-upper",
        ),
        pytest.param(
            "side",
            "Buy",
            "self_unfilled_side_contract_unavailable",
            id="side-case-title",
        ),
        pytest.param(
            "side",
            " buy",
            "self_unfilled_side_contract_unavailable",
            id="side-leading-space",
        ),
        pytest.param(
            "side",
            "buy ",
            "self_unfilled_side_contract_unavailable",
            id="side-trailing-space",
        ),
        pytest.param(
            "side",
            "hold",
            "self_unfilled_side_contract_unavailable",
            id="side-outside-literal",
        ),
        pytest.param(
            "strategy",
            "BUY.SUPPORT_RESERVE_NET",
            "reserve_net_strategy_contract_unavailable",
            id="strategy-case",
        ),
        pytest.param(
            "strategy",
            "buy.support-reserve-net",
            "reserve_net_strategy_contract_unavailable",
            id="strategy-separator",
        ),
        pytest.param(
            "strategy",
            " buy.support_reserve_net",
            "reserve_net_strategy_contract_unavailable",
            id="strategy-leading-space",
        ),
        pytest.param(
            "strategy",
            "buy.support_reserve_net ",
            "reserve_net_strategy_contract_unavailable",
            id="strategy-trailing-space",
        ),
        pytest.param(
            "sector_cluster",
            "Software",
            "sector_cluster_normalization_unavailable",
            id="sector-case-title",
        ),
        pytest.param(
            "sector_cluster",
            "SOFTWARE",
            "sector_cluster_normalization_unavailable",
            id="sector-case-upper",
        ),
        pytest.param(
            "sector_cluster",
            " software",
            "sector_cluster_normalization_unavailable",
            id="sector-leading-space",
        ),
        pytest.param(
            "sector_cluster",
            "software ",
            "sector_cluster_normalization_unavailable",
            id="sector-trailing-space",
        ),
        pytest.param(
            "sector_cluster",
            "soft_ware",
            "sector_cluster_normalization_unavailable",
            id="sector-separator",
        ),
    ],
)
@pytest.mark.asyncio
async def test_non_owner_join_key_drift_stops_before_db_and_seam(
    join_key: str,
    drifted_value: str,
    expected_error: str,
) -> None:
    payload = _request_with_join_key_drift(join_key, drifted_value)
    consumer_factory_calls = 0
    session_factory_calls = 0
    service_factory_calls = 0

    def forbidden_consumer_factory() -> SupportReserveNetConsumer:
        nonlocal consumer_factory_calls
        consumer_factory_calls += 1
        raise AssertionError("join-key drift must stop before consumer")

    def forbidden_session_factory() -> _FakeSession:
        nonlocal session_factory_calls
        session_factory_calls += 1
        raise AssertionError("join-key drift must stop before DB")

    def forbidden_service_factory(_: Any) -> Any:
        nonlocal service_factory_calls
        service_factory_calls += 1
        raise AssertionError("join-key drift must stop before seam service")

    result = await support_reserve_net_consume_impl(
        payload,
        consumer_factory=forbidden_consumer_factory,
        session_factory=forbidden_session_factory,
        service_factory=forbidden_service_factory,
    )

    assert result["success"] is False
    assert result["error"] == expected_error
    assert result["proposal_count"] == 0
    assert result["proposals_created"] == []
    assert consumer_factory_calls == 0
    assert session_factory_calls == 0
    assert service_factory_calls == 0


class _StatefulSeamService:
    def __init__(self) -> None:
        self.groups: dict[uuid.UUID, Any] = {}
        self.active: dict[tuple[str, str, str, str | None, str], Any] = {}
        self.inspected_account_ids: list[str | None] = []
        self.create_calls = 0

    async def inspect_watch_to_order_scope(self, **kwargs: Any) -> Any:
        scope = WatchToOrderScope(**kwargs)
        key = (
            scope.symbol,
            scope.market,
            scope.account_mode,
            scope.broker_account_id,
            scope.action,
        )
        self.inspected_account_ids.append(scope.broker_account_id)
        active = self.active.get(key)
        return WatchToOrderScopeInspection(
            scope=scope,
            lock_acquired=True,
            active_groups=(active,) if active is not None else (),
        )

    async def create_proposal_in_watch_to_order_scope(
        self, inspection: WatchToOrderScopeInspection, **kwargs: Any
    ) -> Any:
        self.create_calls += 1
        group = SimpleNamespace(
            proposal_id=uuid.uuid4(),
            lifecycle_state="pending_approval",
            action="place",
            target_broker_order_id=None,
            valid_until=None,
            account_mode=kwargs["account_mode"],
            side=kwargs["side"],
            broker_account_id=kwargs["broker_account_id"],
            market=kwargs["market"],
        )
        rung_input = kwargs["rungs"][0]
        rung = SimpleNamespace(
            rung_index=rung_input.rung_index,
            side=rung_input.side,
            quantity=rung_input.quantity,
            limit_price=rung_input.limit_price,
            notional=rung_input.notional,
            state="pending_approval",
            broker_order_id=None,
            correlation_id=None,
        )
        self.groups[group.proposal_id] = (group, [rung])
        scope = inspection.scope
        key = (
            scope.symbol,
            scope.market,
            scope.account_mode,
            scope.broker_account_id,
            scope.action,
        )
        self.active[key] = object()
        return group

    async def get_proposal(self, proposal_id: uuid.UUID) -> tuple[Any, list[Any]]:
        return self.groups[proposal_id]


async def _complete_for_test(
    committed: dict[str, Any], **kwargs: Any
) -> dict[str, Any]:
    return {**committed, "approval_dispatch": {"state": "test_only"}}


def _same_owner_multiple_records_payload() -> dict[str, Any]:
    payload = _request(
        _candidate("WIRE-MULTI-A", sector="software"),
        _candidate("WIRE-MULTI-B", sector="hardware"),
    )
    payload["reserve_net_attributions"] = [
        _attribution("WIRE-HISTORY-A", strategy="buy.unrelated"),
        _attribution("WIRE-HISTORY-B", strategy="sell.unrelated", sector="finance"),
    ]
    payload["self_unfilled_orders"] = [
        _self_unfilled_order("WIRE-OPEN-A", side="buy"),
        _self_unfilled_order("WIRE-OPEN-B", side="sell"),
    ]
    payload["sector_exposures"] = [
        _sector_exposure("WIRE-FINANCE", sector="finance"),
        _sector_exposure("WIRE-ENERGY", sector="energy"),
    ]
    return payload


def _distinct_join_vocabularies_payload() -> dict[str, Any]:
    payload = _request()
    payload["reserve_net_attributions"] = [
        _attribution("WIRE-HISTORY-A", strategy="buy.unrelated"),
        _attribution("WIRE-HISTORY-B", strategy="sell.unrelated"),
    ]
    payload["self_unfilled_orders"] = [
        _self_unfilled_order("WIRE-OPEN-A", side="buy"),
        _self_unfilled_order("WIRE-OPEN-B", side="sell"),
    ]
    payload["sector_exposures"] = [
        _sector_exposure("WIRE-HARDWARE", sector="hardware"),
        _sector_exposure("WIRE-FINANCE", sector="finance"),
    ]
    return payload


@pytest.mark.parametrize(
    ("payload_factory", "expected_count"),
    [
        pytest.param(
            _same_owner_multiple_records_payload,
            2,
            id="same-owner-multiple-records",
        ),
        pytest.param(
            _distinct_join_vocabularies_payload,
            1,
            id="distinct-valid-join-vocabularies",
        ),
    ],
)
@pytest.mark.asyncio
async def test_legitimate_multi_record_join_keys_commit_without_over_tightening(
    payload_factory: Callable[[], dict[str, Any]],
    expected_count: int,
) -> None:
    service = _StatefulSeamService()
    session = _FakeSession()

    result = await support_reserve_net_consume_impl(
        payload_factory(),
        session_factory=lambda: session,
        service_factory=lambda _: service,
        complete_committed_create=_complete_for_test,
    )

    assert result["success"] is True
    assert result["proposal_creation_status"] == "created_after_atomic_seam"
    assert result["proposal_count"] == expected_count
    assert service.create_calls == expected_count
    assert session.commit_calls == 1
    assert session.rollback_calls == 0


@pytest.mark.parametrize(
    ("record_group", "record"),
    [
        pytest.param("candidates", None, id="candidates"),
        pytest.param(
            "reserve_net_attributions",
            _attribution(),
            id="reserve-net-attributions",
        ),
        pytest.param(
            "self_unfilled_orders",
            _self_unfilled_order(),
            id="self-unfilled-orders",
        ),
        pytest.param(
            "sector_exposures",
            _sector_exposure(),
            id="sector-exposures",
        ),
    ],
)
@pytest.mark.asyncio
async def test_exact_owner_id_in_each_record_group_commits_one_proposal(
    record_group: str,
    record: dict[str, Any] | None,
) -> None:
    payload = _request()
    if record is not None:
        payload[record_group] = [record]
    service = _StatefulSeamService()
    session = _FakeSession()

    result = await support_reserve_net_consume_impl(
        payload,
        session_factory=lambda: session,
        service_factory=lambda _: service,
        complete_committed_create=_complete_for_test,
    )

    assert result["success"] is True
    assert result["proposal_creation_status"] == "created_after_atomic_seam"
    assert result["proposal_count"] == 1
    assert service.create_calls == 1
    assert session.commit_calls == 1
    assert session.rollback_calls == 0


@pytest.mark.asyncio
async def test_two_consecutive_calls_create_only_one_active_proposal() -> None:
    service = _StatefulSeamService()
    sessions: list[_FakeSession] = []

    def session_factory() -> _FakeSession:
        session = _FakeSession()
        sessions.append(session)
        return session

    first = await support_reserve_net_consume_impl(
        _request(),
        session_factory=session_factory,
        service_factory=lambda _: service,
        complete_committed_create=_complete_for_test,
    )
    second = await support_reserve_net_consume_impl(
        _request(),
        session_factory=session_factory,
        service_factory=lambda _: service,
        complete_committed_create=_complete_for_test,
    )

    assert first["proposal_creation_status"] == "created_after_atomic_seam"
    assert first["proposal_count"] == 1
    assert second["proposal_creation_status"] == (
        "watch_to_order_scope_active_groups_present"
    )
    assert second["proposal_count"] == 0
    assert service.create_calls == 1
    assert service.inspected_account_ids == [None, "acct-exact-1", None, "acct-exact-1"]
    assert sessions[0].commit_calls == 1
    assert sessions[1].rollback_calls == 1


@pytest.mark.asyncio
async def test_selected_candidate_create_failure_rolls_back_whole_transaction(
    db_session,
    monkeypatch,
) -> None:
    first = _candidate(f"WIRE-PARTIAL-A-{uuid.uuid4().hex.upper()}")
    second = _candidate(
        f"WIRE-PARTIAL-B-{uuid.uuid4().hex.upper()}",
        sector="hardware",
    )
    service = OrderProposalsService(db_session)
    original_create = service.create_proposal_in_watch_to_order_scope
    create_calls = 0

    async def fail_second_create(inspection, **kwargs):
        nonlocal create_calls
        create_calls += 1
        if create_calls == 2:
            raise RuntimeError("injected second create failure")
        return await original_create(inspection, **kwargs)

    monkeypatch.setattr(
        service,
        "create_proposal_in_watch_to_order_scope",
        fail_second_create,
    )

    class BorrowedSession(AbstractAsyncContextManager):
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *args: Any) -> None:
            return None

    result = await support_reserve_net_consume_impl(
        _request(first, second),
        session_factory=BorrowedSession,
        service_factory=lambda _: service,
        complete_committed_create=_complete_for_test,
    )

    assert result == {
        "success": False,
        "error": "proposal_transaction_failed",
        "proposal_count": 0,
        "proposals_created": [],
    }
    assert create_calls == 2
    inspection = await service.inspect_watch_to_order_scope(
        symbol=first["normalized_symbol"],
        market="equity_kr",
        account_mode="kis_live",
        broker_account_id="acct-exact-1",
        action="place",
    )
    assert inspection.active_groups == ()


@pytest.mark.asyncio
async def test_post_commit_failure_is_per_proposal_and_cannot_undo_batch() -> None:
    service = _StatefulSeamService()
    session = _FakeSession()
    callback_calls = 0

    async def fail_second_dispatch(committed, **kwargs):
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls == 2:
            raise RuntimeError("injected post-commit failure")
        return await _complete_for_test(committed, **kwargs)

    result = await support_reserve_net_consume_impl(
        _request(
            _candidate("WIRE-DISPATCH-A"),
            _candidate("WIRE-DISPATCH-B", sector="hardware"),
        ),
        session_factory=lambda: session,
        service_factory=lambda _: service,
        complete_committed_create=fail_second_dispatch,
    )

    assert result["success"] is True
    assert result["proposal_count"] == 2
    assert session.commit_calls == 1
    assert session.rollback_calls == 0
    assert result["proposals_created"][0]["approval_dispatch"]["state"] == "test_only"
    assert result["proposals_created"][1]["approval_dispatch"] == {
        "state": "failed",
        "failure_code": "approval_dispatch_boundary_failed",
    }


@pytest.mark.asyncio
async def test_committed_create_uses_existing_post_commit_dispatch_path(
    monkeypatch,
) -> None:
    from app.mcp_server.tooling import order_proposal_tools

    service = _StatefulSeamService()
    session = _FakeSession()
    dispatch_calls: list[dict[str, Any]] = []

    async def record_existing_path(committed, **kwargs):
        dispatch_calls.append(kwargs)
        return await _complete_for_test(committed, **kwargs)

    monkeypatch.setattr(
        order_proposal_tools,
        "_complete_committed_proposal_create",
        record_existing_path,
    )

    result = await support_reserve_net_consume_impl(
        _request(),
        session_factory=lambda: session,
        service_factory=lambda _: service,
    )

    assert result["proposal_count"] == 1
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["normalized_action"] == "place"
    assert dispatch_calls[0]["side"] == "buy"
    assert dispatch_calls[0]["broker_account_id"] == "acct-exact-1"


@pytest.mark.asyncio
async def test_session_close_failure_after_commit_keeps_durable_success() -> None:
    service = _StatefulSeamService()

    class ExitFailureSession(_FakeSession):
        async def __aexit__(self, *args: Any) -> None:
            raise RuntimeError("injected session-close failure")

    session = ExitFailureSession()
    result = await support_reserve_net_consume_impl(
        _request(),
        session_factory=lambda: session,
        service_factory=lambda _: service,
        complete_committed_create=_complete_for_test,
    )

    assert session.commit_calls == 1
    assert result["success"] is True
    assert result["proposal_count"] == 1


@pytest.mark.asyncio
async def test_commit_acknowledgement_failure_reports_unknown_not_zero() -> None:
    service = _StatefulSeamService()

    class CommitUnknownSession(_FakeSession):
        async def commit(self) -> None:
            self.commit_calls += 1
            raise RuntimeError("injected commit acknowledgement failure")

    session = CommitUnknownSession()
    result = await support_reserve_net_consume_impl(
        _request(),
        session_factory=lambda: session,
        service_factory=lambda _: service,
        complete_committed_create=_complete_for_test,
    )

    assert result["success"] is False
    assert result["error"] == "proposal_commit_outcome_unknown"
    assert result["proposal_count"] is None
    assert result["proposals_created"] == []
    assert result["proposal_ids_maybe_committed"] == [str(next(iter(service.groups)))]
    assert session.commit_calls == 1
    assert session.rollback_calls == 1
