"""ROB-758 Prefect automation flow tests.

Prefect is an operator-host runtime dependency. These tests exercise the
prefect-free function bodies and pin the static activation boundary: no in-repo
deployment schedule, recurrence owned by robin-prefect-automations.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.mark.unit
def test_triage_poller_success_captures_subprocess_result(tmp_path: Path) -> None:
    from app.flows.operator_triage_pollers_flow import run_operator_poller

    poller = tmp_path / "poller.sh"
    poller.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\necho "poller ran: ${ROB758_ENV}"\n'
    )

    result = run_operator_poller(
        "watch-alert",
        poller_path=poller,
        timeout_s=5,
        extra_env={"ROB758_ENV": "present"},
    )

    assert result["status"] == "ok"
    assert result["kind"] == "watch-alert"
    assert result["returncode"] == 0
    assert result["poller_path"] == str(poller)
    assert "poller ran: present" in result["stdout_tail"]
    assert result["stderr_tail"] == ""


@pytest.mark.unit
def test_triage_poller_nonzero_raises_with_summary(tmp_path: Path) -> None:
    from app.flows.operator_triage_pollers_flow import (
        PollerExecutionError,
        run_operator_poller,
    )

    poller = tmp_path / "poller.sh"
    poller.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\necho 'poller failed' >&2\nexit 7\n"
    )

    with pytest.raises(PollerExecutionError) as exc_info:
        run_operator_poller("fill-event", poller_path=poller, timeout_s=5)

    summary = exc_info.value.summary
    assert summary["status"] == "failed"
    assert summary["kind"] == "fill-event"
    assert summary["returncode"] == 7
    assert "poller failed" in summary["stderr_tail"]


@pytest.mark.unit
def test_triage_poller_missing_path_fails_loudly(tmp_path: Path) -> None:
    from app.flows.operator_triage_pollers_flow import run_operator_poller

    with pytest.raises(FileNotFoundError):
        run_operator_poller("watch-alert", poller_path=tmp_path / "missing.sh")


@pytest.mark.unit
def test_triage_poller_flow_static_prefect_boundary() -> None:
    flow_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "flows"
        / "operator_triage_pollers_flow.py"
    )

    text = flow_path.read_text()
    assert "@flow" in text
    assert "@task" in text
    assert "retries=2" in text
    assert "retry_delay_seconds=60" in text
    assert "watch_alert_triage_poller_flow" in text
    assert "fill_event_triage_poller_flow" in text
    assert "robin-prefect-automations" in text
    assert "schedule=" not in text
    assert "@broker.task" not in text


@pytest.mark.unit
def test_runbooks_pin_prefect_cutover_and_dry_run_state_boundary() -> None:
    repo = Path(__file__).resolve().parents[1]
    cases = (
        (
            repo / "docs" / "runbooks" / "watch-alert-claude-triage.md",
            "watch_alert_triage_poller_flow",
            "seen_event_uuids",
            "last_delivered_at",
        ),
        (
            repo / "docs" / "runbooks" / "fill-event-claude-triage.md",
            "fill_event_triage_poller_flow",
            "seen_ledger_ids",
            "last_ledger_id",
        ),
    )

    for runbook, flow_name, seen_file, watermark_file in cases:
        text = runbook.read_text(encoding="utf-8")
        assert flow_name in text
        assert "CLAUDE_WORKSPACE" in text
        assert "robin-prefect-automations" in text
        assert "launchctl bootout" in text
        assert "native_daemon_health_flow" in text
        assert (
            f"dry-run은 `{seen_file}`와 `{watermark_file}`를 갱신하지 않으므로" in text
        )

        dry_run_branch = text.split('if [[ "$DRY_RUN" == "1" ]]; then', maxsplit=1)[1]
        dry_run_branch = dry_run_branch.split("else", maxsplit=1)[0]
        assert "seen/워터마크를 절대 전진시키지 않는다" in dry_run_branch
        assert "\n    continue\n" in dry_run_branch


def _completed(args: list[str], returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


@pytest.mark.unit
def test_native_daemon_healthcheck_success_runs_healthcheck_and_launchctl(
    tmp_path: Path,
) -> None:
    from app.flows.native_daemon_health_flow import run_native_daemon_healthcheck

    current = tmp_path / "current"
    current.mkdir()
    script = tmp_path / "healthcheck-native.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(args: list[str], **_: object):
        calls.append((args, _))
        return _completed(args, 0, stdout="ok\n")

    result = run_native_daemon_healthcheck(
        base_dir=tmp_path,
        healthcheck_script=script,
        labels=("com.test.worker",),
        uid=501,
        timeout_s=5,
        run=fake_run,
    )

    assert result["status"] == "ok"
    assert calls[0][0] == ["/bin/bash", str(script)]
    assert calls[0][1]["cwd"] == current
    assert calls[1][0] == ["launchctl", "print", "gui/501/com.test.worker"]
    assert result["launchd"][0]["status"] == "loaded"


@pytest.mark.unit
def test_native_daemon_healthcheck_failure_raises_degraded_summary(
    tmp_path: Path,
) -> None:
    from app.flows.native_daemon_health_flow import (
        NativeDaemonHealthError,
        run_native_daemon_healthcheck,
    )

    script = tmp_path / "healthcheck-native.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n")

    def fake_run(args: list[str], **_: object):
        if args[:2] == ["/bin/bash", str(script)]:
            return _completed(args, 1, stderr="api healthz failed\n")
        return _completed(args, 0)

    with pytest.raises(NativeDaemonHealthError) as exc_info:
        run_native_daemon_healthcheck(
            base_dir=tmp_path,
            healthcheck_script=script,
            labels=("com.test.worker",),
            uid=501,
            timeout_s=5,
            run=fake_run,
        )

    summary = exc_info.value.summary
    assert summary["status"] == "degraded"
    assert summary["healthcheck"]["returncode"] == 1
    assert "api healthz failed" in summary["healthcheck"]["stderr_tail"]
