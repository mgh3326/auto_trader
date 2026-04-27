from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.models.trading import InstrumentType
from app.schemas.tradingagents_research import TradingAgentsRunnerResult
from app.services import tradingagents_research_service as svc

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tradingagents"


class FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.kill_called = False
        self.wait = AsyncMock(return_value=None)

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.kill_called = True


def _payload(name: str = "runner_ok_nvda.json") -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _payload_bytes(name: str = "runner_ok_nvda.json") -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def _configure_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    repo = tmp_path / "TradingAgents"
    runner = repo / "scripts" / "run_auto_trader_research.py"
    runner.parent.mkdir(parents=True)
    runner.write_text("print('stub')\n", encoding="utf-8")
    monkeypatch.setattr(svc.settings, "tradingagents_repo_path", str(repo))
    monkeypatch.setattr(svc.settings, "tradingagents_python", sys.executable)
    monkeypatch.setattr(svc.settings, "tradingagents_runner_path", None)
    monkeypatch.setattr(
        svc.settings, "tradingagents_base_url", "https://shim.invalid/v1"
    )
    monkeypatch.setattr(svc.settings, "tradingagents_model", "gpt-5.5")
    monkeypatch.setattr(svc.settings, "tradingagents_default_analysts", "market")
    monkeypatch.setattr(svc.settings, "tradingagents_subprocess_timeout_sec", 15)
    monkeypatch.setattr(svc.settings, "tradingagents_max_debate_rounds", 1)
    monkeypatch.setattr(svc.settings, "tradingagents_max_risk_discuss_rounds", 1)
    monkeypatch.setattr(svc.settings, "tradingagents_max_recur_limit", 30)
    monkeypatch.setattr(svc.settings, "tradingagents_output_language", "English")
    monkeypatch.setattr(svc.settings, "tradingagents_checkpoint_enabled", False)
    monkeypatch.setattr(svc.settings, "tradingagents_memory_log_path", None)
    return runner


def _required_settings() -> dict[str, str]:
    return {
        "kis_app_key": "dummy",
        "kis_app_secret": "dummy",
        "opendart_api_key": "dummy",
        "DATABASE_URL": "postgresql+asyncpg://postgres:postgres@localhost/test",
        "upbit_access_key": "dummy",
        "upbit_secret_key": "dummy",
        "SECRET_KEY": "Test_Secret_Key_12345_Test_Secret_Key_12345",
    }


def test_settings_parse_tradingagents_env_values(tmp_path: Path) -> None:
    repo_path = tmp_path / "TradingAgents"
    python_path = repo_path / ".venv" / "bin" / "python"
    runner_path = tmp_path / "runner.py"
    memory_path = tmp_path / "tradingagents-memory"

    settings = Settings(
        **_required_settings(),
        tradingagents_repo_path=str(repo_path),
        tradingagents_python=str(python_path),
        tradingagents_runner_path=str(runner_path),
        tradingagents_base_url="https://localhost:8796/v1",
        tradingagents_model="local-model",
        tradingagents_default_analysts="market,news",
        tradingagents_subprocess_timeout_sec=45,
        tradingagents_max_debate_rounds=2,
        tradingagents_max_risk_discuss_rounds=3,
        tradingagents_max_recur_limit=12,
        tradingagents_output_language="Korean",
        tradingagents_checkpoint_enabled=True,
        tradingagents_memory_log_path=str(memory_path),
    )

    assert settings.tradingagents_repo_path == str(repo_path)
    assert settings.tradingagents_runner_path == str(runner_path)
    assert settings.tradingagents_subprocess_timeout_sec == 45
    assert settings.tradingagents_checkpoint_enabled is True


def test_schema_accepts_ok_payload_and_rejects_invariant_violation() -> None:
    ok = TradingAgentsRunnerResult.model_validate(_payload())
    assert ok.advisory_only is True
    assert ok.execution_allowed is False

    with pytest.raises(ValidationError):
        TradingAgentsRunnerResult.model_validate(
            _payload("runner_invariant_violation.json")
        )


@pytest.mark.asyncio
async def test_runner_ok_returns_validated_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = _configure_settings(monkeypatch, tmp_path)
    proc = FakeProcess(stdout=_payload_bytes())
    create = AsyncMock(return_value=proc)
    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", create)

    result = await svc.run_tradingagents_research(
        symbol="NVDA",
        instrument_type=InstrumentType.equity_us,
        analysts=["market", "news"],
    )

    assert result.symbol == "NVDA"
    assert result.advisory_only is True
    assert result.execution_allowed is False
    argv = create.call_args.args
    assert argv[:2] == (str(Path(sys.executable).resolve()), str(runner.resolve()))
    assert "--symbol" in argv
    assert ";" not in argv
    assert create.call_args.kwargs["cwd"] == str(runner.parents[1])
    assert create.call_args.kwargs["stdout"] == asyncio.subprocess.PIPE
    assert create.call_args.kwargs["stderr"] == asyncio.subprocess.PIPE


@pytest.mark.asyncio
async def test_runner_nonzero_exit_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_settings(monkeypatch, tmp_path)
    proc = FakeProcess(stdout=b"{}", stderr=b"boom", returncode=1)
    monkeypatch.setattr(
        svc.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )

    with pytest.raises(svc.TradingAgentsRunnerError, match="exited with 1"):
        await svc.run_tradingagents_research(
            symbol="NVDA", instrument_type=InstrumentType.equity_us
        )


@pytest.mark.asyncio
async def test_runner_timeout_kills_and_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_settings(monkeypatch, tmp_path)
    proc = FakeProcess()
    monkeypatch.setattr(
        svc.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )

    async def _timeout(awaitable, *, timeout):
        _ = timeout
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(svc.asyncio, "wait_for", _timeout)

    with pytest.raises(svc.TradingAgentsRunnerError, match="timed out"):
        await svc.run_tradingagents_research(
            symbol="NVDA", instrument_type=InstrumentType.equity_us
        )

    assert proc.kill_called is True
    proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_runner_non_json_stdout_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_settings(monkeypatch, tmp_path)
    proc = FakeProcess(stdout=b"<<not json>>")
    monkeypatch.setattr(
        svc.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )

    with pytest.raises(svc.TradingAgentsRunnerError, match="non-JSON"):
        await svc.run_tradingagents_research(
            symbol="NVDA", instrument_type=InstrumentType.equity_us
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "error"),
        ("advisory_only", False),
        ("execution_allowed", True),
    ],
)
async def test_runner_advisory_invariant_violations_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    _configure_settings(monkeypatch, tmp_path)
    payload = _payload()
    payload[field] = value
    proc = FakeProcess(stdout=json.dumps(payload).encode("utf-8"))
    monkeypatch.setattr(
        svc.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )

    with pytest.raises(svc.AdvisoryInvariantViolation):
        await svc.run_tradingagents_research(
            symbol="NVDA", instrument_type=InstrumentType.equity_us
        )


@pytest.mark.asyncio
async def test_warnings_structured_output_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(
        svc.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=FakeProcess(stdout=_payload_bytes())),
    )

    result = await svc.run_tradingagents_research(
        symbol="NVDA", instrument_type=InstrumentType.equity_us
    )

    assert result.warnings.structured_output == [
        "earnings sensitivity noted",
        "macro liquidity risk noted",
    ]


@pytest.mark.asyncio
async def test_symbol_argv_validation_rejects_shell_metachars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_settings(monkeypatch, tmp_path)
    create = AsyncMock()
    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", create)

    with pytest.raises(ValueError, match="symbol"):
        await svc.run_tradingagents_research(
            symbol="AAPL; rm -rf /",
            instrument_type=InstrumentType.equity_us,
        )

    create.assert_not_called()


@pytest.mark.asyncio
async def test_analyst_argv_validation_rejects_shell_metachars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_settings(monkeypatch, tmp_path)
    create = AsyncMock()
    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", create)

    with pytest.raises(ValueError, match="analyst"):
        await svc.run_tradingagents_research(
            symbol="NVDA",
            instrument_type=InstrumentType.equity_us,
            analysts=["market;bad"],
        )

    create.assert_not_called()


@pytest.mark.asyncio
async def test_settings_missing_repo_path_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(svc.settings, "tradingagents_repo_path", None)

    with pytest.raises(svc.TradingAgentsNotConfigured):
        await svc.run_tradingagents_research(
            symbol="NVDA", instrument_type=InstrumentType.equity_us
        )


@pytest.mark.asyncio
async def test_filtered_env_does_not_leak_unrelated_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("ROB9_UNRELATED_SECRET", "must-not-leak")
    monkeypatch.setenv("TRADINGAGENTS_ALLOWED_FLAG", "allowed")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    create = AsyncMock(return_value=FakeProcess(stdout=_payload_bytes()))
    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", create)

    await svc.run_tradingagents_research(
        symbol="NVDA", instrument_type=InstrumentType.equity_us
    )

    child_env = create.call_args.kwargs["env"]
    assert "ROB9_UNRELATED_SECRET" not in child_env
    assert child_env["TRADINGAGENTS_ALLOWED_FLAG"] == "allowed"
    assert child_env["OPENAI_API_KEY"] == "test-key"


@pytest.mark.asyncio
async def test_default_openai_api_key_injected_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_settings(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    create = AsyncMock(return_value=FakeProcess(stdout=_payload_bytes()))
    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", create)

    await svc.run_tradingagents_research(
        symbol="NVDA", instrument_type=InstrumentType.equity_us
    )

    assert create.call_args.kwargs["env"]["OPENAI_API_KEY"] == "no-key-required"


@pytest.mark.asyncio
async def test_memory_log_disabled_writes_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_settings(monkeypatch, tmp_path)
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(svc.settings, "tradingagents_memory_log_path", None)
    monkeypatch.setattr(
        svc.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=FakeProcess(stdout=_payload_bytes())),
    )

    await svc.run_tradingagents_research(
        symbol="NVDA", instrument_type=InstrumentType.equity_us
    )

    assert not log_dir.exists()


@pytest.mark.asyncio
async def test_memory_log_enabled_writes_validated_payload_under_configured_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_settings(monkeypatch, tmp_path)
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(svc.settings, "tradingagents_memory_log_path", str(log_dir))
    result = TradingAgentsRunnerResult.model_validate(_payload())
    session_uuid = uuid.uuid4()
    session_obj = SimpleNamespace(id=123, session_uuid=session_uuid)
    proposal = SimpleNamespace(id=456)
    monkeypatch.setattr(
        svc,
        "run_tradingagents_research",
        AsyncMock(return_value=result),
    )
    monkeypatch.setattr(
        svc.trading_decision_service,
        "create_decision_session",
        AsyncMock(return_value=session_obj),
    )
    monkeypatch.setattr(
        svc.trading_decision_service,
        "add_decision_proposals",
        AsyncMock(return_value=[proposal]),
    )

    await svc.ingest_tradingagents_research(
        MagicMock(),
        user_id=1,
        symbol="NVDA",
        instrument_type=InstrumentType.equity_us,
    )

    files = list(log_dir.rglob("*.json"))
    assert len(files) == 1
    assert files[0].is_relative_to(log_dir)
    assert files[0].name == f"NVDA-{session_uuid}.json"
    logged = json.loads(files[0].read_text(encoding="utf-8"))
    assert logged["advisory_only"] is True
    assert logged["execution_allowed"] is False
