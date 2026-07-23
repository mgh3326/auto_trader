"""ROB-1038 R2 migration and database execution-boundary tests."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.services.daily_candles.provenance import with_equity_provenance
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)
from app.services.trade_journal import forecast_service as forecast_svc

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "20260722_rob1023_widen_runner"
R2_REVISION = "20260723_rob1038_r2"

_LEGACY_SCHEMA = """
CREATE SCHEMA review;

CREATE TABLE review.trade_forecasts (
    id BIGSERIAL PRIMARY KEY,
    forecast_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    artifact_uuid TEXT,
    journal_id BIGINT,
    report_uuid TEXT,
    report_item_uuid TEXT,
    correlation_id TEXT,
    created_by TEXT NOT NULL,
    session_label TEXT,
    model_label TEXT,
    policy_version TEXT,
    symbol TEXT NOT NULL,
    instrument_type TEXT NOT NULL,
    forecast_target JSONB NOT NULL,
    horizon TEXT,
    probability NUMERIC(5,4) NOT NULL,
    probability_range_low NUMERIC(5,4),
    probability_range_high NUMERIC(5,4),
    evidence_ids JSONB,
    contrary_evidence TEXT,
    resolution_source TEXT,
    forecast_start_date DATE,
    review_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    outcome BOOLEAN,
    observed_value NUMERIC(20,8),
    resolved_at TIMESTAMPTZ,
    brier_score NUMERIC(6,5),
    resolution_detail JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE public.kr_candles_1d (
    time TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    venue TEXT NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    volume NUMERIC NOT NULL,
    value NUMERIC NOT NULL,
    source TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (time, symbol, venue)
);

CREATE TABLE public.us_candles_1d (
    time TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    adj_close NUMERIC,
    volume NUMERIC NOT NULL,
    value NUMERIC NOT NULL,
    source TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (time, symbol, exchange)
);
"""


def _alembic_head() -> str:
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def test_r2_migration_is_the_single_head():
    assert _alembic_head() == R2_REVISION


@pytest.mark.asyncio
async def test_real_postgresql_upgrade_downgrade_upgrade_and_trigger_guards():
    base_url = make_url(settings.DATABASE_URL)
    assert base_url.get_backend_name() == "postgresql"
    assert base_url.database == "test_db", "migration test must start from test_db"

    database = f"test_rob1038_migration_{uuid4().hex}"
    admin = await asyncpg.connect(
        user=base_url.username,
        password=base_url.password,
        host=base_url.host,
        port=base_url.port,
        database="postgres",
    )
    await admin.execute(f'CREATE DATABASE "{database}"')
    target_url = base_url.set(database=database)
    target_url_text = target_url.render_as_string(hide_password=False)
    engine = create_async_engine(target_url_text)
    legacy_id = uuid4()
    attested_legacy_id = uuid4()
    superseded_legacy_id = uuid4()
    successor_terminal_id = uuid4()
    terminal_id = uuid4()
    bound_terminal_id = uuid4()

    env = {
        **os.environ,
        "DATABASE_URL": target_url_text,
        "ENVIRONMENT": "test",
    }

    def alembic(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(REPO / ".venv/bin/alembic"), *args],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    async def run_alembic(*args: str, succeeds: bool = True):
        completed = await asyncio.to_thread(alembic, *args)
        if succeeds:
            assert completed.returncode == 0, completed.stdout + completed.stderr
        else:
            assert completed.returncode != 0
        return completed

    try:
        async with engine.begin() as connection:
            for statement in _LEGACY_SCHEMA.split(";"):
                if statement.strip():
                    await connection.execute(text(statement))
            await connection.execute(
                text(
                    """
                    INSERT INTO review.trade_forecasts (
                        forecast_id, created_by, symbol, instrument_type,
                        forecast_target, probability, review_date
                    ) VALUES (
                        :forecast_id, 'legacy-writer', 'SMCI', 'equity_us',
                        CAST(:target AS JSONB), 0.6, DATE '2026-06-05'
                    )
                    """
                ),
                {
                    "forecast_id": legacy_id,
                    "target": json.dumps(
                        {
                            "kind": "price_target",
                            "direction": "at_or_above",
                            "target_price": 130.0,
                        }
                    ),
                },
            )
            for forecast_id, symbol in (
                (attested_legacy_id, "AAPL"),
                (superseded_legacy_id, "MSFT"),
            ):
                await connection.execute(
                    text(
                        """
                        INSERT INTO review.trade_forecasts (
                            forecast_id, created_by, symbol, instrument_type,
                            forecast_target, probability, review_date
                        ) VALUES (
                            :forecast_id, 'legacy-writer', :symbol, 'equity_us',
                            CAST(:target AS JSONB), 0.6, DATE '2026-06-05'
                        )
                        """
                    ),
                    {
                        "forecast_id": forecast_id,
                        "symbol": symbol,
                        "target": json.dumps(
                            {
                                "kind": "price_target",
                                "direction": "at_or_above",
                                "target_price": 130.0,
                            }
                        ),
                    },
                )

        await run_alembic("stamp", PREVIOUS_REVISION)
        await run_alembic("upgrade", "head")

        async with engine.connect() as connection:
            columns = set(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_schema = 'review'
                              AND table_name = 'trade_forecasts'
                            """
                        )
                    )
                ).scalars()
            )
            assert {
                "immutable_claim",
                "immutable_claim_hash",
                "target_version",
                "resolution_semantics_status",
                "semantics_evidence",
                "supersedes_forecast_id",
                "superseded_by_forecast_id",
            } <= columns
            trigger_count = await connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM pg_trigger
                    WHERE tgrelid = 'review.trade_forecasts'::regclass
                      AND tgname = 'trg_trade_forecasts_semantics_guard'
                      AND NOT tgisinternal
                    """
                )
            )
            assert trigger_count == 1
            foreign_keys = set(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT conname
                            FROM pg_constraint
                            WHERE conrelid =
                                'review.trade_forecasts'::regclass
                              AND contype = 'f'
                            """
                        )
                    )
                ).scalars()
            )
            assert {
                "fk_trade_forecasts_supersedes_forecast_id",
                "fk_trade_forecasts_superseded_by_forecast_id",
            } <= foreign_keys

        attestation_evidence = {
            "contract_version": "forecast-semantics-attestation-v1",
            "authority_type": "service",
            "actor_principal": "service:migration-test",
            "authentication_method": "service_identity",
            "source_target_sha256": "1" * 64,
            "evidence_sha256": "2" * 64,
            "evidence_ref": "test://forecast-semantics/attestation",
            "reason": "migration trigger positive path",
            "attested_at": "2026-07-23T15:30:00+09:00",
            "authentication_binding": {
                "contract_version": "forecast-evidence-authentication-v1",
                "actor_principal": "service:migration-test",
                "authentication_method": "service_identity",
                "provenance_sha256": "3" * 64,
            },
            "decision": "window_touch",
        }
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE review.trade_forecasts
                    SET forecast_target = CAST(:target AS JSONB),
                        immutable_claim =
                            '{"target_kind":"price_target"}'::jsonb,
                        immutable_claim_hash = :claim_hash,
                        target_version = 1,
                        resolution_semantics_status = 'active',
                        semantics_evidence = CAST(:evidence AS JSONB)
                    WHERE forecast_id = :forecast_id
                    """
                ),
                {
                    "forecast_id": attested_legacy_id,
                    "target": json.dumps(
                        {
                            "kind": "price_target",
                            "direction": "at_or_above",
                            "target_price": 130.0,
                            "outcome_rule_version": (
                                "window-touch-v1-high-gte-low-lte"
                            ),
                        }
                    ),
                    "claim_hash": "4" * 64,
                    "evidence": json.dumps(attestation_evidence),
                },
            )

        with pytest.raises(DBAPIError, match="attestation evidence is immutable"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE review.trade_forecasts
                        SET semantics_evidence = '{}'::jsonb
                        WHERE forecast_id = :forecast_id
                        """
                    ),
                    {"forecast_id": attested_legacy_id},
                )

        supersession_evidence = {
            "contract_version": "forecast-semantics-supersession-v1",
            "authority_type": "service",
            "actor_principal": "service:migration-test",
            "authentication_method": "service_identity",
            "source_target_sha256": "5" * 64,
            "evidence_sha256": "6" * 64,
            "evidence_ref": "test://forecast-semantics/supersession",
            "reason": "migration trigger positive path",
            "attested_at": "2026-07-23T15:30:00+09:00",
            "authentication_binding": {
                "contract_version": "forecast-evidence-authentication-v1",
                "actor_principal": "service:migration-test",
                "authentication_method": "service_identity",
                "provenance_sha256": "7" * 64,
            },
            "decision": "terminal_close",
            "from_forecast_id": str(superseded_legacy_id),
            "to_forecast_id": str(successor_terminal_id),
        }
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO review.trade_forecasts (
                        forecast_id, created_by, symbol, instrument_type,
                        forecast_target, immutable_claim, immutable_claim_hash,
                        target_version, resolution_semantics_status,
                        semantics_evidence, supersedes_forecast_id,
                        probability, review_date
                    ) VALUES (
                        :forecast_id, 'legacy-writer', 'MSFT', 'equity_us',
                        CAST(:target AS JSONB),
                        '{"target_kind":"terminal_close"}'::jsonb,
                        :claim_hash, 1, 'quarantined',
                        CAST(:evidence AS JSONB), :supersedes_forecast_id,
                        0.6, DATE '2026-06-05'
                    )
                    """
                ),
                {
                    "forecast_id": successor_terminal_id,
                    "target": json.dumps(
                        {
                            "kind": "terminal_close",
                            "direction": "up",
                            "target_price": 130.0,
                            "outcome_rule_version": (
                                "terminal-close-v1-up-gte-down-lt"
                            ),
                            "price_adjustment_policy": "unverified_fail_closed",
                        }
                    ),
                    "claim_hash": "8" * 64,
                    "evidence": json.dumps(supersession_evidence),
                    "supersedes_forecast_id": superseded_legacy_id,
                },
            )
            await connection.execute(
                text(
                    """
                    UPDATE review.trade_forecasts
                    SET resolution_semantics_status = 'superseded',
                        semantics_evidence = CAST(:evidence AS JSONB),
                        superseded_by_forecast_id = :successor_id
                    WHERE forecast_id = :legacy_id
                    """
                ),
                {
                    "legacy_id": superseded_legacy_id,
                    "successor_id": successor_terminal_id,
                    "evidence": json.dumps(supersession_evidence),
                },
            )

        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        def candle(
            symbol: str,
            *,
            candle_date: date,
            close: float = 129.0,
            hour: int = 0,
            final: bool = True,
            source: str = "kis",
        ) -> DailyCandleRow:
            row = DailyCandleRow(
                time_utc=datetime(
                    candle_date.year,
                    candle_date.month,
                    candle_date.day,
                    hour,
                    tzinfo=UTC,
                ),
                symbol=symbol,
                partition="KRX",
                open=close,
                high=max(close, 1.0) + 5.0,
                low=max(close - 5.0, 0.0),
                close=close,
                adj_close=None,
                volume=1000.0,
                value=max(close, 0.0) * 1000.0,
                source=source,
            )
            return with_equity_provenance(
                row,
                final_through_date=candle_date if final else None,
            )

        repository_cases = [
            (
                "R2STALE",
                [candle("R2STALE", candle_date=date(2026, 6, 4))],
                "unresolved_stale_data",
            ),
            (
                "R2FORMING",
                [
                    candle(
                        "R2FORMING",
                        candle_date=date(2026, 6, 5),
                        final=False,
                    )
                ],
                "unresolved_non_final_candle",
            ),
            (
                "R2DUPLICATE",
                [
                    candle("R2DUPLICATE", candle_date=date(2026, 6, 5)),
                    candle(
                        "R2DUPLICATE",
                        candle_date=date(2026, 6, 5),
                        hour=12,
                        close=131.0,
                    ),
                ],
                "unresolved_ambiguous_review_candle",
            ),
            (
                "R2INVALID",
                [
                    candle(
                        "R2INVALID",
                        candle_date=date(2026, 6, 5),
                        close=0.0,
                    )
                ],
                "unresolved_invalid_close",
            ),
            (
                "R2EXTENDED",
                [
                    candle(
                        "R2EXTENDED",
                        candle_date=date(2026, 6, 5),
                        source="yahoo_extended",
                    )
                ],
                "unresolved_untrusted_source",
            ),
        ]
        async with session_factory() as session:
            repo = DailyCandlesRepository(session=session)
            for symbol, rows, expected_status in repository_cases:
                await repo.upsert_rows(market=MarketKey.KR, rows=rows)
                await session.commit()
                fetched = await forecast_svc._read_window_candles(
                    session,
                    symbol=symbol,
                    instrument_type="equity_kr",
                    start_date=date(2026, 5, 29),
                    review_date=date(2026, 6, 5),
                )
                assert fetched is not None
                with pytest.raises(forecast_svc.TerminalCloseDataError) as data_error:
                    forecast_svc.classify_terminal_close_outcome(
                        fetched,
                        review_date=date(2026, 6, 5),
                        direction="up",
                        target_price=130.0,
                    )
                assert data_error.value.status == expected_status

            valid = candle("R2VALID", candle_date=date(2026, 6, 5), close=131.0)
            await repo.upsert_rows(market=MarketKey.KR, rows=[valid])
            await session.commit()
            fetched = await forecast_svc._read_window_candles(
                session,
                symbol="R2VALID",
                instrument_type="equity_kr",
                start_date=date(2026, 6, 5),
                review_date=date(2026, 6, 5),
                for_share=True,
            )
            assert fetched is not None
            outcome, observed, selected = forecast_svc.classify_terminal_close_outcome(
                fetched,
                review_date=date(2026, 6, 5),
                direction="up",
                target_price=130.0,
            )
            assert outcome is True
            assert observed == pytest.approx(131.0)
            assert selected.ingested_at is not None
            assert selected.source_row_id == valid.source_row_id

        with pytest.raises(DBAPIError, match="versionless/unattested"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE review.trade_forecasts
                        SET status = 'closed', outcome = TRUE,
                            brier_score = 0.16, resolved_at = now()
                        WHERE forecast_id = :forecast_id
                        """
                    ),
                    {"forecast_id": legacy_id},
                )

        with pytest.raises(DBAPIError, match="requires typed attestation"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE review.trade_forecasts
                        SET forecast_target = '{"kind":"thesis_holds"}'::jsonb
                        WHERE forecast_id = :forecast_id
                        """
                    ),
                    {"forecast_id": legacy_id},
                )

        with pytest.raises(DBAPIError, match="linked typed evidence"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE review.trade_forecasts
                        SET resolution_semantics_status = 'superseded',
                            superseded_by_forecast_id = gen_random_uuid(),
                            semantics_evidence = CAST(:evidence AS JSONB)
                        WHERE forecast_id = :forecast_id
                        """
                    ),
                    {
                        "forecast_id": legacy_id,
                        "evidence": json.dumps(
                            {
                                "contract_version": (
                                    "forecast-semantics-supersession-v1"
                                ),
                                "authentication_binding": {
                                    "contract_version": (
                                        "forecast-evidence-authentication-v1"
                                    )
                                },
                                "from_forecast_id": str(legacy_id),
                                "to_forecast_id": str(uuid4()),
                            }
                        ),
                    },
                )

        with pytest.raises(DBAPIError, match="typed touch evidence"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO review.trade_forecasts (
                            created_by, symbol, instrument_type, forecast_target,
                            probability, review_date
                        ) VALUES (
                            'new-direct-writer', 'AAPL', 'equity_us',
                            CAST(:target AS JSONB), 0.5, DATE '2026-06-05'
                        )
                        """
                    ),
                    {
                        "target": json.dumps(
                            {
                                "kind": "price_target",
                                "direction": "at_or_above",
                                "target_price": 200.0,
                            }
                        )
                    },
                )

        terminal_target = {
            "kind": "terminal_close",
            "direction": "up",
            "target_price": 130.0,
            "outcome_rule_version": "terminal-close-v1-up-gte-down-lt",
            "price_adjustment_policy": "unverified_fail_closed",
        }
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO review.trade_forecasts (
                        forecast_id, created_by, symbol, instrument_type,
                        forecast_target, immutable_claim, immutable_claim_hash,
                        target_version, resolution_semantics_status,
                        probability, review_date
                    ) VALUES (
                        :forecast_id, 'typed-writer', 'SMCI', 'equity_us',
                        CAST(:target AS JSONB),
                        CAST(:claim AS JSONB), :claim_hash, 1, 'quarantined',
                        0.48, DATE '2026-06-05'
                    )
                    """
                ),
                {
                    "forecast_id": terminal_id,
                    "target": json.dumps(terminal_target),
                    "claim": json.dumps({"target_kind": "terminal_close"}),
                    "claim_hash": "a" * 64,
                },
            )

        explicit_terminal_target = {
            **terminal_target,
            "price_adjustment_policy": "explicit-factor-v1",
            "target_to_close_factor": 1.0,
            "adjustment_provenance": {
                "contract_version": "corporate-action-adjustment-v1",
                "authority_type": "licensed_data_vendor",
                "authority_id": "KIS",
                "actor_principal": "service:migration-test",
                "authentication_method": "service_identity",
                "symbol": "SMCI",
                "action_type": "none",
                "action_ratio": 1.0,
                "effective_date": "2026-06-05",
                "verified_through_date": "2026-06-05",
                "source": "migration test evidence",
                "source_ref": "test://corporate-actions/SMCI/2026-06-05",
                "source_sha256": "d" * 64,
                "source_price_basis": "provider_adjusted",
            },
        }
        with pytest.raises(DBAPIError, match="immutable evidence"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO review.trade_forecasts (
                            created_by, symbol, instrument_type, forecast_target,
                            immutable_claim, immutable_claim_hash, target_version,
                            resolution_semantics_status, probability, review_date
                        ) VALUES (
                            'direct-writer', 'SMCI', 'equity_us',
                            CAST(:target AS JSONB),
                            '{"target_kind":"terminal_close"}'::jsonb,
                            :claim_hash, 1, 'active', 0.48, DATE '2026-06-05'
                        )
                        """
                    ),
                    {
                        "target": json.dumps(explicit_terminal_target),
                        "claim_hash": "e" * 64,
                    },
                )

        bound_semantics_evidence = {
            "contract_version": "terminal-adjustment-evidence-v1",
            "adjustment_authentication": {
                "contract_version": "forecast-evidence-authentication-v1",
                "actor_principal": "service:migration-test",
                "authentication_method": "service_identity",
                "provenance_sha256": "f" * 64,
            },
        }
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO review.trade_forecasts (
                        forecast_id, created_by, symbol, instrument_type,
                        forecast_target, immutable_claim, immutable_claim_hash,
                        target_version, resolution_semantics_status,
                        semantics_evidence, probability, review_date
                    ) VALUES (
                        :forecast_id, 'typed-writer', 'SMCI', 'equity_us',
                        CAST(:target AS JSONB),
                        '{"target_kind":"terminal_close"}'::jsonb,
                        :claim_hash, 1, 'active', CAST(:evidence AS JSONB),
                        0.48, DATE '2026-06-05'
                    )
                    """
                ),
                {
                    "forecast_id": bound_terminal_id,
                    "target": json.dumps(explicit_terminal_target),
                    "claim_hash": "e" * 64,
                    "evidence": json.dumps(bound_semantics_evidence),
                },
            )

        with pytest.raises(DBAPIError, match="semantics evidence mutation"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE review.trade_forecasts
                        SET semantics_evidence = '{}'::jsonb
                        WHERE forecast_id = :forecast_id
                        """
                    ),
                    {"forecast_id": bound_terminal_id},
                )

        with pytest.raises(DBAPIError, match="only terminal adjustment promotion"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE review.trade_forecasts
                        SET forecast_target = '{"kind":"thesis_holds"}'::jsonb
                        WHERE forecast_id = :forecast_id
                        """
                    ),
                    {"forecast_id": terminal_id},
                )

        with pytest.raises(DBAPIError, match="authenticated adjustment evidence"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE review.trade_forecasts
                        SET status = 'closed', outcome = TRUE,
                            brier_score = 0.2704, resolved_at = now(),
                            resolution_source = 'manual',
                            resolution_detail = '{"manual_evidence":["bypass"]}'::jsonb
                        WHERE forecast_id = :forecast_id
                        """
                    ),
                    {"forecast_id": terminal_id},
                )

        with pytest.raises(DBAPIError, match="final_provenance"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO public.kr_candles_1d (
                            time, symbol, venue, open, high, low, close,
                            volume, value, source, is_final
                        ) VALUES (
                            now(), 'BADFINAL', 'KRX', 1, 2, 1, 2,
                            10, 20, 'kis', TRUE
                        )
                        """
                    )
                )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO public.kr_candles_1d (
                        time, symbol, venue, open, high, low, close,
                        volume, value, source, is_final, session_scope,
                        source_row_id, source_row_version, price_basis
                    ) VALUES (
                        now(), 'GOODFINAL', 'KRX', 1, 2, 1, 2,
                        10, 20, 'kis', TRUE, 'regular',
                        :source_row_id, 'kis-adjusted-daily-v1',
                        'provider_adjusted'
                    )
                    """
                ),
                {"source_row_id": "c" * 64},
            )

        refused = await run_alembic(
            "downgrade",
            PREVIOUS_REVISION,
            succeeds=False,
        )
        assert "downgrade refused" in refused.stderr

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM review.trade_forecasts "
                    "WHERE forecast_id IN ("
                    ":terminal_id, :bound_terminal_id, :attested_legacy_id, "
                    ":superseded_legacy_id, :successor_terminal_id)"
                ),
                {
                    "terminal_id": terminal_id,
                    "bound_terminal_id": bound_terminal_id,
                    "attested_legacy_id": attested_legacy_id,
                    "superseded_legacy_id": superseded_legacy_id,
                    "successor_terminal_id": successor_terminal_id,
                },
            )
            await connection.execute(
                text("DELETE FROM public.kr_candles_1d WHERE symbol = 'GOODFINAL'")
            )
            await connection.execute(
                text("DELETE FROM public.kr_candles_1d WHERE symbol LIKE 'R2%'")
            )

        await run_alembic("downgrade", PREVIOUS_REVISION)
        async with engine.connect() as connection:
            migrated_column = await connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'review'
                      AND table_name = 'trade_forecasts'
                      AND column_name = 'immutable_claim'
                    """
                )
            )
            assert migrated_column == 0

        await run_alembic("upgrade", "head")
        current = await run_alembic("current")
        assert f"{R2_REVISION} (head)" in current.stdout
    finally:
        await engine.dispose()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.close()
