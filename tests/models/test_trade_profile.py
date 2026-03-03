from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine
from app.models.trading import InstrumentType

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


async def _ensure_trade_profile_tables() -> None:
    try:
        async with SessionLocal() as session:
            row = await session.execute(text("SELECT to_regclass('asset_profiles')"))
            if row.scalar_one_or_none() is None:
                pytest.skip("trade profile tables are not migrated")
    except Exception:
        pytest.skip("database is not available for integration persistence checks")


async def _create_user_and_accounts() -> tuple[int, int, int]:
    suffix = uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        user_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO users (username, email, role, tz, base_currency, is_active)
                    VALUES (:username, :email, 'viewer', 'Asia/Seoul', 'KRW', true)
                    RETURNING id
                    """
                ),
                {
                    "username": f"trade_profile_user_{suffix}",
                    "email": f"trade_profile_{suffix}@example.com",
                },
            )
        ).scalar_one()
        kis_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO broker_accounts
                        (user_id, broker_type, account_name, is_mock, is_active)
                    VALUES (:user_id, 'kis', :name, false, true)
                    RETURNING id
                    """
                ),
                {"user_id": user_id, "name": f"kis_{suffix}"},
            )
        ).scalar_one()
        upbit_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO broker_accounts
                        (user_id, broker_type, account_name, is_mock, is_active)
                    VALUES (:user_id, 'upbit', :name, false, true)
                    RETURNING id
                    """
                ),
                {"user_id": user_id, "name": f"upbit_{suffix}"},
            )
        ).scalar_one()
        await session.commit()
        return user_id, kis_id, upbit_id


async def _cleanup_user(user_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id}
        )
        await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sell_mode_and_hold_only_checks() -> None:
    await _ensure_trade_profile_tables()
    user_id, kis_id, _ = await _create_user_and_accounts()
    try:
        async with SessionLocal() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO asset_profiles
                        (user_id, broker_account_id, symbol, instrument_type, tier, profile, buy_allowed, sell_mode)
                    VALUES
                        (:user_id, :broker_id, '005930', :instrument_type, 2, 'hold_only', true, 'rebalance_only')
                    """
                ),
                {
                    "user_id": user_id,
                    "broker_id": kis_id,
                    "instrument_type": InstrumentType.equity_kr.value,
                },
            )
            await session.commit()

        async with SessionLocal() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        """
                        INSERT INTO asset_profiles
                            (user_id, broker_account_id, symbol, instrument_type, tier, profile, buy_allowed, sell_mode)
                        VALUES
                            (:user_id, :broker_id, '000660', :instrument_type, 2, 'hold_only', true, 'any')
                        """
                    ),
                    {
                        "user_id": user_id,
                        "broker_id": kis_id,
                        "instrument_type": InstrumentType.equity_kr.value,
                    },
                )
                await session.commit()

        async with SessionLocal() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        """
                        INSERT INTO asset_profiles
                            (user_id, broker_account_id, symbol, instrument_type, tier, profile, buy_allowed, sell_mode)
                        VALUES
                            (:user_id, :broker_id, '035420', :instrument_type, 2, 'balanced', true, 'invalid_mode')
                        """
                    ),
                    {
                        "user_id": user_id,
                        "broker_id": kis_id,
                        "instrument_type": InstrumentType.equity_kr.value,
                    },
                )
                await session.commit()

        async with SessionLocal() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        """
                        INSERT INTO asset_profiles
                            (user_id, broker_account_id, symbol, instrument_type, tier, profile, buy_allowed, sell_mode, tags)
                        VALUES
                            (:user_id, :broker_id, '051910', :instrument_type, 2, 'balanced', true, 'any', '{"bad": "shape"}'::jsonb)
                        """
                    ),
                    {
                        "user_id": user_id,
                        "broker_id": kis_id,
                        "instrument_type": InstrumentType.equity_kr.value,
                    },
                )
                await session.commit()
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_param_type_common_and_filter_name_format_and_partial_unique() -> None:
    await _ensure_trade_profile_tables()
    user_id, kis_id, _ = await _create_user_and_accounts()
    try:
        async with SessionLocal() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO tier_rule_params
                        (user_id, instrument_type, tier, profile, param_type, params, version)
                    VALUES
                        (:user_id, :instrument_type, 1, 'balanced', 'common', '{"max": 3}'::jsonb, 1)
                    """
                ),
                {
                    "user_id": user_id,
                    "instrument_type": InstrumentType.crypto.value,
                },
            )
            await session.execute(
                text(
                    """
                        INSERT INTO market_filters
                            (user_id, broker_account_id, instrument_type, filter_name, params, enabled)
                        VALUES
                            (:user_id, :broker_id, :instrument_type, 'liquidity', '{"min": 1}'::jsonb, true)
                        """
                ),
                {
                    "user_id": user_id,
                    "broker_id": kis_id,
                    "instrument_type": InstrumentType.equity_kr.value,
                },
            )
            await session.commit()

        async with SessionLocal() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        """
                        INSERT INTO market_filters
                            (user_id, broker_account_id, instrument_type, filter_name, params, enabled)
                        VALUES
                            (:user_id, :broker_id, :instrument_type, 'INVALID-NAME', '{"x": 1}'::jsonb, true)
                        """
                    ),
                    {
                        "user_id": user_id,
                        "broker_id": kis_id,
                        "instrument_type": InstrumentType.equity_kr.value,
                    },
                )
                await session.commit()

        async with SessionLocal() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO asset_profiles
                        (user_id, broker_account_id, symbol, instrument_type, tier, profile, buy_allowed, sell_mode)
                    VALUES
                        (:user_id, NULL, 'BTC', :instrument_type, 1, 'aggressive', true, 'any')
                    """
                ),
                {
                    "user_id": user_id,
                    "instrument_type": InstrumentType.crypto.value,
                },
            )
            await session.commit()

        async with SessionLocal() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        """
                        INSERT INTO asset_profiles
                            (user_id, broker_account_id, symbol, instrument_type, tier, profile, buy_allowed, sell_mode)
                        VALUES
                            (:user_id, NULL, 'BTC', :instrument_type, 2, 'balanced', true, 'any')
                        """
                    ),
                    {
                        "user_id": user_id,
                        "instrument_type": InstrumentType.crypto.value,
                    },
                )
                await session.commit()
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_change_log_generic_fields() -> None:
    await _ensure_trade_profile_tables()
    user_id, _, _ = await _create_user_and_accounts()
    try:
        async with SessionLocal() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO profile_change_log
                        (user_id, change_type, target, old_value, new_value, reason, changed_by)
                    VALUES
                        (:user_id, 'update', 'asset_profile', '{"tier": 2}'::jsonb, '{"tier": 3}'::jsonb, 'rebalance tuning', 'test_runner')
                    """
                ),
                {"user_id": user_id},
            )
            await session.commit()

        async with SessionLocal() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT change_type, target, reason
                        FROM profile_change_log
                        WHERE user_id = :user_id
                        ORDER BY id DESC
                        LIMIT 1
                        """
                    ),
                    {"user_id": user_id},
                )
            ).one()

        assert row.change_type == "update"
        assert row.target == "asset_profile"
        assert row.reason == "rebalance tuning"
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_change_log_changed_by_not_null() -> None:
    """changed_by=NULL insert must be rejected by DB constraint."""
    await _ensure_trade_profile_tables()
    user_id, _, _ = await _create_user_and_accounts()
    try:
        async with SessionLocal() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        """
                        INSERT INTO profile_change_log
                            (user_id, change_type, target, old_value, new_value, reason, changed_by)
                        VALUES
                            (:user_id, 'update', 'asset_profile', '{}'::jsonb, '{}'::jsonb, 'test', NULL)
                        """
                    ),
                    {"user_id": user_id},
                )
                await session.commit()
    finally:
        await _cleanup_user(user_id)
