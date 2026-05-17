"""ROB-259 review fix: deploy-native.sh preflight before rsync --delete.

Verifies require_haproxy_baseline catches each failure mode and that
scripts/deploy-native.sh calls it BEFORE sync_release_ops_to_base (which
contains the rsync --delete that would wipe legacy api/mcp plists).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "ops" / "native" / "scripts" / "native_deploy_lib.sh"
DEPLOY = REPO_ROOT / "scripts" / "deploy-native.sh"


def _stub_launchctl(tmp_path: Path, *, haproxy_loaded: bool) -> Path:
    """Stub launchctl. `launchctl list <label>` returns 0 iff haproxy_loaded."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "launchctl"
    if haproxy_loaded:
        body = (
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "list" && "$2" == *"haproxy"* ]]; then exit 0; fi\n'
            "exit 0\n"
        )
    else:
        body = (
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "list" && "$2" == *"haproxy"* ]]; then exit 113; fi\n'
            "exit 0\n"
        )
    stub.write_text(body)
    stub.chmod(0o755)
    return bin_dir


def _build_baseline(tmp_path: Path, *, include_color_symlink: bool = True) -> Path:
    base = tmp_path / "auto_trader"
    (base / "shared" / "haproxy").mkdir(parents=True)
    (base / "shared" / "api-active-color").write_text("blue\n")
    (base / "shared" / "mcp-active-color").write_text("blue\n")
    (base / "shared" / "haproxy" / "haproxy.cfg").write_text("global\n")
    (base / "releases" / "sha-old").mkdir(parents=True)
    if include_color_symlink:
        (base / "current-blue").symlink_to(base / "releases" / "sha-old")
    return base


def _run_preflight(
    base: Path, tmp_path: Path, *, haproxy_loaded: bool
) -> subprocess.CompletedProcess:
    bin_dir = _stub_launchctl(tmp_path, haproxy_loaded=haproxy_loaded)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
    }
    script = f'set -Eeuo pipefail\nsource "{LIB}"\nrequire_haproxy_baseline\n'
    return subprocess.run(
        ["bash", "-c", script], check=False, capture_output=True, text=True, env=env
    )


def test_preflight_passes_when_baseline_complete(tmp_path: Path) -> None:
    base = _build_baseline(tmp_path)
    proc = _run_preflight(base, tmp_path, haproxy_loaded=True)
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_preflight_fails_when_haproxy_not_loaded(tmp_path: Path) -> None:
    base = _build_baseline(tmp_path)
    proc = _run_preflight(base, tmp_path, haproxy_loaded=False)
    assert proc.returncode == 78
    assert "haproxy" in proc.stderr.lower()
    assert "first_cutover" in proc.stderr.lower()


def test_preflight_fails_when_api_state_file_missing(tmp_path: Path) -> None:
    base = _build_baseline(tmp_path)
    (base / "shared" / "api-active-color").unlink()
    proc = _run_preflight(base, tmp_path, haproxy_loaded=True)
    assert proc.returncode == 78
    assert "api-active-color" in proc.stderr


def test_preflight_fails_when_mcp_state_file_missing(tmp_path: Path) -> None:
    base = _build_baseline(tmp_path)
    (base / "shared" / "mcp-active-color").unlink()
    proc = _run_preflight(base, tmp_path, haproxy_loaded=True)
    assert proc.returncode == 78
    assert "mcp-active-color" in proc.stderr


def test_preflight_fails_when_haproxy_cfg_missing(tmp_path: Path) -> None:
    base = _build_baseline(tmp_path)
    (base / "shared" / "haproxy" / "haproxy.cfg").unlink()
    proc = _run_preflight(base, tmp_path, haproxy_loaded=True)
    assert proc.returncode == 78
    assert "haproxy.cfg" in proc.stderr


def test_preflight_fails_when_no_color_symlink(tmp_path: Path) -> None:
    base = _build_baseline(tmp_path, include_color_symlink=False)
    proc = _run_preflight(base, tmp_path, haproxy_loaded=True)
    assert proc.returncode == 78
    assert "current-blue" in proc.stderr and "current-green" in proc.stderr


def test_preflight_reports_all_failures(tmp_path: Path) -> None:
    """All failure modes are surfaced in one run (not short-circuited)."""
    base = _build_baseline(tmp_path, include_color_symlink=False)
    (base / "shared" / "api-active-color").unlink()
    (base / "shared" / "haproxy" / "haproxy.cfg").unlink()
    proc = _run_preflight(base, tmp_path, haproxy_loaded=False)
    assert proc.returncode == 78
    # All four issues mentioned
    assert "haproxy" in proc.stderr.lower()
    assert "api-active-color" in proc.stderr
    assert "haproxy.cfg" in proc.stderr
    assert "current-blue" in proc.stderr


def test_deploy_script_calls_preflight_before_sync_release_ops_to_base() -> None:
    """deploy-native.sh must invoke require_haproxy_baseline BEFORE calling
    sync_release_ops_to_base, which contains the rsync --delete that would
    wipe the legacy api/mcp plists if cutover was skipped.
    """
    body = DEPLOY.read_text()
    lines = body.splitlines()

    def _find_call_line(name: str) -> int | None:
        """Find first line that calls `name` as a bare command (not defining it,
        not commenting it, not sourcing it)."""
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Skip function definitions: `name() {` or `name ()  {`
            if re.match(rf"^\s*{re.escape(name)}\s*\(\s*\)\s*\{{", line):
                continue
            # Skip source lines
            if "source " in line:
                continue
            # Match bare call or piped/conditional call
            if re.search(rf"(^|[\s|&;]){re.escape(name)}(\s|$)", line):
                return i
        return None

    preflight_line = _find_call_line("require_haproxy_baseline")
    assert preflight_line is not None, (
        "deploy-native.sh never calls require_haproxy_baseline"
    )

    sync_line = _find_call_line("sync_release_ops_to_base")
    assert sync_line is not None, (
        "deploy-native.sh never calls sync_release_ops_to_base"
    )

    assert preflight_line < sync_line, (
        f"require_haproxy_baseline call (line {preflight_line + 1}) must run BEFORE "
        f"sync_release_ops_to_base call (line {sync_line + 1}). Otherwise a failed "
        f"preflight on a fresh server would still wipe the legacy plists via "
        f"rsync --delete."
    )


def test_deploy_script_sources_lib_from_release_not_base() -> None:
    """The preflight source line must use $NEW_RELEASE/ops/native/scripts (not $BASE/scripts).

    sync_release_ops_to_base hasn't run yet, so the lib in $BASE may not exist
    on a fresh server. The release dir is guaranteed to contain it.
    """
    body = DEPLOY.read_text()
    # The source for preflight comes from $NEW_RELEASE
    assert re.search(
        r'source\s+"\$NEW_RELEASE/ops/native/scripts/native_deploy_lib\.sh"',
        body,
    ), "deploy-native.sh must source the lib from $NEW_RELEASE for the preflight"
