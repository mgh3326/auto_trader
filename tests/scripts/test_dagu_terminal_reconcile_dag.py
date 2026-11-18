"""Static check for the terminal-only reconcile DAG (hk 3794).

Operator decision hk:doc decision/2026-11-18/at-3794-3795-recommended allows
an unattended run that records only evidenced expiry/cancellation. This test
pins that the DAG never asks for fill booking, that each activation flag is set
only through the job's own ``docker run -e``, and that exit code 3 (attention)
is logged instead of failing or being swallowed. It does not call Dagu, Docker
or a database.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DAG = ROOT / "deploy/dagu/at-reconcile/at-terminal-reconcile.yaml"

STEPS = {
    "kis-kr": ("KIS_LIVE_AUTO_RECONCILE_ENABLED", "scripts.kis_live_auto_reconcile"),
    "kis-us": (
        "KIS_US_TERMINAL_AUTO_RECONCILE_ENABLED",
        "scripts.kis_us_auto_reconcile",
    ),
    "toss": (
        "TOSS_TERMINAL_AUTO_RECONCILE_ENABLED",
        "scripts.toss_live_auto_reconcile",
    ),
}
# Gates that open fill booking; none may appear anywhere in the DAG.
FILL_GATES = (
    "KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED",
    "LIVE_AUTO_RECONCILE_DRY_RUN",
    "TOSS_LIVE_AUTO_RECONCILE",
)


def _dag() -> dict:
    return yaml.safe_load(DAG.read_text())


def test_schedule_and_worker() -> None:
    dag = _dag()
    assert dag["schedule"] == "CRON_TZ=UTC 40 1,11 * * *"
    assert dag["worker_selector"] == {"host": "pi"}
    assert dag["max_active_runs"] == 1


def test_steps_are_terminal_only_with_own_flag() -> None:
    raw = _dag()["steps"]
    steps = {s["name"]: s for s in raw}
    assert len(raw) == len(steps), "duplicate step names"
    assert set(steps) == set(STEPS)
    for name, (flag, module) in STEPS.items():
        step = steps[name]
        # script, not command: Dagu expands $? and $rc inside command first.
        assert "command" not in step
        script = step["script"]
        lines = [line for line in script.splitlines() if line.strip()]
        assert lines[0] == "rc=0"
        run = lines[1]
        assert run.startswith("docker run --rm --init --network host ")
        assert "--env-file /root/at-secrets/.env.api" in run
        assert re.findall(r"-e (\S+)", run) == [f"{flag}=true"]
        assert run.endswith(f"-m {module} --apply-terminal-only || rc=$?")
        assert re.search(r"--apply(\s|$)", run) is None
        assert lines[2] == (
            'if [ "$rc" -eq 3 ]; then logger -t at-dagu-batch '
            f'"dag=at-terminal-reconcile step={name} status=attention" || true; exit 0; fi'
        )
        assert lines[3] == 'exit "$rc"'
        assert len(lines) == 4
    assert steps["kis-us"].get("depends") == ["kis-kr"]


def test_no_fill_booking_gate_anywhere() -> None:
    text = DAG.read_text()
    for gate in FILL_GATES:
        assert gate not in text, gate
    # The US fill gate name is a suffix of the KR flag, so match it whole.
    assert re.search(r"(?<![A-Z_])LIVE_AUTO_RECONCILE_ENABLED", text) is None
