"""ROB-755 — shared fill-event sanitizer for CLI and MCP surfaces.

The CLI (``scripts/list_recent_fill_events.py``) and the MCP tool
(``app/mcp_server/tooling/execution_ledger_events.py``) both need to render
``ExecutionLedger`` rows into the same 20-key triage-friendly dict shape.

That shape lives here as the **single source of truth**: both callers import
``sanitize_fill`` / ``derive_market`` from this module so the JSON output stays
byte-for-byte identical across CLI and MCP.

Security constraint: ``raw_payload_json`` is **never** emitted (the sanitize
function never reads that attribute). The shape is intentionally fixed and
documented as a contract — adding/removing keys must come with a contract
review because both surfaces' tests pin the key set.
"""

from __future__ import annotations

from typing import Any

from app.core.timezone import trade_day_kst


def derive_market(instrument_type: str) -> str:
    """``instrument_type`` 컬럼을 poller 친화적인 market 코드로 변환.

    CLI/MCP ``--market``/``market`` 입력과 무관하게 row의 ``instrument_type``에서
    결정된다 (도메인 단일 출처).
    """
    if instrument_type == "equity_kr":
        return "kr"
    if instrument_type == "equity_us":
        return "us"
    if instrument_type == "crypto":
        return "crypto"
    # forex/index 등 (체결 row에는 사실상 없음) — 그대로 소문자.
    return instrument_type.lower()


def sanitize_fill(row: Any) -> dict[str, Any]:
    """``ExecutionLedger`` ORM row → triage용 dict (raw_payload_json 미포함).

    보안 제약: ``raw_payload_json``은 절대 stdout/MCP 응답에 노출되지 않는다.
    이 함수는 해당 속성을 절대 참조하지 않는다.
    """
    return {
        "ledger_id": row.id,
        "event_key": f"execution_ledger:{row.id}",
        "broker": row.broker,
        "account_mode": row.account_mode,
        "venue": row.venue,
        "instrument_type": row.instrument_type,
        "market": derive_market(row.instrument_type),
        "symbol": row.symbol,
        "raw_symbol": row.raw_symbol,
        "side": row.side,
        "filled_qty": str(row.filled_qty),
        "filled_price": str(row.filled_price),
        "filled_notional": str(row.filled_notional),
        "currency": row.currency,
        "broker_order_id": row.broker_order_id,
        "fill_seq": row.fill_seq,
        "correlation_id": (
            str(row.correlation_id) if row.correlation_id is not None else None
        ),
        "source": row.source,
        "filled_at": row.filled_at.isoformat(),
        "trade_day_kst": trade_day_kst(row.filled_at),
        "created_at": row.created_at.isoformat(),
    }


__all__ = ["derive_market", "sanitize_fill"]
