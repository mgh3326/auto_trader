"""Approved configuration for one operator-present E3 run; no runtime wiring."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Final

from app.services.mock_lane_registry import PolicyBinding

ACTIVATION_ENV: Final = "KIS_MOCK_E3_ACTIVATION_ENABLED"


@dataclass(frozen=True, slots=True)
class KISMockE3ActivationContract:
    """The sealed E3 envelope, distinct from the broader E1 capability limits.

    These are configuration bindings. The operator must store an E1 intent with
    these bounds before the run; this module installs no route or send hook.
    Registry count fields describe the single-intent, single-run E3 scope.
    """

    version: str = "kis-mock-e3-activation/v1"
    lane_id: str = "kr.kis.mock"
    account: str = "kis_mock"
    max_quantity_per_intent: int = 1
    max_notional_krw_exclusive: int = 100_000
    max_lifetime_seconds: int = 30 * 60
    max_working_orders_per_intent: int = 1
    max_intents_per_run: int = 1
    owner: str = "E1 gateway route (POST /execution-intents/children)"
    writer: str = "gateway"
    operator_present: bool = True
    foreground_only: bool = True
    canary: str = "docs/runbooks/execution-intent-gateway.md#run-one-mock-order"

    @property
    def policy_binding(self) -> PolicyBinding:
        """Bind the exact activation envelope, not an invented trading policy."""
        digest = hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return PolicyBinding(self.version, digest)


E3_ACTIVATION_CONTRACT: Final = KISMockE3ActivationContract()
