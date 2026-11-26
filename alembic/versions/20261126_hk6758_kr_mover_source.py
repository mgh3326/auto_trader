"""Allow the read-only KIS ranking fallback in KR movers observations."""

# expand-contract: contract-step 6758
from sqlalchemy import text

from alembic import op

revision = "20261126_hk6758_kr_mover_source"
down_revision = "20261126_hk6750"
branch_labels = None
depends_on = None

TABLE = "invest_momentum_event_snapshots"
CHECK = "ck_invest_momentum_event_snapshots_source"
OLD_SOURCES = (
    "'naver_stock', 'alpaca_screener', 'kis_overseas_ranking', 'benzinga_pro', "
    "'kis_overseas_price_fluct', 'kis_overseas_volume_surge', "
    "'kis_overseas_new_highlow', 'kis_overseas_volume_power', 'toss_rankings', "
    "'benzinga_market_movers'"
)
NEW_SOURCES = OLD_SOURCES + ", 'kis_domestic_ranking'"


def _drop_source_check() -> None:
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {CHECK}")


def upgrade() -> None:
    # expand-contract: contract-step 6758
    _drop_source_check()
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK} "
        f"CHECK (source IN ({NEW_SOURCES})) NOT VALID"
    )


def downgrade() -> None:
    op.execute("LOCK TABLE invest_momentum_event_runs IN SHARE MODE")
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        text(
            f"SELECT EXISTS (SELECT 1 FROM {TABLE} WHERE source = 'kis_domestic_ranking') "
            "OR EXISTS (SELECT 1 FROM invest_momentum_event_runs "
            "WHERE surface LIKE 'stock:kis_domestic_ranking:%')"
        )
    ):
        raise RuntimeError("KIS domestic mover observations exist")
    _drop_source_check()
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK} CHECK (source IN ({OLD_SOURCES}))"
    )
