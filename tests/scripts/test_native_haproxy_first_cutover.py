"""ROB-259 review fix: first-cutover ordering + restore-on-failure.

These tests cover two concerns the reviewer raised:

1. The legacy single-active api/mcp must NOT be drained until blue is
   bootstrapped, direct-probed, and the haproxy.cfg has been rendered +
   validated. Blue uses :8001/:8766, different ports from legacy :8000/:8765,
   so there's no port conflict and we can validate before any drain.

2. If anything in the brief drain → start-haproxy → probe-public window
   fails, an ERR trap restores the legacy plists from a backup and
   re-bootstraps the launchd jobs.

We test both by:
  - static-analysis assertions on main() function call ordering, and
  - functional tests of restore_legacy_or_fail (sourced as a library).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "native_haproxy_first_cutover.sh"


# ---------------------------------------------------------------------------
# Static analysis: main() call ordering
# ---------------------------------------------------------------------------


def _main_body() -> list[str]:
    """Return the lines inside main() { ... }."""
    body = SCRIPT.read_text()
    m = re.search(r"^main\s*\(\)\s*\{(.*?)^\}", body, re.MULTILINE | re.DOTALL)
    assert m, "main() function not found in first-cutover script"
    return m.group(1).splitlines()


def _first_index_of_call(lines: list[str], name: str) -> int | None:
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.search(rf"(^|[\s;|&]){re.escape(name)}(\s|$)", stripped):
            return i
    return None


def test_main_invokes_required_steps_in_order() -> None:
    """Each required step appears exactly once in main and in the right order."""
    lines = _main_body()
    required_order = [
        "require_brew_haproxy",
        "require_release_ops",
        "capture_legacy_plists",
        "sync_repo_assets",
        "init_state_and_symlinks",
        "bootstrap_blue",
        "probe_blue_direct",
        "prepare_haproxy_cfg",
        "drain_old_single_active_api_mcp",
        "start_haproxy",
        "probe_public_stable",
        "remove_stale_plists",
        "cleanup_legacy_backup",
    ]
    indices = [_first_index_of_call(lines, step) for step in required_order]
    missing = [
        step for step, i in zip(required_order, indices, strict=True) if i is None
    ]
    assert not missing, f"main() is missing required steps: {missing}"
    # Monotonically increasing
    for prev_step, prev_i, step, i in zip(
        required_order,
        indices,
        required_order[1:],
        indices[1:],
        strict=False,
    ):
        assert prev_i is not None and i is not None
        assert prev_i < i, (
            f"main() calls {step} (idx {i}) before {prev_step} (idx {prev_i})"
        )


def test_blue_brought_up_before_legacy_drain() -> None:
    """bootstrap_blue + probe_blue_direct + prepare_haproxy_cfg must precede
    drain_old_single_active_api_mcp. Otherwise the legacy api/mcp goes down
    before we have a working replacement."""
    lines = _main_body()
    drain_idx = _first_index_of_call(lines, "drain_old_single_active_api_mcp")
    assert drain_idx is not None, "drain_old_single_active_api_mcp not called"
    for required_before in (
        "bootstrap_blue",
        "probe_blue_direct",
        "prepare_haproxy_cfg",
    ):
        idx = _first_index_of_call(lines, required_before)
        assert idx is not None, f"{required_before} not called"
        assert idx < drain_idx, (
            f"{required_before} (line idx {idx}) must run before "
            f"drain_old_single_active_api_mcp (line idx {drain_idx}) so blue "
            f"is verified-up before the legacy api/mcp is taken down."
        )


def test_capture_runs_before_sync_rsync_delete() -> None:
    """capture_legacy_plists must run BEFORE sync_repo_assets, which rsync
    --delete's $BASE/plists/ and would otherwise lose the legacy plists."""
    lines = _main_body()
    capture_idx = _first_index_of_call(lines, "capture_legacy_plists")
    sync_idx = _first_index_of_call(lines, "sync_repo_assets")
    assert capture_idx is not None and sync_idx is not None
    assert capture_idx < sync_idx, (
        "capture_legacy_plists must run before sync_repo_assets so the legacy "
        "plists are preserved before rsync --delete wipes $BASE/plists/."
    )


def test_err_trap_installed_before_drain() -> None:
    """The restore-on-failure ERR trap must be installed BEFORE the legacy drain."""
    lines = _main_body()
    drain_idx = _first_index_of_call(lines, "drain_old_single_active_api_mcp")
    trap_idx = None
    for i, line in enumerate(lines):
        if "trap restore_legacy_or_fail" in line or re.search(
            r"trap\s+restore_legacy_or_fail\s+ERR", line
        ):
            trap_idx = i
            break
    assert trap_idx is not None, (
        "main() never installs `trap restore_legacy_or_fail ERR`"
    )
    assert trap_idx < drain_idx, (
        f"`trap restore_legacy_or_fail ERR` (line idx {trap_idx}) must be "
        f"installed before drain_old_single_active_api_mcp (line idx {drain_idx})."
    )


def test_err_trap_cleared_after_probe_public_stable() -> None:
    """`trap - ERR` must run after probe_public_stable so subsequent housekeeping
    (remove_stale_plists, cleanup_legacy_backup) does not get rolled back."""
    lines = _main_body()
    probe_idx = _first_index_of_call(lines, "probe_public_stable")
    trap_clear_idx = None
    for i, line in enumerate(lines):
        if re.match(r"\s*trap\s+-\s+ERR\b", line):
            trap_clear_idx = i
            break
    assert trap_clear_idx is not None, "main() never clears the ERR trap"
    assert probe_idx is not None and probe_idx < trap_clear_idx, (
        "trap - ERR must come after probe_public_stable succeeds"
    )


# ---------------------------------------------------------------------------
# Functional tests: source the script as a library and exercise functions
# ---------------------------------------------------------------------------


def _bash_eval(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Source the cutover script (no main run thanks to BASH_SOURCE guard)
    and evaluate snippet in the same shell."""
    script = f'set -Eeuo pipefail\nsource "{SCRIPT}"\n{snippet}\n'
    return subprocess.run(
        ["bash", "-c", script], check=False, capture_output=True, text=True, env=env
    )


def _stub_dir_for_launchctl(tmp_path: Path, log_path: Path) -> Path:
    """launchctl stub that logs invocations and always exits 0."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "launchctl").write_text(
        '#!/usr/bin/env bash\necho "launchctl $*" >>"' + str(log_path) + '"\nexit 0\n'
    )
    (bin_dir / "launchctl").chmod(0o755)
    return bin_dir


def _make_legacy_backup(tmp_path: Path, base: Path) -> Path:
    """Create a backup dir containing legacy plist content."""
    backup = tmp_path / "legacy_backup"
    backup.mkdir()
    (backup / "com.robinco.auto-trader.api.plist").write_text(
        "<plist><!-- legacy api --></plist>"
    )
    (backup / "com.robinco.auto-trader.mcp.plist").write_text(
        "<plist><!-- legacy mcp --></plist>"
    )
    return backup


def test_capture_legacy_plists_copies_files_to_tmp(tmp_path: Path) -> None:
    base = tmp_path / "auto_trader"
    (base / "plists").mkdir(parents=True)
    (base / "current" / "ops" / "native").mkdir(parents=True)
    api_body = "<plist><!-- existing api --></plist>"
    mcp_body = "<plist><!-- existing mcp --></plist>"
    (base / "plists" / "com.robinco.auto-trader.api.plist").write_text(api_body)
    (base / "plists" / "com.robinco.auto-trader.mcp.plist").write_text(mcp_body)

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
    }
    # Run capture and then echo the LEGACY_BACKUP path + contents
    snippet = (
        "capture_legacy_plists\n"
        'echo "BACKUP=$LEGACY_BACKUP"\n'
        'cat "$LEGACY_BACKUP/com.robinco.auto-trader.api.plist"\n'
        'cat "$LEGACY_BACKUP/com.robinco.auto-trader.mcp.plist"\n'
    )
    proc = _bash_eval(snippet, env)
    assert proc.returncode == 0, proc.stderr
    assert "BACKUP=" in proc.stdout
    assert api_body in proc.stdout
    assert mcp_body in proc.stdout


def test_capture_legacy_plists_tolerates_missing_legacy(tmp_path: Path) -> None:
    """If the legacy plists don't exist (e.g. the operator already removed them
    manually), capture must still succeed — the restore will then be a no-op."""
    base = tmp_path / "auto_trader"
    (base / "plists").mkdir(parents=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
    }
    proc = _bash_eval("capture_legacy_plists\necho ok\n", env)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_restore_legacy_or_fail_reinstalls_and_bootstraps(tmp_path: Path) -> None:
    base = tmp_path / "auto_trader"
    (base / "plists").mkdir(parents=True)
    legacy = _make_legacy_backup(tmp_path, base)
    launchctl_log = tmp_path / "launchctl.log"
    launchctl_log.write_text("")
    bin_dir = _stub_dir_for_launchctl(tmp_path, launchctl_log)
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
    }
    # Manually set LEGACY_BACKUP since we're not running capture in this test.
    # Use false to provide a non-zero exit code that restore reports back.
    snippet = (
        f'LEGACY_BACKUP="{legacy}"\n'
        "# Trip the trap by running false in a subshell that returns 7\n"
        "( exit 7 ) || restore_legacy_or_fail || true\n"
    )
    # restore_legacy_or_fail calls exit, so the outer || true catches it; we
    # don't inspect the proc directly — we verify the side effects.
    _bash_eval(snippet, env)
    log = launchctl_log.read_text()
    assert "bootstrap" in log
    assert "com.robinco.auto-trader.api" in log
    assert "com.robinco.auto-trader.mcp" in log
    # Plists were reinstalled
    assert (launch_agents / "com.robinco.auto-trader.api.plist").read_text() == (
        "<plist><!-- legacy api --></plist>"
    )
    assert (launch_agents / "com.robinco.auto-trader.mcp.plist").read_text() == (
        "<plist><!-- legacy mcp --></plist>"
    )


def test_restore_legacy_or_fail_warns_when_backup_missing(tmp_path: Path) -> None:
    base = tmp_path / "auto_trader"
    (base / "plists").mkdir(parents=True)
    launchctl_log = tmp_path / "launchctl.log"
    launchctl_log.write_text("")
    bin_dir = _stub_dir_for_launchctl(tmp_path, launchctl_log)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
    }
    # LEGACY_BACKUP unset entirely
    snippet = 'LEGACY_BACKUP=""\nrestore_legacy_or_fail || true\n'
    proc = _bash_eval(snippet, env)
    assert "FATAL" in proc.stderr or "no legacy plist backup" in proc.stderr


def test_script_does_not_auto_run_main_when_sourced(tmp_path: Path) -> None:
    """The script's bottom guard `if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then`
    must prevent main() from running when the script is sourced (which is what
    every functional test above relies on)."""
    base = tmp_path / "auto_trader"
    base.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
    }
    # Just source the script and exit. If main runs, require_brew_haproxy will
    # fail with "haproxy not installed" (exit 78). If sourcing is clean, exit 0.
    proc = _bash_eval("echo sourced\n", env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "sourced"
