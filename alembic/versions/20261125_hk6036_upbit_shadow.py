"""hk 6036: Upbit KRW intraday shadow candidates and virtual round trips.

Revision ID: 20261125_hk6036_upbit_shadow
Revises: 20261125_hk6322_us_venue_val
Create Date: 2026-11-25

Additive: two new tables in ``research``, nothing else is touched. Neither is an
order or fill ledger.

``research.upbit_shadow_candidates`` — one row per (rules version, market,
configuration, closed signal bar): a frozen-rule signal, or a candidate held back
by closed-vocabulary exclusions. ``signal_time`` is the bar close;
``first_received_at`` is when the recorder first held the closed bar.

``research.upbit_shadow_virtual_trades`` — one virtual round trip per
(candidate, horizon): entry at the next bar's open, exit under the frozen exit
rule, MAE/MFE. ``is_virtual`` is always true and ``disposition`` is bound to the
candidate by a composite foreign key. A trade that can never be decided carries
``missing_reason`` and no numbers.

Both are insert-only. Conditional ``at_app`` grants (hard rule 18): SELECT,
INSERT, USAGE on the schema, only if the role exists.

Downgrade refuses while either table has rows (forward evidence would be lost).
"""

# expand-contract: contract-step 6036
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261125_hk6036_upbit_shadow"
down_revision: str | Sequence[str] | None = "20261125_hk6322_us_venue_val"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FAMILIES = ("A_breakout", "B_held_rebound", "C_downside")
DISPOSITIONS = ("signal", "excluded")
EXCLUSION_REASONS = (
    "market_event_warning",
    "market_event_caution",
    "market_event_unknown",
    "market_event_stale",
    "liquidity_below_floor",
    "cooldown_unverified",
)
BUCKETS = ("L1", "L2", "L3")
HORIZONS = (4, 16, 96)
ENTRY_SOURCES = ("next_bar_open",)
EXIT_BASES = ("exit_bar_close", "last_traded_close")
MISSING_REASONS = ("next_bar_no_trades", "market_unavailable")

CANDIDATES = "research.upbit_shadow_candidates"
TRADES = "research.upbit_shadow_virtual_trades"

_NUMBERS = (
    "entry_source, virtual_entry_price, exit_basis, virtual_exit_price, "
    "entry_tick_krw, exit_tick_krw, gross_return, net_return, mae, mfe"
)


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS research")
    op.execute(
        f"""
        CREATE TABLE {CANDIDATES} (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            rules_version TEXT NOT NULL,
            rules_sha256 TEXT NOT NULL,
            market TEXT NOT NULL,
            family TEXT NOT NULL,
            config_id TEXT NOT NULL,
            n_bars SMALLINT NOT NULL,
            x_multiple NUMERIC NOT NULL,
            direction SMALLINT NOT NULL,
            selected_config BOOLEAN NOT NULL,
            signal_bar_open_at TIMESTAMPTZ NOT NULL,
            signal_time TIMESTAMPTZ NOT NULL,
            first_received_at TIMESTAMPTZ NOT NULL,
            disposition TEXT NOT NULL,
            exclusion_reasons TEXT[] NOT NULL,
            open_price NUMERIC NOT NULL,
            high_price NUMERIC NOT NULL,
            low_price NUMERIC NOT NULL,
            close_price NUMERIC NOT NULL,
            quote_volume NUMERIC NOT NULL,
            ref_level NUMERIC NOT NULL,
            breakout_distance NUMERIC NOT NULL,
            median_quote_volume NUMERIC NOT NULL,
            volume_ratio NUMERIC NOT NULL,
            quote_volume_24h NUMERIC NOT NULL,
            liquidity_bucket TEXT NOT NULL,
            fee_rate_per_side NUMERIC NOT NULL,
            roundtrip_cost_rate NUMERIC NOT NULL,
            signal_tick_krw NUMERIC NOT NULL,
            market_event_state TEXT NOT NULL,
            market_event_caution_types TEXT[] NOT NULL,
            market_event_observed_at TIMESTAMPTZ,
            run_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT pk_research_upbit_shadow_candidates PRIMARY KEY (id),
            CONSTRAINT uq_research_upbit_shadow_candidates_bar
                UNIQUE (rules_version, market, config_id, signal_bar_open_at),
            CONSTRAINT uq_research_upbit_shadow_candidates_disposition
                UNIQUE (id, disposition),
            CONSTRAINT ck_research_upbit_shadow_candidates_family
                CHECK (family IN ({_quoted(FAMILIES)})),
            CONSTRAINT ck_research_upbit_shadow_candidates_direction
                CHECK (direction IN (1, -1)),
            CONSTRAINT ck_research_upbit_shadow_candidates_bar_clock
                CHECK (
                    EXTRACT(EPOCH FROM signal_time - signal_bar_open_at) = 900
                    AND MOD(EXTRACT(EPOCH FROM signal_bar_open_at), 900) = 0
                    AND first_received_at >= signal_time
                ),
            CONSTRAINT ck_research_upbit_shadow_candidates_disposition
                CHECK (disposition IN ({_quoted(DISPOSITIONS)})),
            CONSTRAINT ck_research_upbit_shadow_candidates_reasons
                CHECK (
                    exclusion_reasons <@ ARRAY[{_quoted(EXCLUSION_REASONS)}]::text[]
                    AND array_position(exclusion_reasons, NULL) IS NULL
                    AND (disposition = 'signal') = (cardinality(exclusion_reasons) = 0)
                ),
            CONSTRAINT ck_research_upbit_shadow_candidates_bucket
                CHECK (liquidity_bucket IN ({_quoted(BUCKETS)})),
            CONSTRAINT ck_research_upbit_shadow_candidates_market_event
                CHECK (
                    market_event_state IN ('none', 'warning', 'unknown', 'stale')
                    OR market_event_state LIKE 'caution:_%'
                )
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE {TRADES} (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            candidate_id BIGINT NOT NULL,
            disposition TEXT NOT NULL,
            is_virtual BOOLEAN NOT NULL DEFAULT TRUE,
            horizon_bars SMALLINT NOT NULL,
            entry_bar_open_at TIMESTAMPTZ NOT NULL,
            exit_bar_open_at TIMESTAMPTZ NOT NULL,
            entry_source TEXT,
            virtual_entry_price NUMERIC,
            exit_basis TEXT,
            virtual_exit_price NUMERIC,
            entry_tick_krw NUMERIC,
            exit_tick_krw NUMERIC,
            gross_return NUMERIC,
            roundtrip_cost_rate NUMERIC NOT NULL,
            net_return NUMERIC,
            mae NUMERIC,
            mfe NUMERIC,
            missing_reason TEXT,
            outcome_received_at TIMESTAMPTZ NOT NULL,
            run_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT pk_research_upbit_shadow_virtual_trades PRIMARY KEY (id),
            CONSTRAINT uq_research_upbit_shadow_virtual_trades_horizon
                UNIQUE (candidate_id, horizon_bars),
            CONSTRAINT fk_research_upbit_shadow_virtual_trades_candidate
                FOREIGN KEY (candidate_id, disposition)
                REFERENCES {CANDIDATES} (id, disposition),
            CONSTRAINT ck_research_upbit_shadow_virtual_trades_virtual
                CHECK (is_virtual),
            CONSTRAINT ck_research_upbit_shadow_virtual_trades_horizon
                CHECK (horizon_bars IN ({", ".join(str(h) for h in HORIZONS)})),
            CONSTRAINT ck_research_upbit_shadow_virtual_trades_entry_source
                CHECK (entry_source IN ({_quoted(ENTRY_SOURCES)})),
            CONSTRAINT ck_research_upbit_shadow_virtual_trades_exit_basis
                CHECK (exit_basis IN ({_quoted(EXIT_BASES)})),
            CONSTRAINT ck_research_upbit_shadow_virtual_trades_missing_reason
                CHECK (missing_reason IN ({_quoted(MISSING_REASONS)})),
            CONSTRAINT ck_research_upbit_shadow_virtual_trades_all_or_reason
                CHECK (
                    (missing_reason IS NULL AND num_nulls({_NUMBERS}) = 0)
                    OR (missing_reason IS NOT NULL AND num_nonnulls({_NUMBERS}) = 0)
                )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_research_upbit_shadow_candidates_signal_time "
        f"ON {CANDIDATES} (signal_time, market)"
    )
    # Hard rule 18: conditional grants, the role may be absent in dev/CI.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
                GRANT USAGE ON SCHEMA research TO at_app;
                GRANT SELECT, INSERT ON {CANDIDATES} TO at_app;
                GRANT SELECT, INSERT ON {TRADES} TO at_app;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    # Held through the emptiness check and the DROPs (same transaction): a recorder
    # INSERT cannot commit between them and be dropped with its first-receipt time.
    op.execute(f"LOCK TABLE {CANDIDATES}, {TRADES} IN ACCESS EXCLUSIVE MODE")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM {CANDIDATES} LIMIT 1)
               OR EXISTS (SELECT 1 FROM {TRADES} LIMIT 1) THEN
                RAISE EXCEPTION
                    'cannot drop research.upbit_shadow_*: rows exist (forward evidence would be lost)'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END
        $$
        """
    )
    op.execute(f"DROP TABLE {TRADES}")
    op.execute(f"DROP TABLE {CANDIDATES}")
