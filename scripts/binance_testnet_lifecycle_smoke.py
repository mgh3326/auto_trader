"""ROB-294 — Binance testnet scalper lifecycle smoke CLI (operator-gated).

This is the lifecycle-validation companion to
``scripts/binance_testnet_scalper_smoke.py``. The earlier ROB-286/ROB-293
smoke proved connectivity + signed-read + confirm-gate plumbing, but
because its market-snapshot stub returns ``rsi_5m=50`` for every symbol
the decision function always resolves to ``Hold`` — no ``submitted``,
``filled``, or ``tp_sl_armed`` ledger rows are ever produced. This CLI
fills the gap by accepting deterministic snapshot inputs so an operator
can drive a single tick through the full lifecycle on the testnet.

The CLI is **not** scheduled, **not** wired to TaskIQ/cron/Prefect, and
default-disabled. Activation requires:

  1. ``BINANCE_TESTNET_ENABLED=true`` in the environment.
  2. ``BINANCE_TESTNET_API_KEY`` + ``BINANCE_TESTNET_API_SECRET`` set.
  3. ``--symbol`` chosen from the locked MVP set
     (``BTCUSDT``/``ETHUSDT``/``SOLUSDT``).
  4. Explicit ``--confirm`` (and ``--no-dry-run``) for any HTTP
     submission.

Three operator stages — pass exactly the flags for the stage you intend:

  Stage 1 — default-disabled (no env, no flags)::

      uv run python -m scripts.binance_testnet_lifecycle_smoke
      # → exit 0, "scalper disabled" log line, zero side effects.

  Stage 2 — credentialed dry-run lifecycle (env set, ``--dry-run``)::

      BINANCE_TESTNET_ENABLED=true \\
        BINANCE_TESTNET_API_KEY=... BINANCE_TESTNET_API_SECRET=... \\
        uv run python -m scripts.binance_testnet_lifecycle_smoke \\
        --symbol BTCUSDT --simulate-rsi 25 \\
        --simulate-price 50000 \\
        --simulate-ema20 50100 --simulate-ema50 50000 \\
        --dry-run
      # → reconcile signed-read against testnet runs;
      # → tick resolves to Entry (BUY);
      # → ledger walks planned → previewed → validated, then STOPS
      #   (dry_run=True ⇒ no broker submission, no submitted rows).

  Stage 3 — operator-confirmed single-cycle (env set, ``--confirm``)::

      BINANCE_TESTNET_ENABLED=true \\
        BINANCE_TESTNET_API_KEY=... BINANCE_TESTNET_API_SECRET=... \\
        uv run python -m scripts.binance_testnet_lifecycle_smoke \\
        --symbol BTCUSDT --simulate-rsi 25 \\
        --simulate-price 50000 \\
        --simulate-ema20 50100 --simulate-ema50 50000 \\
        --no-dry-run --confirm
      # → real signed POST to testnet.binance.vision /api/v3/order;
      # → if broker fills immediately, paired TP/SL are placed
      #   (sequential, never gather);
      # → evidence summary printed at end.

Evidence summary (always printed at exit; ``--evidence-json`` also writes
a structured file for the PR handoff):

  * env vars present? (presence only — values are NEVER printed)
  * ledger row count before / after the tick
  * client_order_id(s) created this run
  * broker open-order count after the tick
  * final lifecycle states per row
  * anomaly summary
  * mode: dry-run | confirmed-single-cycle

Exit codes mirror the ROB-286 smoke CLI:
  * 0 — clean (or default-disabled)
  * 1 — operator misconfiguration (missing env, invalid symbol, etc.)
  * 2 — runtime failure

Hard non-goals (do not relax without a separate review):
  * No scheduler / TaskIQ / cron / Prefect / Hermes activation.
  * No live Binance hosts (``BinanceLiveHostBlocked`` enforces).
  * No futures path.
  * No secret value printed/persisted.
  * No notional cap weakening — ``--max-notional`` defaults to the
    config value and is bounded to a small testnet ceiling.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

logger = logging.getLogger("scripts.binance_testnet_lifecycle_smoke")

# Locked MVP set — kept in sync with ``ScalperConfig.symbols`` so the CLI
# refuses any symbol outside the lifecycle-validated trio. Expanding the
# set is deliberately a code change in two places (config + here) to
# preserve reviewer friction.
MVP_SYMBOLS: frozenset[str] = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT"})

# Testnet ceiling for the CLI: the operator can lower ``--max-notional``
# below this, but never above it. Keeps a typo-introduced large notional
# from escaping. Mirrors ``ScalperConfig.max_notional_usdt`` default x2 to
# leave operator headroom for symbols where ``MIN_NOTIONAL`` filter
# requires bumping past the default.
MAX_NOTIONAL_CEILING_USDT: Decimal = Decimal("25")


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class LifecycleEvidence:
    """Structured evidence record emitted at the end of a lifecycle run.

    All fields are operator-facing and safe to log. Credential values
    are NEVER stored here — only presence flags.
    """

    mode: str  # "default-disabled" | "dry-run" | "confirmed-single-cycle"
    symbol: str | None
    started_at: str
    completed_at: str | None = None
    env_binance_enabled_present: bool = False
    env_api_key_present: bool = False
    env_api_secret_present: bool = False
    env_base_url_present: bool = False
    snapshot: dict[str, str] = field(default_factory=dict)
    reconcile_rows_examined: int = 0
    reconcile_anomalies_detected: int = 0
    ledger_rows_before: int = 0
    ledger_rows_after: int = 0
    client_order_ids_created: list[str] = field(default_factory=list)
    final_lifecycle_states: dict[str, str] = field(default_factory=dict)
    broker_open_orders_after: int = 0
    anomaly_client_order_ids: list[str] = field(default_factory=list)
    tick_action: str | None = None
    tick_submitted: bool = False
    tick_dry_run: bool = True
    tick_notes: str = ""
    cli_command: list[str] = field(default_factory=list)
    exit_code: int = 0
    notes: list[str] = field(default_factory=list)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="binance_testnet_lifecycle_smoke",
        description=(
            "ROB-294 lifecycle-validation smoke for the Binance testnet "
            "scalper. Default behavior is disabled (zero side effects). "
            "Set BINANCE_TESTNET_ENABLED=true + credentials and pass "
            "--symbol to opt in. Only operator-confirmed flags reach the "
            "testnet."
        ),
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help=(
            "Single MVP symbol to drive through the lifecycle. Required "
            "when opted in. Must be one of "
            f"{sorted(MVP_SYMBOLS)}."
        ),
    )
    parser.add_argument(
        "--simulate-price",
        type=str,
        default=None,
        help="Snapshot ``last_price`` for the deterministic tick (Decimal).",
    )
    parser.add_argument(
        "--simulate-rsi",
        type=float,
        default=50.0,
        help=(
            "Snapshot ``rsi_5m`` for the deterministic tick. Default 50.0 "
            "(neutral; resolves to Hold)."
        ),
    )
    parser.add_argument(
        "--simulate-ema20",
        type=str,
        default=None,
        help="Snapshot ``ema_20_5m`` for the deterministic tick (Decimal).",
    )
    parser.add_argument(
        "--simulate-ema50",
        type=str,
        default=None,
        help="Snapshot ``ema_50_5m`` for the deterministic tick (Decimal).",
    )
    parser.add_argument(
        "--simulate-instrument-health",
        type=str,
        default="healthy",
        choices=("healthy", "degraded", "rate_limited", "manual_backfill_required"),
        help="Snapshot ``instrument_health``. Default ``healthy``.",
    )
    parser.add_argument(
        "--max-notional",
        type=str,
        default=None,
        help=(
            "Override the runner's ``max_notional_usdt`` for this run. "
            f"Bounded to {MAX_NOTIONAL_CEILING_USDT} USDT. Useful when "
            "Binance ``MIN_NOTIONAL`` filter rejects the default 10 USDT "
            "for a high-priced symbol; raise carefully and document the "
            "reason in the PR/handoff."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Dry-run mode (default). No HTTP submission.",
    )
    parser.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Disable dry-run; use with --confirm.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help=(
            "Required for any broker submission. Implies --no-dry-run; "
            "without it the runner stays in preview-only mode."
        ),
    )
    parser.add_argument(
        "--cancel-pending-on-exit",
        action="store_true",
        default=False,
        help=(
            "If a row created during this run is still in ``submitted`` "
            "state at exit (entry submitted but not filled), issue a "
            "broker cancel. Requires --confirm to actually hit testnet; "
            "without it the cancel is itself a dry-run preview."
        ),
    )
    parser.add_argument(
        "--evidence-json",
        type=str,
        default=None,
        help=(
            "Write the evidence summary as JSON to this path. The same "
            "summary is always also printed to stdout. The file should "
            "be reviewed before pasting into Linear; it contains no "
            "secret values by construction."
        ),
    )
    return parser.parse_args(argv)


def _collect_env_presence(evidence: LifecycleEvidence) -> None:
    """Record which env vars are present WITHOUT logging their values."""
    evidence.env_binance_enabled_present = bool(
        os.environ.get("BINANCE_TESTNET_ENABLED")
    )
    evidence.env_api_key_present = bool(os.environ.get("BINANCE_TESTNET_API_KEY"))
    evidence.env_api_secret_present = bool(os.environ.get("BINANCE_TESTNET_API_SECRET"))
    evidence.env_base_url_present = bool(os.environ.get("BINANCE_TESTNET_BASE_URL"))


def _print_evidence(evidence: LifecycleEvidence) -> None:
    """Log the evidence summary as a single structured block."""
    payload = asdict(evidence)
    logger.info("lifecycle smoke evidence: %s", json.dumps(payload, default=str))


def _maybe_write_evidence_json(
    evidence: LifecycleEvidence, *, path: str | None
) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(evidence), fh, indent=2, default=str)
        logger.info("lifecycle smoke evidence written to %s", path)
    except OSError as exc:
        logger.warning("lifecycle smoke: could not write evidence to %s: %s", path, exc)


async def _build_snapshot_factory(
    *,
    symbol: str,
    price: Decimal,
    rsi: float,
    ema20: Decimal,
    ema50: Decimal,
    instrument_health: str,
) -> Callable[[str], Awaitable[Any]]:
    """Return a deterministic ``market_snapshot_for_symbol`` for one tick.

    The factory raises if the runner asks for any symbol other than the
    one the operator chose. That keeps the CLI single-symbol — multi-
    symbol drift would invalidate the deterministic guarantee.
    """
    from app.services.scalping.decision import MarketSnapshot

    async def _snapshot(req_symbol: str) -> MarketSnapshot:
        if req_symbol != symbol:
            raise AssertionError(
                f"lifecycle smoke driver received unexpected symbol "
                f"{req_symbol!r}; expected {symbol!r}. The CLI is "
                "single-symbol by design."
            )
        return MarketSnapshot(
            symbol=symbol,
            last_price=price,
            rsi_5m=rsi,
            ema_20_5m=ema20,
            ema_50_5m=ema50,
            instrument_health=instrument_health,
        )

    return _snapshot


async def _count_ledger_rows(session, instrument_id: int) -> int:
    from sqlalchemy import func, select

    from app.models.binance_testnet_order_ledger import BinanceTestnetOrderLedger

    result = await session.execute(
        select(func.count())
        .select_from(BinanceTestnetOrderLedger)
        .where(BinanceTestnetOrderLedger.instrument_id == instrument_id)
    )
    return int(result.scalar_one())


async def _collect_run_ledger_states(
    session, *, instrument_id: int, since: datetime
) -> dict[str, str]:
    """Return ``{client_order_id: lifecycle_state}`` for rows created during this run."""
    from sqlalchemy import select

    from app.models.binance_testnet_order_ledger import BinanceTestnetOrderLedger

    result = await session.execute(
        select(
            BinanceTestnetOrderLedger.client_order_id,
            BinanceTestnetOrderLedger.lifecycle_state,
        )
        .where(BinanceTestnetOrderLedger.instrument_id == instrument_id)
        .where(BinanceTestnetOrderLedger.created_at >= since)
    )
    return {str(cid): str(state) for cid, state in result.all()}


async def _instrument_id_resolver(session) -> Callable[[str], Awaitable[int]]:
    from sqlalchemy import select

    from app.models.crypto_instruments import CryptoInstrument

    async def _resolve(symbol: str) -> int:
        result = await session.execute(
            select(CryptoInstrument.id).where(
                CryptoInstrument.venue == "binance",
                CryptoInstrument.product == "spot",
                CryptoInstrument.venue_symbol == symbol,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise LookupError(
                f"crypto_instruments row not found for binance/spot/{symbol}. "
                "Run scripts/binance_testnet_seed_instruments.py first."
            )
        return int(row)

    return _resolve


def _resolve_decimal(value: str | None, *, default: Decimal) -> Decimal:
    if value is None or value == "":
        return default
    return Decimal(value)


async def _run_lifecycle(
    *,
    args: argparse.Namespace,
    evidence: LifecycleEvidence,
) -> int:
    """Run the deterministic single-cycle lifecycle. Returns exit code."""
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.binance.testnet.dto import DryRunResult
    from app.services.brokers.binance.testnet.execution_client import (
        BinanceTestnetExecutionClient,
    )
    from app.services.brokers.binance.testnet.ledger.service import (
        BinanceTestnetLedgerService,
    )
    from app.services.scalping.config import ScalperConfig
    from app.services.scalping.runner import ScalperRunner

    if args.symbol not in MVP_SYMBOLS:
        logger.error(
            "lifecycle smoke: --symbol must be one of %s (got %r)",
            sorted(MVP_SYMBOLS),
            args.symbol,
        )
        return 1

    # Build the runner config. Honor --max-notional within the ceiling.
    base_config = ScalperConfig.default_for_testnet()
    max_notional = base_config.max_notional_usdt
    if args.max_notional is not None:
        override = Decimal(args.max_notional)
        if override > MAX_NOTIONAL_CEILING_USDT:
            logger.error(
                "lifecycle smoke: --max-notional %s exceeds testnet ceiling %s",
                override,
                MAX_NOTIONAL_CEILING_USDT,
            )
            return 1
        if override <= 0:
            logger.error("lifecycle smoke: --max-notional must be positive")
            return 1
        max_notional = override
    # ScalperConfig is frozen; spawn a new instance with the overridden notional.
    config = ScalperConfig(
        symbols=base_config.symbols,
        max_notional_usdt=max_notional,
        rsi_oversold=base_config.rsi_oversold,
        rsi_overbought=base_config.rsi_overbought,
        tp_pct=base_config.tp_pct,
        sl_pct=base_config.sl_pct,
        reconcile_open_orders_limit=base_config.reconcile_open_orders_limit,
        reconcile_recent_fills_limit=base_config.reconcile_recent_fills_limit,
        reconcile_lookback_hours=base_config.reconcile_lookback_hours,
    )

    # Resolve snapshot inputs. Defaults intentionally resolve to Hold so
    # that omitting flags yields a no-op tick (mirrors ROB-293 smoke).
    price = _resolve_decimal(args.simulate_price, default=Decimal("50000"))
    ema20 = _resolve_decimal(args.simulate_ema20, default=price)
    ema50 = _resolve_decimal(args.simulate_ema50, default=price)
    evidence.snapshot = {
        "symbol": args.symbol,
        "last_price": str(price),
        "rsi_5m": str(args.simulate_rsi),
        "ema_20_5m": str(ema20),
        "ema_50_5m": str(ema50),
        "instrument_health": args.simulate_instrument_health,
        "max_notional_usdt": str(max_notional),
    }
    snapshot_factory = await _build_snapshot_factory(
        symbol=args.symbol,
        price=price,
        rsi=args.simulate_rsi,
        ema20=ema20,
        ema50=ema50,
        instrument_health=args.simulate_instrument_health,
    )

    client = BinanceTestnetExecutionClient.from_env()
    started = datetime.now(tz=UTC)
    async with AsyncSessionLocal() as session:
        instrument_id_for_symbol = await _instrument_id_resolver(session)
        instrument_id = await instrument_id_for_symbol(args.symbol)
        ledger = BinanceTestnetLedgerService(session=session)

        # Restrict the runner's reconcile + tick surface to the chosen
        # symbol only. ``ScalperConfig.symbols`` is frozen, so we wrap.
        single_symbol_config = ScalperConfig(
            symbols=frozenset({args.symbol}),
            max_notional_usdt=config.max_notional_usdt,
            rsi_oversold=config.rsi_oversold,
            rsi_overbought=config.rsi_overbought,
            tp_pct=config.tp_pct,
            sl_pct=config.sl_pct,
            reconcile_open_orders_limit=config.reconcile_open_orders_limit,
            reconcile_recent_fills_limit=config.reconcile_recent_fills_limit,
            reconcile_lookback_hours=config.reconcile_lookback_hours,
        )

        runner = ScalperRunner(
            execution_client=client,
            ledger_service=ledger,
            config=single_symbol_config,
            instrument_id_for_symbol=instrument_id_for_symbol,
            market_snapshot_for_symbol=snapshot_factory,
            dry_run=args.dry_run,
        )

        evidence.ledger_rows_before = await _count_ledger_rows(
            session, instrument_id=instrument_id
        )

        try:
            recon = await runner.reconcile_on_start()
            evidence.reconcile_rows_examined = recon.rows_examined
            evidence.reconcile_anomalies_detected = recon.anomalies_detected
            if recon.anomaly_client_order_ids:
                evidence.notes.append(
                    f"reconcile detected {recon.anomalies_detected} "
                    "anomaly row(s); review before proceeding"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("lifecycle smoke: reconcile_on_start failed: %s", exc)
            evidence.notes.append(f"reconcile_on_start failed: {exc}")

        try:
            tick = await runner.tick_once(symbol=args.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.error("lifecycle smoke: tick failed: %s", exc)
            evidence.notes.append(f"tick failed: {exc}")
            await client.aclose()
            return 2

        evidence.tick_action = tick.action_name
        evidence.tick_submitted = tick.submitted
        evidence.tick_dry_run = tick.dry_run
        evidence.tick_notes = tick.notes

        # Collect the ledger trail produced this run.
        states = await _collect_run_ledger_states(
            session, instrument_id=instrument_id, since=started
        )
        evidence.client_order_ids_created = sorted(states.keys())
        evidence.final_lifecycle_states = states
        evidence.anomaly_client_order_ids = sorted(
            cid for cid, st in states.items() if st == "anomaly"
        )

        # Optional: cancel any row still in ``submitted`` state. This
        # exercises the not-filled-within-timeout → cancel branch when
        # the broker leaves the entry pending. Cancels use the same
        # ``confirm``/``dry_run`` discipline as the main submit path.
        if args.cancel_pending_on_exit:
            for cid, st in list(states.items()):
                if st != "submitted":
                    continue
                try:
                    cancel_result = await client.cancel_order(
                        symbol=args.symbol,
                        client_order_id=cid,
                        dry_run=args.dry_run,
                        confirm=args.confirm,
                    )
                    if isinstance(cancel_result, DryRunResult):
                        evidence.notes.append(
                            f"cancel-pending: {cid} preview-only "
                            "(confirm/no-dry-run not set)"
                        )
                    else:
                        await ledger.record_cancel(
                            client_order_id=cid,
                            reason="lifecycle_smoke_cancel_pending",
                        )
                        evidence.notes.append(
                            f"cancel-pending: cancelled {cid} at broker"
                        )
                except Exception as exc:  # noqa: BLE001
                    evidence.notes.append(
                        f"cancel-pending: failed to cancel {cid}: {exc}"
                    )

        # Refresh states after possible cancellations.
        states = await _collect_run_ledger_states(
            session, instrument_id=instrument_id, since=started
        )
        evidence.final_lifecycle_states = states
        evidence.anomaly_client_order_ids = sorted(
            cid for cid, st in states.items() if st == "anomaly"
        )

        # Broker open-orders count after the tick — purely observational.
        try:
            broker_open = await client.open_orders(symbol=args.symbol)
            evidence.broker_open_orders_after = len(broker_open)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lifecycle smoke: broker open_orders failed for %s: %s",
                args.symbol,
                exc,
            )
            evidence.notes.append(f"broker open_orders failed: {exc}")

        evidence.ledger_rows_after = await _count_ledger_rows(
            session, instrument_id=instrument_id
        )

        await session.commit()
    await client.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # Avoid double-configuration: if a caller (e.g., tests) already
    # configured root logging, ``basicConfig`` is a no-op.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    evidence = LifecycleEvidence(
        mode="default-disabled",
        symbol=args.symbol,
        started_at=datetime.now(tz=UTC).isoformat(),
        cli_command=[sys.argv[0]] + list(sys.argv[1:]) if argv is None else list(argv),
    )
    _collect_env_presence(evidence)

    # Default-disabled gate (mirrors ROB-286 smoke).
    if not _truthy(os.environ.get("BINANCE_TESTNET_ENABLED")):
        logger.info("scalper disabled — set BINANCE_TESTNET_ENABLED=true to opt in")
        evidence.completed_at = datetime.now(tz=UTC).isoformat()
        evidence.exit_code = 0
        _print_evidence(evidence)
        _maybe_write_evidence_json(evidence, path=args.evidence_json)
        return 0

    if not args.symbol:
        logger.error(
            "lifecycle smoke: --symbol is required when opted in (must be one of %s)",
            sorted(MVP_SYMBOLS),
        )
        evidence.completed_at = datetime.now(tz=UTC).isoformat()
        evidence.exit_code = 1
        _print_evidence(evidence)
        _maybe_write_evidence_json(evidence, path=args.evidence_json)
        return 1

    # --confirm implies --no-dry-run at the CLI layer for symmetry with
    # the ROB-286 smoke. Without --confirm we stay in preview-only mode.
    dry_run = args.dry_run and not args.confirm
    args.dry_run = dry_run
    evidence.tick_dry_run = dry_run
    evidence.mode = "confirmed-single-cycle" if args.confirm else "dry-run"

    try:
        exit_code = asyncio.run(_run_lifecycle(args=args, evidence=evidence))
    except Exception as exc:  # noqa: BLE001
        # Top-level safety net — operator misconfiguration / missing
        # credentials surfaced via custom exceptions land here.
        logger.error("lifecycle smoke: top-level failure: %s", exc)
        evidence.notes.append(f"top-level failure: {exc}")
        exit_code = 1

    evidence.completed_at = datetime.now(tz=UTC).isoformat()
    evidence.exit_code = exit_code
    _print_evidence(evidence)
    _maybe_write_evidence_json(evidence, path=args.evidence_json)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
