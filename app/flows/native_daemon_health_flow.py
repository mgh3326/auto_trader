"""ROB-758 Prefect flow for native launchd daemon health observation.

Long-running daemons remain launchd-owned. This flow is intentionally a short
health snapshot: run the existing native healthcheck script and verify the
expected launchd labels are loaded, then fail the Prefect run on degradation so
operator automations can alert.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

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


DEFAULT_LAUNCHD_LABELS: tuple[str, ...] = (
    "com.robinco.auto-trader.haproxy",
    "com.robinco.auto-trader.worker",
    "com.robinco.auto-trader.scheduler",
    "com.robinco.auto-trader.kis-websocket",
    "com.robinco.auto-trader.upbit-websocket",
    "com.robinco.auto-trader.mcp-watchdog",
)


class NativeDaemonHealthError(RuntimeError):
    """Raised when the native health snapshot is degraded."""

    def __init__(self, summary: dict[str, Any]) -> None:
        self.summary = summary
        super().__init__("native daemon health degraded")


def _tail(text: str | bytes | None, *, limit: int = 4000) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    return text[-limit:]


def _default_base_dir() -> Path:
    return Path(os.getenv("AUTO_TRADER_BASE", "~/services/auto_trader")).expanduser()


def run_native_daemon_healthcheck(
    *,
    base_dir: str | Path | None = None,
    healthcheck_script: str | Path | None = None,
    labels: Sequence[str] = DEFAULT_LAUNCHD_LABELS,
    uid: int | None = None,
    timeout_s: int = 30,
    run=subprocess.run,
) -> dict[str, Any]:
    """Run one native daemon health snapshot.

    ``healthcheck-native.sh`` owns HTTP and websocket heartbeat checks. The
    launchd label pass covers the singleton daemons where liveness is primarily
    launchd's responsibility. Degraded snapshots raise so Prefect run-failure
    automations can page the operator.
    """
    resolved_base = (
        Path(base_dir).expanduser() if base_dir is not None else _default_base_dir()
    )
    script = (
        Path(healthcheck_script).expanduser()
        if healthcheck_script is not None
        else resolved_base / "scripts" / "healthcheck-native.sh"
    )
    if not script.is_file():
        raise FileNotFoundError(f"native healthcheck script not found: {script}")

    work_dir = resolved_base / "current"
    if not work_dir.is_dir():
        work_dir = resolved_base

    resolved_uid = os.getuid() if uid is None else uid
    env = os.environ.copy()
    env["AUTO_TRADER_BASE"] = str(resolved_base)

    started_at = time.time()
    health = run(
        ["/bin/bash", str(script)],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
        cwd=work_dir,
    )

    launchd: list[dict[str, Any]] = []
    for label in labels:
        target = f"gui/{resolved_uid}/{label}"
        status = run(
            ["launchctl", "print", target],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        launchd.append(
            {
                "label": label,
                "target": target,
                "status": "loaded" if status.returncode == 0 else "missing",
                "returncode": status.returncode,
                "stderr_tail": _tail(status.stderr),
            }
        )

    degraded = health.returncode != 0 or any(
        item["status"] != "loaded" for item in launchd
    )
    summary = {
        "status": "degraded" if degraded else "ok",
        "base_dir": str(resolved_base),
        "work_dir": str(work_dir),
        "duration_ms": int((time.time() - started_at) * 1000),
        "healthcheck": {
            "script": str(script),
            "returncode": health.returncode,
            "stdout_tail": _tail(health.stdout),
            "stderr_tail": _tail(health.stderr),
        },
        "launchd": launchd,
    }
    if degraded:
        raise NativeDaemonHealthError(summary)
    return summary


@task(name="native_daemon_healthcheck", retries=1, retry_delay_seconds=60)
def native_daemon_healthcheck_task(
    *,
    base_dir: str | None = None,
    healthcheck_script: str | None = None,
    labels: list[str] | None = None,
    timeout_s: int = 30,
) -> dict[str, Any]:
    return run_native_daemon_healthcheck(
        base_dir=base_dir,
        healthcheck_script=healthcheck_script,
        labels=tuple(labels) if labels is not None else DEFAULT_LAUNCHD_LABELS,
        timeout_s=timeout_s,
    )


@flow(name="native-daemon-health")
def native_daemon_health_flow(
    *,
    base_dir: str | None = None,
    healthcheck_script: str | None = None,
    labels: list[str] | None = None,
    timeout_s: int = 30,
) -> dict[str, Any]:
    """Native daemon observation flow; deployment lives out-of-repo."""
    return native_daemon_healthcheck_task(
        base_dir=base_dir,
        healthcheck_script=healthcheck_script,
        labels=labels,
        timeout_s=timeout_s,
    )


__all__ = [
    "DEFAULT_LAUNCHD_LABELS",
    "NativeDaemonHealthError",
    "native_daemon_health_flow",
    "native_daemon_healthcheck_task",
    "run_native_daemon_healthcheck",
]
