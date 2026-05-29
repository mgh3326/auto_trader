#!/usr/bin/env python3
"""KIS mock **overseas/US** holdings-delta fill-confirmation smoke (ROB-364).

US counterpart to ``scripts/kis_mock_holdings_delta_smoke.py`` (domestic, ROB-358).
Two modes, both KIS **mock** only (``is_mock=True`` -> mock host; overseas mock
order TRs VTTT1002U/VTTT1006U/VTTT1004U):

* ``--preflight`` — READ-ONLY. Resolves the US exchange, reads the mock overseas
  holdings (``fetch_my_us_stocks``) and a best-effort margin snapshot, prints the
  per-symbol holdings + cash availability. Places NO order. Run this first.
* ``--confirm``   — operator-gated bounded round trip: one small marketable-limit
  BUY, holdings-delta fill confirmation, then cleanup (SELL a filled residual, or
  CANCEL an unfilled resting BUY) to return to baseline. Prints a JSON evidence
  packet.

Why this is NOT the domestic smoke with a different symbol:

* There is no overseas ``KisMockBroker`` — the domestic one reads
  ``fetch_domestic_balance_snapshot`` and routes its cleanup SELL through the
  KR-only scalping-exit validator. This smoke talks to the overseas order client
  directly, so there is NO ``KIS_MOCK_SCALPING_ENABLED`` gate and NO scalping-exit
  reason — those are KR-only surfaces.
* USD cash/margin is OPSQ0002-blocked in KIS mock overseas, so cash-delta is
  unavailable: holdings delta is the SOLE fill gate and the fill price always
  falls back to the submitted limit (``fill_price_source="limit_fallback"``).
* ``inquire_overseas_orders`` (pending) is unavailable in mock and
  ``inquire_daily_order_overseas`` can be empty for same-day mock fills, so
  neither gates the verdict — holdings delta does.

Same-day fill confirmation is the baseline-vs-post **holdings delta** (load
bearing). A full directional delta is the only confirmation; partial / zero /
wrong-direction fails closed.

Safety: KIS mock only (no live), limit orders only (no market), no shorting, no
scheduler, no automatic submit, no persistent env/flag changes. Default-disabled
— requires ``KIS_MOCK_OVERSEAS_SMOKE_ENABLED=true`` (read directly from the
environment, NOT a persistent Settings flag) plus KIS mock config. ``--confirm``
must be passed explicitly. Prints only missing env var NAMES, never secret
values. Always attempts cleanup in a ``finally`` block.

Exit codes:
    0  - success (preflight printed, or confirmed round trip cleaned up to baseline)
    1  - unexpected exception
    2  - pre-BUY blocked (no/stale quote, unresolved exchange, size zero, baseline
         read failed) OR fill could not be confirmed in the poll window (but flat)
    3  - anomaly: residual position / pending order could not be cleaned up
    4  - disabled, KIS mock not configured, cleanup path not submittable, or US
         market closed (no order placed)

Usage:
    KIS_MOCK_OVERSEAS_SMOKE_ENABLED=true uv run python -m \
        scripts.kis_mock_overseas_holdings_delta_smoke --preflight --symbol AAPL
    KIS_MOCK_OVERSEAS_SMOKE_ENABLED=true uv run python -m \
        scripts.kis_mock_overseas_holdings_delta_smoke \
        --confirm --symbol AAPL --exchange NASD --notional-usd 20
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from zoneinfo import ZoneInfo

from app.services.brokers.kis.mock_scalping_exec.holdings_delta_confirm import (
    derive_fill_price,
)
from app.services.brokers.kis.mock_scalping_exec.overseas_holdings_confirm import (
    extract_overseas_holdings_qty,
    latest_close_from_minute_frame,
    latest_timestamp_from_minute_frame,
    quote_is_fresh,
)
from app.services.brokers.kis.overseas_orders import _normalize_kis_exchange_code
from app.services.kis_mock_holdings_reconciler import classify_fill_by_delta

logger = logging.getLogger(__name__)

# KIS overseas minute-chart timestamps are exchange-local (US/Eastern) and naive;
# localize to this zone before comparing to wall-clock UTC for the freshness gate.
_QUOTE_TZ = ZoneInfo("America/New_York")
_TRUTHY = {"1", "true", "yes", "on"}


class _ExchangeResolutionError(RuntimeError):
    """The US listing exchange could not be resolved for the smoke symbol."""


@dataclass
class _EntryFill:
    price: Decimal
    quantity: Decimal
    price_source: str


def _env_truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() in _TRUTHY


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KIS mock overseas/US holdings-delta fill-confirmation smoke"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--preflight",
        action="store_true",
        help="read-only: print holdings + cash for --symbol, place no order",
    )
    mode.add_argument(
        "--confirm",
        action="store_true",
        help="operator-gated bounded round trip (one small limit buy + cleanup)",
    )
    parser.add_argument("--symbol", required=True, help="US ticker, e.g. AAPL")
    parser.add_argument(
        "--exchange",
        default="",
        help="KIS exchange code (NASD/NYSE/AMEX); resolved from the universe if omitted",
    )
    parser.add_argument(
        "--notional-usd",
        type=float,
        default=20.0,
        help="max buy notional in USD (default 20)",
    )
    parser.add_argument("--max-poll", type=int, default=10)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument(
        "--max-quote-staleness-min",
        type=float,
        default=10.0,
        help="reject the marketable limit if the latest candle is older than this",
    )
    return parser.parse_args(argv)


def _gate_or_exit() -> object | None:
    """Lazy import + env gate. Returns the settings object, or None if disabled.

    Top-level enable is the dedicated ``KIS_MOCK_OVERSEAS_SMOKE_ENABLED`` env var,
    read directly (NOT a persistent Settings flag) so this smoke cannot be turned
    on by the unrelated WS-scalping flag and so the repo grows no new config field.
    """
    if not _env_truthy(os.environ.get("KIS_MOCK_OVERSEAS_SMOKE_ENABLED")):
        logger.info(
            "KIS_MOCK_OVERSEAS_SMOKE_ENABLED is not set; smoke disabled (no-op)."
        )
        return None
    from app.core.config import settings

    missing = [
        name
        for name, value in (
            ("KIS_MOCK_APP_KEY", settings.kis_mock_app_key),
            ("KIS_MOCK_APP_SECRET", settings.kis_mock_app_secret),
            ("KIS_MOCK_ACCOUNT_NO", settings.kis_mock_account_no),
        )
        if not value
    ]
    if missing:
        logger.error("KIS mock not configured. Missing (names only): %s", missing)
        return None
    return settings


def _cleanup_path_preflight_error(settings_obj: object) -> str | None:
    """Fail-fast check that the cleanup SELL/CANCEL is submittable, BEFORE any BUY.

    The overseas order client derives CANO/ACNT_PRDT_CD from a >=10-digit account
    number; if it cannot, the cleanup SELL/CANCEL would raise and we would be left
    holding a position we cannot flatten. There is no scalping-exit reason gate for
    overseas (that is KR-only).
    """
    acct = getattr(settings_obj, "kis_mock_account_no", None)
    digits = str(acct or "").replace("-", "")
    if len(digits) < 10:
        return (
            "KIS_MOCK_ACCOUNT_NO must be >=10 digits to form CANO/ACNT_PRDT_CD for "
            "the cleanup SELL/CANCEL; refusing to BUY a position we cannot flatten"
        )
    return None


def _us_market_open() -> bool:
    """True iff the US equity market is open right now (XNYS trading minute)."""
    from app.jobs.watch_market_data import is_market_open

    return bool(is_market_open("us"))


async def _resolve_exchange(args: argparse.Namespace) -> str:
    """Resolve the canonical 4-digit KIS exchange code for the smoke symbol.

    Prefers an explicit ``--exchange`` (operator-authoritative); otherwise looks the
    symbol up in ``us_symbol_universe``. Raises :class:`_ExchangeResolutionError`
    rather than guessing — for a real order, the wrong exchange can be rejected.
    """
    explicit = (args.exchange or "").strip()
    if explicit:
        try:
            return _normalize_kis_exchange_code(explicit)
        except ValueError as exc:
            raise _ExchangeResolutionError(str(exc)) from exc
    try:
        from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

        code = await get_us_exchange_by_symbol(args.symbol)
        return _normalize_kis_exchange_code(code)
    except Exception as exc:  # noqa: BLE001 - any resolution fault fails closed
        raise _ExchangeResolutionError(
            f"could not resolve exchange for {args.symbol!r}: {str(exc)[:200]}"
        ) from exc


async def _read_holdings_qty(client, symbol: str, exchange: str) -> Decimal | None:
    """Return the mock overseas held quantity for ``symbol``, or None on read fault.

    A successful read that does not list the symbol means a zero position (the read
    pre-filters to nonzero holdings); only a raised exception is a read failure.
    """
    try:
        rows = await client.fetch_my_us_stocks(is_mock=True, exchange=exchange)
    except Exception as exc:  # noqa: BLE001 - read fault fails closed
        logger.error("overseas holdings read failed: %s", str(exc)[:200])
        return None
    return extract_overseas_holdings_qty(rows, symbol)


async def _latest_close(client, symbol: str, exchange: str) -> Decimal | None:
    try:
        page = await client.inquire_overseas_minute_chart(symbol, exchange, n=1)
    except Exception as exc:  # noqa: BLE001
        logger.error("overseas minute-chart read failed: %s", str(exc)[:200])
        return None
    return latest_close_from_minute_frame(page.frame)


def _localize_quote_ts(naive_or_aware: dt.datetime) -> dt.datetime:
    if naive_or_aware.tzinfo is None:
        return naive_or_aware.replace(tzinfo=_QUOTE_TZ).astimezone(dt.UTC)
    return naive_or_aware.astimezone(dt.UTC)


async def _await_entry_fill(
    client,
    args: argparse.Namespace,
    symbol: str,
    exchange: str,
    base_qty: Decimal,
    ordered_qty: Decimal,
    limit_price: Decimal,
) -> _EntryFill | None:
    for _ in range(max(args.max_poll, 1)):
        cur = await _read_holdings_qty(client, symbol, exchange)
        if cur is None:
            return None
        decision = classify_fill_by_delta(
            side="buy", ordered_qty=ordered_qty, baseline_qty=base_qty, observed_qty=cur
        )
        if decision.verdict == "filled":
            price, source = derive_fill_price(
                side="buy",
                filled_qty=decision.filled_qty,
                cash_baseline=None,  # OPSQ0002: cash unavailable in mock overseas
                cash_observed=None,
                limit_price=limit_price,
            )
            return _EntryFill(
                price=price, quantity=decision.filled_qty, price_source=source
            )
        await asyncio.sleep(args.poll_interval)
    return None


async def _await_flat(
    client, args: argparse.Namespace, symbol: str, exchange: str, base_qty: Decimal
) -> Decimal | None:
    final_qty: Decimal | None = None
    for _ in range(max(args.max_poll, 1)):
        final_qty = await _read_holdings_qty(client, symbol, exchange)
        if final_qty is None:
            return None
        if final_qty - base_qty <= 0:
            return final_qty
        await asyncio.sleep(args.poll_interval)
    return final_qty


async def _cancel_resting_buy(
    client,
    args: argparse.Namespace,
    exchange: str,
    buy_odno: str,
    buy_qty: Decimal,
    evidence: dict,
) -> int:
    """Cancel an unfilled resting BUY so it cannot fill after the smoke exits.

    Inspects the cancel response order id explicitly; a rejection or a missing id
    is an explicit anomaly (exit 3), never a silent success.
    """
    try:
        cancel = await client.cancel_overseas_order(
            order_number=buy_odno,
            symbol=args.symbol,
            exchange_code=exchange,
            quantity=int(buy_qty),
            is_mock=True,
        )
    except Exception as exc:  # noqa: BLE001 - submit rejection is a cleanup anomaly
        evidence["cleanup_cancel_order_id"] = None
        evidence["cleanup_error"] = str(exc)[:200]
        evidence["cleanup"] = "CANCEL_submit_rejected"
        return 3
    cancel_id = cancel.get("odno") if isinstance(cancel, Mapping) else None
    evidence["cleanup_cancel_order_id"] = cancel_id
    if not cancel_id:
        evidence["cleanup_error"] = "cleanup CANCEL response missing odno"
        evidence["cleanup"] = "CANCEL_no_order_id"
        return 3
    return 0


async def _cleanup_and_verify(
    client,
    args: argparse.Namespace,
    exchange: str,
    base_qty: Decimal,
    buy_odno: str | None,
    buy_qty: Decimal,
    evidence: dict,
    entry_fill,
) -> int:
    """Return holdings to baseline. Returns the process exit code (0 clean, 2
    fill-unconfirmed-but-flat, 3 anomaly/residual)."""
    symbol = args.symbol
    cur = await _read_holdings_qty(client, symbol, exchange)
    if cur is None:
        evidence["cleanup"] = "holdings_read_failed"
        evidence["cleanup_error"] = "post-buy holdings read failed"
        return 3
    delta = cur - base_qty
    evidence["cleanup_current_delta"] = str(delta)
    if delta < 0:
        # Holdings below baseline before we sold (over-flatten / external mutation).
        evidence["cleanup"] = "below_baseline_anomaly"
        evidence["cleanup_error"] = (
            f"holdings {cur} below baseline {base_qty} before cleanup"
        )
        evidence["final_position_delta_vs_baseline"] = str(delta)
        return 3

    if delta == 0:
        # No filled shares. A resting unfilled BUY could still fill later -> cancel it.
        if buy_odno:
            cancel_rc = await _cancel_resting_buy(
                client, args, exchange, buy_odno, buy_qty, evidence
            )
            if cancel_rc != 0:
                return cancel_rc
            cur = await _read_holdings_qty(client, symbol, exchange)
            if cur is None:
                evidence["cleanup"] = "holdings_read_failed"
                evidence["cleanup_error"] = "post-cancel holdings read failed"
                return 3
            delta = cur - base_qty
            evidence["post_cancel_delta"] = str(delta)
            if delta < 0:
                evidence["cleanup"] = "below_baseline_anomaly"
                evidence["cleanup_error"] = (
                    f"holdings {cur} below baseline {base_qty} after cancel"
                )
                evidence["final_position_delta_vs_baseline"] = str(delta)
                return 3
        if delta == 0:
            evidence["cleanup"] = (
                "flat_after_cancel" if buy_odno else "nothing_to_flatten"
            )
            evidence["final_position_delta_vs_baseline"] = "0"
            return 0 if entry_fill is not None else 2
        # else: a late fill landed during the cancel -> fall through to SELL.

    # delta > 0: residual filled shares -> SELL flatten.
    sell_close = await _latest_close(client, symbol, exchange)
    if sell_close is None:
        evidence["cleanup"] = "no_quote_for_exit"
        evidence["cleanup_error"] = "no usable close to price the cleanup SELL"
        evidence["final_position_delta_vs_baseline"] = str(delta)
        return 3
    try:
        sell = await client.sell_overseas_stock(
            symbol=symbol,
            exchange_code=exchange,
            quantity=int(delta),
            price=float(sell_close),
            is_mock=True,
        )
    except Exception as exc:  # noqa: BLE001 - submit rejection is a cleanup anomaly
        evidence["cleanup_sell_order_id"] = None
        evidence["cleanup_error"] = str(exc)[:200]
        evidence["cleanup"] = "SELL_submit_rejected"
        evidence["final_position_delta_vs_baseline"] = str(delta)
        return 3
    order_id = sell.get("odno") if isinstance(sell, Mapping) else None
    evidence["cleanup_sell_order_id"] = order_id
    if not order_id:
        evidence["cleanup_error"] = "cleanup SELL response missing odno"
        evidence["cleanup"] = "SELL_no_order_id"
        evidence["final_position_delta_vs_baseline"] = str(delta)
        return 3

    final_qty = await _await_flat(client, args, symbol, exchange, base_qty)
    if final_qty is None:
        evidence["cleanup"] = "holdings_read_failed"
        evidence["cleanup_error"] = "post-sell holdings read failed"
        return 3
    final_delta = final_qty - base_qty
    evidence["post_holdings_qty"] = str(final_qty)
    evidence["final_position_delta_vs_baseline"] = str(final_delta)
    if final_delta > 0:
        evidence["cleanup"] = "UNCONFIRMED_residual_position"
        return 3
    if final_delta < 0:
        evidence["cleanup"] = "over_flattened_anomaly"
        evidence["cleanup_error"] = (
            f"final holdings {final_qty} below baseline {base_qty} after cleanup SELL"
        )
        return 3
    evidence["cleanup"] = "flattened"
    return 0


def _parse_usd_cash(margin_rows) -> tuple[Decimal | None, str]:
    """Best-effort USD cash from an overseas margin response (None in mock OPSQ0002)."""
    if not isinstance(margin_rows, list):
        return None, "unavailable"
    for row in margin_rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("crcy_cd", "")).upper() != "USD":
            continue
        # `inquire_overseas_margin` normalizes the USD row to `frcr_dncl_amt1`
        # (raw `frcr_dncl_amt_2` kept as a fallback for raw payloads).
        for key in ("frcr_dncl_amt1", "frcr_dncl_amt_2"):
            raw = row.get(key)
            if raw in (None, ""):
                continue
            try:
                return Decimal(str(raw).replace(",", "").strip()), key
            except Exception:  # noqa: BLE001
                continue
    return None, "unavailable"


async def run_preflight(args: argparse.Namespace) -> int:
    if _gate_or_exit() is None:
        return 4
    from app.mcp_server.tooling.order_execution import _create_kis_client

    client = _create_kis_client(is_mock=True)
    try:
        exchange = await _resolve_exchange(args)
    except _ExchangeResolutionError as exc:
        logger.error("%s", exc)
        logger.info(
            json.dumps(
                {
                    "mode": "preflight",
                    "symbol": args.symbol,
                    "error": "exchange_unresolved",
                }
            )
        )
        return 2
    try:
        rows = await client.fetch_my_us_stocks(is_mock=True, exchange=exchange)
    except Exception as exc:  # noqa: BLE001 - read-only preflight, classify the fault
        logger.error("overseas balance snapshot read failed: %s", str(exc)[:300])
        return 2
    holdings_qty = extract_overseas_holdings_qty(rows, args.symbol)

    cash: Decimal | None = None
    cash_source = "unavailable_opsq0002"
    try:
        margin = await client.inquire_overseas_margin(is_mock=True)
        cash, cash_source = _parse_usd_cash(margin)
    except Exception as exc:  # noqa: BLE001 - OPSQ0002 expected in mock overseas
        logger.info(
            "overseas margin unavailable (expected in mock): %s", str(exc)[:160]
        )

    logger.info(
        json.dumps(
            {
                "mode": "preflight",
                "symbol": args.symbol,
                "exchange": exchange,
                "holdings_qty": str(holdings_qty),
                "cash_usd": (str(cash) if cash is not None else None),
                "cash_source": cash_source,
            }
        )
    )
    return 0


async def run_confirm(args: argparse.Namespace) -> int:
    settings_obj = _gate_or_exit()
    if settings_obj is None:
        return 4
    # Fail-fast BEFORE any BUY: never acquire a position we cannot flatten.
    cleanup_error = _cleanup_path_preflight_error(settings_obj)
    if cleanup_error is not None:
        logger.error("cleanup preflight failed; no order placed: %s", cleanup_error)
        logger.info(
            json.dumps(
                {
                    "mode": "confirm",
                    "symbol": args.symbol,
                    "preflight": "cleanup_path_unsubmittable",
                    "error": cleanup_error,
                }
            )
        )
        return 4
    if not _us_market_open():
        logger.error("US market is not open right now; no order placed.")
        logger.info(
            json.dumps(
                {
                    "mode": "confirm",
                    "symbol": args.symbol,
                    "preflight": "us_market_closed",
                }
            )
        )
        return 4

    from app.mcp_server.tooling.order_execution import _create_kis_client

    client = _create_kis_client(is_mock=True)
    evidence: dict = {"mode": "confirm", "symbol": args.symbol}

    try:
        exchange = await _resolve_exchange(args)
    except _ExchangeResolutionError as exc:
        evidence["error"] = "exchange_unresolved"
        evidence["detail"] = str(exc)[:200]
        logger.info(json.dumps(evidence))
        return 2
    evidence["exchange"] = exchange

    # Single minute-chart read: derive BOTH the marketable-limit price and the
    # freshness timestamp from the SAME candle (no second read that could pick a
    # different last row).
    try:
        page = await client.inquire_overseas_minute_chart(args.symbol, exchange, n=1)
    except Exception as exc:  # noqa: BLE001 - quote read fault blocks the BUY
        evidence["error"] = "no_quote"
        evidence["detail"] = str(exc)[:200]
        logger.info(json.dumps(evidence))
        return 2
    close = latest_close_from_minute_frame(page.frame)
    if close is None:
        evidence["error"] = "no_quote"
        logger.info(json.dumps(evidence))
        return 2
    latest_ts = latest_timestamp_from_minute_frame(page.frame)
    if latest_ts is None or not quote_is_fresh(
        _localize_quote_ts(latest_ts),
        dt.datetime.now(dt.UTC),
        max_staleness_seconds=args.max_quote_staleness_min * 60,
    ):
        evidence["error"] = "stale_quote"
        logger.info(json.dumps(evidence))
        return 2
    evidence["buy_limit_price"] = str(close)

    base_qty = await _read_holdings_qty(client, args.symbol, exchange)
    if base_qty is None:
        evidence["error"] = "baseline_read_failed"
        logger.info(json.dumps(evidence))
        return 2
    evidence["baseline_holdings_qty"] = str(base_qty)

    qty = int((Decimal(str(args.notional_usd)) / close).to_integral_value(ROUND_DOWN))
    if qty <= 0:
        evidence["error"] = "size_zero"
        logger.info(json.dumps(evidence))
        return 2
    evidence["quantity"] = str(qty)

    buy_odno: str | None = None
    entry_fill: _EntryFill | None = None
    try:
        buy = await client.buy_overseas_stock(
            symbol=args.symbol,
            exchange_code=exchange,
            quantity=qty,
            price=float(close),
            is_mock=True,
        )
        buy_odno = buy.get("odno") if isinstance(buy, Mapping) else None
        evidence["buy_order_id"] = buy_odno
        evidence["confirmation_signal"] = "holdings_delta"
        if not buy_odno:
            evidence["entry"] = "BUY_no_order_id"
            evidence["note"] = "buy response missing odno; nothing acked to flatten"
        else:
            entry_fill = await _await_entry_fill(
                client, args, args.symbol, exchange, base_qty, Decimal(qty), close
            )
            if entry_fill is None:
                evidence["entry_filled"] = False
                evidence["note"] = (
                    "entry fill UNCONFIRMED within poll window — holdings did not "
                    "reflect a same-day mock fill (ROB-364 STOP condition)"
                )
            else:
                evidence["entry_filled"] = True
                evidence["entry_fill_price"] = str(entry_fill.price)
                evidence["entry_fill_qty"] = str(entry_fill.quantity)
                evidence["fill_price_source"] = entry_fill.price_source
    finally:
        result = await _cleanup_and_verify(
            client,
            args,
            exchange,
            base_qty,
            buy_odno,
            Decimal(qty),
            evidence,
            entry_fill,
        )
        evidence["exit_code"] = result
        logger.info(json.dumps(evidence))
    return result


async def _run(args: argparse.Namespace) -> int:
    if args.preflight:
        return await run_preflight(args)
    return await run_confirm(args)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        return asyncio.run(_run(_parse_args(argv)))
    except KeyboardInterrupt:
        return 1
    except Exception:  # noqa: BLE001
        logger.exception("unexpected error in overseas holdings-delta smoke")
        return 1


if __name__ == "__main__":
    sys.exit(main())
