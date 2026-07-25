"""ROB-831: deploy-native.sh must restart AND re-verify the fixed-profile MCP
services (mcp-analysis-readonly / mcp-account-read / mcp-tradingcodex-execution)
after the API blue/green cutover succeeds.

Incident (2026-07-11): after a normal deploy, PR-3a's order_proposal_void tool
was missing from :8770 (mcp-tradingcodex-execution) — the process was still
serving the previous release's code even though restart_single_active_services()
had already bounced it via `launchctl kickstart -k`. An operator had to run
`launchctl kickstart -k` by hand to pick up the new release.

verify_mcp_profile_release_paths() closes that gap: after the restart, it
confirms (via lsof) that the process actually LISTENING on each fixed-profile
MCP port has a working directory under the new release. Deploy-native.sh is a
top-level script (no main-guard, not sourceable in isolation for the full
flow — see tests/scripts/test_deploy_healthcheck_ws_advisory.py), so:

  - static/regex assertions cover wiring (array contents, call ordering)
  - dynamic assertions extract just the array + function definitions and
    execute them standalone against a stubbed `lsof`, covering the actual
    pass/fail logic without invoking any real deploy step.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "deploy-native.sh"

EXPECTED_PROFILE_PORTS = {
    "com.robinco.auto-trader.mcp-analysis-readonly": "8768",
    "com.robinco.auto-trader.mcp-account-read": "8769",
    "com.robinco.auto-trader.mcp-tradingcodex-execution": "8770",
}


# ---------------------------------------------------------------------------
# Static wiring assertions
# ---------------------------------------------------------------------------


def test_mcp_profile_ports_declared_for_all_three_fixed_profile_services() -> None:
    body = DEPLOY.read_text()
    for label, port in EXPECTED_PROFILE_PORTS.items():
        assert f'"{label}:{port}"' in body, (
            f"MCP_PROFILE_PORTS must declare {label}:{port} for release-path verification"
        )


def test_mcp_profile_labels_are_a_subset_of_single_active_labels() -> None:
    """Every label verified for release-path must also be restarted by
    restart_single_active_services(); otherwise the verification races a
    process that was never told to reload."""
    body = DEPLOY.read_text()
    for label in EXPECTED_PROFILE_PORTS:
        assert f'"{label}"' in body


def test_verify_function_called_after_restart_and_before_healthcheck() -> None:
    body = DEPLOY.read_text()
    lines = body.splitlines()

    def _first_call_idx(name: str) -> int | None:
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if re.match(rf"^{re.escape(name)}\s*\(\s*\)\s*\{{", stripped):
                continue  # function definition itself
            if re.search(rf"\b{re.escape(name)}\b", stripped):
                return i
        return None

    restart_idx = _first_call_idx("restart_single_active_services")
    verify_idx = _first_call_idx("verify_mcp_profile_release_paths")
    healthcheck_idx = _first_call_idx("run_healthcheck")

    assert restart_idx is not None
    assert verify_idx is not None
    assert healthcheck_idx is not None
    # verify_idx here matches BOTH the function definition line and the call
    # line; the definition necessarily precedes the call, so just assert the
    # LAST occurrence (the call site) is between restart and healthcheck.
    verify_call_idx = max(
        i
        for i, line in enumerate(lines)
        if "verify_mcp_profile_release_paths" in line
        and not re.match(r"^verify_mcp_profile_release_paths\(\)\s*\{", line.strip())
    )
    assert restart_idx < verify_call_idx < healthcheck_idx, (
        "verify_mcp_profile_release_paths must run AFTER "
        "restart_single_active_services and BEFORE run_healthcheck "
        f"(restart={restart_idx}, verify={verify_call_idx}, healthcheck={healthcheck_idx})"
    )


def test_verify_function_is_not_swallowed_by_best_effort_guard() -> None:
    """The call site must NOT be suffixed with `|| true` / `|| echo ...` —
    a failure here must propagate through `set -Eeuo pipefail` + `trap rollback
    ERR` like every other hard gate in the main flow, not be silently skipped."""
    body = DEPLOY.read_text()
    m = re.search(r"^verify_mcp_profile_release_paths\s*$", body, re.MULTILINE)
    assert m, "expected a bare `verify_mcp_profile_release_paths` call site"


# ---------------------------------------------------------------------------
# Dynamic behavior: extract array + function, run against a stubbed lsof
# ---------------------------------------------------------------------------


def _extract_array(body: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}=\(\n(.*?)\n\)", body, re.DOTALL | re.MULTILINE)
    assert m, f"{name}=(...) array not found in deploy-native.sh"
    return f"{name}=(\n{m.group(1)}\n)"


def _extract_function(body: str, name: str) -> str:
    m = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}", body, re.DOTALL | re.MULTILINE
    )
    assert m, f"{name}() function not found in deploy-native.sh"
    return f"{name}() {{\n{m.group(1)}\n}}"


def _build_lsof_stub(bin_dir: Path, map_file: Path) -> None:
    """lsof stub driven by a map file of `port pid cwd` lines (space-separated,
    cwd may not contain spaces since our test fixtures use plain tmp paths).

    Handles the two invocation shapes used by verify_mcp_profile_release_paths:
      lsof -tiTCP:<port> -sTCP:LISTEN        -> prints the PID or exits 1
      lsof -a -p <pid> -d cwd -Fn            -> prints -F-style p/f/n lines
    """
    stub = bin_dir / "lsof"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'map="{map_file}"\n'
        'if [[ "$1" == -tiTCP:* ]]; then\n'
        '  port="${1#-tiTCP:}"\n'
        "  while read -r p pid cwd; do\n"
        '    if [[ "$p" == "$port" ]]; then echo "$pid"; exit 0; fi\n'
        '  done < "$map"\n'
        "  exit 1\n"
        'elif [[ "$1" == "-a" ]]; then\n'
        '  pid="$3"\n'
        "  while read -r p qpid cwd; do\n"
        '    if [[ "$qpid" == "$pid" ]]; then\n'
        '      printf "p%s\\n" "$pid"\n'
        '      printf "fcwd\\n"\n'
        '      printf "n%s\\n" "$cwd"\n'
        "      exit 0\n"
        "    fi\n"
        '  done < "$map"\n'
        "  exit 1\n"
        "fi\n"
        "exit 1\n"
    )
    stub.chmod(0o755)


def _run_verify(
    tmp_path: Path,
    new_release: Path,
    map_lines: list[str],
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    body = DEPLOY.read_text()
    ports_array = _extract_array(body, "MCP_PROFILE_PORTS")
    fn = _extract_function(body, "verify_mcp_profile_release_paths")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    map_file = tmp_path / "lsof_map.txt"
    map_file.write_text("\n".join(map_lines) + "\n")
    _build_lsof_stub(bin_dir, map_file)

    script = (
        "set -Eeuo pipefail\n"
        'log() { printf "[log] %s\\n" "$*"; }\n'
        f'NEW_RELEASE="{new_release}"\n'
        f"{ports_array}\n"
        f"{fn}\n"
        "verify_mcp_profile_release_paths\n"
    )
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["AUTO_TRADER_MCP_RELEASE_VERIFY_ATTEMPTS"] = "1"
    env["AUTO_TRADER_MCP_RELEASE_VERIFY_INTERVAL_SECONDS"] = "0"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "-c", script], check=False, capture_output=True, text=True, env=env
    )


def test_verify_passes_when_all_three_profiles_report_expected_cwd(
    tmp_path: Path,
) -> None:
    new_release = tmp_path / "releases" / "sha-new"
    new_release.mkdir(parents=True)
    canonical = str(new_release.resolve())
    proc = _run_verify(
        tmp_path,
        new_release,
        [
            f"8768 111 {canonical}",
            f"8769 222 {canonical}",
            f"8770 333 {canonical}",
        ],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_verify_fails_when_no_process_listens_on_a_profile_port(
    tmp_path: Path,
) -> None:
    """Regression guard for a service that never actually restarted (e.g. crash
    loop) — must fail closed, not silently pass."""
    new_release = tmp_path / "releases" / "sha-new"
    new_release.mkdir(parents=True)
    canonical = str(new_release.resolve())
    proc = _run_verify(
        tmp_path,
        new_release,
        [
            f"8768 111 {canonical}",
            f"8769 222 {canonical}",
            # 8770 (mcp-tradingcodex-execution) has no listener at all.
        ],
    )
    assert proc.returncode != 0
    assert "8770" in (proc.stdout + proc.stderr)


def test_verify_fails_when_process_still_serving_stale_release_cwd(
    tmp_path: Path,
) -> None:
    """The ROB-831 incident itself: the process on :8770 is alive and would
    answer /health 200, but its cwd is still the OLD release — kickstart
    silently no-op'd. Must fail closed instead of letting deploy report success."""
    new_release = tmp_path / "releases" / "sha-new"
    old_release = tmp_path / "releases" / "sha-old"
    new_release.mkdir(parents=True)
    old_release.mkdir(parents=True)
    canonical_new = str(new_release.resolve())
    canonical_old = str(old_release.resolve())
    proc = _run_verify(
        tmp_path,
        new_release,
        [
            f"8768 111 {canonical_new}",
            f"8769 222 {canonical_new}",
            f"8770 333 {canonical_old}",
        ],
    )
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "8770" in combined
    assert "mcp-tradingcodex-execution" in combined
    assert canonical_old in combined


def test_verify_retries_before_giving_up(tmp_path: Path) -> None:
    """A process can take a moment after `launchctl kickstart -k` to actually
    rebind the port under the new cwd; the verification must retry rather than
    fail on the very first sample."""
    new_release = tmp_path / "releases" / "sha-new"
    new_release.mkdir(parents=True)
    canonical = str(new_release.resolve())

    body = DEPLOY.read_text()
    ports_array = _extract_array(body, "MCP_PROFILE_PORTS")
    fn = _extract_function(body, "verify_mcp_profile_release_paths")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    attempt_counter = tmp_path / "lsof_attempts_8770"
    # lsof stub: 8768/8769 always resolve correctly; 8770 only resolves to the
    # NEW release cwd from the 3rd call onward (simulating restart lag).
    stub = bin_dir / "lsof"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'counter="{attempt_counter}"\n'
        'if [[ "$1" == -tiTCP:* ]]; then\n'
        '  port="${1#-tiTCP:}"\n'
        '  case "$port" in\n'
        "    8768) echo 111; exit 0 ;;\n"
        "    8769) echo 222; exit 0 ;;\n"
        "    8770) echo 333; exit 0 ;;\n"
        "  esac\n"
        "  exit 1\n"
        'elif [[ "$1" == "-a" ]]; then\n'
        '  pid="$3"\n'
        '  case "$pid" in\n'
        f'    111) printf "p111\\nfcwd\\nn{canonical}\\n"; exit 0 ;;\n'
        f'    222) printf "p222\\nfcwd\\nn{canonical}\\n"; exit 0 ;;\n'
        "    333)\n"
        '      n=$(( $(cat "$counter" 2>/dev/null || echo 0) + 1 ))\n'
        '      echo "$n" > "$counter"\n'
        "      if (( n < 3 )); then\n"
        '        printf "p333\\nfcwd\\nn/old/release\\n"\n'
        "      else\n"
        f'        printf "p333\\nfcwd\\nn{canonical}\\n"\n'
        "      fi\n"
        "      exit 0\n"
        "      ;;\n"
        "  esac\n"
        "  exit 1\n"
        "fi\n"
        "exit 1\n"
    )
    stub.chmod(0o755)

    script = (
        "set -Eeuo pipefail\n"
        'log() { printf "[log] %s\\n" "$*"; }\n'
        f'NEW_RELEASE="{new_release}"\n'
        f"{ports_array}\n"
        f"{fn}\n"
        "verify_mcp_profile_release_paths\n"
    )
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["AUTO_TRADER_MCP_RELEASE_VERIFY_ATTEMPTS"] = "5"
    env["AUTO_TRADER_MCP_RELEASE_VERIFY_INTERVAL_SECONDS"] = "0"
    proc = subprocess.run(
        ["bash", "-c", script], check=False, capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert int(attempt_counter.read_text().strip()) >= 3


def test_verify_bounded_attempts_do_not_hang(tmp_path: Path) -> None:
    """A misconfigured attempts value must clamp to the default rather than
    looping forever or skipping the check entirely (mirrors bootstrap_color's
    clamp in native_deploy_lib.sh)."""
    new_release = tmp_path / "releases" / "sha-new"
    new_release.mkdir(parents=True)
    proc = _run_verify(
        tmp_path,
        new_release,
        [],  # nothing listens anywhere
        extra_env={
            "AUTO_TRADER_MCP_RELEASE_VERIFY_ATTEMPTS": "0",
            "AUTO_TRADER_MCP_RELEASE_VERIFY_INTERVAL_SECONDS": "0",
        },
    )
    assert proc.returncode != 0
