# scripts/kiwoom_mock_smoke.py
"""Operator-safe Kiwoom mock-investment order smoke (ROB-319, ROB-898).

Default-disabled. KRX-only. Mock host only (enforced in KiwoomMockClient's
host allowlist). Never prints secret values — only presence/missing of the
required env keys.

Each broker mutation requires an explicit ``--confirm``. The buy-limit price is
operator-approved via ``--price`` and floored to the KRX tick (no new quote
engine — reference an existing KIS quote out of band to pick a conservative,
non-marketable price). Cancel is wired (ROB-319), so ``full`` mode always
attempts to cancel any order it opened (finally-block) and reconciles, rather
than stranding a real mock order.

ROB-898 adds ``--mode contract``: a **read-only** sweep that calls the four
account-read endpoints (kt00018, kt00001, kt00010, kt00009) via the same MCP
tool path used at runtime. It performs zero broker mutations, never retries
on error, and treats any non-zero ``return_code`` (including capability-refusal
``20``) as a contract failure — never a silent success.

Usage:
    uv run python -m scripts.kiwoom_mock_smoke --mode preflight
    uv run python -m scripts.kiwoom_mock_smoke --mode preview \
        --symbol 005930 --price 50000 --quantity 1
    # Real mock lifecycle (submit -> history -> modify? -> cancel -> reconcile):
    uv run python -m scripts.kiwoom_mock_smoke --mode full \
        --symbol 005930 --price 50000 --quantity 1 \
        --new-price 49900 --new-quantity 1 --confirm
    # Read-only account-read contract sweep (ROB-898, Phase A):
    uv run python -m scripts.kiwoom_mock_smoke --mode contract

See docs/runbooks/kiwoom-mock-smoke.md for the full procedure and safety notes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import settings, validate_kiwoom_mock_config
from app.mcp_server.tick_size import get_tick_size_kr
from app.mcp_server.tooling import orders_kiwoom_variants as kvar
from app.services.brokers.kiwoom import constants as kw_constants

KRX = "KRX"

# ---------------------------------------------------------------------------
# ROB-898 — Contract sweep constants
# ---------------------------------------------------------------------------

#: MCP tool names that perform broker mutations. The contract sweep must never
#: call any of these — the sweep is strictly read-only.
_CONTRACT_MUTATION_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "kiwoom_mock_place_order",
        "kiwoom_mock_modify_order",
        "kiwoom_mock_cancel_order",
    }
)

#: Kiwoom return_code that signals mock capability refusal. Must never be
#: treated as success (ROB-898 safety rule).
_CAPABILITY_REFUSAL_RETURN_CODE = "20"

#: KST timezone for timestamp emission.
_KST = timezone(timedelta(hours=9))

#: Sensitive substrings that must never appear in emitted output.
_SENSITIVE_VALUE_PATTERNS: tuple[str, ...] = (
    "authorization",
    "bearer",
    "token",
    "secret",
    "password",
    "app_key",
    "app_secret",
    "api_key",
    "api_secret",
    "account_no",
    "account_number",
    "acct_no",
)


class SmokeRejected(RuntimeError):
    """Raised when operator inputs violate a smoke safety boundary."""


class _Recorder:
    """Minimal MCP shim that captures registered tool callables."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *, name: str, description: str = ""):  # noqa: ARG002
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def ensure_krx(exchange: str) -> str:
    value = (exchange or KRX).strip().upper()
    if value != KRX:
        raise SmokeRejected(f"Kiwoom mock smoke is KRX-only; got {exchange!r}")
    return value


def tick_aligned_price(price: int) -> int:
    """Floor an operator-approved price to the KRX tick (buy-side rounding)."""

    tick = get_tick_size_kr(price)
    return (int(price) // tick) * tick


def extract_order_id(payload: dict[str, Any]) -> str | None:
    for key in ("ord_no", "order_no"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _tools() -> dict[str, Any]:
    recorder = _Recorder()
    kvar.register(recorder)
    return recorder.tools


def _emit(payload: dict[str, Any]) -> None:
    # Mock-only payloads; no secrets ever flow through these responses.
    print(json.dumps(payload, ensure_ascii=False, default=str))


async def run_preflight() -> dict[str, Any]:
    missing = validate_kiwoom_mock_config()
    return {
        "step": "preflight",
        "ok": not missing,
        "missing_env_keys": missing,  # names only, never values
    }


async def run_preview(symbol: str, price: int, quantity: int) -> dict[str, Any]:
    tools = _tools()
    return await tools["kiwoom_mock_preview_order"](
        symbol=symbol, side="buy", quantity=quantity, price=price
    )


async def run_full(args: argparse.Namespace) -> int:
    """Submit -> history -> modify? -> cancel -> reconcile, fail-closed.

    Cancels in a finally block so a real mock order is never left open.
    """

    tools = _tools()
    price = tick_aligned_price(args.price)
    _emit({"step": "price_tick_aligned", "requested": args.price, "used": price})

    preview = await run_preview(args.symbol, price, args.quantity)
    _emit({"step": "preview", **preview})

    dry = await tools["kiwoom_mock_place_order"](
        symbol=args.symbol,
        side="buy",
        quantity=args.quantity,
        price=price,
        dry_run=True,
    )
    _emit({"step": "place_dry_run", **dry})

    if not dry.get("success"):
        _emit(
            {
                "step": "abort",
                "reason": "place_order dry-run failed; not proceeding to confirmed "
                "broker mutation",
            }
        )
        return 2

    if not args.confirm:
        _emit({"step": "stop", "reason": "no --confirm; stopped after dry-run"})
        return 0

    placed = await tools["kiwoom_mock_place_order"](
        symbol=args.symbol,
        side="buy",
        quantity=args.quantity,
        price=price,
        dry_run=False,
        confirm=True,
    )
    _emit({"step": "place_confirmed", **placed})
    if not placed.get("success"):
        _emit({"step": "abort", "reason": "place_order did not succeed"})
        return 2

    order_id = extract_order_id(placed)
    if not order_id:
        # Order may exist but we could not parse its id — reconcile and surface
        # loudly rather than pretend success.
        history = await tools["kiwoom_mock_get_order_history"]()
        _emit({"step": "reconcile_no_order_id", **history})
        _emit(
            {
                "step": "anomaly",
                "reason": "place succeeded but order id unparsed; check history "
                "and cancel manually if an order is open",
            }
        )
        return 2

    exit_code = 0
    try:
        history = await tools["kiwoom_mock_get_order_history"]()
        _emit({"step": "history_after_place", **history})

        if args.new_price is not None and args.new_quantity is not None:
            modified = await tools["kiwoom_mock_modify_order"](
                order_id=order_id,
                symbol=args.symbol,
                new_price=tick_aligned_price(args.new_price),
                new_quantity=args.new_quantity,
                dry_run=False,
                confirm=True,
            )
            _emit({"step": "modify_confirmed", **modified})
            # A modify may reissue the order number — track it for cancel.
            new_id = extract_order_id(modified)
            if modified.get("success") and new_id:
                order_id = new_id
        else:
            _emit({"step": "modify_skipped", "reason": "no --new-price/--new-quantity"})
    finally:
        cancelled = await tools["kiwoom_mock_cancel_order"](
            order_id=order_id,
            symbol=args.symbol,
            cancel_quantity=args.quantity,
            dry_run=False,
            confirm=True,
        )
        _emit({"step": "cancel_confirmed", "order_id": order_id, **cancelled})
        if not cancelled.get("success"):
            exit_code = 2
            _emit(
                {
                    "step": "cleanup_required",
                    "order_id": order_id,
                    "reason": "cancel did not succeed; verify and cancel manually",
                }
            )

    final_history = await tools["kiwoom_mock_get_order_history"]()
    _emit({"step": "final_reconcile_history", **final_history})
    positions = await tools["kiwoom_mock_get_positions"]()
    _emit({"step": "final_reconcile_positions", **positions})
    return exit_code


# ---------------------------------------------------------------------------
# ROB-898 — Read-only contract sweep (Phase A)
# ---------------------------------------------------------------------------


def _kst_now_iso() -> str:
    return datetime.now(_KST).isoformat(timespec="seconds")


def _get_deploy_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        sha = result.stdout.strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"


def _sanitize_return_code(rc: Any) -> str | int | None:
    if rc is None:
        return None
    rc_str = str(rc).strip()
    if rc_str.isdigit():
        return int(rc_str)
    return rc_str[:20] if rc_str else None


def _sanitize_return_msg(msg: Any) -> str:
    if msg is None:
        return ""
    text = str(msg).strip()
    lower = text.lower()
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        if pattern in lower:
            return "[SANITIZED]"
    return text[:200]


def _verify_mock_host() -> str | None:
    base_url = str(getattr(settings, "kiwoom_mock_base_url", "") or "")
    if not base_url:
        return "kiwoom_mock_base_url is empty"
    if kw_constants.LIVE_BASE_URL in base_url:
        return "live host detected"
    if kw_constants.MOCK_BASE_URL not in base_url:
        return "unrecognized host"
    return None


def _make_mutation_guard(tool_name: str) -> Any:
    async def _guard(**_kwargs: Any) -> dict[str, Any]:
        raise SmokeRejected(
            f"contract sweep is strictly read-only; "
            f"mutation tool {tool_name!r} must not be called"
        )

    return _guard


def _read_only_tools(raw_tools: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for name, func in raw_tools.items():
        if name in _CONTRACT_MUTATION_TOOL_NAMES:
            safe[name] = _make_mutation_guard(name)
        else:
            safe[name] = func
    return safe


async def _run_contract_step(
    tools: dict[str, Any],
    tool_name: str,
    expected_api_id: str,
    evidence_kind: str,
    deploy_sha: str,
    tool_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kst_time = _kst_now_iso()
    call_args = tool_args or {}

    try:
        payload = await tools[tool_name](**call_args)
    except Exception as exc:
        step: dict[str, Any] = {
            "step": "contract_step",
            "stage": evidence_kind,
            "tool": tool_name,
            "expected_api_id": expected_api_id,
            "kst_time": kst_time,
            "deploy_sha": deploy_sha,
            "pass": False,
            "fail_reason": "exception",
            "error_type": type(exc).__name__,
        }
        _emit(step)
        return step

    if not isinstance(payload, dict):
        step = {
            "step": "contract_step",
            "stage": evidence_kind,
            "tool": tool_name,
            "expected_api_id": expected_api_id,
            "kst_time": kst_time,
            "deploy_sha": deploy_sha,
            "pass": False,
            "fail_reason": "malformed_response",
        }
        _emit(step)
        return step

    provenance = payload.get("provenance") or {}
    actual_api_id = str(provenance.get("api_id", ""))
    broker_response = payload.get("broker_response") or {}
    raw_rc = broker_response.get("return_code")
    rc_sanitized = _sanitize_return_code(raw_rc)
    msg_sanitized = _sanitize_return_msg(broker_response.get("return_msg"))
    tool_success = bool(payload.get("success"))

    # Capability refusal (return_code=20) is NEVER success.
    if str(raw_rc).strip() == _CAPABILITY_REFUSAL_RETURN_CODE:
        tool_success = False

    step_pass = tool_success and actual_api_id == expected_api_id

    step = {
        "step": "contract_step",
        "stage": evidence_kind,
        "tool": tool_name,
        "expected_api_id": expected_api_id,
        "actual_api_id": actual_api_id,
        "api_id_match": actual_api_id == expected_api_id,
        "kst_time": kst_time,
        "deploy_sha": deploy_sha,
        "return_code": rc_sanitized,
        "return_msg": msg_sanitized,
        "evidence_kind": evidence_kind,
        "provenance": {
            "broker": provenance.get("broker"),
            "environment": provenance.get("environment"),
            "account_mode": provenance.get("account_mode"),
            "host": provenance.get("host"),
            "api_id": actual_api_id,
        },
        "success": tool_success,
        "pass": step_pass,
    }
    error_code = payload.get("error")
    if error_code:
        step["error_code"] = str(error_code)[:100]
    error_detail = payload.get("error_detail")
    if error_detail:
        step["error_detail"] = _sanitize_return_msg(error_detail)

    _emit(step)
    return step


_CONTRACT_STEPS_SPEC: list[dict[str, Any]] = [
    {
        "stage": "positions",
        "tool": "kiwoom_mock_get_positions",
        "expected_api_id": kw_constants.ACCOUNT_BALANCE_API_ID,
        "evidence_kind": "positions",
        "tool_args": {},
    },
    {
        "stage": "deposit",
        "tool": "kiwoom_mock_get_orderable_cash",
        "expected_api_id": kw_constants.ACCOUNT_DEPOSIT_API_ID,
        "evidence_kind": "deposit",
        "tool_args": {},
    },
    {
        "stage": "orderable_amount",
        "tool": "kiwoom_mock_get_orderable_cash",
        "expected_api_id": kw_constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID,
        "evidence_kind": "orderable_amount",
        "tool_args": {"symbol": "005930", "side": "buy", "price": 50000},
    },
    {
        "stage": "order_history",
        "tool": "kiwoom_mock_get_order_history",
        "expected_api_id": kw_constants.ACCOUNT_ORDER_STATUS_API_ID,
        "evidence_kind": "order_history",
        "tool_args": {},
    },
]


async def run_contract_sweep(args: argparse.Namespace) -> int:
    missing = validate_kiwoom_mock_config()
    if missing:
        _emit(
            {
                "step": "contract_preflight",
                "ok": False,
                "missing_env_keys": missing,
                "kst_time": _kst_now_iso(),
            }
        )
        return 4

    host_error = _verify_mock_host()
    if host_error:
        _emit(
            {
                "step": "contract_preflight",
                "ok": False,
                "error": "mock_host_verification_failed",
                "kst_time": _kst_now_iso(),
            }
        )
        return 2

    deploy_sha = _get_deploy_sha()
    _emit(
        {
            "step": "contract_sweep_start",
            "kst_time": _kst_now_iso(),
            "deploy_sha": deploy_sha,
            "mode": "read_only_contract_sweep",
            "mutations_allowed": False,
            "steps_planned": len(_CONTRACT_STEPS_SPEC),
        }
    )

    tools = _read_only_tools(_tools())

    results: list[dict[str, Any]] = []
    for spec in _CONTRACT_STEPS_SPEC:
        result = await _run_contract_step(
            tools=tools,
            tool_name=spec["tool"],
            expected_api_id=spec["expected_api_id"],
            evidence_kind=spec["evidence_kind"],
            deploy_sha=deploy_sha,
            tool_args=spec.get("tool_args"),
        )
        results.append(result)

    passed_count = sum(1 for r in results if r.get("pass"))
    failed_count = len(results) - passed_count
    overall_pass = failed_count == 0
    _emit(
        {
            "step": "contract_sweep_summary",
            "kst_time": _kst_now_iso(),
            "deploy_sha": deploy_sha,
            "total_steps": len(results),
            "passed": passed_count,
            "failed": failed_count,
            "overall_pass": overall_pass,
            "failed_stages": [
                r.get("stage", "?") for r in results if not r.get("pass")
            ],
            "mutations_performed": 0,
        }
    )

    return 0 if overall_pass else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Kiwoom mock order smoke (ROB-319, ROB-898)"
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["preflight", "preview", "full", "contract"],
    )
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--price", type=int, default=None)
    parser.add_argument("--quantity", type=int, default=None)
    parser.add_argument("--new-price", type=int, default=None)
    parser.add_argument("--new-quantity", type=int, default=None)
    parser.add_argument(
        "--exchange", default=KRX, help="KRX only; any other value is rejected."
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required for any real broker mutation (full mode submit/modify/cancel).",
    )
    return parser


async def _amain(args: argparse.Namespace) -> int:
    ensure_krx(args.exchange)

    if args.mode == "preflight":
        _emit(await run_preflight())
        return 0

    if args.mode == "contract":
        return await run_contract_sweep(args)

    if not (args.symbol and args.price and args.quantity):
        raise SmokeRejected("symbol, price, quantity are required for this mode")

    if args.mode == "preview":
        _emit(
            {
                "step": "price_tick_aligned",
                "requested": args.price,
                "used": tick_aligned_price(args.price),
            }
        )
        _emit(
            {
                "step": "preview",
                **await run_preview(
                    args.symbol, tick_aligned_price(args.price), args.quantity
                ),
            }
        )
        return 0

    return await run_full(args)


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
