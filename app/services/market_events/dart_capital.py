"""Receipt-bound OpenDART capital decision and ownership details."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from http.client import HTTPException, HTTPSConnection
from io import BytesIO
from typing import Any
from urllib.parse import urlencode
from zipfile import BadZipFile, ZipFile

from lxml import etree

from app.core.config import settings
from app.services.market_events.dart_helpers import normalize_dart_corp_name

_ENDPOINTS = {
    "유상증자결정": "piicDecsn",
    "무상증자결정": "fricDecsn",
    "감자결정": "crDecsn",
    "전환사채권발행결정": "cvbdIsDecsn",
    "신주인수권부사채권발행결정": "bdwtIsDecsn",
    "교환사채권발행결정": "exbdIsDecsn",
    "자기주식취득결정": "tsstkAqDecsn",
    "자기주식처분결정": "tsstkDpDecsn",
    "주식등의대량보유상황보고서": "majorstock",
    "임원ㆍ주요주주특정증권등소유상황보고서": "elestock",
    "임원주요주주특정증권등소유상황보고서": "elestock",
    "현금ㆍ현물배당결정": "document",
    "현금배당결정": "document",
    "배당기준일결정": "document",
    "현금ㆍ현물배당을위한주주명부폐쇄": "document",
}
_OWNERSHIP = {
    "majorstock": ("stkrt", "stkrt_irds"),
    "elestock": ("sp_stock_lmp_rate", "sp_stock_lmp_irds_rate"),
}
_SHARE_CHANGES = {
    "piicDecsn": ("nstk", "new_shares", "bfic"),
    "fricDecsn": ("nstk", "new_shares", "bfic"),
    "crDecsn": ("crstk", "reduced_shares", "bfcr"),
}
_TREASURY = {
    "tsstkAqDecsn": ("aq", "buyback"),
    "tsstkDpDecsn": ("dp", "treasury_sale"),
}
_BONDS = {
    "cvbdIsDecsn": ("cv_prc", "cvisstk_cnt", "cvisstk_tisstk_vs"),
    "bdwtIsDecsn": ("ex_prc", "nstk_isstk_cnt", "nstk_isstk_tisstk_vs"),
    "exbdIsDecsn": ("ex_prc", "extg_stkcnt", "extg_tisstk_vs"),
}
_MAX_BODY = 8 * 1024 * 1024
# OpenDART answers HTTP 302 to /error1.html for a request without a User-Agent
# (observed 2026-11-28: same key and params, 302 without the header, 200 with it).
USER_AGENT = "auto-trader-opendart/1.0"


class DartDetailError(RuntimeError):
    """An OpenDART request failed.

    ``failure_code`` is one of the closed v2 collection failure codes
    (``CollectionFailure``: http_4xx, http_5xx, timeout, transport, bad_response,
    unparseable, quota). It is the only error text that is ever recorded in
    ``last_error``; the message is for logs and never carries the API key.
    """

    failure_code: str = "bad_response"

    def __init__(self, message: str = "", *, failure_code: str | None = None) -> None:
        super().__init__(message)
        if failure_code is not None:
            self.failure_code = failure_code


class DartCredentialError(DartDetailError):
    failure_code = "http_4xx"


class DartRateLimitError(DartDetailError):
    failure_code = "quota"


class DartUnavailableError(DartDetailError):
    failure_code = "http_5xx"


class DartArgumentError(DartDetailError):
    failure_code = "http_4xx"


_ERRORS = {
    **dict.fromkeys(("010", "011", "012", "901"), DartCredentialError),
    "020": DartRateLimitError,
    "800": DartUnavailableError,
    "021": DartArgumentError,
    "100": DartArgumentError,
}


def capital_endpoint(title: str) -> str | None:
    cleaned = re.sub(r"\s+|\[[^\]]*\]", "", title).replace("·", "ㆍ").replace("･", "ㆍ")
    cleaned = cleaned.removeprefix("주요사항보고서(")
    if any(
        marker in cleaned
        for marker in ("자회사의주요경영사항", "종속회사의주요경영사항")
    ):
        return None
    return next(
        (
            endpoint
            for prefix, endpoint in _ENDPOINTS.items()
            if cleaned.startswith(prefix)
        ),
        None,
    )


def _corp_code(
    row: Mapping[str, Any],
    event: Mapping[str, Any],
    registry: Mapping[str, list[dict[str, str]]],
) -> str | None:
    def valid(code: Any) -> bool:
        return isinstance(code, str) and bool(re.fullmatch(r"[0-9]{8}", code))

    explicit = row.get("corp_code")
    if valid(explicit):
        return explicit
    entries = registry.get(normalize_dart_corp_name(event["company_name"]), [])
    matching = {
        entry["corp_code"]
        for entry in entries
        if valid(entry.get("corp_code"))
        and (not event["symbol"] or entry.get("stock_code") == event["symbol"])
    }
    return next(iter(matching)) if len(matching) == 1 else None


def _http_failure_code(status: int) -> str:
    """Closed failure code for a non-200 HTTP status.

    429 is rate limiting, so it is ``quota`` and retryable; other 4xx are
    request errors and 5xx are server errors.
    """
    if status == 429:
        return "quota"
    return "http_5xx" if status >= 500 else "http_4xx"


def _get_bytes(endpoint: str, params: Mapping[str, str]) -> bytes:
    api_key = settings.opendart_api_key
    if not api_key:
        raise DartCredentialError("OpenDART API key is missing")
    # http.client does not follow redirects. The Sentry scrubber covers crtfc_key.
    connection = HTTPSConnection("opendart.fss.or.kr", timeout=15)
    try:
        connection.request(
            "GET",
            f"/api/{endpoint}?" + urlencode({"crtfc_key": api_key, **params}),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, application/zip, */*",
            },
        )
        response = connection.getresponse()
        if response.status != 200:
            raise DartUnavailableError(
                f"OpenDART HTTP {response.status}",
                failure_code=_http_failure_code(response.status),
            )
        body = response.read(_MAX_BODY + 1)
        if len(body) > _MAX_BODY:
            raise DartDetailError(
                "OpenDART response exceeds size limit", failure_code="bad_response"
            )
        return body
    except TimeoutError:
        raise DartDetailError(
            "OpenDART request timed out", failure_code="timeout"
        ) from None
    except (OSError, HTTPException):
        raise DartDetailError(
            "OpenDART transport failed", failure_code="transport"
        ) from None
    finally:
        connection.close()


def _parse_xml(body: bytes) -> etree._Element:
    return etree.fromstring(
        body, parser=etree.XMLParser(resolve_entities=False, no_network=True)
    )


async def _request(
    endpoint: str,
    *,
    before_http: Callable[[], Awaitable[None]] | None = None,
    **params: str,
) -> dict[str, Any] | bytes | None:
    """Send one OpenDART request, retrying a 020 (rate limit) up to twice.

    ``before_http`` is awaited before every HTTP attempt, including the 020 retries,
    so a caller can budget real HTTP requests (hk 6408 round 8).
    """
    for attempt in range(3):
        if before_http is not None:
            await before_http()
        body = await asyncio.to_thread(_get_bytes, endpoint, params)
        try:
            if endpoint == "document.xml":
                if body.startswith(b"PK\x03\x04"):
                    return body
                status = _parse_xml(body).findtext("status")
            else:
                payload = json.loads(body)
                status = payload.get("status") if isinstance(payload, dict) else None
                if status == "000":
                    return payload
        except (ValueError, etree.XMLSyntaxError):
            raise DartDetailError(
                "OpenDART detail response is malformed", failure_code="bad_response"
            ) from None
        if status in ("013", "014"):
            return None
        if status == "020" and attempt < 2:
            await asyncio.sleep(2**attempt)
            continue
        if isinstance(status, str) and status in _ERRORS:
            raise _ERRORS[status](f"OpenDART detail status {status}")
        raise DartDetailError(
            "OpenDART detail response has an unexpected status",
            failure_code="bad_response",
        )
    raise AssertionError("unreachable")


async def request_list_page(
    *,
    before_http: Callable[[], Awaitable[None]] | None = None,
    **params: str,
) -> dict[str, Any] | None:
    """One OpenDART ``list.json`` page, or ``None`` when DART answers 013/014.

    The market-wide daily collector (hk 6408) pages through this. Every failure
    raises a :class:`DartDetailError` whose ``failure_code`` is closed.
    """
    payload = await _request("list.json", before_http=before_http, **params)
    if payload is not None and not isinstance(payload, dict):
        raise DartDetailError(
            "OpenDART list response has an unexpected shape",
            failure_code="bad_response",
        )
    return payload


async def filing_list_is_empty(**params: str) -> bool:
    """True only when ``list.json`` answers "no data" (013) for these params.

    ``False`` means DART reports filings; every other answer raises a
    :class:`DartDetailError` subclass.
    """
    return await _request("list.json", **params) is None


def _decimal(value: Any) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        number = Decimal(value.replace(",", "").strip().removesuffix("%"))
    except InvalidOperation:
        return None
    # Bound exponents and precision before arithmetic or plain-string formatting.
    if not number.is_finite() or len(number.as_tuple().digits) > 40:
        return None
    return (
        number
        if -28 <= number.as_tuple().exponent <= 20 and number.adjusted() < 20
        else None
    )


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})", value)
    try:
        return date(*map(int, match.groups())) if match else date.fromisoformat(value)
    except ValueError:
        return None


def normalize_capital_detail(
    endpoint: str,
    detail: Mapping[str, Any],
) -> dict[str, dict[str, str | None]]:
    """Return exact string values and their units keyed by metric name."""
    values: dict[str, dict[str, str | None]] = {}

    def add(
        metric: str, number: Decimal | None, unit: str, reason: str | None = None
    ) -> None:
        values[metric] = {
            "actual": format(number, "f") if number is not None else None,
            "unit": unit,
        }
        if reason:
            values[metric]["reason"] = reason

    def read(field: str) -> Decimal | None:
        return _decimal(detail.get(field))

    with localcontext() as ctx:
        ctx.prec = 40
        if endpoint in _OWNERSHIP:
            ratio_field, change_field = _OWNERSHIP[endpoint]
            after, change = read(ratio_field), read(change_field)
            add("holder_ratio_after", after, "%")
            add("holder_ratio_change", change, "percentage_point")
            before = (
                after - change if after is not None and change is not None else None
            )
            add("holder_ratio_before", before, "%")
        elif endpoint in _SHARE_CHANGES:
            prefix, metric, before_prefix = _SHARE_CHANGES[endpoint]
            for suffix, share_class in (("ostk", "common"), ("estk", "preferred")):
                add(f"{metric}_{share_class}", read(f"{prefix}_{suffix}_cnt"), "shares")
                add(
                    f"shares_before_{share_class}",
                    read(f"{before_prefix}_tisstk_{suffix}"),
                    "shares",
                )
        elif endpoint in _TREASURY:
            prefix, metric = _TREASURY[endpoint]
            for suffix, share_class in (("ostk", "common"), ("estk", "preferred")):
                add(
                    f"{metric}_shares_{share_class}",
                    read(f"{prefix}pln_stk_{suffix}"),
                    "shares",
                )
                add(
                    f"{metric}_amount_{share_class}",
                    read(f"{prefix}pln_prc_{suffix}"),
                    "KRW",
                )
        elif endpoint in _BONDS:
            price_field, count_field, ratio_field = _BONDS[endpoint]
            face, price = read("bd_fta"), read(price_field)
            shares, ratio = read(count_field), read(ratio_field)
            add("bond_face_amount", face, "KRW")
            add(
                "exchange_price" if endpoint == "exbdIsDecsn" else "conversion_price",
                price,
                "KRW/share",
            )
            add(
                "exchange_shares" if endpoint == "exbdIsDecsn" else "conversion_shares",
                shares,
                "shares",
            )
            if endpoint != "exbdIsDecsn":
                floor = read("act_mktprcfl_cvprc_lwtrsprc")
                add("conversion_reset_floor", floor, "KRW/share")
                add("filed_dilution", ratio, "%")
                # The guide reports new shares and their percentage of total
                # shares. This denominator is implied and inherits filing rounding.
                outstanding = (
                    shares * 100 / ratio
                    if shares and shares > 0 and ratio and ratio > 0
                    else None
                )
                add("implied_shares_outstanding", outstanding, "shares")
                missing = next(
                    (
                        name
                        for name, number in (
                            ("face_amount", face),
                            ("reset_floor", floor),
                            ("shares_outstanding", outstanding),
                        )
                        if number is None or number <= 0
                    ),
                    None,
                )
                dilution = None
                if (
                    missing is None
                    and face is not None
                    and floor is not None
                    and outstanding is not None
                ):
                    dilution = face / floor / outstanding * 100
                add(
                    "worst_case_dilution",
                    dilution,
                    "%",
                    f"missing_or_invalid_{missing}" if missing else None,
                )
    return values


def _dividend_record_date(
    body: bytes, receipt: str, *, allow_generic: bool
) -> date | None:
    """Read an explicit date in the labelled record-date table cell."""
    try:
        with ZipFile(BytesIO(body)) as archive:
            name = f"{receipt}.xml"
            if name not in archive.namelist():
                return None
            if archive.getinfo(name).file_size > _MAX_BODY:
                raise DartDetailError("OpenDART filing XML exceeds size limit")
            root = _parse_xml(archive.read(name))
    except DartDetailError:
        raise
    except (BadZipFile, etree.XMLSyntaxError, RuntimeError):
        raise DartDetailError("OpenDART filing document is malformed") from None
    dates: set[date] = set()
    for row in root.iter("TR"):
        cells = [
            "".join(cell.itertext()).strip()
            for cell in row
            if cell.tag in ("TD", "TH", "TU")
        ]
        for label, value in zip(cells[:-1], cells[1:], strict=True):
            label = re.sub(r"\s+", "", label)
            if re.fullmatch(r"(?:\d+\.)?배당기준일", label) or (
                allow_generic and re.fullmatch(r"(?:\d+\.)?기준일", label)
            ):
                parsed = _date(
                    re.sub(r"\s+", "", value)
                    .replace("년", "-")
                    .replace("월", "-")
                    .removesuffix("일")
                )
                if parsed:
                    dates.add(parsed)
    return next(iter(dates)) if len(dates) == 1 else None


async def enrich_capital_event(
    endpoint: str,
    row: Mapping[str, Any],
    event: dict[str, Any],
    registry: Mapping[str, list[dict[str, str]]] | None,
    fiscal_months: dict[str, str | None],
) -> list[dict[str, Any]]:
    receipt = event["source_event_id"]
    receipt_day = _date(receipt[:8])
    if not re.fullmatch(r"[0-9]{14}", receipt) or receipt_day is None:
        return []
    code = _corp_code(row, event, registry or {})
    if code is None and registry is None and event["company_name"]:
        raise DartDetailError("OpenDART corporate registry is unavailable")
    if endpoint == "document":
        body = await _request("document.xml", rcept_no=receipt)
        title = re.sub(r"\s+", "", event["title"])
        record_date = (
            _dividend_record_date(
                body,
                receipt,
                allow_generic="배당기준일" in title
                or "배당을위한주주명부폐쇄" in title,
            )
            if isinstance(body, bytes)
            else None
        )
        if record_date is None:
            return []
        values: dict[str, dict[str, str | None]] = {
            "dividend_record_date": {
                "actual": record_date.strftime("%Y%m%d"),
                "unit": "YYYYMMDD",
            },
        }
        if code:
            if code not in fiscal_months:
                profile = await _request("company.json", corp_code=code)
                month = profile.get("acc_mt") if isinstance(profile, dict) else None
                fiscal_months[code] = (
                    month
                    if isinstance(month, str) and re.fullmatch(r"0[1-9]|1[0-2]", month)
                    else None
                )
            if month := fiscal_months[code]:
                values["fiscal_year_end_month"] = {
                    "actual": str(int(month)),
                    "unit": "month",
                }
    else:
        if code is None:
            return []
        params = {"corp_code": code}
        if endpoint not in _OWNERSHIP:
            # Major-report ranges are keyed by initial receipt date. Use at
            # most a year through this receipt, including recent amendments.
            params.update(
                bgn_de=date.fromordinal(
                    max(date(2015, 1, 1).toordinal(), receipt_day.toordinal() - 365)
                ).strftime("%Y%m%d"),
                end_de=receipt_day.strftime("%Y%m%d"),
            )
        payload = await _request(f"{endpoint}.json", **params)
        if payload is None:
            return []
        rows = payload.get("list") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or any(
            not isinstance(item, dict) for item in rows
        ):
            raise DartDetailError("OpenDART detail response has an invalid list")
        matches = [
            item
            for item in rows
            if item.get("rcept_no") == receipt and item.get("corp_code") == code
        ]
        if not matches:
            return []
        if len(matches) != 1:
            raise DartDetailError("OpenDART detail response has duplicate receipt rows")
        detail = matches[0]
        if endpoint == "elestock":
            stated = _date(detail.get("rcept_dt"))
            event["event_date"] = max(
                receipt_day, event["event_date"], stated or receipt_day
            )
        values = normalize_capital_detail(endpoint, detail)
        if endpoint in ("cvbdIsDecsn", "bdwtIsDecsn"):
            basis = detail.get("act_mktprcfl_cvprc_lwtrsprc_bs")
            if isinstance(basis, str) and basis.strip() not in ("", "-"):
                event["raw_payload_json"]["dart_reset_floor_basis"] = basis

    return [
        {
            "metric_name": name,
            "unit": value["unit"],
            "value_details": {
                key: item for key, item in value.items() if key != "unit"
            },
        }
        for name, value in values.items()
    ]
