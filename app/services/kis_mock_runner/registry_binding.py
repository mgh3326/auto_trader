"""Operator-provided fingerprint binding; no credentials or activation authority."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import replace

from app.services.mock_lane_registry import (
    CANONICAL_LANE_REGISTRY,
    AccountMode,
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
    """Bind only identity. Policy, caps, canary, writer and auto remain separate.

    A fingerprint cannot authorize execution: the existing registry readiness
    checks still require every signed binding and explicit activation.
    """
    assert_registry_startup(entries, require_canonical=True)
    identity = (os.environ if environ is None else environ).get(IDENTITY_ENV, "")
    if not identity:
        return entries
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
        result.append(bound)
    assert_registry_startup(result, require_canonical=True)
    return tuple(result)
