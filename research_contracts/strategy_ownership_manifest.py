"""ROB-1189's pure, static strategy/ownership manifest.

It binds approved locators, evidence status, and reasons.  It does not read
brokers, databases, networks, ledgers, environment variables, or code paths.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

__all__ = [
    "ACTIVE_FLAGSHIP_SLOTS",
    "ALLOWED_PORTFOLIO_CATEGORIES",
    "APPROVED_ROLES_BY_CATEGORY",
    "EXTERNAL_EVIDENCE_UNBLOCKER_SLOTS",
    "NEW_HYPOTHESIS_FAMILY_ADMISSION",
    "OFFLINE_CHALLENGER_SLOTS",
    "UNKNOWN",
    "AccountOwnershipRecord",
    "AuthorityKind",
    "BrokerObservation",
    "DesignatedMutationWriter",
    "EvidenceFact",
    "EvidenceStatus",
    "LocalLedgerLifecycle",
    "LogicalAccountSurface",
    "PhysicalBrokerAccount",
    "PortfolioCategory",
    "PortfolioEntry",
    "PortfolioLane",
    "PortfolioOwnershipManifest",
    "STATIC_PORTFOLIO_OWNERSHIP_MANIFEST",
    "SourceLocator",
    "StrategyExperiment",
    "UnknownValue",
    "ValidationIssue",
    "ValidationResult",
    "compare_physical_account_identity",
    "manifest_as_machine_data",
    "validate_manifest",
]


class EvidenceStatus(StrEnum):
    ACCEPTED = "accepted"
    UNKNOWN = "unknown"
    MISSING = "missing"
    DRAFT = "draft"
    STALE = "stale"
    CONFLICT = "conflict"


class AuthorityKind(StrEnum):
    OPERATOR_ACCEPTED_MAIN = "operator_accepted_main"
    OPERATOR_APPROVED_DECISION = "operator_approved_decision"
    APPROVED_STATIC_INPUT = "approved_static_input"
    REPOSITORY_LOCATOR = "repository_locator"
    CURRENT_BROKER_OBSERVATION = "current_broker_observation"
    HISTORICAL_RECORD = "historical_record"
    DRAFT = "draft"


class PortfolioCategory(StrEnum):
    ACTIVE_FLAGSHIP = "active_flagship"
    OFFLINE_CHALLENGER = "offline_challenger"
    UNBLOCKER = "unblocker"
    QUARANTINE = "quarantine"
    INFRASTRUCTURE = "infrastructure"
    RESERVE = "reserve"


@dataclass(frozen=True)
class UnknownValue:
    def __str__(self) -> str:
        return "UNKNOWN"

    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN: Final = UnknownValue()
_UNKNOWN_STATUSES: Final = frozenset(
    {
        EvidenceStatus.UNKNOWN,
        EvidenceStatus.MISSING,
        EvidenceStatus.DRAFT,
        EvidenceStatus.STALE,
        EvidenceStatus.CONFLICT,
    }
)


@dataclass(frozen=True)
class SourceLocator:
    """Secret-free authority/location/reference for one evidence claim."""

    authority: AuthorityKind
    locator: str
    reference: str
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.authority, AuthorityKind):
            raise ValueError("authority must be an AuthorityKind")
        if not self.locator.strip() or not self.reference.strip():
            raise ValueError("source locator and reference are required")
        if self.sha256 is not None and (
            len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ValueError("sha256 must be a lowercase 64-character digest")


@dataclass(frozen=True)
class EvidenceFact[T]:
    """The only carrier for values that claim to be true in this contract."""

    value: T | UnknownValue
    evidence_status: EvidenceStatus
    source: SourceLocator
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_status, EvidenceStatus):
            raise ValueError("truth-bearing facts require an evidence status")
        if not isinstance(self.source, SourceLocator):
            raise ValueError("truth-bearing facts require a source locator")
        unknown = isinstance(self.value, UnknownValue)
        if self.evidence_status in _UNKNOWN_STATUSES:
            if not unknown or not self.reason or not self.reason.strip():
                raise ValueError(
                    "missing/draft/stale/conflict facts stay UNKNOWN with a reason"
                )
        elif unknown:
            raise ValueError("accepted facts cannot be UNKNOWN")
        if (
            self.evidence_status is EvidenceStatus.ACCEPTED
            and self.source.authority is AuthorityKind.DRAFT
        ):
            raise ValueError("draft evidence cannot become accepted truth")


# The seven concepts below are deliberately distinct types.  None derives from
# another and the validator does not use one as evidence for another.
@dataclass(frozen=True)
class StrategyExperiment:
    experiment_id: str
    hypothesis_family: EvidenceFact[str]
    stage: EvidenceFact[str]


@dataclass(frozen=True)
class PortfolioLane:
    lane_id: str
    category: EvidenceFact[PortfolioCategory]


@dataclass(frozen=True)
class LogicalAccountSurface:
    surface_id: str
    account_mode: EvidenceFact[str]
    tool_surface: EvidenceFact[str]
    keyset_name: EvidenceFact[str]


@dataclass(frozen=True)
class PhysicalBrokerAccount:
    record_id: str
    identity: EvidenceFact[str]


@dataclass(frozen=True)
class DesignatedMutationWriter:
    designation_id: str
    writer_identity: EvidenceFact[str]
    bound_physical_account: EvidenceFact[str]
    enabled: EvidenceFact[bool]
    posture: EvidenceFact[str]
    mutation_eligible: EvidenceFact[bool]


@dataclass(frozen=True)
class BrokerObservation:
    observation_id: str
    observation_scope: EvidenceFact[str]
    holdings: EvidenceFact[tuple[str, ...]]
    open_orders: EvidenceFact[tuple[str, ...]]


@dataclass(frozen=True)
class LocalLedgerLifecycle:
    ledger_id: str
    lifecycle_state: EvidenceFact[str]
    row_reference: EvidenceFact[str]


@dataclass(frozen=True)
class AccountOwnershipRecord:
    record_id: str
    logical_surface: LogicalAccountSurface
    physical_account: PhysicalBrokerAccount
    designated_writers: tuple[DesignatedMutationWriter, ...]
    current_observation: BrokerObservation
    local_ledger: LocalLedgerLifecycle
    execution_posture: EvidenceFact[str]
    mutation_eligible: EvidenceFact[bool]


@dataclass(frozen=True)
class PortfolioEntry:
    entry_id: str
    lane: PortfolioLane
    role: EvidenceFact[str]
    priority: EvidenceFact[str]
    scope: EvidenceFact[str]
    subject: EvidenceFact[str]
    strategy_experiment: StrategyExperiment | None = None
    account_record_id: str | None = None


@dataclass(frozen=True)
class PortfolioOwnershipManifest:
    schema_version: str
    entries: tuple[PortfolioEntry, ...]
    account_records: tuple[AccountOwnershipRecord, ...]
    new_hypothesis_admissions: tuple[StrategyExperiment, ...]


@dataclass(frozen=True, order=True)
class ValidationIssue:
    code: str
    path: str
    detail: str


@dataclass(frozen=True)
class ValidationResult:
    issues: tuple[ValidationIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues


ACTIVE_FLAGSHIP_SLOTS: Final = 1
OFFLINE_CHALLENGER_SLOTS: Final = 1
EXTERNAL_EVIDENCE_UNBLOCKER_SLOTS: Final = 1
NEW_HYPOTHESIS_FAMILY_ADMISSION: Final = 0
ALLOWED_PORTFOLIO_CATEGORIES: Final = frozenset(PortfolioCategory)

ROLE_DFC_4H: Final = "dfc_4h_public_data_shadow_no_order"
ROLE_AP_A1_A2: Final = "ap_a1_a2_offline_family"
ROLE_KR_B1: Final = "kr_b1_krx_koscom_procurement"
ROLE_HISTORICAL_UBER: Final = "historical_uber_record_only"
ROLE_INFRASTRUCTURE: Final = "supported_surfaces_infrastructure_only"
ROLE_RESERVE: Final = "explicitly_unassigned_account_or_keyset"
POSTURE_ACTIVE: Final = "active"
POSTURE_DISABLED: Final = "disabled"
POSTURE_READ_ONLY: Final = "read_only"
POSTURE_NO_ORDER: Final = "no_order"
POSTURE_UNKNOWN: Final = "unknown"
SCOPE_DFC_SHADOW: Final = "public_data_shadow_no_order"
SCOPE_OFFLINE: Final = "offline_analysis"
SCOPE_UNBLOCKER: Final = "external_evidence_only"
SCOPE_HISTORICAL: Final = "historical_record_only"
SCOPE_INFRASTRUCTURE: Final = "infrastructure_only"
SCOPE_RESERVE: Final = "unassigned_reserve"

APPROVED_ROLES_BY_CATEGORY: Final = MappingProxyType(
    {
        PortfolioCategory.ACTIVE_FLAGSHIP: frozenset({ROLE_DFC_4H}),
        PortfolioCategory.OFFLINE_CHALLENGER: frozenset({ROLE_AP_A1_A2}),
        PortfolioCategory.UNBLOCKER: frozenset({ROLE_KR_B1}),
        PortfolioCategory.QUARANTINE: frozenset({ROLE_HISTORICAL_UBER}),
        PortfolioCategory.INFRASTRUCTURE: frozenset({ROLE_INFRASTRUCTURE}),
        PortfolioCategory.RESERVE: frozenset({ROLE_RESERVE}),
    }
)
_SLOT_RULES: Final = MappingProxyType(
    {
        PortfolioCategory.ACTIVE_FLAGSHIP: (
            ACTIVE_FLAGSHIP_SLOTS,
            frozenset({ROLE_DFC_4H}),
        ),
        PortfolioCategory.OFFLINE_CHALLENGER: (
            OFFLINE_CHALLENGER_SLOTS,
            frozenset({ROLE_AP_A1_A2}),
        ),
        PortfolioCategory.UNBLOCKER: (
            EXTERNAL_EVIDENCE_UNBLOCKER_SLOTS,
            frozenset({ROLE_KR_B1}),
        ),
    }
)


def _accepted[T](value: T, source: SourceLocator) -> EvidenceFact[T]:
    return EvidenceFact(value, EvidenceStatus.ACCEPTED, source)


def _unknown(
    source: SourceLocator,
    reason: str,
    status: EvidenceStatus = EvidenceStatus.UNKNOWN,
) -> EvidenceFact[Any]:
    return EvidenceFact(UNKNOWN, status, source, reason)


OPERATOR_CONTRACT_SOURCE: Final = SourceLocator(
    AuthorityKind.OPERATOR_ACCEPTED_MAIN,
    "git://github.com/mgh3326/auto_trader-operator.git@a68e8d78a5f43db478f9e7118cba4c27f252c1ba",
    "operator_contract.yaml#global.mock_account_strategy_exclusive",
    "d09c4ced7e883e5abe92932b6b6131e93f08f2f28d5784c4a3c7a09e2405718f",
)
DIRECTIONAL_LAB_SOURCE: Final = SourceLocator(
    AuthorityKind.OPERATOR_ACCEPTED_MAIN,
    "git://github.com/mgh3326/auto_trader-operator.git@a68e8d78a5f43db478f9e7118cba4c27f252c1ba",
    "prompts/directional-lab-contract.md#0",
    "609d57a527c9ef056524edf3ca4f087d089950ddf8a334605ff204ef1e8ce80a",
)
KR_B1_SOURCE: Final = SourceLocator(
    AuthorityKind.APPROVED_STATIC_INPUT,
    "/Users/mgh3326/work/herdr-inbox/krb1-combined-canonical-2026-07-28.json",
    "canonical strategy identity",
    "d5e1246b2072ad227d924e059091e27fa49719e42a5bfba651ae8f2bced9d6f1",
)
KR_B1_AMENDMENT_SOURCE: Final = SourceLocator(
    AuthorityKind.APPROVED_STATIC_INPUT,
    "/Users/mgh3326/work/herdr-inbox/krb1c-amendment-canonical-2026-07-28.json",
    "canonical amendment",
    "d5da5edd6b49fb759b781c13f627e21a84667ced2be7cf03624a40bb813be389",
)
POSTURE_SOURCE: Final = SourceLocator(
    AuthorityKind.APPROVED_STATIC_INPUT,
    "/Users/mgh3326/work/herdr-inbox/gptpro-posture-v1-2026-07-27.md",
    "posture-v1 identity only",
    "3e558acdcb776e2d53c6a2846347ad1031bdcaf785b390f4e0b6c008e5236df8",
)
ALPACA_BLOCK_SOURCE: Final = SourceLocator(
    AuthorityKind.APPROVED_STATIC_INPUT,
    "/Users/mgh3326/work/herdr-inbox/n5-alpaca-fix-2026-07-29.md",
    "Alpaca basic BLOCK/read-only boundary",
    "e4706e1977c07080f3dd7b3bfe439eee232ffde05439b3d7f2d47e3055f64a84",
)
DFC_SEAL_SOURCE: Final = SourceLocator(
    AuthorityKind.APPROVED_STATIC_INPUT,
    "/Users/mgh3326/work/herdr-inbox/r41-combined-sealed-2026-07-28.md",
    "R4.1-DFC-4H internal seal b3ee7db2f4cd8f76522a9c66ca8201177a01c24bbbd3876f53da4fb2f7c14a94",
    "33ee4a7b59085a8b6c4b5a75d580f8d95bb73ab8aa3d5b34af03358b8a9d01a3",
)
DFC_AUDIT_SOURCE: Final = SourceLocator(
    AuthorityKind.APPROVED_STATIC_INPUT,
    "/Users/mgh3326/work/herdr-inbox/dfc4h-integrity-audit-2026-07-29.md",
    "final DATA_INTEGRITY_FAIL",
    "1a055e71a9c8436334eb722bdc1f830bbef1ec3e4d13d236dede5d883c7f94fe",
)
MISSION_SCOPE_SOURCE: Final = SourceLocator(
    AuthorityKind.OPERATOR_APPROVED_DECISION,
    "operator-decision://ROB-1189/2026-08-01",
    "static-read-only scope; authenticated broker reads prohibited",
)


def _observation(record_id: str) -> BrokerObservation:
    return BrokerObservation(
        f"{record_id}-current-observation",
        _accepted("current", MISSION_SCOPE_SOURCE),
        _unknown(
            MISSION_SCOPE_SOURCE,
            "authenticated broker reads are prohibited in this static contract",
            EvidenceStatus.MISSING,
        ),
        _unknown(
            MISSION_SCOPE_SOURCE,
            "authenticated broker reads are prohibited in this static contract",
            EvidenceStatus.MISSING,
        ),
    )


def _ledger(record_id: str) -> LocalLedgerLifecycle:
    return LocalLedgerLifecycle(
        f"{record_id}-local-ledger",
        _unknown(
            MISSION_SCOPE_SOURCE,
            "local ledger state was not queried by this static contract",
            EvidenceStatus.MISSING,
        ),
        _unknown(
            MISSION_SCOPE_SOURCE,
            "local ledger row was not queried by this static contract",
            EvidenceStatus.MISSING,
        ),
    )


def _surface(
    surface_id: str,
    account_mode: str | UnknownValue,
    tool_surface: str | UnknownValue,
    source: SourceLocator,
    reason: str = "logical surface is unassigned",
) -> LogicalAccountSurface:
    return LogicalAccountSurface(
        surface_id,
        _accepted(account_mode, source)
        if isinstance(account_mode, str)
        else _unknown(source, reason),
        _accepted(tool_surface, source)
        if isinstance(tool_surface, str)
        else _unknown(source, reason),
        _unknown(
            MISSION_SCOPE_SOURCE,
            "keyset equality or difference cannot establish physical-account identity",
            EvidenceStatus.MISSING,
        ),
    )


def _account(
    record_id: str,
    surface: LogicalAccountSurface,
    posture: str | UnknownValue,
    source: SourceLocator,
    reason: str = "no writer posture is authorized for an unknown physical account",
) -> AccountOwnershipRecord:
    return AccountOwnershipRecord(
        record_id,
        surface,
        PhysicalBrokerAccount(
            f"{record_id}-physical",
            _unknown(
                MISSION_SCOPE_SOURCE,
                "physical broker account identity is not authorized or observed in this mission",
                EvidenceStatus.MISSING,
            ),
        ),
        (),
        _observation(record_id),
        _ledger(record_id),
        _accepted(posture, source)
        if isinstance(posture, str)
        else _unknown(source, reason),
        _accepted(False, MISSION_SCOPE_SOURCE),
    )


BINANCE_DEMO_ACCOUNT: Final = _account(
    "binance-demo-logical-surface",
    _surface(
        "binance-demo", "binance_demo", "default-profile", OPERATOR_CONTRACT_SOURCE
    ),
    POSTURE_NO_ORDER,
    DFC_AUDIT_SOURCE,
)
KIWOOM_MOCK_ACCOUNT: Final = _account(
    "kiwoom-mock-logical-surface",
    _surface("kiwoom-mock", "kiwoom_mock", "kiwoom", OPERATOR_CONTRACT_SOURCE),
    UNKNOWN,
    MISSION_SCOPE_SOURCE,
)
KIS_MOCK_ACCOUNT: Final = _account(
    "kis-mock-logical-surface",
    _surface("kis-mock", "kis_mock", "hermes-paper-kis", OPERATOR_CONTRACT_SOURCE),
    POSTURE_READ_ONLY,
    OPERATOR_CONTRACT_SOURCE,
)
ALPACA_PAPER_ACCOUNT: Final = _account(
    "alpaca-paper-logical-surface",
    _surface("alpaca-paper", "alpaca_paper", "us-paper", OPERATOR_CONTRACT_SOURCE),
    POSTURE_READ_ONLY,
    ALPACA_BLOCK_SOURCE,
)
ALPACA_PAPER_LAB_ACCOUNT: Final = _account(
    "alpaca-paper-lab-logical-surface",
    _surface(
        "alpaca-paper-lab",
        "alpaca_paper_lab",
        "directional-lab-us",
        DIRECTIONAL_LAB_SOURCE,
    ),
    UNKNOWN,
    MISSION_SCOPE_SOURCE,
)
UNASSIGNED_KIWOOM_US_ACCOUNT: Final = _account(
    "unassigned-kiwoom-us-logical-surface",
    _surface(
        "unassigned-kiwoom-us",
        UNKNOWN,
        UNKNOWN,
        OPERATOR_CONTRACT_SOURCE,
        "accepted main has no authoritative kiwoom_mock_us mapping",
    ),
    UNKNOWN,
    MISSION_SCOPE_SOURCE,
)
UNASSIGNED_ALPACA_KEYSET_ACCOUNT: Final = _account(
    "unassigned-alpaca-keyset-logical-surface",
    _surface(
        "unassigned-alpaca-keyset",
        UNKNOWN,
        UNKNOWN,
        OPERATOR_CONTRACT_SOURCE,
        "accepted main has no authoritative third-Alpaca-keyset mapping",
    ),
    UNKNOWN,
    MISSION_SCOPE_SOURCE,
)

DFC_4H_EXPERIMENT: Final = StrategyExperiment(
    "dfc-4h",
    _accepted("DFC-4H", DFC_SEAL_SOURCE),
    _accepted("alpha_validation", MISSION_SCOPE_SOURCE),
)
AP_A1_A2_EXPERIMENT: Final = StrategyExperiment(
    "ap-a1-a2",
    _accepted("AP-A1/AP-A2", MISSION_SCOPE_SOURCE),
    _accepted("offline_challenger", MISSION_SCOPE_SOURCE),
)
KR_B1_EXPERIMENT: Final = StrategyExperiment(
    "kr-b1",
    _accepted("KR-B1", KR_B1_SOURCE),
    _accepted("external_evidence_unblocker", KR_B1_AMENDMENT_SOURCE),
)

STATIC_PORTFOLIO_OWNERSHIP_MANIFEST: Final = PortfolioOwnershipManifest(
    "strategy-ownership-manifest.v1",
    (
        PortfolioEntry(
            "dfc-4h-flagship",
            PortfolioLane(
                "active-flagship",
                _accepted(PortfolioCategory.ACTIVE_FLAGSHIP, MISSION_SCOPE_SOURCE),
            ),
            _accepted(ROLE_DFC_4H, MISSION_SCOPE_SOURCE),
            _accepted("alpha_validation", MISSION_SCOPE_SOURCE),
            _accepted(SCOPE_DFC_SHADOW, MISSION_SCOPE_SOURCE),
            _accepted("DFC-4H public-data shadow/no-order", DFC_AUDIT_SOURCE),
            DFC_4H_EXPERIMENT,
            BINANCE_DEMO_ACCOUNT.record_id,
        ),
        PortfolioEntry(
            "ap-a1-a2-offline-challenger",
            PortfolioLane(
                "offline-challenger",
                _accepted(PortfolioCategory.OFFLINE_CHALLENGER, MISSION_SCOPE_SOURCE),
            ),
            _accepted(ROLE_AP_A1_A2, MISSION_SCOPE_SOURCE),
            _accepted("offline_comparison", MISSION_SCOPE_SOURCE),
            _accepted(SCOPE_OFFLINE, MISSION_SCOPE_SOURCE),
            _accepted("AP-A1/AP-A2 family", MISSION_SCOPE_SOURCE),
            AP_A1_A2_EXPERIMENT,
        ),
        PortfolioEntry(
            "kr-b1-procurement-unblocker",
            PortfolioLane(
                "external-evidence-unblocker",
                _accepted(PortfolioCategory.UNBLOCKER, MISSION_SCOPE_SOURCE),
            ),
            _accepted(ROLE_KR_B1, MISSION_SCOPE_SOURCE),
            _accepted("external_evidence", MISSION_SCOPE_SOURCE),
            _accepted(SCOPE_UNBLOCKER, MISSION_SCOPE_SOURCE),
            _accepted("KR-B1 / KRX·Koscom procurement", KR_B1_SOURCE),
            KR_B1_EXPERIMENT,
        ),
        PortfolioEntry(
            "historical-uber-quarantine",
            PortfolioLane(
                "quarantine",
                _accepted(PortfolioCategory.QUARANTINE, MISSION_SCOPE_SOURCE),
            ),
            _accepted(ROLE_HISTORICAL_UBER, MISSION_SCOPE_SOURCE),
            _accepted("record_scope_only", MISSION_SCOPE_SOURCE),
            _accepted(SCOPE_HISTORICAL, MISSION_SCOPE_SOURCE),
            _accepted("historical invalid/blocked UBER sample", ALPACA_BLOCK_SOURCE),
        ),
        PortfolioEntry(
            "supported-surfaces-infrastructure",
            PortfolioLane(
                "infrastructure",
                _accepted(PortfolioCategory.INFRASTRUCTURE, MISSION_SCOPE_SOURCE),
            ),
            _accepted(ROLE_INFRASTRUCTURE, MISSION_SCOPE_SOURCE),
            _accepted("non_strategy_infrastructure", MISSION_SCOPE_SOURCE),
            _accepted(SCOPE_INFRASTRUCTURE, MISSION_SCOPE_SOURCE),
            _unknown(
                POSTURE_SOURCE,
                "unsupported posture/watch/scalping/smoke/autonomous-loop facts remain UNKNOWN",
            ),
        ),
        PortfolioEntry(
            "unassigned-account-keyset-reserve",
            PortfolioLane(
                "reserve",
                _accepted(PortfolioCategory.RESERVE, MISSION_SCOPE_SOURCE),
            ),
            _accepted(ROLE_RESERVE, MISSION_SCOPE_SOURCE),
            _accepted("no_strategy_inference", MISSION_SCOPE_SOURCE),
            _accepted(SCOPE_RESERVE, MISSION_SCOPE_SOURCE),
            _unknown(
                OPERATOR_CONTRACT_SOURCE,
                "unassigned account/keyset has no accepted strategy mapping",
                EvidenceStatus.MISSING,
            ),
        ),
    ),
    (
        BINANCE_DEMO_ACCOUNT,
        KIWOOM_MOCK_ACCOUNT,
        KIS_MOCK_ACCOUNT,
        ALPACA_PAPER_ACCOUNT,
        ALPACA_PAPER_LAB_ACCOUNT,
        UNASSIGNED_KIWOOM_US_ACCOUNT,
        UNASSIGNED_ALPACA_KEYSET_ACCOUNT,
    ),
    (),
)


def _known(fact: object) -> bool:
    return (
        isinstance(fact, EvidenceFact)
        and fact.evidence_status is EvidenceStatus.ACCEPTED
        and not isinstance(fact.value, UnknownValue)
    )


def _value(fact: object) -> object:
    return fact.value if _known(fact) else UNKNOWN


def _operator_authority(source: SourceLocator) -> bool:
    return source.authority in {
        AuthorityKind.OPERATOR_ACCEPTED_MAIN,
        AuthorityKind.OPERATOR_APPROVED_DECISION,
    }


def _authoritative_account(account: PhysicalBrokerAccount) -> bool:
    return _known(account.identity) and _operator_authority(account.identity.source)


def _add(errors: list[ValidationIssue], code: str, path: str, detail: str) -> None:
    errors.append(ValidationIssue(code, path, detail))


def _fact(
    candidate: object, path: str, errors: list[ValidationIssue]
) -> EvidenceFact[Any] | None:
    if not isinstance(candidate, EvidenceFact):
        _add(
            errors,
            "truth_field_not_evidence_fact",
            path,
            "requires locator and evidence status",
        )
        return None
    return candidate


def _validate_observation(
    observation: object, path: str, errors: list[ValidationIssue]
) -> None:
    if not isinstance(observation, BrokerObservation):
        _add(errors, "broker_observation_type", path, "requires BrokerObservation")
        return
    scope = _fact(observation.observation_scope, f"{path}.observation_scope", errors)
    if scope is not None and _known(scope) and scope.value != "current":
        _add(
            errors,
            "historical_observation_not_current_truth",
            f"{path}.observation_scope",
            "current account truth cannot use a historical observation",
        )
    for name, candidate in (
        ("holdings", observation.holdings),
        ("open_orders", observation.open_orders),
    ):
        fact = _fact(candidate, f"{path}.{name}", errors)
        if fact is not None and _known(fact):
            if fact.source.authority is not AuthorityKind.CURRENT_BROKER_OBSERVATION:
                _add(
                    errors,
                    "current_observation_not_broker_evidence",
                    f"{path}.{name}",
                    "local ledger, draft, and historical records cannot prove current broker state",
                )
            if not isinstance(fact.value, tuple):
                _add(
                    errors,
                    "invalid_current_observation_value",
                    f"{path}.{name}",
                    "known broker observations use immutable tuples",
                )


def _validate_writer(
    writer: object,
    account: PhysicalBrokerAccount,
    account_posture: object,
    account_eligible: object,
    path: str,
    errors: list[ValidationIssue],
) -> None:
    if not isinstance(writer, DesignatedMutationWriter):
        _add(
            errors, "designated_writer_type", path, "requires DesignatedMutationWriter"
        )
        return
    writer_id = _fact(writer.writer_identity, f"{path}.writer_identity", errors)
    bound_account = _fact(
        writer.bound_physical_account, f"{path}.bound_physical_account", errors
    )
    enabled = _fact(writer.enabled, f"{path}.enabled", errors)
    writer_posture = _fact(writer.posture, f"{path}.posture", errors)
    writer_eligible = _fact(
        writer.mutation_eligible, f"{path}.mutation_eligible", errors
    )
    is_enabled = enabled is not None and _known(enabled) and enabled.value is True
    is_eligible = (
        writer_eligible is not None
        and _known(writer_eligible)
        and writer_eligible.value is True
    )
    if is_eligible and not is_enabled:
        _add(
            errors,
            "writer_mutation_eligible_without_enablement",
            f"{path}.mutation_eligible",
            "mutation eligibility cannot exceed disabled writer status",
        )
    if not is_enabled:
        return
    exact_binding = (
        _authoritative_account(account)
        and writer_id is not None
        and bound_account is not None
        and _known(writer_id)
        and _known(bound_account)
        and _operator_authority(bound_account.source)
        and writer_id.value == bound_account.value
        and bound_account.value == account.identity.value
    )
    if not exact_binding:
        _add(
            errors,
            "enabled_writer_missing_exact_operator_binding",
            f"{path}.bound_physical_account",
            "enabled writer needs accepted authority for exact account and writer",
        )
    if not _authoritative_account(account):
        _add(
            errors,
            "enabled_writer_for_unknown_or_unapproved_physical_account",
            f"{path}.enabled",
            "unknown or unapproved physical accounts cannot enable a writer",
        )
    if not is_eligible:
        _add(
            errors,
            "enabled_writer_not_mutation_eligible",
            f"{path}.mutation_eligible",
            "required",
        )
    if not (_known(writer_posture) and writer_posture.value == POSTURE_ACTIVE):
        _add(errors, "enabled_writer_not_active", f"{path}.posture", "required")
    if not (
        _known(account_posture)
        and account_posture.value == POSTURE_ACTIVE
        and account_eligible is True
    ):
        _add(errors, "enabled_writer_account_not_active", f"{path}.enabled", "required")


def _validate_account(
    account: object, path: str, errors: list[ValidationIssue]
) -> None:
    if not isinstance(account, AccountOwnershipRecord):
        _add(
            errors,
            "account_ownership_record_type",
            path,
            "requires AccountOwnershipRecord",
        )
        return
    if not isinstance(account.logical_surface, LogicalAccountSurface):
        _add(
            errors,
            "logical_surface_type",
            f"{path}.logical_surface",
            "requires LogicalAccountSurface",
        )
    else:
        for name in ("account_mode", "tool_surface", "keyset_name"):
            _fact(
                getattr(account.logical_surface, name),
                f"{path}.logical_surface.{name}",
                errors,
            )
    if not isinstance(account.physical_account, PhysicalBrokerAccount):
        _add(
            errors,
            "physical_account_type",
            f"{path}.physical_account",
            "requires PhysicalBrokerAccount",
        )
        return
    physical_identity = _fact(
        account.physical_account.identity, f"{path}.physical_account.identity", errors
    )
    if (
        physical_identity is not None
        and _known(physical_identity)
        and not _operator_authority(physical_identity.source)
    ):
        _add(
            errors,
            "physical_account_not_accepted_operator_authority",
            f"{path}.physical_account.identity",
            "mode/keyset/tool/env/ledger/code cannot identify a physical account",
        )
    _validate_observation(
        account.current_observation, f"{path}.current_observation", errors
    )
    if not isinstance(account.local_ledger, LocalLedgerLifecycle):
        _add(
            errors,
            "local_ledger_type",
            f"{path}.local_ledger",
            "requires LocalLedgerLifecycle",
        )
    else:
        _fact(
            account.local_ledger.lifecycle_state,
            f"{path}.local_ledger.lifecycle_state",
            errors,
        )
        _fact(
            account.local_ledger.row_reference,
            f"{path}.local_ledger.row_reference",
            errors,
        )
    posture = _fact(account.execution_posture, f"{path}.execution_posture", errors)
    eligible = _fact(account.mutation_eligible, f"{path}.mutation_eligible", errors)
    posture_value = _value(posture)
    eligible_value = _value(eligible)
    if not isinstance(account.designated_writers, tuple):
        _add(
            errors,
            "designated_writers_not_immutable",
            f"{path}.designated_writers",
            "requires tuple",
        )
        writers: tuple[object, ...] = ()
    else:
        writers = account.designated_writers
    authoritative = _authoritative_account(account.physical_account)
    if authoritative and len(writers) != 1:
        _add(
            errors,
            "designated_writer_cardinality",
            f"{path}.designated_writers",
            "every authoritative physical account requires exactly one writer record",
        )
    safe_postures = {
        UNKNOWN,
        POSTURE_DISABLED,
        POSTURE_READ_ONLY,
        POSTURE_NO_ORDER,
        POSTURE_UNKNOWN,
    }
    if not authoritative:
        if eligible_value is not False:
            _add(
                errors,
                "unknown_physical_account_must_not_be_mutation_eligible",
                f"{path}.mutation_eligible",
                "unknown/unapproved physical accounts are mutation-ineligible",
            )
        if posture_value not in safe_postures:
            _add(
                errors,
                "unknown_physical_account_requires_safe_posture",
                f"{path}.execution_posture",
                "posture must remain disabled/read-only/no-order/unknown",
            )
    if (
        posture_value in {POSTURE_DISABLED, POSTURE_READ_ONLY, POSTURE_NO_ORDER}
        and eligible_value is not False
    ):
        _add(
            errors,
            "safe_posture_must_not_be_mutation_eligible",
            f"{path}.mutation_eligible",
            "required",
        )
    for index, writer in enumerate(writers):
        _validate_writer(
            writer,
            account.physical_account,
            posture,
            eligible_value,
            f"{path}.designated_writers[{index}]",
            errors,
        )


def _validate_entry(
    entry: object,
    accounts: dict[str, AccountOwnershipRecord],
    path: str,
    errors: list[ValidationIssue],
) -> None:
    if not isinstance(entry, PortfolioEntry):
        _add(errors, "portfolio_entry_type", path, "requires PortfolioEntry")
        return
    if not isinstance(entry.lane, PortfolioLane):
        _add(errors, "portfolio_lane_type", f"{path}.lane", "requires PortfolioLane")
        return
    category_fact = _fact(entry.lane.category, f"{path}.lane.category", errors)
    role_fact = _fact(entry.role, f"{path}.role", errors)
    priority = _fact(entry.priority, f"{path}.priority", errors)
    scope = _fact(entry.scope, f"{path}.scope", errors)
    _fact(entry.subject, f"{path}.subject", errors)
    category, role = _value(category_fact), _value(role_fact)
    if category is not UNKNOWN and category not in ALLOWED_PORTFOLIO_CATEGORIES:
        _add(
            errors,
            "invalid_portfolio_category",
            f"{path}.lane.category",
            "only six values are valid",
        )
    if (
        category in APPROVED_ROLES_BY_CATEGORY
        and role not in APPROVED_ROLES_BY_CATEGORY[category]
    ):
        _add(
            errors,
            "role_not_approved_for_category",
            f"{path}.role",
            "role/category mismatch",
        )
    if entry.strategy_experiment is not None:
        if not isinstance(entry.strategy_experiment, StrategyExperiment):
            _add(
                errors,
                "strategy_experiment_type",
                f"{path}.strategy_experiment",
                "required type",
            )
        else:
            _fact(
                entry.strategy_experiment.hypothesis_family,
                f"{path}.strategy_experiment.hypothesis_family",
                errors,
            )
            _fact(
                entry.strategy_experiment.stage,
                f"{path}.strategy_experiment.stage",
                errors,
            )
    slotted = {
        PortfolioCategory.ACTIVE_FLAGSHIP,
        PortfolioCategory.OFFLINE_CHALLENGER,
        PortfolioCategory.UNBLOCKER,
    }
    if category in slotted and entry.strategy_experiment is None:
        _add(
            errors,
            "slotted_role_requires_strategy_experiment",
            f"{path}.strategy_experiment",
            "required",
        )
    if (
        category
        in {
            PortfolioCategory.QUARANTINE,
            PortfolioCategory.INFRASTRUCTURE,
            PortfolioCategory.RESERVE,
        }
        and entry.strategy_experiment is not None
    ):
        _add(
            errors,
            "non_strategy_category_cannot_invent_experiment",
            f"{path}.strategy_experiment",
            "forbidden",
        )
    if entry.account_record_id is not None and entry.account_record_id not in accounts:
        _add(
            errors,
            "unknown_account_record_link",
            f"{path}.account_record_id",
            "missing account record",
        )
    if role == ROLE_DFC_4H:
        experiment = entry.strategy_experiment
        if (
            not isinstance(experiment, StrategyExperiment)
            or experiment.experiment_id != "dfc-4h"
        ):
            _add(
                errors,
                "flagship_not_dfc_4h_experiment",
                f"{path}.strategy_experiment",
                "required",
            )
        if (
            isinstance(experiment, StrategyExperiment)
            and experiment.hypothesis_family.source.authority
            is AuthorityKind.REPOSITORY_LOCATOR
        ):
            _add(
                errors,
                "capability_promoted_to_flagship",
                f"{path}.strategy_experiment",
                "forbidden",
            )
        if _value(scope) != SCOPE_DFC_SHADOW or _value(priority) != "alpha_validation":
            _add(
                errors,
                "dfc_flagship_scope_or_priority",
                path,
                "must remain alpha-validation public-data no-order",
            )
        account = accounts.get(entry.account_record_id or "")
        if account is None:
            _add(
                errors,
                "dfc_flagship_missing_account_surface",
                f"{path}.account_record_id",
                "required",
            )
        elif any(
            _known(writer.enabled) and writer.enabled.value is True
            for writer in account.designated_writers
        ):
            _add(
                errors,
                "dfc_flagship_cannot_authorize_writer",
                f"{path}.account_record_id",
                "forbidden",
            )
    if role == ROLE_HISTORICAL_UBER and (
        _value(scope) != SCOPE_HISTORICAL or entry.account_record_id is not None
    ):
        _add(
            errors,
            "historical_uber_not_current_account_truth",
            path,
            "quarantine record only",
        )


def validate_manifest(manifest: PortfolioOwnershipManifest) -> ValidationResult:
    """Pure deterministic validation: no I/O, mutations, or inferred facts."""

    errors: list[ValidationIssue] = []
    if not isinstance(manifest, PortfolioOwnershipManifest):
        return ValidationResult(
            (ValidationIssue("manifest_type", "manifest", "required type"),)
        )
    if not manifest.schema_version.strip():
        _add(errors, "missing_schema_version", "manifest.schema_version", "required")
    accounts = {
        account.record_id: account
        for account in manifest.account_records
        if isinstance(account, AccountOwnershipRecord)
    }
    if len(accounts) != len(manifest.account_records):
        _add(
            errors,
            "duplicate_or_invalid_account_record_id",
            "manifest.account_records",
            "unique ids required",
        )
    for index, account in enumerate(manifest.account_records):
        _validate_account(account, f"manifest.account_records[{index}]", errors)
    for index, entry in enumerate(manifest.entries):
        _validate_entry(entry, accounts, f"manifest.entries[{index}]", errors)
    for category, (slots, roles) in _SLOT_RULES.items():
        entries = [
            entry
            for entry in manifest.entries
            if isinstance(entry, PortfolioEntry)
            and _value(entry.lane.category) is category
        ]
        if len(entries) != slots:
            _add(
                errors,
                "portfolio_slot_count_mismatch",
                f"manifest.entries[{category.value}]",
                f"expected {slots}, got {len(entries)}",
            )
        if frozenset(_value(entry.role) for entry in entries) != roles:
            _add(
                errors,
                "portfolio_slot_role_mismatch",
                f"manifest.entries[{category.value}]",
                "exact roles required",
            )
    if len(manifest.new_hypothesis_admissions) != NEW_HYPOTHESIS_FAMILY_ADMISSION:
        _add(
            errors,
            "new_hypothesis_family_admission_nonzero",
            "manifest.new_hypothesis_admissions",
            "fixed at zero",
        )
    return ValidationResult(tuple(sorted(errors)))


def compare_physical_account_identity(
    left: AccountOwnershipRecord, right: AccountOwnershipRecord
) -> str:
    """Only accepted physical identities can be same/distinct; modes/keysets cannot."""

    if not (
        _authoritative_account(left.physical_account)
        and _authoritative_account(right.physical_account)
    ):
        return "UNKNOWN"
    return (
        "SAME"
        if left.physical_account.identity.value == right.physical_account.identity.value
        else "DISTINCT"
    )


def _machine_value(value: Any) -> Any:
    if isinstance(value, UnknownValue):
        return "UNKNOWN"
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: _machine_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_machine_value(item) for item in value]
    return value


def manifest_as_machine_data(
    manifest: PortfolioOwnershipManifest = STATIC_PORTFOLIO_OWNERSHIP_MANIFEST,
) -> dict[str, Any]:
    """Return deterministic JSON-ready data without consulting external state."""

    return _machine_value(manifest)
