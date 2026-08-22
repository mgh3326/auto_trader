"""ROB-1272 (J7) — cross-lane read model: schema, invariants, and mutant kills.

Every test here is offline: no database, no broker, no network. File access is
confined to ``tmp_path`` and to read-only stat/hash checks of the orch-stamped
binding artifacts (skipped when those artifacts are not present, e.g. CI).
"""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from app.schemas.execution_contracts import EvidenceTier
from app.schemas.mock_auto_read_model import (
    AncestorUnknown,
    AnomalyEntry,
    EvidenceClass,
    EvidenceRef,
    EvidenceSourceBinding,
    HoldEntry,
    LaneCoverageRow,
    LifecycleObservationRow,
    LifecycleStage,
    ManifestRef,
    MockAutoReadModelResponse,
    PredecessorRecord,
    ReadModelNotes,
    ReadModelReject,
    UnlinkedEvidenceEntry,
    assert_no_forbidden_fields,
    canonical_evidence_refs,
    derive_observation_id,
    forbidden_field_names,
)
from app.services.mock_auto_read_model import (
    ANCESTOR_UNKNOWN_ANOMALY_PREFIX,
    ANCESTOR_UNKNOWNS,
    EVIDENCE_LINEAGE_ABSENT,
    EVIDENCE_SOURCE_BINDINGS,
    J3A_REVIEW_B_COMPANION,
    J7_PREDECESSORS,
    J7_SOURCE_BINDING_MANIFEST,
    J7_SOURCE_BINDING_MANIFEST_SHA256,
    JOURNAL_LOCATOR_ABSENT,
    JOURNAL_PATH_ESCAPES_ROOT,
    JOURNAL_PATH_IS_SYMLINK,
    JOURNAL_ROOT_UNSET,
    JOURNAL_ROW_CORRUPT,
    KIWOOM_MANIFEST_JOURNAL_SEGMENT,
    KIWOOM_REPO_WRITER_JOURNAL_SEGMENT,
    LANE_SOURCE_IDS,
    LANE_STRUCTURAL_NO_EVIDENCE_REASON,
    MANIFEST_LOCATOR_SEGMENT_DIFFERS,
    READER_SYMBOL_NOT_ALLOWLISTED,
    READER_SYMBOL_UNRESOLVED,
    SOURCE_READ_FAILED,
    UNRESOLVED_READER_SYMBOL,
    JournalReadRejected,
    JournalSourcePort,
    RawEvidenceRecord,
    SourceReadResult,
    build_read_model,
    normalize_stage,
    read_jsonl_fail_closed,
    resolve_journal_path,
    resolve_reader_callable,
    resolve_reader_symbol,
    select_by_decision_intent_id,
)
from app.services.mock_lane_registry import (
    CANONICAL_LANE_IDS,
    CANONICAL_LANE_REGISTRY,
)

AS_OF = datetime(2026, 8, 22, 0, 0, tzinfo=UTC)

J7_MODULE_PATHS = (
    Path("app/schemas/mock_auto_read_model.py"),
    Path("app/services/mock_auto_read_model.py"),
    Path("app/routers/mock_auto_read_model.py"),
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _StaticPort:
    def __init__(self, result: SourceReadResult) -> None:
        self._result = result

    async def read(self, *, lane_id: str, source_id: str) -> SourceReadResult:
        del lane_id
        return SourceReadResult(
            source_id=source_id,
            records=self._result.records,
            anomaly_codes=self._result.anomaly_codes,
            unreadable_reason=self._result.unreadable_reason,
        )


def _record(**overrides) -> RawEvidenceRecord:
    base = {
        "source_id": "kis_mock_ledger",
        "evidence_class": EvidenceClass.DB_LEDGER,
        "native_key": "kis_mock_ledger:1",
        "as_of": AS_OF,
        "native_status": "accepted",
        "venue_basis": "kis_mock_ledger",
        "observed_at": AS_OF,
        "decision_intent_id": "intent-1",
        "execution_plan_id": "plan-1",
        "order_attempt_id": "attempt-1",
        "cycle_id": "cycle-1",
        "idempotency_key": "idem-1",
        "broker_ack": True,
    }
    base.update(overrides)
    return RawEvidenceRecord(**base)  # type: ignore[arg-type]


def _ref(**overrides) -> EvidenceRef:
    base = {
        "evidence_class": EvidenceClass.DB_LEDGER,
        "source_id": "kis_mock_ledger",
        "native_key": "kis_mock_ledger:1",
        "as_of": AS_OF,
    }
    base.update(overrides)
    return EvidenceRef(**base)  # type: ignore[arg-type]


def _observation(**overrides) -> LifecycleObservationRow:
    refs = overrides.pop("evidence_refs", canonical_evidence_refs([_ref()]))
    base = {
        "lane_id": "kr.kis.mock",
        "decision_intent_id": "intent-1",
        "execution_plan_id": "plan-1",
        "order_attempt_id": "attempt-1",
        "cycle_id": "cycle-1",
        "idempotency_key": "idem-1",
        "stage": LifecycleStage.ACKED,
        "evidence_refs": refs,
        "synthetic": False,
        "quote_currency": "KRW",
        "venue_basis": "kis_mock_ledger",
        "native_status": "accepted",
        "partial_fill": False,
        "filled_quantity": None,
        "remaining_quantity": None,
        "anomaly_codes": (),
        "on_hold": False,
        "hold_reason_codes": (),
        "evidence_tier": EvidenceTier.FACT,
        "observed_at": AS_OF,
    }
    base.update(overrides)
    base["observation_id"] = overrides.get(
        "observation_id",
        derive_observation_id(
            lane_id=base["lane_id"],
            decision_intent_id=base["decision_intent_id"],
            execution_plan_id=base["execution_plan_id"],
            order_attempt_id=base["order_attempt_id"],
            cycle_id=base["cycle_id"],
            idempotency_key=base["idempotency_key"],
            stage=base["stage"],
            evidence_refs=base["evidence_refs"],
        ),
    )
    return LifecycleObservationRow(**base)  # type: ignore[arg-type]


def _coverage(**overrides) -> LaneCoverageRow:
    base = {
        "lane_id": "kr.kis.mock",
        "lane_status": "OBSERVATION_TEMPORARY",
        "activation_status": "BLOCKED",
        "role": "AUTO_MIRROR",
        "role_pending_reason": None,
        "scheduler_owner": None,
        "writer": False,
        "auto_order_enabled": False,
        "quote_currency": "KRW",
        "synthetic": False,
        "source_ids": ("kis_mock_ledger",),
        "evidence_classes": (EvidenceClass.DB_LEDGER,),
        "observed_evidence_classes": (EvidenceClass.DB_LEDGER,),
        "lifecycle_observation_count": 1,
        "unlinked_evidence_count": 0,
        "source_anomaly_codes": (),
        "no_evidence_reason": "",
        "evidence_tier": EvidenceTier.FACT,
        "as_of": AS_OF,
    }
    base.update(overrides)
    return LaneCoverageRow(**base)  # type: ignore[arg-type]


def _response(**overrides) -> MockAutoReadModelResponse:
    base = {
        "as_of": AS_OF,
        "manifest": ManifestRef(
            path=J7_SOURCE_BINDING_MANIFEST, sha256=J7_SOURCE_BINDING_MANIFEST_SHA256
        ),
        "notes": ReadModelNotes(
            role_semantics="role is the registry purpose value only",
            scheduler_owner_absent_meaning="None (owner absent)",
            lineage_requirement="three lineage ids preserved separately",
            aggregation_boundary="never summed across synthetic or currency",
        ),
        "source_bindings": EVIDENCE_SOURCE_BINDINGS,
        "predecessors": J7_PREDECESSORS,
        "ancestor_unknowns": ANCESTOR_UNKNOWNS,
        "coverage_rows": (_coverage(),),
        "lifecycle_rows": (_observation(),),
        "anomalies": (),
        "anomaly_counts": {},
        "holds": (),
        "hold_counts": {},
        "unlinked_evidence": (),
        "unlinked_evidence_counts": {},
    }
    base.update(overrides)
    return MockAutoReadModelResponse(**base)  # type: ignore[arg-type]


def _reject_code(excinfo: pytest.ExceptionInfo) -> str:
    return str(excinfo.value)


def _j7_module_trees() -> dict[Path, ast.Module]:
    return {
        path: ast.parse(path.read_text(encoding="utf-8")) for path in J7_MODULE_PATHS
    }


# ---------------------------------------------------------------------------
# Binding chain — the stamped manifest and predecessor array
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_predecessor_array_is_ordered_unique_and_exactly_eleven():
    jobs = [record.job for record in J7_PREDECESSORS]
    assert jobs == [
        "J2A",
        "J2B",
        "J3A",
        "J3B",
        "J3C",
        "J5A",
        "J5B",
        "J5C",
        "J6A",
        "J6B",
        "J6C",
    ]
    assert len(set(jobs)) == len(jobs)
    merge_shas = [record.merge_sha for record in J7_PREDECESSORS]
    assert len(set(merge_shas)) == len(merge_shas)


@pytest.mark.unit
def test_every_source_binding_predecessor_matches_exactly_one_array_element():
    members = {
        (
            record.job,
            record.merge_sha,
            record.verifier_report_path,
            record.verifier_report_sha256,
        )
        for record in J7_PREDECESSORS
    }
    for binding in EVIDENCE_SOURCE_BINDINGS:
        key = (
            binding.predecessor_job,
            binding.predecessor_merge_sha,
            binding.predecessor_verifier_report_path,
            binding.predecessor_verifier_report_sha256,
        )
        assert key in members, binding.source_id


@pytest.mark.unit
def test_j3a_review_b_verdict_is_carried_not_erased():
    assert J3A_REVIEW_B_COMPANION["path"].endswith("identity19-review-b-20260816.md")
    assert len(J3A_REVIEW_B_COMPANION["sha256"]) == 64
    assert J3A_REVIEW_B_COMPANION["code"] == "j3a_verifier_report_pair_second_member"


@pytest.mark.unit
def test_source_ids_are_globally_unique():
    ids = [binding.source_id for binding in EVIDENCE_SOURCE_BINDINGS]
    assert len(set(ids)) == len(ids)


@pytest.mark.unit
def test_lane_source_map_covers_every_canonical_lane_with_bound_ids():
    assert set(LANE_SOURCE_IDS) == set(CANONICAL_LANE_IDS)
    known = {binding.source_id for binding in EVIDENCE_SOURCE_BINDINGS}
    for lane_id, source_ids in LANE_SOURCE_IDS.items():
        assert set(source_ids) <= known, lane_id
        assert len(set(source_ids)) == len(source_ids), lane_id


@pytest.mark.unit
def test_stamped_manifest_hash_matches_when_the_artifact_is_present():
    path = Path(J7_SOURCE_BINDING_MANIFEST).expanduser()
    if not path.is_file():
        pytest.skip(f"stamped manifest not present in this environment: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == J7_SOURCE_BINDING_MANIFEST_SHA256


@pytest.mark.unit
def test_predecessor_verifier_reports_match_recorded_hashes_when_present():
    checked = 0
    for record in J7_PREDECESSORS:
        path = Path(record.verifier_report_path).expanduser()
        if not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == record.verifier_report_sha256, record.job
        checked += 1
    if checked == 0:
        pytest.skip("no predecessor verifier reports present in this environment")


@pytest.mark.unit
def test_ancestor_unknowns_are_surfaced_not_swallowed():
    jobs = {entry.job for entry in ANCESTOR_UNKNOWNS}
    assert jobs == {"J3A", "J6C"}
    for entry in ANCESTOR_UNKNOWNS:
        assert entry.axis == "C5"
        assert isinstance(entry, AncestorUnknown)


# ---------------------------------------------------------------------------
# §B signed table — consumed unchanged
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coverage_rows_reproduce_the_signed_registry_values():
    response = await build_read_model(as_of=AS_OF)
    by_lane = {row.lane_id: row for row in response.coverage_rows}
    assert list(by_lane) == list(CANONICAL_LANE_IDS)
    for entry in CANONICAL_LANE_REGISTRY:
        row = by_lane[entry.lane_id]
        assert row.lane_status == entry.lane_status.value
        assert row.activation_status == entry.activation_status.value
        assert row.role == (entry.role.value if entry.role is not None else None)
        assert row.role_pending_reason == entry.role_pending_reason
        assert row.writer is False
        assert row.auto_order_enabled is False
        assert row.quote_currency == entry.quote_currency


@pytest.mark.unit
@pytest.mark.asyncio
async def test_absent_scheduler_owner_is_null_and_never_rewritten_as_disabled():
    response = await build_read_model(as_of=AS_OF)
    payload = json.loads(response.model_dump_json())
    absent = {
        row["lane_id"]
        for row in payload["coverage_rows"]
        if row["scheduler_owner"] is None
    }
    disabled = {
        row["lane_id"]
        for row in payload["coverage_rows"]
        if row["scheduler_owner"] == "disabled"
    }
    assert disabled == {
        "crypto.binance.spot_demo.canonical",
        "crypto.binance.spot_demo.b0x_sidecar",
        "crypto.binance.futures_demo",
    }
    assert len(absent) == 9
    assert absent & disabled == set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_role_is_reported_as_purpose_only_semantics():
    response = await build_read_model(as_of=AS_OF)
    assert "purpose" in response.notes.role_semantics
    assert response.notes.read_only is True


# ---------------------------------------------------------------------------
# Mutant 1 / 2 — lane_id is the primary axis; twelve rows always
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exactly_twelve_coverage_rows_even_with_no_evidence():
    response = await build_read_model(as_of=AS_OF)
    assert len(response.coverage_rows) == 12
    assert [row.lane_id for row in response.coverage_rows] == list(CANONICAL_LANE_IDS)


@pytest.mark.unit
def test_legacy_account_mode_would_collapse_lanes_so_it_cannot_be_the_key():
    modes = {entry.account_mode.value for entry in CANONICAL_LANE_REGISTRY}
    assert len(modes) < 12
    assert len({entry.lane_id for entry in CANONICAL_LANE_REGISTRY}) == 12


@pytest.mark.unit
def test_coverage_response_rejects_a_missing_lane_row():
    with pytest.raises(ValidationError) as excinfo:
        _response(
            coverage_rows=(_coverage(), _coverage()),
        )
    assert ReadModelReject.COVERAGE_DUPLICATE_LANE.value in _reject_code(excinfo)


# ---------------------------------------------------------------------------
# Mutant 3 / 14 (recheck) — no evidence means no lifecycle row, and no
# configured-but-empty false pass
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unlinked_native_evidence_never_becomes_a_lifecycle_row():
    port = _StaticPort(
        SourceReadResult(
            source_id="kis_mock_ledger",
            records=(_record(decision_intent_id=None),),
        )
    )
    response = await build_read_model(ports={"kis_mock_ledger": port}, as_of=AS_OF)
    row = next(r for r in response.coverage_rows if r.lane_id == "kr.kis.mock")
    assert row.lifecycle_observation_count == 0
    assert row.unlinked_evidence_count == 1
    # The bound source is still bound, so evidence_classes keeps its binding
    # class and no_evidence_reason stays blank; the zero shows up in the
    # observation count, the unlinked count and the observed class set.
    assert row.evidence_classes == (EvidenceClass.DB_LEDGER,)
    assert row.observed_evidence_classes == ()
    assert row.no_evidence_reason == ""
    assert any(
        entry.reason == EVIDENCE_LINEAGE_ABSENT for entry in response.unlinked_evidence
    )
    assert response.unlinked_evidence_counts == {"kr.kis.mock": 1}


@pytest.mark.unit
def test_configured_source_with_zero_rows_and_no_anomaly_is_rejected():
    with pytest.raises(ValidationError) as excinfo:
        _coverage(
            lifecycle_observation_count=0,
            unlinked_evidence_count=0,
            source_anomaly_codes=(),
            no_evidence_reason="",
            observed_evidence_classes=(),
        )
    assert (
        ReadModelReject.COVERAGE_CONFIGURED_BUT_EMPTY_FALSE_PASS.value
        in _reject_code(excinfo)
    )


@pytest.mark.unit
def test_no_evidence_reason_is_bound_to_source_absence_in_both_directions():
    """`source_ids == [] <=> no_evidence_reason non-blank` — both directions."""

    # bound source + a reason: the lane HAS a source, so a reason is a lie.
    with pytest.raises(ValidationError) as excinfo:
        _coverage(no_evidence_reason="still blocked")
    assert ReadModelReject.COVERAGE_NO_EVIDENCE_REASON_MISMATCH.value in _reject_code(
        excinfo
    )
    # no source + no reason: the absence would be unexplained.
    with pytest.raises(ValidationError) as excinfo:
        _coverage(
            source_ids=(),
            evidence_classes=(),
            observed_evidence_classes=(),
            lifecycle_observation_count=0,
            no_evidence_reason="",
        )
    assert ReadModelReject.COVERAGE_NO_EVIDENCE_REASON_MISMATCH.value in _reject_code(
        excinfo
    )


@pytest.mark.unit
def test_a_bound_source_that_observed_nothing_carries_a_blank_reason():
    """Zero observations is not source absence; it never fills the reason."""

    row = _coverage(
        lifecycle_observation_count=0,
        observed_evidence_classes=(),
        unlinked_evidence_count=2,
        no_evidence_reason="",
    )
    assert row.source_ids == ("kis_mock_ledger",)
    assert row.evidence_classes == (EvidenceClass.DB_LEDGER,)
    assert row.no_evidence_reason == ""


@pytest.mark.unit
def test_a_lane_without_sources_states_its_reason():
    row = _coverage(
        source_ids=(),
        evidence_classes=(),
        observed_evidence_classes=(),
        lifecycle_observation_count=0,
        no_evidence_reason="shadow-only lane has no persisted lifecycle surface",
    )
    assert row.no_evidence_reason


@pytest.mark.unit
@pytest.mark.asyncio
async def test_only_source_absent_lanes_carry_a_no_evidence_reason():
    response = await build_read_model(as_of=AS_OF)
    with_reason = {
        row.lane_id for row in response.coverage_rows if row.no_evidence_reason.strip()
    }
    without_sources = {
        row.lane_id for row in response.coverage_rows if not row.source_ids
    }
    assert with_reason == without_sources
    assert with_reason == set(LANE_STRUCTURAL_NO_EVIDENCE_REASON)
    assert len(with_reason) == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_configured_but_unread_source_reports_an_anomaly_not_silence():
    response = await build_read_model(as_of=AS_OF)
    row = next(r for r in response.coverage_rows if r.lane_id == "kr.kis.mock")
    assert row.source_ids == ("kis_mock_ledger",)
    assert any(code.startswith(SOURCE_READ_FAILED) for code in row.source_anomaly_codes)
    assert response.anomaly_counts


# ---------------------------------------------------------------------------
# Mutant 4 / 15 (recheck) — evidence class axes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_evidence_classes_must_equal_the_bound_binding_classes():
    """The public axis is the referenced bindings' sorted-unique class set."""

    with pytest.raises(ValidationError) as excinfo:
        _response(
            coverage_rows=(
                _coverage(
                    evidence_classes=(
                        EvidenceClass.DB_LEDGER,
                        EvidenceClass.FILE_JOURNAL,
                    )
                ),
            )
        )
    assert ReadModelReject.COVERAGE_EVIDENCE_CLASS_MISMATCH.value in _reject_code(
        excinfo
    )
    with pytest.raises(ValidationError) as excinfo:
        _response(coverage_rows=(_coverage(evidence_classes=()),))
    assert ReadModelReject.COVERAGE_EVIDENCE_CLASS_MISMATCH.value in _reject_code(
        excinfo
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_evidence_classes_equal_the_bound_binding_classes_for_all_twelve():
    response = await build_read_model(as_of=AS_OF)
    bindings = {b.source_id: b.evidence_class for b in response.source_bindings}
    for row in response.coverage_rows:
        expected = tuple(
            sorted(
                {bindings[source_id] for source_id in row.source_ids},
                key=lambda item: item.value,
            )
        )
        assert row.evidence_classes == expected, row.lane_id


@pytest.mark.unit
def test_multi_source_evidence_refs_are_all_preserved():
    refs = canonical_evidence_refs(
        [
            _ref(),
            _ref(
                evidence_class=EvidenceClass.FILE_JOURNAL,
                source_id="kiwoom_kr_own_orders",
                native_key="kiwoom_kr_own_orders:9",
            ),
        ]
    )
    row = _observation(evidence_refs=refs)
    assert len(row.evidence_refs) == 2
    assert {ref.evidence_class for ref in row.evidence_refs} == {
        EvidenceClass.DB_LEDGER,
        EvidenceClass.FILE_JOURNAL,
    }


@pytest.mark.unit
def test_observed_classes_must_equal_the_lifecycle_evidence_ref_union():
    with pytest.raises(ValidationError) as excinfo:
        _response(
            coverage_rows=(
                _coverage(observed_evidence_classes=(EvidenceClass.FILE_JOURNAL,)),
            )
        )
    assert ReadModelReject.COVERAGE_EVIDENCE_CLASS_MISMATCH.value in _reject_code(
        excinfo
    )
    with pytest.raises(ValidationError) as excinfo:
        _response(coverage_rows=(_coverage(observed_evidence_classes=()),))
    assert ReadModelReject.COVERAGE_EVIDENCE_CLASS_MISMATCH.value in _reject_code(
        excinfo
    )


# ---------------------------------------------------------------------------
# Mutant 5 / 6 — stage normalizer
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ack_evidence_never_becomes_filled_or_reconciled():
    assert normalize_stage(_record(broker_ack=True)) is LifecycleStage.ACKED
    assert (
        normalize_stage(_record(broker_ack=True, native_status="filled"))
        is LifecycleStage.ACKED
    )


@pytest.mark.unit
def test_plan_without_broker_evidence_is_planned():
    assert (
        normalize_stage(_record(broker_ack=False, native_status="planned"))
        is LifecycleStage.PLANNED
    )


@pytest.mark.unit
def test_fill_requires_positive_filled_quantity():
    with pytest.raises(JournalReadRejected):
        normalize_stage(_record(fill_evidence=True, filled_quantity=Decimal("0")))
    assert (
        normalize_stage(_record(fill_evidence=True, filled_quantity=Decimal("3")))
        is LifecycleStage.FILLED
    )


@pytest.mark.unit
def test_terminal_without_account_convergence_is_not_reconciled():
    assert (
        normalize_stage(
            _record(
                broker_ack=True, terminal_outcome="CANCELED", position_convergence=False
            )
        )
        is LifecycleStage.ACKED
    )
    assert (
        normalize_stage(_record(terminal_outcome="FILLED", position_convergence=True))
        is LifecycleStage.RECONCILED
    )


@pytest.mark.unit
def test_filled_row_requires_quantity_and_fill_evidence():
    with pytest.raises(ValidationError) as excinfo:
        _observation(stage=LifecycleStage.FILLED, filled_quantity=None)
    assert ReadModelReject.FILL_WITHOUT_QUANTITY.value in _reject_code(excinfo)


@pytest.mark.unit
def test_partial_fill_may_not_be_collapsed_into_a_full_fill():
    with pytest.raises(ValidationError) as excinfo:
        _observation(
            stage=LifecycleStage.FILLED,
            filled_quantity=Decimal("1"),
            remaining_quantity=Decimal("2"),
            partial_fill=False,
        )
    assert ReadModelReject.PARTIAL_FILL_COLLAPSED.value in _reject_code(excinfo)


@pytest.mark.unit
def test_reconciled_requires_terminal_outcome_and_convergence_evidence():
    with pytest.raises(ValidationError) as excinfo:
        _observation(stage=LifecycleStage.RECONCILED)
    assert ReadModelReject.RECONCILE_WITHOUT_CONVERGENCE.value in _reject_code(excinfo)
    with pytest.raises(ValidationError) as excinfo:
        _observation(
            stage=LifecycleStage.RECONCILED,
            convergence_evidence_refs=canonical_evidence_refs([_ref()]),
            native_terminal_outcome=None,
        )
    assert ReadModelReject.RECONCILE_WITHOUT_TERMINAL_OUTCOME.value in _reject_code(
        excinfo
    )


# ---------------------------------------------------------------------------
# Mutant 7 / 19 (recheck) — anomalies and holds never hide
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_anomaly_list_and_counts_must_agree_in_both_directions():
    anomaly = AnomalyEntry(
        lane_id="kr.kis.mock", source_id="kis_mock_ledger", code="x", detail="d"
    )
    with pytest.raises(ValidationError) as excinfo:
        _response(anomalies=(anomaly,), anomaly_counts={})
    assert ReadModelReject.ANOMALY_COUNT_MISMATCH.value in _reject_code(excinfo)
    with pytest.raises(ValidationError) as excinfo:
        _response(anomalies=(), anomaly_counts={"x": 1})
    assert ReadModelReject.ANOMALY_COUNT_MISMATCH.value in _reject_code(excinfo)


@pytest.mark.unit
def test_hold_list_and_counts_must_agree():
    row = _observation(on_hold=True, hold_reason_codes=("unknown_pending_reconcile",))
    hold = HoldEntry(
        lane_id=row.lane_id,
        observation_id=row.observation_id,
        hold_reason_codes=row.hold_reason_codes,
    )
    with pytest.raises(ValidationError) as excinfo:
        _response(lifecycle_rows=(row,), holds=(hold,), hold_counts={})
    assert ReadModelReject.HOLD_COUNT_MISMATCH.value in _reject_code(excinfo)
    ok = _response(
        lifecycle_rows=(row,),
        holds=(hold,),
        hold_counts={"unknown_pending_reconcile": 1},
    )
    assert ok.hold_counts == {"unknown_pending_reconcile": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_holds_appear_in_both_the_list_and_the_counts():
    port = _StaticPort(
        SourceReadResult(
            source_id="kis_mock_ledger",
            records=(
                _record(on_hold=True, hold_reason_codes=("unknown_pending_reconcile",)),
            ),
        )
    )
    response = await build_read_model(ports={"kis_mock_ledger": port}, as_of=AS_OF)
    assert len(response.holds) == 1
    assert response.hold_counts == {"unknown_pending_reconcile": 1}


@pytest.mark.unit
def test_unlinked_list_and_counts_must_agree():
    entry = UnlinkedEvidenceEntry(
        lane_id="kr.kis.mock",
        source_id="kis_mock_ledger",
        evidence_class=EvidenceClass.DB_LEDGER,
        native_key="k",
        reason=EVIDENCE_LINEAGE_ABSENT,
    )
    with pytest.raises(ValidationError) as excinfo:
        _response(
            coverage_rows=(_coverage(unlinked_evidence_count=1),),
            unlinked_evidence=(entry,),
            unlinked_evidence_counts={},
        )
    assert ReadModelReject.UNLINKED_COUNT_MISMATCH.value in _reject_code(excinfo)


# ---------------------------------------------------------------------------
# Mutant 8 — synthetic and currency never mix
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_row_in_a_different_currency_than_its_lane_is_rejected():
    with pytest.raises(ValidationError) as excinfo:
        _response(lifecycle_rows=(_observation(quote_currency="USD"),))
    assert ReadModelReject.CURRENCY_MIXING_FORBIDDEN.value in _reject_code(excinfo)


@pytest.mark.unit
def test_a_synthetic_row_in_a_native_lane_is_rejected():
    with pytest.raises(ValidationError) as excinfo:
        _response(lifecycle_rows=(_observation(synthetic=True),))
    assert ReadModelReject.SYNTHETIC_MIXING_FORBIDDEN.value in _reject_code(excinfo)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upbit_shadow_is_the_only_synthetic_lane():
    response = await build_read_model(as_of=AS_OF)
    synthetic = {row.lane_id for row in response.coverage_rows if row.synthetic}
    assert synthetic == {"crypto.upbit.shadow"}


# ---------------------------------------------------------------------------
# Mutant 9 — no FX, parity, profitability or strategy score fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_response_model_exposes_a_forbidden_field():
    assert_no_forbidden_fields(
        [
            EvidenceRef,
            EvidenceSourceBinding,
            PredecessorRecord,
            AncestorUnknown,
            AnomalyEntry,
            HoldEntry,
            UnlinkedEvidenceEntry,
            LaneCoverageRow,
            LifecycleObservationRow,
            ManifestRef,
            ReadModelNotes,
            MockAutoReadModelResponse,
        ]
    )


@pytest.mark.unit
def test_forbidden_field_detector_actually_fires():
    class _Probe(BaseModel):
        fx_rate: float
        realized_profit: float

    assert forbidden_field_names(_Probe) == ("fx_rate", "realized_profit")


# ---------------------------------------------------------------------------
# Mutant 10 — no runtime write, no broker network, no mutation adapter import
# ---------------------------------------------------------------------------


_FORBIDDEN_IMPORT_PREFIXES = (
    "httpx",
    "requests",
    "aiohttp",
    "websockets",
    "taskiq",
    "prefect",
    "app.services.brokers",
    "app.tasks",
    "app.flows",
    "app.jobs",
    "app.services.kis_mock_lifecycle_service",
    "app.services.alpaca_paper_ledger_service",
    "app.mcp_server",
    "scripts.b0x",
    "app.models.fill_observation",
    "app.services.fill_observation",
)

_FORBIDDEN_CALL_ATTRS = frozenset(
    {"commit", "flush", "add_all", "write_text", "write_bytes", "unlink", "mkdir"}
)


@pytest.mark.unit
def test_j7_modules_have_no_forbidden_static_import():
    for path, tree in _j7_module_trees().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                for prefix in _FORBIDDEN_IMPORT_PREFIXES:
                    assert not (name == prefix or name.startswith(prefix + ".")), (
                        f"{path}: forbidden import {name}"
                    )


@pytest.mark.unit
def test_j7_modules_never_call_a_write_method():
    for path, tree in _j7_module_trees().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in _FORBIDDEN_CALL_ATTRS, (
                    f"{path}: forbidden call .{node.func.attr}()"
                )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "open":
                    modes = [
                        arg.value
                        for arg in node.args[1:]
                        if isinstance(arg, ast.Constant)
                    ]
                    assert all(
                        "w" not in str(mode) and "a" not in str(mode) for mode in modes
                    ), f"{path}: forbidden open() mode"


@pytest.mark.unit
def test_j7_router_declares_no_mutating_http_verb():
    tree = ast.parse(
        Path("app/routers/mock_auto_read_model.py").read_text(encoding="utf-8")
    )
    verbs = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "router"
    }
    assert verbs == {"get"}


# ---------------------------------------------------------------------------
# Mutant 11 — journal traversal, symlink escape, corrupt rows fail closed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_journal_root_unset_is_its_own_reason():
    with pytest.raises(JournalReadRejected) as excinfo:
        resolve_journal_path(root=None, lane_segment="a", filename="b.jsonl")
    assert excinfo.value.code == JOURNAL_ROOT_UNSET


@pytest.mark.unit
@pytest.mark.parametrize("segment", ["../escape", "a/b", "..", ""])
def test_journal_path_traversal_is_rejected(tmp_path: Path, segment: str):
    with pytest.raises(JournalReadRejected) as excinfo:
        resolve_journal_path(
            root=tmp_path, lane_segment=segment, filename="own-orders.jsonl"
        )
    assert excinfo.value.code == JOURNAL_PATH_ESCAPES_ROOT


@pytest.mark.unit
def test_journal_symlink_escape_is_rejected(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "own-orders.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    root = tmp_path / "root"
    (root / KIWOOM_MANIFEST_JOURNAL_SEGMENT).mkdir(parents=True)
    link = root / KIWOOM_MANIFEST_JOURNAL_SEGMENT / "own-orders.jsonl"
    link.symlink_to(target)
    with pytest.raises(JournalReadRejected) as excinfo:
        resolve_journal_path(
            root=root,
            lane_segment=KIWOOM_MANIFEST_JOURNAL_SEGMENT,
            filename="own-orders.jsonl",
        )
    assert excinfo.value.code == JOURNAL_PATH_IS_SYMLINK


@pytest.mark.unit
def test_absent_journal_is_reported_not_read_as_empty(tmp_path: Path):
    (tmp_path / KIWOOM_MANIFEST_JOURNAL_SEGMENT).mkdir()
    with pytest.raises(JournalReadRejected) as excinfo:
        resolve_journal_path(
            root=tmp_path,
            lane_segment=KIWOOM_MANIFEST_JOURNAL_SEGMENT,
            filename="own-orders.jsonl",
        )
    assert excinfo.value.code == JOURNAL_LOCATOR_ABSENT


@pytest.mark.unit
def test_corrupt_journal_row_is_never_skipped(tmp_path: Path):
    path = tmp_path / "ordering-events.jsonl"
    path.write_text('{"at": "x"}\nnot-json\n', encoding="utf-8")
    with pytest.raises(JournalReadRejected) as excinfo:
        read_jsonl_fail_closed(path)
    assert excinfo.value.code == JOURNAL_ROW_CORRUPT


@pytest.mark.unit
def test_non_object_journal_row_is_corrupt(tmp_path: Path):
    path = tmp_path / "ordering-events.jsonl"
    path.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(JournalReadRejected) as excinfo:
        read_jsonl_fail_closed(path)
    assert excinfo.value.code == JOURNAL_ROW_CORRUPT


@pytest.mark.unit
@pytest.mark.asyncio
async def test_journal_port_reports_absence_as_an_anomaly(tmp_path: Path):
    port = JournalSourcePort(
        source_id="kiwoom_kr_own_orders", filename="own-orders.jsonl", root=tmp_path
    )
    result = await port.read(lane_id="kr.kiwoom.mock", source_id="kiwoom_kr_own_orders")
    assert result.records == ()
    assert result.unreadable_reason == JOURNAL_LOCATOR_ABSENT
    assert MANIFEST_LOCATOR_SEGMENT_DIFFERS in result.anomaly_codes


@pytest.mark.unit
def test_manifest_journal_segment_differs_from_the_repo_writer_segment():
    from scripts.b0x.scope import KIWOOM_MOCK_SCOPE_KEY

    assert KIWOOM_REPO_WRITER_JOURNAL_SEGMENT == KIWOOM_MOCK_SCOPE_KEY
    assert KIWOOM_MANIFEST_JOURNAL_SEGMENT == "kr.kiwoom.mock"
    assert KIWOOM_MANIFEST_JOURNAL_SEGMENT != KIWOOM_REPO_WRITER_JOURNAL_SEGMENT


# ---------------------------------------------------------------------------
# Mutant 12 — no raw account identifier or secret in the response
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("with_lineage", [True, False])
async def test_account_identifiers_and_secrets_are_not_carried_into_the_response(
    with_lineage: bool,
):
    """Neither a lineage-linked row nor an unlinked record may leak identity."""

    lineage = (
        {
            "decision_intent_id": "intent-1",
            "execution_plan_id": "plan-1",
            "order_attempt_id": "attempt-1",
            "cycle_id": "cycle-1",
            "idempotency_key": "idem-1",
        }
        if with_lineage
        else {}
    )
    row = SimpleNamespace(
        id=7,
        order_no="ORD-7",
        lifecycle_state="accepted",
        account_no="12345678-01",
        account_number="12345678-01",
        app_key="SECRET-APP-KEY",
        app_secret="SECRET-VALUE",
        created_at=AS_OF,
        **lineage,
    )
    from app.services.mock_auto_read_model import _record_from_ledger_row

    record = _record_from_ledger_row(
        row=row,
        source_id="kis_mock_ledger",
        evidence_class=EvidenceClass.DB_LEDGER,
        fallback_time=AS_OF,
    )
    assert record.has_lineage is with_lineage
    port = _StaticPort(SourceReadResult(source_id="kis_mock_ledger", records=(record,)))
    response = await build_read_model(ports={"kis_mock_ledger": port}, as_of=AS_OF)
    coverage = next(r for r in response.coverage_rows if r.lane_id == "kr.kis.mock")
    assert coverage.lifecycle_observation_count == (1 if with_lineage else 0)
    payload = response.model_dump_json()
    for secret in ("12345678-01", "SECRET-APP-KEY", "SECRET-VALUE"):
        assert secret not in payload, secret


@pytest.mark.unit
def test_every_binding_declares_a_redaction_contract():
    for binding in EVIDENCE_SOURCE_BINDINGS:
        assert "secret" in binding.redaction_contract
        assert binding.read_scope_note.strip()


# ---------------------------------------------------------------------------
# Mutant 13 — no dependency on the open PR #1751 surface
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_open_pr_1751_surface_is_absent_and_unreferenced():
    assert not Path("app/models/fill_observation.py").exists()
    assert not Path("app/services/fill_observation").exists()
    for path in J7_MODULE_PATHS:
        text = path.read_text(encoding="utf-8")
        assert "fill_observation" not in text, path


# ---------------------------------------------------------------------------
# Mutant 14 / 15 (brief §G) — lineage identity is never collapsed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_three_lineage_ids_may_not_share_one_alias():
    with pytest.raises(ValidationError) as excinfo:
        _observation(execution_plan_id="intent-1")
    assert ReadModelReject.LINEAGE_ALIAS_COLLAPSE.value in _reject_code(excinfo)


@pytest.mark.unit
def test_acked_and_later_stages_require_plan_and_attempt_ids():
    for stage in (
        LifecycleStage.ACKED,
        LifecycleStage.FILLED,
        LifecycleStage.RECONCILED,
    ):
        with pytest.raises(ValidationError) as excinfo:
            _observation(stage=stage, execution_plan_id=None, order_attempt_id=None)
        assert ReadModelReject.LINEAGE_MISSING_FOR_STAGE.value in _reject_code(excinfo)


@pytest.mark.unit
def test_planned_may_exist_before_a_plan_or_attempt():
    row = _observation(
        stage=LifecycleStage.PLANNED,
        execution_plan_id=None,
        order_attempt_id=None,
        cycle_id=None,
        idempotency_key=None,
    )
    assert row.stage is LifecycleStage.PLANNED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repeated_partial_fills_stay_distinct_observations():
    first = _record(
        native_key="kis_mock_ledger:fill-1",
        broker_ack=False,
        fill_evidence=True,
        filled_quantity=Decimal("1"),
        remaining_quantity=Decimal("2"),
    )
    second = _record(
        native_key="kis_mock_ledger:fill-2",
        broker_ack=False,
        fill_evidence=True,
        filled_quantity=Decimal("2"),
        remaining_quantity=Decimal("1"),
    )
    port = _StaticPort(
        SourceReadResult(source_id="kis_mock_ledger", records=(first, second))
    )
    response = await build_read_model(ports={"kis_mock_ledger": port}, as_of=AS_OF)
    rows = [r for r in response.lifecycle_rows if r.lane_id == "kr.kis.mock"]
    assert len(rows) == 2
    assert len({row.observation_id for row in rows}) == 2
    assert all(row.partial_fill for row in rows)
    assert [row.filled_quantity for row in rows] == [Decimal("1"), Decimal("2")]


@pytest.mark.unit
def test_observation_id_is_derived_and_a_sentinel_is_rejected():
    with pytest.raises(ValidationError) as excinfo:
        _observation(observation_id="caller-picked")
    assert ReadModelReject.OBSERVATION_ID_NOT_DERIVED.value in _reject_code(excinfo)


@pytest.mark.unit
def test_observation_id_is_stable_across_calls():
    kwargs = {
        "lane_id": "kr.kis.mock",
        "decision_intent_id": "intent-1",
        "execution_plan_id": "plan-1",
        "order_attempt_id": "attempt-1",
        "cycle_id": "cycle-1",
        "idempotency_key": "idem-1",
        "stage": LifecycleStage.ACKED,
        "evidence_refs": canonical_evidence_refs([_ref()]),
    }
    assert derive_observation_id(**kwargs) == derive_observation_id(**kwargs)
    other = dict(kwargs, stage=LifecycleStage.PLANNED)
    assert derive_observation_id(**kwargs) != derive_observation_id(**other)


# ---------------------------------------------------------------------------
# Mutant 20 (recheck) — duplicate evidence refs may not inflate anything
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_duplicate_evidence_refs_are_rejected():
    with pytest.raises(ValueError) as excinfo:
        canonical_evidence_refs([_ref(), _ref()])
    assert ReadModelReject.EVIDENCE_REFS_DUPLICATE.value in str(excinfo.value)


@pytest.mark.unit
def test_evidence_refs_must_be_canonically_ordered():
    unordered = (
        _ref(
            evidence_class=EvidenceClass.FILE_JOURNAL,
            source_id="kiwoom_kr_own_orders",
            native_key="z",
        ),
        _ref(),
    )
    with pytest.raises(ValidationError) as excinfo:
        _observation(evidence_refs=unordered)
    assert ReadModelReject.EVIDENCE_REFS_NOT_CANONICAL.value in _reject_code(excinfo)


@pytest.mark.unit
def test_a_lifecycle_row_without_evidence_refs_is_rejected():
    with pytest.raises(ValidationError) as excinfo:
        _observation(evidence_refs=())
    assert ReadModelReject.EVIDENCE_REFS_EMPTY.value in _reject_code(excinfo)


# ---------------------------------------------------------------------------
# Cross-lane fan-out — the acceptance path
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_intent_is_traced_across_lanes_in_one_row_shape():
    kis = _record(native_key="kis_mock_ledger:1")
    alpaca = _record(
        source_id="alpaca_paper_ledger",
        native_key="alpaca_paper_ledger:1",
        venue_basis="alpaca_paper_ledger",
    )
    response = await build_read_model(
        ports={
            "kis_mock_ledger": _StaticPort(
                SourceReadResult(source_id="kis_mock_ledger", records=(kis,))
            ),
            "alpaca_paper_ledger": _StaticPort(
                SourceReadResult(source_id="alpaca_paper_ledger", records=(alpaca,))
            ),
        },
        as_of=AS_OF,
    )
    rows = select_by_decision_intent_id(response, "intent-1")
    lanes = {row.lane_id for row in rows}
    assert "kr.kis.mock" in lanes
    assert "us.alpaca.paper.default" in lanes
    assert len({type(row) for row in rows}) == 1
    assert all(row.decision_intent_id == "intent-1" for row in rows)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fan_out_on_an_unknown_intent_returns_nothing_not_a_sentinel():
    response = await build_read_model(as_of=AS_OF)
    assert select_by_decision_intent_id(response, "no-such-intent") == ()


# ---------------------------------------------------------------------------
# Reader symbol allowlist
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unresolved_reader_symbol_fails_closed():
    with pytest.raises(JournalReadRejected) as excinfo:
        resolve_reader_symbol(UNRESOLVED_READER_SYMBOL)
    assert excinfo.value.code == READER_SYMBOL_UNRESOLVED


@pytest.mark.unit
def test_a_symbol_outside_the_allowlist_is_refused():
    with pytest.raises(JournalReadRejected) as excinfo:
        resolve_reader_symbol("os.system")
    assert excinfo.value.code == READER_SYMBOL_NOT_ALLOWLISTED
    with pytest.raises(JournalReadRejected) as excinfo:
        resolve_reader_callable(
            "app.services.kis_mock_lifecycle_service.KISMockLifecycleService.update_order_terms"
        )
    assert excinfo.value.code == READER_SYMBOL_NOT_ALLOWLISTED


@pytest.mark.unit
def test_every_allowlisted_reader_symbol_resolves():
    for binding in EVIDENCE_SOURCE_BINDINGS:
        if binding.read_only_reader_symbol == UNRESOLVED_READER_SYMBOL:
            continue
        owner, attribute = resolve_reader_callable(binding.read_only_reader_symbol)
        assert hasattr(owner, attribute), binding.source_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_unresolved_readback_source_yields_an_anomaly_never_a_row():
    response = await build_read_model(as_of=AS_OF)
    row = next(r for r in response.coverage_rows if r.lane_id == "kr.kiwoom.mock")
    assert "kiwoom_kr_native_readback" in row.source_ids
    assert any(
        code.startswith(READER_SYMBOL_UNRESOLVED) for code in row.source_anomaly_codes
    )
    assert row.lifecycle_observation_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ancestor_unknown_codes_reach_every_affected_coverage_row():
    response = await build_read_model(as_of=AS_OF)
    for row in response.coverage_rows:
        assert any(
            code.startswith(ANCESTOR_UNKNOWN_ANOMALY_PREFIX)
            for code in row.source_anomaly_codes
        ), row.lane_id
    for lane_id in ("crypto.upbit.shadow", "crypto.binance.futures_demo"):
        row = next(r for r in response.coverage_rows if r.lane_id == lane_id)
        assert (
            f"{ANCESTOR_UNKNOWN_ANOMALY_PREFIX}:j6c_rob1271" in row.source_anomaly_codes
        )


# ---------------------------------------------------------------------------
# Verifier round 1 — the exact mutants that were ACCEPTED before
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_verifier_r1_source_ids_iff_reason_mutant_is_rejected():
    """The exact in-memory mutant that round 1 accepted must now be red.

    A lane with bound sources, zero observations, a non-empty anomaly set and
    a non-blank reason: the reason claims "no source is bound", which is false.
    """

    with pytest.raises(ValidationError) as excinfo:
        _coverage(
            source_ids=("kis_mock_ledger",),
            evidence_classes=(EvidenceClass.DB_LEDGER,),
            observed_evidence_classes=(),
            lifecycle_observation_count=0,
            unlinked_evidence_count=0,
            source_anomaly_codes=("source_read_failed:kis_mock_ledger",),
            no_evidence_reason="bound sources produced no lifecycle evidence",
        )
    assert ReadModelReject.COVERAGE_NO_EVIDENCE_REASON_MISMATCH.value in _reject_code(
        excinfo
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_verifier_r1_binding_class_mismatch_is_gone():
    """`evidence_classes` is the binding axis for every lane, including the
    eight that round 1 reported as mismatched."""

    response = await build_read_model(as_of=AS_OF)
    bindings = {b.source_id: b.evidence_class.value for b in response.source_bindings}
    mismatched = [
        row.lane_id
        for row in response.coverage_rows
        if [item.value for item in row.evidence_classes]
        != sorted({bindings[source_id] for source_id in row.source_ids})
    ]
    assert mismatched == []


# ---------------------------------------------------------------------------
# ROB-285 — this observation module is not a second venue code location
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_j7_app_modules_contain_no_venue_literal():
    """The ROB-285 location audit greps `app/` case-insensitively.

    Lane identity and the crypto demo-ledger source are addressed through
    `mock_lane_registry` (which already owns those literals) instead, so this
    module never becomes a parallel venue code location.
    """

    for path in J7_MODULE_PATHS:
        text = path.read_text(encoding="utf-8").lower()
        assert "binance" not in text, path


@pytest.mark.unit
def test_lane_constants_resolve_to_the_exact_canonical_ids():
    """Pin the values the module derives instead of spelling.

    These literals are the manifest §B rows 1-12 in order; the test lives here
    because `tests/` is outside the ROB-285 audit's `app/` scan.
    """

    from app.services import mock_auto_read_model as service

    assert service._KR_KIS == "kr.kis.mock"
    assert service._KR_KIWOOM == "kr.kiwoom.mock"
    assert service._US_KIS == "us.kis.mock"
    assert service._US_KIWOOM == "us.kiwoom.mock"
    assert service._US_ALPACA_DEFAULT == "us.alpaca.paper.default"
    assert service._US_ALPACA_LAB == "us.alpaca.paper.lab"
    assert service._CRYPTO_SPOT_DEMO_CANONICAL == "crypto.binance.spot_demo.canonical"
    assert service._CRYPTO_SPOT_DEMO_SIDECAR == "crypto.binance.spot_demo.b0x_sidecar"
    assert service._CRYPTO_ALPACA_DEFAULT == "crypto.alpaca.paper.default"
    assert service._CRYPTO_ALPACA_CLEAN == "crypto.alpaca.paper.clean"
    assert service._CRYPTO_UPBIT_SHADOW == "crypto.upbit.shadow"
    assert service._CRYPTO_FUTURES_DEMO == "crypto.binance.futures_demo"


@pytest.mark.unit
def test_derived_crypto_demo_binding_matches_the_manifest_literals():
    """The derived source_id / locator / reader equal the stamped strings."""

    from app.services import mock_auto_read_model as service

    assert service._CRYPTO_DEMO_LEDGER_SOURCE_ID == "binance_demo_ledger"
    assert service._CRYPTO_DEMO_LEDGER_LOCATOR == "binance_demo_order_ledger"
    assert service._CRYPTO_DEMO_LEDGER_READER == (
        "app.mcp_server.tooling.binance_demo_ledger_status_read"
        ".binance_demo_ledger_status"
    )
    binding = next(
        b
        for b in EVIDENCE_SOURCE_BINDINGS
        if b.source_id == service._CRYPTO_DEMO_LEDGER_SOURCE_ID
    )
    assert binding.evidence_class is EvidenceClass.DB_LEDGER
    assert binding.predecessor_job == "J6B"
    assert LANE_SOURCE_IDS["crypto.binance.spot_demo.canonical"] == (
        "binance_demo_ledger",
    )
    assert LANE_SOURCE_IDS["crypto.binance.futures_demo"] == ("binance_demo_ledger",)


@pytest.mark.unit
def test_j6c_owned_lanes_are_the_upbit_and_futures_lanes():
    from app.services.mock_auto_read_model import J6C_OWNED_LANE_IDS

    assert J6C_OWNED_LANE_IDS == frozenset(
        {"crypto.upbit.shadow", "crypto.binance.futures_demo"}
    )
