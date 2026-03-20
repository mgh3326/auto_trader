from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import cast

from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.kr_symbols import normalize_kr_symbol
from app.mcp_server.tooling.shared import MCP_USER_ID, normalize_market
from app.models.trade_profile import (
    AssetProfile,
    MarketFilter,
    ProfileChangeLog,
    TierRuleParam,
)
from app.models.trading import InstrumentType


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _parse_market_type(market_type: str | None) -> InstrumentType | None:
    """Parse and validate market_type input.

    Returns None when *market_type* is not supplied.
    Raises ValueError when the value is provided but unrecognised.
    """
    if market_type is None:
        return None
    normalized = normalize_market(market_type)
    if normalized is None:
        raise ValueError(
            f"market_type must be one of: kr, us, crypto (got {market_type!r})"
        )
    return InstrumentType(normalized)


_VALID_PROFILES = frozenset(
    {"aggressive", "balanced", "conservative", "exit", "hold_only"}
)


def _validate_tier(tier: int | None) -> None:
    """Raise ValueError when *tier* is outside 1-4."""
    if tier is not None and not (1 <= tier <= 4):
        raise ValueError("tier must be 1-4")


def _validate_profile(profile: str | None) -> None:
    """Raise ValueError when *profile* is not in the allowed set."""
    if profile is None:
        return
    normalized = profile.strip().lower()
    if normalized and normalized not in _VALID_PROFILES:
        raise ValueError(f"Invalid profile: {profile!r}")


def _normalize_profile(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _normalize_sell_mode(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


_VALID_PARAM_TYPES = frozenset({"buy", "sell", "stop", "rebalance", "common"})


def _validate_param_type(param_type: str | None) -> None:
    if param_type is None:
        return
    normalized = param_type.strip().lower()
    if normalized and normalized not in _VALID_PARAM_TYPES:
        raise ValueError(
            "param_type must be one of: buy, sell, stop, rebalance, common"
        )


def _normalize_param_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _serialize_filter(model: MarketFilter) -> dict[str, object]:
    return {
        "id": model.id,
        "instrument_type": model.instrument_type.value,
        "filter_name": model.filter_name,
        "params": model.params,
        "enabled": model.enabled,
        "updated_by": model.updated_by,
        "created_at": model.created_at.isoformat(),
        "updated_at": model.updated_at.isoformat(),
    }


def _normalize_symbol_for_instrument(
    symbol: str, instrument_type: InstrumentType
) -> str:
    candidate = symbol.strip()
    if instrument_type == InstrumentType.equity_kr:
        if candidate.isdigit() and len(candidate) <= 6:
            return candidate.zfill(6)
        return normalize_kr_symbol(candidate)
    return candidate.upper()


def _to_decimal_pct(value: float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("max_position_pct must be a valid number") from exc


def _serialize_profile(model: AssetProfile) -> dict[str, object]:
    return {
        "id": model.id,
        "symbol": model.symbol,
        "instrument_type": model.instrument_type.value,
        "tier": model.tier,
        "profile": model.profile,
        "sector": model.sector,
        "tags": model.tags,
        "max_position_pct": (
            float(model.max_position_pct)
            if model.max_position_pct is not None
            else None
        ),
        "buy_allowed": model.buy_allowed,
        "sell_mode": model.sell_mode,
        "note": model.note,
        "updated_by": model.updated_by,
        "created_at": model.created_at.isoformat(),
        "updated_at": model.updated_at.isoformat(),
    }


def _serialize_rule(model: TierRuleParam) -> dict[str, object]:
    return {
        "id": model.id,
        "instrument_type": model.instrument_type.value,
        "tier": model.tier,
        "profile": model.profile,
        "param_type": model.param_type,
        "params": model.params,
        "version": model.version,
        "updated_by": model.updated_by,
        "created_at": model.created_at.isoformat(),
        "updated_at": model.updated_at.isoformat(),
    }


def _snapshot_for_change_log(model: AssetProfile) -> dict[str, object]:
    return {
        "symbol": model.symbol,
        "instrument_type": model.instrument_type.value,
        "tier": model.tier,
        "profile": model.profile,
        "sector": model.sector,
        "tags": model.tags,
        "max_position_pct": (
            float(model.max_position_pct)
            if model.max_position_pct is not None
            else None
        ),
        "buy_allowed": model.buy_allowed,
        "sell_mode": model.sell_mode,
        "note": model.note,
        "updated_by": model.updated_by,
    }


def _apply_profile_rules(
    *,
    profile_value: str,
    buy_allowed_value: bool,
    sell_mode_value: str,
    requested_buy_allowed: bool | None,
    requested_sell_mode: str | None,
) -> tuple[bool, str]:
    if profile_value == "exit":
        if requested_buy_allowed is True:
            raise ValueError("profile=exit requires buy_allowed=False")
        if requested_sell_mode is not None and requested_sell_mode != "any":
            raise ValueError("profile=exit requires sell_mode='any'")
        return False, "any"
    if profile_value == "hold_only":
        if requested_sell_mode is not None and requested_sell_mode != "rebalance_only":
            raise ValueError("profile=hold_only requires sell_mode='rebalance_only'")
        return buy_allowed_value, "rebalance_only"
    return buy_allowed_value, sell_mode_value


async def get_asset_profile(
    symbol: str | None = None,
    market_type: str | None = None,
    profile: str | None = None,
    tier: int | None = None,
    include_rules: bool = False,
) -> dict[str, object]:
    try:
        instrument_type = _parse_market_type(market_type)
        _validate_tier(tier)
        _validate_profile(profile)
        normalized_profile = _normalize_profile(profile)
        normalized_symbol = symbol.strip() if symbol is not None else None

        async with _session_factory()() as db:
            conditions = [AssetProfile.user_id == MCP_USER_ID]

            if instrument_type is not None:
                conditions.append(AssetProfile.instrument_type == instrument_type)
            if normalized_profile is not None:
                conditions.append(AssetProfile.profile == normalized_profile)
            if tier is not None:
                conditions.append(AssetProfile.tier == tier)
            if normalized_symbol is not None:
                conditions.append(AssetProfile.symbol == normalized_symbol)

            stmt = (
                select(AssetProfile)
                .where(*conditions)
                .order_by(AssetProfile.tier.asc(), AssetProfile.symbol.asc())
            )
            result = await db.execute(stmt)
            rows: list[AssetProfile] = list(result.scalars().all())

            data: list[dict[str, object]] = [_serialize_profile(row) for row in rows]
            if include_rules and rows:
                combos: set[tuple[InstrumentType, int, str]] = {
                    (row.instrument_type, row.tier, row.profile) for row in rows
                }
                rules_stmt = select(TierRuleParam).where(
                    TierRuleParam.user_id == MCP_USER_ID,
                    tuple_(
                        TierRuleParam.instrument_type,
                        TierRuleParam.tier,
                        TierRuleParam.profile,
                    ).in_(list(combos)),
                )
                rule_result = await db.execute(rules_stmt)
                rule_rows: list[TierRuleParam] = list(rule_result.scalars().all())

                rules_by_combo: dict[tuple[str, int, str], list[dict[str, object]]] = {}
                for rule in rule_rows:
                    key = (rule.instrument_type.value, rule.tier, rule.profile)
                    rules_by_combo.setdefault(key, []).append(_serialize_rule(rule))

                for profile_data in data:
                    key = (
                        cast(str, profile_data["instrument_type"]),
                        cast(int, profile_data["tier"]),
                        cast(str, profile_data["profile"]),
                    )
                    profile_data["tier_rule_params"] = rules_by_combo.get(key, [])

            return {"success": True, "data": data, "count": len(data)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def set_asset_profile(
    symbol: str,
    market_type: str | None = None,
    tier: int | None = None,
    profile: str | None = None,
    sector: str | None = None,
    tags: list[str] | None = None,
    max_position_pct: float | None = None,
    buy_allowed: bool | None = None,
    sell_mode: str | None = None,
    note: str | None = None,
    reason: str | None = None,
    updated_by: str = "mcp",
) -> dict[str, object]:
    try:
        symbol_input = symbol.strip()
        if not symbol_input:
            raise ValueError("symbol is required")

        _validate_tier(tier)
        _validate_profile(profile)
        requested_profile = _normalize_profile(profile)
        requested_sell_mode = _normalize_sell_mode(sell_mode)
        requested_decimal_pct = _to_decimal_pct(max_position_pct)

        explicit_instrument_type = _parse_market_type(market_type)
        existing: AssetProfile | None = None
        instrument_type: InstrumentType
        normalized_symbol: str

        async with _session_factory()() as db:
            async with db.begin():
                if explicit_instrument_type is not None:
                    instrument_type = explicit_instrument_type
                    normalized_symbol = _normalize_symbol_for_instrument(
                        symbol_input, instrument_type
                    )
                    existing_stmt = select(AssetProfile).where(
                        AssetProfile.user_id == MCP_USER_ID,
                        AssetProfile.symbol == normalized_symbol,
                        AssetProfile.instrument_type == instrument_type,
                    )
                    existing_result = await db.execute(existing_stmt)
                    existing = existing_result.scalar_one_or_none()
                else:
                    candidate_pairs: list[tuple[InstrumentType, str]] = []
                    if symbol_input.isdigit() and len(symbol_input) <= 6:
                        candidate_pairs.append(
                            (InstrumentType.equity_kr, symbol_input.zfill(6))
                        )
                    upper_symbol = symbol_input.upper()
                    if upper_symbol.startswith("KRW-"):
                        candidate_pairs.append((InstrumentType.crypto, upper_symbol))
                    if not candidate_pairs:
                        candidate_pairs.append((InstrumentType.equity_us, upper_symbol))

                    predicates = [
                        and_(
                            AssetProfile.instrument_type == candidate_type,
                            AssetProfile.symbol == candidate_symbol,
                        )
                        for candidate_type, candidate_symbol in candidate_pairs
                    ]
                    existing_stmt = select(AssetProfile).where(
                        AssetProfile.user_id == MCP_USER_ID,
                        or_(*predicates),
                    )
                    existing_result = await db.execute(existing_stmt)
                    existing = existing_result.scalar_one_or_none()

                    if existing is not None:
                        instrument_type = existing.instrument_type
                        normalized_symbol = existing.symbol
                    else:
                        raise ValueError("market_type is required for new profile")

                if existing is None:
                    if tier is None:
                        raise ValueError("tier is required for new profile")
                    if requested_profile is None:
                        raise ValueError("profile is required for new profile")

                    effective_buy_allowed = (
                        buy_allowed if buy_allowed is not None else True
                    )
                    effective_sell_mode = requested_sell_mode or "any"
                    effective_buy_allowed, effective_sell_mode = _apply_profile_rules(
                        profile_value=requested_profile,
                        buy_allowed_value=effective_buy_allowed,
                        sell_mode_value=effective_sell_mode,
                        requested_buy_allowed=buy_allowed,
                        requested_sell_mode=requested_sell_mode,
                    )

                    existing = AssetProfile(
                        user_id=MCP_USER_ID,
                        symbol=normalized_symbol,
                        instrument_type=instrument_type,
                        tier=tier,
                        profile=requested_profile,
                        sector=sector,
                        tags=tags,
                        max_position_pct=requested_decimal_pct,
                        buy_allowed=effective_buy_allowed,
                        sell_mode=effective_sell_mode,
                        note=note,
                        updated_by=updated_by,
                    )
                    db.add(existing)
                    await db.flush()
                    await db.refresh(existing)

                    new_snapshot = _snapshot_for_change_log(existing)
                    db.add(
                        ProfileChangeLog(
                            user_id=MCP_USER_ID,
                            change_type="asset_profile",
                            target=f"asset:{instrument_type.value}:{normalized_symbol}",
                            old_value=None,
                            new_value=new_snapshot,
                            reason=reason,
                            changed_by=updated_by,
                        )
                    )
                    action = "created"
                else:
                    old_snapshot = _snapshot_for_change_log(existing)

                    effective_profile = requested_profile or existing.profile
                    effective_buy_allowed = (
                        buy_allowed if buy_allowed is not None else existing.buy_allowed
                    )
                    effective_sell_mode = (
                        requested_sell_mode
                        if requested_sell_mode is not None
                        else existing.sell_mode
                    )
                    effective_buy_allowed, effective_sell_mode = _apply_profile_rules(
                        profile_value=effective_profile,
                        buy_allowed_value=effective_buy_allowed,
                        sell_mode_value=effective_sell_mode,
                        requested_buy_allowed=buy_allowed,
                        requested_sell_mode=requested_sell_mode,
                    )

                    if tier is not None:
                        existing.tier = tier
                    if requested_profile is not None:
                        existing.profile = requested_profile
                    if sector is not None:
                        existing.sector = sector
                    if tags is not None:
                        existing.tags = tags
                    if max_position_pct is not None:
                        existing.max_position_pct = requested_decimal_pct
                    if buy_allowed is not None or effective_profile == "exit":
                        existing.buy_allowed = effective_buy_allowed
                    if requested_sell_mode is not None or effective_profile in {
                        "exit",
                        "hold_only",
                    }:
                        existing.sell_mode = effective_sell_mode
                    if note is not None:
                        existing.note = note
                    existing.updated_by = updated_by

                    await db.flush()
                    await db.refresh(existing)

                    new_snapshot = _snapshot_for_change_log(existing)
                    db.add(
                        ProfileChangeLog(
                            user_id=MCP_USER_ID,
                            change_type="asset_profile",
                            target=(
                                f"asset:{existing.instrument_type.value}:{existing.symbol}"
                            ),
                            old_value=old_snapshot,
                            new_value=new_snapshot,
                            reason=reason,
                            changed_by=updated_by,
                        )
                    )
                    action = "updated"

            return {
                "success": True,
                "action": action,
                "data": _serialize_profile(existing),
            }
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "error": f"set_asset_profile failed: {exc}"}


async def get_tier_rule_params(
    instrument_type: str | None = None,
    tier: int | None = None,
    profile: str | None = None,
    param_type: str | None = None,
) -> dict[str, object]:
    """Get tier rule params with optional filtering."""
    try:
        parsed_instrument_type = _parse_market_type(instrument_type)
        _validate_tier(tier)
        _validate_profile(profile)
        _validate_param_type(param_type)

        normalized_profile = _normalize_profile(profile)
        normalized_param_type = _normalize_param_type(param_type)

        async with _session_factory()() as db:
            conditions = [TierRuleParam.user_id == MCP_USER_ID]

            if parsed_instrument_type is not None:
                conditions.append(
                    TierRuleParam.instrument_type == parsed_instrument_type
                )
            if tier is not None:
                conditions.append(TierRuleParam.tier == tier)
            if normalized_profile is not None:
                conditions.append(TierRuleParam.profile == normalized_profile)
            if normalized_param_type is not None:
                conditions.append(TierRuleParam.param_type == normalized_param_type)

            stmt = (
                select(TierRuleParam)
                .where(*conditions)
                .order_by(
                    TierRuleParam.tier.asc(), TierRuleParam.param_type.asc()
                )
            )
            result = await db.execute(stmt)
            rows: list[TierRuleParam] = list(result.scalars().all())
            data: list[dict[str, object]] = [_serialize_rule(row) for row in rows]

            return {"success": True, "data": data, "count": len(data)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def set_tier_rule_params(
    instrument_type: str,
    tier: int,
    profile: str,
    param_type: str,
    params: dict[str, object],
    updated_by: str = "mcp",
) -> dict[str, object]:
    """Set tier rule params with upsert semantics and versioning."""
    try:
        parsed_instrument_type = _parse_market_type(instrument_type)
        if parsed_instrument_type is None:
            raise ValueError("instrument_type is required")

        _validate_tier(tier)
        if tier is None or not (1 <= tier <= 4):
            raise ValueError("tier must be 1-4")

        _validate_profile(profile)
        normalized_profile = _normalize_profile(profile)
        if normalized_profile is None:
            raise ValueError(f"Invalid profile: {profile!r}")

        _validate_param_type(param_type)
        normalized_param_type = _normalize_param_type(param_type)
        if normalized_param_type is None:
            raise ValueError(
                "param_type must be one of: buy, sell, stop, rebalance, common"
            )

        async with _session_factory()() as db:
            async with db.begin():
                existing_stmt = select(TierRuleParam).where(
                    TierRuleParam.user_id == MCP_USER_ID,
                    TierRuleParam.instrument_type == parsed_instrument_type,
                    TierRuleParam.tier == tier,
                    TierRuleParam.profile == normalized_profile,
                    TierRuleParam.param_type == normalized_param_type,
                )
                existing_result = await db.execute(existing_stmt)
                existing = existing_result.scalar_one_or_none()

                target = f"rule:{parsed_instrument_type.value}:tier{tier}:{normalized_profile}:{normalized_param_type}"

                if existing is None:
                    row = TierRuleParam(
                        user_id=MCP_USER_ID,
                        instrument_type=parsed_instrument_type,
                        tier=tier,
                        profile=normalized_profile,
                        param_type=normalized_param_type,
                        params=params,
                        version=1,
                        updated_by=updated_by,
                    )
                    db.add(row)
                    await db.flush()
                    await db.refresh(row)

                    db.add(
                        ProfileChangeLog(
                            user_id=MCP_USER_ID,
                            change_type="tier_rule_param",
                            target=target,
                            old_value=None,
                            new_value=params,
                            reason=None,
                            changed_by=updated_by,
                        )
                    )
                    action = "created"
                else:
                    old_value = existing.params
                    existing.params = params
                    existing.version += 1
                    existing.updated_by = updated_by
                    await db.flush()
                    await db.refresh(existing)

                    db.add(
                        ProfileChangeLog(
                            user_id=MCP_USER_ID,
                            change_type="tier_rule_param",
                            target=target,
                            old_value=old_value,
                            new_value=params,
                            reason=None,
                            changed_by=updated_by,
                        )
                    )
                    action = "updated"

            return {
                "success": True,
                "action": action,
                "data": _serialize_rule(row if existing is None else existing),
            }
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "error": f"set_tier_rule_params failed: {exc}"}


async def get_market_filters(
    instrument_type: str | None = None,
    enabled_only: bool = False,
) -> dict[str, object]:
    """Get market filters with optional filtering."""
    try:
        parsed_instrument_type = _parse_market_type(instrument_type)

        async with _session_factory()() as db:
            conditions = [MarketFilter.user_id == MCP_USER_ID]

            if parsed_instrument_type is not None:
                conditions.append(
                    MarketFilter.instrument_type == parsed_instrument_type
                )
            if enabled_only:
                conditions.append(MarketFilter.enabled.is_(True))

            stmt = (
                select(MarketFilter)
                .where(*conditions)
                .order_by(
                    MarketFilter.instrument_type.asc(),
                    MarketFilter.filter_name.asc(),
                )
            )
            result = await db.execute(stmt)
            rows: list[MarketFilter] = list(result.scalars().all())
            data: list[dict[str, object]] = [_serialize_filter(row) for row in rows]

            return {"success": True, "data": data, "count": len(data)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def set_market_filter(
    instrument_type: str,
    filter_name: str,
    params: dict[str, object],
    enabled: bool = True,
    updated_by: str = "mcp",
) -> dict[str, object]:
    """Set market filter with upsert semantics."""
    try:
        parsed_instrument_type = _parse_market_type(instrument_type)
        if parsed_instrument_type is None:
            raise ValueError("instrument_type is required")

        normalized_filter_name = filter_name.strip().lower()
        if not normalized_filter_name:
            raise ValueError("filter_name is required")

        async with _session_factory()() as db:
            async with db.begin():
                existing_stmt = select(MarketFilter).where(
                    MarketFilter.user_id == MCP_USER_ID,
                    MarketFilter.instrument_type == parsed_instrument_type,
                    MarketFilter.filter_name == normalized_filter_name,
                )
                existing_result = await db.execute(existing_stmt)
                existing = existing_result.scalar_one_or_none()

                target = f"filter:{parsed_instrument_type.value}:{normalized_filter_name}"

                if existing is None:
                    row = MarketFilter(
                        user_id=MCP_USER_ID,
                        instrument_type=parsed_instrument_type,
                        filter_name=normalized_filter_name,
                        params=params,
                        enabled=enabled,
                        updated_by=updated_by,
                    )
                    db.add(row)
                    await db.flush()
                    await db.refresh(row)

                    db.add(
                        ProfileChangeLog(
                            user_id=MCP_USER_ID,
                            change_type="market_filter",
                            target=target,
                            old_value=None,
                            new_value={"params": params, "enabled": enabled},
                            reason=None,
                            changed_by=updated_by,
                        )
                    )
                    action = "created"
                else:
                    old_value = {"params": existing.params, "enabled": existing.enabled}
                    existing.params = params
                    existing.enabled = enabled
                    existing.updated_by = updated_by
                    await db.flush()
                    await db.refresh(existing)

                    db.add(
                        ProfileChangeLog(
                            user_id=MCP_USER_ID,
                            change_type="market_filter",
                            target=target,
                            old_value=old_value,
                            new_value={"params": params, "enabled": enabled},
                            reason=None,
                            changed_by=updated_by,
                        )
                    )
                    action = "updated"

            return {
                "success": True,
                "action": action,
                "data": _serialize_filter(row if existing is None else existing),
            }
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "error": f"set_market_filter failed: {exc}"}


__all__ = [
    "get_asset_profile",
    "set_asset_profile",
    "get_tier_rule_params",
    "set_tier_rule_params",
    "get_market_filters",
    "set_market_filter",
]
