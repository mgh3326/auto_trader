from __future__ import annotations

import builtins
import dataclasses
import inspect
import io
from collections.abc import Callable
from typing import Any, cast

import pytest
import rob1040_crs24_evidence as evidence_module
import rob1040_crs24_feasibility as feasibility_module
from rob974_h4_contracts import exact_h4_folds
from rob1040_crs24_cli import RUN_AUTHORITY_CLOSED, run_cli
from rob1040_crs24_contracts import ACTIVE_CONFIGS
from rob1040_crs24_evidence import (
    build_evidence,
    build_frozen_synthetic_evidence,
    open_frozen_synthetic_test_seam,
    render_evidence_bytes,
    run_frozen_synthetic_cells,
)
from rob1040_crs24_feasibility import (
    DISPERSION_GATE_CLOSED,
    ENTRY_REFERENCE_MISSING,
    RunAuthorityClosedError,
)
from rob1040_crs24_features import CRSFeatureGenerator
from rob1040_crs24_synthetic import build_synthetic_fixture


@pytest.fixture(scope="module")
def evidence():
    return build_frozen_synthetic_evidence()


def test_exact_ordered_3x8_cells_and_campaign_reconciliation(evidence) -> None:
    assert tuple((cell.config_id, cell.fold_id) for cell in evidence.cells) == tuple(
        (config.config_id, fold.fold_id)
        for config in ACTIVE_CONFIGS
        for fold in exact_h4_folds()
    )
    assert len(evidence.cells) == 24
    assert evidence.totals.scheduled == 24 * 56 == 1_344
    assert evidence.totals.horizon_eligible == 24 * 54 == 1_296
    assert evidence.totals.fold_horizon_closed == 24 * 2 == 48
    assert evidence.totals.valid_input == 1_296
    assert evidence.totals.planned == 205
    assert evidence.totals.occupied == 286
    assert evidence.totals.long_count == 94
    assert evidence.totals.short_count == 111
    assert (
        evidence.totals.planned
        + sum(count for _reason, count in evidence.totals.closed_histogram)
        == evidence.totals.scheduled
    )


def test_full_input_identity_is_bound_once_and_cell_hashes_remain_causal(
    evidence,
) -> None:
    payload = evidence.to_payload()
    assert payload["cell_shape"] == [3, 8]
    assert payload["posture"] == "outcome_blind_feasibility_only"
    assert payload["authorities"]["input"] == {
        "posture": "frozen_synthetic_fixture",
        "fixture_version": "rob1040.crs24.corr1.synthetic.v1",
        "complete_bar_snapshot_sha256": (
            "429c1e26490cef60652df6ef75f23f2fb386092815ea71019ea0041bf2a9f14f"
        ),
        "entry_reference_source_sha256": (
            "5cc827b30d3a12ad5799f9da10cd866292b1e9708b4a734cf9296e3285157669"
        ),
        "exit_presence_source_sha256": (
            "14a217e426ef8c6f4c02661f6bfbd128a467263e1121665b983223d6a306bf43"
        ),
        "fixture_content_sha256": (
            "718d1bd865cc77dc6b26d44c1da2859b8180ad6f4a8520dfdbef9f8ae35b5b49"
        ),
        "binding": "validated_campaign_context_object_identity",
    }
    assert len(payload["cells"]) == 24
    hashes: set[str] = set()
    forbidden_full_identity_keys = {
        "complete_bar_snapshot_sha256",
        "entry_reference_source_sha256",
        "exit_presence_source_sha256",
        "fixture_content_sha256",
    }
    for cell in payload["cells"]:
        assert len(cell["cell_sha256"]) == 64
        hashes.add(cell["cell_sha256"])
        assert set(cell["hashes"]) == {
            "preregistration_sha256",
            "contract_sha256",
            "filter_manifest_sha256",
            "fold_schedule_sha256",
            "causal_feature_source_sha256",
            "consulted_entry_reference_sha256",
            "consulted_exit_presence_sha256",
        }
        assert forbidden_full_identity_keys.isdisjoint(cell["hashes"])
        assert cell["authority"]["exit_reference"] == "timestamp_presence_only"
        assert cell["authority"]["numeric_hash_scope"] == (
            "causal_feature_and_consulted_entry_only"
        )
        assert cell["calendar_counts"] == {
            "scheduled": 56,
            "horizon_eligible": 54,
            "fold_horizon_closed": 2,
        }
        assert cell["reconciliation"] == {
            "closed_total": 56 - cell["planned"],
            "planned": cell["planned"],
            "scheduled": 56,
            "closed_plus_planned_equals_scheduled": True,
            "horizon_partition_exact": True,
            "winner_lifecycle_exact": True,
            "event_terminal_exact": True,
        }
        assert cell["movement_capacity"]["count"] == cell["planned"]
        assert sum(cell["directions"].values()) == cell["planned"]
        assert (
            sum(cell["symbol_concentration"]["planned_by_symbol"].values())
            == cell["planned"]
        )
    assert len(hashes) == 24


def test_context_and_evidence_cannot_be_forged_outside_the_factory(evidence) -> None:
    context = open_frozen_synthetic_test_seam()
    context_type = type(context)
    with pytest.raises(RunAuthorityClosedError, match="capability denied"):
        cast(Callable[..., object], context_type)(object())
    forged_context = object.__new__(context_type)
    with pytest.raises(RunAuthorityClosedError, match="unregistered campaign"):
        forged_context.cells()

    evidence_type = type(evidence)
    with pytest.raises(RunAuthorityClosedError, match="capability denied"):
        evidence_type(object())
    forged_evidence = object.__new__(evidence_type)
    with pytest.raises(RunAuthorityClosedError, match="unregistered evidence"):
        forged_evidence.to_payload()
    assert not dataclasses.is_dataclass(evidence)
    assert not hasattr(feasibility_module, "InputAuthority")


def test_all_evidence_accessors_share_one_immutable_closure_seal(evidence) -> None:
    original_digest = evidence.evidence_sha256
    original_totals = evidence.totals
    mutable = cast(Any, evidence)
    with pytest.raises(AttributeError):
        mutable._digest = "f" * 64
    with pytest.raises(AttributeError):
        mutable._totals = dataclasses.replace(
            original_totals,
            long_count=original_totals.short_count,
            short_count=original_totals.long_count,
        )
    with pytest.raises(AttributeError):
        mutable._cells = evidence.cells
    with pytest.raises(AttributeError):
        object.__setattr__(evidence, "_digest", "f" * 64)

    assert evidence.evidence_sha256 == original_digest
    assert evidence.totals == original_totals
    assert evidence.totals is not original_totals


def test_each_evidence_accessor_revalidates_issued_state_symmetrically() -> None:
    context = open_frozen_synthetic_test_seam()
    cells = context.cells()
    evidence = context.seal_cells(cells)
    event = next(
        event
        for cell in cells
        for event in cell.events
        if event.closed_reason == DISPERSION_GATE_CLOSED
    )
    object.__setattr__(event, "closed_reason", ENTRY_REFERENCE_MISSING)

    accessors = (
        lambda: evidence.cells,
        lambda: evidence.totals,
        lambda: evidence.evidence_sha256,
        evidence.to_payload,
    )
    for accessor in accessors:
        with pytest.raises(RunAuthorityClosedError, match="state changed after"):
            accessor()


def test_caller_inputs_cannot_reach_campaign_computation(evidence) -> None:
    assert not hasattr(feasibility_module, "_evaluate_cell")
    assert not hasattr(feasibility_module, "_evaluate_all_cells")
    with pytest.raises(TypeError):
        cast(Callable[..., object], open_frozen_synthetic_test_seam)(object())
    with pytest.raises(TypeError):
        cast(Callable[..., object], run_frozen_synthetic_cells)(object())
    with pytest.raises(RunAuthorityClosedError, match="issuance is closed"):
        build_evidence(evidence.cells)

    context = open_frozen_synthetic_test_seam()
    evaluate_all = inspect.getclosurevars(cast(Any, context.cells).__func__).nonlocals[
        "evaluate_all"
    ]
    evaluate_cell = inspect.getclosurevars(evaluate_all).nonlocals["evaluate_cell"]
    forged_config = dataclasses.replace(ACTIVE_CONFIGS[0])
    with pytest.raises(RunAuthorityClosedError, match="unregistered config"):
        evaluate_cell(forged_config, exact_h4_folds()[0])
    with pytest.raises(RunAuthorityClosedError, match="unregistered fold"):
        evaluate_cell(ACTIVE_CONFIGS[0], exact_h4_folds()[0])


def test_cells_from_distinct_contexts_or_equal_copies_cannot_mix() -> None:
    first = open_frozen_synthetic_test_seam()
    second = open_frozen_synthetic_test_seam()
    first_cells = first.cells()
    second_cells = second.cells()
    mixed = (first_cells[0], *second_cells[1:])
    with pytest.raises(RunAuthorityClosedError, match="not issued by this"):
        first.seal_cells(mixed)

    equal_copy = dataclasses.replace(first_cells[0])
    assert equal_copy == first_cells[0]
    assert equal_copy is not first_cells[0]
    copied = (equal_copy, *first_cells[1:])
    with pytest.raises(RunAuthorityClosedError, match="not issued by this"):
        first.seal_cells(copied)


def test_bound_generator_and_reference_content_are_rechecked_at_use() -> None:
    context = open_frozen_synthetic_test_seam()
    evaluate_all = inspect.getclosurevars(cast(Any, context.cells).__func__).nonlocals[
        "evaluate_all"
    ]
    evaluate_cell = inspect.getclosurevars(evaluate_all).nonlocals["evaluate_cell"]
    bound = inspect.getclosurevars(evaluate_cell).nonlocals

    fixture = build_synthetic_fixture()
    changed_bars = fixture.bars_by_symbol()
    first = changed_bars["XRPUSDT"][0]
    changed_bars["XRPUSDT"] = (
        dataclasses.replace(first, volume=first.volume + 1.0),
        *changed_bars["XRPUSDT"][1:],
    )
    changed_generator = CRSFeatureGenerator(changed_bars)
    object.__setattr__(
        bound["generator"],
        "_snapshot",
        cast(Any, changed_generator)._snapshot,
    )
    with pytest.raises(RunAuthorityClosedError, match="snapshot pin changed at use"):
        context.cells()

    context = open_frozen_synthetic_test_seam()
    evaluate_all = inspect.getclosurevars(cast(Any, context.cells).__func__).nonlocals[
        "evaluate_all"
    ]
    evaluate_cell = inspect.getclosurevars(evaluate_all).nonlocals["evaluate_cell"]
    references = inspect.getclosurevars(evaluate_cell).nonlocals["references"]
    entries = references.entries
    entry_value = entries[0].value
    assert entry_value is not None
    object.__setattr__(
        references,
        "entries",
        (
            dataclasses.replace(entries[0], value=entry_value * 2),
            *entries[1:],
        ),
    )
    with pytest.raises(
        RunAuthorityClosedError, match="entry-source pin changed at use"
    ):
        context.cells()


def test_same_identity_cell_state_mutation_cannot_be_sealed() -> None:
    context = open_frozen_synthetic_test_seam()
    cells = context.cells()
    event = next(
        event
        for cell in cells
        for event in cell.events
        if event.closed_reason == DISPERSION_GATE_CLOSED
    )
    object.__setattr__(event, "closed_reason", ENTRY_REFERENCE_MISSING)
    with pytest.raises(RunAuthorityClosedError, match="state changed after"):
        context.seal_cells(cells)


def test_synthetic_evidence_hash_is_byte_stable_and_tamper_evident(evidence) -> None:
    assert (
        evidence.evidence_sha256
        == "ef9b0b819bef5e4cb0a63a9f88b5df0a7a65832d103d4ed379ba460fa3232bab"
    )
    assert evidence.to_payload()["evidence_sha256"] == evidence.evidence_sha256
    rendered = render_evidence_bytes()
    assert rendered == render_evidence_bytes()
    assert rendered.endswith(b"\n")


def test_numeric_payload_counts_are_recomputed_from_terminal_events(evidence) -> None:
    for model, payload in zip(
        evidence.cells,
        evidence.to_payload()["cells"],
        strict=True,
    ):
        assert payload["valid_input"] == sum(
            event.gate is not None
            and event.gate.feature.__class__.__name__ == "CRSFeature"
            for event in model.events
        )
        assert payload["joint_gate_pass"] == sum(
            event.gate is not None and event.gate.joint_pass for event in model.events
        )
        assert payload["planned"] == sum(
            event.closed_reason is None for event in model.events
        )
        assert payload["closed_histogram"] == {
            reason: sum(event.closed_reason == reason for event in model.events)
            for reason in payload["closed_histogram"]
        }
        assert payload["reconciliation"]["event_terminal_exact"] is (
            model.lifecycle_replay.event_terminal_exact
        )


def test_public_cell_payload_sealer_is_closed(evidence) -> None:
    with pytest.raises(RunAuthorityClosedError, match="cell sealing is closed"):
        evidence_module.cell_payload(evidence.cells[0])
    with pytest.raises(TypeError):
        cast(Callable[..., object], render_evidence_bytes)(evidence)


def test_no_arg_and_plan_are_pure_identical_and_do_not_read_files(monkeypatch) -> None:
    def refuse_open(*_args, **_kwargs):
        raise AssertionError("plan attempted a filesystem read")

    monkeypatch.setattr(builtins, "open", refuse_open)
    first_out = io.StringIO()
    first_err = io.StringIO()
    second_out = io.StringIO()
    second_err = io.StringIO()
    assert run_cli((), stdout=first_out, stderr=first_err) == 0
    assert run_cli(("--plan",), stdout=second_out, stderr=second_err) == 0
    assert first_out.getvalue() == second_out.getvalue()
    assert first_err.getvalue() == second_err.getvalue() == ""
    assert '"launch_state":"closed_pending_merge_refreeze_and_separate_approval"' in (
        first_out.getvalue()
    )


def test_run_is_unconditionally_closed_before_any_adapter() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    assert run_cli(("--run",), stdout=stdout, stderr=stderr) == RUN_AUTHORITY_CLOSED
    assert stdout.getvalue() == ""
    assert (
        stderr.getvalue()
        == "RUN_AUTHORITY_CLOSED merge_refreeze_and_separate_approval_required\n"
    )
