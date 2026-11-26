"""Allow additional US intraday mover observation sources."""

# expand-contract: contract-step 6663
from sqlalchemy import text

from alembic import op

revision = "20261126_hk6663_us_mover_sources"
down_revision = "20261125_hk6582_dart_capital"
branch_labels = None
depends_on = None

TABLE = "invest_momentum_event_snapshots"
CHECK = "ck_invest_momentum_event_snapshots_source"
OLD_SOURCES = "'naver_stock', 'alpaca_screener', 'kis_overseas_ranking', 'benzinga_pro'"
NEW_SOURCES = (
    OLD_SOURCES
    + ", 'kis_overseas_price_fluct', 'kis_overseas_volume_surge', "
    + "'kis_overseas_new_highlow', 'kis_overseas_volume_power', "
    + "'toss_rankings', 'benzinga_market_movers'"
)


def upgrade() -> None:
    # expand-contract: contract-step 6663
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {CHECK}")
    # expand-contract: contract-step 6663
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK} CHECK (source IN ({NEW_SOURCES})) NOT VALID"
    )


def downgrade() -> None:
    op.execute("LOCK TABLE invest_momentum_event_runs IN SHARE MODE")
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    new_sources = (
        "kis_overseas_price_fluct",
        "kis_overseas_volume_surge",
        "kis_overseas_new_highlow",
        "kis_overseas_volume_power",
        "toss_rankings",
        "benzinga_market_movers",
    )
    quoted = ", ".join("'" + item + "'" for item in new_sources)
    if op.get_bind().scalar(
        text(
            f"SELECT EXISTS (SELECT 1 FROM {TABLE} WHERE source IN ({quoted})) "
            f"OR EXISTS (SELECT 1 FROM invest_momentum_event_runs "
            f"WHERE split_part(surface, ':', 2) IN ({quoted}))"
        )
    ):
        raise RuntimeError("US mover source rows exist")
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {CHECK}")
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK} CHECK (source IN ({OLD_SOURCES}))"
    )
