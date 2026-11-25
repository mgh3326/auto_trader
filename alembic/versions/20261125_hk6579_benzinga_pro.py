"""Allow Benzinga Pro snapshots and protect captured evidence on downgrade."""

from sqlalchemy import text

from alembic import op

revision = "20261125_hk6579_benzinga_pro"
down_revision = "20261125_hk6513_benzinga"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Replace the source CHECK in the same transaction with a wider vocabulary.
    # expand-contract: contract-step 6579
    op.execute(
        "ALTER TABLE invest_momentum_event_snapshots "
        "DROP CONSTRAINT ck_invest_momentum_event_snapshots_source"
    )
    op.create_check_constraint(
        op.f("ck_invest_momentum_event_snapshots_source"),
        "invest_momentum_event_snapshots",
        "source IN ('naver_stock', 'alpaca_screener', 'kis_overseas_ranking', 'benzinga_pro')",
    )


def downgrade() -> None:
    # Hold the writer locks until the guard and constraint replacement finish.
    op.execute("LOCK TABLE invest_momentum_event_runs IN SHARE MODE")
    op.execute("LOCK TABLE invest_momentum_event_snapshots IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE market_events IN SHARE MODE")
    if op.get_bind().scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM invest_momentum_event_snapshots "
            "WHERE source = 'benzinga_pro') OR EXISTS (SELECT 1 FROM market_events "
            "WHERE source = 'benzinga_pro') OR EXISTS ("
            "SELECT 1 FROM invest_momentum_event_runs "
            "WHERE split_part(surface, ':', 2) = 'benzinga_pro')"
        )
    ):
        raise RuntimeError("Benzinga Pro capture rows exist")
    op.drop_constraint(
        op.f("ck_invest_momentum_event_snapshots_source"),
        "invest_momentum_event_snapshots",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_invest_momentum_event_snapshots_source"),
        "invest_momentum_event_snapshots",
        "source IN ('naver_stock', 'alpaca_screener', 'kis_overseas_ranking')",
    )
