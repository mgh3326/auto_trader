"""Strict normalization for read-only KIS VI and member market data."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


class KISFlowResponseContractError(ValueError):
    """A KIS flow/event response did not match its documented wire shape."""


_DECIMAL_FIELD = re.compile(r"_(?:vol|qty|pbmn|tr_pbmn|prpr|rate|ctrt|val|amt|price)\d*$")


def _decimal(value: Any, field: str) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise KISFlowResponseContractError(f"{field} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise KISFlowResponseContractError(
            f"{field} must be a decimal string"
        ) from exc
    if not parsed.is_finite():
        raise KISFlowResponseContractError(f"{field} must be finite")
    return parsed


def _normalize_fields(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        if not isinstance(key, str):
            raise KISFlowResponseContractError("KIS field names must be strings")
        if value is None or value == "":
            result[key] = None if value is None else ""
        elif _DECIMAL_FIELD.search(key):
            result[key] = _decimal(value, key)
        elif key.endswith(("_date", "_dt")) and isinstance(value, str) and re.fullmatch(r"\d{8}", value):
            try:
                result[key] = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
            except ValueError as exc:
                raise KISFlowResponseContractError(f"{key} must be YYYYMMDD") from exc
        else:
            if not isinstance(value, str):
                raise KISFlowResponseContractError(f"{key} must be a string")
            result[key] = value
    return result


def parse_vi_status_rows(raw: Any, *, symbol: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise KISFlowResponseContractError("VI output must be a list")
    rows: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            raise KISFlowResponseContractError("VI output rows must be objects")
        code = row.get("stck_shrn_iscd")
        if code != symbol:
            raise KISFlowResponseContractError("VI row symbol did not match request")
        rows.append(_normalize_fields(row))
    return rows


def parse_member_summary(raw: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(raw, dict):
        raise KISFlowResponseContractError("member output must be an object")
    normalized = _normalize_fields(raw)
    buyers: list[dict[str, Any]] = []
    sellers: list[dict[str, Any]] = []
    for rank in range(1, 6):
        for side, target, member_field, name_field, volume_field, amount_field in (
            ("buyer", buyers, f"shnu_mbcr_no{rank}", f"shnu_mbcr_name{rank}", f"shnu_vol{rank}", f"shnu_tr_pbmn{rank}"),
            ("seller", sellers, f"seln_mbcr_no{rank}", f"seln_mbcr_name{rank}", f"seln_vol{rank}", f"seln_tr_pbmn{rank}"),
        ):
            name = normalized.get(name_field)
            code = normalized.get(member_field)
            volume = normalized.get(volume_field)
            amount = normalized.get(amount_field)
            if name in (None, "") and code in (None, "") and volume in (None, "") and amount in (None, ""):
                continue
            if not isinstance(name, str) or not name or not isinstance(code, str) or not code:
                raise KISFlowResponseContractError(
                    f"member {side} rank {rank} is incomplete"
                )
            if not isinstance(volume, Decimal):
                raise KISFlowResponseContractError(
                    f"member {side} rank {rank} volume is invalid"
                )
            if not isinstance(amount, Decimal):
                raise KISFlowResponseContractError(
                    f"member {side} rank {rank} amount is invalid"
                )
            target.append({"rank": rank, "member_code": code, "member_name": name, "volume": volume, "trading_amount": amount})
    return {"buyers": buyers, "sellers": sellers}
