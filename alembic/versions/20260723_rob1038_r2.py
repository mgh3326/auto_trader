"""ROB-1038 R2 immutable forecast semantics and final candle provenance.

Revision ID: 20260723_rob1038_r2
Revises: 20260722_rob1023_widen_runner
Create Date: 2026-07-23 15:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260723_rob1038_r2"
down_revision: str | Sequence[str] | None = "20260722_rob1023_widen_runner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FORECAST_TABLE = "trade_forecasts"
_FORECAST_SCHEMA = "review"
_CANDLE_TABLES = ("kr_candles_1d", "us_candles_1d")


def _add_candle_provenance(table: str) -> None:
    op.add_column(table, sa.Column("is_final", sa.Boolean(), nullable=True))
    op.add_column(table, sa.Column("session_scope", sa.Text(), nullable=True))
    op.add_column(table, sa.Column("source_row_id", sa.Text(), nullable=True))
    op.add_column(table, sa.Column("source_row_version", sa.Text(), nullable=True))
    op.add_column(table, sa.Column("price_basis", sa.Text(), nullable=True))
    op.create_check_constraint(
        f"ck_{table}_session_scope",
        table,
        "session_scope IS NULL OR "
        "session_scope IN ('regular','extended','mixed','unknown')",
    )
    op.create_check_constraint(
        f"ck_{table}_price_basis",
        table,
        "price_basis IS NULL OR price_basis IN ('raw','provider_adjusted')",
    )
    op.create_check_constraint(
        f"ck_{table}_final_provenance",
        table,
        "is_final IS DISTINCT FROM TRUE OR "
        "(session_scope = 'regular' "
        "AND NULLIF(btrim(source_row_id), '') IS NOT NULL "
        "AND NULLIF(btrim(source_row_version), '') IS NOT NULL "
        "AND price_basis IN ('raw','provider_adjusted'))",
    )


def upgrade() -> None:
    op.add_column(
        _FORECAST_TABLE,
        sa.Column(
            "immutable_claim",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema=_FORECAST_SCHEMA,
    )
    op.add_column(
        _FORECAST_TABLE,
        sa.Column("immutable_claim_hash", sa.String(length=64), nullable=True),
        schema=_FORECAST_SCHEMA,
    )
    op.add_column(
        _FORECAST_TABLE,
        sa.Column(
            "target_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        schema=_FORECAST_SCHEMA,
    )
    op.add_column(
        _FORECAST_TABLE,
        sa.Column("resolution_semantics_status", sa.Text(), nullable=True),
        schema=_FORECAST_SCHEMA,
    )
    op.add_column(
        _FORECAST_TABLE,
        sa.Column(
            "semantics_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema=_FORECAST_SCHEMA,
    )
    op.add_column(
        _FORECAST_TABLE,
        sa.Column(
            "supersedes_forecast_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        schema=_FORECAST_SCHEMA,
    )
    op.add_column(
        _FORECAST_TABLE,
        sa.Column(
            "superseded_by_forecast_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        schema=_FORECAST_SCHEMA,
    )

    op.create_foreign_key(
        "fk_trade_forecasts_supersedes_forecast_id",
        _FORECAST_TABLE,
        _FORECAST_TABLE,
        ["supersedes_forecast_id"],
        ["forecast_id"],
        source_schema=_FORECAST_SCHEMA,
        referent_schema=_FORECAST_SCHEMA,
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_trade_forecasts_superseded_by_forecast_id",
        _FORECAST_TABLE,
        _FORECAST_TABLE,
        ["superseded_by_forecast_id"],
        ["forecast_id"],
        source_schema=_FORECAST_SCHEMA,
        referent_schema=_FORECAST_SCHEMA,
        ondelete="RESTRICT",
    )

    op.create_check_constraint(
        "ck_trade_forecasts_target_version",
        _FORECAST_TABLE,
        "target_version >= 0",
        schema=_FORECAST_SCHEMA,
    )
    op.create_check_constraint(
        "ck_trade_forecasts_resolution_semantics_status",
        _FORECAST_TABLE,
        "resolution_semantics_status IS NULL OR "
        "resolution_semantics_status IN ('active','quarantined','superseded')",
        schema=_FORECAST_SCHEMA,
    )
    op.create_check_constraint(
        "ck_trade_forecasts_immutable_claim_pair",
        _FORECAST_TABLE,
        "(immutable_claim IS NULL) = (immutable_claim_hash IS NULL)",
        schema=_FORECAST_SCHEMA,
    )
    op.create_check_constraint(
        "ck_trade_forecasts_immutable_claim_hash",
        _FORECAST_TABLE,
        "immutable_claim_hash IS NULL OR immutable_claim_hash ~ '^[0-9a-f]{64}$'",
        schema=_FORECAST_SCHEMA,
    )
    op.create_check_constraint(
        "ck_trade_forecasts_supersedes_not_self",
        _FORECAST_TABLE,
        "supersedes_forecast_id IS NULL OR supersedes_forecast_id <> forecast_id",
        schema=_FORECAST_SCHEMA,
    )
    op.create_check_constraint(
        "ck_trade_forecasts_superseded_by_not_self",
        _FORECAST_TABLE,
        "superseded_by_forecast_id IS NULL OR superseded_by_forecast_id <> forecast_id",
        schema=_FORECAST_SCHEMA,
    )
    op.create_index(
        "ix_trade_forecasts_semantics_due",
        _FORECAST_TABLE,
        ["status", "resolution_semantics_status", "review_date"],
        schema=_FORECAST_SCHEMA,
    )
    op.create_index(
        "ix_trade_forecasts_supersedes",
        _FORECAST_TABLE,
        ["supersedes_forecast_id"],
        schema=_FORECAST_SCHEMA,
    )
    op.create_index(
        "ix_trade_forecasts_superseded_by",
        _FORECAST_TABLE,
        ["superseded_by_forecast_id"],
        schema=_FORECAST_SCHEMA,
    )

    for table in _CANDLE_TABLES:
        _add_candle_provenance(table)

    op.execute(
        """
        CREATE OR REPLACE FUNCTION review.enforce_trade_forecast_semantics()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            original_kind TEXT;
            resolving BOOLEAN;
            target_changed BOOLEAN;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.forecast_target->>'kind' = 'price_target'
                   AND (
                        NEW.forecast_target->>'outcome_rule_version'
                            IS DISTINCT FROM
                            'window-touch-v1-high-gte-low-lte'
                        OR NEW.target_version < 1
                        OR NEW.immutable_claim IS NULL
                        OR NEW.immutable_claim_hash IS NULL
                        OR NEW.immutable_claim->>'target_kind'
                            IS DISTINCT FROM 'price_target'
                        OR NEW.resolution_semantics_status
                            IS DISTINCT FROM 'active'
                        OR (
                            NEW.semantics_evidence IS NOT NULL
                            AND jsonb_typeof(NEW.semantics_evidence)
                                IS DISTINCT FROM 'null'
                        )
                   )
                THEN
                    RAISE EXCEPTION
                        'ROB-1038: new price_target requires typed touch evidence';
                END IF;

                IF NEW.forecast_target->>'kind' = 'terminal_close'
                   AND (
                        NEW.forecast_target->>'outcome_rule_version'
                            IS DISTINCT FROM
                            'terminal-close-v1-up-gte-down-lt'
                        OR NEW.target_version < 1
                        OR NEW.immutable_claim IS NULL
                        OR NEW.immutable_claim_hash IS NULL
                        OR NEW.immutable_claim->>'target_kind'
                            IS DISTINCT FROM 'terminal_close'
                        OR NEW.resolution_semantics_status IS NULL
                        OR NEW.resolution_semantics_status
                            NOT IN ('active', 'quarantined')
                        OR NEW.forecast_target->>'price_adjustment_policy'
                            IS NULL
                        OR NEW.forecast_target->>'price_adjustment_policy'
                            NOT IN (
                                'unverified_fail_closed',
                                'explicit-factor-v1'
                            )
                        OR (
                            NEW.forecast_target->>'price_adjustment_policy'
                                = 'explicit-factor-v1'
                            AND NEW.semantics_evidence
                                ->'adjustment_authentication'
                                ->>'contract_version'
                                IS DISTINCT FROM
                                'forecast-evidence-authentication-v1'
                        )
                        OR (
                            NEW.supersedes_forecast_id IS NULL
                            AND NEW.forecast_target
                                    ->>'price_adjustment_policy'
                                = 'explicit-factor-v1'
                            AND NEW.semantics_evidence->>'contract_version'
                                IS DISTINCT FROM
                                'terminal-adjustment-evidence-v1'
                        )
                        OR (
                            NEW.supersedes_forecast_id IS NULL
                            AND NEW.forecast_target
                                    ->>'price_adjustment_policy'
                                = 'unverified_fail_closed'
                            AND NEW.semantics_evidence IS NOT NULL
                            AND jsonb_typeof(NEW.semantics_evidence)
                                IS DISTINCT FROM 'null'
                        )
                        OR (
                            NEW.supersedes_forecast_id IS NOT NULL
                            AND (
                                NEW.semantics_evidence->>'contract_version'
                                    IS DISTINCT FROM
                                    'forecast-semantics-supersession-v1'
                                OR NEW.semantics_evidence
                                    ->'authentication_binding'
                                    ->>'contract_version'
                                    IS DISTINCT FROM
                                    'forecast-evidence-authentication-v1'
                                OR NEW.semantics_evidence->>'from_forecast_id'
                                    IS DISTINCT FROM
                                    NEW.supersedes_forecast_id::text
                                OR NEW.semantics_evidence->>'to_forecast_id'
                                    IS DISTINCT FROM NEW.forecast_id::text
                                OR NOT EXISTS (
                                    SELECT 1
                                    FROM review.trade_forecasts predecessor
                                    WHERE predecessor.forecast_id
                                        = NEW.supersedes_forecast_id
                                      AND predecessor.status = 'open'
                                      AND predecessor.forecast_target->>'kind'
                                        = 'price_target'
                                      AND predecessor.forecast_target
                                            ->>'outcome_rule_version'
                                        IS NULL
                                      AND predecessor.superseded_by_forecast_id
                                        IS NULL
                                      AND predecessor.symbol = NEW.symbol
                                      AND predecessor.instrument_type
                                        = NEW.instrument_type
                                      AND predecessor.probability
                                        = NEW.probability
                                      AND predecessor.probability_range_low
                                        IS NOT DISTINCT FROM
                                        NEW.probability_range_low
                                      AND predecessor.probability_range_high
                                        IS NOT DISTINCT FROM
                                        NEW.probability_range_high
                                      AND predecessor.forecast_start_date
                                        IS NOT DISTINCT FROM
                                        NEW.forecast_start_date
                                      AND predecessor.review_date
                                        = NEW.review_date
                                      AND predecessor.forecast_target
                                            ->>'target_price'
                                        = NEW.forecast_target->>'target_price'
                                      AND (
                                        (
                                            predecessor.forecast_target
                                                ->>'direction'
                                                = 'at_or_above'
                                            AND NEW.forecast_target->>'direction'
                                                = 'up'
                                        )
                                        OR (
                                            predecessor.forecast_target
                                                ->>'direction'
                                                = 'at_or_below'
                                            AND NEW.forecast_target->>'direction'
                                                = 'down'
                                        )
                                      )
                                )
                            )
                        )
                   )
                THEN
                    RAISE EXCEPTION
                        'ROB-1038: new terminal_close requires immutable evidence';
                END IF;
                RETURN NEW;
            END IF;

            original_kind := COALESCE(
                OLD.immutable_claim->>'target_kind',
                OLD.forecast_target->>'kind'
            );
            resolving :=
                NEW.status IS DISTINCT FROM OLD.status
                OR NEW.outcome IS DISTINCT FROM OLD.outcome
                OR NEW.observed_value IS DISTINCT FROM OLD.observed_value
                OR NEW.brier_score IS DISTINCT FROM OLD.brier_score
                OR NEW.resolved_at IS DISTINCT FROM OLD.resolved_at;
            target_changed := NEW.forecast_target IS DISTINCT FROM OLD.forecast_target;

            IF OLD.forecast_target->>'kind' = 'price_target'
               AND OLD.forecast_target->>'outcome_rule_version' IS NULL
            THEN
                IF target_changed AND (
                    NEW.forecast_target->>'kind'
                        IS DISTINCT FROM 'price_target'
                    OR NEW.forecast_target->>'outcome_rule_version'
                        IS DISTINCT FROM 'window-touch-v1-high-gte-low-lte'
                    OR NEW.forecast_target - 'outcome_rule_version'
                        IS DISTINCT FROM OLD.forecast_target
                    OR NEW.target_version <> 1
                    OR NEW.immutable_claim IS NULL
                    OR NEW.immutable_claim_hash IS NULL
                    OR NEW.immutable_claim->>'target_kind'
                        IS DISTINCT FROM 'price_target'
                    OR NEW.resolution_semantics_status
                        IS DISTINCT FROM 'active'
                    OR NEW.supersedes_forecast_id
                        IS DISTINCT FROM OLD.supersedes_forecast_id
                    OR NEW.superseded_by_forecast_id
                        IS DISTINCT FROM OLD.superseded_by_forecast_id
                    OR NEW.semantics_evidence->>'contract_version'
                        IS DISTINCT FROM
                        'forecast-semantics-attestation-v1'
                    OR NEW.semantics_evidence
                        ->'authentication_binding'
                        ->>'contract_version'
                        IS DISTINCT FROM
                        'forecast-evidence-authentication-v1'
                )
                THEN
                    RAISE EXCEPTION
                        'ROB-1038: legacy price_target mutation requires typed attestation';
                END IF;

                IF NEW.created_by IS DISTINCT FROM OLD.created_by
                   OR NEW.symbol IS DISTINCT FROM OLD.symbol
                   OR NEW.instrument_type IS DISTINCT FROM OLD.instrument_type
                   OR NEW.probability IS DISTINCT FROM OLD.probability
                   OR NEW.probability_range_low
                        IS DISTINCT FROM OLD.probability_range_low
                   OR NEW.probability_range_high
                        IS DISTINCT FROM OLD.probability_range_high
                   OR NEW.forecast_start_date
                        IS DISTINCT FROM OLD.forecast_start_date
                   OR NEW.review_date IS DISTINCT FROM OLD.review_date
                   OR NEW.session_label IS DISTINCT FROM OLD.session_label
                   OR NEW.model_label IS DISTINCT FROM OLD.model_label
                   OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
                   OR NEW.artifact_uuid IS DISTINCT FROM OLD.artifact_uuid
                   OR NEW.journal_id IS DISTINCT FROM OLD.journal_id
                   OR NEW.report_uuid IS DISTINCT FROM OLD.report_uuid
                   OR NEW.report_item_uuid IS DISTINCT FROM OLD.report_item_uuid
                   OR NEW.correlation_id IS DISTINCT FROM OLD.correlation_id
                   OR NEW.evidence_ids IS DISTINCT FROM OLD.evidence_ids
                   OR NEW.contrary_evidence IS DISTINCT FROM OLD.contrary_evidence
                   OR NEW.resolution_source IS DISTINCT FROM OLD.resolution_source
                   OR NEW.horizon IS DISTINCT FROM OLD.horizon
                THEN
                    RAISE EXCEPTION
                        'ROB-1038: quarantined legacy claim mutation rejected';
                END IF;

                IF NOT target_changed
                   AND (
                        NEW.target_version IS DISTINCT FROM OLD.target_version
                        OR NEW.immutable_claim IS DISTINCT FROM OLD.immutable_claim
                        OR NEW.immutable_claim_hash
                            IS DISTINCT FROM OLD.immutable_claim_hash
                        OR NEW.resolution_semantics_status
                            IS DISTINCT FROM OLD.resolution_semantics_status
                        OR NEW.semantics_evidence
                            IS DISTINCT FROM OLD.semantics_evidence
                        OR NEW.superseded_by_forecast_id
                            IS DISTINCT FROM OLD.superseded_by_forecast_id
                        OR NEW.supersedes_forecast_id
                            IS DISTINCT FROM OLD.supersedes_forecast_id
                   )
                   AND (
                        NEW.target_version <> OLD.target_version
                        OR NEW.immutable_claim IS DISTINCT FROM OLD.immutable_claim
                        OR NEW.immutable_claim_hash
                            IS DISTINCT FROM OLD.immutable_claim_hash
                        OR NEW.resolution_semantics_status
                            IS DISTINCT FROM 'superseded'
                        OR NEW.superseded_by_forecast_id IS NULL
                        OR NEW.supersedes_forecast_id
                            IS DISTINCT FROM OLD.supersedes_forecast_id
                        OR NEW.semantics_evidence->>'contract_version'
                            IS DISTINCT FROM
                            'forecast-semantics-supersession-v1'
                        OR NEW.semantics_evidence
                            ->'authentication_binding'
                            ->>'contract_version'
                            IS DISTINCT FROM
                            'forecast-evidence-authentication-v1'
                        OR NEW.semantics_evidence->>'from_forecast_id'
                            IS DISTINCT FROM OLD.forecast_id::text
                        OR NEW.semantics_evidence->>'to_forecast_id'
                            IS DISTINCT FROM
                            NEW.superseded_by_forecast_id::text
                        OR NOT EXISTS (
                            SELECT 1
                            FROM review.trade_forecasts successor
                            WHERE successor.forecast_id
                                = NEW.superseded_by_forecast_id
                              AND successor.supersedes_forecast_id
                                = OLD.forecast_id
                              AND successor.forecast_target->>'kind'
                                = 'terminal_close'
                              AND successor.forecast_target
                                    ->>'outcome_rule_version'
                                = 'terminal-close-v1-up-gte-down-lt'
                              AND successor.semantics_evidence
                                = NEW.semantics_evidence
                        )
                   )
                THEN
                    RAISE EXCEPTION
                        'ROB-1038: legacy supersession requires linked typed evidence';
                END IF;
            END IF;

            IF OLD.resolution_semantics_status = 'superseded' AND resolving THEN
                RAISE EXCEPTION
                    'ROB-1038: superseded forecast cannot be resolved';
            END IF;

            IF resolving
               AND OLD.forecast_target->>'kind' = 'price_target'
               AND (
                    OLD.forecast_target->>'outcome_rule_version'
                        IS DISTINCT FROM 'window-touch-v1-high-gte-low-lte'
                    OR OLD.target_version < 1
                    OR OLD.immutable_claim IS NULL
               )
            THEN
                RAISE EXCEPTION
                    'ROB-1038: versionless/unattested price_target is quarantined';
            END IF;

            IF resolving
               AND original_kind = 'terminal_close'
               AND (
                    OLD.target_version < 1
                    OR OLD.immutable_claim IS NULL
                    OR OLD.immutable_claim_hash IS NULL
               )
            THEN
                RAISE EXCEPTION
                    'ROB-1038: terminal forecast lacks immutable claim evidence';
            END IF;

            IF resolving
               AND original_kind = 'terminal_close'
               AND (
                    OLD.forecast_target->>'price_adjustment_policy'
                        IS DISTINCT FROM 'explicit-factor-v1'
                    OR OLD.semantics_evidence
                        ->'adjustment_authentication'
                        ->>'contract_version'
                        IS DISTINCT FROM
                        'forecast-evidence-authentication-v1'
               )
            THEN
                RAISE EXCEPTION
                    'ROB-1038: terminal forecast lacks authenticated adjustment evidence';
            END IF;

            IF resolving
               AND original_kind = 'terminal_close'
               AND (
                    NEW.status IS DISTINCT FROM 'closed'
                    OR NEW.resolution_source
                        IS DISTINCT FROM 'ohlcv_day_terminal_close'
                    OR NEW.resolution_detail->>'target_kind'
                        IS DISTINCT FROM 'terminal_close'
                    OR NEW.resolution_detail->>'outcome_rule_version'
                        IS DISTINCT FROM 'terminal-close-v1-up-gte-down-lt'
                    OR NEW.resolution_detail->>'source_row_id' IS NULL
                    OR NEW.resolution_detail->>'source_row_version' IS NULL
                    OR NEW.resolution_detail->>'source_ingested_at' IS NULL
                    OR NEW.resolution_detail->>'is_final'
                        IS DISTINCT FROM 'true'
                    OR NEW.resolution_detail->>'session_scope'
                        IS DISTINCT FROM 'regular'
                    OR NEW.resolution_detail->'resolution_contract'
                        IS NULL
               )
            THEN
                RAISE EXCEPTION
                    'ROB-1038: terminal resolution must use typed deterministic evidence';
            END IF;

            IF original_kind = 'terminal_close' THEN
                IF NEW.immutable_claim IS DISTINCT FROM OLD.immutable_claim
                   OR NEW.immutable_claim_hash IS DISTINCT FROM OLD.immutable_claim_hash
                   OR NEW.created_by IS DISTINCT FROM OLD.created_by
                   OR NEW.symbol IS DISTINCT FROM OLD.symbol
                   OR NEW.instrument_type IS DISTINCT FROM OLD.instrument_type
                   OR NEW.probability IS DISTINCT FROM OLD.probability
                   OR NEW.probability_range_low
                        IS DISTINCT FROM OLD.probability_range_low
                   OR NEW.probability_range_high
                        IS DISTINCT FROM OLD.probability_range_high
                   OR NEW.forecast_start_date
                        IS DISTINCT FROM OLD.forecast_start_date
                   OR NEW.review_date IS DISTINCT FROM OLD.review_date
                   OR NEW.session_label IS DISTINCT FROM OLD.session_label
                   OR NEW.model_label IS DISTINCT FROM OLD.model_label
                   OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
                   OR NEW.artifact_uuid IS DISTINCT FROM OLD.artifact_uuid
                   OR NEW.journal_id IS DISTINCT FROM OLD.journal_id
                   OR NEW.report_uuid IS DISTINCT FROM OLD.report_uuid
                   OR NEW.report_item_uuid IS DISTINCT FROM OLD.report_item_uuid
                   OR NEW.correlation_id IS DISTINCT FROM OLD.correlation_id
                   OR NEW.evidence_ids IS DISTINCT FROM OLD.evidence_ids
                   OR NEW.contrary_evidence IS DISTINCT FROM OLD.contrary_evidence
                   OR NEW.horizon IS DISTINCT FROM OLD.horizon
                THEN
                    RAISE EXCEPTION
                        'ROB-1038: terminal immutable claim mutation rejected';
                END IF;

                IF target_changed THEN
                    IF OLD.immutable_claim IS NULL
                       OR OLD.forecast_target->>'kind' <> 'terminal_close'
                       OR NEW.forecast_target->>'kind' <> 'terminal_close'
                       OR OLD.forecast_target->>'direction'
                            IS DISTINCT FROM NEW.forecast_target->>'direction'
                       OR OLD.forecast_target->>'target_price'
                            IS DISTINCT FROM NEW.forecast_target->>'target_price'
                       OR OLD.forecast_target->>'outcome_rule_version'
                            IS DISTINCT FROM NEW.forecast_target->>'outcome_rule_version'
                       OR OLD.forecast_target->>'price_adjustment_policy'
                            <> 'unverified_fail_closed'
                       OR NEW.forecast_target->>'price_adjustment_policy'
                            <> 'explicit-factor-v1'
                       OR NEW.target_version <> OLD.target_version + 1
                       OR NEW.resolution_semantics_status
                            IS DISTINCT FROM 'active'
                       OR NEW.semantics_evidence
                            ->'adjustment_authentication'
                            ->>'contract_version'
                            IS DISTINCT FROM
                            'forecast-evidence-authentication-v1'
                    THEN
                        RAISE EXCEPTION
                            'ROB-1038: only terminal adjustment promotion is allowed';
                    END IF;
                ELSIF NEW.target_version <> OLD.target_version THEN
                    RAISE EXCEPTION
                        'ROB-1038: target_version changed without target promotion';
                ELSIF NEW.semantics_evidence IS DISTINCT FROM OLD.semantics_evidence
                   OR NEW.resolution_semantics_status
                        IS DISTINCT FROM OLD.resolution_semantics_status
                   OR NEW.supersedes_forecast_id
                        IS DISTINCT FROM OLD.supersedes_forecast_id
                   OR NEW.superseded_by_forecast_id
                        IS DISTINCT FROM OLD.superseded_by_forecast_id
                THEN
                    RAISE EXCEPTION
                        'ROB-1038: terminal semantics evidence mutation rejected';
                END IF;
            END IF;

            IF OLD.resolution_semantics_status = 'superseded'
               AND (
                    NEW.resolution_semantics_status
                        IS DISTINCT FROM OLD.resolution_semantics_status
                    OR NEW.semantics_evidence
                        IS DISTINCT FROM OLD.semantics_evidence
                    OR NEW.superseded_by_forecast_id
                        IS DISTINCT FROM OLD.superseded_by_forecast_id
                    OR NEW.supersedes_forecast_id
                        IS DISTINCT FROM OLD.supersedes_forecast_id
               )
            THEN
                RAISE EXCEPTION
                    'ROB-1038: supersession evidence is immutable';
            END IF;

            IF OLD.semantics_evidence->>'contract_version'
                = 'forecast-semantics-attestation-v1'
               AND (
                    NEW.semantics_evidence
                        IS DISTINCT FROM OLD.semantics_evidence
                    OR NEW.supersedes_forecast_id
                        IS DISTINCT FROM OLD.supersedes_forecast_id
                    OR NEW.superseded_by_forecast_id
                        IS DISTINCT FROM OLD.superseded_by_forecast_id
               )
            THEN
                RAISE EXCEPTION
                    'ROB-1038: touch attestation evidence is immutable';
            END IF;

            IF target_changed
               AND NEW.forecast_target->>'kind' = 'price_target'
               AND (
                    NEW.forecast_target->>'outcome_rule_version'
                        IS DISTINCT FROM 'window-touch-v1-high-gte-low-lte'
                    OR NEW.target_version < 1
                    OR NEW.immutable_claim IS NULL
                    OR NEW.immutable_claim_hash IS NULL
                    OR NEW.immutable_claim->>'target_kind'
                        IS DISTINCT FROM 'price_target'
                    OR NEW.resolution_semantics_status
                        IS DISTINCT FROM 'active'
               )
            THEN
                RAISE EXCEPTION
                    'ROB-1038: price_target transition requires typed touch evidence';
            END IF;

            IF target_changed
               AND NEW.forecast_target->>'kind' = 'terminal_close'
               AND (
                    NEW.forecast_target->>'outcome_rule_version'
                        IS DISTINCT FROM 'terminal-close-v1-up-gte-down-lt'
                    OR NEW.target_version < 1
                    OR NEW.immutable_claim IS NULL
                    OR NEW.immutable_claim_hash IS NULL
                    OR NEW.immutable_claim->>'target_kind'
                        IS DISTINCT FROM 'terminal_close'
                    OR NEW.resolution_semantics_status IS NULL
                    OR NEW.resolution_semantics_status
                        NOT IN ('active', 'quarantined')
                    OR (
                        NEW.forecast_target->>'price_adjustment_policy'
                            = 'explicit-factor-v1'
                        AND NEW.semantics_evidence
                            ->'adjustment_authentication'
                            ->>'contract_version'
                            IS DISTINCT FROM
                            'forecast-evidence-authentication-v1'
                    )
               )
            THEN
                RAISE EXCEPTION
                    'ROB-1038: terminal transition requires immutable evidence';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_trade_forecasts_semantics_guard
        BEFORE INSERT OR UPDATE ON review.trade_forecasts
        FOR EACH ROW
        EXECUTE FUNCTION review.enforce_trade_forecast_semantics()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM review.trade_forecasts
                WHERE target_version > 0
                   OR immutable_claim IS NOT NULL
                   OR resolution_semantics_status IS NOT NULL
                   OR semantics_evidence IS NOT NULL
                   OR supersedes_forecast_id IS NOT NULL
                   OR superseded_by_forecast_id IS NOT NULL
                   OR forecast_target->>'kind' = 'terminal_close'
            ) OR EXISTS (
                SELECT 1 FROM public.kr_candles_1d
                WHERE is_final IS NOT NULL
                   OR session_scope IS NOT NULL
                   OR source_row_id IS NOT NULL
                   OR source_row_version IS NOT NULL
                   OR price_basis IS NOT NULL
            ) OR EXISTS (
                SELECT 1 FROM public.us_candles_1d
                WHERE is_final IS NOT NULL
                   OR session_scope IS NOT NULL
                   OR source_row_id IS NOT NULL
                   OR source_row_version IS NOT NULL
                   OR price_basis IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'ROB-1038 downgrade refused: typed semantics/provenance exists';
            END IF;
        END
        $$
        """
    )

    op.execute(
        "DROP TRIGGER IF EXISTS trg_trade_forecasts_semantics_guard "
        "ON review.trade_forecasts"
    )
    op.execute("DROP FUNCTION IF EXISTS review.enforce_trade_forecast_semantics()")

    for table in reversed(_CANDLE_TABLES):
        op.drop_constraint(f"ck_{table}_final_provenance", table, type_="check")
        op.drop_constraint(f"ck_{table}_price_basis", table, type_="check")
        op.drop_constraint(f"ck_{table}_session_scope", table, type_="check")
        for column in (
            "price_basis",
            "source_row_version",
            "source_row_id",
            "session_scope",
            "is_final",
        ):
            op.drop_column(table, column)

    op.drop_index(
        "ix_trade_forecasts_superseded_by",
        table_name=_FORECAST_TABLE,
        schema=_FORECAST_SCHEMA,
    )
    op.drop_index(
        "ix_trade_forecasts_supersedes",
        table_name=_FORECAST_TABLE,
        schema=_FORECAST_SCHEMA,
    )
    op.drop_index(
        "ix_trade_forecasts_semantics_due",
        table_name=_FORECAST_TABLE,
        schema=_FORECAST_SCHEMA,
    )
    for name in (
        "ck_trade_forecasts_superseded_by_not_self",
        "ck_trade_forecasts_supersedes_not_self",
        "ck_trade_forecasts_immutable_claim_hash",
        "ck_trade_forecasts_immutable_claim_pair",
        "ck_trade_forecasts_resolution_semantics_status",
        "ck_trade_forecasts_target_version",
    ):
        op.drop_constraint(
            name,
            _FORECAST_TABLE,
            schema=_FORECAST_SCHEMA,
            type_="check",
        )
    for name in (
        "fk_trade_forecasts_superseded_by_forecast_id",
        "fk_trade_forecasts_supersedes_forecast_id",
    ):
        op.drop_constraint(
            name,
            _FORECAST_TABLE,
            schema=_FORECAST_SCHEMA,
            type_="foreignkey",
        )
    for column in (
        "superseded_by_forecast_id",
        "supersedes_forecast_id",
        "semantics_evidence",
        "resolution_semantics_status",
        "target_version",
        "immutable_claim_hash",
        "immutable_claim",
    ):
        op.drop_column(_FORECAST_TABLE, column, schema=_FORECAST_SCHEMA)
