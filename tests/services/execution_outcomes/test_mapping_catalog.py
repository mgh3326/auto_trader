from __future__ import annotations

import ast
import json
from pathlib import Path

from app.services.execution_outcomes import (
    CURRENT_RESPONSE_MAPPINGS,
    BrokerAcceptance,
    BrokerSurface,
    EvidenceFinality,
    LegacyResponseMapping,
    TerminalEvidence,
    TrackingState,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).parent / "fixtures" / "current_response_semantics.json"
OUTCOME_FIELDS = (
    "request_validated",
    "mutation_sent",
    "broker_acceptance",
    "tracking",
    "local_recorded",
    "reconcile_required",
    "terminal_evidence",
)


def _outcome_values(entry: LegacyResponseMapping) -> list[bool | str]:
    values: list[bool | str] = []
    for field_name in OUTCOME_FIELDS:
        value = getattr(entry.outcome, field_name)
        values.append(value.value if hasattr(value, "value") else value)
    return values


def _mapping(mapping_id: str) -> LegacyResponseMapping:
    matches = [
        entry for entry in CURRENT_RESPONSE_MAPPINGS if entry.mapping_id == mapping_id
    ]
    assert len(matches) == 1
    return matches[0]


def _function_node(relative_path: str, function_name: str) -> ast.AST:
    tree = ast.parse((REPO_ROOT / relative_path).read_text())
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == function_name
    ]
    assert len(matches) == 1
    return matches[0]


def _response_dict(node: ast.AST, sentinel_key: str) -> dict[str, ast.expr]:
    for child in ast.walk(node):
        if not isinstance(child, ast.Return) or not isinstance(child.value, ast.Dict):
            continue
        keys = [
            key.value if isinstance(key, ast.Constant) else None
            for key in child.value.keys
        ]
        if sentinel_key not in keys:
            continue
        return {
            key: value
            for key, value in zip(keys, child.value.values, strict=True)
            if isinstance(key, str)
        }
    raise AssertionError(f"response dict with {sentinel_key=} not found")


def _assigned_expressions(node: ast.AST, target_name: str) -> set[str]:
    expressions: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == target_name
            for target in child.targets
        ):
            expressions.add(ast.unparse(child.value))
    return expressions


def test_mapping_catalog_matches_frozen_current_response_fixture() -> None:
    expected = json.loads(FIXTURE.read_text())
    actual = {
        entry.mapping_id: {
            "surface": entry.surface.value,
            "legacy_success_meaning": entry.legacy_success_meaning.value,
            "legacy_response_markers": list(entry.legacy_response_markers),
            "outcome": _outcome_values(entry),
        }
        for entry in CURRENT_RESPONSE_MAPPINGS
    }

    assert expected["schema_version"] == "execution-outcome-mapping-fixture/v2"
    assert expected["outcome_fields"] == list(OUTCOME_FIELDS)
    assert actual == expected["mappings"]


def test_catalog_covers_every_approved_broker_surface() -> None:
    assert {entry.surface for entry in CURRENT_RESPONSE_MAPPINGS} == set(BrokerSurface)
    marker_sets = {
        frozenset(entry.legacy_response_markers) for entry in CURRENT_RESPONSE_MAPPINGS
    }
    assert len(marker_sets) == len(CURRENT_RESPONSE_MAPPINGS)


def test_legacy_success_true_needs_explicit_evidence_to_mean_filled() -> None:
    success_true_rows = [
        entry
        for entry in CURRENT_RESPONSE_MAPPINGS
        if "success=true" in entry.legacy_response_markers
    ]

    assert success_true_rows
    assert any(not entry.outcome.mutation_sent for entry in success_true_rows)
    assert any(
        entry.outcome.broker_acceptance is BrokerAcceptance.ACCEPTED
        and entry.outcome.terminal_evidence is TerminalEvidence.NONE
        for entry in success_true_rows
    )
    filled_rows = [
        entry
        for entry in success_true_rows
        if entry.outcome.terminal_evidence is TerminalEvidence.FILLED
    ]
    assert filled_rows
    assert all(
        "order_status=filled" in entry.legacy_response_markers for entry in filled_rows
    )


def test_generic_tracking_matches_current_live_response_source() -> None:
    from app.mcp_server.tooling.live_order_ledger import _derive_live_send_status

    assert _derive_live_send_status(rt_cd="0", order_no="broker-1") == "accepted"
    assert _derive_live_send_status(rt_cd="0", order_no=None) == "accepted"

    record_node = _function_node(
        "app/mcp_server/tooling/live_order_ledger.py", "_record_live_order"
    )
    response = _response_dict(record_node, "broker_status")
    assert ast.unparse(response["order_id"]) == "str(order_no) if order_no else None"
    assert ast.unparse(response["broker_status"]) == "status"
    assert ast.unparse(response["fill_recorded"]) == "fill_recorded"

    tracked = _mapping("generic.live_accepted_pending_fill")
    untracked = _mapping("generic.live_accepted_untracked_pending_fill")
    assert "order_id=non_empty" in tracked.legacy_response_markers
    assert tracked.outcome.tracking is TrackingState.TRACKED
    assert "order_id=absent" in untracked.legacy_response_markers
    assert untracked.outcome.tracking is TrackingState.UNTRACKED
    assert untracked.outcome.reconcile_required is True


def test_alpaca_cancel_mappings_match_current_readback_and_sync_source() -> None:
    from app.services.alpaca_paper_ledger_service import (
        normalize_known_broker_order_status,
    )

    assert normalize_known_broker_order_status("pending_cancel") == "pending_cancel"
    assert normalize_known_broker_order_status("partially_filled") == "partially_filled"
    assert normalize_known_broker_order_status("filled") == "filled"
    assert normalize_known_broker_order_status("unknown") is None

    cancel_node = _function_node(
        "app/mcp_server/tooling/alpaca_paper_orders.py", "alpaca_paper_cancel_order"
    )
    response = _response_dict(cancel_node, "cancel_requested")
    assert ast.unparse(response["cancelled"]) == "cancel_confirmed"
    assert ast.unparse(response["order_status"]) == "broker_status"
    assert ast.unparse(response["read_back_status"]) == "read_back_status"
    assert ast.unparse(response["lifecycle_synced"]) == "lifecycle_synced"
    assert _assigned_expressions(cancel_node, "read_back_status") == {
        "'ok'",
        "'unavailable'",
    }
    assert _assigned_expressions(cancel_node, "cancel_confirmed") == {
        "read_back_status == 'ok' and normalized_status == 'canceled'"
    }
    assert _assigned_expressions(cancel_node, "lifecycle_synced") == {
        "False",
        "True",
    }

    unavailable = _mapping("alpaca.cancel_readback_unavailable")
    assert unavailable.outcome.local_recorded is False
    assert unavailable.outcome.reconcile_required is True

    for suffix, evidence in (
        ("open", TerminalEvidence.NONE),
        ("partial", TerminalEvidence.PARTIAL_FILL),
        ("filled", TerminalEvidence.FILLED),
    ):
        synced = _mapping(f"alpaca.cancel_{suffix}_synced")
        unsynced = _mapping(f"alpaca.cancel_{suffix}_unsynced")
        assert synced.outcome.terminal_evidence is evidence
        assert unsynced.outcome.terminal_evidence is evidence
        assert synced.outcome.local_recorded is True
        assert unsynced.outcome.local_recorded is False
        assert synced.outcome.reconcile_required is True
        assert unsynced.outcome.reconcile_required is True

    filled = _mapping("alpaca.cancel_filled_synced")
    assert "order_status=filled" in filled.legacy_response_markers
    assert "lifecycle_synced=true" in filled.legacy_response_markers
    assert filled.outcome.is_filled is True


def test_unknown_and_partial_fixture_rows_are_fail_closed() -> None:
    unknown_rows = [
        entry
        for entry in CURRENT_RESPONSE_MAPPINGS
        if entry.outcome.broker_acceptance is BrokerAcceptance.UNKNOWN
    ]
    partial_rows = [
        entry
        for entry in CURRENT_RESPONSE_MAPPINGS
        if entry.outcome.terminal_evidence is TerminalEvidence.PARTIAL_FILL
    ]

    assert unknown_rows and partial_rows
    assert all(entry.outcome.reconcile_required for entry in unknown_rows)
    assert all(not entry.outcome.automatic_resend_allowed for entry in unknown_rows)
    assert all(entry.outcome.reconcile_required for entry in partial_rows)
    assert all(
        entry.outcome.evidence_finality is EvidenceFinality.NON_TERMINAL
        for entry in partial_rows
    )
    assert all(not entry.outcome.automatic_resend_allowed for entry in partial_rows)


def test_catalog_is_declarative_and_has_reviewable_source_locators() -> None:
    assert all(
        entry.source_locator.startswith("app/") for entry in CURRENT_RESPONSE_MAPPINGS
    )
    assert all("::" in entry.source_locator for entry in CURRENT_RESPONSE_MAPPINGS)
