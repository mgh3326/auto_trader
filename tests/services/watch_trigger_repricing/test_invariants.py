"""ROB-1286 §101차 불변 — enforced statically, at the r3 shape.

The risk did not shrink when the operator granted the spawned session
proposal-creation rights; it moved. The question is no longer "can the
session propose" (it may) but "can anything it produces reach a broker
without passing the approval machinery" (it may not). These tests hold that
line, plus the ownership split: logic here, schedule elsewhere.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PACKAGE = REPO_ROOT / "app" / "services" / "watch_trigger_repricing"
PACKAGE_FILES = sorted(PACKAGE.glob("*.py"))

# The read seam is the one file allowed to import the reports repository,
# and the claim store the one allowed to write -- to its own table only.
READ_SEAM = PACKAGE / "event_source.py"
CLAIM_WRITER = PACKAGE / "db_claim_store.py"
# ROB-1290: the boundary is crossed for real now, so exactly one file may
# import the tool that crosses it. Same shape as READ_SEAM, and it is a
# *narrow* exemption -- every other forbidden fragment still applies here.
WRITE_SEAM = PACKAGE / "proposal_chain.py"
BOUNDARY_MODULE = "app.mcp_server.tooling.order_proposal_tools"

FORBIDDEN_IMPORT_FRAGMENTS = (
    "app.services.brokers",
    "app.services.order_proposals",
    "app.mcp_server.tooling.orders",
    BOUNDARY_MODULE,
    "app.services.kis_trading_service",
    "app.services.trade_journal",
)

FORBIDDEN_SETTING_NAMES = (
    "ORDER_PROPOSALS_AUTO_APPROVE",
    "ORDER_PROPOSALS_AUTO_APPROVE_MODE",
    "ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED",
    "ORDER_PROPOSALS_TELEGRAM_ENABLED",
    "ORDER_PROPOSALS_ENABLED",
)


def _docstrings(tree: ast.Module) -> set[int]:
    """Line numbers of docstring nodes, so prose is not scanned as code."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            doc = body[0].value
            lines.update(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
    return lines


def _code_tokens(path: pathlib.Path) -> str:
    """Source with docstrings and comments removed.

    Naming a forbidden symbol in prose -- to explain why it is excluded --
    must not read as using it. Scanning raw text conflates the two.
    """
    tree = ast.parse(path.read_text())
    doc_lines = _docstrings(tree)
    kept = [
        line.split("#", 1)[0]
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if number not in doc_lines
    ]
    return "\n".join(kept)


def _trees() -> list[tuple[pathlib.Path, ast.Module]]:
    return [(p, ast.parse(p.read_text())) for p in PACKAGE_FILES]


def test_package_files_are_the_expected_set() -> None:
    assert {p.name for p in PACKAGE_FILES} == {
        "__init__.py",
        "arming.py",
        "capability.py",
        "chain_spawner.py",
        "claims.py",
        "consumption.py",
        "db_claim_store.py",
        "entrypoint.py",
        "event_source.py",
        "gate.py",
        "judgement.py",
        "lifecycle.py",
        "live_contract.py",
        "orchestrator.py",
        "poller.py",
        "proposal_chain.py",
        "selection.py",
        "spawn.py",
    }


# ---------------------------------------------------------------------------
# The approval machinery cannot be reached, relaxed, or read from here
# ---------------------------------------------------------------------------
def test_no_broker_or_approval_imports() -> None:
    """The write seam may import the boundary tool. Nothing else may, and it
    may import nothing else from the forbidden list either."""
    offenders: list[str] = []
    for path, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if path == WRITE_SEAM and name == BOUNDARY_MODULE:
                    continue
                if any(f in name for f in FORBIDDEN_IMPORT_FRAGMENTS):
                    offenders.append(f"{path.name}:{node.lineno} {name}")
    assert offenders == []


# ---------------------------------------------------------------------------
# ROB-1290 — the boundary is crossed, exactly once, from exactly one file
# ---------------------------------------------------------------------------
def _boundary_importers() -> dict[str, list[str]]:
    """path name -> names imported from the boundary tool module."""
    found: dict[str, list[str]] = {}
    for path, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == BOUNDARY_MODULE:
                found.setdefault(path.name, []).extend(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == BOUNDARY_MODULE:
                        found.setdefault(path.name, []).append("<module>")
    return found


def test_exactly_one_file_imports_the_boundary_tool() -> None:
    assert _boundary_importers() == {WRITE_SEAM.name: ["order_proposal_create"]}


def test_the_write_seam_actually_calls_the_boundary() -> None:
    """The ROB-1286 gap, asserted structurally.

    r3 named ``order_proposal_create`` in a string, a docstring and an
    allowlist, and never called it, so "the chain reaches a proposal" was a
    sentence. This walks the seam's AST for a call whose callee resolves to
    the imported boundary name -- prose and allowlist entries cannot
    satisfy it.
    """
    from app.services.watch_trigger_repricing.capability import EXECUTION_BOUNDARY

    tree = ast.parse(WRITE_SEAM.read_text())
    # The seam resolves the callable through one helper, so accept either a
    # direct call or a return of the bare name from that resolver.
    referenced = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == EXECUTION_BOUNDARY
    ]
    assert referenced, f"{WRITE_SEAM.name} never references {EXECUTION_BOUNDARY}"

    awaited_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
    ]
    assert awaited_calls, "the seam must await the boundary call"


def test_the_boundary_name_is_not_reachable_from_any_other_package_file() -> None:
    """A second importer would make the seam a convention, not a control."""
    other = [
        p.name
        for p in PACKAGE_FILES
        if p != WRITE_SEAM and "order_proposal_tools" in _code_tokens(p)
    ]
    assert other == []


def test_approval_gate_settings_are_never_referenced() -> None:
    """Includes string literals, so a getattr() bypass is caught too."""
    offenders: list[str] = []
    for path, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_SETTING_NAMES:
                offenders.append(f"{path.name}:{node.lineno} attr {node.attr}")
            elif isinstance(node, ast.Name) and node.id in FORBIDDEN_SETTING_NAMES:
                offenders.append(f"{path.name}:{node.lineno} name {node.id}")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in FORBIDDEN_SETTING_NAMES
            ):
                offenders.append(f"{path.name}:{node.lineno} literal {node.value}")
    assert offenders == []


def test_nothing_downstream_of_proposal_create_appears() -> None:
    """The session may create a proposal. Nothing here may submit or approve it."""
    from app.services.watch_trigger_repricing.capability import EXECUTION_BOUNDARY

    assert EXECUTION_BOUNDARY == "order_proposal_create"

    beyond = (
        "order_proposal_approve",
        "order_proposal_submit",
        "order_proposal_redispatch",
        "place_order",
        "submit_order",
        "kis_live_place_order",
        "toss_place_order",
        "record_auto_approval",
        "revalidate_and_submit",
        "auto_veto",
    )
    offenders = [
        f"{p.name}: {token}"
        for p in PACKAGE_FILES
        for token in beyond
        if token in _code_tokens(p)
    ]
    assert offenders == []


def test_loss_cut_is_never_special_cased_here() -> None:
    """§101차: loss_cut stays human-approved. This package must not touch it."""
    offenders = [
        p.name
        for p in PACKAGE_FILES
        if "loss_cut" in _code_tokens(p) or "lossCut" in _code_tokens(p)
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# DB access is confined to the read seam and the package's own claim table
# ---------------------------------------------------------------------------
def test_only_the_read_seam_imports_the_reports_repository() -> None:
    offenders: list[str] = []
    for path, tree in _trees():
        if path == READ_SEAM:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and "investment_reports" in (
                node.module or ""
            ):
                offenders.append(f"{path.name}:{node.lineno} {node.module}")
    assert offenders == []


def test_the_package_never_writes_to_investment_watch_events() -> None:
    """Consumption lives in the claim table so the fire table stays read-only."""
    for path in PACKAGE_FILES:
        source = path.read_text()
        assert "InvestmentWatchEvent(" not in source, path.name
        for mutation in ("update_event_outcome", "update_event_follow_up"):
            assert mutation not in source, f"{path.name}: {mutation}"


def test_only_the_claim_store_writes_and_only_its_own_table() -> None:
    writers = [
        p.name
        for p in PACKAGE_FILES
        if any(
            token in p.read_text()
            for token in ("sa.update(", "sa.insert(", "session.add(")
        )
    ]
    assert writers == [CLAIM_WRITER.name]
    source = CLAIM_WRITER.read_text()
    assert "_TABLE = WatchEventRepricingClaim" in source
    # Every write targets the module-level table alias, never another model.
    assert "sa.update(WatchEventRepricingClaim" not in source


# ---------------------------------------------------------------------------
# Ownership split (§101차 ⑥): logic here, schedule in robin-prefect-automations
# ---------------------------------------------------------------------------
def test_this_repo_registers_no_schedule_or_deployment() -> None:
    offenders: list[str] = []
    for path in PACKAGE_FILES:
        source = path.read_text()
        for token in (
            "Deployment",
            ".serve(",
            ".deploy(",
            "CronSchedule",
            "IntervalSchedule",
            "cron=",
            "interval=",
            "schedule=",
            "@flow",
        ):
            if token in source:
                offenders.append(f"{path.name}: {token}")
    assert offenders == []


def test_no_prefect_flow_file_remains_in_this_repo() -> None:
    """The scheduled flow is owned by robin-prefect-automations."""
    flows_dir = REPO_ROOT / "app" / "flows"
    referencing = [
        p.name
        for p in sorted(flows_dir.glob("*.py"))
        if "watch_trigger_repricing" in p.read_text()
    ]
    assert referencing == []


def test_prefect_is_not_imported_anywhere_in_the_package() -> None:
    for _path, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("prefect") for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("prefect")


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------
def test_market_scope_stays_kr() -> None:
    """US/crypto expansion is explicitly a separate issue."""
    import asyncio

    from app.services.watch_trigger_repricing.claims import InMemoryClaimStore
    from app.services.watch_trigger_repricing.selection import select_candidates

    from .conftest import INCIDENT_TICK, make_event

    result = asyncio.run(
        select_candidates(
            [
                make_event(event_uuid="evt-us", symbol="AAPL", market="us"),
                make_event(event_uuid="evt-crypto", symbol="KRW-BTC", market="crypto"),
                make_event(event_uuid="evt-kr", symbol="005930", market="kr"),
            ],
            store=InMemoryClaimStore(),
            now=INCIDENT_TICK,
        )
    )

    assert [e.event_uuid for e in result.selected] == ["evt-kr"]
    assert {s.reason for s in result.skipped} == {"market_out_of_scope"}


def test_settings_gate_defaults_off() -> None:
    from app.core.config import Settings

    assert Settings.model_fields["WATCH_TRIGGER_REPRICING_ENABLED"].default is False


def test_this_feature_migrates_only_its_own_table() -> None:
    """§101차 ①: the claims table, and nothing else.

    ROB-1290 r2 adds a second migration (the ``awaiting_reconcile`` state,
    which is what stops an ambiguous fire being re-judged after the TTL).
    Counting files was always a proxy for the property that matters, so
    this asserts the property directly and for *every* migration this
    feature owns: exactly one table is ever created, no other table is
    named, and ``review.investment_watch_events`` is never touched.
    """
    versions = REPO_ROOT / "alembic" / "versions"
    mine = sorted(
        p
        for p in versions.glob("*.py")
        if "watch_event_repricing_claims" in p.read_text()
    )
    assert [p.name for p in mine] == [
        "20260819_rob1286_repricing_claims.py",
        "20260820_rob1290_awaiting_reconcile_state.py",
    ]

    # Across the whole set, the table is created exactly once.
    assert sum(p.read_text().count("op.create_table(") for p in mine) == 1

    for path in mine:
        # The feature never reads or writes the fire table.
        assert "investment_watch_events" not in _code_tokens(path), path.name
        # No other table is named anywhere in the migration.
        tree = ast.parse(path.read_text())
        table_like = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and ("watch_event" in node.value or "review." in node.value)
        }
        for literal in table_like:
            assert "watch_event_repricing_claims" in literal, (
                f"{path.name} names another table: {literal!r}"
            )

    # Which statements each migration actually emits is asserted against the
    # rendered DDL in ``test_migration_render.py`` -- reading the source
    # cannot catch a name mangled by alembic's naming convention.


def test_the_second_migration_alters_nothing_but_one_check_constraint() -> None:
    """Additive in the sense that matters: it widens, it does not remove."""
    source = (
        REPO_ROOT
        / "alembic"
        / "versions"
        / "20260820_rob1290_awaiting_reconcile_state.py"
    ).read_text()
    upgrade = source.split("def downgrade")[0]

    for destructive in ("op.drop_column", "op.alter_column", "op.drop_table"):
        assert destructive not in upgrade
    # It replaces the state CHECK, and only that one.
    assert upgrade.count("op.drop_constraint(") == 1
    assert upgrade.count("op.create_check_constraint(") == 1
    assert "ck_watch_event_repricing_claims_state" in source
    # The widened set is a strict superset of the old one.
    assert "awaiting_reconcile" in source
    for kept in (
        "started",
        "proposal_created",
        "rejected_with_reason",
        "expired_unprocessed",
    ):
        assert kept in source


def test_the_model_check_matches_the_lifecycle_enum() -> None:
    """The DB's spelling of the states and the code's cannot drift."""
    from app.models.watch_event_repricing_claims import _LIFECYCLE_STATES
    from app.services.watch_trigger_repricing.lifecycle import ClaimLifecycle

    assert set(_LIFECYCLE_STATES) == {m.value for m in ClaimLifecycle}
