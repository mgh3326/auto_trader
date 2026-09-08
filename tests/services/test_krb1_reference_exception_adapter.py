"""AC4 — prove the reference-exception adapter blocks rather than passes."""

from __future__ import annotations

import ast
import datetime as dt
import inspect
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.krb1_reference_exception_adapter import (
    FAIL_CLOSED_REASON,
    SOURCE_WIRED,
    ReferenceExceptionFetchResult,
    ReferenceExceptionSourceNotWired,
    evaluate_with_adapter,
    fetch_reference_price_exceptions,
)
from app.services.krb1_reference_price_evidence import (
    ReferencePriceExceptionRecord,
    evaluate_reference_price_exception_coverage,
)

pytestmark = pytest.mark.unit

KST = dt.timezone(dt.timedelta(hours=9))
TARGET = dt.date(2026, 7, 30)
DECISION_AT = dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)
ADAPTER_PATH = Path("app/services/krb1_reference_exception_adapter.py")


def _record(symbol: str) -> ReferencePriceExceptionRecord:
    return ReferencePriceExceptionRecord(
        symbol=symbol,
        effective_session=TARGET,
        is_exception=False,
        source="krx_official_base_price",
        published_at=dt.datetime(2026, 7, 29, 16, 0, tzinfo=KST),
        retrieved_at=dt.datetime(2026, 7, 29, 17, 0, tzinfo=KST),
        determination_method="NORMAL_PRIOR_CLOSE",
        raw_reference_price="10000",
        raw_reason_code="NORMAL",
        raw_payload_sha256="b" * 64,
    )


def test_source_is_not_wired() -> None:
    assert SOURCE_WIRED is False


@pytest.mark.parametrize(
    "symbols",
    [
        [],
        ["005930"],
        [f"{index:06d}" for index in range(500)],
    ],
)
def test_fetch_always_returns_zero_records_and_unprovable(
    symbols: list[str],
) -> None:
    result = fetch_reference_price_exceptions(
        symbols=symbols, target_session=TARGET, decision_at=DECISION_AT
    )

    assert result.status == "unprovable"
    assert result.reason == FAIL_CLOSED_REASON
    assert result.records == ()
    assert result.source_wired is False
    assert result.fallback_forbidden is True


@pytest.mark.parametrize(
    "target_session",
    [dt.date(2020, 1, 2), dt.date(2026, 7, 30), dt.date(2099, 12, 31)],
)
def test_fetch_cannot_be_coaxed_by_session_choice(target_session: dt.date) -> None:
    result = fetch_reference_price_exceptions(
        symbols=["005930"],
        target_session=target_session,
        decision_at=DECISION_AT,
    )
    assert result.records == ()
    assert result.status == "unprovable"


def test_result_refuses_to_carry_records() -> None:
    with pytest.raises(ReferenceExceptionSourceNotWired):
        ReferenceExceptionFetchResult(
            requested_symbols=("005930",),
            target_session=TARGET.isoformat(),
            decision_at=DECISION_AT.isoformat(),
            records=(_record("005930"),),
        )


def test_result_refuses_status_reason_or_wired_override() -> None:
    fetched = fetch_reference_price_exceptions(
        symbols=["005930"], target_session=TARGET, decision_at=DECISION_AT
    )
    for override in (
        {"status": "proven"},
        {"reason": "operator_says_it_is_fine"},
        {"source_wired": True},
        {"records": (_record("005930"),)},
    ):
        with pytest.raises(ReferenceExceptionSourceNotWired):
            replace(fetched, **override)  # type: ignore[arg-type]


def test_adapter_gate_result_is_unprovable() -> None:
    gate, fetched = evaluate_with_adapter(
        symbols=["005930", "000660"],
        target_session=TARGET,
        decision_at=DECISION_AT,
    )

    assert fetched.records == ()
    assert gate.status == "unprovable"
    assert gate.reason == "target_session_reference_price_exception_unproven"
    assert gate.evidence["defect"] == "authoritative_source_unavailable"
    assert gate.evidence["source_unavailable_reason"] == FAIL_CLOSED_REASON
    assert gate.evidence["fallback_forbidden"] is True


def test_unavailable_source_blocks_even_when_valid_records_are_supplied() -> None:
    """The unwired reason wins over caller-supplied evidence."""
    gate = evaluate_reference_price_exception_coverage(
        records=[_record("005930")],
        required_symbols=["005930"],
        target_session=TARGET,
        decision_at=DECISION_AT,
        source_unavailable_reason=FAIL_CLOSED_REASON,
    )
    assert gate.status == "unprovable"
    assert gate.evidence["defect"] == "authoritative_source_unavailable"


def test_gate_is_satisfiable_in_principle_so_the_block_is_attributable() -> None:
    """Guard against a false block: with a wired source the gate can pass.

    Without this, an always-unprovable gate would be indistinguishable from a
    structurally broken one, and the fail-close reason would not be attributable
    to the missing source.
    """
    gate = evaluate_reference_price_exception_coverage(
        records=[_record("005930")],
        required_symbols=["005930"],
        target_session=TARGET,
        decision_at=DECISION_AT,
    )
    assert gate.status == "proven"
    assert gate.reason == "target_session_reference_price_exception_coverage_proven"


def test_adapter_has_no_record_producing_parameter() -> None:
    signature = inspect.signature(fetch_reference_price_exceptions)
    assert set(signature.parameters) == {"symbols", "target_session", "decision_at"}
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )


def test_adapter_module_never_constructs_a_record_and_reads_no_config() -> None:
    tree = ast.parse(ADAPTER_PATH.read_text())

    constructed = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ReferencePriceExceptionRecord"
    ]
    assert not constructed, "a fail-closed stub must never build evidence"

    assigns_exception_flag = [
        keyword
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "is_exception"
    ]
    assert not assigns_exception_flag

    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert "os" not in {module.split(".")[0] for module in imported_modules}
    assert not [module for module in imported_modules if "config" in module]
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}
    ]
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in {"settings", "getenv", "environ"}
    ]


def test_source_wired_constant_is_assigned_false_exactly_once() -> None:
    tree = ast.parse(ADAPTER_PATH.read_text())
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "SOURCE_WIRED"
    ]
    assert len(assignments) == 1
    value = assignments[0].value
    assert isinstance(value, ast.Constant) and value.value is False


def test_environment_cannot_flip_the_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "KRB1_REFERENCE_EXCEPTION_SOURCE_WIRED",
        "REFERENCE_EXCEPTION_SOURCE",
        "TOSS_API_ENABLED",
        "KIWOOM_MOCK_ENABLED",
    ):
        monkeypatch.setenv(key, "1")

    result = fetch_reference_price_exceptions(
        symbols=["005930"], target_session=TARGET, decision_at=DECISION_AT
    )
    assert result.status == "unprovable"
    assert result.records == ()
