"""Orchestrates the Naver remote-debug audit for one bundle.

Read-only: loads the bundle's persisted ``symbol`` snapshots, reads each
symbol's auto_trader quote, drives a per-symbol CDP cross-check (sequential,
fail-open), and assembles the stdout audit payload. No DB writes.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.services.action_report.remote_debug_audit.cdp_client import CdpSession
from app.services.action_report.remote_debug_audit.cross_check import (
    SymbolQuote,
    build_audit,
    cross_check_symbol,
)
from app.services.action_report.remote_debug_audit.naver_quote import (
    NAVER_EXTRACT_JS,
    naver_url,
    parse_naver_quote,
)

_DEFAULT_TOLERANCE_PCT = 5.0
_PER_SYMBOL_TIMEOUT_S = 15.0


def extract_symbol_quotes(
    item_snapshot_pairs: list[tuple[Any, Any]],
) -> list[SymbolQuote]:
    """Pull (symbol, name, last_price, quote_status) from ``symbol`` snapshots."""
    out: list[SymbolQuote] = []
    for _item, snap in item_snapshot_pairs:
        if getattr(snap, "snapshot_kind", None) != "symbol":
            continue
        payload = getattr(snap, "payload_json", None) or {}
        symbol = getattr(snap, "symbol", None) or payload.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        quote = payload.get("quote") if isinstance(payload, dict) else None
        quote = quote if isinstance(quote, dict) else {}
        last_price = quote.get("last_price")
        out.append(
            SymbolQuote(
                symbol=symbol,
                name=payload.get("name") if isinstance(payload, dict) else None,
                last_price=last_price if isinstance(last_price, (int, float)) else None,
                quote_status=quote.get("status"),
            )
        )
    return out


class RemoteDebugAuditService:
    def __init__(
        self,
        *,
        snapshots_repo: Any,
        reports_repo: Any,
        cdp_session: CdpSession,
        tolerance_pct: float = _DEFAULT_TOLERANCE_PCT,
    ) -> None:
        self._snapshots_repo = snapshots_repo
        self._reports_repo = reports_repo
        self._cdp = cdp_session
        self._tolerance_pct = tolerance_pct

    async def resolve_bundle_uuid(self, report_uuid: uuid.UUID) -> uuid.UUID:
        report = await self._reports_repo.get_report_by_uuid(report_uuid)
        if report is None or report.snapshot_bundle_uuid is None:
            raise LookupError(
                f"report {report_uuid} not found or has no snapshot bundle"
            )
        return report.snapshot_bundle_uuid

    async def audit_bundle(
        self, bundle_uuid: uuid.UUID, *, max_symbols: int
    ) -> dict[str, Any]:
        bundle = await self._snapshots_repo.get_bundle_by_uuid(bundle_uuid)
        if bundle is None:
            raise LookupError(f"bundle {bundle_uuid} not found")
        pairs = await self._snapshots_repo.list_bundle_items_with_snapshots(bundle.id)
        quotes = extract_symbol_quotes(pairs)[: max(1, max_symbols)]

        findings: list[dict[str, Any]] = []
        for at in quotes:
            naver = await self._fetch_naver(at.symbol)
            findings.append(
                cross_check_symbol(at, naver, tolerance_pct=self._tolerance_pct)
            )
        return build_audit(
            snapshot_bundle_uuid=str(bundle_uuid),
            report_uuid=None,
            findings=findings,
        )

    async def _fetch_naver(self, symbol: str):
        try:
            raw = await self._cdp.fetch_rendered(
                naver_url(symbol), NAVER_EXTRACT_JS, timeout_s=_PER_SYMBOL_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 — per-symbol fail-open
            return None
        return parse_naver_quote(raw)
