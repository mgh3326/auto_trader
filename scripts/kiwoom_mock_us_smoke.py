"""Operator-safe Kiwoom US mock lifecycle smoke (ROB-867)."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.config import validate_kiwoom_mock_us_config
from app.mcp_server.tooling import orders_kiwoom_us_variants as us_variants
from app.mcp_server.tooling.orders_kiwoom_shared import (
    derive_broker_success,
    finalize_place_broker_response,
)
from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.normalization import redact_broker_response
from app.services.brokers.kiwoom.us_account import KiwoomUsAccountClient
from app.services.brokers.kiwoom.us_client import KiwoomMockUsClient
from app.services.brokers.kiwoom.us_orders import KiwoomUsOrderClient
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

# Bounded-digits acceptance: the guide documents nine digits, but the live
# mock shape is unverified and cancel must never be skipped over a width
# mismatch. The actual observed length is emitted as evidence per accept.
_ORDER_ID_RE = re.compile(r"^[0-9]{1,18}$")
_PROBE_CODES = frozenset({"26", "27", "30", "33", "34", "35"})
_BUY_PROBE_CODES = frozenset({"26", "27", "30"})
_SELL_PROBE_CODES = frozenset({"33", "34", "35"})
_ORDER_ID_FIELDS = frozenset({"ord_no", "order_no", "orig_ord_no", "orgn_odno", "odno"})
_PAGE_CAP = 5
_CLEANUP_TIMEOUT = 8.0
_POLL_INTERVAL = 1.0


class SmokeRejected(RuntimeError):
    """Raised when operator input violates a smoke safety boundary."""


@dataclass(frozen=True)
class CleanupProof:
    ok: bool
    state: str
    reason: str
    position_delta: dict[str, dict[str, str]]
    order_states: dict[str, str]


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
        raw = payload.get(key)
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        if _ORDER_ID_RE.fullmatch(value):
            return value
    broker_response = payload.get("broker_response")
    if isinstance(broker_response, dict):
        return extract_order_id(broker_response)
    return None


def _extract_unique_mutation_order_id(payload: dict[str, Any]) -> str | None:
    """Return one unambiguous broker-issued ID, excluding request echoes."""

    broker_payload = payload.get("broker_response")
    evidence = broker_payload if isinstance(broker_payload, dict) else payload
    order_ids: set[str] = set()
    for key in ("ord_no", "order_no"):
        if key not in evidence:
            continue
        raw = evidence.get(key)
        if not isinstance(raw, str):
            return None
        value = raw.strip()
        if not _ORDER_ID_RE.fullmatch(value):
            return None
        order_ids.add(value)
    if len(order_ids) != 1:
        return None
    return next(iter(order_ids))


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
    if not _ORDER_ID_RE.fullmatch(text):
        return None
    return text


def _payload_contains_order_id(value: Any, order_id: str) -> bool:
    if isinstance(value, dict):
        target = _normalize_digits(order_id)
        for key in _ORDER_ID_FIELDS:
            if key not in value:
                continue
            raw = value[key]
            if not isinstance(raw, str):
                continue
            candidate = _normalize_digits(raw)
            if target is not None and candidate == target:
                return True
        return any(
            _payload_contains_order_id(item, order_id)
            for item in value.values()
            if isinstance(item, (dict, list))
        )
    if isinstance(value, list):
        return any(_payload_contains_order_id(item, order_id) for item in value)
    return False


def _broker_payload(response: dict[str, Any]) -> dict[str, Any]:
    nested = response.get("broker_response")
    return nested if isinstance(nested, dict) else response


def _response_succeeded(response: dict[str, Any]) -> bool:
    if "success" in response and response.get("success") is not True:
        return False
    payload = _broker_payload(response)
    if "return_code" in payload:
        return derive_broker_success(payload)
    return False


async def _collect_pages(
    reader: Callable[..., Awaitable[dict[str, Any]]], *, page_cap: int = _PAGE_CAP
) -> list[dict[str, Any]]:
    if page_cap <= 0:
        raise SmokeRejected("pagination page cap must be positive")
    rows: list[dict[str, Any]] = []
    next_key: str | None = None
    seen_tokens: set[str] = set()
    for page_number in range(1, page_cap + 1):
        try:
            response = await reader(
                cont_yn="Y" if next_key is not None else None,
                next_key=next_key,
            )
        except SmokeRejected:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize provider read failures
            raise SmokeRejected(
                f"broker page query failed: {type(exc).__name__}"
            ) from exc
        payload = _broker_payload(response)
        if not derive_broker_success(payload):
            raise SmokeRejected("broker page query did not strictly succeed")
        page_rows = payload.get("result_list")
        if not isinstance(page_rows, list) or not all(
            isinstance(row, dict) for row in page_rows
        ):
            raise SmokeRejected("malformed broker result_list")
        rows.extend(page_rows)

        continuation = payload.get("continuation")
        if not isinstance(continuation, dict):
            raise SmokeRejected("malformed continuation metadata")
        cont_yn = continuation.get("cont_yn")
        token = continuation.get("next_key")
        if cont_yn in {"", "N"}:
            if token not in {None, ""}:
                raise SmokeRejected("malformed terminal continuation token")
            return rows
        if cont_yn != "Y" or not isinstance(token, str) or not token.strip():
            raise SmokeRejected("malformed continuation metadata")
        if token in seen_tokens:
            raise SmokeRejected("repeated continuation token")
        if page_number == page_cap:
            raise SmokeRejected("pagination page cap exceeded")
        seen_tokens.add(token)
        next_key = token
    raise SmokeRejected("pagination page cap exceeded")


def _non_negative_decimal(row: dict[str, Any], key: str) -> Decimal | None:
    raw = row.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    return value


def _target_rows(rows: list[dict[str, Any]], order_id: str) -> list[dict[str, Any]]:
    return [row for row in rows if _payload_contains_order_id(row, order_id)]


def _classify_target(
    open_rows: list[dict[str, Any]],
    today_rows: list[dict[str, Any]],
    order_id: str,
) -> str:
    rows = _target_rows([*open_rows, *today_rows], order_id)
    if not rows:
        return "unknown"

    states: set[str] = set()
    for row in rows:
        filled = _non_negative_decimal(row, "cntr_qty")
        remaining = _non_negative_decimal(row, "ord_remnq")
        if filled is None or remaining is None:
            states.add("unknown")
            continue
        if filled > 0 and remaining > 0:
            states.add("partial")
            continue
        if filled > 0 and remaining == 0:
            states.add("filled")
            continue

        status = str(row.get("ord_stat") or "").strip().casefold()
        is_cancel_row = str(row.get("ord_cntr_tp") or "").strip() == "12"
        if is_cancel_row:
            states.add("cancelled" if remaining == 0 else "cancel_pending")
        elif status in {"cancelled", "canceled", "취소", "취소완료"}:
            states.add("cancelled" if remaining == 0 else "cancel_pending")
        elif status in {"rejected", "거부"}:
            states.add("rejected")
        elif remaining > 0:
            states.add("open")
        else:
            states.add("unknown")

    for state in ("partial", "filled", "cancel_pending"):
        if state in states:
            return state
    if "unknown" in states or len(states) != 1:
        return "unknown"
    return next(iter(states))


async def _position_snapshot(
    reader: Callable[..., Awaitable[dict[str, Any]]], *, page_cap: int = _PAGE_CAP
) -> dict[str, Decimal]:
    snapshot: dict[str, Decimal] = {}
    for row in await _collect_pages(reader, page_cap=page_cap):
        symbol = str(row.get("stk_cd") or "").strip().upper()
        quantity = _non_negative_decimal(row, "poss_qty")
        if not symbol or quantity is None:
            raise SmokeRejected("malformed position row")
        snapshot[symbol] = snapshot.get(symbol, Decimal("0")) + quantity
    return snapshot


def _position_delta(
    baseline: dict[str, Decimal], current: dict[str, Decimal]
) -> dict[str, dict[str, str]]:
    delta: dict[str, dict[str, str]] = {}
    for symbol in sorted(set(baseline) | set(current)):
        before = baseline.get(symbol, Decimal("0"))
        after = current.get(symbol, Decimal("0"))
        if before != after:
            delta[symbol] = {"before": str(before), "after": str(after)}
    return delta


async def _prove_cleanup(
    history_reader: Callable[..., Awaitable[dict[str, Any]]],
    positions_reader: Callable[..., Awaitable[dict[str, Any]]],
    *,
    symbol: str,
    order_id: str,
    baseline: dict[str, Decimal],
    related_order_ids: tuple[str, ...] = (),
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    timeout: float = _CLEANUP_TIMEOUT,
    poll_interval: float = _POLL_INTERVAL,
    page_cap: int = _PAGE_CAP,
) -> CleanupProof:
    del symbol  # The injected readers are already scoped to the requested symbol.
    tracked_order_ids = tuple(dict.fromkeys((order_id, *related_order_ids)))
    started = clock()
    last_state = "unknown"
    last_order_states = dict.fromkeys(tracked_order_ids, "unknown")
    while True:
        try:
            open_rows = await _collect_pages(
                lambda **cursor: history_reader(scope="open", **cursor),
                page_cap=page_cap,
            )
            today_rows = await _collect_pages(
                lambda **cursor: history_reader(scope="today", **cursor),
                page_cap=page_cap,
            )
            current = await _position_snapshot(positions_reader, page_cap=page_cap)
        except SmokeRejected as exc:
            return CleanupProof(False, "unknown", str(exc), {}, last_order_states)

        last_order_states = {
            tracked_id: _classify_target(open_rows, today_rows, tracked_id)
            for tracked_id in tracked_order_ids
        }
        unique_states = set(last_order_states.values())
        last_state = next(iter(unique_states)) if len(unique_states) == 1 else "mixed"
        delta = _position_delta(baseline, current)
        if delta:
            return CleanupProof(
                False,
                last_state,
                "unexpected position delta",
                delta,
                last_order_states,
            )
        if unique_states & {"partial", "filled"}:
            return CleanupProof(
                False,
                last_state,
                "unexpected fill evidence",
                {},
                last_order_states,
            )
        if "unknown" in unique_states:
            return CleanupProof(
                False,
                last_state,
                "unknown target order state",
                {},
                last_order_states,
            )
        if unique_states <= {"cancelled", "rejected"}:
            return CleanupProof(
                True, last_state, "cleanup proven", {}, last_order_states
            )
        if clock() - started >= timeout:
            return CleanupProof(
                False,
                last_state,
                "cleanup reconciliation timed out",
                {},
                last_order_states,
            )
        await sleep(poll_interval)


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
            if not derive_broker_success(raw):
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


async def run_full(
    args: argparse.Namespace,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    cleanup_timeout: float = _CLEANUP_TIMEOUT,
    poll_interval: float = _POLL_INTERVAL,
) -> int:
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

    async def history_reader(
        *, scope: str, cont_yn: str | None = None, next_key: str | None = None
    ) -> dict[str, Any]:
        return await tools["kiwoom_mock_us_get_order_history"](
            scope=scope,
            symbol=args.symbol,
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def positions_reader(
        *, cont_yn: str | None = None, next_key: str | None = None
    ) -> dict[str, Any]:
        return await tools["kiwoom_mock_us_get_positions"](
            symbol=args.symbol,
            cont_yn=cont_yn,
            next_key=next_key,
        )

    try:
        baseline = await _position_snapshot(positions_reader)
    except SmokeRejected as exc:
        _emit({"step": "baseline_failed", "reason": str(exc)})
        return 2
    _emit(
        {
            "step": "positions_baseline",
            "positions": {key: str(value) for key, value in baseline.items()},
        }
    )

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
    if not _response_succeeded(placed):
        if placed.get("status") in {"accepted_untracked", "acceptance_uncertain"}:
            _emit(
                {
                    "step": "cleanup_required",
                    "reason": (
                        "broker acceptance is not safely trackable; "
                        "do not retry; reconcile in broker UI"
                    ),
                }
            )
        return 2

    order_id = extract_order_id(placed)
    if order_id is None:
        history = await history_reader(scope="open")
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
            "within_bounded_1_18_digits": True,
        }
    )

    lifecycle_order_ids = [order_id]
    exit_code = 0
    try:
        try:
            open_rows = await _collect_pages(
                lambda **cursor: history_reader(scope="open", **cursor)
            )
            today_rows = await _collect_pages(
                lambda **cursor: history_reader(scope="today", **cursor)
            )
            placed_state = _classify_target(open_rows, today_rows, order_id)
            _emit({"step": "history_after_place", "state": placed_state})
        except SmokeRejected as exc:
            placed_state = "unknown"
            exit_code = 2
            _emit(
                {
                    "step": "history_after_place",
                    "state": placed_state,
                    "reason": str(exc),
                }
            )
        if placed_state in {"unknown", "partial", "filled"}:
            exit_code = 2
            _emit(
                {
                    "step": "cleanup_required",
                    "order_id": order_id,
                    "reason": f"unsafe post-place state={placed_state}",
                }
            )

        if args.new_price is not None and placed_state == "open" and exit_code == 0:
            modified = await tools["kiwoom_mock_us_modify_order"](
                order_id=order_id,
                symbol=args.symbol,
                new_price=args.new_price,
                dry_run=False,
                confirm=True,
            )
            _emit({"step": "modify_confirmed", **modified})
            if not _response_succeeded(modified):
                exit_code = 2
            elif modified_id := _extract_unique_mutation_order_id(modified):
                if modified_id not in lifecycle_order_ids:
                    lifecycle_order_ids.append(modified_id)
                order_id = modified_id
            else:
                exit_code = 2
                _emit(
                    {
                        "step": "cleanup_required",
                        "order_id": order_id,
                        "reason": (
                            "modify succeeded without one unambiguous broker-issued "
                            "order id; do not retry; reconcile in broker UI"
                        ),
                    }
                )
    finally:
        try:
            cancelled = await tools["kiwoom_mock_us_cancel_order"](
                order_id=order_id,
                symbol=args.symbol,
                dry_run=False,
                confirm=True,
            )
            _emit({"step": "cancel_confirmed", **cancelled})
            if not _response_succeeded(cancelled):
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

    proof = await _prove_cleanup(
        history_reader,
        positions_reader,
        symbol=args.symbol,
        order_id=lifecycle_order_ids[0],
        related_order_ids=tuple(lifecycle_order_ids[1:]),
        baseline=baseline,
        clock=clock,
        sleep=sleep,
        timeout=cleanup_timeout,
        poll_interval=poll_interval,
    )
    _emit(
        {
            "step": "final_reconciliation",
            "ok": proof.ok,
            "state": proof.state,
            "order_states": proof.order_states,
            "reason": proof.reason,
            "position_delta": proof.position_delta,
        }
    )
    if not proof.ok:
        exit_code = 2
        _emit(
            {
                "step": "cleanup_required",
                "order_id": order_id,
                "unresolved_order_ids": [
                    tracked_id
                    for tracked_id, state in proof.order_states.items()
                    if state not in {"cancelled", "rejected"}
                ],
                "reason": (
                    f"{proof.reason}; inspect broker history/positions and manually "
                    "cancel or unwind without retrying the place"
                ),
            }
        )
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


async def run_probe(
    args: argparse.Namespace,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    cleanup_timeout: float = _CLEANUP_TIMEOUT,
    poll_interval: float = _POLL_INTERVAL,
) -> int:
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
    account = KiwoomUsAccountClient(client)

    async def history_reader(
        *, scope: str, cont_yn: str | None = None, next_key: str | None = None
    ) -> dict[str, Any]:
        method = (
            account.get_open_orders if scope == "open" else account.get_today_orders
        )
        return await method(
            stex_tp=stex_tp,
            symbol=args.symbol,
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def positions_reader(
        *, cont_yn: str | None = None, next_key: str | None = None
    ) -> dict[str, Any]:
        return await account.get_positions(
            stex_tp=stex_tp,
            symbol=args.symbol,
            cont_yn=cont_yn,
            next_key=next_key,
        )

    exit_code = 0
    for code in codes:
        order_id: str | None = None
        accepted = False
        try:
            baseline = await _position_snapshot(positions_reader)
        except SmokeRejected as exc:
            exit_code = 2
            _emit(
                {
                    "step": "probe_baseline_failed",
                    "trde_tp": code,
                    "reason": str(exc),
                }
            )
            return exit_code
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
                exit_code = 2
                _emit(
                    {
                        "step": "probe_order_type",
                        "trde_tp": code,
                        "accepted": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                _emit(
                    {
                        "step": "cleanup_required",
                        "trde_tp": code,
                        "reason": (
                            "probe place raised with an uncertain send outcome; "
                            "do not retry; reconcile in broker UI"
                        ),
                    }
                )
                return 2
            accepted = derive_broker_success(raw)
            acceptance = finalize_place_broker_response({}, raw)
            order_id = acceptance.get("order_id")
            if not isinstance(order_id, str):
                order_id = None
            _emit(
                {
                    "step": "probe_order_type",
                    "trde_tp": code,
                    "accepted": accepted,
                    "broker_response": raw,
                }
            )
            if acceptance.get("status") == "accepted_untracked":
                exit_code = 2
                _emit(
                    {
                        "step": "cleanup_required",
                        "trde_tp": code,
                        "reason": (
                            "accepted probe returned no bounded 1-18 digit order id; "
                            "do not retry; reconcile in broker UI"
                        ),
                    }
                )
            elif acceptance.get("status") == "acceptance_uncertain":
                exit_code = 2
                _emit(
                    {
                        "step": "cleanup_required",
                        "trde_tp": code,
                        "reason": (
                            "probe acceptance is uncertain; do not retry; "
                            "reconcile in broker UI"
                        ),
                    }
                )
            elif accepted and order_id is not None:
                try:
                    open_rows = await _collect_pages(
                        lambda **cursor: history_reader(scope="open", **cursor)
                    )
                    today_rows = await _collect_pages(
                        lambda **cursor: history_reader(scope="today", **cursor)
                    )
                    placed_state = _classify_target(open_rows, today_rows, order_id)
                except SmokeRejected:
                    placed_state = "unknown"
                if placed_state in {"unknown", "partial", "filled"}:
                    exit_code = 2
                    _emit(
                        {
                            "step": "cleanup_required",
                            "trde_tp": code,
                            "order_id": order_id,
                            "reason": f"unsafe post-place state={placed_state}",
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
                    cancelled_ok = derive_broker_success(cancelled)
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
                proof = await _prove_cleanup(
                    history_reader,
                    positions_reader,
                    symbol=args.symbol,
                    order_id=order_id,
                    baseline=baseline,
                    clock=clock,
                    sleep=sleep,
                    timeout=cleanup_timeout,
                    poll_interval=poll_interval,
                )
                _emit(
                    {
                        "step": "probe_final_reconciliation",
                        "trde_tp": code,
                        "order_id": order_id,
                        "ok": proof.ok,
                        "state": proof.state,
                        "order_states": proof.order_states,
                        "reason": proof.reason,
                        "position_delta": proof.position_delta,
                    }
                )
                if not proof.ok:
                    exit_code = 2
                    _emit(
                        {
                            "step": "cleanup_required",
                            "trde_tp": code,
                            "order_id": order_id,
                            "reason": (
                                f"{proof.reason}; inspect broker history/positions "
                                "and manually cancel or unwind"
                            ),
                        }
                    )
        if exit_code != 0:
            return exit_code
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
