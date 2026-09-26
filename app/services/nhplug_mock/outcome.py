"""Total response classifier: absent vendor proof codes mean uncertainty."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.services.nhplug_mock.intent import ORDER_PATHS
from app.services.nhplug_mock.ledger import DispatchOutcome

_ORDER_NUMBER = re.compile(r"^[1-9][0-9]{0,9}$")


@dataclass(frozen=True, slots=True)
class ResponseMeta:
    http_status: int

    @classmethod
    def of(cls, response: Any) -> ResponseMeta:
        return cls(response.status_code)


@dataclass(frozen=True, slots=True)
class ParsedOrderResponse:
    rsp_cd: str | None


def extract_order_no(raw: object) -> str | None:
    """Extract evidence before any fallible metadata or business parsing."""

    try:
        payload = json.loads(raw) if type(raw) in {bytes, str} else None
        if type(payload) is not dict or type(payload.get("Output_0")) is not dict:
            return None
        number = payload["Output_0"].get("mkt_orr_no")
        if type(number) is int:
            number = str(number)
        return (
            number if type(number) is str and _ORDER_NUMBER.fullmatch(number) else None
        )
    except (ValueError, TypeError, UnicodeDecodeError, OverflowError):
        return None


def parse_order_response(raw: object) -> ParsedOrderResponse | None:
    try:
        payload = json.loads(raw) if type(raw) in {bytes, str} else None
        if type(payload) is not dict:
            return None
        code = payload.get("rsp_cd")
        return ParsedOrderResponse(code if type(code) is str else None)
    except (ValueError, TypeError, UnicodeDecodeError, OverflowError):
        return None


def classify(
    path: object,
    meta: object,
    parsed: object,
    evidence_no: object,
    failure: BaseException | None,
    *,
    success_codes: object = frozenset(),
    no_order_codes: object = frozenset(),
) -> DispatchOutcome:
    """Never raise, including for malformed paths and unhashable inputs."""

    number: str | None = None
    try:
        number = (
            evidence_no
            if type(evidence_no) is str and _ORDER_NUMBER.fullmatch(evidence_no)
            else None
        )
        if type(path) is not str or path not in ORDER_PATHS:
            return DispatchOutcome("uncertain", "unknown_path", number)
        if failure is not None:
            return DispatchOutcome("uncertain", type(failure).__name__, number)
        if type(meta) is not ResponseMeta or type(parsed) is not ParsedOrderResponse:
            return DispatchOutcome("uncertain", "unparsed_response", number)
        if (
            type(meta.http_status) is not int
            or meta.http_status != 200
            or type(parsed.rsp_cd) is not str
        ):
            return DispatchOutcome("uncertain", "no_proof_code", number)
        if (
            type(success_codes) in {set, frozenset}
            and parsed.rsp_cd in success_codes
            and number is not None
        ):
            return DispatchOutcome(
                "accepted", evidence_order_id=number, rsp_cd=parsed.rsp_cd
            )
        if (
            type(no_order_codes) in {set, frozenset}
            and parsed.rsp_cd in no_order_codes
            and number is None
        ):
            return DispatchOutcome("rejected", rsp_cd=parsed.rsp_cd)
        return DispatchOutcome("uncertain", "no_proof_code", number)
    except BaseException:
        return DispatchOutcome("uncertain", "classify_failed", number)
