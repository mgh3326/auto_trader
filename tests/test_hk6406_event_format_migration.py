from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.base import Base


@pytest.mark.unit
def test_official_event_format_is_additive_and_downgrade_guards_new_data(monkeypatch):
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/20261126_hk6406_event_format.py"
    )
    spec = spec_from_file_location("official_event_format_revision", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={
            "as_sql": True,
            "output_buffer": output,
            "target_metadata": Base.metadata,
        },
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    migration.upgrade()
    upgrade = output.getvalue()
    assert "DROP " not in upgrade
    assert "UPDATE market_events SET" not in upgrade
    assert "uq_market_events_source_event_id" not in upgrade
    assert "UNIQUE (source, source_event_id)" in upgrade
    assert "ADD COLUMN IF NOT EXISTS published_at TIMESTAMP WITH TIME ZONE" in upgrade
    assert "ADD COLUMN IF NOT EXISTS published_date DATE" in upgrade
    assert upgrade.count("published_precision IN ('second', 'minute', 'datetime')") == 2
    assert "NOT VALID;" in upgrade
    assert "VALIDATE CONSTRAINT ck_market_events_publication;" in upgrade
    assert "ck_market_events_ck_market_events_publication" not in upgrade
    assert "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app')" in upgrade
    assert "GRANT SELECT, INSERT ON market_event_sources TO at_app;" in upgrade
    assert (
        "GRANT USAGE, SELECT ON SEQUENCE market_event_sources_id_seq TO at_app;"
        in upgrade
    )

    output.seek(0)
    output.truncate()
    migration.downgrade()
    downgrade = output.getvalue()
    assert downgrade.startswith("LOCK TABLE")
    assert "IF EXISTS (SELECT 1 FROM market_event_sources)" in downgrade
    assert (
        "EXISTS (SELECT 1 FROM market_event_revisions WHERE changed_fields IS NOT NULL"
        in downgrade
    )
    assert (
        "EXISTS (SELECT 1 FROM market_events WHERE published_at IS NOT NULL"
        in downgrade
    )
    assert downgrade.index("RAISE EXCEPTION") < downgrade.index("DROP TABLE")
    # Dropping the table removes its trigger. Drop the separate function after
    # its dependent trigger so the same names are available on re-upgrade.
    assert "CREATE TRIGGER trg_market_event_sources_append_only" in upgrade
    assert downgrade.index("DROP TABLE market_event_sources") < downgrade.index(
        "DROP FUNCTION reject_market_event_source_mutation()"
    )
