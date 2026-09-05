from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from scripts import ncp_job_timers_golden as golden

PREFECT_REPO = Path("/Users/mgh3326/services/prefect")
HELPER_FIXTURE = '''"""Captured production helper fallback for isolated CI."""
import os
from pathlib import Path
DEFAULT_IMAGE_REPOSITORY = "ghcr.io/mgh3326/auto_trader"
def auto_trader_exec_mode(env=None):
    effective_env = os.environ if env is None else env
    value = effective_env.get("AUTO_TRADER_EXEC_MODE", "local").strip().lower()
    if value not in {"local", "docker"}: raise ValueError("AUTO_TRADER_EXEC_MODE must be local or docker")
    return value
def _docker_image(env):
    repository = env.get("AUTO_TRADER_IMAGE", DEFAULT_IMAGE_REPOSITORY)
    digest = Path(env["AUTO_TRADER_IMAGE_DIGEST_FILE"]).read_text().strip()
    return f"{repository}@{digest}" if not digest.startswith(repository) else digest
def auto_trader_command(local_command, *, env=None):
    effective_env = os.environ if env is None else env
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


_FLOW_FILENAMES = (
    "investor_flow_snapshots.py",
    "toss_warnings_sync.py",
    "invest_screener_snapshots_us.py",
    "invest_crypto_screener_snapshots.py",
    "invest_kr_fundamentals_snapshots.py",
    "market_valuation_snapshots_us.py",
    "us_fundamentals_snapshots.py",
    "toss_symbol_master_sync.py",
    "invest_crypto_insight_snapshots.py",
)


def _standin_task(
    *, task_name: str, command: list[str], commit_env: str | None = None
) -> str:
    """Make a captureable task with Prefect's direct-env then env-file gate."""
    return dedent(
        f"""
        import os
        from pathlib import Path
        from types import SimpleNamespace

        from robin_automation.auto_trader_execution import run_auto_trader_command

        COMMAND = {command!r}
        COMMIT_ENV = {commit_env!r}

        def _env_file_commit_gate_enabled(key):
            direct = os.getenv(key)
            if direct is not None:
                return direct.strip().lower() in {{"1", "true", "yes", "on"}}
            env_file = Path(os.getenv("AUTO_TRADER_ENV_FILE") or os.getenv("ENV_FILE") or "")
            try:
                for line in env_file.read_text(errors="ignore").splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if stripped.startswith(f"{{key}}="):
                        value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                        return value.lower() in {{"1", "true", "yes", "on"}}
            except FileNotFoundError:
                return False
            return False

        def _run(**kwargs):
            argv = list(COMMAND)
            if (
                COMMIT_ENV
                and kwargs.get("commit_with_gate")
                and _env_file_commit_gate_enabled(COMMIT_ENV)
            ):
                argv.append("--commit")
            return run_auto_trader_command(
                [], docker_command=argv, timeout=kwargs["timeout_seconds"]
            )

        {task_name} = SimpleNamespace(fn=_run)
        """
    )


def _standin_flow(filename: str) -> str:
    tasks: dict[str, tuple[str, list[str], str | None]] = {
        "investor_flow_snapshots.py": (
            "run_investor_flow_snapshot_build",
            [
                "/app/.venv/bin/python",
                "-m",
                "scripts.build_investor_flow_snapshots",
                "--market",
                "kr",
                "--days",
                "5",
                "--batch-size",
                "100",
                "--concurrency",
                "4",
                "--all",
                "--commit",
            ],
            None,
        ),
        "toss_warnings_sync.py": (
            "run_toss_warnings_sync",
            ["/app/.venv/bin/python", "-m", "scripts.sync_toss_warnings"],
            None,
        ),
        "invest_screener_snapshots_us.py": (
            "run_us_invest_screener_snapshot_build",
            [
                "/app/.venv/bin/python",
                "-m",
                "scripts.build_invest_screener_snapshots",
                "--market",
                "us",
                "--batch-size",
                "200",
                "--concurrency",
                "4",
                "--all",
                "--common-stocks-only",
                "--commit",
            ],
            None,
        ),
        "invest_crypto_screener_snapshots.py": (
            "run_invest_crypto_screener_snapshot_build",
            [
                "/app/.venv/bin/python",
                "-m",
                "scripts.build_invest_crypto_screener_snapshots",
                "--all",
            ],
            "INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED",
        ),
        "invest_kr_fundamentals_snapshots.py": (
            "run_kr_fundamentals_snapshot_build",
            [
                "/app/.venv/bin/python",
                "-m",
                "scripts.build_invest_kr_fundamentals_snapshots",
                "--all",
            ],
            "INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED",
        ),
        "market_valuation_snapshots_us.py": (
            "run_us_market_valuation_snapshot_build",
            [
                "/app/.venv/bin/python",
                "-m",
                "scripts.build_market_valuation_snapshots",
                "--market",
                "us",
                "--batch-size",
                "100",
                "--concurrency",
                "4",
                "--all",
                "--common-stocks-only",
            ],
            "MARKET_VALUATION_SNAPSHOTS_COMMIT_ENABLED",
        ),
        "us_fundamentals_snapshots.py": (
            "run_us_fundamentals_snapshot_build",
            [
                "/app/.venv/bin/python",
                "-m",
                "scripts.build_us_fundamentals_snapshots",
                "--concurrency",
                "4",
                "--all",
                "--with-dividends",
            ],
            "MARKET_VALUATION_SNAPSHOTS_COMMIT_ENABLED",
        ),
    }
    task_name, command, commit_env = tasks[filename]
    return _standin_task(task_name=task_name, command=command, commit_env=commit_env)


_TOSS_FLOW_STANDIN = dedent(
    """
    from types import SimpleNamespace

    from robin_automation.toss_symbol_master import run_toss_symbol_master_sync

    def _sync_market(market, dry_run):
        return run_toss_symbol_master_sync(market=market, dry_run=dry_run)

    sync_market = SimpleNamespace(fn=_sync_market)
    """
)

_TOSS_SUPPORT_STANDIN = dedent(
    """
    from robin_automation.auto_trader_execution import run_auto_trader_command

    def run_toss_symbol_master_sync(*, market, dry_run=False):
        command = [
            "uv", "run", "python", "-m", "scripts.sync_toss_symbol_master",
            "--market", market, "--all",
        ]
        if not dry_run:
            command.append("--commit")
        return run_auto_trader_command(command, timeout=900)
    """
)

_INSIGHT_STANDIN = dedent(
    '''
    from types import SimpleNamespace

    from robin_automation.auto_trader_execution import run_auto_trader_command

    RUNNER_CODE = """
    from __future__ import annotations

    print("synthetic crypto insight runner")
    """

    def _run(**kwargs):
        return run_auto_trader_command(
            [],
            docker_command=["/app/.venv/bin/python", "-c", RUNNER_CODE],
            timeout=kwargs["timeout_seconds"],
        )

    run_invest_crypto_insight_snapshot_build = SimpleNamespace(fn=_run)
    '''
)


def _copy_or_standin(source: Path, target: Path, standin: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        shutil.copy2(source, target)
    else:
        target.write_text(standin)


def _fake_prefect_repo(tmp_path: Path) -> Path:
    """Build a 9-flow capture checkout, with functional CI stand-ins."""
    repo = tmp_path / "prefect"
    helper_target = repo / "src/robin_automation/auto_trader_execution.py"
    _copy_or_standin(
        PREFECT_REPO / "src/robin_automation/auto_trader_execution.py",
        helper_target,
        HELPER_FIXTURE,
    )
    for filename in _FLOW_FILENAMES:
        target = repo / "flows/auto_trader" / filename
        source_flow = PREFECT_REPO / "flows/auto_trader" / filename
        if filename == "toss_symbol_master_sync.py":
            standin = _TOSS_FLOW_STANDIN
        elif filename == "invest_crypto_insight_snapshots.py":
            standin = _INSIGHT_STANDIN
        else:
            standin = _standin_flow(filename)
        _copy_or_standin(source_flow, target, standin)
    support = repo / "src/robin_automation"
    (support / "news_ingestor.py").write_text(
        "def redact_secrets(value):\n    return value\n"
    )
    (support / "placement.py").write_text(
        "def deployment_source_root(*_): return None\n"
        "def placement_job_variables(*_): return {}\n"
        "def placement_work_pool(*_): return ''\n"
    )
    _copy_or_standin(
        PREFECT_REPO / "src/robin_automation/toss_symbol_master.py",
        support / "toss_symbol_master.py",
        _TOSS_SUPPORT_STANDIN,
    )
    (support / "discord.py").write_text(
        "def send_discord_mbp_server_message(*_, **__): return True\n"
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
    frozen_module = tmp_path / "build_crypto_insight_snapshots.py"
    monkeypatch.setattr(golden, "FIXTURE", fixture)
    monkeypatch.setattr(golden, "FROZEN_INSIGHT_MODULE", frozen_module)

    assert golden.run_cli(["--prefect-repo", str(repo), "--write"]) == 0
    assert golden.run_cli(["--prefect-repo", str(repo), "--check"]) == 0

    fixture.write_text(fixture.read_text().replace("--commit", "--mutant", 1))
    # Assertion mutant: removing the diff check makes this assertion red.
    assert golden.run_cli(["--prefect-repo", str(repo), "--check"]) == 1


def test_synthetic_prefect_repo_runs_without_local_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "PREFECT_REPO", tmp_path / "missing")
    repo = _fake_prefect_repo(tmp_path)
    fixture = tmp_path / "ncp_job_timers_prefect_argv.json"
    frozen_module = tmp_path / "build_crypto_insight_snapshots.py"
    monkeypatch.setattr(golden, "FIXTURE", fixture)
    monkeypatch.setattr(golden, "FROZEN_INSIGHT_MODULE", frozen_module)

    assert golden.run_cli(["--prefect-repo", str(repo), "--write"]) == 0
    assert golden.run_cli(["--prefect-repo", str(repo), "--check"]) == 0

    payload = json.loads(fixture.read_text())
    gate_jobs = [job for job in payload["jobs"] if "commit_env" in job]
    assert len(gate_jobs) == 4
    assert all(job["argv_commit_on"][-1] == "--commit" for job in gate_jobs)
    assert all(job["argv_commit_off"][-1] != "--commit" for job in gate_jobs)
    toss = next(
        job for job in payload["jobs"] if job["unit"] == "toss-symbol-master-sync"
    )
    assert [step["timeout_seconds"] for step in toss["steps"]] == [900, 900]
    assert [step["argv"][-2] for step in toss["steps"]] == ["--all", "--all"]
    insight = next(
        job
        for job in payload["jobs"]
        if job["unit"] == "crypto-invest-insight-snapshots"
    )
    assert (
        hashlib.sha256(frozen_module.read_bytes()).hexdigest()
        == insight["runner_code_sha256"]
    )

    env_file = tmp_path / "gate.env"
    env_file.write_text("INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED=on\n")
    monkeypatch.setenv("AUTO_TRADER_ENV_FILE", str(env_file))
    monkeypatch.delenv("INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED", raising=False)
    source_root = repo / "src"
    sys.path.insert(0, str(source_root))
    try:
        gate_module = golden._load_module(
            "_ncp_timer_standin_gate",
            repo / "flows/auto_trader/invest_crypto_screener_snapshots.py",
        )
        assert gate_module._env_file_commit_gate_enabled(
            "INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED"
        )
        monkeypatch.setenv("INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED", "false")
        assert not gate_module._env_file_commit_gate_enabled(
            "INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED"
        )
    finally:
        sys.path.remove(str(source_root))


def test_frozen_crypto_insight_runner_matches_golden_sha_and_module_contract() -> None:
    payload = json.loads(golden.FIXTURE.read_text())
    insight = next(
        job
        for job in payload["jobs"]
        if job["unit"] == "crypto-invest-insight-snapshots"
    )
    source = golden.FROZEN_INSIGHT_MODULE.read_bytes()
    assert hashlib.sha256(source).hexdigest() == insight["runner_code_sha256"]
    text = source.decode()
    assert "__file__" not in text
    assert "sys.argv" not in text


def test_missing_prefect_repo_is_explicit_exit_two(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing-prefect"
    assert golden.run_cli(["--prefect-repo", str(missing), "--check"]) == 2
    assert f"Prefect repo is unavailable: {missing}" in capsys.readouterr().err
