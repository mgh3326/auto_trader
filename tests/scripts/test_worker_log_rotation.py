from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ROTATE_SCRIPT = REPO_ROOT / "ops" / "native" / "scripts" / "rotate-worker-logs.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _rotation_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    base = tmp_path / "service"
    log_dir = base / "logs"
    shared_dir = base / "shared"
    run_dir = base / "run"
    bin_dir = tmp_path / "bin"
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    for directory in (log_dir, shared_dir, run_dir, bin_dir, launch_agents):
        directory.mkdir(parents=True, exist_ok=True)

    arm_file = shared_dir / "log-rotation.enabled"
    worker_plist = launch_agents / "com.robinco.auto-trader.worker.plist"
    worker_plist.write_text("<plist/>")
    launchctl_log = tmp_path / "launchctl.log"

    launchctl = bin_dir / "launchctl"
    _write_executable(
        launchctl,
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >>"$AUTO_TRADER_TEST_LAUNCHCTL_LOG"
exit 0
""",
    )
    lsof = bin_dir / "lsof"
    _write_executable(lsof, "#!/usr/bin/env bash\nexit 1\n")

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
        "AUTO_TRADER_LOG_DIR": str(log_dir),
        "AUTO_TRADER_LOG_ROTATION_ARM_FILE": str(arm_file),
        "AUTO_TRADER_LOG_MAX_BYTES": "1024",
        "AUTO_TRADER_LOG_ARCHIVE_COUNT": "2",
        "AUTO_TRADER_WORKER_PLIST": str(worker_plist),
        "AUTO_TRADER_LAUNCHCTL_BIN": str(launchctl),
        "AUTO_TRADER_LSOF_BIN": str(lsof),
        "AUTO_TRADER_TEST_LAUNCHCTL_LOG": str(launchctl_log),
    }
    return env, log_dir, arm_file


def test_rotation_is_inert_until_operator_arms_it(tmp_path: Path) -> None:
    env, log_dir, _ = _rotation_env(tmp_path)
    err_log = log_dir / "com.robinco.auto-trader.worker.err.log"
    err_log.write_bytes(b"x" * 2048)
    env["AUTO_TRADER_NEWSYSLOG_BIN"] = str(tmp_path / "not-used-newsyslog")

    proc = subprocess.run(
        ["bash", str(ROTATE_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    assert err_log.stat().st_size == 2048
    assert not list(log_dir.glob("*.0*"))


def test_open_descriptors_block_rotation_and_worker_is_restored(
    tmp_path: Path,
) -> None:
    env, log_dir, arm_file = _rotation_env(tmp_path)
    arm_file.touch()
    err_log = log_dir / "com.robinco.auto-trader.worker.err.log"
    err_log.write_bytes(b"x" * 2048)

    lsof = Path(env["AUTO_TRADER_LSOF_BIN"])
    _write_executable(lsof, "#!/usr/bin/env bash\nprintf '4242\\n'\n")
    newsyslog = tmp_path / "bin" / "newsyslog"
    called = tmp_path / "newsyslog.called"
    _write_executable(
        newsyslog,
        f"#!/usr/bin/env bash\ntouch {called}\n",
    )
    env["AUTO_TRADER_NEWSYSLOG_BIN"] = str(newsyslog)
    env["AUTO_TRADER_LOG_FD_WAIT_ATTEMPTS"] = "1"
    env["AUTO_TRADER_LOG_FD_WAIT_SECONDS"] = "0"

    proc = subprocess.run(
        ["bash", str(ROTATE_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert proc.returncode == 75
    assert "open file descriptors remain" in proc.stderr
    assert err_log.stat().st_size == 2048
    assert not called.exists()

    launchctl_calls = (tmp_path / "launchctl.log").read_text()
    assert "bootout" in launchctl_calls
    assert "bootstrap" in launchctl_calls
    assert "kickstart -k" in launchctl_calls


@pytest.mark.skipif(
    shutil.which("newsyslog") is None,
    reason="newsyslog is only available on macOS/BSD",
)
def test_real_newsyslog_enforces_size_and_archive_count(tmp_path: Path) -> None:
    env, log_dir, arm_file = _rotation_env(tmp_path)
    arm_file.touch()
    env["AUTO_TRADER_NEWSYSLOG_BIN"] = shutil.which("newsyslog") or ""
    err_log = log_dir / "com.robinco.auto-trader.worker.err.log"

    for index in range(4):
        err_log.write_bytes(bytes([65 + index]) * 2048)
        proc = subprocess.run(
            ["bash", str(ROTATE_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert err_log.stat().st_size < 1024

    archives = list(log_dir.glob(f"{err_log.name}.[0-9]*"))
    assert len(archives) == 2

    launchctl_calls = (tmp_path / "launchctl.log").read_text()
    assert "bootout" in launchctl_calls
    assert "bootstrap" in launchctl_calls
    assert "kickstart -k" in launchctl_calls
