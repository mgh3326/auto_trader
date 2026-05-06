import base64
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .protocol import (
    _SIDE_MAP,
    _US_SYMBOL_RESERVED_TOKENS,
    DOMESTIC_COMPACT_FILL_FIELDS,
    DOMESTIC_EXECUTION_TR_CODES,
    DOMESTIC_OFFICIAL_FILL_FIELDS,
    EXECUTION_TR_CODES,
    OVERSEAS_EXECUTION_TR_CODES,
    OVERSEAS_FILL_FIELDS,
    OVERSEAS_SIDE_MAP,
)

logger = logging.getLogger(__name__)


class ExecutionMessageParser:
    def __init__(self, encryption_keys_by_tr: dict[str, tuple[str, str]]):
        self._encryption_keys_by_tr = encryption_keys_by_tr

    def parse_message(self, message: str | bytes) -> dict[str, Any] | None:
        """
        KIS WebSocket 메시지 파싱

        |/^ 구분자로 파싱하며 인덱스 안전 처리를 수행합니다.

        Args:
            message: 원본 메시지 (문자열 또는 바이트)

        Returns:
            dict | None: 파싱된 데이터 (파싱 실패 시 None)

        Examples:
            "0|H0STCNI0|01|005930|..."
            JSON: {"type": "error", "message": "..."}
        """
        if isinstance(message, bytes):
            try:
                message = message.decode("utf-8")
            except Exception as e:
                logger.error(f"UTF-8 decode error: {e}")
                return None

        message = message.strip()

        if not message:
            return None

        if message.startswith("{"):
            try:
                parsed = json.loads(message)
                if (
                    isinstance(parsed, dict)
                    and isinstance(parsed.get("header"), dict)
                    and str(parsed["header"].get("tr_id", "")).upper() == "PINGPONG"
                ):
                    return {"system": "pingpong"}
                return parsed
            except Exception as e:
                logger.error(f"JSON parse error: {e}, message: {message}")
                return None

        if "pingpong" in message.lower():
            return {"system": "pingpong"}

        parts = message.split("|")
        if len(parts) < 3:
            logger.warning(f"Invalid message format: {message}")
            return None

        envelope = self._extract_envelope(parts)
        if envelope is None:
            logger.warning(f"Unsupported message envelope: {message}")
            return None

        parsed = {
            "tr_code": envelope["tr_code"],
            "execution_type": envelope["execution_type"],
            "market": "kr"
            if envelope["tr_code"] in DOMESTIC_EXECUTION_TR_CODES
            else "us"
            if envelope["tr_code"] in OVERSEAS_EXECUTION_TR_CODES
            else "unknown",
        }

        payload_source = envelope["payload_source"]
        payload_fields: list[str]
        if envelope.get("encrypted"):
            decrypted_payload = self._decrypt_execution_payload(
                envelope["tr_code"], payload_source
            )
            if not decrypted_payload:
                return None
            payload_fields = self._split_payload(decrypted_payload)
        else:
            payload_fields = self._split_payload(payload_source)

        if payload_fields:
            parsed["raw_fields_count"] = len(payload_fields)
            parsed.update(
                self._parse_execution_payload(
                    payload_fields,
                    parsed["market"],
                    parsed["tr_code"],
                )
            )

        if not parsed.get("symbol"):
            allow_symbol_fallback = not (
                parsed["tr_code"] in OVERSEAS_EXECUTION_TR_CODES
                and len(payload_fields) > OVERSEAS_FILL_FIELDS["symbol"]
            )
            if allow_symbol_fallback:
                parsed["symbol"] = (
                    self._extract_symbol(payload_fields, parsed["market"]) or ""
                )
            else:
                parsed["symbol"] = ""

        if parsed["tr_code"] in EXECUTION_TR_CODES and (
            not parsed.get("filled_price") or not parsed.get("filled_qty")
        ):
            logger.debug(
                "KIS execution payload parsed with fallback values: raw=%s", message
            )

        return parsed

    def _extract_envelope(self, parts: list[str]) -> dict[str, Any] | None:
        first = parts[0]
        second = parts[1] if len(parts) > 1 else ""

        if first in {"0", "1"} and second in EXECUTION_TR_CODES:
            is_encrypted = first == "1"
            if is_encrypted:
                payload_source = (
                    "|".join(parts[3:]) if len(parts) > 3 else "|".join(parts[2:])
                )
            else:
                payload_source = (
                    "|".join(parts[3:])
                    if len(parts) > 3 and parts[2].isdigit() and len(parts[2]) <= 3
                    else "|".join(parts[2:])
                )
            return {
                "tr_code": second,
                "execution_type": 1,
                "payload_source": payload_source,
                "encrypted": is_encrypted,
            }

        if first in EXECUTION_TR_CODES:
            execution_type = int(second) if second.isdigit() else 1
            return {
                "tr_code": first,
                "execution_type": execution_type,
                "payload_source": "|".join(parts[2:]),
                "encrypted": False,
            }

        execution_type = int(second) if second.isdigit() else None
        return {
            "tr_code": first,
            "execution_type": execution_type,
            "payload_source": "|".join(parts[2:]),
            "encrypted": False,
        }

    def _decrypt_execution_payload(self, tr_code: str, payload: str) -> str | None:
        if not payload:
            logger.warning("Encrypted payload is empty: tr_id=%s", tr_code)
            return None

        crypto = self._encryption_keys_by_tr.get(tr_code)
        if not crypto:
            logger.warning(
                "Encrypted payload received but key/iv is missing: tr_id=%s", tr_code
            )
            return None

        key_raw, iv_raw = crypto
        try:
            key_bytes = self._decode_aes_material(key_raw, iv=False)
            iv_bytes = self._decode_aes_material(iv_raw, iv=True)
            cipher_bytes = base64.b64decode(payload)

            # NOTE: KIS WebSocket protocol mandates AES/CBC + PKCS7. Cannot change cipher mode.
            # SonarCloud python:S5542: marked Safe (external protocol requirement).
            decryptor = Cipher(
                algorithms.AES(key_bytes), modes.CBC(iv_bytes)
            ).decryptor()
            padded_plain = decryptor.update(cipher_bytes) + decryptor.finalize()

            unpadder = padding.PKCS7(128).unpadder()
            plain = unpadder.update(padded_plain) + unpadder.finalize()
            return plain.decode("utf-8").strip()
        except Exception as e:
            logger.warning(
                "Failed to decrypt execution payload: tr_id=%s error=%s", tr_code, e
            )
            return None

    def _decode_aes_material(self, raw: str, *, iv: bool) -> bytes:
        expected_lengths = {16} if iv else {16, 24, 32}

        utf8_bytes = raw.encode("utf-8")
        if len(utf8_bytes) in expected_lengths:
            return utf8_bytes

        decoded_bytes = base64.b64decode(raw, validate=True)
        if len(decoded_bytes) in expected_lengths:
            return decoded_bytes

        kind = "iv" if iv else "key"
        raise ValueError(f"Invalid AES {kind} length")

    def _split_payload(self, payload: str) -> list[str]:
        if not payload:
            return []
        if "^" in payload:
            return payload.split("^")
        return [part for part in payload.split("|") if part]

    def _parse_execution_payload(
        self,
        payload_fields: list[str],
        market: str,
        tr_code: str,
    ) -> dict[str, Any]:
        raw_fields = [field.strip() for field in payload_fields]
        compact_fields = [field for field in raw_fields if field]
        if not compact_fields:
            return {}

        if market == "us":
            parsed_overseas = self._parse_overseas_execution(raw_fields)
            if parsed_overseas is not None:
                return parsed_overseas
            logger.error(
                "Overseas execution payload parse FAILED (returned None): "
                "tr_code=%s field_count=%d raw_fields=%r",
                tr_code,
                len(raw_fields),
                raw_fields[:16],
            )

        if market == "kr":
            parsed_domestic = self._parse_domestic_execution(raw_fields)
            if parsed_domestic is not None:
                return parsed_domestic

        kv: dict[str, str] = {}
        for token in compact_fields:
            if "=" in token:
                key, value = token.split("=", 1)
                kv[key.strip().lower()] = value.strip()

        if market in {"kr", "us"} and payload_fields and not kv:
            return {}

        fields = compact_fields
        symbol = self._extract_symbol(fields, market)

        side_token = self._first_token(
            kv,
            fields,
            ["side", "sll_buy_dvsn_cd", "buy_sell", "bsop_gb"],
            lambda v: v.upper() in _SIDE_MAP,
        )
        side = _SIDE_MAP.get(side_token.upper(), "unknown") if side_token else "unknown"

        order_id = self._first_token(
            kv,
            fields,
            ["order_id", "ord_no", "odno", "orgn_ord_no"],
            lambda v: len(v) >= 6 and not (v.isdigit() and len(v) == 6),
        )
        filled_price = self._to_float(
            self._first_token(
                kv,
                fields,
                ["filled_price", "ccld_unpr", "ft_ccld_unpr3", "price", "trade_price"],
                lambda v: self._to_float(v) > 0,
                scan_fields=False,
            )
        )
        filled_qty = self._to_float(
            self._first_token(
                kv,
                fields,
                ["filled_qty", "ccld_qty", "ft_ccld_qty", "qty", "trade_volume"],
                lambda v: self._to_float(v) > 0,
                scan_fields=False,
            )
        )
        filled_amount = self._to_float(
            self._first_token(
                kv,
                fields,
                ["filled_amount", "ccld_amt", "ft_ccld_amt3", "amount", "trade_amount"],
                lambda v: self._to_float(v) > 0,
                scan_fields=False,
            )
        )

        if filled_price <= 0 and len(fields) >= 4:
            filled_price = self._to_float(fields[3])
        if filled_qty <= 0 and len(fields) >= 5:
            filled_qty = self._to_float(fields[4])
        if filled_amount <= 0 and len(fields) >= 6:
            filled_amount = self._to_float(fields[5])

        if filled_price <= 0 or filled_qty <= 0:
            numeric_candidates = []
            for token in fields:
                if token in {symbol, side_token, order_id}:
                    continue
                numeric_value = self._to_float(token)
                if numeric_value > 0:
                    numeric_candidates.append(numeric_value)
            if filled_price <= 0 and numeric_candidates:
                filled_price = max(numeric_candidates)
            if filled_qty <= 0 and numeric_candidates:
                filled_qty = min(numeric_candidates)

        if filled_amount <= 0 and filled_price > 0 and filled_qty > 0:
            filled_amount = filled_price * filled_qty

        filled_at = self._extract_timestamp(
            self._first_token(
                kv,
                fields,
                ["filled_at", "timestamp", "exec_time", "ord_tmd", "ccld_time"],
                lambda v: bool(v.strip()),
            )
        )

        return {
            "symbol": symbol or "",
            "side": side,
            "order_id": order_id,
            "filled_price": filled_price,
            "filled_qty": filled_qty,
            "filled_amount": filled_amount,
            "filled_at": filled_at,
        }

    def _parse_overseas_execution(self, fields: list[str]) -> dict[str, Any] | None:
        if len(fields) <= max(OVERSEAS_FILL_FIELDS.values()):
            logger.error(
                "Overseas execution payload has insufficient fields: field_count=%d required=%d",
                len(fields),
                max(OVERSEAS_FILL_FIELDS.values()) + 1,
            )
            return None

        symbol = fields[OVERSEAS_FILL_FIELDS["symbol"]].strip()
        if not symbol:
            logger.error(
                "Overseas execution payload missing symbol at index %d: field_count=%d",
                OVERSEAS_FILL_FIELDS["symbol"],
                len(fields),
            )
            return None

        side_token = fields[OVERSEAS_FILL_FIELDS["side"]].strip().upper()
        side = OVERSEAS_SIDE_MAP.get(side_token, "unknown")
        order_qty = self._to_float(fields[OVERSEAS_FILL_FIELDS["order_qty"]])
        rctf_cls = fields[OVERSEAS_FILL_FIELDS["rctf_cls"]].strip()
        acpt_yn = fields[OVERSEAS_FILL_FIELDS["acpt_yn"]].strip()
        rfus_yn = fields[OVERSEAS_FILL_FIELDS["rfus_yn"]].strip()
        cntg_yn = fields[OVERSEAS_FILL_FIELDS["cntg_yn"]].strip()

        filled_qty = self._to_float(fields[OVERSEAS_FILL_FIELDS["filled_qty"]])
        filled_price = self._to_float(fields[OVERSEAS_FILL_FIELDS["filled_price"]])

        order_id = fields[2].strip() if len(fields) > 2 else ""
        if not order_id:
            order_id = None

        filled_at = self._extract_timestamp(fields[OVERSEAS_FILL_FIELDS["filled_at"]])
        execution_status = self._classify_overseas_execution_status(
            rfus_yn=rfus_yn,
            rctf_cls=rctf_cls,
            acpt_yn=acpt_yn,
            cntg_yn=cntg_yn,
            filled_qty=filled_qty,
            filled_price=filled_price,
            order_qty=order_qty,
        )

        filled_amount = (
            filled_price * filled_qty if filled_price > 0 and filled_qty > 0 else 0
        )

        return {
            "symbol": symbol,
            "side": side,
            "order_id": order_id,
            "filled_price": filled_price,
            "filled_qty": filled_qty,
            "filled_amount": filled_amount,
            "filled_at": filled_at,
            "currency": "USD",
            "order_qty": order_qty,
            "rctf_cls": rctf_cls,
            "acpt_yn": acpt_yn,
            "rfus_yn": rfus_yn,
            "cntg_yn": cntg_yn,
            "fill_yn": cntg_yn,
            "execution_status": execution_status,
        }

    def _parse_domestic_execution(self, fields: list[str]) -> dict[str, Any] | None:
        parsed = self._parse_domestic_execution_by_official_index(fields)
        if parsed is not None:
            return parsed
        return self._parse_domestic_execution_compact(fields)

    def _parse_domestic_execution_by_official_index(
        self, fields: list[str]
    ) -> dict[str, Any] | None:
        if len(fields) <= max(DOMESTIC_OFFICIAL_FILL_FIELDS.values()):
            return None

        symbol = fields[DOMESTIC_OFFICIAL_FILL_FIELDS["symbol"]].strip()
        if not (symbol.isdigit() and len(symbol) == 6):
            return None

        side_token = fields[DOMESTIC_OFFICIAL_FILL_FIELDS["side"]].strip().upper()
        side = _SIDE_MAP.get(side_token, "unknown")
        order_id = fields[DOMESTIC_OFFICIAL_FILL_FIELDS["order_id"]].strip() or None

        filled_qty = self._to_float(fields[DOMESTIC_OFFICIAL_FILL_FIELDS["filled_qty"]])
        filled_price = self._to_float(
            fields[DOMESTIC_OFFICIAL_FILL_FIELDS["filled_price"]]
        )
        if filled_qty <= 0 or filled_price <= 0:
            return None

        filled_at = self._extract_timestamp(
            fields[DOMESTIC_OFFICIAL_FILL_FIELDS["filled_at"]]
        )
        fill_yn = fields[DOMESTIC_OFFICIAL_FILL_FIELDS["fill_yn"]].strip()
        if not self._is_supported_timestamp_token(
            fields[DOMESTIC_OFFICIAL_FILL_FIELDS["filled_at"]]
        ):
            return None
        if fill_yn not in {"1", "2"}:
            return None

        return {
            "symbol": symbol,
            "side": side,
            "order_id": order_id,
            "filled_price": filled_price,
            "filled_qty": filled_qty,
            "filled_amount": filled_price * filled_qty,
            "filled_at": filled_at,
            "fill_yn": fill_yn,
        }

    def _parse_domestic_execution_compact(
        self, fields: list[str]
    ) -> dict[str, Any] | None:
        if len(fields) <= max(DOMESTIC_COMPACT_FILL_FIELDS.values()):
            return None

        symbol = fields[DOMESTIC_COMPACT_FILL_FIELDS["symbol"]].strip()
        if not (symbol.isdigit() and len(symbol) == 6):
            return None

        side_token = fields[DOMESTIC_COMPACT_FILL_FIELDS["side"]].strip().upper()
        side = _SIDE_MAP.get(side_token, "unknown")
        order_id = fields[DOMESTIC_COMPACT_FILL_FIELDS["order_id"]].strip() or None

        first_numeric = self._to_float(
            fields[DOMESTIC_COMPACT_FILL_FIELDS["first_numeric"]]
        )
        second_numeric = self._to_float(
            fields[DOMESTIC_COMPACT_FILL_FIELDS["second_numeric"]]
        )
        if first_numeric <= 0 or second_numeric <= 0:
            return None

        if first_numeric <= second_numeric:
            filled_qty = first_numeric
            filled_price = second_numeric
        else:
            filled_qty = second_numeric
            filled_price = first_numeric

        filled_at_token = self._find_hhmmss_token(
            fields, exclude={symbol, order_id or ""}
        )
        if (
            not filled_at_token
            and len(fields) > DOMESTIC_COMPACT_FILL_FIELDS["filled_at"]
        ):
            fallback_token = fields[DOMESTIC_COMPACT_FILL_FIELDS["filled_at"]].strip()
            if self._is_hhmmss(fallback_token):
                filled_at_token = fallback_token
        if not filled_at_token:
            return None
        filled_at = self._extract_timestamp(filled_at_token)

        fill_yn = ""
        if len(fields) > DOMESTIC_OFFICIAL_FILL_FIELDS["fill_yn"]:
            fill_yn = fields[DOMESTIC_OFFICIAL_FILL_FIELDS["fill_yn"]].strip()

        return {
            "symbol": symbol,
            "side": side,
            "order_id": order_id,
            "filled_price": filled_price,
            "filled_qty": filled_qty,
            "filled_amount": filled_price * filled_qty,
            "filled_at": filled_at,
            "fill_yn": fill_yn,
        }

    def _first_token(
        self,
        kv: dict[str, str],
        fields: list[str],
        preferred_keys: list[str],
        predicate: Callable[[str], bool],
        *,
        scan_fields: bool = True,
    ) -> str | None:
        for key in preferred_keys:
            value = kv.get(key.lower())
            if value and predicate(value):
                return value

        if not scan_fields:
            return None

        for token in fields:
            if predicate(token):
                return token
        return None

    def _extract_symbol(self, fields: list[str], market: str) -> str | None:
        for token in fields:
            stripped = token.strip()
            if market == "kr" and stripped.isdigit() and len(stripped) == 6:
                return stripped
            if market != "us":
                continue

            normalized = stripped.upper()
            cleaned = normalized.replace(".", "").replace("-", "").replace("/", "")
            if not cleaned or not cleaned.isalnum() or not (1 <= len(cleaned) <= 10):
                continue
            if not cleaned[0].isalpha() or cleaned.isdigit():
                continue
            if normalized != stripped:
                continue
            if cleaned in _US_SYMBOL_RESERVED_TOKENS:
                continue
            if cleaned.startswith(("ORDER", "ACNT", "ACCOUNT", "CUST", "USER")):
                continue
            if any(ch.isdigit() for ch in cleaned) and len(cleaned) >= 8:
                continue
            return stripped
        return None

    def _extract_timestamp(self, value: str | None) -> str:
        if not value:
            return datetime.now(UTC).replace(microsecond=0).isoformat()

        cleaned = value.strip()
        if "T" in cleaned:
            return cleaned
        if cleaned.isdigit():
            if len(cleaned) == 6:
                today = datetime.now(UTC).strftime("%Y%m%d")
                return datetime.strptime(today + cleaned, "%Y%m%d%H%M%S").isoformat()
            if len(cleaned) == 14:
                return datetime.strptime(cleaned, "%Y%m%d%H%M%S").isoformat()
        return cleaned

    def _find_hhmmss_token(
        self, fields: list[str], *, exclude: set[str] | None = None
    ) -> str | None:
        excluded = {token.strip() for token in (exclude or set()) if token}
        for token in fields:
            stripped = token.strip()
            if stripped in excluded:
                continue
            if self._is_hhmmss(stripped):
                return stripped
        return None

    def _is_hhmmss(self, value: str) -> bool:
        if len(value) != 6 or not value.isdigit():
            return False
        hour = int(value[:2])
        minute = int(value[2:4])
        second = int(value[4:6])
        return hour < 24 and minute < 60 and second < 60

    def _is_supported_timestamp_token(self, value: str | None) -> bool:
        if not value:
            return False
        cleaned = value.strip()
        if self._is_hhmmss(cleaned):
            return True
        if len(cleaned) == 14 and cleaned.isdigit():
            try:
                datetime.strptime(cleaned, "%Y%m%d%H%M%S")
                return True
            except ValueError:
                return False
        return False

    def _to_float(self, value: Any) -> float:
        if value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _parse_response(self, message: str | bytes) -> dict[str, Any]:
        """
        구독 응답 메시지 파싱

        Args:
            message: 응답 메시지

        Returns:
            dict: 파싱된 데이터
        """
        if isinstance(message, bytes):
            message = message.decode("utf-8")

        message = message.strip()

        if message.startswith("{"):
            return json.loads(message)

        return {"type": "ack", "message": message}

    def _now_iso(self) -> str:
        return datetime.now(UTC).isoformat()

    def _new_correlation_id(self) -> str:
        return uuid4().hex

    def _classify_overseas_execution_status(
        self,
        *,
        rfus_yn: str,
        rctf_cls: str,
        acpt_yn: str,
        cntg_yn: str,
        filled_qty: float,
        filled_price: float,
        order_qty: float,
    ) -> str:
        if rfus_yn == "1":
            return "rejected"
        if rctf_cls == "2" or acpt_yn == "3":
            return "canceled"
        if cntg_yn != "2":
            return "order_notice"
        if (
            filled_qty > 0
            and filled_price > 0
            and order_qty > 0
            and filled_qty < order_qty
        ):
            return "partial"
        if filled_qty > 0 and filled_price > 0:
            return "filled"
        return "invalid_fill"

    def is_execution_event(self, data: dict[str, Any]) -> bool:
        if data.get("type") in {"error", "ack"}:
            return False
        tr_code = str(data.get("tr_code", ""))
        if tr_code in OVERSEAS_EXECUTION_TR_CODES:
            status = str(data.get("execution_status", "")).strip().lower()
            if status:
                is_executable = status in {"filled", "partial"}
                if not is_executable:
                    logger.error(
                        "Overseas execution event REJECTED (possible field index mismatch): "
                        "tr_code=%s fill_yn=%r cntg_yn_raw=%r filled_qty=%s filled_price=%s "
                        "execution_status=%s raw_fields_count=%d",
                        tr_code,
                        data.get("fill_yn"),
                        data.get("cntg_yn"),
                        data.get("filled_qty"),
                        data.get("filled_price"),
                        data.get("execution_status"),
                        int(data.get("raw_fields_count", 0)),
                    )
                return is_executable
            is_filled = str(data.get("fill_yn", "")).strip() == "2"
            has_qty = self._to_float(data.get("filled_qty")) > 0
            has_price = self._to_float(data.get("filled_price")) > 0
            if not (is_filled and has_qty and has_price):
                logger.error(
                    "Overseas execution event REJECTED (possible field index mismatch): "
                    "tr_code=%s fill_yn=%r cntg_yn_raw=%r filled_qty=%s filled_price=%s "
                    "execution_status=%s raw_fields_count=%d",
                    tr_code,
                    data.get("fill_yn"),
                    data.get("cntg_yn"),
                    data.get("filled_qty"),
                    data.get("filled_price"),
                    data.get("execution_status"),
                    int(data.get("raw_fields_count", 0)),
                )
            return is_filled and has_qty and has_price
        if tr_code in DOMESTIC_EXECUTION_TR_CODES:
            fill_yn = str(data.get("fill_yn") or data.get("cntg_yn") or "").strip()
            if fill_yn:
                return fill_yn == "2"
            status = str(data.get("execution_status", "")).strip().lower()
            if status:
                return status in {"filled", "partial"}
            logger.info(
                "Drop domestic execution event without fill_yn: correlation_id=%s "
                "tr_code=%s symbol=%s execution_type=%s",
                data.get("correlation_id"),
                tr_code,
                data.get("symbol"),
                data.get("execution_type"),
            )
            return False
        if data.get("execution_type") == 1:
            return True
        return tr_code in EXECUTION_TR_CODES
