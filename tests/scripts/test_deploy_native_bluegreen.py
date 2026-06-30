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


def test_bootstrap_color_retries_and_path_bootouts_on_eio(tmp_path: Path) -> None:
    """launchctl bootstrap can return EIO ("5: Input/output error") right after a
    label bootout while launchd finishes its asynchronous teardown / keeps a
    stale plist-PATH registration around. bootstrap_color must boot out the
    plist PATH too (not just the label) and retry the bootstrap, mirroring the
    proven restart_single_active_services() path in scripts/deploy-native.sh.

    Regression guard for the deploy abort observed in GitHub Actions run
    28408243128 ("bootstrap api-blue failed" -> "Bootstrap failed: 5:
    Input/output error"): a single transient EIO must not kill the deploy.
    """
    base = _setup_base(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # launchctl stub: the FIRST `bootstrap` returns EIO (exit 5); later attempts
    # succeed. Every invocation is still logged so we can assert the call shape.
    (bin_dir / "launchctl").write_text(
        "#!/usr/bin/env bash\n"
        'echo "launchctl $*" >>"$LAUNCHCTL_LOG"\n'
        'if [[ "$1" == "bootstrap" ]]; then\n'
        '  cnt_file="$LAUNCHCTL_LOG.bootstrap_count"\n'
        '  cnt=$(( $(cat "$cnt_file" 2>/dev/null || echo 0) + 1 ))\n'
        '  echo "$cnt" >"$cnt_file"\n'
        "  if (( cnt == 1 )); then\n"
        '    echo "Bootstrap failed: 5: Input/output error" >&2\n'
        "    exit 5\n"
        "  fi\n"
        "fi\n"
        "exit 0\n"
    )
    (bin_dir / "launchctl").chmod(0o755)
    log = tmp_path / "launchctl.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
        "LAUNCHCTL_LOG": str(log),
        # Keep the retry backoff instant so the test stays fast.
        "AUTO_TRADER_BOOTSTRAP_RETRY_SECONDS": "0",
    }
    script = f'set -Eeuo pipefail\nsource "{LIB}"\nbootstrap_color api green\n'
    proc = subprocess.run(
        ["bash", "-c", script], check=False, capture_output=True, text=True, env=env
    )
    log_text = log.read_text()
    assert proc.returncode == 0, (
        f"bootstrap_color must survive a transient EIO via retry; "
        f"rc={proc.returncode} stderr={proc.stderr}\nlog:\n{log_text}"
    )

    target = f"{tmp_path}/Library/LaunchAgents/com.robinco.auto-trader.api-green.plist"
    bootout_lines = [
        line for line in log_text.splitlines() if line.startswith("launchctl bootout")
    ]
    # Must boot out the plist PATH (target), not just the label, to clear a
    # stale plist-path registration that survives a label-only bootout.
    assert any(target in line for line in bootout_lines), (
        f"expected a path-level bootout of {target}; bootout lines:\n"
        + "\n".join(bootout_lines)
    )

    bootstrap_lines = [
        line for line in log_text.splitlines() if line.startswith("launchctl bootstrap")
    ]
    # Must retry the bootstrap (>=2 attempts) so a single EIO does not abort.
    assert len(bootstrap_lines) >= 2, (
        "expected bootstrap to be retried after EIO; bootstrap lines:\n"
        + "\n".join(bootstrap_lines)
    )


def test_bootstrap_color_gives_up_after_bounded_attempts(tmp_path: Path) -> None:
    """When bootstrap EIOs on every attempt, bootstrap_color must give up after a
    BOUNDED number of attempts (no infinite loop) and return non-zero so the
    blue/green flow rolls back instead of hanging the deploy.
    """
    base = _setup_base(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # launchctl stub: every `bootstrap` returns EIO (exit 5); everything else ok.
    (bin_dir / "launchctl").write_text(
        "#!/usr/bin/env bash\n"
        'echo "launchctl $*" >>"$LAUNCHCTL_LOG"\n'
        '[[ "$1" == "bootstrap" ]] && exit 5\n'
        "exit 0\n"
    )
    (bin_dir / "launchctl").chmod(0o755)
    log = tmp_path / "launchctl.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
        "LAUNCHCTL_LOG": str(log),
        "AUTO_TRADER_BOOTSTRAP_ATTEMPTS": "3",
        "AUTO_TRADER_BOOTSTRAP_RETRY_SECONDS": "0",
    }
    script = f'set -Eeuo pipefail\nsource "{LIB}"\nbootstrap_color api green\n'
    proc = subprocess.run(
        ["bash", "-c", script], check=False, capture_output=True, text=True, env=env
    )
    log_text = log.read_text()
    assert proc.returncode != 0, (
        f"bootstrap_color must fail when every bootstrap EIOs; log:\n{log_text}"
    )
    bootstrap_lines = [
        line for line in log_text.splitlines() if line.startswith("launchctl bootstrap")
    ]
    # Exactly the configured attempt count — bounded, not infinite, not fewer.
    assert len(bootstrap_lines) == 3, (
        f"expected exactly 3 bootstrap attempts (bounded retry); got "
        f"{len(bootstrap_lines)}:\n" + "\n".join(bootstrap_lines)
    )
    # Must NOT have enabled/kickstarted a job that never bootstrapped.
    assert not any(
        line.startswith("launchctl kickstart") for line in log_text.splitlines()
    ), f"must not kickstart after exhausting bootstrap retries; log:\n{log_text}"


def test_bootstrap_color_clamps_misconfigured_attempt_count(tmp_path: Path) -> None:
    """A misconfigured AUTO_TRADER_BOOTSTRAP_ATTEMPTS (0 / non-numeric) must be
    clamped back to the default so the retry loop still runs at least once.
    Otherwise execution would fall through to enable/kickstart against a label
    that was never bootstrapped.
    """
    base = _setup_base(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # bootstrap always EIOs: with a working clamp the loop runs the default 5x
    # then gives up; with a broken clamp the loop runs 0x and falls through.
    (bin_dir / "launchctl").write_text(
        "#!/usr/bin/env bash\n"
        'echo "launchctl $*" >>"$LAUNCHCTL_LOG"\n'
        '[[ "$1" == "bootstrap" ]] && exit 5\n'
        "exit 0\n"
    )
    (bin_dir / "launchctl").chmod(0o755)
    log = tmp_path / "launchctl.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
        "LAUNCHCTL_LOG": str(log),
        "AUTO_TRADER_BOOTSTRAP_ATTEMPTS": "0",  # misconfigured
        "AUTO_TRADER_BOOTSTRAP_RETRY_SECONDS": "0",
    }
    script = f'set -Eeuo pipefail\nsource "{LIB}"\nbootstrap_color api green\n'
    proc = subprocess.run(
        ["bash", "-c", script], check=False, capture_output=True, text=True, env=env
    )
    log_text = log.read_text()
    bootstrap_lines = [
        line for line in log_text.splitlines() if line.startswith("launchctl bootstrap")
    ]
    # Clamped to the default of 5 attempts — never 0.
    assert len(bootstrap_lines) == 5, (
        f"expected attempt count clamped to default 5; got "
        f"{len(bootstrap_lines)}:\n" + "\n".join(bootstrap_lines)
    )
    assert proc.returncode != 0
    # Never kickstart a label that never bootstrapped.
    assert not any(
        line.startswith("launchctl kickstart") for line in log_text.splitlines()
    ), f"must not kickstart when no bootstrap succeeded; log:\n{log_text}"


def test_drain_color_bootouts(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run_bash("drain_color api blue", base, tmp_path)
    assert proc.returncode == 0, proc.stderr
    log = proc.launchctl_log  # type: ignore[attr-defined]
    assert "bootout" in log and "api-blue" in log
    # Must ALSO boot out by plist PATH so launchd does not accumulate stale
    # plist-path registrations, which are the source of later bootstrap EIO
    # (the leftover loaded inactive-color jobs seen on the host are evidence a
    # label-only drain does not fully deregister).
    target = f"{tmp_path}/Library/LaunchAgents/com.robinco.auto-trader.api-blue.plist"
    assert any(
        line.startswith("launchctl bootout") and target in line
        for line in log.splitlines()
    ), f"expected a path-level bootout of {target}; log:\n{log}"


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
