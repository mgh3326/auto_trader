#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.db import engine
from app.models.manual_holdings import BrokerAccount
from app.models.trade_profile import (
    AssetProfile,
    FilterName,
    MarketFilter,
    ProfileName,
    SellMode,
    TierParamType,
    TierRuleParam,
)
from app.models.trading import InstrumentType
from app.monitoring.sentry import capture_exception, init_sentry

logger = logging.getLogger(__name__)
SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)

KR_SYMBOLS: dict[str, str] = {
    "한화에어로": "012450",
    "삼양식품": "003230",
    "HD한국조선": "329180",
    "크래프톤": "259960",
    "NAVER": "035420",
    "파마리서치": "214450",
    "펩트론": "087010",
    "알테오젠": "196170",
}


@dataclass(frozen=True)
class AssetProfileSeed:
    instrument_type: InstrumentType
    symbol_input: str
    tier: int
    profile: ProfileName
    broker_account_id: int | None
    sector: str | None
    tags: list[str] | None
    max_position_pct: Decimal | None
    buy_allowed: bool
    sell_mode: SellMode
    note: str | None
    updated_by: str


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seed trade profile tables")
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--updated-by", default="seed_trade_profiles")
    parser.add_argument("--kis-account-name", default=None)
    parser.add_argument("--upbit-account-name", default=None)
    return parser


def _normalize_kr_symbol(symbol_input: str) -> str:
    candidate = symbol_input.strip()
    if candidate.isdigit() and len(candidate) <= 6:
        return candidate.zfill(6)
    mapped = KR_SYMBOLS.get(candidate)
    if mapped is None:
        raise ValueError(f"KR symbol mapping missing for input: {symbol_input}")
    return mapped


def _resolve_symbol(symbol_input: str, instrument_type: InstrumentType) -> str:
    if instrument_type == InstrumentType.equity_kr:
        return _normalize_kr_symbol(symbol_input)
    if instrument_type == InstrumentType.crypto:
        normalized = symbol_input.strip().upper()
        if normalized.startswith("KRW-"):
            return normalized
        return f"KRW-{normalized}"
    return symbol_input.strip().upper()


async def _require_broker_account(
    user_id: int,
    broker_type: str,
    account_name: str | None = None,
) -> int:
    async with SessionLocal() as session:
        stmt = select(BrokerAccount.id, BrokerAccount.account_name).where(
            BrokerAccount.user_id == user_id,
            BrokerAccount.broker_type == broker_type,
            BrokerAccount.is_active.is_(True),
        )
        if account_name is not None:
            stmt = stmt.where(BrokerAccount.account_name == account_name)

        rows = (await session.execute(stmt)).all()
        if not rows:
            if account_name is None:
                raise ValueError(
                    "Required broker account missing: "
                    f"user_id={user_id}, broker_type={broker_type}"
                )
            raise ValueError(
                "Required broker account missing: "
                f"user_id={user_id}, broker_type={broker_type}, account_name={account_name}"
            )
        if len(rows) > 1:
            account_names = sorted(str(row.account_name) for row in rows)
            raise ValueError(
                "Ambiguous broker account selection: "
                f"user_id={user_id}, broker_type={broker_type}, "
                f"account_name={account_name}, candidates={account_names}"
            )
        return int(rows[0].id)


def _seed_sell_mode_for_profile(profile: ProfileName) -> SellMode:
    if profile == ProfileName.hold_only:
        return SellMode.rebalance_only
    return SellMode.any


async def seed_trade_profiles(
    user_id: int,
    updated_by: str,
    kis_account_name: str | None = None,
    upbit_account_name: str | None = None,
) -> None:
    kis_account_id = await _require_broker_account(
        user_id,
        "kis",
        account_name=kis_account_name,
    )
    upbit_account_id = await _require_broker_account(
        user_id,
        "upbit",
        account_name=upbit_account_name,
    )

    asset_profiles = [
        AssetProfileSeed(
            instrument_type=InstrumentType.equity_kr,
            symbol_input="한화에어로",
            tier=2,
            profile=ProfileName.balanced,
            broker_account_id=kis_account_id,
            sector="semiconductor",
            tags=["region:kr", "style:core"],
            max_position_pct=Decimal("15.00"),
            buy_allowed=True,
            sell_mode=_seed_sell_mode_for_profile(ProfileName.balanced),
            note="core kr equity",
            updated_by=updated_by,
        ),
        AssetProfileSeed(
            instrument_type=InstrumentType.crypto,
            symbol_input="BTC",
            tier=1,
            profile=ProfileName.aggressive,
            broker_account_id=upbit_account_id,
            sector="crypto",
            tags=["bucket:trend"],
            max_position_pct=Decimal("20.00"),
            buy_allowed=True,
            sell_mode=_seed_sell_mode_for_profile(ProfileName.aggressive),
            note="crypto trend",
            updated_by=updated_by,
        ),
        AssetProfileSeed(
            instrument_type=InstrumentType.crypto,
            symbol_input="ETH",
            tier=3,
            profile=ProfileName.hold_only,
            broker_account_id=upbit_account_id,
            sector="crypto",
            tags=["bucket:core"],
            max_position_pct=Decimal("12.50"),
            buy_allowed=True,
            sell_mode=_seed_sell_mode_for_profile(ProfileName.hold_only),
            note="hold-only rebalance",
            updated_by=updated_by,
        ),
        AssetProfileSeed(
            instrument_type=InstrumentType.crypto,
            symbol_input="XRP",
            tier=4,
            profile=ProfileName.exit,
            broker_account_id=upbit_account_id,
            sector="crypto",
            tags=["bucket:risk_off"],
            max_position_pct=Decimal("5.00"),
            buy_allowed=False,
            sell_mode=_seed_sell_mode_for_profile(ProfileName.exit),
            note="exit profile",
            updated_by=updated_by,
        ),
    ]

    market_filters: list[dict[str, Any]] = [
        {
            "instrument_type": InstrumentType.crypto,
            "filter_name": FilterName.funding_rate.value,
            "params": {"max_abs_funding_rate": 0.01},
            "enabled": True,
            "broker_account_id": upbit_account_id,
            "updated_by": updated_by,
        },
        {
            "instrument_type": InstrumentType.equity_kr,
            "filter_name": FilterName.liquidity.value,
            "params": {"min_volume": 300000},
            "enabled": True,
            "broker_account_id": kis_account_id,
            "updated_by": updated_by,
        },
    ]

    tier_rule_params: list[dict[str, Any]] = []
    for tier in (1, 2, 3, 4):
        for profile in (
            ProfileName.aggressive,
            ProfileName.balanced,
            ProfileName.conservative,
            ProfileName.exit,
            ProfileName.hold_only,
        ):
            tier_rule_params.extend(
                [
                    {
                        "user_id": user_id,
                        "instrument_type": InstrumentType.crypto,
                        "tier": tier,
                        "profile": profile,
                        "param_type": TierParamType.buy,
                        "params": {"slice_count": max(1, 5 - tier)},
                        "version": 1,
                        "updated_by": updated_by,
                    },
                    {
                        "user_id": user_id,
                        "instrument_type": InstrumentType.crypto,
                        "tier": tier,
                        "profile": profile,
                        "param_type": TierParamType.sell,
                        "params": {"take_profit_pct": float(4 + tier)},
                        "version": 1,
                        "updated_by": updated_by,
                    },
                    {
                        "user_id": user_id,
                        "instrument_type": InstrumentType.crypto,
                        "tier": tier,
                        "profile": profile,
                        "param_type": TierParamType.stop,
                        "params": {"stop_loss_pct": float(2 + tier)},
                        "version": 1,
                        "updated_by": updated_by,
                    },
                    {
                        "user_id": user_id,
                        "instrument_type": InstrumentType.crypto,
                        "tier": tier,
                        "profile": profile,
                        "param_type": TierParamType.rebalance,
                        "params": {"window_days": 7 * tier},
                        "version": 1,
                        "updated_by": updated_by,
                    },
                    {
                        "user_id": user_id,
                        "instrument_type": InstrumentType.crypto,
                        "tier": tier,
                        "profile": profile,
                        "param_type": TierParamType.common,
                        "params": {
                            "price_base": "close_1",
                            "regime_ema": [60, 200],
                            "max_concurrent_orders": max(1, 5 - tier),
                        },
                        "version": 1,
                        "updated_by": updated_by,
                    },
                ]
            )

    async with SessionLocal() as session:
        async with session.begin():
            for profile in asset_profiles:
                symbol = _resolve_symbol(profile.symbol_input, profile.instrument_type)
                payload = {
                    "user_id": user_id,
                    "broker_account_id": profile.broker_account_id,
                    "symbol": symbol,
                    "instrument_type": profile.instrument_type,
                    "tier": profile.tier,
                    "profile": profile.profile,
                    "sector": profile.sector,
                    "tags": profile.tags,
                    "max_position_pct": profile.max_position_pct,
                    "buy_allowed": profile.buy_allowed,
                    "sell_mode": profile.sell_mode,
                    "note": profile.note,
                    "updated_by": profile.updated_by,
                }
                stmt = insert(AssetProfile).values(**payload)
                if profile.broker_account_id is None:
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["user_id", "symbol", "instrument_type"],
                        index_where=text("broker_account_id IS NULL"),
                        set_={k: v for k, v in payload.items() if k != "user_id"},
                    )
                else:
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[
                            "user_id",
                            "broker_account_id",
                            "symbol",
                            "instrument_type",
                        ],
                        index_where=text("broker_account_id IS NOT NULL"),
                        set_={
                            k: v
                            for k, v in payload.items()
                            if k not in {"user_id", "broker_account_id"}
                        },
                    )
                await session.execute(stmt)

            for item in market_filters:
                payload = {
                    "user_id": user_id,
                    "broker_account_id": item["broker_account_id"],
                    "instrument_type": item["instrument_type"],
                    "filter_name": item["filter_name"],
                    "params": item["params"],
                    "enabled": item["enabled"],
                    "updated_by": item["updated_by"],
                }
                stmt = insert(MarketFilter).values(**payload)
                if payload["broker_account_id"] is None:
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["user_id", "instrument_type", "filter_name"],
                        index_where=text("broker_account_id IS NULL"),
                        set_={
                            "params": payload["params"],
                            "enabled": payload["enabled"],
                            "updated_by": payload["updated_by"],
                        },
                    )
                else:
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[
                            "user_id",
                            "broker_account_id",
                            "instrument_type",
                            "filter_name",
                        ],
                        index_where=text("broker_account_id IS NOT NULL"),
                        set_={
                            "params": payload["params"],
                            "enabled": payload["enabled"],
                            "updated_by": payload["updated_by"],
                        },
                    )
                await session.execute(stmt)

            for item in tier_rule_params:
                stmt = (
                    insert(TierRuleParam)
                    .values(**item)
                    .on_conflict_do_update(
                        index_elements=[
                            "user_id",
                            "instrument_type",
                            "tier",
                            "profile",
                            "param_type",
                        ],
                        set_={
                            "params": item["params"],
                            "version": item["version"],
                            "updated_by": item["updated_by"],
                        },
                    )
                )
                await session.execute(stmt)

        await session.commit()


async def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    init_sentry(service_name="trade-profile-seed")
    try:
        await seed_trade_profiles(
            user_id=args.user_id,
            updated_by=args.updated_by,
            kis_account_name=args.kis_account_name,
            upbit_account_name=args.upbit_account_name,
        )
    except Exception as exc:
        capture_exception(exc, process="seed_trade_profiles")
        logger.error("Trade profile seed failed: %s", exc, exc_info=True)
        return 1

    logger.info("Trade profile seed completed for user_id=%s", args.user_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
