"""Fail-closed mock/paper/demo account identity registry (ROB-1260).

This module defines no broker transport, database, scheduler, signing, or
credential-value I/O. Canonical rows describe the evidence available at the
J2A boundary; unavailable bindings stay present and blocked instead of being
guessed or filtered out. Guarded helpers validate caller declarations before
invoking opaque callbacks, but do not bind those declarations to a transport.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal, Protocol
from urllib.parse import urlsplit

from app.schemas.execution_contracts import LaneStatus, SchedulerOwner
from app.services.mock_integration.lineage import (
    BROKER_CLIENT_ID_TARGET_MISMATCH,
    LineageEnvelope,
)

QuoteCurrency = Literal["KRW", "USD", "USDT"]


class AccountMode(StrEnum):
    """Only non-live account modes are representable in this registry."""

    MOCK = "mock"
    PAPER = "paper"
    DEMO = "demo"
    SHADOW = "shadow"


class EndpointClass(StrEnum):
    """Endpoint classes intentionally omit a live value."""

    MOCK = "mock"
    PAPER = "paper"
    DEMO = "demo"
    SHADOW = "shadow"


class RegistryRole(StrEnum):
    """Single-value role vocabulary used by the lane registry."""

    PRIMARY_AUTO = "PRIMARY_AUTO"
    AUTO_MIRROR = "AUTO_MIRROR"
    BROKER_REGRESSION = "BROKER_REGRESSION"
    EXECUTION_AUTO = "EXECUTION_AUTO"
    AUTO_CHALLENGER = "AUTO_CHALLENGER"
    SHADOW_ONLY = "SHADOW_ONLY"


class ActivationStatus(StrEnum):
    """Activation is a separate axis from :class:`LaneStatus`."""

    DISABLED = "DISABLED"
    BLOCKED = "BLOCKED"
    READY = "READY"
    ENABLED = "ENABLED"
    RUNTIME_ACCEPTANCE_PENDING = "RUNTIME_ACCEPTANCE_PENDING"
    READY_FOR_MOCK_DEPLOYMENT = "READY_FOR_MOCK_DEPLOYMENT"


class MissingBinding(StrEnum):
    """Bindings that may not be synthesized at the J2A boundary."""

    PHYSICAL_ACCOUNT_FINGERPRINT = "physical_account_fingerprint"
    POLICY = "policy"
    CAP = "cap"
    OWNER = "owner"
    CANARY = "canary"


@dataclass(frozen=True, slots=True)
class PolicyBinding:
    """In-memory exact policy identity; neither field may be blank."""

    policy_version: str
    policy_version_hash: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and bool(value.strip())
            for value in (self.policy_version, self.policy_version_hash)
        ):
            raise ValueError("lane_binding_incomplete")


ACTIVATION_TRANSITION_GUARDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "G1": (
            "ENABLED 진입은 기존에 직접 증명된 cadence 보존 lane 에만. 신규 recurring\n"
            "       schedule 이 필요하면 진입 금지 → "
            "AUTO_READY_BLOCKED_BY_SCHEDULER (D4)"
        ),
        "G2": (
            "READY_FOR_MOCK_DEPLOYMENT 는 종착 상태. shared production release 또는\n"
            "       live restart 필요 시 여기서 정지, 자동 승격 경로 없음 "
            "(운영자 결정 6)"
        ),
        "G3": (
            "J8 canary 성공은 RUNTIME_ACCEPTANCE_PENDING → READY 까지만 이동시키며\n"
            "       ENABLED 로 자동 전이시키지 않는다 (D4)"
        ),
    }
)

UNKNOWN_IDENTITY_RULE: Final[str] = (
    "broker fingerprint 증거 부재 시 physical_account_id=null,\n"
    "  identity_status=UNKNOWN, writer=false, auto=false. 행은 삭제하지 않는다."
)
MISSING_BINDING_RULE: Final[str] = (
    "policy/cap/owner/canary 부재 시 행을 보존하고\n"
    "  blocked|disabled + 사유. worker 는 값을 발명하지 않는다."
)

R2_REJECT_CODES: Final[frozenset[str]] = frozenset(
    {
        "lane_signed_restriction_violation",
        "lane_recurring_not_authorized",
        "canonical_lane_identity_mismatch",
        "invalid_scheduler_owner",
        "invalid_timing_owner",
        "physical_account_writer_conflict",
        "canonical_lane_ids_mismatch",
        "canonical_credential_namespace_mismatch",
        "canonical_host_allowlist_mismatch",
        "lane_broker_mismatch",
        "lane_account_profile_mismatch",
        "lane_account_mode_mismatch",
        "lane_policy_binding_mismatch",
        "lane_binding_incomplete",
        BROKER_CLIENT_ID_TARGET_MISMATCH,
        "lane_quote_currency_mismatch",
    }
)

CANONICAL_LANE_IDS: Final[tuple[str, ...]] = (
    "kr.kis.mock",
    "kr.kiwoom.mock",
    "us.kis.mock",
    "us.kiwoom.mock",
    "us.alpaca.paper.default",
    "us.alpaca.paper.lab",
    "crypto.binance.spot_demo.canonical",
    "crypto.binance.spot_demo.b0x_sidecar",
    "crypto.alpaca.paper.default",
    "crypto.alpaca.paper.clean",
    "crypto.upbit.shadow",
    "crypto.binance.futures_demo",
)

LANE_QUOTE_CURRENCIES: Final[Mapping[str, QuoteCurrency]] = MappingProxyType(
    {
        "kr.kis.mock": "KRW",
        "kr.kiwoom.mock": "KRW",
        "us.kis.mock": "USD",
        "us.kiwoom.mock": "USD",
        "us.alpaca.paper.default": "USD",
        "us.alpaca.paper.lab": "USD",
        "crypto.binance.spot_demo.canonical": "USDT",
        "crypto.binance.spot_demo.b0x_sidecar": "USDT",
        "crypto.alpaca.paper.default": "USD",
        "crypto.alpaca.paper.clean": "USD",
        "crypto.upbit.shadow": "KRW",
        "crypto.binance.futures_demo": "USDT",
    }
)

# Symbolic namespace names only; credential values are never loaded here.
LANE_CREDENTIAL_NAMESPACES: Final[Mapping[str, str | None]] = MappingProxyType(
    {
        "kr.kis.mock": "KIS_MOCK_*",
        "kr.kiwoom.mock": "KIWOOM_MOCK_*",
        "us.kis.mock": "KIS_MOCK_*",
        "us.kiwoom.mock": "KIWOOM_MOCK_US_*",
        "us.alpaca.paper.default": "ALPACA_PAPER_*",
        "us.alpaca.paper.lab": "ALPACA_PAPER_LAB_*",
        "crypto.binance.spot_demo.canonical": "BINANCE_SPOT_DEMO_API_*",
        "crypto.binance.spot_demo.b0x_sidecar": "BINANCE_SPOT_DEMO_API_*",
        "crypto.alpaca.paper.default": "ALPACA_PAPER_*",
        "crypto.alpaca.paper.clean": "ALPACA_PAPER_CRYPTO_*",
        "crypto.upbit.shadow": None,
        "crypto.binance.futures_demo": "BINANCE_FUTURES_DEMO_API_*",
    }
)

LANE_ALLOWED_HOSTS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "kr.kis.mock": ("openapivts.koreainvestment.com:29443",),
        "kr.kiwoom.mock": ("mockapi.kiwoom.com",),
        "us.kis.mock": ("openapivts.koreainvestment.com:29443",),
        "us.kiwoom.mock": ("mockapi.kiwoom.com",),
        "us.alpaca.paper.default": ("paper-api.alpaca.markets",),
        "us.alpaca.paper.lab": ("paper-api.alpaca.markets",),
        "crypto.binance.spot_demo.canonical": ("demo-api.binance.com",),
        "crypto.binance.spot_demo.b0x_sidecar": ("demo-api.binance.com",),
        "crypto.alpaca.paper.default": ("paper-api.alpaca.markets",),
        "crypto.alpaca.paper.clean": ("paper-api.alpaca.markets",),
        "crypto.upbit.shadow": (),
        "crypto.binance.futures_demo": ("demo-fapi.binance.com",),
    }
)

_FORBIDDEN_LIVE_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "openapi.koreainvestment.com",
        "openapi.koreainvestment.com:9443",
        "api.kiwoom.com",
        "api.alpaca.markets",
        "data.alpaca.markets",
        "api.binance.com",
        "fapi.binance.com",
        "api.upbit.com",
    }
)

_MISSING_REQUIRED_BINDINGS: Final[tuple[MissingBinding, ...]] = (
    MissingBinding.PHYSICAL_ACCOUNT_FINGERPRINT,
    MissingBinding.POLICY,
    MissingBinding.CAP,
    MissingBinding.OWNER,
    MissingBinding.CANARY,
)

_SIGNED_WRITER_FALSE_LANES: Final[frozenset[str]] = frozenset(
    {
        "crypto.alpaca.paper.default",
        "crypto.alpaca.paper.clean",
        "crypto.binance.spot_demo.b0x_sidecar",
        "crypto.binance.futures_demo",
    }
)
_NON_AUTONOMOUS_ROLES: Final[frozenset[RegistryRole | None]] = frozenset(
    {None, RegistryRole.BROKER_REGRESSION, RegistryRole.SHADOW_ONLY}
)
_SIGNED_LANE_STATUS_ALLOWLISTS: Final[Mapping[str, frozenset[LaneStatus]]] = (
    MappingProxyType(
        {
            "us.alpaca.paper.default": frozenset(
                {LaneStatus.AUTO_READY_BLOCKED_BY_POLICY}
            ),
            "us.alpaca.paper.lab": frozenset(
                {LaneStatus.AUTO_READY_BLOCKED_BY_LIFECYCLE}
            ),
            "us.kis.mock": frozenset({LaneStatus.NOT_READY}),
            "us.kiwoom.mock": frozenset({LaneStatus.NOT_READY}),
            "crypto.binance.spot_demo.canonical": frozenset(
                {
                    LaneStatus.NOT_READY,
                    LaneStatus.AUTO_READY_BLOCKED_BY_POLICY,
                    LaneStatus.AUTO_READY_BLOCKED_BY_SCHEDULER,
                }
            ),
            "crypto.binance.spot_demo.b0x_sidecar": frozenset(
                {LaneStatus.OBSERVATION_TEMPORARY}
            ),
            "crypto.alpaca.paper.default": frozenset({LaneStatus.NOT_READY}),
            "crypto.alpaca.paper.clean": frozenset({LaneStatus.NOT_READY}),
            "crypto.upbit.shadow": frozenset({LaneStatus.SHADOW_ONLY}),
            "crypto.binance.futures_demo": frozenset({LaneStatus.DISABLED_NO_STRATEGY}),
        }
    )
)


@dataclass(frozen=True, slots=True)
class LaneRegistryEntry:
    """One preserved logical lane and its current physical-binding evidence."""

    lane_id: str
    market: str
    broker: str
    account_profile: str
    profile_variant: str | None
    account_mode: AccountMode
    lane_type: AccountMode
    quote_currency: QuoteCurrency
    role: RegistryRole | None
    role_pending_reason: str | None
    role_on_policy_approval: RegistryRole | None
    lane_status: LaneStatus
    activation_status: ActivationStatus
    activation_reason: str
    policy_binding: PolicyBinding | None
    execution_mode: str | None
    scheduler_owner: SchedulerOwner | None
    timing_owner: str | None
    writer: bool
    auto_order_enabled: bool
    max_order_notional: Decimal | None
    max_orders_per_session: int | None
    max_open_orders: int | None
    allowed_order_types: tuple[str, ...]
    allowed_time_in_force: tuple[str, ...]
    endpoint_class: EndpointClass
    reconcile_required: bool | None
    credential_namespace: str | None
    allowed_hosts: tuple[str, ...]
    physical_account_id: str | None = field(repr=False)
    identity_status: str
    fingerprint_evidence_ref: str | None = field(repr=False)
    canary_binding: str | None
    missing_bindings: tuple[MissingBinding, ...]

    @property
    def auto(self) -> bool:
        """Exact short name used by the unknown-identity safety rule."""

        return self.auto_order_enabled


def _canonical_entry(
    lane_id: str,
    *,
    market: str,
    broker: str,
    account_profile: str,
    profile_variant: str | None,
    account_mode: AccountMode,
    role: RegistryRole | None,
    role_pending_reason: str | None = None,
    role_on_policy_approval: RegistryRole | None = None,
    lane_status: LaneStatus,
    activation_status: ActivationStatus,
    scheduler_owner: SchedulerOwner | None = None,
) -> LaneRegistryEntry:
    return LaneRegistryEntry(
        lane_id=lane_id,
        market=market,
        broker=broker,
        account_profile=account_profile,
        profile_variant=profile_variant,
        account_mode=account_mode,
        lane_type=account_mode,
        quote_currency=LANE_QUOTE_CURRENCIES[lane_id],
        role=role,
        role_pending_reason=role_pending_reason,
        role_on_policy_approval=role_on_policy_approval,
        lane_status=lane_status,
        activation_status=activation_status,
        activation_reason=lane_status.value,
        policy_binding=None,
        execution_mode=None,
        scheduler_owner=scheduler_owner,
        timing_owner=None,
        writer=False,
        auto_order_enabled=False,
        max_order_notional=None,
        max_orders_per_session=None,
        max_open_orders=None,
        allowed_order_types=(),
        allowed_time_in_force=(),
        endpoint_class=EndpointClass(account_mode.value),
        reconcile_required=None,
        credential_namespace=LANE_CREDENTIAL_NAMESPACES[lane_id],
        allowed_hosts=LANE_ALLOWED_HOSTS[lane_id],
        physical_account_id=None,
        identity_status="UNKNOWN",
        fingerprint_evidence_ref=None,
        canary_binding=None,
        missing_bindings=_MISSING_REQUIRED_BINDINGS,
    )


_BASE_CANONICAL_LANE_REGISTRY: Final[tuple[LaneRegistryEntry, ...]] = (
    _canonical_entry(
        "kr.kis.mock",
        market="kr",
        broker="kis",
        account_profile="mock",
        profile_variant=None,
        account_mode=AccountMode.MOCK,
        role=RegistryRole.AUTO_MIRROR,
        lane_status=LaneStatus.OBSERVATION_TEMPORARY,
        activation_status=ActivationStatus.BLOCKED,
    ),
    _canonical_entry(
        "kr.kiwoom.mock",
        market="kr",
        broker="kiwoom",
        account_profile="mock",
        profile_variant=None,
        account_mode=AccountMode.MOCK,
        role=RegistryRole.PRIMARY_AUTO,
        lane_status=LaneStatus.NOT_READY,
        activation_status=ActivationStatus.BLOCKED,
    ),
    _canonical_entry(
        "us.kis.mock",
        market="us",
        broker="kis",
        account_profile="mock",
        profile_variant=None,
        account_mode=AccountMode.MOCK,
        role=RegistryRole.AUTO_MIRROR,
        lane_status=LaneStatus.NOT_READY,
        activation_status=ActivationStatus.BLOCKED,
    ),
    _canonical_entry(
        "us.kiwoom.mock",
        market="us",
        broker="kiwoom",
        account_profile="mock",
        profile_variant=None,
        account_mode=AccountMode.MOCK,
        role=RegistryRole.BROKER_REGRESSION,
        lane_status=LaneStatus.NOT_READY,
        activation_status=ActivationStatus.BLOCKED,
    ),
    _canonical_entry(
        "us.alpaca.paper.default",
        market="us",
        broker="alpaca",
        account_profile="paper",
        profile_variant="default",
        account_mode=AccountMode.PAPER,
        role=RegistryRole.PRIMARY_AUTO,
        lane_status=LaneStatus.AUTO_READY_BLOCKED_BY_POLICY,
        activation_status=ActivationStatus.BLOCKED,
    ),
    _canonical_entry(
        "us.alpaca.paper.lab",
        market="us",
        broker="alpaca",
        account_profile="paper",
        profile_variant="lab",
        account_mode=AccountMode.PAPER,
        role=None,
        role_pending_reason="policy_absent",
        role_on_policy_approval=RegistryRole.AUTO_CHALLENGER,
        lane_status=LaneStatus.AUTO_READY_BLOCKED_BY_LIFECYCLE,
        activation_status=ActivationStatus.BLOCKED,
    ),
    _canonical_entry(
        "crypto.binance.spot_demo.canonical",
        market="crypto",
        broker="binance",
        account_profile="spot_demo",
        profile_variant="canonical",
        account_mode=AccountMode.DEMO,
        role=RegistryRole.PRIMARY_AUTO,
        lane_status=LaneStatus.NOT_READY,
        activation_status=ActivationStatus.BLOCKED,
        scheduler_owner=SchedulerOwner.DISABLED,
    ),
    _canonical_entry(
        "crypto.binance.spot_demo.b0x_sidecar",
        market="crypto",
        broker="binance",
        account_profile="spot_demo",
        profile_variant="b0x_sidecar",
        account_mode=AccountMode.DEMO,
        role=RegistryRole.SHADOW_ONLY,
        lane_status=LaneStatus.OBSERVATION_TEMPORARY,
        activation_status=ActivationStatus.DISABLED,
        scheduler_owner=SchedulerOwner.DISABLED,
    ),
    _canonical_entry(
        "crypto.alpaca.paper.default",
        market="crypto",
        broker="alpaca",
        account_profile="paper",
        profile_variant="default",
        account_mode=AccountMode.PAPER,
        role=RegistryRole.AUTO_MIRROR,
        lane_status=LaneStatus.NOT_READY,
        activation_status=ActivationStatus.DISABLED,
    ),
    _canonical_entry(
        "crypto.alpaca.paper.clean",
        market="crypto",
        broker="alpaca",
        account_profile="paper",
        profile_variant="clean",
        account_mode=AccountMode.PAPER,
        role=RegistryRole.AUTO_MIRROR,
        lane_status=LaneStatus.NOT_READY,
        activation_status=ActivationStatus.DISABLED,
    ),
    _canonical_entry(
        "crypto.upbit.shadow",
        market="crypto",
        broker="upbit",
        account_profile="shadow",
        profile_variant=None,
        account_mode=AccountMode.SHADOW,
        role=RegistryRole.SHADOW_ONLY,
        lane_status=LaneStatus.SHADOW_ONLY,
        activation_status=ActivationStatus.DISABLED,
    ),
    _canonical_entry(
        "crypto.binance.futures_demo",
        market="crypto",
        broker="binance",
        account_profile="futures_demo",
        profile_variant=None,
        account_mode=AccountMode.DEMO,
        role=None,
        lane_status=LaneStatus.DISABLED_NO_STRATEGY,
        activation_status=ActivationStatus.DISABLED,
        scheduler_owner=SchedulerOwner.DISABLED,
    ),
)


_BINANCE_DEMO_IDENTITY_LANE_IDS: Final[tuple[str, ...]] = (
    "crypto.binance.spot_demo.canonical",
    "crypto.binance.spot_demo.b0x_sidecar",
    "crypto.binance.futures_demo",
)
_BINANCE_DEMO_SHARED_PHYSICAL_ACCOUNT_ID: Final[str] = (
    "binance_demo:spot_plus_futures:credential_fingerprint="
    "sha256:e33925948f2cb6e03842cca9967b70f11f9242bc5c8f99c69ce0ca5cbc4d73df:"
    "one_shared_domain"
)
_BINANCE_DEMO_FINGERPRINT_EVIDENCE_REF: Final[str] = (
    "d2-phasea-20260817:impl="
    "sha256:44a9a5b4059c176eb8300d23048cd396daa77d6400faa3be8bbaf7c465d6ee82;"
    "verify=sha256:03cfae4c8a9193ce0aa8ef4803d7e4ff3190eca1b6de777862b44d410a498e21"
)


def _apply_binance_demo_identity_amendment(
    entries: tuple[LaneRegistryEntry, ...],
) -> tuple[LaneRegistryEntry, ...]:
    """Apply the signed three-field J2A identity binding to its effective view."""

    if tuple(entry.lane_id for entry in entries) != CANONICAL_LANE_IDS:
        raise RuntimeError("binance_demo_identity_base_registry_mismatch")

    base_by_id = {entry.lane_id: entry for entry in entries}
    if set(_BINANCE_DEMO_IDENTITY_LANE_IDS) - set(base_by_id):
        raise RuntimeError("binance_demo_identity_lane_missing")

    for lane_id in _BINANCE_DEMO_IDENTITY_LANE_IDS:
        entry = base_by_id[lane_id]
        if (
            entry.physical_account_id is not None
            or entry.identity_status != "UNKNOWN"
            or entry.fingerprint_evidence_ref is not None
            or MissingBinding.PHYSICAL_ACCOUNT_FINGERPRINT not in entry.missing_bindings
        ):
            raise RuntimeError("binance_demo_identity_base_binding_mismatch")

    return tuple(
        replace(
            entry,
            physical_account_id=_BINANCE_DEMO_SHARED_PHYSICAL_ACCOUNT_ID,
            identity_status="KNOWN",
            fingerprint_evidence_ref=_BINANCE_DEMO_FINGERPRINT_EVIDENCE_REF,
            missing_bindings=tuple(
                binding
                for binding in entry.missing_bindings
                if binding is not MissingBinding.PHYSICAL_ACCOUNT_FINGERPRINT
            ),
        )
        if entry.lane_id in _BINANCE_DEMO_IDENTITY_LANE_IDS
        else entry
        for entry in entries
    )


CANONICAL_LANE_REGISTRY: Final[tuple[LaneRegistryEntry, ...]] = (
    _apply_binance_demo_identity_amendment(_BASE_CANONICAL_LANE_REGISTRY)
)

_CANONICAL_BY_ID: Final[Mapping[str, LaneRegistryEntry]] = MappingProxyType(
    {entry.lane_id: entry for entry in CANONICAL_LANE_REGISTRY}
)

_IMMUTABLE_IDENTITY_FIELDS: Final[tuple[str, ...]] = (
    "market",
    "broker",
    "account_profile",
    "profile_variant",
    "account_mode",
    "lane_type",
    "quote_currency",
    "role",
    "role_pending_reason",
    "role_on_policy_approval",
    "endpoint_class",
    "credential_namespace",
    "allowed_hosts",
)


@dataclass(frozen=True, slots=True, order=True)
class RegistryIssue:
    code: str
    lane_ids: tuple[str, ...]
    detail: str


class RegistryStartupError(RuntimeError):
    """Raised before runtime construction when registry invariants fail."""

    def __init__(self, issues: Iterable[RegistryIssue]) -> None:
        self.issues = tuple(sorted(issues))
        message = "; ".join(
            f"{issue.code}[{','.join(issue.lane_ids)}]: {issue.detail}"
            for issue in self.issues
        )
        super().__init__(message)


class LaneGuardError(RuntimeError):
    """Fail-closed pre-I/O rejection with a stable machine code."""

    def __init__(self, code: str, *, lane_id: str | None = None) -> None:
        self.code = code
        self.lane_id = lane_id
        lane_suffix = "" if lane_id is None else f": lane={lane_id}"
        super().__init__(f"{code}{lane_suffix}")


class ActivationTransitionBlocked(LaneGuardError):
    """An activation transition rejected by one exact amendment guard."""

    def __init__(self, code: str, *, lane_id: str, guard_id: str) -> None:
        self.guard_id = guard_id
        super().__init__(code, lane_id=lane_id)


def mask_account_identifier(value: str | None) -> str | None:
    """Return a non-reversible display value without exposing any identifier part."""

    return None if value is None else "[MASKED]"


def get_lane_registry_entry(lane_id: str) -> LaneRegistryEntry:
    """Return blocked/disabled rows too; missing bindings never delete a lane."""

    try:
        return _CANONICAL_BY_ID[lane_id]
    except KeyError as exc:
        raise LaneGuardError("unknown_lane", lane_id=lane_id) from exc


def _violates_signed_lane_restriction(entry: LaneRegistryEntry) -> bool:
    enabled_or_autonomous = (
        entry.writer
        or entry.auto
        or entry.activation_status is ActivationStatus.ENABLED
    )
    if entry.lane_id in _SIGNED_WRITER_FALSE_LANES and enabled_or_autonomous:
        return True
    if entry.role in _NON_AUTONOMOUS_ROLES and enabled_or_autonomous:
        return True
    allowed_statuses = _SIGNED_LANE_STATUS_ALLOWLISTS.get(entry.lane_id)
    if allowed_statuses is None:
        return False
    return (
        entry.lane_status not in allowed_statuses
        or entry.activation_status is ActivationStatus.ENABLED
    )


def _entry_issues(entry: LaneRegistryEntry) -> list[RegistryIssue]:
    issues: list[RegistryIssue] = []
    lane_ids = (entry.lane_id,)
    expected_lane_id = ".".join(
        part
        for part in (
            entry.market,
            entry.broker,
            entry.account_profile,
            entry.profile_variant,
        )
        if part is not None
    )
    if entry.lane_id != expected_lane_id:
        issues.append(
            RegistryIssue(
                "lane_id_components_mismatch",
                lane_ids,
                "market.broker.profile[.variant] must reconstruct lane_id",
            )
        )
    if entry.quote_currency not in {"KRW", "USD", "USDT"}:
        issues.append(
            RegistryIssue("invalid_quote_currency", lane_ids, "allowed: KRW|USD|USDT")
        )
    if not isinstance(entry.role, RegistryRole | type(None)):
        issues.append(
            RegistryIssue("role_not_single_value", lane_ids, "role must be scalar")
        )
    if entry.role is None and entry.role_pending_reason is not None:
        if not entry.role_pending_reason.strip():
            issues.append(
                RegistryIssue(
                    "blank_role_pending_reason", lane_ids, "reason must be non-blank"
                )
            )
    if entry.role is None and entry.role_on_policy_approval is not None:
        if entry.role_pending_reason is None:
            issues.append(
                RegistryIssue(
                    "role_future_reference_without_reason",
                    lane_ids,
                    "a future role reference requires a pending reason",
                )
            )
    if entry.role is not None and (
        entry.role_pending_reason is not None
        or entry.role_on_policy_approval is not None
    ):
        issues.append(
            RegistryIssue(
                "active_role_has_pending_fields",
                lane_ids,
                "pending fields apply only while role is null",
            )
        )
    valid_account_mode = isinstance(entry.account_mode, AccountMode)
    valid_endpoint_class = isinstance(entry.endpoint_class, EndpointClass)
    valid_lane_type = isinstance(entry.lane_type, AccountMode)
    if not valid_account_mode:
        code = (
            "live_account_mode_forbidden"
            if str(entry.account_mode).lower() == "live"
            else "invalid_account_mode"
        )
        issues.append(
            RegistryIssue(code, lane_ids, "only mock|paper|demo|shadow are allowed")
        )
    if not valid_endpoint_class:
        issues.append(
            RegistryIssue(
                "invalid_endpoint_class",
                lane_ids,
                "only mock|paper|demo|shadow are allowed",
            )
        )
    if not valid_lane_type:
        issues.append(
            RegistryIssue(
                "invalid_lane_type",
                lane_ids,
                "lane_type must use the non-live account-mode vocabulary",
            )
        )
    if valid_account_mode and valid_endpoint_class:
        if entry.account_mode.value != entry.endpoint_class.value:
            issues.append(
                RegistryIssue(
                    "endpoint_class_mode_mismatch",
                    lane_ids,
                    "endpoint class and non-live account mode must agree",
                )
            )
    if valid_account_mode and valid_lane_type:
        if entry.lane_type is not entry.account_mode:
            issues.append(
                RegistryIssue(
                    "lane_type_mode_mismatch",
                    lane_ids,
                    "lane type and account mode must agree",
                )
            )
    valid_scheduler_owner = isinstance(
        entry.scheduler_owner, SchedulerOwner | type(None)
    )
    if not valid_scheduler_owner:
        issues.append(
            RegistryIssue(
                "invalid_scheduler_owner",
                lane_ids,
                "scheduler_owner must be SchedulerOwner or null",
            )
        )
    valid_timing_owner = entry.timing_owner is None or (
        isinstance(entry.timing_owner, str) and bool(entry.timing_owner.strip())
    )
    if not valid_timing_owner:
        issues.append(
            RegistryIssue(
                "invalid_timing_owner",
                lane_ids,
                "timing_owner must be a nonblank string or null",
            )
        )
    if (
        valid_scheduler_owner
        and valid_timing_owner
        and entry.scheduler_owner is not None
        and entry.timing_owner is not None
        and entry.scheduler_owner.value == entry.timing_owner
    ):
        issues.append(
            RegistryIssue(
                "invalid_timing_owner",
                lane_ids,
                "scheduler_owner and timing_owner are distinct bindings",
            )
        )
    if entry.policy_binding is not None and not isinstance(
        entry.policy_binding, PolicyBinding
    ):
        issues.append(
            RegistryIssue(
                "lane_binding_incomplete",
                lane_ids,
                "policy_binding must be a complete PolicyBinding",
            )
        )
    if entry.allowed_hosts != tuple(dict.fromkeys(entry.allowed_hosts)):
        issues.append(
            RegistryIssue(
                "duplicate_allowed_host", lane_ids, "allowed hosts must be unique"
            )
        )
    forbidden_hosts = sorted(set(entry.allowed_hosts) & _FORBIDDEN_LIVE_HOSTS)
    if forbidden_hosts:
        issues.append(
            RegistryIssue(
                "live_host_in_allowlist",
                lane_ids,
                "live hosts cannot be registered",
            )
        )
    identity_unknown = (
        entry.physical_account_id is None
        or entry.fingerprint_evidence_ref is None
        or entry.identity_status == "UNKNOWN"
    )
    if identity_unknown and not (
        entry.physical_account_id is None
        and entry.fingerprint_evidence_ref is None
        and entry.identity_status == "UNKNOWN"
        and entry.writer is False
        and entry.auto is False
    ):
        issues.append(
            RegistryIssue(
                "unknown_identity_must_be_safe",
                lane_ids,
                "requires null/UNKNOWN with writer=false and auto=false",
            )
        )
    identity_evidence_values = (
        entry.physical_account_id,
        entry.fingerprint_evidence_ref,
        entry.identity_status,
    )
    if not identity_unknown and not all(
        isinstance(value, str) and value.strip() for value in identity_evidence_values
    ):
        issues.append(
            RegistryIssue(
                "physical_account_identity_blank",
                lane_ids,
                "identity evidence fields must be non-blank",
            )
        )
    if entry.missing_bindings:
        if entry.activation_status not in {
            ActivationStatus.BLOCKED,
            ActivationStatus.DISABLED,
        }:
            issues.append(
                RegistryIssue(
                    "missing_binding_not_blocked",
                    lane_ids,
                    "missing bindings require blocked or disabled activation",
                )
            )
        if not entry.activation_reason.strip():
            issues.append(
                RegistryIssue(
                    "missing_binding_reason_absent",
                    lane_ids,
                    "blocked or disabled rows require a reason",
                )
            )
    if entry.max_order_notional is not None and entry.max_order_notional <= 0:
        issues.append(
            RegistryIssue(
                "invalid_max_order_notional",
                lane_ids,
                "a supplied cap must be positive",
            )
        )
    for field_name, value in (
        ("max_orders_per_session", entry.max_orders_per_session),
        ("max_open_orders", entry.max_open_orders),
    ):
        if value is not None and value <= 0:
            issues.append(
                RegistryIssue(
                    f"invalid_{field_name}",
                    lane_ids,
                    "a supplied cap must be positive",
                )
            )
    if entry.auto:
        required = (
            entry.writer,
            not identity_unknown,
            not entry.missing_bindings,
            entry.policy_binding is not None,
            entry.scheduler_owner not in {None, SchedulerOwner.DISABLED},
            entry.max_order_notional is not None,
            entry.max_orders_per_session is not None,
            entry.max_open_orders is not None,
            bool(entry.allowed_order_types),
            bool(entry.allowed_time_in_force),
            entry.reconcile_required is True,
            entry.canary_binding is not None,
            entry.credential_namespace is not None,
            bool(entry.allowed_hosts),
        )
        if not all(required):
            issues.append(
                RegistryIssue(
                    "auto_lane_missing_required_binding",
                    lane_ids,
                    "auto cannot be enabled without every signed binding",
                )
            )
    if _violates_signed_lane_restriction(entry):
        issues.append(
            RegistryIssue(
                "lane_signed_restriction_violation",
                lane_ids,
                "signed lane writer, role, status, and activation facts are immutable",
            )
        )
    return issues


def _single_writer_issues(
    entries: Iterable[LaneRegistryEntry],
) -> list[RegistryIssue]:
    writers_by_account: defaultdict[str, list[str]] = defaultdict(list)
    for entry in entries:
        if entry.writer and entry.physical_account_id is not None:
            writers_by_account[entry.physical_account_id].append(entry.lane_id)
    return [
        RegistryIssue(
            "physical_account_writer_conflict",
            tuple(sorted(lane_ids)),
            "more than one lane claims writer ownership for [MASKED]",
        )
        for lane_ids in writers_by_account.values()
        if len(lane_ids) > 1
    ]


def assert_single_writer(entries: Iterable[LaneRegistryEntry]) -> None:
    """Fail startup when two lanes claim the same physical-account writer."""

    issues = _single_writer_issues(tuple(entries))
    if issues:
        raise RegistryStartupError(issues)


def assert_registry_startup(
    entries: Iterable[LaneRegistryEntry], *, require_canonical: bool = False
) -> None:
    """Validate registry structure and writer cardinality before client startup."""

    materialized = tuple(entries)
    issues: list[RegistryIssue] = []
    lane_ids = tuple(entry.lane_id for entry in materialized)
    if len(set(lane_ids)) != len(lane_ids):
        issues.append(
            RegistryIssue(
                "duplicate_lane_id", tuple(sorted(lane_ids)), "lane ids must be unique"
            )
        )
    if require_canonical and lane_ids != CANONICAL_LANE_IDS:
        issues.append(
            RegistryIssue(
                "canonical_lane_ids_mismatch",
                lane_ids,
                "the 12 canonical lane ids and order are immutable",
            )
        )
    for entry in materialized:
        issues.extend(_entry_issues(entry))
        if require_canonical:
            expected_currency = LANE_QUOTE_CURRENCIES.get(entry.lane_id)
            if expected_currency != entry.quote_currency:
                issues.append(
                    RegistryIssue(
                        "lane_quote_currency_mismatch",
                        (entry.lane_id,),
                        "canonical registry currency differs",
                    )
                )
            expected_namespace = LANE_CREDENTIAL_NAMESPACES.get(entry.lane_id)
            if expected_namespace != entry.credential_namespace:
                issues.append(
                    RegistryIssue(
                        "canonical_credential_namespace_mismatch",
                        (entry.lane_id,),
                        "credential namespace differs from repository evidence",
                    )
                )
            expected_hosts = LANE_ALLOWED_HOSTS.get(entry.lane_id)
            if expected_hosts != entry.allowed_hosts:
                issues.append(
                    RegistryIssue(
                        "canonical_host_allowlist_mismatch",
                        (entry.lane_id,),
                        "host allowlist differs from repository evidence",
                    )
                )
            canonical_entry = _CANONICAL_BY_ID.get(entry.lane_id)
            if canonical_entry is not None and any(
                getattr(entry, field_name) != getattr(canonical_entry, field_name)
                for field_name in _IMMUTABLE_IDENTITY_FIELDS
            ):
                issues.append(
                    RegistryIssue(
                        "canonical_lane_identity_mismatch",
                        (entry.lane_id,),
                        "one or more of the 13 immutable identity fields differ",
                    )
                )
    issues.extend(_single_writer_issues(materialized))
    if issues:
        raise RegistryStartupError(issues)


@dataclass(frozen=True, slots=True)
class ActivationEvidence:
    """Caller-supplied evidence; defaults do not authorize advancement."""

    directly_proven_cadence_preserved: bool = False
    requires_new_recurring_schedule: bool = False
    shared_production_release_required: bool = False
    live_restart_required: bool = False
    j8_canary_succeeded: bool = False


def transition_activation(
    lane_id: str,
    current: ActivationStatus,
    target: ActivationStatus,
    *,
    evidence: ActivationEvidence = ActivationEvidence(),
) -> ActivationStatus:
    """Apply G1-G3 without selecting cadence, canary, or release behavior."""

    canonical_entry = _CANONICAL_BY_ID.get(lane_id)
    if (
        target is ActivationStatus.ENABLED
        and canonical_entry is not None
        and (
            lane_id in _SIGNED_WRITER_FALSE_LANES
            or lane_id in _SIGNED_LANE_STATUS_ALLOWLISTS
            or canonical_entry.role in _NON_AUTONOMOUS_ROLES
        )
    ):
        raise ActivationTransitionBlocked(
            "lane_signed_restriction_violation",
            lane_id=lane_id,
            guard_id="B2",
        )

    if current is ActivationStatus.READY_FOR_MOCK_DEPLOYMENT:
        if target is not current:
            raise ActivationTransitionBlocked(
                "ready_for_mock_deployment_terminal",
                lane_id=lane_id,
                guard_id="G2",
            )
        return current

    release_stop = (
        evidence.shared_production_release_required or evidence.live_restart_required
    )
    if release_stop:
        if target is not ActivationStatus.READY_FOR_MOCK_DEPLOYMENT:
            raise ActivationTransitionBlocked(
                "ready_for_mock_deployment_required",
                lane_id=lane_id,
                guard_id="G2",
            )
        return target

    if current is ActivationStatus.RUNTIME_ACCEPTANCE_PENDING:
        if target is ActivationStatus.ENABLED:
            raise ActivationTransitionBlocked(
                "j8_canary_cannot_auto_enable", lane_id=lane_id, guard_id="G3"
            )
        if target is ActivationStatus.READY and not evidence.j8_canary_succeeded:
            raise ActivationTransitionBlocked(
                "j8_canary_evidence_required", lane_id=lane_id, guard_id="G3"
            )
    if evidence.j8_canary_succeeded and not (
        current is ActivationStatus.RUNTIME_ACCEPTANCE_PENDING
        and target is ActivationStatus.READY
    ):
        raise ActivationTransitionBlocked(
            "j8_canary_transition_scope_violation",
            lane_id=lane_id,
            guard_id="G3",
        )

    if target is ActivationStatus.ENABLED:
        if evidence.requires_new_recurring_schedule:
            raise ActivationTransitionBlocked(
                "AUTO_READY_BLOCKED_BY_SCHEDULER",
                lane_id=lane_id,
                guard_id="G1",
            )
        if not evidence.directly_proven_cadence_preserved:
            raise ActivationTransitionBlocked(
                "directly_proven_cadence_required",
                lane_id=lane_id,
                guard_id="G1",
            )
    return target


class LaneCurrencyPlan(Protocol):
    lane_id: str
    quote_currency: str


type RegistrySource = Mapping[str, LaneRegistryEntry] | Iterable[LaneRegistryEntry]


def _validated_registry(
    registry: RegistrySource | None = None,
) -> Mapping[str, LaneRegistryEntry]:
    if registry is None:
        entries = CANONICAL_LANE_REGISTRY
    elif isinstance(registry, Mapping):
        entries = tuple(registry.values())
    else:
        entries = tuple(registry)
    assert_registry_startup(entries, require_canonical=True)
    return MappingProxyType({entry.lane_id: entry for entry in entries})


def assert_lane_quote_currency(
    plan: LaneCurrencyPlan,
    registry: Mapping[str, LaneRegistryEntry] = _CANONICAL_BY_ID,
) -> LaneRegistryEntry:
    """Reject a plan/registry mismatch before any broker boundary is reached."""

    try:
        entry = registry[plan.lane_id]
    except KeyError as exc:
        raise LaneGuardError("unknown_lane", lane_id=plan.lane_id) from exc
    if plan.quote_currency != entry.quote_currency:
        raise LaneGuardError("lane_quote_currency_mismatch", lane_id=plan.lane_id)
    return entry


def assert_lineage_registry_binding(
    envelope: LineageEnvelope,
    registry: RegistrySource | None = None,
) -> LaneRegistryEntry:
    """Compare one exact J2B factory lineage to the canonical in-memory registry."""

    validated_registry = _validated_registry(registry)
    if type(envelope) is not LineageEnvelope or envelope.execution_plan is None:
        raise LaneGuardError("lane_binding_incomplete")
    plan = envelope.execution_plan
    entry = assert_lane_quote_currency(plan, validated_registry)
    if plan.broker != entry.broker:
        raise LaneGuardError("lane_broker_mismatch", lane_id=plan.lane_id)
    if plan.account_profile != entry.account_profile:
        raise LaneGuardError("lane_account_profile_mismatch", lane_id=plan.lane_id)
    if plan.account_mode != entry.account_mode.value:
        raise LaneGuardError("lane_account_mode_mismatch", lane_id=plan.lane_id)
    policy_binding = entry.policy_binding
    if (
        not isinstance(policy_binding, PolicyBinding)
        or MissingBinding.POLICY in entry.missing_bindings
    ):
        raise LaneGuardError("lane_binding_incomplete", lane_id=plan.lane_id)
    intent_policy_binding = PolicyBinding(
        envelope.decision_intent.policy_version,
        envelope.decision_intent.policy_version_hash,
    )
    if intent_policy_binding != policy_binding:
        raise LaneGuardError("lane_policy_binding_mismatch", lane_id=plan.lane_id)
    return entry


def assert_mock_only_endpoint(entry: LaneRegistryEntry, endpoint_url: str) -> str:
    """Validate one caller-declared HTTPS endpoint string against its lane."""

    if not isinstance(entry.account_mode, AccountMode) or not isinstance(
        entry.endpoint_class, EndpointClass
    ):
        raise LaneGuardError("live_account_mode_forbidden", lane_id=entry.lane_id)
    if entry.account_mode is AccountMode.SHADOW:
        raise LaneGuardError("shadow_broker_io_forbidden", lane_id=entry.lane_id)
    try:
        parsed = urlsplit(endpoint_url)
        port = parsed.port
    except ValueError as exc:
        raise LaneGuardError("endpoint_url_invalid", lane_id=entry.lane_id) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise LaneGuardError("endpoint_url_invalid", lane_id=entry.lane_id)
    host = parsed.hostname.lower()
    netloc = host if port is None else f"{host}:{port}"
    if netloc in _FORBIDDEN_LIVE_HOSTS or host in _FORBIDDEN_LIVE_HOSTS:
        raise LaneGuardError("live_endpoint_forbidden", lane_id=entry.lane_id)
    if netloc not in entry.allowed_hosts:
        raise LaneGuardError("lane_endpoint_host_mismatch", lane_id=entry.lane_id)
    return netloc


def assert_credential_namespace(
    entry: LaneRegistryEntry, credential_namespace: str
) -> None:
    """Validate one caller-declared symbolic credential namespace."""

    if entry.credential_namespace is None:
        raise LaneGuardError("credential_namespace_unbound", lane_id=entry.lane_id)
    if credential_namespace != entry.credential_namespace:
        raise LaneGuardError("credential_namespace_mismatch", lane_id=entry.lane_id)


def assert_entry_execution_ready(entry: LaneRegistryEntry) -> None:
    """Require every signed binding; registry roles never imply activation."""

    if _violates_signed_lane_restriction(entry):
        raise LaneGuardError("lane_signed_restriction_violation", lane_id=entry.lane_id)
    if entry.activation_status is not ActivationStatus.ENABLED:
        raise LaneGuardError("lane_activation_not_enabled", lane_id=entry.lane_id)
    if not entry.writer or not entry.auto:
        raise LaneGuardError("lane_writer_not_enabled", lane_id=entry.lane_id)
    physical_account_id = entry.physical_account_id
    fingerprint_evidence_ref = entry.fingerprint_evidence_ref
    if not (
        isinstance(physical_account_id, str)
        and physical_account_id.strip()
        and isinstance(fingerprint_evidence_ref, str)
        and fingerprint_evidence_ref.strip()
        and isinstance(entry.identity_status, str)
        and entry.identity_status != "UNKNOWN"
        and entry.identity_status.strip()
    ):
        raise LaneGuardError("physical_account_identity_unknown", lane_id=entry.lane_id)
    if entry.missing_bindings:
        raise LaneGuardError("lane_binding_incomplete", lane_id=entry.lane_id)
    required_bindings = (
        isinstance(entry.policy_binding, PolicyBinding),
        bool(entry.execution_mode and entry.execution_mode.strip()),
        isinstance(entry.scheduler_owner, SchedulerOwner)
        and entry.scheduler_owner is not SchedulerOwner.DISABLED,
        entry.max_order_notional is not None and entry.max_order_notional > 0,
        entry.max_orders_per_session is not None and entry.max_orders_per_session > 0,
        entry.max_open_orders is not None and entry.max_open_orders > 0,
        bool(entry.allowed_order_types),
        bool(entry.allowed_time_in_force),
        entry.reconcile_required is True,
        bool(entry.credential_namespace and entry.credential_namespace.strip()),
        bool(entry.allowed_hosts),
        bool(entry.canary_binding and entry.canary_binding.strip()),
    )
    if not all(required_bindings):
        raise LaneGuardError("lane_binding_incomplete", lane_id=entry.lane_id)


def assert_recurring_authorized(
    entry: LaneRegistryEntry,
    *,
    recurring_requested: bool,
    bounded_canary: bool = False,
) -> None:
    """Require both signed states; bounded canary evidence grants no recurrence."""

    if recurring_requested and (
        bounded_canary
        or entry.lane_status is not LaneStatus.AUTO_ENABLED
        or entry.activation_status is not ActivationStatus.ENABLED
    ):
        raise LaneGuardError("lane_recurring_not_authorized", lane_id=entry.lane_id)


async def guarded_broker_io[BrokerResult](
    envelope: LineageEnvelope,
    *,
    endpoint_url: str,
    credential_namespace: str,
    broker_io: Callable[[], Awaitable[BrokerResult]],
    registry: RegistrySource | None = None,
    recurring_requested: bool = False,
    bounded_canary: bool = False,
) -> BrokerResult:
    """Validate declared strings, then invoke an opaque caller-owned callback.

    This inert helper does not bind the declarations to the callback's actual
    transport. Each broker transport must revalidate its real host and profile.
    """

    entry = assert_lineage_registry_binding(envelope, registry)
    assert_recurring_authorized(
        entry,
        recurring_requested=recurring_requested,
        bounded_canary=bounded_canary,
    )
    assert_mock_only_endpoint(entry, endpoint_url)
    assert_credential_namespace(entry, credential_namespace)
    assert_entry_execution_ready(entry)
    return await broker_io()


def guarded_client_factory[Client](
    envelope: LineageEnvelope,
    *,
    endpoint_url: str,
    credential_namespace: str,
    factory: Callable[[], Client],
    registry: RegistrySource | None = None,
    recurring_requested: bool = False,
    bounded_canary: bool = False,
) -> Client:
    """Validate declared strings, then invoke an opaque caller-owned factory.

    This inert helper cannot prove which transport the returned client uses.
    Transport-level host and profile validation remains broker-owned.
    """

    entry = assert_lineage_registry_binding(envelope, registry)
    assert_recurring_authorized(
        entry,
        recurring_requested=recurring_requested,
        bounded_canary=bounded_canary,
    )
    assert_mock_only_endpoint(entry, endpoint_url)
    assert_credential_namespace(entry, credential_namespace)
    assert_entry_execution_ready(entry)
    return factory()


@dataclass(frozen=True, slots=True)
class LaneDivergence:
    """A lane-scoped failure record that cannot cancel or roll back peers."""

    primary_lane_id: str
    divergent_lane_id: str
    reason: str
    rollback_other_lanes: bool = field(default=False, init=False)
    cancel_other_lanes: bool = field(default=False, init=False)


def record_mirror_divergence(
    primary_lane_id: str, divergent_lane_id: str, reason: str
) -> LaneDivergence:
    """Record divergence without invoking either lane or mutating peer state."""

    get_lane_registry_entry(primary_lane_id)
    get_lane_registry_entry(divergent_lane_id)
    if primary_lane_id == divergent_lane_id:
        raise LaneGuardError(
            "divergence_requires_distinct_lanes", lane_id=divergent_lane_id
        )
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise LaneGuardError("divergence_reason_required", lane_id=divergent_lane_id)
    return LaneDivergence(primary_lane_id, divergent_lane_id, normalized_reason)


assert_registry_startup(CANONICAL_LANE_REGISTRY, require_canonical=True)


__all__ = [
    "ACTIVATION_TRANSITION_GUARDS",
    "MISSING_BINDING_RULE",
    "R2_REJECT_CODES",
    "UNKNOWN_IDENTITY_RULE",
    "AccountMode",
    "ActivationEvidence",
    "ActivationStatus",
    "ActivationTransitionBlocked",
    "CANONICAL_LANE_IDS",
    "CANONICAL_LANE_REGISTRY",
    "EndpointClass",
    "LANE_ALLOWED_HOSTS",
    "LANE_CREDENTIAL_NAMESPACES",
    "LANE_QUOTE_CURRENCIES",
    "LaneDivergence",
    "LaneGuardError",
    "LaneRegistryEntry",
    "MissingBinding",
    "PolicyBinding",
    "RegistryIssue",
    "RegistryRole",
    "RegistryStartupError",
    "assert_credential_namespace",
    "assert_entry_execution_ready",
    "assert_lane_quote_currency",
    "assert_lineage_registry_binding",
    "assert_mock_only_endpoint",
    "assert_recurring_authorized",
    "assert_registry_startup",
    "assert_single_writer",
    "get_lane_registry_entry",
    "guarded_broker_io",
    "guarded_client_factory",
    "mask_account_identifier",
    "record_mirror_divergence",
    "transition_activation",
]
