"""Daily plan v0 append-only records in the review schema."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import Base
from app.services.daily_plan.vocabulary import (
    ACTION_BASES,
    ACTIONS,
    LINK_STATES,
    ROW_KINDS,
    VERSION_KINDS,
)


def _table(name, *columns):
    return Table(f"daily_plan_{name}", Base.metadata, *columns, schema="review")


def _id():
    return Column("id", Uuid, primary_key=True)


def _ref(name, table):
    return Column(
        name, Uuid, ForeignKey(f"review.daily_plan_{table}.id"), nullable=False
    )


def _created():
    return Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


def _vocabulary(column, words):
    return CheckConstraint(f"{column} IN ({', '.join(map(repr, words))})", name=column)


heads = _table(
    "heads",
    _id(),
    Column("market", Text, nullable=False),
    Column("session_date", Date, nullable=False),
    Column("mode", Text, nullable=False, server_default="plan_only"),
    Column("cutoff_at", DateTime(timezone=True), nullable=False),
    _created(),
    UniqueConstraint("market", "session_date"),
    CheckConstraint(
        "market IN ('kr', 'us', 'crypto') AND mode IN ('plan_only', 'executable')",
        name="market_mode",
    ),
)

bundles = _table(
    "input_bundles",
    _id(),
    _ref("head_id", "heads"),
    Column("sha256", Text, nullable=False),
    Column("body", JSONB, nullable=False),
    _created(),
    UniqueConstraint("head_id", "sha256"),
    UniqueConstraint("head_id", "id", "sha256", name="uq_daily_plan_bundle_binding"),
    CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256"),
)

versions = _table(
    "versions",
    _id(),
    _ref("head_id", "heads"),
    Column("seq", Integer, nullable=False),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="PUBLISHED"),
    Column("idempotency_key", Text, nullable=False),
    Column("request_sha256", Text, nullable=False),
    Column("input_bundle_id", Uuid),
    Column("input_bundle_sha256", Text),
    Column("strategy_id", Text, nullable=False),
    Column("strategy_rule_version", Text, nullable=False),
    Column("policy_version", Text, nullable=False),
    Column("policy_content_hash", Text, nullable=False),
    Column("metadata", JSONB, nullable=False),
    _created(),
    ForeignKeyConstraint(
        ["head_id", "input_bundle_id", "input_bundle_sha256"],
        [
            "review.daily_plan_input_bundles.head_id",
            "review.daily_plan_input_bundles.id",
            "review.daily_plan_input_bundles.sha256",
        ],
    ),
    UniqueConstraint("head_id", "seq", name="uq_daily_plan_versions_head_seq"),
    UniqueConstraint(
        "head_id", "idempotency_key", name="uq_daily_plan_versions_head_idempotency"
    ),
    _vocabulary("kind", VERSION_KINDS),
    CheckConstraint("seq > 0 AND status = 'PUBLISHED'", name="published_seq"),
    CheckConstraint(
        "(input_bundle_id IS NULL) = (input_bundle_sha256 IS NULL)", name="bundle_pair"
    ),
)

rows = _table(
    "rows",
    _id(),
    _ref("version_id", "versions"),
    Column("row_kind", Text, nullable=False),
    Column("market", Text, nullable=False),
    Column("symbol", Text, nullable=False),
    Column("account_ref", Text),
    Column("action", Text, nullable=False),
    Column("action_basis", Text, nullable=False),
    Column("execution_state", Text, nullable=False, server_default="plan_only"),
    Column("adoption_key", Text, unique=True),
    Column("adoption_content_sha256", Text),
    Column("forecast_id", Uuid),
    Column("body", JSONB, nullable=False),
    _vocabulary("row_kind", ROW_KINDS),
    _vocabulary("action", ACTIONS),
    _vocabulary("action_basis", ACTION_BASES),
    CheckConstraint(
        "execution_state IN ('plan_only', 'executable')", name="execution_state"
    ),
    CheckConstraint(
        "(market = 'kr' AND symbol ~ '^[0-9A-Z]{6}$') OR "
        "(market = 'us' AND symbol ~ '^[A-Z][A-Z0-9]{0,9}([.][A-Z0-9]{1,4})?$') OR "
        "(market = 'crypto' AND symbol ~ '^KRW-[A-Z0-9]{1,20}$')",
        name="market_symbol",
    ),
    CheckConstraint(
        "(row_kind = 'holding' AND account_ref IS NOT NULL AND account_ref <> '') OR "
        "(row_kind = 'candidate' AND account_ref IS NULL)",
        name="account_ref",
    ),
    CheckConstraint(
        "(adoption_key IS NULL) = (adoption_content_sha256 IS NULL) AND "
        "(adoption_key IS NULL) = (forecast_id IS NULL)",
        name="adoption_binding",
    ),
)
Index(
    "uq_daily_plan_candidate_key",
    rows.c.version_id,
    rows.c.market,
    rows.c.symbol,
    unique=True,
    postgresql_where=rows.c.row_kind == "candidate",
)
Index(
    "uq_daily_plan_holding_key",
    rows.c.version_id,
    rows.c.market,
    rows.c.symbol,
    rows.c.account_ref,
    unique=True,
    postgresql_where=rows.c.row_kind == "holding",
)

outcomes = _table(
    "outcomes",
    _id(),
    _ref("row_id", "rows"),
    Column("horizon", Integer, nullable=False),
    Column("body", JSONB, nullable=False),
    _created(),
    UniqueConstraint("row_id", "horizon"),
    CheckConstraint("horizon > 0", name="horizon"),
)

links = _table(
    "row_links",
    _id(),
    _ref("row_id", "rows"),
    Column("state", Text, nullable=False),
    Column("seq", Integer, nullable=False),
    Column("decision_id", Uuid),
    _created(),
    _vocabulary("state", tuple(state for state in LINK_STATES if state != "pending")),
    UniqueConstraint("row_id", "seq"),
    CheckConstraint("seq > 0", name="link_seq"),
    CheckConstraint(
        "(state = 'confirmed') = (decision_id IS NOT NULL)", name="decision_binding"
    ),
)
