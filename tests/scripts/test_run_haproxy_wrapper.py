"""ROB-259 review fix: run-haproxy.sh wrapper.

Verifies the plist binary-path mismatch is resolved by routing through a
wrapper that uses `command -v haproxy` so the same plist works on Intel and
Apple Silicon Homebrew layouts.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "ops" / "native" / "scripts" / "run-haproxy.sh"


def _stub_dir(tmp_path: Path, *, with_haproxy: bool) -> Path:
    """Build a PATH dir with optional fake haproxy binary that echoes argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if with_haproxy:
        stub = bin_dir / "haproxy"
        stub.write_text('#!/usr/bin/env bash\necho "haproxy invoked: $*"\nexit 0\n')
        stub.chmod(0o755)
    return bin_dir


def _run(
    env_extra: dict[str, str], tmp_path: Path, with_haproxy: bool
) -> subprocess.CompletedProcess:
    bin_dir = _stub_dir(tmp_path, with_haproxy=with_haproxy)
    # Need /usr/bin:/bin on PATH so subprocess can find `bash` itself; the test
    # controls haproxy availability by whether we wrote a stub into bin_dir.
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(tmp_path / "auto_trader"),
        **env_extra,
    }
    return subprocess.run(
        ["bash", str(WRAPPER)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _make_cfg(base: Path) -> Path:
    cfg = base / "shared" / "haproxy" / "haproxy.cfg"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("global\n  daemon\n")
    return cfg


def test_wrapper_no_hardcoded_homebrew_path() -> None:
    """The wrapper must NOT hardcode /opt/homebrew/bin/haproxy (Intel Mac compat)."""
    body = WRAPPER.read_text()
    assert "/opt/homebrew/bin/haproxy" not in body
    assert "command -v haproxy" in body


def test_wrapper_executes_haproxy_with_master_worker(tmp_path: Path) -> None:
    base = tmp_path / "auto_trader"
    _make_cfg(base)
    proc = _run({}, tmp_path, with_haproxy=True)
    assert proc.returncode == 0, proc.stderr
    assert "haproxy invoked:" in proc.stdout
    # -W (master-worker) is required for SIGUSR2 seamless reload
    assert "-W" in proc.stdout
    # -f points at the live cfg under shared/haproxy/
    assert str(base / "shared" / "haproxy" / "haproxy.cfg") in proc.stdout


def test_wrapper_errors_when_haproxy_missing(tmp_path: Path) -> None:
    base = tmp_path / "auto_trader"
    _make_cfg(base)
    proc = _run({}, tmp_path, with_haproxy=False)
    assert proc.returncode == 78
    assert "not found on PATH" in proc.stderr
    assert "brew install haproxy" in proc.stderr


def test_wrapper_errors_when_cfg_missing(tmp_path: Path) -> None:
    # AUTO_TRADER_BASE exists but no cfg yet — common when cutover wasn't run.
    (tmp_path / "auto_trader" / "shared" / "haproxy").mkdir(parents=True, exist_ok=True)
    proc = _run({}, tmp_path, with_haproxy=True)
    assert proc.returncode == 78
    assert "config missing" in proc.stderr
    assert "native_haproxy_first_cutover.sh" in proc.stderr


def test_wrapper_honors_AUTO_TRADER_HAPROXY_LIVE_override(tmp_path: Path) -> None:
    base = tmp_path / "auto_trader"
    base.mkdir()
    custom_cfg = tmp_path / "custom.cfg"
    custom_cfg.write_text("global\n")
    proc = _run(
        {"AUTO_TRADER_HAPROXY_LIVE": str(custom_cfg)},
        tmp_path,
        with_haproxy=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert str(custom_cfg) in proc.stdout
