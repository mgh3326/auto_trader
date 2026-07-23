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
from app.services.brokers.binance.demo_scalping.market_data import (
    MarketConditionsUnavailable,
)
from app.services.brokers.binance.demo_scalping.order_intent import OrderIntent
from app.services.brokers.binance.demo_scalping_exec.validated_signal_gate import (
    evaluate_validated_signal_gate,
)

logger = logging.getLogger(__name__)

_ALLOWLIST = frozenset({"XRPUSDT", "DOGEUSDT", "SOLUSDT"})
_PRODUCT = "usdm_futures"


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


async def _build_conditions_and_run(
    *,
    symbol: str,
    side: str,
    tp_bps: Decimal,
    sl_bps: Decimal,
    notional_usdt: Decimal,
    session_tag: str,
    signal_snapshot: dict[str, Any],
    now: dt.datetime,
    confirm: bool,
) -> tuple[Any, Any]:
    """Collect a *server-derived* MarketConditions snapshot from the Demo host,
    then run the executor preflight (``confirm=False`` → dry-run judgment) or the
    full monitored round-trip (``confirm=True`` → real Demo order).

    The snapshot is built BEFORE any session is opened or signed client is
    constructed, so an unavailable snapshot (raised as
    :class:`MarketConditionsUnavailable`) touches neither broker nor ledger
    (ROB-841 AC1). ``confirm=False`` reads the ledger for the risk preflight but
    inserts nothing and places no order (AC6). Returns ``(market, result)``."""
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.binance.demo_scalping.market_data import (
        DemoScalpingMarketData,
        build_market_conditions,
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
    reference = DemoReferenceData()
    market_data = DemoScalpingMarketData()
    client: Any = None
    try:
        # Server-observed spread + data-age BEFORE any ledger/broker touch.
        # The builder samples the clock after both observations complete, so
        # fetch latency is counted toward staleness (no now_ms passed in).
        market = await build_market_conditions(
            market_data, product=_PRODUCT, symbol=symbol
        )
        # The signed client is constructed ONLY for a real order, and only
        # after conditions are proven available.
        if confirm:
            client = BinanceFuturesDemoExecutionClient.from_env()
        async with AsyncSessionLocal() as session:
            executor = DemoScalpingExecutor(
                product=_PRODUCT,
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
                confirm=confirm,
                market=market,
                tp_bps=tp_bps,
                sl_bps=sl_bps,
                session_tag=session_tag,
                signal_snapshot=signal_snapshot,
            )
            if confirm:
                await session.commit()
            return market, result
    finally:
        await reference.aclose()
        await market_data.aclose()
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()


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
    """Real Demo order — only reached on confirm=True. Server-derived market
    conditions are collected and handed to the executor; an unavailable snapshot
    raises before any broker/ledger interaction."""
    _market, result = await _build_conditions_and_run(
        symbol=symbol,
        side=side,
        tp_bps=tp_bps,
        sl_bps=sl_bps,
        notional_usdt=notional_usdt,
        session_tag=session_tag,
        signal_snapshot=signal_snapshot,
        now=now,
        confirm=True,
    )
    return result


async def _dry_run_preflight(
    *,
    symbol: str,
    side: str,
    tp_bps: Decimal,
    sl_bps: Decimal,
    notional_usdt: Decimal,
    signal_snapshot: dict[str, Any],
    now: dt.datetime,
) -> tuple[Any, Any]:
    """Same server-derived market/risk preflight as a real order, but with no
    broker mutation and no ledger insert (ROB-841 AC6). Returns
    ``(market, result)`` where ``result`` is a ``dry_run`` or ``blocked``
    ExecutionResult."""
    return await _build_conditions_and_run(
        symbol=symbol,
        side=side,
        tp_bps=tp_bps,
        sl_bps=sl_bps,
        notional_usdt=notional_usdt,
        session_tag="llm",
        signal_snapshot=signal_snapshot,
        now=now,
        confirm=False,
    )


def _unavailable_response(
    sym: str, side: str, exc: MarketConditionsUnavailable, *, dry_run: bool
) -> dict[str, Any]:
    """Fail-close response: no trustworthy server market snapshot, no order."""
    return {
        "status": "market_conditions_unavailable",
        "dry_run": dry_run,
        "symbol": sym,
        "side": side,
        "reason": exc.reason,
        "note": (
            "server could not derive a trustworthy market snapshot "
            "(spread/data-age); no order placed and no ledger touched"
        ),
    }


def _dry_run_response(
    sym: str,
    side: str,
    rationale: str,
    result: Any,
    market: Any,
    tp_bps: float,
    sl_bps: float,
) -> dict[str, Any]:
    """Map a dry-run ExecutionResult (dry_run/blocked) to the tool response,
    echoing the *server-observed* market snapshot for auditability."""
    base: dict[str, Any] = {
        "dry_run": True,
        "symbol": sym,
        "side": side,
        "rationale": rationale,
        "session_tag": "llm",
        "tp_bps": str(tp_bps),
        "sl_bps": str(sl_bps),
        "market_conditions": {
            "spread_bps": str(market.spread_bps),
            "data_age_seconds": market.data_age_seconds,
        },
    }
    if result.status == "blocked":
        return {
            **base,
            "status": "blocked",
            "reason_codes": list(result.reason_codes),
            "note": "server-derived market/risk preflight blocked this order",
        }
    sized_notional = getattr(result, "sized_notional_usdt", None)
    sized_qty = getattr(result, "sized_qty", None)
    return {
        **base,
        "status": "planned",
        "notional_usdt": None if sized_notional is None else str(sized_notional),
        "sized_qty": None if sized_qty is None else str(sized_qty),
        "note": "set dry_run=false AND confirm=true to place the real Demo order",
    }


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
    now = dt.datetime.now(dt.UTC)

    # ROB-937: this interactive LLM path is INTENTIONALLY exempt from the
    # ROB-905 validated-signal gate (which arms only recurring scheduler ticks).
    # Its authorization comes from registry opt-in + the human per-call
    # ``confirm=true`` + ROB-841 market-conditions fail-close + the allowlist/1x/
    # notional caps — NOT the gate artifact. We evaluate the gate once here for
    # AUDIT ONLY and echo the verdict in every response; execution is UNCHANGED
    # whether the gate allows or denies. Do not add a block/downgrade here — that
    # would reverse the ROB-905 scoping and break the human-in-the-loop design.
    gate = evaluate_validated_signal_gate(now=now)
    validated_signal_gate = {"allowed": gate.allowed, "reason": gate.reason}

    if dry_run or not confirm:
        # ROB-841 AC6: run the SAME server-derived market/risk preflight as a
        # real order, but place no order and insert no ledger row.
        try:
            market, result = await _dry_run_preflight(
                symbol=sym,
                side=side,
                tp_bps=Decimal(str(tp_bps)),
                sl_bps=Decimal(str(sl_bps)),
                notional_usdt=notional,
                signal_snapshot=signal_snapshot,
                now=now,
            )
        except MarketConditionsUnavailable as exc:
            return {
                **_unavailable_response(sym, side, exc, dry_run=True),
                "validated_signal_gate": validated_signal_gate,
                "authorization_mode": "dry_run",
            }
        except Exception as exc:  # noqa: BLE001 — surface setup errors as data
            logger.exception("binance demo scalping dry-run preflight failed")
            return {
                "status": "error",
                "dry_run": True,
                "error": f"{type(exc).__name__}: {exc}",
                "validated_signal_gate": validated_signal_gate,
                "authorization_mode": "dry_run",
            }
        return {
            **_dry_run_response(
                sym, side, rationale.strip(), result, market, tp_bps, sl_bps
            ),
            "validated_signal_gate": validated_signal_gate,
            "authorization_mode": "dry_run",
        }

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
    except MarketConditionsUnavailable as exc:
        return {
            **_unavailable_response(sym, side, exc, dry_run=False),
            "validated_signal_gate": validated_signal_gate,
            "authorization_mode": "operator_interactive_exception",
        }
    except Exception as exc:  # noqa: BLE001 — surface broker/setup errors as data
        logger.exception("binance demo scalping submit_decision failed")
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "validated_signal_gate": validated_signal_gate,
            "authorization_mode": "operator_interactive_exception",
        }

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
        # ROB-937: audit-only gate verdict + explicit authorization marker. The
        # round-trip above ALREADY ran regardless of gate.allowed — this path's
        # authorization is the human confirm=true, not the gate artifact.
        "validated_signal_gate": validated_signal_gate,
        "authorization_mode": "operator_interactive_exception",
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
