#!/usr/bin/env python3
"""Read-only KIS mock US cash-TR probe for ROB-951.

This script is default-disabled and has no order, modify, or cancel code path.
When an operator explicitly enables it, it sends only the two GET inquiry TRs
needed to measure KIS mock support: VTTS3007R and VTTC0869R.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

PROBE_ENABLE_ENV = "KIS_MOCK_US_CASH_PROBE_ENABLED"
REQUIRED_MOCK_ENV: tuple[str, ...] = (
    "KIS_MOCK_APP_KEY",
    "KIS_MOCK_APP_SECRET",
    "KIS_MOCK_ACCOUNT_NO",
)
_READ_ONLY_PACING_SECONDS = 0.25
_REDACTED = "[REDACTED]"
_SENSITIVE_TEXT = re.compile(
    r"(?i)(authorization|bearer|access[_ -]?token|app[_ -]?(?:key|secret)|"
    r"account(?:[_ -]?(?:no|number))?|cano)\\s*[:=]\\s*[^,\\s}]+"
)


@dataclass(frozen=True)
class ProbeTarget:
    key: str
    label: str
    tr_id: str
    path: str

    def params(self, *, cano: str, product_code: str) -> dict[str, str]:
        if self.key == "vtts3007":
            # A non-ordering reference quote only; this endpoint calculates
            # buying power and does not submit an order.
            return {
                "CANO": cano,
                "ACNT_PRDT_CD": product_code,
                "OVRS_EXCG_CD": "NASD",
                "OVRS_ORD_UNPR": "1",
                "ITEM_CD": "AAPL",
            }
        return {
            "CANO": cano,
            "ACNT_PRDT_CD": product_code,
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "WCRC_FRCR_DVSN_CD": "01",
            "FWEX_CTRT_FRCR_DVSN_CD": "01",
        }


TARGETS: dict[str, ProbeTarget] = {
    "vtts3007": ProbeTarget(
        key="vtts3007",
        label="overseas buyable amount",
        tr_id="VTTS3007R",
        path="/uapi/overseas-stock/v1/trading/inquire-psamount",
    ),
    "vttc0869": ProbeTarget(
        key="vttc0869",
        label="integrated margin",
        tr_id="VTTC0869R",
        path="/uapi/domestic-stock/v1/trading/intgr-margin",
    ),
}


class ProbeTransport(Protocol):
    secret_values: tuple[str, ...]

    async def request(self, target: ProbeTarget) -> tuple[int | None, dict[str, Any]]:
        """Dispatch exactly one read-only request and return status plus JSON."""


def probe_enabled(environ: Mapping[str, str] | None = None) -> bool:
    value = (environ or os.environ).get(PROBE_ENABLE_ENV, "")
    return value.strip().lower() == "true"


def missing_mock_credential_names(
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    values = environ or os.environ
    return [name for name in REQUIRED_MOCK_ENV if not values.get(name, "").strip()]


def _account_parts(account_no: str) -> tuple[str, str]:
    compact = account_no.strip().replace("-", "")
    if len(compact) < 3 or not compact.isdigit():
        raise ValueError(
            "KIS_MOCK_ACCOUNT_NO must be digits with an account product code"
        )
    return compact[:-2], compact[-2:]


class KISReadOnlyTransport:
    """Adapter that preserves KISClient token and rate-limit machinery."""

    def __init__(self) -> None:
        from app.services.brokers.kis import constants
        from app.services.brokers.kis.client import KISClient

        self._client = KISClient(is_mock=True)
        self._constants = constants
        self._account_no = str(self._client._settings.kis_account_no or "")
        self.secret_values = tuple(
            value
            for value in (
                self._account_no,
                str(self._client._settings.kis_app_key or ""),
                str(self._client._settings.kis_app_secret or ""),
                str(self._client._settings.kis_access_token or ""),
            )
            if value
        )

    async def request(self, target: ProbeTarget) -> tuple[int | None, dict[str, Any]]:
        import httpx

        expected_tr = (
            self._constants.OVERSEAS_BUYABLE_AMOUNT_TR_MOCK
            if target.key == "vtts3007"
            else self._constants.INTEGRATED_MARGIN_TR_MOCK
        )
        expected_path = (
            self._constants.OVERSEAS_BUYABLE_AMOUNT_URL
            if target.key == "vtts3007"
            else self._constants.INTEGRATED_MARGIN_URL
        )
        if target.tr_id != expected_tr or target.path != expected_path:
            raise RuntimeError(
                "probe target does not match the KIS read-only constants"
            )

        # Deliberately bypasses AccountClient.inquire_integrated_margin(): that
        # production wrapper fail-closes mock mode before token/TR selection.
        await self._client._ensure_token()
        cano, product_code = _account_parts(self._account_no)
        headers = self._client._hdr_base | {
            "authorization": f"Bearer {self._client._settings.kis_access_token}",
            "tr_id": target.tr_id,
        }
        status_code: int | None = None
        original_execute = self._client._execute_http_request

        async def capture_status(*args: Any, **kwargs: Any) -> Any:
            nonlocal status_code
            response = await original_execute(*args, **kwargs)
            status_code = response.status_code
            return response

        self._client._execute_http_request = capture_status
        try:
            try:
                payload = await self._client._request_with_rate_limit(
                    "GET",
                    self._client._kis_url(target.path),
                    headers=headers,
                    params=target.params(cano=cano, product_code=product_code),
                    timeout=10,
                    api_name=f"rob_951_{target.key}_probe",
                    tr_id=target.tr_id,
                )
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                try:
                    body = exc.response.json()
                except ValueError:
                    body = {"msg1": "non-JSON HTTP error response"}
                payload = body if isinstance(body, dict) else {"msg1": str(body)}
        finally:
            self._client._execute_http_request = original_execute
        return status_code, payload


def redact_payload(payload: Any, *, secret_values: tuple[str, ...]) -> Any:
    """Use existing broker-key redaction, then mask known credential echoes."""
    from app.services.brokers.kiwoom.normalization import redact_broker_response

    def scrub(value: Any) -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized_key in {"cano", "acntprdtcd"}:
                    result[str(key)] = _REDACTED
                else:
                    result[str(key)] = scrub(item)
            return result
        if isinstance(value, list | tuple):
            return [scrub(item) for item in value]
        if not isinstance(value, str):
            return value
        redacted = value
        for secret in secret_values:
            if secret:
                redacted = redacted.replace(secret, _REDACTED)
                redacted = redacted.replace(secret.replace("-", ""), _REDACTED)
        return _SENSITIVE_TEXT.sub(_REDACTED, redacted)

    if isinstance(payload, Mapping):
        return scrub(redact_broker_response(dict(payload)))
    return scrub(payload)


def _first_field(payload: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    containers = [payload]
    for key in ("output", "output1", "output2"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
    for name in names:
        for container in containers:
            value = container.get(name)
            if value not in (None, ""):
                return value
    return None


def parse_cash_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Expose parsed field values or null; absence is evidence, not success."""
    fields = {
        "stck_cash_ord_psbl_amt": ("stck_cash_ord_psbl_amt",),
        "usd_ord_psbl_amt": ("usd_ord_psbl_amt",),
        "usd_balance": ("usd_balance",),
        "overseas_orderable_amount": (
            "ovrs_ord_psbl_amt",
            "frcr_ord_psbl_amt",
            "frcr_ord_psbl_amt1",
        ),
    }
    return {
        name: _first_field(payload, candidates) for name, candidates in fields.items()
    }


def evaluate_response(
    http_status: int | None, payload: Mapping[str, Any]
) -> tuple[int, str]:
    if str(payload.get("rt_cd", "")) == "0":
        return 0, "supported"
    if http_status is not None or any(
        key in payload for key in ("rt_cd", "msg_cd", "msg1")
    ):
        return 2, "broker_rejected_or_not_supported"
    return 1, "unclassified_transport_result"


def _emit_result(
    *,
    target: ProbeTarget,
    http_status: int | None,
    payload: dict[str, Any],
    secrets: tuple[str, ...],
) -> tuple[int, str]:
    code, verdict = evaluate_response(http_status, payload)
    evidence = {
        "target": target.key,
        "tr_id": target.tr_id,
        "http_status": http_status,
        "rt_cd": payload.get("rt_cd"),
        "msg_cd": payload.get("msg_cd"),
        "msg1": payload.get("msg1"),
        "parsed_cash_fields": parse_cash_fields(payload),
        "verdict": verdict,
        "raw_response_redacted": redact_payload(payload, secret_values=secrets),
    }
    print(json.dumps(evidence, ensure_ascii=False, default=str))
    return code, verdict


async def run_probe(
    *,
    selected: str,
    transport_factory: Callable[[], ProbeTransport] = KISReadOnlyTransport,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> int:
    if not probe_enabled():
        print(
            f"[probe] disabled: set {PROBE_ENABLE_ENV}=true to permit read-only TR calls"
        )
        return 0

    missing = missing_mock_credential_names()
    if missing:
        print(
            f"[probe] missing KIS mock credentials: {', '.join(missing)} (names only)"
        )
        return 3

    if selected == "preflight":
        print("[probe] preflight only: no --tr selected; no broker request sent")
        return 0

    keys = tuple(TARGETS) if selected == "both" else (selected,)
    transport = transport_factory()
    exit_code = 0
    for index, key in enumerate(keys):
        if index:
            await sleep(_READ_ONLY_PACING_SECONDS)
        target = TARGETS[key]
        try:
            status, payload = await transport.request(target)
        except Exception as exc:  # noqa: BLE001 - diagnostic script must not pretend success
            print(
                json.dumps(
                    {
                        "target": target.key,
                        "tr_id": target.tr_id,
                        "http_status": None,
                        "error_type": type(exc).__name__,
                        "error": redact_payload(
                            str(exc), secret_values=transport.secret_values
                        ),
                        "verdict": "transport_or_setup_error",
                    },
                    ensure_ascii=False,
                )
            )
            exit_code = max(exit_code, 1)
            continue
        code, _ = _emit_result(
            target=target,
            http_status=status,
            payload=payload,
            secrets=transport.secret_values,
        )
        exit_code = max(exit_code, code)
    return exit_code


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kis_mock_us_cash_probe",
        description="ROB-951 KIS mock US cash read-only TR probe (default-disabled)",
    )
    parser.add_argument(
        "--tr",
        choices=("preflight", "vtts3007", "vttc0869", "both"),
        default="preflight",
        help="TR to probe; default preflight makes no broker request",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        return asyncio.run(run_probe(selected=args.tr))
    except KeyboardInterrupt:
        print("[probe] interrupted")
        return 1


if __name__ == "__main__":
    sys.exit(main())
