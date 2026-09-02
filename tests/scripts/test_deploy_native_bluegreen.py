"""API-only Mac native blue/green deploy tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "ops" / "native" / "scripts" / "native_deploy_lib.sh"


def _setup_base(tmp_path: Path) -> Path:
    base = tmp_path / "services" / "auto_trader"
    for subdir in (
        "releases/old",
        "releases/new",
        "shared/haproxy",
        "scripts/haproxy",
        "plists",
    ):
        (base / subdir).mkdir(parents=True, exist_ok=True)
    (base / "shared" / "api-active-color").write_text("blue\n")
    (base / "current-blue").symlink_to(base / "releases" / "old")
    for name in (
        "native_bluegreen_lib.sh",
        "haproxy_render.sh",
        "haproxy_switch.sh",
        "healthcheck-native.sh",
    ):
        source = REPO_ROOT / "ops" / "native" / "scripts" / name
        destination = base / "scripts" / name
        destination.write_text(source.read_text())
        destination.chmod(0o755)
    (base / "scripts" / "common.sh").write_text("#!/usr/bin/env bash\n")
    template = REPO_ROOT / "ops" / "native" / "haproxy" / "haproxy.cfg.tmpl"
    (base / "scripts" / "haproxy" / "haproxy.cfg.tmpl").write_text(template.read_text())
    for plist in (REPO_ROOT / "ops" / "native" / "plists").glob("*.plist"):
        (base / "plists" / plist.name).write_text(plist.read_text())
    (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True)
    return base


def _run(
    tmp_path: Path, base: Path, command: str, *, fail_green_probe: bool = False
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    launch_log = tmp_path / "launchctl.log"
    launch_log.write_text("")
    (bin_dir / "launchctl").write_text(
        '#!/usr/bin/env bash\necho "$*" >> "$LAUNCHCTL_LOG"\n'
    )
    (bin_dir / "haproxy").write_text("#!/usr/bin/env bash\n")
    curl_body = (
        "#!/usr/bin/env bash\n"
        '[[ "$*" == *"8002"* ]] && '
        + ("exit 22\n" if fail_green_probe else "true\n")
        + "exit 0\n"
    )
    (bin_dir / "curl").write_text(curl_body)
    for executable in bin_dir.iterdir():
        executable.chmod(0o755)
    return subprocess.run(
        ["bash", "-c", f'set -Eeuo pipefail; source "{LIB}"; {command}'],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "AUTO_TRADER_BASE": str(base),
            "AUTO_TRADER_HAPROXY_RELOAD": "skip",
            "AUTO_TRADER_HEALTHCHECK_ATTEMPTS": "1",
            "AUTO_TRADER_HEALTHCHECK_INTERVAL_SECONDS": "0",
            "AUTO_TRADER_HEALTHCHECK_SKIP_WS": "1",
            "LAUNCHCTL_LOG": str(launch_log),
        },
    )


def test_api_only_flow_switches_and_drains_colors(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run(tmp_path, base, f'deploy_bluegreen_flow "{base}/releases/new"')
    assert proc.returncode == 0, proc.stderr
    assert (base / "shared" / "api-active-color").read_text().strip() == "green"
    assert (base / "current-green").resolve() == base / "releases" / "new"
    log = (tmp_path / "launchctl.log").read_text()
    assert "api-green" in log and "api-blue" in log
    assert "mcp-" not in log


def test_api_probe_failure_keeps_previous_color(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run(
        tmp_path,
        base,
        f'deploy_bluegreen_flow "{base}/releases/new"',
        fail_green_probe=True,
    )
    assert proc.returncode != 0
    assert (base / "shared" / "api-active-color").read_text().strip() == "blue"


def test_capture_and_rollback_are_api_only(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    capture = _run(tmp_path, base, "capture_bluegreen_state")
    assert capture.returncode == 0
    assert capture.stdout.strip().split() == [
        "blue",
        str(base / "releases" / "old"),
        "-",
    ]

    (base / "shared" / "api-active-color").write_text("green\n")
    rollback = _run(
        tmp_path,
        base,
        f'rollback_bluegreen_post_deploy blue "{base}/releases/old" "-"',
    )
    assert rollback.returncode == 0, rollback.stderr
    assert (base / "shared" / "api-active-color").read_text().strip() == "blue"
