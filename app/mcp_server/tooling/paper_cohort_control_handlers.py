"""Production application composition for the ROB-849 kill switch."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.paper_validation_handlers import (
    ConfiguredActorRoleProvider,
    jsonable,
)
from app.services.paper_cohort.contracts import PaperCohortError, PaperCohortKillRequest
from app.services.paper_cohort.kill_switch import (
    PaperCohortCleanupPass,
    PaperCohortKillSwitch,
    PaperCohortRecoveryPass,
)
from app.services.paper_cohort.provenance import (
    CohortFrozenInputHashProvider,
    CohortPolicyHashProvider,
    PaperCohortProvenanceVerifier,
)
from app.services.paper_cohort.runner import PaperCohortRunner
from app.services.paper_validation.service import PaperValidationService

logger = logging.getLogger(__name__)


class PaperCohortControlApplication(Protocol):
    async def kill_switch(
        self, caller_id: str, request: PaperCohortKillRequest
    ) -> object: ...


ApplicationProvider = Callable[[], PaperCohortControlApplication]


class _RecoveryOnlyBoundary:
    async def capture(self, request):  # noqa: ANN001, ANN202
        del request
        raise AssertionError("kill-switch recovery must not capture")

    async def get_quote(self, venue, symbol):  # noqa: ANN001, ANN202
        del venue, symbol
        raise AssertionError("kill-switch recovery must not fetch quotes")


class DefaultPaperCohortControlApplication:
    def _verifier(
        self, session: AsyncSession, caller_id: str
    ) -> PaperCohortProvenanceVerifier:
        validation = PaperValidationService(
            session,
            actor_role_provider=ConfiguredActorRoleProvider(
                settings.PAPER_VALIDATION_ACTOR_ROLES
            ),
            frozen_input_provider=CohortFrozenInputHashProvider(session),
            policy_provider=CohortPolicyHashProvider(session),
        )
        return PaperCohortProvenanceVerifier(
            session,
            validation_service=validation,
            caller_id=caller_id,
        )

    async def kill_switch(
        self, caller_id: str, request: PaperCohortKillRequest
    ) -> object:
        actor_roles = ConfiguredActorRoleProvider(settings.PAPER_VALIDATION_ACTOR_ROLES)

        def verifier(session: AsyncSession) -> PaperCohortProvenanceVerifier:
            return self._verifier(session, caller_id)

        recovery = PaperCohortRecoveryPass(
            AsyncSessionLocal,
            runner_factory=lambda session: PaperCohortRunner(
                session,
                capture=_RecoveryOnlyBoundary(),
                quote_provider=_RecoveryOnlyBoundary(),
                verifier=verifier(session),
            ),
        )
        cleanup = PaperCohortCleanupPass(
            AsyncSessionLocal,
            verifier_factory=verifier,
        )
        service = PaperCohortKillSwitch(
            AsyncSessionLocal,
            actor_role_provider=actor_roles,
            recovery_pass=recovery.run,
            cleanup_pass=cleanup.run,
        )
        try:
            return jsonable(await service.execute(caller_id, request))
        except PaperCohortError as exc:
            return {"status": "blocked", "reason_code": exc.reason_code}
        except Exception as exc:  # noqa: BLE001 - stable MCP failure boundary
            logger.warning("paper cohort kill switch failed (%s)", type(exc).__name__)
            return {"status": "blocked", "reason_code": "kill_switch_failed"}


def default_application_provider() -> PaperCohortControlApplication:
    return DefaultPaperCohortControlApplication()


__all__ = [
    "ApplicationProvider",
    "DefaultPaperCohortControlApplication",
    "PaperCohortControlApplication",
    "default_application_provider",
]
