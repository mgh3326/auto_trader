"""Explicit MCP caller for the seam-gated support-reserve-net consumer.

The tool accepts an already assembled evidence packet.  It does not query a
broker or infer account identities.  Proposal persistence is one database
transaction; approval dispatch starts only after that transaction commits.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Final, Literal, TypeGuard

from pydantic import TypeAdapter, ValidationError

from app.core.db import AsyncSessionLocal
from app.services.order_proposals import OrderProposalsService
from app.services.support_reserve_net_consumer import (
    STRATEGY,
    ReserveNetConsumeResult,
    ReserveNetPlan,
    ReserveNetRequest,
    SupportReserveNetConsumer,
)

logger = logging.getLogger(__name__)

_REQUEST_ADAPTER = TypeAdapter(ReserveNetRequest)
_PLAN_ADAPTER = TypeAdapter(ReserveNetPlan)
_SELF_UNFILLED_SIDE_ADAPTER = TypeAdapter(Literal["buy", "sell"])


def _validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Return useful locations without echoing the caller's evidence values."""
    return [
        {
            "location": [str(part) for part in error["loc"]],
            "type": error["type"],
            "message": error["msg"],
        }
        for error in exc.errors(include_input=False, include_url=False)
    ]


def _is_exact_opaque_identifier(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _join_key_collision_fingerprint(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _has_no_ambiguous_exact_representations(values: list[object]) -> bool:
    seen: dict[str, str] = {}
    for value in values:
        if not _is_exact_opaque_identifier(value):
            return False
        fingerprint = _join_key_collision_fingerprint(value)
        previous = seen.setdefault(fingerprint, value)
        if previous != value:
            return False
    return True


def _has_exact_opaque_account_ids(request: ReserveNetRequest) -> bool:
    """Accept exact non-empty opaque IDs; never trim, case-fold, or alias-map."""
    account_ids = (
        [candidate.broker_account_id for candidate in request.candidates]
        + [snapshot.broker_account_id for snapshot in request.cash_snapshots]
        + [item.broker_account_id for item in request.reserve_net_attributions]
        + [item.broker_account_id for item in request.self_unfilled_orders]
    )
    return _has_no_ambiguous_exact_representations(account_ids)


def _has_exact_opaque_beneficial_owner_ids(request: ReserveNetRequest) -> bool:
    """Require one exact non-empty owner representation for the whole packet."""
    owner_ids = (
        [candidate.beneficial_owner_id for candidate in request.candidates]
        + [item.beneficial_owner_id for item in request.reserve_net_attributions]
        + [item.beneficial_owner_id for item in request.self_unfilled_orders]
        + [item.beneficial_owner_id for item in request.sector_exposures]
    )
    return (
        all(_is_exact_opaque_identifier(owner_id) for owner_id in owner_ids)
        and len(set(owner_ids)) <= 1
    )


def _has_exact_self_unfilled_sides(request: ReserveNetRequest) -> bool:
    """Accept only the consumer's exact lower-case side vocabulary."""
    try:
        for order in request.self_unfilled_orders:
            _SELF_UNFILLED_SIDE_ADAPTER.validate_python(order.side)
    except ValidationError:
        return False
    return True


def _has_exact_reserve_net_strategy_ids(request: ReserveNetRequest) -> bool:
    """Reject any non-canonical spelling that could mean reserve-net."""
    target = _join_key_collision_fingerprint(STRATEGY)
    for attribution in request.reserve_net_attributions:
        value = attribution.strategy
        if not _is_exact_opaque_identifier(value):
            return False
        if _join_key_collision_fingerprint(value) == target and value != STRATEGY:
            return False
    return True


def _has_exact_sector_clusters(request: ReserveNetRequest) -> bool:
    """Reject ambiguous sector spellings within each owner and market scope."""
    seen: dict[tuple[str, str, str], str] = {}
    items = (*request.candidates, *request.sector_exposures)
    for item in items:
        value = item.sector_cluster
        if not _is_exact_opaque_identifier(value):
            return False
        key = (
            item.beneficial_owner_id,
            item.market,
            _join_key_collision_fingerprint(value),
        )
        previous = seen.setdefault(key, value)
        if previous != value:
            return False
    return True


_JOIN_KEY_BOUNDARY_GUARDS: Final = MappingProxyType(
    {
        "broker_account_id": (
            _has_exact_opaque_account_ids,
            "broker_account_id_normalization_unavailable",
            "not_attempted_account_id_unavailable",
        ),
        "beneficial_owner_id": (
            _has_exact_opaque_beneficial_owner_ids,
            "beneficial_owner_id_normalization_unavailable",
            "not_attempted_beneficial_owner_id_unavailable",
        ),
        "side": (
            _has_exact_self_unfilled_sides,
            "self_unfilled_side_contract_unavailable",
            "not_attempted_self_unfilled_side_unavailable",
        ),
        "strategy": (
            _has_exact_reserve_net_strategy_ids,
            "reserve_net_strategy_contract_unavailable",
            "not_attempted_reserve_net_strategy_unavailable",
        ),
        "sector_cluster": (
            _has_exact_sector_clusters,
            "sector_cluster_normalization_unavailable",
            "not_attempted_sector_cluster_unavailable",
        ),
    }
)

RESERVE_NET_JOIN_KEY_CENSUS: Final = frozenset(
    {
        "account_mode",
        "beneficial_owner_id",
        "broker_account_id",
        "currency",
        "intent",
        "market",
        "normalized_symbol",
        "sector_cluster",
        "side",
        "state",
        "strategy",
    }
)

RESERVE_NET_JOIN_KEY_PROTECTION_PARTITIONS: Final = MappingProxyType(
    {
        "mcp_boundary": frozenset(_JOIN_KEY_BOUNDARY_GUARDS),
        "pydantic_literal": frozenset({"intent", "market", "state"}),
        "shared_normalization": frozenset({"normalized_symbol"}),
        "fail_closed_join_miss": frozenset({"account_mode", "currency"}),
    }
)


async def _complete_committed_create(
    committed_result: Mapping[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    # Imported lazily because order_proposal_tools owns registration and imports
    # this module.  The existing helper is the one authoritative post-commit
    # classification/approval path; reserve-net provenance is not an input.
    from app.mcp_server.tooling.order_proposal_tools import (
        _complete_committed_proposal_create,
    )

    return await _complete_committed_proposal_create(committed_result, **kwargs)


def _committed_snapshot(group: Any, rungs: list[Any]) -> dict[str, Any]:
    return {
        "success": True,
        "proposal_id": str(group.proposal_id),
        "lifecycle_state": group.lifecycle_state,
        "action": group.action or "place",
        "target_broker_order_id": group.target_broker_order_id,
        "valid_until": group.valid_until.isoformat() if group.valid_until else None,
        "rungs": [
            {
                "rung_index": rung.rung_index,
                "side": rung.side,
                "quantity": str(rung.quantity),
                "limit_price": (
                    str(rung.limit_price) if rung.limit_price is not None else None
                ),
                "notional": str(rung.notional) if rung.notional is not None else None,
                "state": rung.state,
                "broker_order_id": rung.broker_order_id,
                "correlation_id": rung.correlation_id,
            }
            for rung in rungs
        ],
    }


async def _rollback(session: Any, *, event: str) -> bool:
    try:
        await session.rollback()
        return True
    except Exception as exc:  # noqa: BLE001 - session close is the last fallback
        logger.error(event, extra={"exception_type": type(exc).__name__})
        return False


async def support_reserve_net_consume_impl(
    request: dict[str, Any],
    *,
    session_factory: Callable[[], Any] = AsyncSessionLocal,
    consumer_factory: Callable[[], SupportReserveNetConsumer] | None = None,
    service_factory: Callable[[Any], Any] = OrderProposalsService,
    complete_committed_create: Callable[..., Any] = _complete_committed_create,
) -> dict[str, Any]:
    """Validate evidence, call ``consume()``, commit, then dispatch approvals."""
    if "submissions_frozen" not in request:
        return {
            "success": False,
            "error": "submissions_frozen_evidence_required",
            "proposal_creation_status": (
                "not_attempted_submissions_frozen_evidence_unavailable"
            ),
            "proposal_count": 0,
            "proposals_created": [],
        }

    try:
        parsed_request = _REQUEST_ADAPTER.validate_python(request)
    except ValidationError as exc:
        return {
            "success": False,
            "error": "invalid_reserve_net_request",
            "validation_errors": _validation_errors(exc),
            "proposal_count": 0,
            "proposals_created": [],
        }

    # Join-key guards never repair caller evidence.  Any invalid or ambiguous
    # representation stops before consumer construction, DB, or seam access.
    for guard, error, status in _JOIN_KEY_BOUNDARY_GUARDS.values():
        if not guard(parsed_request):
            return {
                "success": False,
                "error": error,
                "proposal_creation_status": status,
                "proposal_count": 0,
                "proposals_created": [],
            }

    make_consumer = consumer_factory or SupportReserveNetConsumer.from_current_policy
    try:
        consumer = make_consumer()
    except Exception as exc:  # noqa: BLE001 - fixed non-sensitive MCP boundary
        logger.error(
            "support_reserve_net_consume.consumer_unavailable",
            extra={"exception_type": type(exc).__name__},
        )
        return {
            "success": False,
            "error": "reserve_net_consumer_unavailable",
            "proposal_count": 0,
            "proposals_created": [],
        }

    result: ReserveNetConsumeResult | None = None
    pending_dispatch: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
    transaction_committed = False
    try:
        async with session_factory() as session:
            service = service_factory(session)
            try:
                result = await consumer.consume(
                    parsed_request,
                    proposal_creator=service,
                )
                for group in result.proposals_created:
                    _, rungs = await service.get_proposal(group.proposal_id)
                    pending_dispatch.append(
                        (
                            {
                                "proposal_id": group.proposal_id,
                                "superseded_message": None,
                                "normalized_action": group.action or "place",
                                "account_mode": group.account_mode,
                                "side": group.side,
                                "broker_account_id": group.broker_account_id,
                                "market": group.market,
                            },
                            MappingProxyType(_committed_snapshot(group, rungs)),
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - transaction must fail closed
                await _rollback(
                    session,
                    event="support_reserve_net_consume.rollback_after_failure_failed",
                )
                logger.error(
                    "support_reserve_net_consume.transaction_failed",
                    extra={"exception_type": type(exc).__name__},
                )
                return {
                    "success": False,
                    "error": "proposal_transaction_failed",
                    "proposal_count": 0,
                    "proposals_created": [],
                }

            if not pending_dispatch:
                # No proposal row may remain.  Rollback also releases every
                # transaction-scoped advisory lock acquired by seam inspection.
                if not await _rollback(
                    session,
                    event="support_reserve_net_consume.read_only_rollback_failed",
                ):
                    return {
                        "success": False,
                        "error": "proposal_transaction_cleanup_failed",
                        "proposal_count": 0,
                        "proposals_created": [],
                    }
            else:
                try:
                    await session.commit()
                    transaction_committed = True
                except Exception as exc:  # noqa: BLE001 - outcome is uncertain
                    await _rollback(
                        session,
                        event=(
                            "support_reserve_net_consume.rollback_after_commit_failed"
                        ),
                    )
                    logger.error(
                        "support_reserve_net_consume.commit_outcome_unknown",
                        extra={"exception_type": type(exc).__name__},
                    )
                    return {
                        "success": False,
                        "error": "proposal_commit_outcome_unknown",
                        "proposal_count": None,
                        "proposals_created": [],
                        "proposal_ids_maybe_committed": [
                            str(dispatch_args["proposal_id"])
                            for dispatch_args, _ in pending_dispatch
                        ],
                    }
    except Exception as exc:  # noqa: BLE001 - fixed non-sensitive MCP boundary
        logger.error(
            "support_reserve_net_consume.session_boundary_failed",
            extra={
                "commit_succeeded": transaction_committed,
                "exception_type": type(exc).__name__,
            },
        )
        if not transaction_committed:
            return {
                "success": False,
                "error": "proposal_session_unavailable",
                "proposal_count": 0,
                "proposals_created": [],
            }

    if result is None:
        logger.error("support_reserve_net_consume.result_unavailable")
        return {
            "success": False,
            "error": "proposal_result_unavailable",
            "proposal_count": 0,
            "proposals_created": [],
        }

    completed: list[dict[str, Any]] = []
    for dispatch_args, committed in pending_dispatch:
        try:
            completed.append(
                await complete_committed_create(
                    committed,
                    **dispatch_args,
                )
            )
        except Exception as exc:  # noqa: BLE001 - proposal is already durable
            logger.error(
                "support_reserve_net_consume.post_commit_boundary_failed",
                extra={
                    "proposal_id": str(dispatch_args["proposal_id"]),
                    "exception_type": type(exc).__name__,
                },
            )
            fallback = dict(committed)
            fallback["approval_dispatch"] = {
                "state": "failed",
                "failure_code": "approval_dispatch_boundary_failed",
            }
            completed.append(fallback)

    return {
        "success": True,
        "proposal_creation_status": result.proposal_creation_status,
        "proposal_creation_call_site": result.proposal_creation_call_site,
        "plan": _PLAN_ADAPTER.dump_python(result.plan, mode="json"),
        "proposal_count": len(completed),
        "proposals_created": completed,
    }


async def support_reserve_net_consume(request: dict[str, Any]) -> dict[str, Any]:
    """Consume one fully evidenced reserve-net packet.

    Account and beneficial-owner IDs are exact opaque identifiers.  Distinct
    owners may coexist, but ambiguous spellings of one owner fail closed.
    Self-unfilled sides use the exact ``buy``/``sell`` vocabulary; a spelling
    that could mean the reserve-net strategy must equal its canonical value;
    sector spellings may not conflict within one owner/market scope.  The tool
    never repairs aliases, case, whitespace, or separators.  It performs no
    broker/account reads: the calling session owns evidence freshness and
    completeness, and must supply ``submissions_frozen`` explicitly.  Proposal
    creation is one atomic DB transaction; existing approval classification
    runs only after the commit and keeps its own default-off/live safety gates.
    """
    return await support_reserve_net_consume_impl(request)


__all__ = [
    "RESERVE_NET_JOIN_KEY_CENSUS",
    "RESERVE_NET_JOIN_KEY_PROTECTION_PARTITIONS",
    "support_reserve_net_consume",
    "support_reserve_net_consume_impl",
]
