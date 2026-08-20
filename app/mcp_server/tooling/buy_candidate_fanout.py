"""Bounded, observation-only multi-source buy-candidate discovery.

This module intentionally stops before any proposal, broker, account, or
database-write surface.  It widens the *discovery* population only; all
regular gates remain literal policy gates and the budget gate is fail-closed
because this read-only surface never obtains account evidence.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.core.symbol import to_db_symbol
from app.services.analyst_normalizer import (
    consensus_has_stale_window_inputs,
    stale_window_input_count,
)
from app.services.trading_policy_service import (
    load_trading_policy,
    policy_version_stamp,
)

# The batch revalidator accepts at most ten symbols.  Keeping every source at
# this same bound makes both source collection and fresh revalidation bounded.
TOP_N_PER_SOURCE = 10
TOP_N_REVALIDATION = 10
MAX_SNAPSHOT_PRESETS_PER_CALL = 5
SNAPSHOT_MAX_STALE_SESSIONS = 1

# Eligibility requires a named, top-level full-analysis freshness result.  A
# compact/legacy payload that omits this field is neither fresh nor stale: it is
# explicitly unprovable and may only continue through the observation funnel.
_FRESHNESS_EVIDENCE_SOURCE = "full_analysis_top_level_data_state"
_KNOWN_NOT_FRESH_DATA_STATES = frozenset(
    {"stale", "partial", "degraded", "missing", "error", "fallback", "halted_suspect"}
)


@dataclass(frozen=True, slots=True)
class _LiveSource:
    source: str
    sort_by: Literal["rsi", "change_rate", "trade_amount"]
    sort_order: Literal["asc", "desc"]


_LIVE_SOURCES: tuple[_LiveSource, ...] = (
    # RSI is deliberately a sort only.  Do not add max_rsi here.
    _LiveSource("rsi", "rsi", "asc"),
    _LiveSource("change_rate", "change_rate", "asc"),
    _LiveSource("trade_amount", "trade_amount", "desc"),
)

_SNAPSHOT_SOURCE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "snapshot_support_flow",
        (
            "support_proximity",
            "investor_flow_momentum",
            "double_buy",
            "stable_growth",
            "undervalued_growth",
        ),
    ),
    (
        "snapshot_value_catalyst",
        (
            "cheap_value",
            "high_yield_value",
            "undervalued_breakout",
            "profitable_company",
            "growth_expectation_toss",
        ),
    ),
)

_FUNNEL_STAGE_NAMES: tuple[str, ...] = (
    "source",
    "base_eligibility",
    "support_source_count",
    "upside",
    "rsi",
    "anchor_band",
    "budget",
)

_SUPPORT_FAMILY_ALIASES: tuple[tuple[str, str], ...] = (
    ("fib_", "fib"),
    ("bb_lower", "bb_lower"),
    ("volume_", "volume_profile"),
)

_LiveReader = Callable[[_LiveSource, str, int], Awaitable[dict[str, Any]]]
_SnapshotReader = Callable[
    [str, tuple[str, ...], str, int], Awaitable[list[dict[str, Any]]]
]
_FreshRevalidator = Callable[[list[str], str], Awaitable[dict[str, dict[str, Any]]]]
_PolicyLoader = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class _FanoutGates:
    """Immutable literal gate view read from the authoritative policy."""

    rsi_max: float
    support_source_count_min: int
    support_within_current_pct_max: float
    honest_upside_pct_min: float
    support_strength_min: str
    support_families: tuple[str, ...]
    discount_below_support_pct_range: tuple[float, float]
    final_limit_distance_from_current_pct_range: tuple[float, float]
    all_pending_buy_required_cash_hard_cap_pct: int
    tier_armed_required_cash_cap_pct: int

    @classmethod
    def from_policy(cls, policy: Any) -> _FanoutGates:
        threshold = policy.thresholds["screen.rsi_max"]
        reserve = policy.decision_rules["buy.support_reserve_net"]
        gates = cls(
            rsi_max=float(threshold.value),
            support_source_count_min=int(reserve.independent_support_source_count_min),
            support_within_current_pct_max=float(
                reserve.support_within_current_pct_max
            ),
            honest_upside_pct_min=float(reserve.honest_upside_pct_min),
            support_strength_min=str(reserve.support_strength_min),
            support_families=tuple(reserve.independent_support_source_families),
            discount_below_support_pct_range=tuple(
                float(value) for value in reserve.discount_below_support_pct_range
            ),
            final_limit_distance_from_current_pct_range=tuple(
                float(value)
                for value in reserve.final_limit_distance_from_current_pct_range
            ),
            all_pending_buy_required_cash_hard_cap_pct=int(
                reserve.all_pending_buy_required_cash_hard_cap_pct
            ),
            tier_armed_required_cash_cap_pct=int(
                reserve.tier_armed_required_cash_cap_pct
            ),
        )
        # These are operator-frozen safety literals.  Fail closed if a future
        # policy shape is accidentally substituted for the reserve-net contract.
        if (
            gates.rsi_max != 45
            or gates.support_source_count_min != 2
            or gates.support_within_current_pct_max != 8
            or gates.honest_upside_pct_min != 40
            or gates.support_strength_min != "moderate"
            or set(gates.support_families) != {"fib", "bb_lower", "volume_profile"}
            or gates.discount_below_support_pct_range != (5, 10)
            or gates.final_limit_distance_from_current_pct_range != (-15, -5)
            or gates.all_pending_buy_required_cash_hard_cap_pct != 90
            or gates.tier_armed_required_cash_cap_pct != 50
        ):
            raise ValueError(
                "fanout gate literals do not match buy.support_reserve_net"
            )
        return gates

    def as_dict(self) -> dict[str, Any]:
        return {
            "rsi_max": self.rsi_max,
            "support_source_count_min": self.support_source_count_min,
            "support_within_current_pct_max": self.support_within_current_pct_max,
            "honest_upside_pct_min": self.honest_upside_pct_min,
            "support_strength_min": self.support_strength_min,
            "support_families": list(self.support_families),
            "discount_below_support_pct_range": list(
                self.discount_below_support_pct_range
            ),
            "final_limit_distance_from_current_pct_range": list(
                self.final_limit_distance_from_current_pct_range
            ),
            "all_pending_buy_required_cash_hard_cap_pct": (
                self.all_pending_buy_required_cash_hard_cap_pct
            ),
            "tier_armed_required_cash_cap_pct": self.tier_armed_required_cash_cap_pct,
        }


class _NoPortfolioRelationResolver:
    """Snapshot rows are read without holdings or watchlist lookups."""

    def relation(self, market: str, symbol: str) -> str:  # noqa: ARG002
        return "none"


def _validate_snapshot_preset_group(family: str, presets: tuple[str, ...]) -> None:
    if not presets:
        raise ValueError(f"snapshot preset group {family} must not be empty")
    if len(presets) > MAX_SNAPSHOT_PRESETS_PER_CALL:
        raise ValueError(
            f"snapshot preset group {family} exceeds "
            f"{MAX_SNAPSHOT_PRESETS_PER_CALL} presets"
        )
    if len(set(presets)) != len(presets):
        raise ValueError(f"snapshot preset group {family} contains duplicates")


def _canonical_symbol(symbol: object) -> str | None:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return None
    return to_db_symbol(raw).upper()


def _as_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _first_float(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _as_float(row.get(key))
        if value is not None:
            return value
    return None


def _support_family(source: object) -> str | None:
    normalized = str(source or "").strip().lower()
    for prefix, family in _SUPPORT_FAMILY_ALIASES:
        if normalized.startswith(prefix):
            return family
    return None


def _support_evidence(
    supports: object,
    *,
    current_price: float,
    gates: _FanoutGates,
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(supports, list):
        return None, "fresh_supports_missing"

    strength_rank = {"weak": 0, "moderate": 1, "strong": 2}
    required_strength = strength_rank[gates.support_strength_min]
    nearest_failure = "no_support_below_current_price"
    usable: list[dict[str, Any]] = []
    for level in supports:
        if not isinstance(level, Mapping):
            continue
        price = _as_float(level.get("price"))
        if price is None or price <= 0 or price >= current_price:
            continue
        distance_pct = (current_price - price) / current_price * 100
        sources = level.get("sources") or []
        raw_sources = [sources] if isinstance(sources, str) else list(sources)
        families = sorted(
            {
                family
                for source in raw_sources
                if (family := _support_family(source)) is not None
                and family in gates.support_families
            }
        )
        strength = str(level.get("strength") or "").strip().lower()
        if distance_pct > gates.support_within_current_pct_max:
            nearest_failure = "support_more_than_8pct_below_current"
            continue
        if strength_rank.get(strength, -1) < required_strength:
            nearest_failure = "support_strength_below_moderate"
            continue
        if len(families) < gates.support_source_count_min:
            nearest_failure = "independent_support_family_count_below_2"
            continue
        usable.append(
            {
                "price": round(price, 6),
                "distance_pct": round(distance_pct, 6),
                "strength": strength,
                "source_families": families,
                "sources": [str(source) for source in raw_sources],
            }
        )

    if not usable:
        return None, nearest_failure
    return min(usable, key=lambda level: float(level["distance_pct"])), "pass"


def _trading_restriction_reason(fresh: Mapping[str, Any]) -> str | None:
    """Use only fresh-analysis restriction evidence; unknown never becomes pass."""

    if fresh.get("nxt_tradable") is False:
        return "nxt_not_tradable"
    if fresh.get("trading_suspended") is True:
        return "trading_suspended"
    if fresh.get("trading_restricted") is True:
        return "trading_restricted"
    if fresh.get("is_tradable") is False:
        return "not_tradable"
    if fresh.get("halt_suspect"):
        return "halt_suspect"
    return None


def _not_evaluated(reason: str) -> dict[str, Any]:
    return {"status": "not_evaluated", "reason": reason}


def _freshness_contract(fresh: Mapping[str, Any]) -> dict[str, Any]:
    """Classify freshness without treating an omitted key as a fresh pass.

    The concrete revalidator requests a full analysis specifically because its
    top-level ``data_state`` is the established aggregate freshness contract.
    This defensive branch still accepts legacy compact-shaped input for
    observation, but it never establishes eligibility from it.
    """

    if "data_state" not in fresh:
        return {
            "status": "undetermined",
            "reason": "freshness_data_state_missing",
            "data_state": None,
            "evidence_source": _FRESHNESS_EVIDENCE_SOURCE,
            "eligibility_blocked": True,
        }
    raw_state = fresh.get("data_state")
    state = str(raw_state).strip().lower() if raw_state is not None else ""
    if state == "fresh":
        return {
            "status": "proven_fresh",
            "reason": "freshness_proven_by_data_state",
            "data_state": state,
            "evidence_source": _FRESHNESS_EVIDENCE_SOURCE,
            "eligibility_blocked": False,
        }
    if state in _KNOWN_NOT_FRESH_DATA_STATES:
        return {
            "status": "not_fresh",
            "reason": "fresh_revalidation_data_state_not_fresh",
            "data_state": state,
            "evidence_source": _FRESHNESS_EVIDENCE_SOURCE,
            "eligibility_blocked": True,
        }
    return {
        "status": "undetermined",
        "reason": "freshness_data_state_unrecognized",
        "data_state": raw_state,
        "evidence_source": _FRESHNESS_EVIDENCE_SOURCE,
        "eligibility_blocked": True,
    }


def _funnel_result(
    funnel: dict[str, dict[str, Any]],
    freshness: Mapping[str, Any],
    *,
    regular_rsi_pass: bool = False,
    rsi_only_fail: bool = False,
    observation_gate_path_complete: bool = False,
) -> dict[str, Any]:
    """Keep unproven freshness from becoming a regular or RSI-only pass."""

    proven_fresh = freshness.get("status") == "proven_fresh"
    return {
        "funnel": funnel,
        "freshness": dict(freshness),
        "regular_evidence_eligible": proven_fresh and regular_rsi_pass,
        "rsi_only_fail_candidate": proven_fresh and rsi_only_fail,
        "observation_gate_path_complete": observation_gate_path_complete,
        "actionable": False,
    }


def _minimum_snapshot_date(market: str) -> dt.date:
    """Return the oldest permitted date: current baseline minus one session."""

    from app.services.invest_screener_snapshots.freshness import (
        expected_baseline_date,
    )

    minimum = expected_baseline_date(market)
    for _ in range(SNAPSHOT_MAX_STALE_SESSIONS):
        minimum -= dt.timedelta(days=1)
        while minimum.weekday() >= 5:
            minimum -= dt.timedelta(days=1)
    return minimum


def _snapshot_staleness_contract(
    freshness: Mapping[str, Any], market: str
) -> dict[str, Any]:
    """Fail closed if a snapshot cannot prove it is at most one session old."""

    primary = freshness.get("primary")
    primary_data = primary if isinstance(primary, Mapping) else {}
    raw_date = primary_data.get("snapshotDate")
    try:
        snapshot_date = dt.date.fromisoformat(str(raw_date))
    except (TypeError, ValueError):
        return {
            "within_limit": False,
            "reason": "snapshot_date_missing_or_invalid",
            "max_stale_sessions": SNAPSHOT_MAX_STALE_SESSIONS,
        }
    minimum_date = _minimum_snapshot_date(market)
    return {
        "within_limit": snapshot_date >= minimum_date,
        "reason": (
            "within_one_session_stale_limit"
            if snapshot_date >= minimum_date
            else "snapshot_more_than_one_session_stale"
        ),
        "snapshot_date": snapshot_date.isoformat(),
        "minimum_allowed_snapshot_date": minimum_date.isoformat(),
        "max_stale_sessions": SNAPSHOT_MAX_STALE_SESSIONS,
    }


def _anchor_band(
    *, support_price: float, current_price: float, gates: _FanoutGates
) -> tuple[dict[str, Any] | None, str]:
    discount_min, discount_max = gates.discount_below_support_pct_range
    final_min, final_max = gates.final_limit_distance_from_current_pct_range
    raw_low = support_price * (1 - discount_max / 100)
    raw_high = support_price * (1 - discount_min / 100)
    final_low = current_price * (1 + final_min / 100)
    final_high = current_price * (1 + final_max / 100)
    valid_low = max(raw_low, final_low)
    valid_high = min(raw_high, final_high)
    if valid_low > valid_high:
        return None, "anchor_band_outside_final_distance_range"
    return (
        {
            # This is intentionally pre-tick and non-executable.  A later
            # authorized consumer must perform its own tick-floor revalidation.
            "non_executable": True,
            "raw_anchor_range": [round(raw_low, 6), round(raw_high, 6)],
            "valid_observation_anchor_range": [
                round(valid_low, 6),
                round(valid_high, 6),
            ],
            "support_discount_pct_range": [discount_min, discount_max],
            "final_distance_from_current_pct_range": [final_min, final_max],
        },
        "pass",
    )


def _evaluate_funnel(
    candidate: Mapping[str, Any],
    fresh: Mapping[str, Any] | None,
    gates: _FanoutGates,
) -> dict[str, Any]:
    """Record every fixed funnel stage without relaxing a failed gate.

    A missing freshness key is intentionally distinct from an explicit stale
    result.  The former allows downstream gate *observation* so the digest can
    diagnose the population, but it can never produce an eligibility pass.
    """

    funnel: dict[str, dict[str, Any]] = {
        "source": {
            "status": "pass",
            "matched_sources": list(candidate["matched_sources"]),
        }
    }
    if fresh is None:
        unavailable = {
            "status": "unavailable",
            "reason": "fresh_revalidation_unavailable",
            "evidence_source": _FRESHNESS_EVIDENCE_SOURCE,
            "eligibility_blocked": True,
        }
        funnel["base_eligibility"] = {
            "status": "fail",
            "reason": "fresh_revalidation_unavailable",
        }
        for stage in _FUNNEL_STAGE_NAMES[2:]:
            funnel[stage] = _not_evaluated("base_eligibility_failed")
        return _funnel_result(funnel, unavailable)

    freshness = _freshness_contract(fresh)
    current_price = _first_float(fresh, "current_price", "price", "currentPrice")
    restriction_reason = _trading_restriction_reason(fresh)
    if freshness["status"] == "not_fresh":
        funnel["base_eligibility"] = {
            "status": "fail",
            "reason": freshness["reason"],
            "data_state": freshness["data_state"],
            "freshness_evidence_source": freshness["evidence_source"],
        }
        for stage in _FUNNEL_STAGE_NAMES[2:]:
            funnel[stage] = _not_evaluated("base_eligibility_failed")
        return _funnel_result(funnel, freshness)
    if current_price is None or current_price <= 0:
        funnel["base_eligibility"] = {
            "status": "fail",
            "reason": "fresh_current_price_missing",
            "freshness_status": freshness["status"],
        }
        for stage in _FUNNEL_STAGE_NAMES[2:]:
            funnel[stage] = _not_evaluated("base_eligibility_failed")
        return _funnel_result(funnel, freshness)
    if restriction_reason is not None:
        funnel["base_eligibility"] = {
            "status": "fail",
            "reason": restriction_reason,
            "freshness_status": freshness["status"],
        }
        for stage in _FUNNEL_STAGE_NAMES[2:]:
            funnel[stage] = _not_evaluated("base_eligibility_failed")
        return _funnel_result(funnel, freshness)

    observation_only_due_to_freshness = freshness["status"] == "undetermined"

    def observed_stage(status: str, **evidence: Any) -> dict[str, Any]:
        if observation_only_due_to_freshness:
            evidence.update(
                {
                    "freshness_status": "undetermined",
                    "eligibility_blocked_by_freshness": True,
                }
            )
        return {"status": status, **evidence}

    if observation_only_due_to_freshness:
        funnel["base_eligibility"] = {
            "status": "undetermined",
            "reason": freshness["reason"],
            "current_price": current_price,
            "freshness_evidence_source": freshness["evidence_source"],
            "continued_as_observation_only": True,
            "eligibility_blocked_by_freshness": True,
            "trading_restriction": "clear",
        }
    else:
        funnel["base_eligibility"] = {
            "status": "pass",
            "current_price": current_price,
            "data_state": freshness["data_state"],
            "freshness_evidence_source": freshness["evidence_source"],
            "trading_restriction": "clear",
        }

    support, support_reason = _support_evidence(
        fresh.get("supports"), current_price=current_price, gates=gates
    )
    if support is None:
        funnel["support_source_count"] = observed_stage("fail", reason=support_reason)
        for stage in _FUNNEL_STAGE_NAMES[3:]:
            funnel[stage] = _not_evaluated("support_source_count_failed")
        return _funnel_result(funnel, freshness)
    funnel["support_source_count"] = observed_stage("pass", **support)

    consensus = fresh.get("consensus")
    consensus_data = consensus if isinstance(consensus, Mapping) else {}
    if consensus_has_stale_window_inputs(consensus_data):
        # ROB-1300: a numeric leftover from remaining-window rows is not a
        # pass. Stale inputs stop the honest-upside calculation.
        funnel["upside"] = observed_stage(
            "fail",
            reason="honest_upside_stale_inputs",
            rows_excluded_stale=stale_window_input_count(consensus_data),
            target_price_honest=consensus_data.get("target_price_honest"),
            newest_opinion_date=consensus_data.get("newest_opinion_date"),
            calculation_suppressed=True,
        )
        for stage in _FUNNEL_STAGE_NAMES[4:]:
            funnel[stage] = _not_evaluated("upside_failed")
        return _funnel_result(funnel, freshness)
    target_price = _first_float(
        consensus_data, "avg_target_price", "avgTargetPrice", "target_price"
    )
    if target_price is None or target_price <= 0:
        funnel["upside"] = observed_stage("fail", reason="fresh_consensus_missing")
        for stage in _FUNNEL_STAGE_NAMES[4:]:
            funnel[stage] = _not_evaluated("upside_failed")
        return _funnel_result(funnel, freshness)
    upside_pct = (target_price - current_price) / current_price * 100
    if upside_pct < gates.honest_upside_pct_min:
        funnel["upside"] = observed_stage(
            "fail",
            reason="honest_upside_below_40pct",
            current_price=current_price,
            avg_target_price=target_price,
            honest_upside_pct=round(upside_pct, 6),
        )
        for stage in _FUNNEL_STAGE_NAMES[4:]:
            funnel[stage] = _not_evaluated("upside_failed")
        return _funnel_result(funnel, freshness)
    funnel["upside"] = observed_stage(
        "pass",
        current_price=current_price,
        avg_target_price=target_price,
        honest_upside_pct=round(upside_pct, 6),
    )

    rsi = _first_float(fresh, "rsi_14", "rsi14", "rsi")
    regular_rsi_pass = rsi is not None and rsi <= gates.rsi_max
    if regular_rsi_pass:
        funnel["rsi"] = observed_stage(
            "regular_pass",
            rsi_14=rsi,
            max_rsi=gates.rsi_max,
        )
    else:
        # This is a classification only.  It cannot create a proposal, an order,
        # or an actionable candidate from this read-only surface.
        funnel["rsi"] = observed_stage(
            "rsi_only_fail",
            reason="rsi_missing_or_above_regular_max",
            rsi_14=rsi,
            max_rsi=gates.rsi_max,
        )

    anchor, anchor_reason = _anchor_band(
        support_price=float(support["price"]),
        current_price=current_price,
        gates=gates,
    )
    if anchor is None:
        funnel["anchor_band"] = observed_stage("fail", reason=anchor_reason)
        funnel["budget"] = _not_evaluated("anchor_band_failed")
        return _funnel_result(funnel, freshness)
    funnel["anchor_band"] = observed_stage("pass", **anchor)
    # Account/broker evidence is deliberately unavailable in this task.  Keep
    # the cap literals visible but never infer a budget pass.
    funnel["budget"] = observed_stage(
        "deferred",
        reason="broker_account_budget_out_of_scope",
        all_pending_buy_required_cash_hard_cap_pct=(
            gates.all_pending_buy_required_cash_hard_cap_pct
        ),
        tier_armed_required_cash_cap_pct=gates.tier_armed_required_cash_cap_pct,
    )
    return _funnel_result(
        funnel,
        freshness,
        regular_rsi_pass=regular_rsi_pass,
        rsi_only_fail=not regular_rsi_pass,
        observation_gate_path_complete=True,
    )


async def _read_live_source(
    source: _LiveSource, market: str, top_n: int
) -> dict[str, Any]:
    from app.mcp_server.tooling.analysis_tool_handlers import screen_stocks_impl

    request: dict[str, Any] = {
        "market": market,
        "sort_by": source.sort_by,
        "sort_order": source.sort_order,
        "limit": top_n,
    }
    # max_rsi is intentionally omitted: this is an RSI ordering source, not a
    # pre-filtered RSI source.
    response = await screen_stocks_impl(**request)
    return {
        "source": source.source,
        "family": source.source,
        "kind": "live",
        "rows": list(response.get("results") or []),
        "metadata": {
            "request": request,
            # Reading a server-reported total does not widen the top-N source
            # read. It lets the digest distinguish a known population from a
            # bounded/unknown one when the upstream does not report the total.
            "upstream_total_count": response.get("total_count"),
        },
    }


async def _read_snapshot_group(
    family: str, presets: tuple[str, ...], market: str, top_n: int
) -> list[dict[str, Any]]:
    """Read only persisted snapshot rows; never use the held-aware MCP wrapper."""

    _validate_snapshot_preset_group(family, presets)
    from app.core.db import AsyncSessionLocal
    from app.services.invest_view_model.screener_service import build_screener_results
    from app.services.screener_service import ScreenerService

    service = ScreenerService()
    resolver = _NoPortfolioRelationResolver()
    payloads: list[dict[str, Any]] = []
    async with AsyncSessionLocal() as session:
        for preset in presets:
            response = await build_screener_results(
                preset,
                service,
                resolver,
                market=market,
                session=session,
            )
            freshness = response.freshness.model_dump(mode="json")
            source_rows = [
                row.model_dump(mode="json") for row in response.results[:top_n]
            ]
            staleness = _snapshot_staleness_contract(freshness, market)
            source_drop_reasons: dict[str, int] = {}
            if source_rows and not staleness["within_limit"]:
                source_drop_reasons[str(staleness["reason"])] = len(source_rows)
                source_rows = []
            payloads.append(
                {
                    "source": f"{family}:{preset}",
                    "family": family,
                    "kind": "snapshot",
                    "rows": source_rows,
                    "metadata": {
                        "preset": preset,
                        "scheduled_preset_group": list(presets),
                        "scheduled_preset_count": len(presets),
                        "snapshot_freshness": freshness,
                        "snapshot_staleness_contract": staleness,
                        "incoming_count_before_staleness_filter": len(response.results),
                        "source_drop_reasons": source_drop_reasons,
                    },
                }
            )
    return payloads


def _full_analysis_rsi(analysis: Mapping[str, Any]) -> float | None:
    indicators = analysis.get("indicators")
    indicators_data = indicators if isinstance(indicators, Mapping) else {}
    nested = indicators_data.get("indicators")
    flat = nested if isinstance(nested, Mapping) else indicators_data
    rsi = flat.get("rsi")
    rsi_data = rsi if isinstance(rsi, Mapping) else {}
    rsi_value = rsi_data.get("14")
    if rsi_value is None:
        rsi_value = rsi_data.get(14)
    return _as_float(rsi_value)


def _normalize_full_revalidation_row(analysis: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt full analysis output without losing its top-level freshness state.

    ``quick=True`` omits aggregate ``data_state`` on ordinary KR regular-session
    compact rows. This adapter intentionally consumes the existing full-analysis
    response so eligibility has an explicit freshness source; it does not change
    the shared compact response contract.
    """

    output: dict[str, Any] = {}
    quote = analysis.get("quote")
    quote_data = quote if isinstance(quote, Mapping) else {}
    support_resistance = analysis.get("support_resistance")
    support_data = support_resistance if isinstance(support_resistance, Mapping) else {}
    opinions = analysis.get("opinions")
    opinions_data = opinions if isinstance(opinions, Mapping) else {}

    # Presence is deliberately retained: missing top-level data_state must
    # remain distinguishable from an explicit non-fresh value downstream.
    for key in ("data_state", "price_data_state", "halt_suspect", "error"):
        if key in analysis:
            output[key] = analysis[key]
    current_price = _first_float(quote_data, "price", "current_price")
    if current_price is not None:
        output["current_price"] = current_price
    rsi_14 = _full_analysis_rsi(analysis)
    if rsi_14 is not None:
        output["rsi_14"] = rsi_14
    supports = support_data.get("supports")
    if isinstance(supports, list):
        # Unlike quick compact summaries, do not silently cut this evidence to
        # three levels; the fan-out still chooses one qualifying nearest support.
        output["supports"] = list(supports)
    consensus = opinions_data.get("consensus")
    if isinstance(consensus, Mapping):
        output["consensus"] = dict(consensus)
    for key in (
        "nxt_tradable",
        "trading_suspended",
        "trading_restricted",
        "is_tradable",
    ):
        if key in analysis:
            output[key] = analysis[key]
        elif key in quote_data:
            output[key] = quote_data[key]
    return output


async def _fresh_revalidate(
    symbols: list[str], market: str
) -> dict[str, dict[str, Any]]:
    """Run bounded full analysis with position attachment disabled.

    Full output is required only to retain the existing aggregate data_state
    freshness contract. This remains a read-only market-analysis call and does
    not add portfolio, account, or order access.
    """

    from app.mcp_server.tooling.analysis_tool_handlers import analyze_stock_batch_impl

    response = await analyze_stock_batch_impl(
        symbols=symbols,
        market=market,
        include_peers=False,
        quick=False,
        include_position=False,
        # refresh=True writes provider data to its cache; this read-only fan-out
        # relies on the analysis path's fresh quote/RSI/support computation.
        refresh=False,
    )
    output: dict[str, dict[str, Any]] = {}
    results = response.get("results") or {}
    if not isinstance(results, Mapping):
        return output
    for returned_symbol, row in results.items():
        canonical = _canonical_symbol(returned_symbol)
        if canonical is not None and isinstance(row, Mapping):
            output[canonical] = _normalize_full_revalidation_row(row)
    return output


def _initial_source_stats(payload: Mapping[str, Any]) -> dict[str, Any]:
    rows = payload.get("rows")
    metadata = payload.get("metadata")
    metadata_data = metadata if isinstance(metadata, Mapping) else {}
    visible_count = len(rows) if isinstance(rows, list) else 0
    incoming_count = int(
        metadata_data.get("incoming_count_before_staleness_filter", visible_count)
    )
    dropped_reasons = dict(metadata_data.get("source_drop_reasons") or {})
    if incoming_count > TOP_N_PER_SOURCE:
        dropped_reasons["outside_source_top_n"] = (
            int(dropped_reasons.get("outside_source_top_n", 0))
            + incoming_count
            - TOP_N_PER_SOURCE
        )
    raw_upstream_total = metadata_data.get("upstream_total_count")
    upstream_total = _as_float(raw_upstream_total)
    if payload["kind"] == "live" and upstream_total is not None and upstream_total >= 0:
        source_population = {
            "status": "reported",
            "reported_total_count": int(upstream_total),
            "top_n_read_count": visible_count,
        }
    elif payload["kind"] == "live":
        source_population = {
            "status": "bounded_unknown",
            "reason": "upstream_total_not_reported_with_top_n_only_read",
            "top_n_read_count": visible_count,
        }
    else:
        source_population = {"status": "snapshot_source"}
    return {
        "source": payload["source"],
        "family": payload["family"],
        "kind": payload["kind"],
        "incoming_count": incoming_count,
        "top_n_count": min(incoming_count, TOP_N_PER_SOURCE),
        "candidate_rows_after_source_validity": visible_count,
        "deduped_candidate_count": 0,
        "dropped_reasons": dropped_reasons,
        "freshness_undetermined_reasons": {},
        "source_population": source_population,
        "funnel_stage_counts": _empty_funnel_stage_counts(),
        "regular_evidence_eligible_count": 0,
        "rsi_only_fail_candidate_count": 0,
        "actionable_count": 0,
        "final_eligible_counts": {
            "regular_evidence": 0,
            "rsi_only_fail_candidate": 0,
            "actionable": 0,
        },
    }


def _count_reason(stats: dict[str, Any], reason: str) -> None:
    dropped = stats["dropped_reasons"]
    dropped[reason] = int(dropped.get(reason, 0)) + 1


def _empty_funnel_stage_counts() -> dict[str, dict[str, int]]:
    return {stage: {} for stage in _FUNNEL_STAGE_NAMES}


def _record_funnel_stage_counts(
    stage_counts: dict[str, dict[str, int]],
    funnel: Mapping[str, Mapping[str, Any]],
) -> None:
    for stage in _FUNNEL_STAGE_NAMES:
        stage_data = funnel.get(stage) or {}
        status = str(stage_data.get("status") or "missing")
        counts = stage_counts.setdefault(stage, {})
        counts[status] = int(counts.get(status, 0)) + 1


def _count_undetermined_reason(stats: dict[str, Any], reason: str) -> None:
    reasons = stats["freshness_undetermined_reasons"]
    reasons[reason] = int(reasons.get(reason, 0)) + 1


def _dedupe_candidates(
    payloads: Sequence[Mapping[str, Any]], market: str
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    candidates: dict[str, dict[str, Any]] = {}
    source_stats: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        source = str(payload["source"])
        source_stats[source] = _initial_source_stats(payload)
        rows = payload.get("rows")
        if not isinstance(rows, list):
            continue
        for row in rows[:TOP_N_PER_SOURCE]:
            if not isinstance(row, Mapping):
                _count_reason(source_stats[source], "malformed_source_row")
                continue
            # Generic live screeners name their database-standard ticker `code`;
            # snapshot rows use `symbol`.  Accept both before one common DB-form
            # normalization so either source can meet the dedupe contract.
            symbol = _canonical_symbol(row.get("symbol") or row.get("code"))
            if symbol is None:
                _count_reason(source_stats[source], "missing_symbol")
                continue
            existing = candidates.get(symbol)
            if existing is None:
                existing = {
                    "symbol": symbol,
                    "market": market,
                    "name": row.get("name"),
                    "matched_sources": [source],
                    "source_rows": [
                        {
                            "source": source,
                            "family": payload["family"],
                            "kind": payload["kind"],
                            "rank": row.get("rank"),
                        }
                    ],
                }
                candidates[symbol] = existing
            else:
                # Do not collapse away provenance when DB-standard symbols meet.
                if source not in existing["matched_sources"]:
                    existing["matched_sources"].append(source)
                    existing["source_rows"].append(
                        {
                            "source": source,
                            "family": payload["family"],
                            "kind": payload["kind"],
                            "rank": row.get("rank"),
                        }
                    )
    ordered = list(candidates.values())
    for candidate in ordered:
        for source in candidate["matched_sources"]:
            source_stats[source]["deduped_candidate_count"] += 1
    return ordered, source_stats


def _first_failed_reason(funnel: Mapping[str, Mapping[str, Any]]) -> str | None:
    for stage in _FUNNEL_STAGE_NAMES[1:]:
        value = funnel.get(stage) or {}
        if value.get("status") == "fail":
            return str(value.get("reason") or stage)
    return None


async def discover_buy_candidates_fanout_impl(
    *,
    market: Literal["kr"] = "kr",
    _live_reader: _LiveReader | None = None,
    _snapshot_reader: _SnapshotReader | None = None,
    _fresh_revalidator: _FreshRevalidator | None = None,
    _policy_loader: _PolicyLoader = load_trading_policy,
) -> dict[str, Any]:
    """Discover a bounded KR candidate pool and record an observation funnel.

    The return is explicitly non-actionable.  It has no account/broker access,
    performs no writes, and must not be used as PnL scoring or immediate
    threshold-tuning evidence.
    """

    for family, presets in _SNAPSHOT_SOURCE_GROUPS:
        _validate_snapshot_preset_group(family, presets)
    gates = _FanoutGates.from_policy(_policy_loader())
    live_reader = _live_reader or _read_live_source
    snapshot_reader = _snapshot_reader or _read_snapshot_group
    fresh_revalidator = _fresh_revalidator or _fresh_revalidate

    live_payloads, snapshot_groups = await asyncio.gather(
        asyncio.gather(
            *(live_reader(source, market, TOP_N_PER_SOURCE) for source in _LIVE_SOURCES)
        ),
        asyncio.gather(
            *(
                snapshot_reader(family, presets, market, TOP_N_PER_SOURCE)
                for family, presets in _SNAPSHOT_SOURCE_GROUPS
            )
        ),
    )
    payloads: list[dict[str, Any]] = [*live_payloads]
    for group in snapshot_groups:
        payloads.extend(group)

    candidates, source_stats = _dedupe_candidates(payloads, market)
    fresh_targets = candidates[:TOP_N_REVALIDATION]
    fresh_by_symbol: dict[str, dict[str, Any]] = {}
    revalidation_error: str | None = None
    if fresh_targets:
        try:
            fresh_by_symbol = await fresh_revalidator(
                [candidate["symbol"] for candidate in fresh_targets], market
            )
        except Exception as exc:  # noqa: BLE001 - fail closed without source details
            revalidation_error = type(exc).__name__

    regular_evidence_eligible_count = 0
    rsi_only_fail_candidate_count = 0
    freshness_undetermined_count = 0
    freshness_undetermined_reasons: dict[str, int] = {}
    funnel_stage_counts = _empty_funnel_stage_counts()
    for index, candidate in enumerate(candidates):
        symbol = candidate["symbol"]
        if index >= TOP_N_REVALIDATION:
            fresh: Mapping[str, Any] | None = None
            candidate["revalidation"] = {
                "status": "not_revalidated_top_n_limit",
                "top_n_revalidation_limit": TOP_N_REVALIDATION,
            }
        elif revalidation_error is not None:
            fresh = None
            candidate["revalidation"] = {
                "status": "unavailable",
                "reason": "fresh_revalidation_error",
                "error_type": revalidation_error,
            }
        else:
            fresh = fresh_by_symbol.get(symbol)
            candidate["revalidation"] = {
                "status": "received" if fresh is not None else "missing",
                "scope": [
                    "data_state",
                    "current_price",
                    "supports",
                    "consensus",
                    "trading_restriction",
                    "rsi_14",
                ],
            }
        evaluation = _evaluate_funnel(candidate, fresh, gates)
        candidate.update(evaluation)
        _record_funnel_stage_counts(funnel_stage_counts, candidate["funnel"])
        freshness = candidate["freshness"]
        if freshness["status"] == "undetermined":
            freshness_undetermined_count += 1
            reason = str(freshness["reason"])
            freshness_undetermined_reasons[reason] = (
                int(freshness_undetermined_reasons.get(reason, 0)) + 1
            )
        if candidate["regular_evidence_eligible"]:
            regular_evidence_eligible_count += 1
        if candidate["rsi_only_fail_candidate"]:
            rsi_only_fail_candidate_count += 1
        failure_reason = _first_failed_reason(candidate["funnel"])
        for source in candidate["matched_sources"]:
            stats = source_stats[source]
            _record_funnel_stage_counts(
                stats["funnel_stage_counts"], candidate["funnel"]
            )
            if freshness["status"] == "undetermined":
                _count_undetermined_reason(stats, str(freshness["reason"]))
            if candidate["regular_evidence_eligible"]:
                stats["regular_evidence_eligible_count"] += 1
                stats["final_eligible_counts"]["regular_evidence"] += 1
            elif candidate["rsi_only_fail_candidate"]:
                stats["rsi_only_fail_candidate_count"] += 1
                stats["final_eligible_counts"]["rsi_only_fail_candidate"] += 1
            elif failure_reason is not None:
                _count_reason(stats, failure_reason)

    return {
        "success": True,
        "market": market,
        "observation_only": True,
        "observation_notice": (
            "Observation only: this output is not PnL scoring and must not be used "
            "as immediate threshold-tuning evidence. It creates no proposal or order."
        ),
        "bounds": {
            "top_n_per_source": TOP_N_PER_SOURCE,
            "top_n_revalidation": TOP_N_REVALIDATION,
            "max_snapshot_presets_per_call": MAX_SNAPSHOT_PRESETS_PER_CALL,
            "snapshot_max_stale_sessions": SNAPSHOT_MAX_STALE_SESSIONS,
            "snapshot_values_are_input_only_until_fresh_revalidation": True,
        },
        "policy": {**policy_version_stamp(), "frozen_gates": gates.as_dict()},
        "funnel_stage_order": list(_FUNNEL_STAGE_NAMES),
        "sources": [
            {
                "source": payload["source"],
                "family": payload["family"],
                "kind": payload["kind"],
                "metadata": payload.get("metadata", {}),
            }
            for payload in payloads
        ],
        "candidates": candidates,
        "digest_observation": {
            "observation_only": True,
            "not_for_pnl_scoring_or_immediate_threshold_tuning": True,
            "source_stats": list(source_stats.values()),
            "funnel_stage_counts": funnel_stage_counts,
            "freshness_undetermined_count": freshness_undetermined_count,
            "freshness_undetermined_reasons": freshness_undetermined_reasons,
            "regular_evidence_eligible_count": regular_evidence_eligible_count,
            "rsi_only_fail_candidate_count": rsi_only_fail_candidate_count,
            "actionable_count": 0,
            "final_eligible_counts": {
                "regular_evidence": regular_evidence_eligible_count,
                "rsi_only_fail_candidate": rsi_only_fail_candidate_count,
                "actionable": 0,
            },
            "budget_state": "deferred_without_broker_or_account_access",
        },
    }


__all__ = [
    "MAX_SNAPSHOT_PRESETS_PER_CALL",
    "SNAPSHOT_MAX_STALE_SESSIONS",
    "TOP_N_PER_SOURCE",
    "TOP_N_REVALIDATION",
    "discover_buy_candidates_fanout_impl",
]
