"""hk 4902: static contract for the unused-table audit and its DROP migration.

No database. The migration is rendered offline (``as_sql``) and compared with
the closed DROP-now list in ``docs/research/4902-unused-table-audit.md``.
"""

from __future__ import annotations

import importlib.util
import io
import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

REPO = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPO / "alembic" / "versions" / "20261116_hk4902_drop_unused_tables.py"
AUDIT_PATH = REPO / "docs" / "research" / "4902-unused-table-audit.md"
REVISION = "20261116_hk4902_drop_unused"
PARENT_REVISION = "20261115_hk4734_kiwoom_validate"

CANDIDATE_COUNT = 94
SECTION_HEADINGS = {
    "DROP-now": r"^## \d+\. DROP-now \((\d+)\)$",
    "KEEP-live": r"^## \d+\. KEEP-live \((\d+)\)$",
    "KEEP-hypertable": r"^## \d+\. KEEP-hypertable \((\d+)\)$",
    "DEAD-code": r"^## \d+\. DEAD-code \((\d+)\)$",
    "REVIEW": r"^## \d+\. REVIEW \((\d+)\)$",
}

# Hard rule 5 ledgers: never DROP-now material.
LEDGER_TABLES = {
    "alpaca_paper_order_ledger",
    "binance_demo_order_ledger",
    "kis_live_order_ledger",
    "live_order_ledger",
    "toss_live_order_ledger",
    "execution_ledger",
    "kis_mock_order_ledger",
    "kiwoom_mock_order_ledger",
    "nhplug_mock_order_ledger",
}


def _load_migration():
    spec = importlib.util.spec_from_file_location("hk4902_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(module, name: str) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer},
    )
    with Operations.context(context):
        getattr(module, name)()
    return buffer.getvalue()


def _sections() -> dict[str, list[str]]:
    lines = AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    found: dict[str, list[str]] = {}
    current: str | None = None
    declared: dict[str, int] = {}
    for line in lines:
        matched = False
        for name, pattern in SECTION_HEADINGS.items():
            m = re.match(pattern, line)
            if m:
                current = name
                declared[name] = int(m.group(1))
                found.setdefault(name, [])
                matched = True
        if matched:
            continue
        if line.startswith("## "):
            current = None
            continue
        if current is None:
            continue
        row = re.match(r"^\| `((?:public|review|research)\.[a-z0-9_]+)` \|", line)
        if row:
            found[current].append(row.group(1))
    assert declared == {k: len(v) for k, v in found.items()}, (
        "section headings must state the number of table rows they hold"
    )
    return found


def _drop_now_block() -> list[str]:
    text = AUDIT_PATH.read_text(encoding="utf-8")
    m = re.search(r"<!-- drop-now:begin -->\n(.*?)<!-- drop-now:end -->", text, re.S)
    assert m, "drop-now block is missing"
    return [x.strip() for x in m.group(1).splitlines() if x.strip()]


def test_every_candidate_is_in_exactly_one_class() -> None:
    sections = _sections()
    assert set(sections) == set(SECTION_HEADINGS)
    flat = [t for rows in sections.values() for t in rows]
    assert len(flat) == CANDIDATE_COUNT
    assert len(set(flat)) == CANDIDATE_COUNT, "a table appears in two classes"
    assert len(sections["KEEP-hypertable"]) == 8


def test_drop_now_block_matches_the_drop_now_table_rows() -> None:
    assert _drop_now_block() == _sections()["DROP-now"]


def test_migration_drops_exactly_the_documented_drop_now_set() -> None:
    migration = _load_migration()
    documented = {t.split(".", 1)[1] for t in _sections()["DROP-now"]}
    assert all(t.startswith("public.") for t in _sections()["DROP-now"])
    assert set(migration.DROPPED_TABLES) == documented
    assert len(migration.DROPPED_TABLES) == len(set(migration.DROPPED_TABLES))

    upgrade_sql = _render(migration, "upgrade")
    dropped = re.findall(r"^DROP TABLE public\.(\w+);$", upgrade_sql, re.M)
    assert sorted(dropped) == sorted(documented)
    assert "CASCADE" not in upgrade_sql.upper().replace("ON DELETE CASCADE", "")
    for table in documented:
        assert f"LOCK TABLE public.{table} IN ACCESS EXCLUSIVE MODE" in upgrade_sql
        assert f"hk4902_{table}_rows_retained" in upgrade_sql
        # The guard must run before the DROP of the same table.
        assert upgrade_sql.index(f"hk4902_{table}_rows_retained") < upgrade_sql.index(
            f"DROP TABLE public.{table};"
        )


def test_downgrade_recreates_the_same_set_empty_with_schema_and_grants() -> None:
    migration = _load_migration()
    documented = {t.split(".", 1)[1] for t in _sections()["DROP-now"]}
    sql = _render(migration, "downgrade")

    created = re.findall(r"^CREATE TABLE (\w+) \(", sql, re.M)
    assert sorted(created) == sorted(documented)
    assert "INSERT" not in sql.replace("GRANT SELECT, INSERT", "")

    # Last known schema: names the original migrations gave.
    for fragment in (
        "CONSTRAINT pk_tier_rule_params PRIMARY KEY",
        "CONSTRAINT uq_tier_rule_params_key UNIQUE (user_id, instrument_type, tier, profile, param_type)",
        "CONSTRAINT tier_rule_params_tier_range CHECK (tier BETWEEN 1 AND 4)",
        "CONSTRAINT pk_market_filters PRIMARY KEY",
        "CONSTRAINT market_filters_filter_name_format CHECK",
        "CREATE INDEX ix_market_filters_user_instrument_enabled ON market_filters (user_id, instrument_type, enabled)",
        "CREATE UNIQUE INDEX uq_market_filters_user_instrument_filter ON market_filters (user_id, instrument_type, filter_name)",
        "CONSTRAINT pk_user_channels PRIMARY KEY",
        "channel notify_channel NOT NULL",
        "CREATE TYPE public.notify_channel AS ENUM",
        "FOREIGN KEY(session_id) REFERENCES research_sessions (id)",
        "CREATE INDEX ix_user_research_notes_session_id ON user_research_notes (session_id)",
    ):
        assert fragment in sql, fragment
    # market_filters lost broker_account_id in a1b2c3d4e5f6; do not bring it back.
    assert "broker_account_id" not in sql

    # Hard rule 18: conditional at_app grant per recreated table.
    for table in documented:
        assert f"ON public.{table} TO at_app" in sql
        assert f"pg_get_serial_sequence('public.{table}', 'id')" in sql
    assert sql.count("SELECT 1 FROM pg_roles WHERE rolname = 'at_app'") == len(
        documented
    )


def test_revision_chain_is_single_and_contains_the_migration() -> None:
    module = _load_migration()
    assert module.revision == REVISION
    assert module.down_revision == PARENT_REVISION
    assert len(REVISION) <= 32  # alembic_version.version_num is VARCHAR(32)
    directory = ScriptDirectory.from_config(Config(str(REPO / "alembic.ini")))
    heads = directory.get_heads()
    assert len(heads) == 1, heads
    ancestry = {r.revision for r in directory.walk_revisions("base", heads[0])}
    assert REVISION in ancestry


def test_drop_now_tables_are_not_contract_ledger_or_trigger_protected() -> None:
    documented = {t.split(".", 1)[1] for t in _sections()["DROP-now"]}
    assert not documented & LEDGER_TABLES

    contract_text = "\n".join(
        p.read_text(encoding="utf-8")
        for p in [*(REPO / "docs" / "agent-contracts").glob("*.md"), REPO / "AGENTS.md"]
    )
    versions = {
        p: p.read_text(encoding="utf-8")
        for p in (REPO / "alembic" / "versions").glob("*.py")
        if p != MIGRATION_PATH
    }
    for table in documented:
        word = rf"(?<![A-Za-z0-9_]){re.escape(table)}(?![A-Za-z0-9_])"
        assert not re.search(word, contract_text), f"{table} is named by a contract"
        for path, text in versions.items():
            # No trigger guards the table, and no other table references it.
            assert not re.search(
                rf"(?is)CREATE\s+(?:CONSTRAINT\s+)?TRIGGER[^;]*?\bON\s+(?:\w+\.)?{table}\b",
                text,
            ), f"{table} has a trigger in {path.name}"
            assert not re.search(rf"REFERENCES\s+(?:\w+\.)?{table}\b", text, re.I)
            assert f'"{table}.id"' not in text and f"'{table}.id'" not in text


def test_no_runtime_code_or_model_still_mentions_the_dropped_tables() -> None:
    documented = {t.split(".", 1)[1] for t in _sections()["DROP-now"]}
    from app.models.base import Base

    registered = {t.name for t in Base.metadata.tables.values()}
    assert not documented & registered

    word = re.compile(
        r"(?<![A-Za-z0-9_])(" + "|".join(sorted(documented)) + r")(?![A-Za-z0-9_])"
    )
    offenders: list[str] = []
    for root in ("app", "scripts", "deploy", "config"):
        for path in (REPO / root).rglob("*"):
            if not path.is_file() or path.suffix not in {
                ".py",
                ".sql",
                ".sh",
                ".yaml",
                ".yml",
                ".toml",
            }:
                continue
            for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
            ):
                if word.search(line):
                    offenders.append(f"{path.relative_to(REPO)}:{number}")
    # app/models/research_pipeline.py documents the drop in its docstring.
    offenders = [
        o for o in offenders if not o.startswith("app/models/research_pipeline.py")
    ]
    assert offenders == []

    import app.models as models

    for name in (
        "TierRuleParam",
        "MarketFilter",
        "TierParamType",
        "FilterName",
        "UserChannel",
        "UserResearchNote",
    ):
        assert not hasattr(models, name), name
        assert name not in models.__all__, name


@pytest.mark.parametrize("section", ["KEEP-live", "DEAD-code"])
def test_evidence_locations_exist(section: str) -> None:
    """KEEP-live and DEAD-code rows cite path:line locations that exist."""
    text = AUDIT_PATH.read_text(encoding="utf-8")
    pattern = re.compile(SECTION_HEADINGS[section], re.M)
    start = pattern.search(text)
    assert start is not None
    rest = text[start.end() :]
    end = re.search(r"^## ", rest, re.M)
    body = rest[: end.start()] if end else rest

    missing: list[str] = []
    for line in body.splitlines():
        row = re.match(r"^\| `((?:public|review|research)\.[a-z0-9_]+)` \|(.*)$", line)
        if not row:
            continue
        # The evidence cell is the fourth column; split on unescaped pipes.
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        evidence = cells[3]
        locations = re.findall(
            r"((?:app|scripts|alembic)/[\w/.\-]+\.py):(\d+)", evidence
        )
        if not locations:
            missing.append(f"{row.group(1)}: no path:line")
            continue
        for rel, number in locations:
            target = REPO / rel
            if not target.is_file():
                missing.append(f"{row.group(1)}: {rel} does not exist")
            elif int(number) > len(target.read_text(encoding="utf-8").splitlines()):
                missing.append(f"{row.group(1)}: {rel}:{number} is past the end")
    assert missing == []
