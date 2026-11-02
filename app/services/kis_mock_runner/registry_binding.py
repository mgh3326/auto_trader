"""Mock identity and explicit, default-off, single-run E3 configuration."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal

from app.schemas.execution_contracts import SchedulerOwner
from app.services.kis_mock_runner.e3_activation import (
    ACTIVATION_ENV,
    E3_ACTIVATION_CONTRACT,
)
from app.services.mock_lane_registry import (
    CANONICAL_LANE_REGISTRY,
    AccountMode,
    ActivationStatus,
    EndpointClass,
    LaneGuardError,
    LaneRegistryEntry,
    MissingBinding,
    assert_registry_startup,
)

IDENTITY_ENV = "KIS_MOCK_PHYSICAL_ACCOUNT_FINGERPRINT"


def require_kis_mock_identity(entry: LaneRegistryEntry) -> None:
    if (
        entry.lane_id != "kr.kis.mock"
        or entry.broker != "kis"
        or entry.account_profile != "mock"
        or entry.account_mode is not AccountMode.MOCK
        or entry.endpoint_class is not EndpointClass.MOCK
        or entry.credential_namespace != "KIS_MOCK_*"
        or entry.allowed_hosts != ("openapivts.koreainvestment.com:29443",)
        or entry.identity_status != "KNOWN"
        or not re.fullmatch(r"kismock:v1:[0-9a-f]{64}", entry.physical_account_id or "")
    ):
        raise LaneGuardError(
            "kis_mock_identity_binding_required", lane_id="kr.kis.mock"
        )


def kis_mock_registry_from_environment(
    *,
    environ: Mapping[str, str] | None = None,
    entries: tuple[LaneRegistryEntry, ...] = CANONICAL_LANE_REGISTRY,
) -> tuple[LaneRegistryEntry, ...]:
    """Apply operator configuration to the KR mock row, never install a route.

    Identity alone remains blocked. Only an exact explicit activation opt-in
    supplies the approved E3 bindings; all existing readiness guards still run.
    """
    assert_registry_startup(entries, require_canonical=True)
    config = os.environ if environ is None else environ
    activated = config.get(ACTIVATION_ENV, "false") == "true"
    identity = config.get(IDENTITY_ENV, "")
    if not identity:
        if activated:
            raise LaneGuardError(
                "kis_mock_activation_identity_required", lane_id="kr.kis.mock"
            )
        return tuple(
            replace(
                entry,
                activation_status=ActivationStatus.BLOCKED,
                writer=False,
                auto_order_enabled=False,
            )
            if entry.lane_id == "kr.kis.mock"
            else entry
            for entry in entries
        )
    if not re.fullmatch(r"kismock:v1:[0-9a-f]{64}", identity):
        raise LaneGuardError(
            "kis_mock_identity_configuration_invalid", lane_id="kr.kis.mock"
        )
    result = []
    for entry in entries:
        if entry.lane_id != "kr.kis.mock":
            result.append(entry)
            continue
        if entry.physical_account_id not in (None, identity):
            raise LaneGuardError("kis_mock_identity_mismatch", lane_id=entry.lane_id)
        bound = replace(
            entry,
            physical_account_id=identity,
            identity_status="KNOWN",
            fingerprint_evidence_ref=f"env:{IDENTITY_ENV}",
            missing_bindings=tuple(
                binding
                for binding in entry.missing_bindings
                if binding is not MissingBinding.PHYSICAL_ACCOUNT_FINGERPRINT
            ),
        )
        require_kis_mock_identity(bound)
        if activated:
            contract = E3_ACTIVATION_CONTRACT
            bound = replace(
                bound,
                activation_status=ActivationStatus.ENABLED,
                activation_reason=contract.version,
                policy_binding=contract.policy_binding,
                execution_mode="operator_present_foreground_e3",
                scheduler_owner=SchedulerOwner.MANUAL,
                timing_owner=contract.owner,
                writer=True,
                auto_order_enabled=True,
                # E1 prices and notional are whole won; the strict bound's
                # largest permitted integer is one less than its exclusive end.
                max_order_notional=Decimal(contract.max_notional_krw_exclusive - 1),
                max_orders_per_session=contract.max_intents_per_run,
                max_open_orders=contract.max_working_orders_per_intent,
                allowed_order_types=("limit",),
                allowed_time_in_force=("day",),
                reconcile_required=True,
                canary_binding=contract.canary,
                missing_bindings=(),
            )
        else:
            bound = replace(
                bound,
                activation_status=ActivationStatus.BLOCKED,
                writer=False,
                auto_order_enabled=False,
            )
        result.append(bound)
    assert_registry_startup(result, require_canonical=True)
    return tuple(result)
