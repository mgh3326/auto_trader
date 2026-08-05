"""Runtime gates for the KIS mock runner.

The runner intentionally reads its small, deployment-owned environment surface
directly instead of adding a broadly available settings field.  That keeps the
new execution shell default-disabled in every existing process.
"""

from __future__ import annotations

from collections.abc import Mapping

RUNNER_ENABLED_ENV = "KIS_MOCK_RUNNER_ENABLED"
REARM_ENABLED_ENV = "KIS_MOCK_RUNNER_REARM_ENABLED"


class KISMockRunnerDisabled(RuntimeError):
    """Raised before any DB, lock, broker, or webhook work when unarmed."""


class KISMockRunnerRearmUnauthorized(RuntimeError):
    """Raised unless the operator CLI's two re-arm gates are both present."""


def is_truthy(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})


def assert_runner_enabled(environment: Mapping[str, str]) -> None:
    """Enforce the default-disabled master gate at the top of every run."""
    if not is_truthy(environment.get(RUNNER_ENABLED_ENV)):
        raise KISMockRunnerDisabled(
            f"{RUNNER_ENABLED_ENV} is not explicitly enabled; refusing runner start"
        )


def assert_rearm_authorized(environment: Mapping[str, str], *, confirm: bool) -> None:
    """Require an operator-only CLI env gate and a per-invocation confirmation."""
    if not is_truthy(environment.get(REARM_ENABLED_ENV)) or not confirm:
        raise KISMockRunnerRearmUnauthorized(
            "re-arm requires KIS_MOCK_RUNNER_REARM_ENABLED=true and --confirm"
        )
