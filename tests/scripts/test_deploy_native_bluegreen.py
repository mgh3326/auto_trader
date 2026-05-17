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
        '    if [[ "$capture_status" == "1" ]]; then echo 401; fi\n'
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
        '  *127.0.0.1*) if [[ "$capture_status" == "1" ]]; then echo 401; fi; exit 0 ;;\n'
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
