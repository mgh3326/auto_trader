from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine
from app.models.trading import InstrumentType
from scripts.seed_trade_profiles import seed_trade_profiles

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


async def _tables_ready() -> bool:
    try:
        async with SessionLocal() as session:
            row = await session.execute(text("SELECT to_regclass('asset_profiles')"))
            return row.scalar_one_or_none() is not None
    except Exception:
        return False


async def _create_user_and_accounts() -> tuple[int, str, str]:
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
                    "username": f"seed_profile_user_{suffix}",
                    "email": f"seed_profile_{suffix}@example.com",
                },
            )
        ).scalar_one()
        kis_name = f"kis_{suffix}"
        upbit_name = f"upbit_{suffix}"

        await session.execute(
            text(
                """
                INSERT INTO broker_accounts
                    (user_id, broker_type, account_name, is_mock, is_active)
                VALUES
                    (:user_id, 'kis', :kis_name, false, true),
                    (:user_id, 'upbit', :upbit_name, false, true)
                """
            ),
            {
                "user_id": user_id,
                "kis_name": kis_name,
                "upbit_name": upbit_name,
            },
        )
        await session.commit()
        return user_id, kis_name, upbit_name


async def _cleanup_user(user_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id}
        )
        await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_trade_profile_seed_idempotency_and_crypto_common() -> None:
    if not await _tables_ready():
        pytest.skip("trade profile tables are not migrated")

    user_id, kis_account_name, upbit_account_name = await _create_user_and_accounts()
    try:
        await seed_trade_profiles(
            user_id=user_id,
            updated_by="integration_test",
            kis_account_name=kis_account_name,
            upbit_account_name=upbit_account_name,
        )
        await seed_trade_profiles(
            user_id=user_id,
            updated_by="integration_test",
            kis_account_name=kis_account_name,
            upbit_account_name=upbit_account_name,
        )

        async with SessionLocal() as session:
            profile_count = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM asset_profiles WHERE user_id = :user_id"
                    ),
                    {"user_id": user_id},
                )
            ).scalar_one()
            assert profile_count > 0

            kr_row = (
                await session.execute(
                    text(
                        """
                        SELECT symbol FROM asset_profiles
                        WHERE user_id = :user_id
                          AND instrument_type = :instrument_type
                        ORDER BY id ASC
                        LIMIT 1
                        """
                    ),
                    {
                        "user_id": user_id,
                        "instrument_type": InstrumentType.equity_kr.value,
                    },
                )
            ).scalar_one_or_none()
            assert kr_row is not None
            assert isinstance(kr_row, str)
            assert len(kr_row) == 6
            assert kr_row.isdigit()

            # Verify KR symbol is from agreed-upon mapping set
            agreed_kr_codes = {
                "012450",
                "003230",
                "329180",
                "259960",
                "035420",
                "214450",
                "087010",
                "196170",
            }
            assert kr_row in agreed_kr_codes, (
                f"KR symbol {kr_row} not in agreed mapping set"
            )

            common_count = (
                await session.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM tier_rule_params
                        WHERE user_id = :user_id
                          AND instrument_type = :instrument_type
                          AND param_type = 'common'
                        """
                    ),
                    {
                        "user_id": user_id,
                        "instrument_type": InstrumentType.crypto.value,
                    },
                )
            ).scalar_one()
            assert common_count > 0

            # Verify common params structure matches agreed spec
            common_row = (
                await session.execute(
                    text(
                        """
                        SELECT params FROM tier_rule_params
                        WHERE user_id = :user_id
                          AND instrument_type = :instrument_type
                          AND param_type = 'common'
                        ORDER BY id ASC
                        LIMIT 1
                        """
                    ),
                    {
                        "user_id": user_id,
                        "instrument_type": InstrumentType.crypto.value,
                    },
                )
            ).scalar_one()
            assert common_row["price_base"] == "close_1"
            assert isinstance(common_row["regime_ema"], list)
            assert common_row["regime_ema"] == [60, 200]
            assert isinstance(common_row["max_concurrent_orders"], int)

            crypto_symbols = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT symbol FROM asset_profiles
                        WHERE user_id = :user_id
                          AND instrument_type = :instrument_type
                        ORDER BY symbol ASC
                        """
                        ),
                        {
                            "user_id": user_id,
                            "instrument_type": InstrumentType.crypto.value,
                        },
                    )
                )
                .scalars()
                .all()
            )
            assert crypto_symbols
            assert all(symbol.startswith("KRW-") for symbol in crypto_symbols)

            total_rows_after_second_seed = (
                await session.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM tier_rule_params
                        WHERE user_id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                )
            ).scalar_one()

        await seed_trade_profiles(
            user_id=user_id,
            updated_by="integration_test",
            kis_account_name=kis_account_name,
            upbit_account_name=upbit_account_name,
        )

        async with SessionLocal() as session:
            total_rows_after_third_seed = (
                await session.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM tier_rule_params
                        WHERE user_id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                )
            ).scalar_one()
            assert total_rows_after_second_seed == total_rows_after_third_seed

            await session.execute(
                text(
                    """
                    INSERT INTO profile_change_log
                        (user_id, change_type, target, old_value, new_value, reason, changed_by)
                    VALUES
                        (:user_id, 'update', 'asset_profile', '{"old": true}'::jsonb, '{"new": true}'::jsonb, 'test', 'integration_test')
                    """
                ),
                {"user_id": user_id},
            )
            await session.commit()

            log_count = (
                await session.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM profile_change_log
                        WHERE user_id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                )
            ).scalar_one()
            assert log_count == 1
    finally:
        await _cleanup_user(user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_trade_profile_seed_fails_fast_on_ambiguous_accounts() -> None:
    if not await _tables_ready():
        pytest.skip("trade profile tables are not migrated")

    user_id, _, _ = await _create_user_and_accounts()
    try:
        async with SessionLocal() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO broker_accounts
                        (user_id, broker_type, account_name, is_mock, is_active)
                    VALUES
                        (:user_id, 'upbit', :account_name, false, true)
                    """
                ),
                {
                    "user_id": user_id,
                    "account_name": f"upbit_extra_{uuid.uuid4().hex[:6]}",
                },
            )
            await session.commit()

        with pytest.raises(ValueError, match="Ambiguous broker account selection"):
            await seed_trade_profiles(user_id=user_id, updated_by="integration_test")
    finally:
        await _cleanup_user(user_id)
