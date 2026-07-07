"""ROB-758 Prefect wrappers for operator-host triage pollers.

The poller scripts stay outside this repo under ``~/ops`` and keep their
existing state files. These flows only execute the scripts as short-lived
subprocesses so Prefect can provide run history, retries, pause/resume, and
failure automations. Deployment registration and cadence live in
``robin-prefect-automations``.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Literal

try:  # pragma: no cover - imported at module level
    from prefect import flow, task
except ImportError:  # pragma: no cover - exercised when prefect absent

    def _identity_decorator(*args: Any, **kwargs: Any) -> Any:
        if args and callable(args[0]) and not kwargs:
            return args[0]

        def _wrap(fn: Any) -> Any:
            return fn

        return _wrap

    flow = _identity_decorator  # type: ignore[assignment]
    task = _identity_decorator  # type: ignore[assignment]


PollerKind = Literal["watch-alert", "fill-event"]

DEFAULT_POLLER_PATHS: dict[PollerKind, Path] = {
    "watch-alert": Path("~/ops/watch-alert-triage/poller.sh"),
    "fill-event": Path("~/ops/fill-event-triage/poller.sh"),
}
POLLER_PATH_ENV: dict[PollerKind, str] = {
    "watch-alert": "WATCH_ALERT_TRIAGE_POLLER",
    "fill-event": "FILL_EVENT_TRIAGE_POLLER",
}


class PollerExecutionError(RuntimeError):
    """Raised when a poller subprocess fails so Prefect marks the run failed."""

    def __init__(self, summary: dict[str, Any]) -> None:
        self.summary = summary
        kind = summary.get("kind", "unknown")
        status = summary.get("status", "failed")
        returncode = summary.get("returncode")
        super().__init__(f"{kind} poller {status} (returncode={returncode})")


def _tail(text: str | bytes | None, *, limit: int = 4000) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    return text[-limit:]


def resolve_poller_path(
    kind: PollerKind, poller_path: str | Path | None = None
) -> Path:
    if poller_path is not None:
        return Path(poller_path).expanduser()
    env_path = os.getenv(POLLER_PATH_ENV[kind])
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_POLLER_PATHS[kind].expanduser()


def run_operator_poller(
    kind: PollerKind,
    *,
    poller_path: str | Path | None = None,
    timeout_s: int = 600,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run one operator poller script and return a compact Prefect result.

    Non-zero return codes and timeouts raise ``PollerExecutionError``. That is
    intentional: Prefect failure automations should alert on the failed flow run
    instead of burying the failure in a successful result payload.
    """
    resolved_path = resolve_poller_path(kind, poller_path)
    if not resolved_path.is_file():
        raise FileNotFoundError(f"{kind} poller not found: {resolved_path}")

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    started_at = time.time()
    command = ["/bin/bash", str(resolved_path)]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        summary: dict[str, Any] = {
            "status": "timeout",
            "kind": kind,
            "poller_path": str(resolved_path),
            "command": command,
            "returncode": None,
            "duration_ms": int((time.time() - started_at) * 1000),
            "stdout_tail": _tail(exc.stdout),
            "stderr_tail": _tail(exc.stderr),
        }
        raise PollerExecutionError(summary)

    summary = {
        "status": "ok" if completed.returncode == 0 else "failed",
        "kind": kind,
        "poller_path": str(resolved_path),
        "command": command,
        "returncode": completed.returncode,
        "duration_ms": int((time.time() - started_at) * 1000),
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
    }
    if completed.returncode != 0:
        raise PollerExecutionError(summary)
    return summary


@task(name="operator_triage_poller", retries=2, retry_delay_seconds=60)
def operator_triage_poller_task(
    kind: PollerKind,
    *,
    poller_path: str | None = None,
    timeout_s: int = 600,
) -> dict[str, Any]:
    return run_operator_poller(kind, poller_path=poller_path, timeout_s=timeout_s)


@flow(name="operator-triage-poller")
def operator_triage_poller_flow(
    kind: PollerKind = "watch-alert",
    *,
    poller_path: str | None = None,
    timeout_s: int = 600,
) -> dict[str, Any]:
    return operator_triage_poller_task(
        kind,
        poller_path=poller_path,
        timeout_s=timeout_s,
    )


@flow(name="watch-alert-triage-poller")
def watch_alert_triage_poller_flow(
    *,
    poller_path: str | None = None,
    timeout_s: int = 600,
) -> dict[str, Any]:
    return operator_triage_poller_task(
        "watch-alert",
        poller_path=poller_path,
        timeout_s=timeout_s,
    )


@flow(name="fill-event-triage-poller")
def fill_event_triage_poller_flow(
    *,
    poller_path: str | None = None,
    timeout_s: int = 600,
) -> dict[str, Any]:
    return operator_triage_poller_task(
        "fill-event",
        poller_path=poller_path,
        timeout_s=timeout_s,
    )


__all__ = [
    "PollerExecutionError",
    "fill_event_triage_poller_flow",
    "operator_triage_poller_flow",
    "operator_triage_poller_task",
    "resolve_poller_path",
    "run_operator_poller",
    "watch_alert_triage_poller_flow",
]
