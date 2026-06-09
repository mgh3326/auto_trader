"""ROB-259 deploy-native blue/green flow unit tests.

Tests the extracted helper lib (ops/native/scripts/native_deploy_lib.sh) end-to-end
under a fake AUTO_TRADER_BASE with stubbed launchctl/curl/haproxy.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "ops" / "native" / "scripts" / "native_deploy_lib.sh"


def _stub_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # launchctl stub logs invocations to $LAUNCHCTL_LOG, always exit 0
    (bin_dir / "launchctl").write_text(
        '#!/usr/bin/env bash\necho "launchctl $*" >>"$LAUNCHCTL_LOG"\nexit 0\n'
    )
    (bin_dir / "launchctl").chmod(0o755)
    # curl stub: succeeds for 127.0.0.1 probes
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        'fsS=0; capture_status=0; url=""\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        "    -fsS) fsS=1 ;;\n"
        "    -sS) capture_status=1 ;;\n"
        "    -o|-w|-H) ;;\n"
        "    -*) ;;\n"
        '    http*) url="$arg" ;;\n'
        "  esac\n"
        "done\n"
        'case "$url" in\n'
        "  *127.0.0.1*)\n"
        '    if [[ "$capture_status" == "1" ]]; then echo 200; fi\n'
        "    exit 0 ;;\n"
        "  *) exit 6 ;;\n"
        "esac\n"
    )
    (bin_dir / "curl").chmod(0o755)
    # haproxy stub: validation -c always succeeds
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
    (base / "shared" / "api-active-color").write_text("blue\n")
    (base / "shared" / "mcp-active-color").write_text("blue\n")
    (base / "current-blue").symlink_to(base / "releases" / "sha-old")
    # Sync repo assets into the prod-like base (mirrors what deploy-native.sh sync step does).
    for src in [
        REPO_ROOT / "ops" / "native" / "scripts" / "native_bluegreen_lib.sh",
        REPO_ROOT / "ops" / "native" / "scripts" / "haproxy_render.sh",
        REPO_ROOT / "ops" / "native" / "scripts" / "haproxy_switch.sh",
        REPO_ROOT / "ops" / "native" / "scripts" / "healthcheck-native.sh",
        REPO_ROOT / "ops" / "native" / "scripts" / "native_deploy_lib.sh",
    ]:
        dst = base / "scripts" / src.name
        dst.write_text(src.read_text())
        dst.chmod(0o755)
    # Minimal common.sh so healthcheck-native.sh can source it.
    (base / "scripts" / "common.sh").write_text(
        '#!/bin/zsh\nset -euo pipefail\nexport AUTO_TRADER_BASE="${AUTO_TRADER_BASE}"\n'
    )
    (base / "scripts" / "haproxy" / "haproxy.cfg.tmpl").write_text(
        (REPO_ROOT / "ops" / "native" / "haproxy" / "haproxy.cfg.tmpl").read_text()
    )
    for p in (REPO_ROOT / "ops" / "native" / "plists").glob("*.plist"):
        (base / "plists" / p.name).write_text(p.read_text())
    # Pre-create LaunchAgents dir under fake HOME so install -m commands succeed.
    (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    return base


def _run_bash(
    snippet: str,
    base: Path,
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    bin_dir = _stub_dir(tmp_path)
    log = tmp_path / "launchctl.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
        "AUTO_TRADER_HAPROXY_RELOAD": "skip",
        "LAUNCHCTL_LOG": str(log),
        "AUTO_TRADER_HEALTHCHECK_SKIP_WS": "1",
        "AUTO_TRADER_HEALTHCHECK_ATTEMPTS": "1",
        "AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS": "0",
    }
    if extra_env:
        env.update(extra_env)
    script = f'set -Eeuo pipefail\nsource "{LIB}"\n{snippet}\n'
    proc = subprocess.run(
        ["bash", "-c", script], check=False, capture_output=True, text=True, env=env
    )
    proc.launchctl_log = log.read_text()  # type: ignore[attr-defined]
    return proc


def test_sync_release_to_color_symlink_creates(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run_bash(
        f'sync_release_to_color_symlink green "{base}/releases/sha-new"', base, tmp_path
    )
    assert proc.returncode == 0, proc.stderr
    assert (base / "current-green").resolve() == (base / "releases" / "sha-new")


def test_bootstrap_color_invokes_launchctl(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run_bash("bootstrap_color api green", base, tmp_path)
    assert proc.returncode == 0, proc.stderr
    log = proc.launchctl_log  # type: ignore[attr-defined]
    assert "bootstrap" in log and "api-green" in log


def test_drain_color_bootouts(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run_bash("drain_color api blue", base, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "bootout" in proc.launchctl_log and "api-blue" in proc.launchctl_log  # type: ignore[attr-defined]


def test_probe_color_direct_passes(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run_bash("probe_color_direct green", base, tmp_path)
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_healthcheck_default_attempts_allow_slow_cold_start() -> None:
    body = LIB.read_text()
    assert "AUTO_TRADER_HEALTHCHECK_ATTEMPTS:-24" in body


def test_probe_public_stable_skips_websocket_singletons(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    healthcheck_log = tmp_path / "healthcheck.log"
    (base / "scripts" / "healthcheck-native.sh").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "skip=${{AUTO_TRADER_HEALTHCHECK_SKIP_WS:-0}} args=$*" >>"{healthcheck_log}"\n'
        '[[ "${AUTO_TRADER_HEALTHCHECK_SKIP_WS:-0}" == "1" ]]\n'
    )
    (base / "scripts" / "healthcheck-native.sh").chmod(0o755)

    proc = _run_bash(
        "probe_public_stable",
        base,
        tmp_path,
        extra_env={"AUTO_TRADER_HEALTHCHECK_SKIP_WS": "0"},
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "skip=1 args=" in healthcheck_log.read_text()


def test_haproxy_swap_to_color_updates_state(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run_bash("haproxy_swap_to_color api green", base, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (base / "shared" / "api-active-color").read_text().strip() == "green"
    assert (base / "shared" / "haproxy" / "haproxy.cfg").is_file()


def test_full_deploy_flow_happy_path(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run_bash(f'deploy_bluegreen_flow "{base}/releases/sha-new"', base, tmp_path)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert (base / "shared" / "api-active-color").read_text().strip() == "green"
    assert (base / "shared" / "mcp-active-color").read_text().strip() == "green"
    assert (base / "current-green").resolve() == (base / "releases" / "sha-new")
    log = proc.launchctl_log  # type: ignore[attr-defined]
    assert "bootout" in log and "api-blue" in log
    assert "bootstrap" in log and "api-green" in log


def test_full_deploy_rolls_back_on_probe_failure(tmp_path: Path) -> None:
    """If green direct probe fails, no HAProxy swap, no state-file change."""
    base = _setup_base(tmp_path)
    # Build a sabotaged curl stub: fails for the green ports (8002 + 8767)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        'capture_status=0; url=""\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        "    -sS) capture_status=1 ;;\n"
        "    -o|-w|-H|-fsS|-*) ;;\n"
        '    http*) url="$arg" ;;\n'
        "  esac\n"
        "done\n"
        'case "$url" in\n'
        "  *8002*|*8767*) exit 22 ;;\n"
        '  *127.0.0.1*) if [[ "$capture_status" == "1" ]]; then echo 200; fi; exit 0 ;;\n'
        "  *) exit 6 ;;\n"
        "esac\n"
    )
    (bin_dir / "curl").chmod(0o755)
    # launchctl + haproxy stubs (reuse)
    (bin_dir / "launchctl").write_text(
        '#!/usr/bin/env bash\necho "launchctl $*" >>"$LAUNCHCTL_LOG"\nexit 0\n'
    )
    (bin_dir / "launchctl").chmod(0o755)
    (bin_dir / "haproxy").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_dir / "haproxy").chmod(0o755)
    log = tmp_path / "launchctl.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
        "AUTO_TRADER_HAPROXY_RELOAD": "skip",
        "LAUNCHCTL_LOG": str(log),
        "AUTO_TRADER_HEALTHCHECK_SKIP_WS": "1",
        "AUTO_TRADER_HEALTHCHECK_ATTEMPTS": "1",
        "AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS": "0",
    }
    script = f'set -Eeuo pipefail\nsource "{LIB}"\ndeploy_bluegreen_flow "{base}/releases/sha-new"\n'
    proc = subprocess.run(
        ["bash", "-c", script], check=False, capture_output=True, text=True, env=env
    )
    assert proc.returncode != 0, proc.stdout
    assert (base / "shared" / "api-active-color").read_text().strip() == "blue"
    assert (base / "shared" / "mcp-active-color").read_text().strip() == "blue"


def test_deploy_rolls_back_api_state_on_api_swap_failure(tmp_path: Path) -> None:
    """If haproxy_swap_to_color api fails, api-active-color must be restored to blue
    AND a compensating haproxy_switch must run so the live cfg matches the restored state.
    """
    base = _setup_base(tmp_path)
    # Replace haproxy_switch.sh with one that always fails, BUT counts calls so we can
    # verify the compensating switch is attempted (vs silently skipped).
    (base / "scripts" / "haproxy_switch.sh").write_text(
        "#!/usr/bin/env bash\n"
        'counter_file="$AUTO_TRADER_BASE/shared/switch-call-count"\n'
        'count=$(cat "$counter_file" 2>/dev/null || echo 0)\n'
        "count=$((count + 1))\n"
        'echo "$count" > "$counter_file"\n'
        'echo "switch failure (call $count)" >&2\n'
        "exit 1\n"
    )
    (base / "scripts" / "haproxy_switch.sh").chmod(0o755)
    proc = _run_bash(f'deploy_bluegreen_flow "{base}/releases/sha-new"', base, tmp_path)
    assert proc.returncode != 0
    assert (base / "shared" / "api-active-color").read_text().strip() == "blue"
    assert (base / "shared" / "mcp-active-color").read_text().strip() == "blue"
    # Tight assertion: exactly 2 calls. 1st = api swap (failed), 2nd = compensating
    # switch after state restore (also failed in this test, but MUST have been
    # attempted). Permissive `in {"1","2"}` would hide a regression that silently
    # skips the compensating call.
    count = (base / "shared" / "switch-call-count").read_text().strip()
    assert count == "2", (
        f"expected exactly 2 haproxy_switch invocations (api swap + compensating "
        f"switch), got {count}. Compensating switch is missing from the api-swap "
        f"failure rollback path."
    )


def test_deploy_rolls_back_both_states_on_mcp_swap_failure(tmp_path: Path) -> None:
    """If api swap succeeds but mcp swap fails, both state files must restore
    AND a compensating haproxy_switch must run after the restore.
    """
    base = _setup_base(tmp_path)
    # Replace haproxy_switch.sh with one that succeeds the first call and fails the second.
    (base / "scripts" / "haproxy_switch.sh").write_text(
        "#!/usr/bin/env bash\n"
        'counter_file="$AUTO_TRADER_BASE/shared/switch-call-count"\n'
        'count=$(cat "$counter_file" 2>/dev/null || echo 0)\n'
        "count=$((count + 1))\n"
        'echo "$count" > "$counter_file"\n'
        "# 1st call (api swap): succeed\n"
        "# 2nd call (mcp swap): fail\n"
        "# 3rd call (compensating swap after restore): succeed\n"
        'if [[ "$count" == "2" ]]; then echo "second call fail" >&2; exit 1; fi\n'
        "exit 0\n"
    )
    (base / "scripts" / "haproxy_switch.sh").chmod(0o755)
    proc = _run_bash(f'deploy_bluegreen_flow "{base}/releases/sha-new"', base, tmp_path)
    assert proc.returncode != 0, proc.stdout
    # Both state files must be back to blue
    assert (base / "shared" / "api-active-color").read_text().strip() == "blue"
    assert (base / "shared" / "mcp-active-color").read_text().strip() == "blue"
    # Tight assertion: exactly 3 calls. 1st = api swap (ok), 2nd = mcp swap (fail),
    # 3rd = compensating switch after restoring both colors. Previously this was
    # `count in {"2","3"}` which silently allowed a regression that skips the
    # compensating call.
    count = (base / "shared" / "switch-call-count").read_text().strip()
    assert count == "3", (
        f"expected exactly 3 haproxy_switch invocations (api ok, mcp fail, "
        f"compensating), got {count}. Compensating switch is missing from the "
        f"mcp-swap failure rollback path."
    )
