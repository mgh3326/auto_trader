"""Shared H0STCNI0 frame and row builders for the #1175 quarantine tests.

The frame mirrors what fillwire stores in ``raw_payload_json``:
``{"tr", "fields", "received_at"}`` with the decrypted record fields in go-kis
order. Fields 0 and 1 (customer id, account number) are fake placeholders.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.services.execution_ledger.quarantine import fillwire_fill_seq


def frame_fields(
    *,
    order_no: str = "0000012345",
    symbol: str = "034220",
    side: str = "02",
    qty: str = "1",
    price: str = "5000",
    hhmmss: str = "093001",
    cntg_yn: str = "1",
) -> list[str]:
    fields = [
        "FAKECUST",  # 0 CUST_ID (never read)
        "ACCTFAKE01",  # 1 ACNT_NO (never read)
        order_no,  # 2 ODER_NO
        "",  # 3 OODER_NO
        side,  # 4 SELN_BYOV_CLS
        "0",  # 5 RCTF_CLS
        "00",  # 6 ODER_KIND
        "0",  # 7 ODER_COND
        symbol,  # 8 STCK_SHRN_ISCD
        qty,  # 9 CNTG_QTY
        price,  # 10 CNTG_UNPR
        hhmmss,  # 11 STCK_CNTG_HOUR
        "0",  # 12 RFUS_YN
        cntg_yn,  # 13 CNTG_YN
        "1",  # 14 ACPT_YN
        "00950",  # 15 BRNC_NO
        qty,  # 16 ODER_QTY
        "FAKENAME",  # 17 ACNT_NAME
        "",  # 18
        "",  # 19
        "",  # 20
        "",  # 21
        "",  # 22
    ]
    return fields


def frame(*, tr: str = "H0STCNI0", **field_overrides: Any) -> dict[str, Any]:
    return {
        "tr": tr,
        "fields": frame_fields(**field_overrides),
        "received_at": "2026-09-30T09:30:01.123+09:00",
    }


def row_kwargs(**overrides: Any) -> dict[str, Any]:
    """ExecutionLedgerUpsert-shaped kwargs for a phantom accept-notice row."""
    order_no = overrides.pop("order_no", "0000012345")
    symbol = overrides.pop("symbol", "034220")
    cntg_yn = overrides.pop("cntg_yn", "1")
    raw = overrides.pop(
        "raw_payload_json",
        frame(order_no=order_no, symbol=symbol, cntg_yn=cntg_yn),
    )
    fill_seq = (
        fillwire_fill_seq(raw["fields"])
        if isinstance(raw, dict)
        and isinstance(raw.get("fields"), list)
        and all(isinstance(item, str) for item in raw["fields"])
        else 7
    )
    data: dict[str, Any] = {
        "broker": "kis",
        "account_mode": "live",
        "venue": "krx",
        "instrument_type": "equity_kr",
        "symbol": symbol,
        "raw_symbol": symbol,
        "side": "buy",
        "broker_order_id": order_no,
        "fill_seq": fill_seq,
        "filled_qty": "1",
        "filled_price": "5000",
        "filled_notional": "5000",
        "filled_at": "2026-09-30T09:30:01+09:00",
        "currency": "KRW",
        "source": "websocket",
        "raw_payload_json": raw,
    }
    data.update(overrides)
    return data


def fake_row(**overrides: Any) -> SimpleNamespace:
    """An in-memory row with the attributes ``evaluate_row`` reads."""
    from datetime import datetime
    from decimal import Decimal

    data = row_kwargs(**{k: v for k, v in overrides.items() if k != "quarantined_at"})
    data["filled_qty"] = Decimal(str(data["filled_qty"]))
    data["filled_price"] = Decimal(str(data["filled_price"]))
    data["filled_at"] = datetime.fromisoformat(str(data["filled_at"]))
    data.setdefault("quarantined_at", overrides.get("quarantined_at"))
    data.setdefault("quarantine_reason", "earlier" if data["quarantined_at"] else None)
    data.setdefault("quarantined_by", "desk" if data["quarantined_at"] else None)
    return SimpleNamespace(**data)
