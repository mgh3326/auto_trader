"""Operator-safe Kiwoom US mock lifecycle smoke (ROB-867)."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from typing import Any

from app.core.config import validate_kiwoom_mock_us_config
from app.mcp_server.tooling import orders_kiwoom_us_variants as us_variants
from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.normalization import redact_broker_response
from app.services.brokers.kiwoom.us_account import KiwoomUsAccountClient
from app.services.brokers.kiwoom.us_client import KiwoomMockUsClient
from app.services.brokers.kiwoom.us_orders import KiwoomUsOrderClient
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

# Bounded-digits acceptance: the guide documents nine digits, but the live
# mock shape is unverified and cancel must never be skipped over a width
# mismatch. The actual observed length is emitted as evidence per accept.
_ORDER_ID_RE = re.compile(r"^\d{1,18}$")
_PROBE_CODES = frozenset({"26", "27", "30", "33", "34", "35"})
_BUY_PROBE_CODES = frozenset({"26", "27", "30"})
_SELL_PROBE_CODES = frozenset({"33", "34", "35"})


class SmokeRejected(RuntimeError):
    """Raised when operator input violates a smoke safety boundary."""


class _Recorder:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *, name: str, description: str = ""):
        del description

        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def _tools() -> dict[str, Any]:
    recorder = _Recorder()
    us_variants.register(recorder)
    return recorder.tools


def _sanitize_output(value: Any) -> Any:
    if isinstance(value, dict):
        return redact_broker_response(value)
    if isinstance(value, list):
        return [_sanitize_output(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_output(item) for item in value]
    return value


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(_sanitize_output(payload), ensure_ascii=False, default=str))


def extract_order_id(payload: dict[str, Any]) -> str | None:
    for key in ("ord_no", "order_no", "orgn_odno", "odno"):
        value = str(payload.get(key) or "").strip()
        if _ORDER_ID_RE.fullmatch(value):
            return value
    broker_response = payload.get("broker_response")
    if isinstance(broker_response, dict):
        return extract_order_id(broker_response)
    return None


def parse_probe_codes(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return ()
    result: list[str] = []
    for item in raw.split(","):
        code = item.strip()
        if code not in _PROBE_CODES:
            raise SmokeRejected(
                "--probe-order-types supports documented candidates "
                "26,27,30,33,34,35 only"
            )
        if code not in result:
            result.append(code)
    return tuple(result)


def _normalize_digits(value: str) -> str | None:
    text = value.strip()
    if not text.isdigit():
        return None
    return text.lstrip("0") or "0"


def _payload_contains_order_id(value: Any, order_id: str) -> bool:
    if isinstance(value, dict):
        return any(
            _payload_contains_order_id(item, order_id) for item in value.values()
        )
    if isinstance(value, list):
        return any(_payload_contains_order_id(item, order_id) for item in value)
    # Zero-padding representation may differ between the accept response and
    # the history TR (12-char left-padded), so compare digit-normalized.
    normalized = _normalize_digits(str(value))
    target = _normalize_digits(order_id)
    return normalized is not None and target is not None and normalized == target


async def run_preflight() -> dict[str, Any]:
    missing = validate_kiwoom_mock_us_config()
    result: dict[str, Any] = {
        "step": "preflight",
        "ok": not missing,
        "missing_env_keys": missing,
    }
    if missing:
        return result

    try:
        client = KiwoomMockUsClient.from_app_settings()
        account = KiwoomUsAccountClient(client)
        checks = (
            ("ust21050", account.get_open_orders),
            ("ust21070", account.get_positions),
            ("ust21510", account.get_today_orders),
            ("ust21110", account.get_foreign_deposit),
            ("ust21160", account.get_us_deposit_detail),
        )
        for api_id, method in checks:
            raw = await method()
            _emit({"step": "preflight_read", "api_id": api_id, "broker_response": raw})
            try:
                succeeded = int(raw.get("return_code", -1)) == 0
            except (TypeError, ValueError):
                succeeded = False
            if not succeeded:
                result["ok"] = False
    except Exception as exc:  # noqa: BLE001 - operator evidence, fail closed
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


async def run_preview(args: argparse.Namespace) -> dict[str, Any]:
    return await _tools()["kiwoom_mock_us_preview_order"](
        symbol=args.symbol,
        side=args.side,
        quantity=args.quantity,
        price=args.price,
        trde_tp=args.trde_tp,
    )


async def run_full(args: argparse.Namespace) -> int:
    if args.trde_tp != "00":
        raise SmokeRejected("full mode is limit-only; use trde_tp=00")
    if args.price is None:
        raise SmokeRejected("full mode limit order requires --price")

    tools = _tools()
    preview = await run_preview(args)
    _emit({"step": "preview", **preview})
    if not preview.get("success"):
        return 2

    dry = await tools["kiwoom_mock_us_place_order"](
        symbol=args.symbol,
        side=args.side,
        quantity=args.quantity,
        price=args.price,
        trde_tp="00",
        dry_run=True,
    )
    _emit({"step": "place_dry_run", **dry})
    if not dry.get("success"):
        return 2
    if not args.confirm:
        _emit({"step": "stop", "reason": "no --confirm; no broker mutation"})
        return 0

    placed = await tools["kiwoom_mock_us_place_order"](
        symbol=args.symbol,
        side=args.side,
        quantity=args.quantity,
        price=args.price,
        trde_tp="00",
        dry_run=False,
        confirm=True,
    )
    _emit({"step": "place_confirmed", **placed})
    if not placed.get("success"):
        return 2

    order_id = extract_order_id(placed)
    if order_id is None:
        history = await tools["kiwoom_mock_us_get_order_history"](
            scope="open", symbol=args.symbol
        )
        _emit({"step": "reconcile_no_order_id", **history})
        _emit(
            {
                "step": "cleanup_required",
                "reason": "accepted order id was not an all-digits value",
            }
        )
        return 2
    _emit(
        {
            "step": "order_id_evidence",
            "order_id_length": len(order_id),
            "matches_documented_nine_digits": len(order_id) == 9,
        }
    )

    exit_code = 0
    try:
        history = await tools["kiwoom_mock_us_get_order_history"](
            scope="open", symbol=args.symbol
        )
        _emit({"step": "history_after_place", **history})
        if not history.get("success"):
            exit_code = 2

        if args.new_price is not None:
            modified = await tools["kiwoom_mock_us_modify_order"](
                order_id=order_id,
                symbol=args.symbol,
                new_price=args.new_price,
                dry_run=False,
                confirm=True,
            )
            _emit({"step": "modify_confirmed", **modified})
            if not modified.get("success"):
                exit_code = 2
            elif modified_id := extract_order_id(modified):
                order_id = modified_id
    finally:
        try:
            cancelled = await tools["kiwoom_mock_us_cancel_order"](
                order_id=order_id,
                symbol=args.symbol,
                dry_run=False,
                confirm=True,
            )
            _emit({"step": "cancel_confirmed", **cancelled})
            if not cancelled.get("success"):
                exit_code = 2
                _emit(
                    {
                        "step": "cleanup_required",
                        "order_id": order_id,
                        "reason": "cancel did not succeed",
                    }
                )
        except Exception as exc:  # noqa: BLE001 - cleanup must affect exit status
            exit_code = 2
            _emit(
                {
                    "step": "cleanup_required",
                    "order_id": order_id,
                    "reason": f"cancel raised {type(exc).__name__}: {exc}",
                }
            )

    final_history = await tools["kiwoom_mock_us_get_order_history"](
        scope="open", symbol=args.symbol
    )
    _emit({"step": "final_open_orders", **final_history})
    if not final_history.get("success") or _payload_contains_order_id(
        final_history, order_id
    ):
        exit_code = 2
        _emit(
            {
                "step": "cleanup_required",
                "order_id": order_id,
                "reason": "final open-order reconciliation did not prove removal",
            }
        )
    positions = await tools["kiwoom_mock_us_get_positions"](symbol=args.symbol)
    _emit({"step": "final_positions", **positions})
    if not positions.get("success"):
        exit_code = 2
    return exit_code


def _probe_price_args(
    code: str, args: argparse.Namespace
) -> tuple[float | None, float | None]:
    if code in {"33", "35"}:
        price = None
    else:
        price = args.price
    stop_price = args.stop_price if code in {"34", "35"} else None
    if code in {"26", "27", "30", "34"} and price is None:
        raise SmokeRejected(f"trde_tp={code} requires --price")
    if code in {"34", "35"} and stop_price is None:
        raise SmokeRejected(f"trde_tp={code} requires --stop-price")
    return price, stop_price


async def run_probe(args: argparse.Namespace) -> int:
    codes = parse_probe_codes(args.probe_order_types)
    if not codes:
        return 0
    if not args.confirm_probes:
        raise SmokeRejected(
            "--confirm-probes is required before advanced broker mutations"
        )
    if args.probe_side == "sell" and not args.confirm_existing_position:
        raise SmokeRejected(
            "sell probes require --confirm-existing-position for a real mock holding"
        )
    allowed = _BUY_PROBE_CODES if args.probe_side == "buy" else _SELL_PROBE_CODES
    invalid = set(codes) - allowed
    if invalid:
        raise SmokeRejected(
            f"trde_tp={sorted(invalid)} is not valid for probe_side={args.probe_side}"
        )

    exchange = str(await get_us_exchange_by_symbol(args.symbol)).strip().upper()
    try:
        stex_tp = constants.US_EXCHANGE_TO_STEX[exchange]
    except KeyError as exc:
        raise SmokeRejected(f"unsupported exchange={exchange!r}") from exc

    client = KiwoomMockUsClient.from_app_settings()
    orders = KiwoomUsOrderClient(client)
    exit_code = 0
    for code in codes:
        order_id: str | None = None
        accepted = False
        price, stop_price = _probe_price_args(code, args)
        try:
            try:
                if args.probe_side == "buy":
                    raw = await orders.place_buy_order(
                        symbol=args.symbol,
                        stex_tp=stex_tp,
                        quantity=args.quantity,
                        trde_tp=code,
                        price=price,
                    )
                else:
                    raw = await orders.place_sell_order(
                        symbol=args.symbol,
                        stex_tp=stex_tp,
                        quantity=args.quantity,
                        trde_tp=code,
                        price=price,
                        stop_price=stop_price,
                    )
            except Exception as exc:  # noqa: BLE001 - record unsupported evidence
                _emit(
                    {
                        "step": "probe_order_type",
                        "trde_tp": code,
                        "accepted": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            try:
                accepted = int(raw.get("return_code", -1)) == 0
            except (TypeError, ValueError):
                accepted = False
            order_id = extract_order_id(raw)
            _emit(
                {
                    "step": "probe_order_type",
                    "trde_tp": code,
                    "accepted": accepted,
                    "broker_response": raw,
                }
            )
            if accepted and order_id is None:
                exit_code = 2
                _emit(
                    {
                        "step": "cleanup_required",
                        "trde_tp": code,
                        "reason": "accepted probe returned no exact nine-digit order id",
                    }
                )
        finally:
            if accepted and order_id is not None:
                try:
                    cancelled = await orders.cancel_order(
                        original_order_no=order_id,
                        symbol=args.symbol,
                        stex_tp=stex_tp,
                    )
                    _emit(
                        {
                            "step": "probe_cancel",
                            "trde_tp": code,
                            "order_id": order_id,
                            "broker_response": cancelled,
                        }
                    )
                    try:
                        cancelled_ok = int(cancelled.get("return_code", -1)) == 0
                    except (TypeError, ValueError):
                        cancelled_ok = False
                    if not cancelled_ok:
                        exit_code = 2
                except Exception as exc:  # noqa: BLE001 - cleanup must fail the smoke
                    exit_code = 2
                    _emit(
                        {
                            "step": "cleanup_required",
                            "trde_tp": code,
                            "order_id": order_id,
                            "reason": f"cancel raised {type(exc).__name__}: {exc}",
                        }
                    )
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kiwoom US mock smoke (ROB-867)")
    parser.add_argument(
        "--mode", required=True, choices=["preflight", "preview", "full", "probe"]
    )
    parser.add_argument("--symbol")
    parser.add_argument("--side", choices=["buy", "sell"], default="buy")
    parser.add_argument("--quantity", type=int)
    parser.add_argument("--price", type=float)
    parser.add_argument("--new-price", type=float)
    parser.add_argument("--trde-tp", default="00")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--probe-order-types")
    parser.add_argument("--probe-side", choices=["buy", "sell"], default="buy")
    parser.add_argument("--stop-price", type=float)
    parser.add_argument("--confirm-probes", action="store_true")
    parser.add_argument("--confirm-existing-position", action="store_true")
    return parser


async def _amain(args: argparse.Namespace) -> int:
    if args.mode == "preflight":
        if args.probe_order_types:
            raise SmokeRejected(
                "preflight is read-only; broker order-type probes moved to --mode probe"
            )
        preflight = await run_preflight()
        _emit(preflight)
        return 0 if preflight.get("ok") else 2
    if args.mode == "probe":
        if not args.symbol or not args.quantity:
            raise SmokeRejected("probe mode requires --symbol and --quantity")
        if not args.probe_order_types:
            raise SmokeRejected("probe mode requires --probe-order-types")
        preflight = await run_preflight()
        _emit(preflight)
        if not preflight.get("ok"):
            return 2
        return await run_probe(args)
    if not args.symbol or not args.quantity or args.quantity <= 0:
        raise SmokeRejected("symbol and positive quantity are required")
    if args.trde_tp == "00" and args.price is None:
        raise SmokeRejected("limit order requires --price")
    if args.mode == "preview":
        preview = await run_preview(args)
        _emit({"step": "preview", **preview})
        return 0 if preview.get("success") is True else 2
    return await run_full(args)


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
