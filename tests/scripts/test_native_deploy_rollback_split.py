"""ROB-259 review fix: deploy rollback covers api/mcp + worker.

Reviewer raised the case where deploy_bluegreen_flow has already committed
(api/mcp on new release color), but a later step (restart_single_active_services
or run_healthcheck) fails. The previous rollback only flipped the `current`
symlink — leaving api/mcp on the new release and worker/scheduler/websocket
on the old one (a release split).

Tests in this module cover:
  - capture_bluegreen_state prints the pre-deploy snapshot fields
  - rollback_bluegreen_post_deploy restores state files, color symlinks,
    color launchd jobs, and re-runs haproxy_switch
  - scripts/deploy-native.sh calls capture_bluegreen_state BEFORE
    deploy_bluegreen_flow and rollback_bluegreen_post_deploy from
    rollback() when BLUEGREEN_COMMITTED=1
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "ops" / "native" / "scripts" / "native_deploy_lib.sh"
DEPLOY = REPO_ROOT / "scripts" / "deploy-native.sh"


def _stub_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "launchctl").write_text(
        '#!/usr/bin/env bash\necho "launchctl $*" >>"$LAUNCHCTL_LOG"\nexit 0\n'
    )
    (bin_dir / "launchctl").chmod(0o755)
    (bin_dir / "haproxy").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_dir / "haproxy").chmod(0o755)
    return bin_dir


def _setup_base(tmp_path: Path) -> Path:
    base = tmp_path / "services" / "auto_trader"
    for sub in [
        "releases/sha-old",
        "releases/sha-new",
        "shared/haproxy",
        "scripts",
        "scripts/haproxy",
        "plists",
        "logs",
    ]:
        (base / sub).mkdir(parents=True, exist_ok=True)
    # Sync repo helper scripts into the fake base.
    for src in [
        REPO_ROOT / "ops" / "native" / "scripts" / "native_bluegreen_lib.sh",
        REPO_ROOT / "ops" / "native" / "scripts" / "haproxy_render.sh",
        REPO_ROOT / "ops" / "native" / "scripts" / "haproxy_switch.sh",
        REPO_ROOT / "ops" / "native" / "scripts" / "native_deploy_lib.sh",
    ]:
        dst = base / "scripts" / src.name
        dst.write_text(src.read_text())
        dst.chmod(0o755)
    (base / "scripts" / "haproxy" / "haproxy.cfg.tmpl").write_text(
        (REPO_ROOT / "ops" / "native" / "haproxy" / "haproxy.cfg.tmpl").read_text()
    )
    for p in (REPO_ROOT / "ops" / "native" / "plists").glob("*.plist"):
        (base / "plists" / p.name).write_text(p.read_text())
    (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    return base


def _run(snippet: str, base: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    bin_dir = _stub_dir(tmp_path)
    log = tmp_path / "launchctl.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
        "AUTO_TRADER_HAPROXY_RELOAD": "skip",
        "LAUNCHCTL_LOG": str(log),
    }
    script = f'set -Eeuo pipefail\nsource "{LIB}"\n{snippet}\n'
    proc = subprocess.run(
        ["bash", "-c", script], check=False, capture_output=True, text=True, env=env
    )
    proc.launchctl_log = log.read_text()  # type: ignore[attr-defined]
    return proc


# ---------------------------------------------------------------------------
# capture_bluegreen_state
# ---------------------------------------------------------------------------


def test_capture_returns_blue_blue_no_symlinks_initially(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    (base / "shared" / "api-active-color").write_text("blue\n")
    (base / "shared" / "mcp-active-color").write_text("blue\n")
    proc = _run("capture_bluegreen_state", base, tmp_path)
    assert proc.returncode == 0, proc.stderr
    fields = proc.stdout.strip().split()
    assert fields == ["blue", "blue", "-", "-"]


def test_capture_returns_symlink_targets_when_present(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    (base / "shared" / "api-active-color").write_text("green\n")
    (base / "shared" / "mcp-active-color").write_text("blue\n")
    (base / "current-blue").symlink_to(base / "releases" / "sha-old")
    (base / "current-green").symlink_to(base / "releases" / "sha-new")
    proc = _run("capture_bluegreen_state", base, tmp_path)
    assert proc.returncode == 0, proc.stderr
    fields = proc.stdout.strip().split()
    assert fields[0] == "green"
    assert fields[1] == "blue"
    assert fields[2] == str(base / "releases" / "sha-old")
    assert fields[3] == str(base / "releases" / "sha-new")


# ---------------------------------------------------------------------------
# rollback_bluegreen_post_deploy
# ---------------------------------------------------------------------------


def test_rollback_restores_state_files_and_symlinks(tmp_path: Path) -> None:
    """Scenario: deploy flipped blue→green, then later step failed.
    Pre-state: api=blue mcp=blue current-blue=<old>, current-green did not exist.
    Post-deploy: api=green mcp=green current-green=<new>.
    Rollback must restore api=blue mcp=blue and leave current-blue intact.
    """
    base = _setup_base(tmp_path)
    (base / "shared" / "api-active-color").write_text("green\n")
    (base / "shared" / "mcp-active-color").write_text("green\n")
    (base / "current-blue").symlink_to(base / "releases" / "sha-old")
    (base / "current-green").symlink_to(base / "releases" / "sha-new")

    proc = _run(
        f"rollback_bluegreen_post_deploy blue blue "
        f'"{base / "releases" / "sha-old"}" "-"',
        base,
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr

    # State files restored
    assert (base / "shared" / "api-active-color").read_text().strip() == "blue"
    assert (base / "shared" / "mcp-active-color").read_text().strip() == "blue"
    # current-blue unchanged (still points at old)
    assert (base / "current-blue").resolve() == (base / "releases" / "sha-old")
    # current-green also pre-existed at green=<new>; the rollback passes "-"
    # for green_pre, so we don't touch current-green.
    # launchctl was invoked: bootstrap blue + drain green for both services.
    log = proc.launchctl_log  # type: ignore[attr-defined]
    assert "bootstrap" in log and "api-blue" in log
    assert "bootstrap" in log and "mcp-blue" in log
    assert "bootout" in log and "api-green" in log
    assert "bootout" in log and "mcp-green" in log
    # haproxy_switch was invoked (the haproxy stub returns 0 so the switch
    # writes the live cfg). We verify the live cfg exists with restored content.
    live = base / "shared" / "haproxy" / "haproxy.cfg"
    assert live.is_file()
    body = live.read_text()
    # Should now reflect blue/blue (primary lines for blue, backup for green)
    assert "server api_blue 127.0.0.1:8001 check\n" in body
    assert "server api_green 127.0.0.1:8002 check backup\n" in body


def test_rollback_skips_color_symlink_when_marked_dash(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    (base / "shared" / "api-active-color").write_text("green\n")
    (base / "shared" / "mcp-active-color").write_text("green\n")
    (base / "current-green").symlink_to(base / "releases" / "sha-new")

    proc = _run(
        'rollback_bluegreen_post_deploy blue blue "-" "-"',
        base,
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    # No current-blue symlink should have been created from `-`
    assert not (base / "current-blue").exists()


def test_rollback_noop_when_pre_equals_current(tmp_path: Path) -> None:
    """If api/mcp pre-color equals current (e.g. deploy was rolled back inside
    deploy_bluegreen_flow already), rollback should NOT churn launchd jobs."""
    base = _setup_base(tmp_path)
    (base / "shared" / "api-active-color").write_text("blue\n")
    (base / "shared" / "mcp-active-color").write_text("blue\n")
    (base / "current-blue").symlink_to(base / "releases" / "sha-old")

    proc = _run(
        f"rollback_bluegreen_post_deploy blue blue "
        f'"{base / "releases" / "sha-old"}" "-"',
        base,
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    log = proc.launchctl_log  # type: ignore[attr-defined]
    # No bootstrap or bootout should have run for api/mcp colors (current == pre).
    # haproxy_switch still runs (idempotent) but no launchctl color juggling.
    assert "api-blue" not in log
    assert "mcp-blue" not in log
    assert "api-green" not in log
    assert "mcp-green" not in log


# ---------------------------------------------------------------------------
# Static-analysis on deploy-native.sh wiring
# ---------------------------------------------------------------------------


def _find_call_line_idx(body: str, name: str) -> int | None:
    """Find first line that references `name` outside a comment / definition /
    source statement. Accepts both bare invocations and command-substitution
    forms like `<<<"$(name)"`."""
    for i, line in enumerate(body.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(rf"^\s*{re.escape(name)}\s*\(\s*\)\s*\{{", line):
            continue  # function definition
        if "source " in line:
            continue
        if re.search(rf"\b{re.escape(name)}\b", line):
            return i
    return None


def test_deploy_captures_state_before_deploy_bluegreen_flow() -> None:
    body = DEPLOY.read_text()
    capture_idx = _find_call_line_idx(body, "capture_bluegreen_state")
    flow_idx = _find_call_line_idx(body, "deploy_bluegreen_flow")
    assert capture_idx is not None, (
        "deploy-native.sh must call capture_bluegreen_state before deploy_bluegreen_flow"
    )
    assert flow_idx is not None
    assert capture_idx < flow_idx, (
        f"capture_bluegreen_state (line {capture_idx + 1}) must run BEFORE "
        f"deploy_bluegreen_flow (line {flow_idx + 1})"
    )


def test_deploy_sets_bluegreen_committed_after_flow_returns() -> None:
    body = DEPLOY.read_text()
    lines = body.splitlines()
    flow_call_idx = None
    committed_set_idx = None
    for i, line in enumerate(lines):
        if "deploy_bluegreen_flow" in line and "shellcheck" not in line:
            # The call line (not the source line)
            if "$NEW_RELEASE" in line and "source" not in line:
                flow_call_idx = i
        if re.match(r"\s*BLUEGREEN_COMMITTED\s*=\s*1\b", line):
            committed_set_idx = i
    assert flow_call_idx is not None, "no deploy_bluegreen_flow call found"
    assert committed_set_idx is not None, "BLUEGREEN_COMMITTED=1 never set"
    assert flow_call_idx < committed_set_idx, (
        f"BLUEGREEN_COMMITTED=1 (line {committed_set_idx + 1}) must be set "
        f"AFTER deploy_bluegreen_flow (line {flow_call_idx + 1}) succeeds"
    )


def test_rollback_calls_rollback_bluegreen_post_deploy_when_committed() -> None:
    body = DEPLOY.read_text()
    # Look for `rollback_bluegreen_post_deploy` invocation inside a guard that
    # checks BLUEGREEN_COMMITTED. We accept either form (numeric or string).
    assert "rollback_bluegreen_post_deploy" in body, (
        "deploy-native.sh rollback() never calls rollback_bluegreen_post_deploy"
    )
    # Ensure it's gated by BLUEGREEN_COMMITTED — otherwise it would always run,
    # which is wrong (e.g. when bluegreen flow itself failed and already cleaned up).
    rb_post_idx = body.find("rollback_bluegreen_post_deploy")
    # Find the nearest preceding BLUEGREEN_COMMITTED check before this call
    preceding = body[:rb_post_idx]
    assert "BLUEGREEN_COMMITTED" in preceding, (
        "rollback_bluegreen_post_deploy must be guarded by a BLUEGREEN_COMMITTED check"
    )


def test_rollback_bluegreen_runs_before_current_symlink_revert() -> None:
    """In rollback(), api/mcp rollback must happen BEFORE the current symlink
    revert / single-active restart, so api/mcp + worker end up on the same
    (previous) release after rollback."""
    body = DEPLOY.read_text()
    # Extract rollback() body
    m = re.search(r"^rollback\s*\(\s*\)\s*\{(.*?)^\}", body, re.MULTILINE | re.DOTALL)
    assert m, "rollback() function not found"
    rb_body = m.group(1)
    rb_post_idx = rb_body.find("rollback_bluegreen_post_deploy")
    symlink_revert_idx = rb_body.find('ln -sfn "$PREVIOUS_RELEASE"')
    assert rb_post_idx >= 0
    assert symlink_revert_idx >= 0
    assert rb_post_idx < symlink_revert_idx, (
        "rollback_bluegreen_post_deploy must run BEFORE the current symlink "
        "revert so api/mcp settles back to the previous color before worker "
        "is restarted against PREVIOUS_RELEASE."
    )
