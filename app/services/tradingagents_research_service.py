from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.trading import InstrumentType
from app.models.trading_decision import (
    ProposalKind,
    TradingDecisionProposal,
    TradingDecisionSession,
)
from app.schemas.tradingagents_research import TradingAgentsRunnerResult
from app.services import trading_decision_service

logger = logging.getLogger(__name__)

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._/-]{1,32}$")
_ANALYST_RE = re.compile(r"^[a-z_]{1,32}$")
_ENV_ALLOWLIST = {"PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH"}
_SENSITIVE_LINE_RE = re.compile(
    r"(key|token|secret|authorization)", flags=re.IGNORECASE
)


class TradingAgentsNotConfigured(RuntimeError):
    """TradingAgents runner settings or files are missing."""


class TradingAgentsRunnerError(RuntimeError):
    """TradingAgents subprocess failed or produced unusable output."""


class AdvisoryInvariantViolation(TradingAgentsRunnerError):
    """Runner JSON violated the advisory-only safety contract."""


def _validate_symbol(symbol: str) -> str:
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol contains unsupported characters")
    return symbol


def _analysts_or_default(analysts: Sequence[str] | None) -> list[str]:
    values = (
        list(analysts)
        if analysts is not None
        else [
            item.strip()
            for item in settings.tradingagents_default_analysts.split(",")
            if item.strip()
        ]
    )
    if not values:
        raise ValueError("at least one analyst is required")
    for analyst in values:
        if not _ANALYST_RE.fullmatch(analyst):
            raise ValueError("analyst contains unsupported characters")
    return values


def _resolve_existing_file(path: Path, label: str) -> str:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise TradingAgentsNotConfigured(f"{label} does not exist or is not a file")
    return str(resolved)


def _resolve_existing_dir(path: Path, label: str) -> str:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise TradingAgentsNotConfigured(
            f"{label} does not exist or is not a directory"
        )
    return str(resolved)


def _resolve_runner_paths() -> tuple[str, str, str]:
    if settings.tradingagents_repo_path is None:
        raise TradingAgentsNotConfigured("tradingagents_repo_path is not configured")

    repo = Path(settings.tradingagents_repo_path)
    repo_path = _resolve_existing_dir(repo, "tradingagents_repo_path")

    if settings.tradingagents_runner_path is None:
        runner = Path(repo_path) / "scripts" / "run_auto_trader_research.py"
    else:
        runner = Path(settings.tradingagents_runner_path)
    runner_path = _resolve_existing_file(runner, "tradingagents_runner_path")

    if settings.tradingagents_python is None:
        if importlib.util.find_spec("tradingagents") is None:
            raise TradingAgentsNotConfigured("tradingagents_python is not configured")
        python_path = sys.executable
    else:
        python_path = _resolve_existing_file(
            Path(settings.tradingagents_python), "tradingagents_python"
        )

    return repo_path, python_path, runner_path


def _filtered_child_env() -> dict[str, str]:
    child_env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _ENV_ALLOWLIST or key.startswith("TRADINGAGENTS_"):
            child_env[key] = value
        elif key == "OPENAI_API_KEY":
            child_env[key] = value
    child_env.setdefault("OPENAI_API_KEY", "no-key-required")
    return child_env


def _redact_stderr(stderr: bytes) -> str:
    text = stderr.decode("utf-8", errors="replace")[:4096]
    lines = [line for line in text.splitlines() if not _SENSITIVE_LINE_RE.search(line)]
    return "\n".join(lines)


def _build_argv(
    *,
    python_path: str,
    runner_path: str,
    symbol: str,
    as_of_date: date,
    analysts: Sequence[str],
) -> list[str]:
    argv = [
        python_path,
        runner_path,
        "--symbol",
        symbol,
        "--date",
        as_of_date.isoformat(),
        "--analysts",
        ",".join(analysts),
        "--base-url",
        settings.tradingagents_base_url,
        "--model",
        settings.tradingagents_model,
        "--max-debate-rounds",
        str(settings.tradingagents_max_debate_rounds),
        "--max-risk-discuss-rounds",
        str(settings.tradingagents_max_risk_discuss_rounds),
        "--max-recur-limit",
        str(settings.tradingagents_max_recur_limit),
        "--output-language",
        settings.tradingagents_output_language,
    ]
    if settings.tradingagents_checkpoint_enabled:
        argv.append("--checkpoint-enabled")
    return argv


def _validate_runner_payload(payload: object) -> TradingAgentsRunnerResult:
    try:
        return TradingAgentsRunnerResult.model_validate(payload)
    except ValidationError as exc:
        details = exc.errors(include_url=False)
        invariant_fields = {"status", "advisory_only", "execution_allowed"}
        failed_fields = {
            str(error.get("loc", ("",))[0]) for error in details if error.get("loc")
        }
        if failed_fields & invariant_fields:
            raise AdvisoryInvariantViolation(
                f"runner output violated advisory contract: {details}"
            ) from exc
        raise TradingAgentsRunnerError(
            f"runner output failed advisory contract: {details}"
        ) from exc


def _write_memory_log(
    result: TradingAgentsRunnerResult, *, session_uuid: object
) -> None:
    if settings.tradingagents_memory_log_path is None:
        return

    base = Path(settings.tradingagents_memory_log_path).expanduser().resolve()
    target_dir = (base / result.as_of_date.isoformat()).resolve()
    target_path = (target_dir / f"{result.symbol}-{session_uuid}.json").resolve()
    if not target_path.is_relative_to(base):
        logger.warning("TradingAgents memory log path escaped configured base")
        return

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    except OSError:
        logger.warning("Failed to write TradingAgents memory log", exc_info=True)


async def run_tradingagents_research(
    *,
    symbol: str,
    instrument_type: InstrumentType,
    as_of_date: date | None = None,
    analysts: Sequence[str] | None = None,
) -> TradingAgentsRunnerResult:
    """Invoke the advisory-only TradingAgents runner and validate its JSON output."""
    _ = instrument_type
    safe_symbol = _validate_symbol(symbol)
    safe_analysts = _analysts_or_default(analysts)
    run_date = as_of_date or date.today()
    repo_path, python_path, runner_path = _resolve_runner_paths()
    argv = _build_argv(
        python_path=python_path,
        runner_path=runner_path,
        symbol=safe_symbol,
        as_of_date=run_date,
        analysts=safe_analysts,
    )

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=repo_path,
        env=_filtered_child_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=settings.tradingagents_subprocess_timeout_sec,
        )
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise TradingAgentsRunnerError("tradingagents runner timed out") from exc

    if stderr:
        logger.debug("TradingAgents runner stderr: %s", _redact_stderr(stderr))

    if proc.returncode != 0:
        raise TradingAgentsRunnerError(
            f"tradingagents runner exited with {proc.returncode}"
        )

    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TradingAgentsRunnerError("runner produced non-JSON stdout") from exc

    return _validate_runner_payload(payload)


def _market_scope_for(instrument_type: InstrumentType) -> str:
    if instrument_type == InstrumentType.equity_kr:
        return "kr"
    if instrument_type == InstrumentType.equity_us:
        return "us"
    if instrument_type == InstrumentType.crypto:
        return "crypto"
    return instrument_type.value


async def ingest_tradingagents_research(
    db: AsyncSession,
    *,
    user_id: int,
    symbol: str,
    instrument_type: InstrumentType,
    as_of_date: date | None = None,
    analysts: Sequence[str] | None = None,
) -> tuple[TradingDecisionSession, TradingDecisionProposal]:
    """Run TradingAgents advisory research and persist one session plus proposal."""
    result = await run_tradingagents_research(
        symbol=symbol,
        instrument_type=instrument_type,
        as_of_date=as_of_date,
        analysts=analysts,
    )

    session_obj = await trading_decision_service.create_decision_session(
        db,
        user_id=user_id,
        source_profile="tradingagents",
        strategy_name=f"tradingagents:{result.llm.model}:{','.join(result.analysts)}",
        market_scope=_market_scope_for(instrument_type),
        market_brief={
            "advisory_only": True,
            "execution_allowed": False,
            "llm": result.llm.model_dump(),
            "config": result.config.model_dump(),
            "warnings": result.warnings.model_dump(),
            "raw_state_keys": result.raw_state_keys,
        },
        generated_at=datetime.combine(result.as_of_date, time.min, tzinfo=UTC),
        notes=(
            "TradingAgents advisory research; advisory-only. "
            "No execution, watch alert, or paper trade is authorized by this row."
        ),
    )
    _write_memory_log(result, session_uuid=session_obj.session_uuid)

    proposals = await trading_decision_service.add_decision_proposals(
        db,
        session_id=session_obj.id,
        proposals=[
            {
                "symbol": result.symbol,
                "instrument_type": instrument_type,
                "proposal_kind": ProposalKind.other,
                "side": "none",
                "original_payload": {
                    "advisory_only": True,
                    "execution_allowed": False,
                    "decision": result.decision,
                    "final_trade_decision": result.final_trade_decision,
                    "warnings": result.warnings.model_dump(),
                    "llm": result.llm.model_dump(),
                    "config": result.config.model_dump(),
                    "as_of_date": result.as_of_date.isoformat(),
                },
                "original_rationale": result.decision[:4000],
            }
        ],
    )
    return session_obj, proposals[0]
