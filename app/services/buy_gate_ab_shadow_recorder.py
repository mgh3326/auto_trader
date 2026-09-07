"""Fail-open, pre-arming witness recorder for fanout buy candidates.

This observer lives outside ``buy_candidate_fanout`` so the discovery funnel
keeps its no-write contract.  Fanout cannot supply v2's account/disclosure
review bits; every row produced here is therefore a plumbing witness, not an
experiment sample.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.mcp_server.tooling.forecast_tools import forecast_save
from app.services.buy_gate_ab_shadow.evaluate_v2 import (
    CandidateEvidence,
    EvaluationError,
    evaluate_candidate,
)
from app.services.buy_gate_ab_shadow.forecast_tag_v2 import (
    build_v2_witness_forecasts,
)

logger = logging.getLogger(__name__)

_TRUE = frozenset({"1", "true", "yes", "on"})
MAX_FANOUT_WITNESS_CANDIDATES = 10
FANOUT_WITNESS_CREATED_BY = "buy_candidate_fanout_witness"


def env_gate_enabled() -> bool:
    """Call-time default-off gate for the observational recorder."""

    return (
        os.environ.get("BUY_GATE_AB_SHADOW_RECORD_ENABLED", "").strip().lower() in _TRUE
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_present(*values: object) -> object:
    for value in values:
        if value is not None:
            return value
    return None


def _fanout_evidence(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Project the returned fanout funnel into the v2 evaluator's input.

    The intentionally absent ``other_gate_bits`` field is not an omission in
    this adapter: fanout has no authority to supply liquid-midcap,
    concentration, or overhang review evidence.
    """

    funnel = _mapping(candidate.get("funnel"))
    base = _mapping(funnel.get("base_eligibility"))
    support = _mapping(funnel.get("support_source_count"))
    rsi = _mapping(funnel.get("rsi"))
    upside = _mapping(funnel.get("upside"))
    return {
        "symbol": candidate.get("symbol"),
        "market": "kr",
        "current_price": _first_present(
            base.get("current_price"),
            upside.get("current_price"),
            candidate.get("current_price"),
        ),
        "support_strength": support.get("strength"),
        "support_distance_pct": support.get("distance_pct"),
        "rsi": _first_present(rsi.get("rsi_14"), candidate.get("rsi_14")),
        "honest_upside_pct": _first_present(
            upside.get("honest_upside_pct"), candidate.get("honest_upside_pct")
        ),
        # Do not add an empty/pass-looking bit mapping here.  v2 evidence
        # treats every missing bit as False and labels this non-sample.
    }


async def maybe_record_buy_gate_ab_shadow(
    result: Mapping[str, Any],
    *,
    enabled: bool | None = None,
    save: Callable[..., Awaitable[Any]] | None = None,
    now: datetime | None = None,
) -> None:
    """Record witness rows when the env gate is on. Never raises. Never mutates ``result``."""

    try:
        if enabled is None:
            enabled = env_gate_enabled()
        if not enabled or not isinstance(result, Mapping):
            return
        raw_candidates = result.get("candidates")
        if not isinstance(raw_candidates, list):
            return
        if len(raw_candidates) > MAX_FANOUT_WITNESS_CANDIDATES:
            logger.warning(
                "buy-gate A/B v2 witness recorder truncating fanout candidates",
                extra={
                    "candidate_count": len(raw_candidates),
                    "recording_limit": MAX_FANOUT_WITNESS_CANDIDATES,
                },
            )
        evaluated_at = now or datetime.now(UTC)
        if evaluated_at.tzinfo is None:
            evaluated_at = evaluated_at.replace(tzinfo=UTC)
        writer = save or forecast_save
        for candidate in raw_candidates[:MAX_FANOUT_WITNESS_CANDIDATES]:
            if not isinstance(candidate, Mapping):
                continue
            try:
                evaluation = evaluate_candidate(
                    CandidateEvidence.from_mapping(_fanout_evidence(candidate)),
                    evaluation_as_of=evaluated_at,
                )
            except (EvaluationError, ValueError):
                logger.warning(
                    "buy-gate A/B v2 witness candidate could not be evaluated",
                    exc_info=True,
                )
                continue
            # Record every evaluable cohort.  Filtering to B-only would turn
            # missing shared bits into a permanent zero-row observer.
            for payload in build_v2_witness_forecasts(
                evaluation,
                created_by=FANOUT_WITNESS_CREATED_BY,
            ):
                await writer(**payload)
    except Exception:
        logger.warning(
            "buy-gate A/B v2 witness recording failed; fanout result is unchanged",
            exc_info=True,
        )


__all__ = [
    "FANOUT_WITNESS_CREATED_BY",
    "MAX_FANOUT_WITNESS_CANDIDATES",
    "env_gate_enabled",
    "maybe_record_buy_gate_ab_shadow",
]
