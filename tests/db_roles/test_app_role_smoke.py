"""Disposable database smoke for the post-cutover application login.

Run with pytest --noconftest and a localhost auto_trader fixture URL. The
ordinary suite creates its own database and intentionally needs admin rights.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.core.db import build_engine
from app.services.stock_info_service import StockInfoService


@pytest.mark.live
def test_application_login_can_use_normal_service_and_not_control_rows() -> None:
    url = make_url(os.environ["DATABASE_URL"])
    assert url.host in {"127.0.0.1", "localhost"}
    assert url.database == "auto_trader"
    assert url.username == "at_api_login"
    fixture_nonce = os.environ["AT789_FIXTURE_NONCE"]
    assert len(fixture_nonce) == 48

    async def exercise() -> None:
        symbol = f"T789-{uuid4().hex[:20]}"
        engine = build_engine()
        try:
            from sqlalchemy.ext.asyncio import AsyncSession

            async with AsyncSession(engine) as session:
                identity = (
                    await session.execute(text("SELECT session_user, current_user"))
                ).one()
                assert identity == ("at_api_login", "at_api_login")
                stored_nonce = await session.scalar(
                    text("SELECT current_setting('t789.fixture_nonce', true)")
                )
                assert stored_nonce == fixture_nonce
                paper_accounts = await session.scalar(
                    text("SELECT count(*) FROM paper.paper_accounts")
                )
                assert paper_accounts == 0

                service = StockInfoService(session)
                stock = await service.create_stock_info(
                    {
                        "symbol": symbol,
                        "name": "Role fixture",
                        "instrument_type": "equity_us",
                    }
                )
                found = await service.get_stock_info_by_symbol(symbol)
                assert found is not None and found.id == stock.id
                changed = await service.update_stock_info(
                    stock.id, {"name": "Role updated"}
                )
                assert changed is not None and changed.name == "Role updated"

                # A normal #711 intent insert exercises the generated digest and
                # SECURITY DEFINER trigger with only digest helpers executable.
                account_ref = str(uuid4())
                await session.execute(
                    text(
                        "INSERT INTO review.nhplug_mock_account_ref(account_ref) "
                        "VALUES (CAST(:account_ref AS uuid))"
                    ),
                    {"account_ref": account_ref},
                )
                intent = (
                    await session.execute(
                        text(
                            "INSERT INTO review.nhplug_mock_order_ledger "
                            "(client_request_id,account_ref,idempotency_key,attempt_no,"
                            "order_date,operation_kind,side,symbol,quantity,price) "
                            "VALUES (CAST(:request_id AS uuid),CAST(:account_ref AS uuid),"
                            ":idempotency_key,1,current_date,'place','buy','005930',1,1000) "
                            "RETURNING id,body_digest"
                        ),
                        {
                            "request_id": str(uuid4()),
                            "account_ref": account_ref,
                            "idempotency_key": "T789" + uuid4().hex,
                        },
                    )
                ).one()
                assert intent.id > 0 and len(intent.body_digest) == 64
                await session.commit()

                checks = (
                    await session.execute(
                        text(
                            "SELECT "
                            "has_schema_privilege(current_user, 'public', 'CREATE'), "
                            "has_table_privilege(current_user, "
                            "  'review.nhplug_mock_operator_authorization', 'INSERT'), "
                            "has_table_privilege(current_user, "
                            "  'review.kiwoom_authority_attempts', 'UPDATE'), "
                            "has_function_privilege(current_user, "
                            "  'review.nhplug_consume_authorization(uuid,text,bigint,uuid,date,text,text,bigint)', "
                            "  'EXECUTE'), "
                            "has_function_privilege(current_user, "
                            "  'review.nhplug_body_field(text,text)', 'EXECUTE'), "
                            "has_function_privilege(current_user, "
                            "  'review.nhplug_body_digest_v1(text,text,text,bigint,bigint,text,text,text)', "
                            "  'EXECUTE'), "
                            "has_function_privilege(current_user, "
                            "  'review.nhplug_order_guard()', 'EXECUTE')"
                        )
                    )
                ).one()
                assert checks == (False, False, False, False, True, True, False)
                await session.execute(
                    text(
                        "SELECT id FROM review.nhplug_mock_operator_authorization LIMIT 1"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
