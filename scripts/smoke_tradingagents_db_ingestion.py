"""ROB-13 advisory-only TradingAgents production DB smoke harness.

This module imports ONLY tradingagents_research_service + DB session +
trading_decision models. It MUST NOT import broker / watch_alerts / order /
paper trading / kis trading / upbit trading / openclaw modules.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

_FORBIDDEN_PREFIXES = (
    "app.services.kis",
    "app.services.upbit",
    "app.services.brokers",
    "app.services.order_service",
    "app.services.watch_alerts",
    "app.services.paper_trading_service",
    "app.services.openclaw_client",
    "app.services.crypto_trade_cooldown_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.redis_token_manager",
    "app.services.kis_websocket",
    "app.tasks",
)

_FORBIDDEN_ARGV = (
    "--dry-run=False",
    "--place-order",
    "--register-watch",
    "--order-intent",
    "--no-advisory",
    "--execute",
)

_SECRET_KEY_RE = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|URL)$", re.I)
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._/-]{1,32}$")

logger = logging.getLogger("smoke_tradingagents")

Settings: Any | None = None
ingest_tradingagents_research: Any | None = None


def _redact_env_value(key: str, value: str) -> str:
    if _SECRET_KEY_RE.search(key):
        return "<redacted>"
    return value


def _refuse_forbidden_argv(argv: list[str]) -> None:
    for token in argv:
        for forbidden in _FORBIDDEN_ARGV:
            if forbidden in token:
                print(
                    f"smoke refused: forbidden argv token {forbidden!r} present",
                    file=sys.stderr,
                )
                raise SystemExit(64)


def _refuse_forbidden_modules() -> None:
    for name in list(sys.modules):
        for prefix in _FORBIDDEN_PREFIXES:
            if name == prefix or name.startswith(prefix + "."):
                print(
                    f"smoke refused: forbidden module loaded: {name}",
                    file=sys.stderr,
                )
                raise SystemExit(70)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ROB-13 advisory-only TradingAgents DB ingestion smoke",
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--as-of", required=True, dest="as_of")
    parser.add_argument(
        "--instrument-type",
        choices=("equity_kr", "equity_us", "crypto"),
        default="equity_kr",
    )
    parser.add_argument("--user-id", required=True, type=int)
    parser.add_argument("--analysts", default="market")
    keep_group = parser.add_mutually_exclusive_group()
    keep_group.add_argument(
        "--keep-on-success",
        dest="keep_on_success",
        action="store_true",
        default=True,
    )
    keep_group.add_argument(
        "--delete-on-success",
        dest="keep_on_success",
        action="store_false",
    )
    args = parser.parse_args(argv)
    if not _SYMBOL_RE.fullmatch(args.symbol):
        parser.error("--symbol contains unsupported characters")
    try:
        args.as_of = date.fromisoformat(args.as_of)
    except ValueError:
        parser.error("--as-of must be YYYY-MM-DD")
    args.analysts = [a.strip() for a in args.analysts.split(",") if a.strip()]
    if args.user_id <= 0:
        parser.error("--user-id must be positive")
    return args


def _load_settings() -> Any:
    global Settings
    if Settings is None:
        from app.core.config import Settings as SettingsClass

        Settings = SettingsClass
    return Settings()


def _require_settings(settings: Any) -> None:
    if settings.tradingagents_python is None:
        print(
            "TRADINGAGENTS_PYTHON is required for the production smoke",
            file=sys.stderr,
        )
        raise SystemExit(78)
    if settings.tradingagents_repo_path is None:
        print(
            "TRADINGAGENTS_REPO_PATH is required for the production smoke",
            file=sys.stderr,
        )
        raise SystemExit(78)

    python_path = Path(settings.tradingagents_python).expanduser()
    repo_path = Path(settings.tradingagents_repo_path).expanduser()
    runner_path = (
        Path(settings.tradingagents_runner_path).expanduser()
        if settings.tradingagents_runner_path is not None
        else repo_path / "scripts" / "run_auto_trader_research.py"
    )
    if not python_path.is_file():
        print("TRADINGAGENTS_PYTHON must resolve to an existing file", file=sys.stderr)
        raise SystemExit(78)
    if not repo_path.is_dir():
        print(
            "TRADINGAGENTS_REPO_PATH must resolve to an existing directory",
            file=sys.stderr,
        )
        raise SystemExit(78)
    if not runner_path.is_file():
        print(
            "TRADINGAGENTS_RUNNER_PATH must resolve to an existing file",
            file=sys.stderr,
        )
        raise SystemExit(78)


async def _count_proposals(db: Any, session_id: int) -> int:
    from sqlalchemy import func, select

    from app.models.trading_decision import TradingDecisionProposal

    result = await db.execute(
        select(func.count(TradingDecisionProposal.id)).where(
            TradingDecisionProposal.session_id == session_id
        )
    )
    return int(result.scalar_one())


async def _count_side_effects(db: Any, session_id: int) -> dict[str, int]:
    from sqlalchemy import text

    counts: dict[str, int] = {}
    for label, table_name in (
        ("actions", "trading_decision_actions"),
        ("counterfactuals", "trading_decision_counterfactuals"),
        ("outcomes", "trading_decision_outcomes"),
    ):
        result = await db.execute(
            text(
                f"""
                SELECT COUNT(*)
                FROM {table_name} child
                JOIN trading_decision_proposals p ON child.proposal_id = p.id
                WHERE p.session_id = :session_id
                """
            ),
            {"session_id": session_id},
        )
        counts[label] = int(result.scalar_one())
    return counts


async def _reload_session_and_proposal(db: Any, session_id: int) -> tuple[Any, Any]:
    from sqlalchemy import select

    from app.models.trading_decision import (
        TradingDecisionProposal,
        TradingDecisionSession,
    )

    session_result = await db.execute(
        select(TradingDecisionSession).where(TradingDecisionSession.id == session_id)
    )
    session_obj = session_result.scalar_one()
    proposal_result = await db.execute(
        select(TradingDecisionProposal).where(
            TradingDecisionProposal.session_id == session_id
        )
    )
    proposal = proposal_result.scalar_one()
    return session_obj, proposal


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _isoformat(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _validate_invariants(
    *,
    session_obj: Any,
    proposal: Any,
    proposal_count: int,
    side_effect_counts: dict[str, int],
    expected_market_scope: str | None,
) -> list[str]:
    from app.models.trading_decision import ProposalKind, UserResponse

    problems: list[str] = []
    session_brief = session_obj.market_brief or {}
    original_payload = proposal.original_payload or {}
    if session_obj.source_profile != "tradingagents":
        problems.append("source_profile != tradingagents")
    if (
        expected_market_scope is not None
        and session_obj.market_scope != expected_market_scope
    ):
        problems.append(f"market_scope != {expected_market_scope}")
    if session_brief.get("advisory_only") is not True:
        problems.append("session.market_brief.advisory_only is not True")
    if session_brief.get("execution_allowed") is not False:
        problems.append("session.market_brief.execution_allowed is not False")
    if proposal_count != 1:
        problems.append("proposal_count != 1")
    if proposal.proposal_kind != ProposalKind.other:
        problems.append("proposal_kind != other")
    if proposal.side != "none":
        problems.append("side != none")
    if original_payload.get("advisory_only") is not True:
        problems.append("proposal.original_payload.advisory_only is not True")
    if original_payload.get("execution_allowed") is not False:
        problems.append("proposal.original_payload.execution_allowed is not False")
    if proposal.user_response != UserResponse.pending:
        problems.append("user_response != pending")
    for field_name in (
        "user_quantity",
        "user_quantity_pct",
        "user_amount",
        "user_price",
        "user_trigger_price",
        "user_threshold_pct",
        "user_note",
        "responded_at",
    ):
        if getattr(proposal, field_name) is not None:
            problems.append(f"{field_name} is not None")
    for label, count in side_effect_counts.items():
        if count != 0:
            problems.append(f"{label} side_effect_count != 0")
    return problems


def _build_report(
    *,
    session_obj: Any,
    proposal: Any,
    side_effect_counts: dict[str, int],
) -> dict[str, Any]:
    session_brief = session_obj.market_brief or {}
    original_payload = proposal.original_payload or {}
    return {
        "ok": True,
        "proposal": {
            "id": proposal.id,
            "instrument_type": _enum_value(proposal.instrument_type),
            "original_payload_advisory_only": original_payload.get("advisory_only"),
            "original_payload_execution_allowed": original_payload.get(
                "execution_allowed"
            ),
            "proposal_kind": _enum_value(proposal.proposal_kind),
            "side": proposal.side,
            "symbol": proposal.symbol,
            "user_response": _enum_value(proposal.user_response),
        },
        "session": {
            "advisory_only": session_brief.get("advisory_only"),
            "execution_allowed": session_brief.get("execution_allowed"),
            "generated_at": _isoformat(session_obj.generated_at),
            "id": session_obj.id,
            "market_scope": session_obj.market_scope,
            "session_uuid": str(session_obj.session_uuid),
            "source_profile": session_obj.source_profile,
        },
        "side_effect_counts": side_effect_counts,
    }


async def _run(args: argparse.Namespace) -> int:
    from app.core.db import AsyncSessionLocal
    from app.models.trading import InstrumentType
    from app.services import tradingagents_research_service as svc

    _refuse_forbidden_modules()

    global ingest_tradingagents_research
    if ingest_tradingagents_research is None:
        ingest_tradingagents_research = svc.ingest_tradingagents_research

    settings = _load_settings()
    _require_settings(settings)

    instrument = InstrumentType(args.instrument_type)
    expected_market_scope = "kr" if instrument == InstrumentType.equity_kr else None
    async with AsyncSessionLocal() as db:
        try:
            session_obj, proposal = await ingest_tradingagents_research(
                db,
                user_id=args.user_id,
                symbol=args.symbol,
                instrument_type=instrument,
                as_of_date=args.as_of,
                analysts=args.analysts,
            )
            session_id = session_obj.id
            proposal_count = await _count_proposals(db, session_id)
            side_effect_counts = await _count_side_effects(db, session_id)
            problems = _validate_invariants(
                session_obj=session_obj,
                proposal=proposal,
                proposal_count=proposal_count,
                side_effect_counts=side_effect_counts,
                expected_market_scope=expected_market_scope,
            )
            if problems:
                await db.rollback()
                print(json.dumps({"ok": False, "problems": problems}, sort_keys=True))
                return 1
            if args.keep_on_success:
                await db.commit()
            else:
                await db.delete(session_obj)
                await db.commit()
        except SystemExit:
            raise
        except Exception:
            await db.rollback()
            logger.exception("ingest failed")
            return 1

    if not args.keep_on_success:
        print(
            json.dumps(
                {
                    "ok": True,
                    "proposal": None,
                    "session": {"id": session_id, "deleted": True},
                    "side_effect_counts": side_effect_counts,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    async with AsyncSessionLocal() as db:
        session_obj, proposal = await _reload_session_and_proposal(db, session_id)
        side_effect_counts = await _count_side_effects(db, session_id)
    print(
        json.dumps(
            _build_report(
                session_obj=session_obj,
                proposal=proposal,
                side_effect_counts=side_effect_counts,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    _refuse_forbidden_argv(raw)
    args = _parse_args(raw)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(asyncio.run(_run(args)))


_refuse_forbidden_modules()


if __name__ == "__main__":  # pragma: no cover
    main()
