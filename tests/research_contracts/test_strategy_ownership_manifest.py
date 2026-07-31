"""False-pass tests for the ROB-1189 static ownership manifest."""

from __future__ import annotations

import inspect
import json
from dataclasses import FrozenInstanceError, replace

import pytest

from research_contracts import strategy_ownership_manifest as contract

OPERATOR_SOURCE = contract.SourceLocator(
    contract.AuthorityKind.OPERATOR_ACCEPTED_MAIN,
    "accepted-test://operator",
    "exact physical writer binding",
)
CAPABILITY_SOURCE = contract.SourceLocator(
    contract.AuthorityKind.REPOSITORY_LOCATOR,
    "app/services/example.py",
    "tool/env/ledger capability only",
)
BROKER_SOURCE = contract.SourceLocator(
    contract.AuthorityKind.CURRENT_BROKER_OBSERVATION,
    "broker-observation://test",
    "authenticated snapshot fixture",
)
HISTORICAL_SOURCE = contract.SourceLocator(
    contract.AuthorityKind.HISTORICAL_RECORD,
    "historical://uber",
    "historical UBER observation/forecast fixture",
)
DRAFT_PR_15_SOURCE = contract.SourceLocator(
    contract.AuthorityKind.DRAFT,
    "github://mgh3326/auto_trader/pull/15",
    "head 8840c5d15d5496bee453cf65bd9d9585c237be9a",
)


def _accepted(value, source=OPERATOR_SOURCE):
    return contract.EvidenceFact(value, contract.EvidenceStatus.ACCEPTED, source)


def _codes(manifest):
    return {issue.code for issue in contract.validate_manifest(manifest).issues}


def _replace_first_account(account):
    manifest = contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST
    return replace(manifest, account_records=(account, *manifest.account_records[1:]))


def _writer(
    *,
    enabled: bool,
    source=OPERATOR_SOURCE,
    physical_id: str = "physical-test-account",
    writer_id: str = "writer-test-id",
):
    return contract.DesignatedMutationWriter(
        "writer-designation",
        _accepted(writer_id, source),
        _accepted(physical_id, source),
        _accepted(enabled, source),
        _accepted("active" if enabled else "disabled", source),
        _accepted(enabled, source),
    )


def _known_account(*writers, source=OPERATOR_SOURCE, active: bool = False):
    base = contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST.account_records[0]
    return replace(
        base,
        physical_account=contract.PhysicalBrokerAccount(
            "known-physical-account",
            _accepted("physical-test-account", source),
        ),
        designated_writers=writers,
        execution_posture=_accepted("active" if active else "disabled", source),
        mutation_eligible=_accepted(active, source),
    )


def test_static_manifest_is_immutable_machine_readable_and_valid():
    manifest = contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST

    assert contract.validate_manifest(manifest).is_valid
    with pytest.raises(FrozenInstanceError):
        manifest.schema_version = "changed"  # type: ignore[misc]

    payload = contract.manifest_as_machine_data()
    assert json.loads(json.dumps(payload, sort_keys=True)) == payload
    assert (
        payload["account_records"][0]["current_observation"]["holdings"]["value"]
        == "UNKNOWN"
    )


def test_only_the_six_approved_portfolio_categories_are_valid():
    assert contract.ALLOWED_PORTFOLIO_CATEGORIES == frozenset(
        {
            contract.PortfolioCategory.ACTIVE_FLAGSHIP,
            contract.PortfolioCategory.OFFLINE_CHALLENGER,
            contract.PortfolioCategory.UNBLOCKER,
            contract.PortfolioCategory.QUARANTINE,
            contract.PortfolioCategory.INFRASTRUCTURE,
            contract.PortfolioCategory.RESERVE,
        }
    )

    manifest = contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST
    first_entry = manifest.entries[0]
    invalid_lane = replace(first_entry.lane, category=_accepted("invented_category"))
    invalid_manifest = replace(
        manifest,
        entries=(replace(first_entry, lane=invalid_lane), *manifest.entries[1:]),
    )

    assert "invalid_portfolio_category" in _codes(invalid_manifest)


def test_slot_counts_exact_roles_and_zero_new_hypothesis_admission_are_enforced():
    manifest = contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST
    duplicate_challenger = replace(
        manifest.entries[1], entry_id="second-offline-challenger"
    )
    too_many = replace(manifest, entries=(*manifest.entries, duplicate_challenger))
    assert "portfolio_slot_count_mismatch" in _codes(too_many)

    wrong_role = replace(manifest.entries[1], role=_accepted("unapproved_role"))
    wrong_role_manifest = replace(
        manifest,
        entries=(manifest.entries[0], wrong_role, *manifest.entries[2:]),
    )
    assert {
        "role_not_approved_for_category",
        "portfolio_slot_role_mismatch",
    } <= _codes(wrong_role_manifest)

    admitted = replace(
        manifest,
        new_hypothesis_admissions=(manifest.entries[0].strategy_experiment,),
    )
    assert "new_hypothesis_family_admission_nonzero" in _codes(admitted)


def test_the_seven_logical_concepts_are_distinct_types_and_data():
    concept_types = {
        contract.StrategyExperiment,
        contract.PortfolioLane,
        contract.LogicalAccountSurface,
        contract.PhysicalBrokerAccount,
        contract.DesignatedMutationWriter,
        contract.BrokerObservation,
        contract.LocalLedgerLifecycle,
    }
    assert len(concept_types) == 7

    account = contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST.account_records[0]
    assert (
        type(
            contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST.entries[0].strategy_experiment
        )
        is contract.StrategyExperiment
    )
    assert (
        type(contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST.entries[0].lane)
        is contract.PortfolioLane
    )
    assert type(account.logical_surface) is contract.LogicalAccountSurface
    assert type(account.physical_account) is contract.PhysicalBrokerAccount
    assert type(account.current_observation) is contract.BrokerObservation
    assert type(account.local_ledger) is contract.LocalLedgerLifecycle


def test_every_truth_bearing_field_requires_locator_and_evidence_status():
    with pytest.raises(ValueError, match="source locator"):
        contract.EvidenceFact(
            True,
            contract.EvidenceStatus.ACCEPTED,
            None,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="source locator and reference"):
        contract.SourceLocator(contract.AuthorityKind.OPERATOR_ACCEPTED_MAIN, "", "ref")
    with pytest.raises(ValueError, match="stay UNKNOWN"):
        contract.EvidenceFact(
            False,
            contract.EvidenceStatus.MISSING,
            OPERATOR_SOURCE,
            "missing source",
        )


@pytest.mark.parametrize(
    "status",
    [
        contract.EvidenceStatus.MISSING,
        contract.EvidenceStatus.DRAFT,
        contract.EvidenceStatus.STALE,
        contract.EvidenceStatus.CONFLICT,
    ],
)
def test_missing_draft_stale_and_conflict_facts_preserve_unknown(status):
    fact = contract.EvidenceFact(
        contract.UNKNOWN, status, OPERATOR_SOURCE, "not accepted"
    )
    assert fact.value is contract.UNKNOWN
    with pytest.raises(ValueError, match="stay UNKNOWN"):
        contract.EvidenceFact(False, status, OPERATOR_SOURCE, "not accepted")


def test_authoritative_physical_account_writer_cardinality_rejects_zero_and_two():
    valid_writer = _writer(enabled=False)
    valid_one = _replace_first_account(_known_account(valid_writer))
    assert contract.validate_manifest(valid_one).is_valid

    zero_writers = _replace_first_account(_known_account())
    assert "designated_writer_cardinality" in _codes(zero_writers)

    two_writers = _replace_first_account(
        _known_account(valid_writer, replace(valid_writer, designation_id="writer-two"))
    )
    assert "designated_writer_cardinality" in _codes(two_writers)


def test_unknown_or_unapproved_physical_accounts_cannot_enable_a_writer():
    base = contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST.account_records[0]
    enabled = _writer(enabled=True)
    invalid = replace(
        base,
        designated_writers=(enabled,),
        execution_posture=_accepted("active"),
        mutation_eligible=_accepted(True),
    )

    assert "enabled_writer_for_unknown_or_unapproved_physical_account" in _codes(
        _replace_first_account(invalid)
    )


def test_capability_cannot_be_promoted_to_writer_or_flagship():
    capability_writer = _writer(enabled=True, source=CAPABILITY_SOURCE)
    capability_account = _known_account(
        capability_writer,
        source=CAPABILITY_SOURCE,
        active=True,
    )
    capability_manifest = _replace_first_account(capability_account)
    assert {
        "physical_account_not_accepted_operator_authority",
        "enabled_writer_missing_exact_operator_binding",
    } <= _codes(capability_manifest)

    manifest = contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST
    fake_experiment = contract.StrategyExperiment(
        "tool-capability",
        _accepted("ledger/tool/env capability", CAPABILITY_SOURCE),
        _accepted("alpha_validation", CAPABILITY_SOURCE),
    )
    false_flagship = replace(manifest.entries[0], strategy_experiment=fake_experiment)
    false_flagship_manifest = replace(
        manifest,
        entries=(false_flagship, *manifest.entries[1:]),
    )
    assert {
        "flagship_not_dfc_4h_experiment",
        "capability_promoted_to_flagship",
    } <= _codes(false_flagship_manifest)


def test_missing_local_ledger_row_cannot_become_no_holdings_or_no_orders():
    base = contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST.account_records[0]
    ledger_as_broker_truth = replace(
        base.current_observation,
        holdings=_accepted((), CAPABILITY_SOURCE),
        open_orders=_accepted((), CAPABILITY_SOURCE),
    )
    invalid = replace(base, current_observation=ledger_as_broker_truth)

    assert "current_observation_not_broker_evidence" in _codes(
        _replace_first_account(invalid)
    )


def test_equal_or_different_modes_and_keysets_cannot_establish_physical_identity():
    base = contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST.account_records[0]
    same_surface = replace(
        base.logical_surface,
        account_mode=_accepted("same-mode"),
        keyset_name=_accepted("same-keyset"),
    )
    different_surface = replace(
        base.logical_surface,
        account_mode=_accepted("different-mode"),
        keyset_name=_accepted("different-keyset"),
    )
    equal_labels_left = replace(base, logical_surface=same_surface)
    equal_labels_right = replace(base, logical_surface=same_surface)
    different_labels = replace(base, logical_surface=different_surface)

    assert (
        contract.compare_physical_account_identity(
            equal_labels_left, equal_labels_right
        )
        == "UNKNOWN"
    )
    assert (
        contract.compare_physical_account_identity(equal_labels_left, different_labels)
        == "UNKNOWN"
    )


def test_draft_pr15_rows_or_holdings_cannot_be_constructed_as_accepted_truth():
    with pytest.raises(ValueError, match="draft evidence"):
        contract.EvidenceFact(
            ("UBER",),
            contract.EvidenceStatus.ACCEPTED,
            DRAFT_PR_15_SOURCE,
        )


def test_historical_uber_observation_cannot_become_current_account_truth():
    base = contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST.account_records[0]
    historical_observation = replace(
        base.current_observation,
        holdings=_accepted(("UBER",), HISTORICAL_SOURCE),
        open_orders=_accepted((), HISTORICAL_SOURCE),
    )
    invalid = replace(base, current_observation=historical_observation)

    assert "current_observation_not_broker_evidence" in _codes(
        _replace_first_account(invalid)
    )


def test_manifest_and_validator_are_pure_without_broker_database_or_network_dependencies():
    module_source = inspect.getsource(contract)
    for forbidden in (
        "import app",
        "from app",
        "sqlalchemy",
        "httpx",
        "requests",
        "socket",
        "urllib",
        "sqlite",
    ):
        assert forbidden not in module_source

    first = contract.validate_manifest(contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST)
    second = contract.validate_manifest(contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST)
    assert first == second
    assert contract.manifest_as_machine_data() == contract.manifest_as_machine_data()


def test_authenticated_broker_snapshot_remains_the_only_way_to_state_empty_current_truth():
    base = contract.STATIC_PORTFOLIO_OWNERSHIP_MANIFEST.account_records[0]
    broker_observation = replace(
        base.current_observation,
        holdings=_accepted((), BROKER_SOURCE),
        open_orders=_accepted((), BROKER_SOURCE),
    )
    verified = replace(base, current_observation=broker_observation)

    assert contract.validate_manifest(_replace_first_account(verified)).is_valid
