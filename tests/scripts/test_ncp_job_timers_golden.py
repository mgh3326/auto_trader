from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from scripts import ncp_job_timers_golden as golden

PREFECT_REPO = Path("/Users/mgh3326/services/prefect")
HELPER_FIXTURE = '''"""Captured production helper fallback for isolated CI."""
from pathlib import Path
DEFAULT_IMAGE_REPOSITORY = "ghcr.io/mgh3326/auto_trader"
def auto_trader_exec_mode(env=None):
    value = (env or {}).get("AUTO_TRADER_EXEC_MODE", "local").strip().lower()
    if value not in {"local", "docker"}: raise ValueError("AUTO_TRADER_EXEC_MODE must be local or docker")
    return value
def _docker_image(env):
    repository = env.get("AUTO_TRADER_IMAGE", DEFAULT_IMAGE_REPOSITORY)
    digest = Path(env["AUTO_TRADER_IMAGE_DIGEST_FILE"]).read_text().strip()
    return f"{repository}@{digest}" if not digest.startswith(repository) else digest
def auto_trader_command(local_command, *, env=None):
    effective_env = env or {}
    command = list(local_command)
    if auto_trader_exec_mode(effective_env) == "local": return command
    env_file = Path(effective_env.get("AUTO_TRADER_ENV_FILE", "/root/at-secrets/.env.api"))
    if command[1:3] == ["run", "python"]: container_command = ["/app/.venv/bin/python", *command[3:]]
    elif len(command) >= 3 and command[1] == "run": container_command = [f"/app/.venv/bin/{command[2]}", *command[3:]]
    else: container_command = command
    return ["docker", "run", "--rm", "--network", "host", "--workdir", "/app", "--env-file", str(env_file), _docker_image(effective_env), *container_command]
def run_auto_trader_command(*args, **kwargs):
    raise AssertionError("test capture must replace this")
'''


def _fake_prefect_repo(tmp_path: Path) -> Path:
    """Copy the real helper and three real task modules into a tiny checkout."""
    repo = tmp_path / "prefect"
    helper_target = repo / "src/robin_automation/auto_trader_execution.py"
    helper_target.parent.mkdir(parents=True)
    source_helper = PREFECT_REPO / "src/robin_automation/auto_trader_execution.py"
    if source_helper.is_file():
        shutil.copy2(source_helper, helper_target)
    else:
        helper_target.write_text(HELPER_FIXTURE)
    for filename in (
        "investor_flow_snapshots.py",
        "toss_warnings_sync.py",
        "invest_screener_snapshots_us.py",
    ):
        target = repo / "flows/auto_trader" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        source_flow = PREFECT_REPO / "flows/auto_trader" / filename
        if source_flow.is_file():
            shutil.copy2(source_flow, target)
        else:
            target.write_text(
                "from types import SimpleNamespace\n"
                "import subprocess\n"
                "from robin_automation.auto_trader_execution import run_auto_trader_command\n"
                "def _run(docker_command, timeout_seconds):\n"
                " return subprocess.CompletedProcess([], 0, '', '')\n"
                "def _task(command):\n"
                " def fn(**kwargs):\n"
                "  import sys\n"
                "  return sys.modules[__name__].run_auto_trader_command([], docker_command=command, timeout=kwargs['timeout_seconds'])\n"
                " return SimpleNamespace(fn=fn)\n"
                + (
                    {
                        "investor_flow_snapshots.py": "run_investor_flow_snapshot_build = _task(['/app/.venv/bin/python','-m','scripts.build_investor_flow_snapshots','--market','kr','--days','5','--batch-size','100','--concurrency','4','--all','--commit'])\n",
                        "toss_warnings_sync.py": "run_toss_warnings_sync = _task(['/app/.venv/bin/python','-m','scripts.sync_toss_warnings'])\n",
                        "invest_screener_snapshots_us.py": "run_us_invest_screener_snapshot_build = _task(['/app/.venv/bin/python','-m','scripts.build_invest_screener_snapshots','--market','us','--batch-size','200','--concurrency','4','--all','--common-stocks-only','--commit'])\n",
                    }[filename]
                )
            )
    support = repo / "src/robin_automation"
    (support / "news_ingestor.py").write_text(
        "def redact_secrets(value):\n    return value\n"
    )
    (support / "placement.py").write_text(
        "def deployment_source_root(*_): return None\n"
        "def placement_job_variables(*_): return {}\n"
        "def placement_work_pool(*_): return ''\n"
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return repo


def test_check_matches_generated_fixture_and_detects_one_token_mutant(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _fake_prefect_repo(tmp_path)
    fixture = tmp_path / "ncp_job_timers_prefect_argv.json"
    monkeypatch.setattr(golden, "FIXTURE", fixture)

    assert golden.run_cli(["--prefect-repo", str(repo), "--write"]) == 0
    assert golden.run_cli(["--prefect-repo", str(repo), "--check"]) == 0

    fixture.write_text(fixture.read_text().replace("--commit", "--mutant", 1))
    # Assertion mutant: removing the diff check makes this assertion red.
    assert golden.run_cli(["--prefect-repo", str(repo), "--check"]) == 1


def test_missing_prefect_repo_is_explicit_exit_two(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing-prefect"
    assert golden.run_cli(["--prefect-repo", str(missing), "--check"]) == 2
    assert f"Prefect repo is unavailable: {missing}" in capsys.readouterr().err
