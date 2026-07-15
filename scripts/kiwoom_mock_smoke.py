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
import contextlib
import json
import re
import subprocess
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from app.core.config import settings, validate_kiwoom_mock_config
from app.mcp_server.tick_size import get_tick_size_kr
from app.mcp_server.tooling import orders_kiwoom_variants as kvar
from app.services.brokers.kiwoom import constants as kw_constants

KRX = "KRX"

_CONTRACT_MUTATION_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "kiwoom_mock_place_order",
        "kiwoom_mock_modify_order",
        "kiwoom_mock_cancel_order",
    }
)

_KST = timezone(timedelta(hours=9))

_MOCK_HOSTNAME = "mockapi.kiwoom.com"
_LIVE_HOSTNAME = "api.kiwoom.com"
_PACING_SECONDS = 0.1
_SENSITIVE_DIGIT_RUN = re.compile(r"\d{6,}")
_TOKEN_FORMAT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"bearer\s+\S+", re.IGNORECASE),
    re.compile(r"authorization\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"access(?:[_ -]?token)?\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(
        r"\b[A-Z0-9._-]*(?:bearer|token|authorization)[A-Z0-9._-]*\b", re.IGNORECASE
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9._-]+\.[A-Za-z0-9._-]+\b"),
)
_ACCOUNT_FORMAT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{2,6}(?:-\d{1,6}){1,4}\b"),
    re.compile(
        r"(?:계좌(?:번호)?|계좌\s*no|account(?:\s*_?no)?|acct(?:\s*_?no)?)\s*[:=]?\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b[A-Z0-9._-]*(?:secret|app[_ -]?key|app[_ -]?secret|account[_ -]?no|acct)[A-Z0-9._-]*\b",
        re.IGNORECASE,
    ),
)

_MOCK_PROVENANCE_REQUIRED: dict[str, str] = {
    "broker": "kiwoom",
    "environment": "mock",
    "account_mode": "kiwoom_mock",
    "host": _MOCK_HOSTNAME,
}


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
    print(json.dumps(_sanitize_untrusted(payload), ensure_ascii=False, default=str))


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


def _is_return_code_zero(raw_rc: Any) -> bool:
    if isinstance(raw_rc, bool):
        return False
    if isinstance(raw_rc, int):
        return raw_rc == 0
    if raw_rc is None:
        return False
    return str(raw_rc).strip() == "0"


def _sanitize_return_code(rc: Any) -> str | int | None:
    if rc is None:
        return None
    rc_str = str(rc).strip()
    if rc_str.isdigit():
        return int(rc_str)
    return _sanitize_free_form_text(rc_str) if rc_str else None


def _configured_sensitive_values() -> frozenset[str]:
    values: set[str] = set()
    for attr in (
        "kiwoom_mock_app_key",
        "kiwoom_mock_app_secret",
        "kiwoom_mock_account_no",
    ):
        val = str(getattr(settings, attr, "") or "").strip()
        if val and len(val) >= 4:
            values.add(val)
    return frozenset(values)


def _sanitize_free_form_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    for val in _configured_sensitive_values():
        if val in text:
            return "[SANITIZED]"
    for pattern in _TOKEN_FORMAT_PATTERNS:
        if pattern.search(text):
            return "[SANITIZED]"
    for pattern in _ACCOUNT_FORMAT_PATTERNS:
        if pattern.search(text):
            return "[SANITIZED]"
    if _SENSITIVE_DIGIT_RUN.search(text):
        return "[SANITIZED]"
    return text[:200]


def _sanitize_return_msg(msg: Any) -> str:
    return _sanitize_free_form_text(msg)


def _sanitize_untrusted(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _sanitize_free_form_text(value)
    if isinstance(value, Mapping):
        return {str(key): _sanitize_untrusted(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_untrusted(item) for item in value]
    return _sanitize_free_form_text(value)


def _is_strict_success_flag(value: Any) -> bool:
    return value is True


def _verify_mock_host() -> str | None:
    base_url = str(getattr(settings, "kiwoom_mock_base_url", "") or "")
    if not base_url:
        return "mock_base_url_missing"
    if base_url != base_url.strip():
        return "mock_base_url_whitespace_disallowed"
    if base_url == kw_constants.MOCK_BASE_URL:
        return None
    try:
        parsed = urlparse(base_url)
    except Exception:
        return "mock_base_url_malformed"
    if parsed.scheme != "https":
        return "mock_base_url_scheme_invalid"
    if parsed.username or parsed.password:
        return "mock_base_url_userinfo_disallowed"
    hostname = parsed.hostname or ""
    if not hostname:
        return "mock_base_url_host_missing"
    if hostname.lower() == _LIVE_HOSTNAME:
        return "mock_base_url_live_host_disallowed"
    if hostname != _MOCK_HOSTNAME:
        return "mock_base_url_host_invalid"
    if parsed.port is not None:
        return "mock_base_url_port_disallowed"
    if parsed.path:
        return "mock_base_url_path_disallowed"
    if parsed.query:
        return "mock_base_url_query_disallowed"
    if parsed.fragment:
        return "mock_base_url_fragment_disallowed"
    return "mock_base_url_noncanonical"


def _verify_provenance_mock(provenance: Any) -> bool:
    if not isinstance(provenance, dict):
        return False
    return all(
        provenance.get(key) == expected
        for key, expected in _MOCK_PROVENANCE_REQUIRED.items()
    )


def _sanitize_provenance(provenance: Any) -> dict[str, Any]:
    if not isinstance(provenance, dict):
        return {}
    return {
        "broker": _sanitize_free_form_text(provenance.get("broker")),
        "environment": _sanitize_free_form_text(provenance.get("environment")),
        "account_mode": _sanitize_free_form_text(provenance.get("account_mode")),
        "host": _sanitize_free_form_text(provenance.get("host")),
        "api_id": _sanitize_free_form_text(provenance.get("api_id", "")),
    }


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


@contextlib.contextmanager
def _reused_kiwoom_mock_client() -> Iterator[tuple[int, Any]]:
    from app.services.brokers.kiwoom.client import KiwoomMockClient

    sweep_client = KiwoomMockClient.from_app_settings()
    original_descriptor = KiwoomMockClient.__dict__["from_app_settings"]
    from_settings_calls = 1

    def _reusing_from_settings(cls: type[KiwoomMockClient]) -> KiwoomMockClient:
        nonlocal from_settings_calls
        del cls
        from_settings_calls += 1
        return sweep_client

    KiwoomMockClient.from_app_settings = classmethod(_reusing_from_settings)
    try:
        yield from_settings_calls, lambda: from_settings_calls
    finally:
        KiwoomMockClient.from_app_settings = original_descriptor


async def _run_contract_step(
    tools: dict[str, Any],
    tool_name: str,
    expected_api_id: str,
    evidence_kind: str,
    deploy_sha: str,
    contract_fields: dict[str, Any],
    tool_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kst_time = _kst_now_iso()
    call_args = tool_args or {}

    try:
        payload = await tools[tool_name](**call_args)
    except Exception as exc:
        step = _sanitize_untrusted(
            {
                "step": "contract_step",
                "stage": evidence_kind,
                "tool": tool_name,
                "expected_api_id": expected_api_id,
                "kst_time": kst_time,
                "deploy_sha": deploy_sha,
                "contract_fields": contract_fields,
                "pass": False,
                "fail_reason": "exception",
                "error_type": type(exc).__name__,
                "error_detail": str(exc),
            }
        )
        _emit(step)
        return step

    if not isinstance(payload, dict):
        step = _sanitize_untrusted(
            {
                "step": "contract_step",
                "stage": evidence_kind,
                "tool": tool_name,
                "expected_api_id": expected_api_id,
                "kst_time": kst_time,
                "deploy_sha": deploy_sha,
                "contract_fields": contract_fields,
                "pass": False,
                "fail_reason": "malformed_response",
            }
        )
        _emit(step)
        return step

    provenance_raw = payload.get("provenance")
    provenance = _sanitize_provenance(provenance_raw)
    actual_api_id_raw = (
        provenance_raw.get("api_id", "") if isinstance(provenance_raw, dict) else ""
    )
    actual_api_id = provenance.get("api_id", "")
    broker_response = payload.get("broker_response")
    if not isinstance(broker_response, dict):
        broker_response = {}
    raw_rc = broker_response.get("return_code")
    rc_sanitized = _sanitize_return_code(raw_rc)
    msg_sanitized = _sanitize_return_msg(broker_response.get("return_msg"))
    rc_is_zero = _is_return_code_zero(raw_rc)
    provenance_mock = _verify_provenance_mock(provenance_raw)
    tool_success = _is_strict_success_flag(payload.get("success"))

    step_pass = (
        tool_success
        and rc_is_zero
        and provenance_mock
        and actual_api_id_raw == expected_api_id
    )

    step: dict[str, Any] = {
        "step": "contract_step",
        "stage": evidence_kind,
        "tool": tool_name,
        "expected_api_id": expected_api_id,
        "actual_api_id": actual_api_id,
        "api_id_match": actual_api_id_raw == expected_api_id,
        "kst_time": kst_time,
        "deploy_sha": deploy_sha,
        "contract_fields": contract_fields,
        "return_code": rc_sanitized,
        "return_code_is_zero": rc_is_zero,
        "return_msg": msg_sanitized,
        "evidence_kind": evidence_kind,
        "provenance": provenance,
        "provenance_mock": provenance_mock,
        "success": tool_success,
        "pass": step_pass,
    }
    if not step_pass:
        reasons: list[str] = []
        if not tool_success:
            reasons.append("success_false")
        if not rc_is_zero:
            reasons.append("return_code_nonzero")
        if not provenance_mock:
            reasons.append("provenance_not_mock")
        if actual_api_id_raw != expected_api_id:
            reasons.append("api_id_mismatch")
        step["fail_reasons"] = reasons
    error_code = payload.get("error")
    if error_code:
        step["error_code"] = _sanitize_free_form_text(error_code)
    error_detail = payload.get("error_detail")
    if error_detail:
        step["error_detail"] = _sanitize_free_form_text(error_detail)

    step = _sanitize_untrusted(step)
    _emit(step)
    return step


_CONTRACT_STEPS_SPEC: list[dict[str, Any]] = [
    {
        "stage": "positions",
        "tool": "kiwoom_mock_get_positions",
        "expected_api_id": kw_constants.ACCOUNT_BALANCE_API_ID,
        "evidence_kind": "positions",
        "tool_args": {},
        "contract_fields": {
            "request_body": {"qry_tp": "1", "dmst_stex_tp": "KRX"},
            "response_array": "acnt_evlt_remn_indv_tot",
        },
    },
    {
        "stage": "deposit",
        "tool": "kiwoom_mock_get_orderable_cash",
        "expected_api_id": kw_constants.ACCOUNT_DEPOSIT_API_ID,
        "evidence_kind": "deposit",
        "tool_args": {},
        "contract_fields": {
            "request_body": {"qry_tp": "2"},
            "response_field": "ord_alow_amt",
        },
    },
    {
        "stage": "orderable_amount",
        "tool": "kiwoom_mock_get_orderable_cash",
        "expected_api_id": kw_constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID,
        "evidence_kind": "orderable_amount",
        "tool_args": {"symbol": "005930", "side": "buy", "price": 50000},
        "contract_fields": {
            "request_body": {"stk_cd": "<symbol>", "trde_tp": "1|2", "uv": "<price>"},
            "response_field": "ord_alowa",
        },
    },
    {
        "stage": "order_history",
        "tool": "kiwoom_mock_get_order_history",
        "expected_api_id": kw_constants.ACCOUNT_ORDER_STATUS_API_ID,
        "evidence_kind": "order_history",
        "tool_args": {},
        "contract_fields": {
            "request_body": {"stk_bond_tp": "0"},
            "response_array": "acnt_ord_cntr_prst_array",
        },
    },
]


async def run_contract_sweep(
    args: argparse.Namespace,
    *,
    pacing_fn: Any = None,
) -> int:
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
                "reason": host_error,
                "kst_time": _kst_now_iso(),
            }
        )
        return 2

    deploy_sha = _get_deploy_sha()
    sleep_fn = pacing_fn or asyncio.sleep
    with _reused_kiwoom_mock_client() as (_client_count, get_call_count):
        _emit(
            {
                "step": "contract_sweep_start",
                "kst_time": _kst_now_iso(),
                "deploy_sha": deploy_sha,
                "mode": "read_only_contract_sweep",
                "mutations_allowed": False,
                "steps_planned": len(_CONTRACT_STEPS_SPEC),
                "client_instances_created": 1,
            }
        )

        tools = _read_only_tools(_tools())

        results: list[dict[str, Any]] = []
        pacing_calls = 0
        for i, spec in enumerate(_CONTRACT_STEPS_SPEC):
            if i > 0:
                await sleep_fn(_PACING_SECONDS)
                pacing_calls += 1
            result = await _run_contract_step(
                tools=tools,
                tool_name=spec["tool"],
                expected_api_id=spec["expected_api_id"],
                evidence_kind=spec["evidence_kind"],
                deploy_sha=deploy_sha,
                contract_fields=spec.get("contract_fields", {}),
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
                "client_instances_created": 1,
                "from_app_settings_calls": get_call_count(),
                "pacing_calls": pacing_calls,
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
