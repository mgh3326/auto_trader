"""Phase 3 — LLM decision-injection MCP tool for Binance Demo scalping.

The out-of-process LLM (an MCP session) reads market data + recent scalping
reviews, decides, and calls ``binance_demo_scalping_submit_decision`` with its
decision + rationale. The tool is deterministic: it executes the LLM's decision
via the existing ``DemoScalpingExecutor.execute_monitored`` (one round-trip),
tagging the trade ``session_tag="llm"`` and recording the rationale in
``signal_snapshot``. NO LLM call here — judgment belongs to the MCP caller
(runtime in-process LLM boundary). Demo-only; dry_run default + confirm gate.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any, Literal

from app.services.brokers.binance.demo_scalping.contract import ScalpingRiskLimits
from app.services.brokers.binance.demo_scalping.order_intent import OrderIntent

logger = logging.getLogger(__name__)

_ALLOWLIST = frozenset({"XRPUSDT", "DOGEUSDT", "SOLUSDT"})


def _build_intent(
    *, symbol: str, side: str, notional_usdt: Decimal, now: dt.datetime
) -> OrderIntent:
    now_ms = int(now.timestamp() * 1000)
    return OrderIntent(
        product="usdm_futures",
        symbol=symbol,
        side=side,
        order_type="MARKET",
        target_notional_usdt=notional_usdt,
        entry_reference_price=None,
        tp_price=None,
        sl_price=None,
        confidence=Decimal("0"),
        reason_codes=("llm_decision",),
        source_candle_close_time_ms=now_ms,
        evaluated_at_ms=now_ms,
    )


async def _execute_confirmed_round_trip(
    *,
    symbol: str,
    side: str,
    tp_bps: Decimal,
    sl_bps: Decimal,
    notional_usdt: Decimal,
    session_tag: str,
    signal_snapshot: dict[str, Any],
    now: dt.datetime,
) -> Any:
    """Construct the demo executor and run one monitored round-trip. Real Demo
    order — only reached on confirm=True. Mirrors the confirmed execution path in
    scripts/binance_demo_scalping_execute.py."""
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.binance.demo_scalping.market_data import (
        DemoScalpingMarketData,
    )
    from app.services.brokers.binance.demo_scalping_exec.executor import (
        DemoScalpingExecutor,
    )
    from app.services.brokers.binance.demo_scalping_exec.reference import (
        DemoReferenceData,
    )
    from app.services.brokers.binance.futures_demo.execution_client import (
        BinanceFuturesDemoExecutionClient,
    )

    limits = ScalpingRiskLimits()
    client = BinanceFuturesDemoExecutionClient.from_env()
    reference = DemoReferenceData()
    market_data = DemoScalpingMarketData()
    try:
        async with AsyncSessionLocal() as session:
            executor = DemoScalpingExecutor(
                product="usdm_futures",
                client=client,
                session=session,
                reference=reference,
                now=now,
                limits=limits,
                market_data=market_data,
            )
            intent = _build_intent(
                symbol=symbol, side=side, notional_usdt=notional_usdt, now=now
            )
            result = await executor.execute_monitored(
                intent,
                confirm=True,
                tp_bps=tp_bps,
                sl_bps=sl_bps,
                session_tag=session_tag,
                signal_snapshot=signal_snapshot,
            )
            await session.commit()
            return result
    finally:
        await reference.aclose()
        await market_data.aclose()
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()


async def binance_demo_scalping_submit_decision(
    symbol: str,
    side: Literal["BUY", "SELL"],
    rationale: str,
    tp_bps: float = 30.0,
    sl_bps: float = 20.0,
    notional_usdt: float = 10.0,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Submit an LLM-decided Binance Demo (USD-M futures) scalping round-trip.

    DEMO ONLY. ``dry_run`` (default) returns the plan with no order. A real Demo
    order requires ``dry_run=False`` AND ``confirm=True``. The trade is tagged
    ``session_tag="llm"`` and the rationale is recorded in ``signal_snapshot`` so
    the daily review can compare LLM vs the rule-based baseline. Symbols limited
    to XRPUSDT/DOGEUSDT/SOLUSDT; 1x; notional capped at 10 USDT by the executor."""
    sym = symbol.upper().strip()
    side = side.upper().strip()
    if sym not in _ALLOWLIST:
        return {
            "status": "rejected",
            "error": f"symbol {sym!r} not allowlisted (allowed: {sorted(_ALLOWLIST)})",
        }
    if side not in ("BUY", "SELL"):
        return {"status": "rejected", "error": f"side must be BUY|SELL, got {side!r}"}
    if not rationale or not rationale.strip():
        return {"status": "rejected", "error": "rationale must be a non-empty string"}

    notional = Decimal(str(notional_usdt))
    signal_snapshot = {
        "source": "llm",
        "rationale": rationale.strip(),
        "requested_side": side,
        "tp_bps": str(tp_bps),
        "sl_bps": str(sl_bps),
    }

    if dry_run or not confirm:
        return {
            "status": "planned",
            "dry_run": True,
            "symbol": sym,
            "side": side,
            "rationale": rationale.strip(),
            "session_tag": "llm",
            "notional_usdt": str(notional),
            "tp_bps": str(tp_bps),
            "sl_bps": str(sl_bps),
            "note": "set dry_run=false AND confirm=true to place the real Demo order",
        }

    now = dt.datetime.now(dt.UTC)
    try:
        result = await _execute_confirmed_round_trip(
            symbol=sym,
            side=side,
            tp_bps=Decimal(str(tp_bps)),
            sl_bps=Decimal(str(sl_bps)),
            notional_usdt=notional,
            session_tag="llm",
            signal_snapshot=signal_snapshot,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 — surface broker/setup errors as data
        logger.exception("binance demo scalping submit_decision failed")
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    evidence = result.to_evidence_dict()
    return {
        "status": result.status,
        "dry_run": False,
        "symbol": sym,
        "side": side,
        "rationale": rationale.strip(),
        "session_tag": "llm",
        "open_client_order_id": result.open_client_order_id,
        "close_client_order_id": result.close_client_order_id,
        "exit_reason": result.exit_reason,
        "evidence": evidence,
    }


def register_binance_demo_scalping_tools(mcp: Any) -> None:
    mcp.tool(
        name="binance_demo_scalping_submit_decision",
        description=(
            "Submit an LLM-decided Binance Demo (USD-M futures) scalping "
            "round-trip. DEMO ONLY, dry_run default; real order needs "
            "dry_run=false + confirm=true. Tags the trade session_tag='llm' and "
            "records the rationale for LLM-vs-baseline comparison. Symbols: "
            "XRPUSDT/DOGEUSDT/SOLUSDT; 1x; <=10 USDT."
        ),
    )(binance_demo_scalping_submit_decision)
