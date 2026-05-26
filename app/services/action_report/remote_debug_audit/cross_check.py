"""Cross-check an auto_trader symbol quote against a Naver quote, and assemble
the stdout audit payload. Pure logic — no IO, no browser.

Coverage/plausibility, not exact reconciliation: the goal is to surface gaps
(auto_trader missing/mis-resolving data Naver has), not to reconcile prices to
the won.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from app.services.action_report.remote_debug_audit.naver_quote import NaverQuote


@dataclass(frozen=True)
class SymbolQuote:
    """auto_trader side, extracted from a persisted ``symbol`` snapshot."""

    symbol: str
    name: str | None
    last_price: float | None
    quote_status: str | None


def cross_check_symbol(
    at: SymbolQuote,
    naver: NaverQuote | None,
    *,
    tolerance_pct: float,
) -> dict[str, Any]:
    symbol_resolved = naver is not None and naver.price is not None
    at_quote_present = at.quote_status == "ok" and isinstance(
        at.last_price, (int, float)
    )

    name_match: bool | None = None
    if naver is not None and naver.name and at.name:
        name_match = _normalize(naver.name) == _normalize(at.name)

    price_within_tolerance: bool | None = None
    if symbol_resolved and at_quote_present:
        assert naver is not None and naver.price is not None  # narrowed above
        denom = abs(at.last_price) or 1.0
        price_within_tolerance = (
            abs(naver.price - at.last_price) / denom * 100.0 <= tolerance_pct
        )

    # Status precedence: unresolved > at-missing > mismatch > ok.
    if not symbol_resolved:
        status, reason_code = "unavailable", "naver_symbol_unresolved"
    elif not at_quote_present:
        status, reason_code = "at_quote_missing", "at_quote_missing"
    elif price_within_tolerance is False:
        status, reason_code = "mismatch", "naver_price_mismatch"
    else:
        status, reason_code = "ok", None

    finding: dict[str, Any] = {
        "symbol": at.symbol,
        "symbol_resolved": symbol_resolved,
        "name_match": name_match,
        "at_quote_present": at_quote_present,
        "at_price": at.last_price,
        "naver_price": naver.price if naver else None,
        "price_within_tolerance": price_within_tolerance,
        "status": status,
    }
    if reason_code is not None:
        finding["reason_code"] = reason_code
    return finding


def build_audit(
    *,
    snapshot_bundle_uuid: str | None,
    report_uuid: str | None,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    gaps: list[dict[str, Any]] = []
    mismatched = sorted(f["symbol"] for f in findings if f["status"] == "mismatch")
    unresolved = sorted(f["symbol"] for f in findings if f["status"] == "unavailable")
    at_missing = sorted(
        f["symbol"] for f in findings if f["status"] == "at_quote_missing"
    )
    if mismatched:
        gaps.append(
            {
                "severity": "warning",
                "kind": "naver_price_mismatch",
                "sources": mismatched,
                "message": "Naver와 auto_trader 가격 차이가 허용범위 초과 — 후속 데이터 점검 검토",
            }
        )
    if unresolved:
        gaps.append(
            {
                "severity": "warning",
                "kind": "naver_symbol_unresolved",
                "sources": unresolved,
                "message": "Naver에서 심볼을 해석하지 못함 — 심볼 매핑/커버리지 점검 검토",
            }
        )
    if at_missing:
        gaps.append(
            {
                "severity": "info",
                "kind": "at_quote_missing",
                "sources": at_missing,
                "message": "auto_trader가 해당 심볼 quote를 못 가짐(Naver는 있음) — 수집 점검 검토",
            }
        )
    return {
        "source": "naver_remote_debug",
        "snapshot_bundle_uuid": snapshot_bundle_uuid,
        "report_uuid": report_uuid,
        "as_of": dt.datetime.now(tz=dt.UTC).isoformat(),
        "affects_report_generation": False,
        "checked_symbols": len(findings),
        "findings": findings,
        "gaps": gaps,
    }


def _normalize(name: str) -> str:
    return "".join(name.split()).lower()
