"""WiseFn KR earnings calendar fetch helper (ROB-171 / ROB-183).

This module exposes a single public coroutine, `fetch_wisefn_earnings_for_date`,
that returns a list of row dicts shaped for `normalize_wisefn_earnings_row`.

The live HTTP fetch is encapsulated in two module-level seams:
  - `_http_get_monthly(ym)` — raw HTTP GET → raw bytes (patchable in tests).
  - `_fetch_calendar_payload(target_date)` — retry + decode + map, calls the above.

Tests that only need the parsing layer patch `_fetch_calendar_payload` directly
with ``AsyncMock(return_value=SAMPLE_PAYLOAD)`` (existing pattern).  Tests that
exercise the HTTP/decode/map path patch ``_http_get_monthly`` with raw fixture
bytes instead.  Neither path calls the live WiseFn endpoint from CI.

Production runs are additionally gated behind ``settings.wisefn_earnings_enabled``
in the CLI.

Row shape returned by `fetch_wisefn_earnings_for_date`:

    {
        "stock_code": "005930",          # KR 6-digit ticker
        "corp_name": "삼성전자",
        "release_date": "2026-05-13",    # ISO date string
        "fiscal_year": 2026,
        "fiscal_quarter": 1,
        "release_type": "scheduled",     # or "released"
        "title": None,
        "time_hint": "after_close",      # before_open|after_close|during_market|unknown
    }

WiseFn raw JSON response structure (euc-kr encoded bytes, ROB-183 verified):
    Endpoint:
        https://www.wisereport.co.kr/wiseCalendar/GetCalendarAjax.aspx
            ?call_typ=2&param1=<YYYYMM>&param2=
    Response: a JSON array of earnings-schedule objects.  Each raw element has:
        Current fields observed on the live endpoint:
        - CMP_CD     : KR ticker prefixed with "A" (e.g. "A005930")
        - CMP_NM_KOR : Korean company name, sometimes suffixed with " (연결)"
        - WK_DT      : announcement date with weekday (e.g. "2026-05-13 (수)")
        - VAL05      : fiscal period (e.g. "2026/03(분기)")

        Historical fields still accepted for fixture/backward compatibility:
        - jongcode   : 6-digit KR stock code (string)
        - jongname   : company name (Korean, may be empty)
        - expect_dt  : expected announcement date  YYYYMMDD   (string)
        - confirm_dt : confirmed date YYYYMMDD if released, else empty string
        - gyulsan_ym : fiscal period end YYYYMM (e.g. "202603" = Q1 FY2026)
        - expect_time: "1"=before open, "2"=after close, "3"=during market
        - pub_yn     : "Y"=released/published, "N"=scheduled
    Field names are matched case-insensitively by `_parse_raw_wisefn_items`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_WISEFN_BASE_URL = "https://www.wisereport.co.kr"
_WISEFN_CALENDAR_PATH = "/wiseCalendar/GetCalendarAjax.aspx"
_WISEFN_TIMEOUT_SEC = 20.0
_WISEFN_MAX_RETRIES = 3
_WISEFN_BACKOFF_BASE_SEC = 1.0
_WISEFN_USER_AGENT = (
    "Mozilla/5.0 (compatible; auto-trader-market-events/1.0; "
    "+https://watcha.com/robots.txt)"
)

_TIME_CODE_TO_HINT: dict[str, str] = {
    "1": "before_open",
    "2": "after_close",
    "3": "during_market",
}


def _parse_raw_wisefn_items(
    raw_list: list[Any],
    *,
    as_of_ym: str | None = None,
) -> list[dict[str, Any]]:
    """Map raw WiseFn response items to the normalized row shape.

    Field name lookup is case-insensitive to tolerate minor API drift.
    Items that cannot be mapped to a usable release_date are silently skipped.
    """
    out: list[dict[str, Any]] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            logger.debug("wisefn: skipping non-dict item: %r", raw)
            continue

        item: dict[str, str] = {k.lower(): str(v) for k, v in raw.items()}

        stock_code = item.get("jongcode", "").strip()
        if not stock_code:
            stock_code = item.get("cmp_cd", "").strip().removeprefix("A")

        corp_name = item.get("jongname", "").strip()
        if not corp_name:
            corp_name = item.get("cmp_nm_kor", "").strip()
            if corp_name.endswith(" (연결)"):
                corp_name = corp_name[: -len(" (연결)")].strip()
        corp_name = corp_name or None

        pub_yn = item.get("pub_yn", "N").strip().upper()
        release_type = "released" if pub_yn == "Y" else "scheduled"

        dt_str = (
            item.get("confirm_dt", "").strip() if pub_yn == "Y" else ""
        ) or item.get("expect_dt", "").strip()

        release_date: str | None = None
        if dt_str and len(dt_str) == 8:
            try:
                release_date = datetime.strptime(dt_str, "%Y%m%d").date().isoformat()
            except ValueError:
                logger.debug("wisefn: bad date %r for %s", dt_str, stock_code)
        if not release_date:
            wk_dt = item.get("wk_dt", "").strip()
            if len(wk_dt) >= 10:
                try:
                    release_date = date.fromisoformat(wk_dt[:10]).isoformat()
                except ValueError:
                    logger.debug("wisefn: bad WK_DT %r for %s", wk_dt, stock_code)
        if not release_date and as_of_ym and len(as_of_ym) == 6:
            day_dt = item.get("day_dt", "").strip()
            if day_dt.isdigit():
                try:
                    release_date = date(
                        int(as_of_ym[:4]),
                        int(as_of_ym[4:]),
                        int(day_dt),
                    ).isoformat()
                except ValueError:
                    logger.debug("wisefn: bad DAY_DT %r for %s", day_dt, stock_code)

        if not release_date:
            logger.debug("wisefn: no usable date for item %r; skipping", item)
            continue

        fiscal_year: int | None = None
        fiscal_quarter: int | None = None
        gyulsan_ym = item.get("gyulsan_ym", "").strip()
        if not gyulsan_ym:
            val05 = item.get("val05", "").strip()
            if len(val05) >= 7 and val05[4:5] == "/":
                gyulsan_ym = f"{val05[:4]}{val05[5:7]}"
        if len(gyulsan_ym) == 6:
            try:
                fy_year = int(gyulsan_ym[:4])
                fy_month = int(gyulsan_ym[4:])
                fiscal_year = fy_year
                fiscal_quarter = {3: 1, 6: 2, 9: 3, 12: 4}.get(fy_month)
            except ValueError:
                pass

        expect_time = item.get("expect_time", "").strip()
        time_hint = _TIME_CODE_TO_HINT.get(expect_time, "unknown")

        out.append(
            {
                "stock_code": stock_code,
                "corp_name": corp_name,
                "release_date": release_date,
                "fiscal_year": fiscal_year,
                "fiscal_quarter": fiscal_quarter,
                "release_type": release_type,
                "title": None,
                "time_hint": time_hint,
            }
        )
    return out


async def _http_get_monthly(ym: str) -> bytes:
    """Perform one HTTP GET to the WiseFn monthly calendar endpoint.

    Module-level seam — tests patch this with ``AsyncMock(return_value=raw_bytes)``.
    Returns the raw response body bytes (not decoded).
    Raises ``httpx.HTTPStatusError`` on non-2xx, ``httpx.RequestError`` on
    network failures.
    """
    url = f"{_WISEFN_BASE_URL}{_WISEFN_CALENDAR_PATH}?call_typ=2&param1={ym}&param2="
    headers = {
        "User-Agent": _WISEFN_USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": f"{_WISEFN_BASE_URL}/",
        "X-Requested-With": "XMLHttpRequest",
    }
    async with httpx.AsyncClient(timeout=_WISEFN_TIMEOUT_SEC) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.content


async def _fetch_calendar_payload(target_date: date) -> dict[str, Any]:
    """Fetch and parse the WiseFn monthly earnings calendar for ``target_date``.

    Retries up to ``_WISEFN_MAX_RETRIES`` times with exponential backoff on
    transient HTTP / connection / JSON errors.  Raises ``RuntimeError`` after
    all retries are exhausted.

    Returns a dict of the form ``{"items": [...], "as_of_ym": "YYYYMM"}``
    where every item is already mapped to the normalized row shape expected by
    ``normalize_wisefn_earnings_row``.
    """
    ym = target_date.strftime("%Y%m")
    last_exc: BaseException | None = None
    for attempt in range(_WISEFN_MAX_RETRIES):
        if attempt:
            delay = _WISEFN_BACKOFF_BASE_SEC * (2 ** (attempt - 1))
            logger.info(
                "wisefn: retry %d/%d after %.1fs (prev error: %s)",
                attempt,
                _WISEFN_MAX_RETRIES - 1,
                delay,
                last_exc,
            )
            await asyncio.sleep(delay)
        try:
            raw_bytes = await _http_get_monthly(ym)
            text = raw_bytes.decode("euc-kr", errors="replace")
            data = json.loads(text)
            raw_list: list[Any] = data if isinstance(data, list) else []
            items = _parse_raw_wisefn_items(raw_list, as_of_ym=ym)
            logger.info(
                "wisefn: fetched %d raw items → %d mapped for ym=%s",
                len(raw_list),
                len(items),
                ym,
            )
            return {"items": items, "as_of_ym": ym}
        except (httpx.HTTPStatusError, httpx.RequestError, json.JSONDecodeError) as exc:
            last_exc = exc
            logger.warning("wisefn: attempt %d failed: %s", attempt + 1, exc)

    raise RuntimeError(
        f"wisefn: all {_WISEFN_MAX_RETRIES} attempts failed for ym={ym}"
    ) from last_exc


def _row_matches_date(row: dict[str, Any], target_date: date) -> bool:
    raw = row.get("release_date") or row.get("date")
    if not raw:
        return False
    try:
        return date.fromisoformat(str(raw)) == target_date
    except ValueError:
        return False


async def fetch_wisefn_earnings_for_date(target_date: date) -> list[dict[str, Any]]:
    """Return WiseFn earnings rows for one calendar day.

    The returned rows are passed through to
    `app.services.market_events.normalizers.normalize_wisefn_earnings_row`.
    """
    payload = await _fetch_calendar_payload(target_date)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        logger.warning(
            "wisefn payload missing 'items' list for %s; got keys=%s",
            target_date,
            list(payload.keys())
            if isinstance(payload, dict)
            else type(payload).__name__,
        )
        return []
    return [row for row in items if _row_matches_date(row, target_date)]
