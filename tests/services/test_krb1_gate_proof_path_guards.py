"""Guards for the ROB-1172 gate proof path: read-only, GET-only, scheduleless."""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
from pathlib import Path

import pytest

from scripts import krb1_p0_completed_session_oneshot as oneshot

pytestmark = pytest.mark.unit

KST = dt.timezone(dt.timedelta(hours=9))
SESSION = dt.date(2026, 7, 29)
DECISION_AT = dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)

SERVICE_MODULES = (
    Path("app/services/krb1_gate_result.py"),
    Path("app/services/krb1_evidence_chain.py"),
    Path("app/services/krb1_metadata_authority.py"),
    Path("app/services/krb1_completion_manifest.py"),
    Path("app/services/krb1_reference_price_evidence.py"),
    Path("app/services/krb1_reference_exception_adapter.py"),
    Path("app/services/krb1_quote_timestamp_capture.py"),
)
CLI_MODULES = (
    Path("scripts/krb1_p0_completed_session_oneshot.py"),
    Path("scripts/krb1_p0_metadata_snapshot_capture.py"),
    Path("scripts/krb1_p0_quote_timestamp_capture.py"),
    Path("scripts/krb1_p0_liquidity_selector.py"),
)
FORBIDDEN_SCHEDULER_TOKENS = (
    "taskiq",
    "broker.task",
    "@broker",
    "celery",
    "crontab",
    "CronTrigger",
    "prefect",
    "add_job",
    "scheduled_task",
)
FORBIDDEN_MUTATION_TOKENS = (
    "INSERT INTO",
    "UPDATE ",
    "DELETE FROM",
    "order_preview",
    "place_order",
    "cancel_order",
    "submit_order",
    "session.add(",
    "session.commit(",
)


@pytest.mark.parametrize("path", SERVICE_MODULES + CLI_MODULES, ids=lambda p: p.name)
def test_no_scheduler_registration(path: Path) -> None:
    source = path.read_text()
    for token in FORBIDDEN_SCHEDULER_TOKENS:
        assert token not in source, f"{path} references scheduler token {token!r}"


@pytest.mark.parametrize("path", SERVICE_MODULES + CLI_MODULES, ids=lambda p: p.name)
def test_no_mutation_or_order_surface(path: Path) -> None:
    source = path.read_text()
    for token in FORBIDDEN_MUTATION_TOKENS:
        assert token not in source, f"{path} references mutation token {token!r}"


@pytest.mark.parametrize("path", SERVICE_MODULES, ids=lambda p: p.name)
def test_service_modules_stay_out_of_db_and_broker_surfaces(path: Path) -> None:
    tree = ast.parse(path.read_text())
    modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    for module in modules:
        assert "sqlalchemy" not in module, path
        assert "app.core.db" not in module, path
        assert "app.models" not in module, path
        assert "app.services.brokers" not in module, path


@pytest.mark.parametrize(
    "path",
    (
        Path("scripts/krb1_p0_completed_session_oneshot.py"),
        Path("scripts/krb1_p0_metadata_snapshot_capture.py"),
        Path("scripts/krb1_p0_liquidity_selector.py"),
    ),
    ids=lambda p: p.name,
)
def test_db_reading_clis_open_read_only_transactions(path: Path) -> None:
    source = path.read_text()
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in source
    assert "session.rollback()" in source


def test_no_scheduler_module_references_the_new_clis() -> None:
    names = {path.stem for path in CLI_MODULES}
    scheduler_roots = (Path("app/tasks"), Path("app/jobs"), Path("config"))
    for root in scheduler_roots:
        if not root.exists():
            continue
        for candidate in root.rglob("*"):
            if not candidate.is_file() or candidate.suffix not in {
                ".py",
                ".yaml",
                ".yml",
                ".json",
                ".toml",
            }:
                continue
            content = candidate.read_text(errors="ignore")
            for name in names:
                assert name not in content, f"{candidate} schedules {name}"


def test_oneshot_refuses_to_run_before_the_daily_completion_cutoff() -> None:
    result = asyncio.run(
        oneshot.run(
            as_of_session=SESSION,
            decision_at=DECISION_AT,
            store_dir=Path("/nonexistent"),
            max_symbols=None,
            append=False,
            now=dt.datetime(2026, 7, 29, 14, 0, tzinfo=KST),
        )
    )

    assert result["status"] == "fail_closed"
    assert result["reason"] == (
        "completed_session_raw_collection_before_daily_completion_cutoff"
    )
    assert result["required_at_or_after_kst"] == "2026-07-29T15:35:00+09:00"
    assert result["scheduleless_one_shot"] is True


def test_oneshot_refuses_to_run_after_the_decision_clock() -> None:
    result = asyncio.run(
        oneshot.run(
            as_of_session=SESSION,
            decision_at=DECISION_AT,
            store_dir=Path("/nonexistent"),
            max_symbols=None,
            append=False,
            now=dt.datetime(2026, 7, 29, 19, 0, tzinfo=KST),
        )
    )

    assert result["status"] == "fail_closed"
    assert result["reason"] == "sweep_started_after_decision_at"


def test_oneshot_cli_requires_an_offset_aware_decision_clock() -> None:
    with pytest.raises(SystemExit):
        oneshot.parse_args(
            ["--as-of-session", "2026-07-29", "--decision-at", "2026-07-29T18:00:00"]
        )
    args = oneshot.parse_args(
        ["--as-of-session", "2026-07-29", "--decision-at", "2026-07-29T18:00:00+09:00"]
    )
    assert args.decision_at == DECISION_AT


# ───────── A1/A2: no local clock may be dressed up as provider authority ─────────

METADATA_CAPTURE = Path("scripts/krb1_p0_metadata_snapshot_capture.py")
PROVIDER_CLOCK_PARAMS = frozenset(
    {"provider_clock", "published_at", "effective_session"}
)
LOCAL_CLOCK_NAMES = frozenset({"retrieved_at", "now", "utcnow", "captured_at"})


def test_metadata_capture_never_synthesizes_a_provider_clock() -> None:
    """🔴 The capture path must extract the provider clock, never derive it."""
    tree = ast.parse(METADATA_CAPTURE.read_text())

    # No provider-clock argument may be fed a local clock or a now() call.
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg not in PROVIDER_CLOCK_PARAMS:
                continue
            value = keyword.value
            if isinstance(value, ast.Name) and value.id in LOCAL_CLOCK_NAMES:
                offenders.append(f"{keyword.arg}={value.id}")
            if isinstance(value, ast.Call):
                target = value.func
                if isinstance(target, ast.Attribute) and target.attr in {
                    "now",
                    "utcnow",
                }:
                    offenders.append(f"{keyword.arg}=<clock call>")
    assert not offenders, offenders

    # The only sanctioned builder is the extractor.
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "extract_provider_authority_clock" in called
    assert "ProviderAuthorityClock" not in called, (
        "capture must not construct a provider clock directly"
    )


def test_metadata_capture_dropped_the_retrieval_as_authority_labels() -> None:
    """The old substitution and its label must be gone from executable code.

    Historical mentions inside docstrings are fine — that is where the defect is
    documented — so only non-docstring string constants are inspected.
    """
    tree = ast.parse(METADATA_CAPTURE.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    }
    assert "http_retrieval" not in literals
    assert "authority_clock_source" not in literals
    assert "metadata_as_of" not in literals

    source_calls = [
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
    ]
    assert "metadata_as_of" not in source_calls
    assert "provider_clock" in source_calls


def test_metadata_capture_uses_a_local_clock_only_for_retrieval() -> None:
    """``datetime.now`` may only produce the retrieval clock or the ``now`` argument."""
    tree = ast.parse(METADATA_CAPTURE.read_text())
    clock_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"now", "utcnow"}
    ]
    assert clock_calls, "capture still needs a retrieval clock"

    allowed_assign_targets = {"retrieved_at"}
    allowed_keywords = {"now"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_clock_call(node.value):
            targets = {
                target.id for target in node.targets if isinstance(target, ast.Name)
            }
            assert targets <= allowed_assign_targets, targets
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if _contains_clock_call(keyword.value):
                    assert keyword.arg in allowed_keywords, keyword.arg


def _is_clock_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"now", "utcnow"}
    )


def _contains_clock_call(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr in {"now", "utcnow"}
        for child in ast.walk(node)
    )
