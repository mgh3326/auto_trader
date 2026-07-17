"""ROB-407 — broker별 live 주문 체결 evidence 어댑터.

각 어댑터는 ledger row를 받아 FillEvidence(verdict/filled_qty/avg_price)를 돌려준다.
US 해외는 일별주문 거래소 순회 + client-side 필터(KIS odno 미지원) 후
classify_fill_evidence를 재사용(해외 ft_ 키를 canonical 키로 정규화).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Protocol

from app.mcp_server.tooling.kis_live_ledger import _create_live_kis_client
from app.mcp_server.tooling.orders_modify_cancel import (
    _build_us_exchange_candidates,
    _find_us_order_in_recent_history,
    _normalize_kis_overseas_order,
)
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    FillEvidence,
    FillVerdict,
    classify_fill_evidence,
)
from app.services.brokers.upbit.orders import fetch_order_detail

logger = logging.getLogger(__name__)


class LiveFillEvidenceAdapter(Protocol):
    broker: str

    async def fetch_evidence(self, row: Any) -> FillEvidence: ...


def _normalize_overseas_for_classify(order: dict[str, Any]) -> dict[str, Any]:
    """해외 일별주문 row(ft_ 키)를 classify_fill_evidence canonical 키로 정규화."""
    return {
        "odno": order.get("odno") or order.get("ODNO") or order.get("ord_no"),
        "ord_qty": order.get("ft_ord_qty")
        or order.get("FT_ORD_QTY")
        or order.get("ord_qty")
        or 0,
        "tot_ccld_qty": order.get("ft_ccld_qty")
        or order.get("FT_CCLD_QTY")
        or order.get("ccld_qty")
        or 0,
        "ccld_unpr": order.get("ft_ccld_unpr3")
        or order.get("FT_CCLD_UNPR3")
        or order.get("ccld_unpr")
        or 0,
    }


class UsOverseasEvidenceAdapter:
    broker = "kis"

    async def fetch_evidence(self, row: Any) -> FillEvidence:
        kis = _create_live_kis_client()
        candidates = await _build_us_exchange_candidates(row.symbol)
        order, _exch = await _find_us_order_in_recent_history(
            kis, str(row.order_no), str(row.symbol), candidates
        )
        if order is None:
            # fail-closed: 증거 미발견 → pending 유지(취소/만료 단정 금지)
            return FillEvidence(
                FillVerdict.PENDING,
                Decimal("0"),
                None,
                None,
                "not_found",
                f"order {row.order_no} not in recent overseas history",
            )

        # Reuse the same normalizer as order-history / proposal target
        # preflight.  It consumes KIS ``nccs_qty`` and direct cancellation
        # evidence, unlike the fill-only canonical row below.  Without this,
        # a DAY order with zero remaining and zero fills was always reduced to
        # ``PENDING`` by ``classify_fill_evidence``.
        normalized_order = _normalize_kis_overseas_order(order)
        filled_qty = _to_decimal(normalized_order["filled_qty"]) or Decimal("0")
        remaining_qty = _to_decimal(normalized_order["remaining_qty"]) or Decimal("0")
        normalized_status = str(normalized_order["status"])
        if filled_qty == 0 and remaining_qty == 0:
            if normalized_status == "expired":
                return FillEvidence(
                    FillVerdict.EXPIRED,
                    Decimal("0"),
                    None,
                    None,
                    "expired",
                    f"KIS overseas order {row.order_no} expired with no fills",
                )
            if normalized_status == "cancelled":
                return FillEvidence(
                    FillVerdict.NONE,
                    Decimal("0"),
                    None,
                    None,
                    "cancelled",
                    f"KIS overseas order {row.order_no} cancelled with no fills",
                )
            if normalized_status != "pending":
                # This invariant must never silently regress into pending: an
                # unfilled order with zero remaining is terminal broker evidence.
                raise RuntimeError(
                    "unexpected terminal KIS overseas order state "
                    f"status={normalized_status!r} order_no={row.order_no}"
                )
        normalized = _normalize_overseas_for_classify(order)
        return classify_fill_evidence(order_no=str(row.order_no), rows=[normalized])


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


class UpbitEvidenceAdapter:
    broker = "upbit"

    async def fetch_evidence(self, row: Any) -> FillEvidence:
        detail = await fetch_order_detail(str(row.order_no))
        if not detail:
            return FillEvidence(
                FillVerdict.PENDING,
                Decimal("0"),
                None,
                None,
                "not_found",
                f"order {row.order_no} detail empty",
            )
        state = str(detail.get("state", "")).strip()
        executed = _to_decimal(detail.get("executed_volume")) or Decimal("0")
        remaining = _to_decimal(detail.get("remaining_volume")) or Decimal("0")
        avg = _to_decimal(detail.get("avg_price")) or _to_decimal(detail.get("price"))

        # 체결분이 있으면 (취소 후 부분체결 포함) 체결을 우선 인정
        if executed > 0 and avg and avg > 0:
            verdict = FillVerdict.FILLED if remaining <= 0 else FillVerdict.PARTIAL
            return FillEvidence(
                verdict,
                executed,
                avg,
                None,
                verdict.value,
                f"upbit {row.order_no} {verdict.value} {executed}@{avg}",
            )
        if state == "wait":
            return FillEvidence(
                FillVerdict.PENDING,
                Decimal("0"),
                None,
                None,
                "pending",
                f"upbit {row.order_no} waiting",
            )
        # done/cancel with zero executed → 미체결 종료
        return FillEvidence(
            FillVerdict.NONE,
            Decimal("0"),
            None,
            None,
            "cancelled",
            f"upbit {row.order_no} ended unfilled",
        )


_ADAPTERS: dict[str, LiveFillEvidenceAdapter] = {
    "kis": UsOverseasEvidenceAdapter(),
    "upbit": UpbitEvidenceAdapter(),
}


def get_evidence_adapter(broker: str) -> LiveFillEvidenceAdapter:
    adapter = _ADAPTERS.get(broker)
    if adapter is None:
        raise ValueError(f"no live evidence adapter for broker={broker!r}")
    return adapter
