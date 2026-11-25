"""Benzinga Pro scanner capture through the logged-in aside browser (hk 6573).

Runs only on the operator's Mac, where the aside browser holds the operator's own
Benzinga Pro trial session (decisions hk:doc
decision/2026-11-25/at-benzinga-pro-session-use and
decision/2026-11-25/at-benzinga-pro-capture-and-trading-use).

Fail-closed rules (PR #2595 review): no capture outside XNYS trading days, no
POST of a failed or partial side, no redirects on the ingest POST, and the token
only goes to https (or loopback http) without userinfo.

The scanner request runs *inside* the logged-in page through ``aside repl``. The
in-page JavaScript reads the session key from the page's own resource timings,
uses it for the fetch, and returns only the scanner rows. The key never leaves
the page: it is not returned, printed, logged or written anywhere by this script.

Output:
- one JSON line appended to ``--out-dir/benzinga-pro-<UTC date>.jsonl`` (the
  payload, which has no key field), and
- unless ``--dry-run``, a POST of the same payload to the AT ingest route
  (hk 6579) with the token from ``BENZINGA_PRO_INGEST_TOKEN``.

Payload schema ``benzinga-pro-capture/v1`` (fixed with hk 6579):
movers.up / movers.down rows carry decimal strings or null; rows whose session
change is exactly -100 have no session price and are sent with
``excluded_reason="no_session_price"``. ``breaking_events`` is empty in v1
(extension point).

Usage::

    uv run python -m scripts.benzinga_pro_capture --dry-run
    uv run python -m scripts.benzinga_pro_capture --out-dir ~/work/herdr-inbox/benzinga-pro
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = "benzinga-pro-capture/v1"
DASHBOARD_URL = "https://pro.benzinga.com/dashboard"
SCANNER_URL = "https://data-api-pro.benzinga.com/rest/v2/scanner/us/equity/scan"
PAGE_SIZE = 25
RESULT_MARK = "__RESULT__"
ET = ZoneInfo("America/New_York")

# session -> (change field, volume field) on the Pro scanner (verified 2026-11-25)
SESSION_FIELDS = {
    "us_pre": ("preMarketChangePercent", "preMarketVolume"),
    "us_regular": ("changePercent", "dayVolume"),
    "us_post": ("changePercent", "dayVolume"),
}

RunJs = Callable[[str, int], str]


def _is_xnys_session(day: dt.date) -> bool:
    """XNYS trading day, fail-closed (the shared AT calendar never raises)."""
    from app.services.market_events.session_calendar import is_trading_session

    return is_trading_session("us", day)


def us_session(
    now_utc: dt.datetime,
    *,
    is_trading_day: Callable[[dt.date], bool] = _is_xnys_session,
) -> str | None:
    """US equity session for ``now_utc`` by the ET clock on XNYS trading days.

    Weekends, market holidays and days the calendar cannot confirm return
    ``None`` (fail-closed): on those days the scanner only serves stale values.
    """
    et = now_utc.astimezone(ET)
    if et.weekday() >= 5 or not is_trading_day(et.date()):
        return None
    minutes = et.hour * 60 + et.minute
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return "us_pre"
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "us_regular"
    if 16 * 60 <= minutes < 20 * 60:
        return "us_post"
    return None


def build_js(
    *, change_field: str, volume_field: str, volume_floor: int, pages: int
) -> str:
    """In-page JS. It returns rows only; the key stays a local inside evaluate()."""
    fields = (
        f"symbol,{change_field},{volume_field},price,previousClose,marketCap,sector"
    )
    arg = {
        "base": SCANNER_URL,
        "fields": fields,
        "filter": f"{volume_field} > {volume_floor}",
        "sortField": change_field,
        "pageSize": PAGE_SIZE,
        "pages": pages,
    }
    return (
        f"const page=await openTab({json.dumps(DASHBOARD_URL)});"
        "await page.waitForTimeout(5000);"
        "const r=await page.evaluate(async (a)=>{"
        "const names=performance.getEntriesByType('resource').map(e=>e.name)"
        ".filter(x=>x.includes('data-api-pro'));"
        "const k=(names.map(u=>(u.match(/apikey=([^&]+)/)||[])[1]).find(Boolean))||'';"
        "if(!k) return {error:'no_session_in_page'};"
        "const out={};"
        "for (const dir of ['DESC','ASC']){"
        "let rows=[];let total=null;let err=null;"
        "for(let pg=0;pg<a.pages;pg++){"
        "const u=a.base+'?apikey='+k+'&fields='+encodeURIComponent(a.fields)"
        "+'&filter='+encodeURIComponent(a.filter)+'&pageSize='+a.pageSize"
        "+'&offset='+(pg*a.pageSize)+'&sortField='+a.sortField+'&sortDir='+dir;"
        "const res=await fetch(u);"
        "if(!res.ok){err=res.status;break;}"
        "const j=await res.json();"
        "if(!j||!Array.isArray(j.instruments)){err='no_instruments_array';break;}"
        "total=j.totalInstruments;"
        "const ins=j.instruments;rows=rows.concat(ins);"
        "if(ins.length<a.pageSize)break;}"
        "out[dir]={total:total,error:err,rows:rows};}"
        "return out;"
        f"}}, {json.dumps(arg)});"
        f"console.log({json.dumps(RESULT_MARK)}+JSON.stringify(r));"
    )


def run_aside(js: str, timeout: int) -> str:
    proc = subprocess.run(
        ["aside", "repl", js], capture_output=True, text=True, timeout=timeout
    )
    return (proc.stdout or "") + (proc.stderr or "")


def parse_result(output: str) -> dict:
    i = output.find(RESULT_MARK)
    if i == -1:
        raise RuntimeError("aside returned no result")
    line = output[i + len(RESULT_MARK) :].splitlines()[0]
    return json.loads(line)


def _dec(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return str(d)


def to_rows(raw: list[dict], *, change_field: str, volume_field: str) -> list[dict]:
    rows: list[dict] = []
    for rank, item in enumerate(raw, start=1):
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        change = _dec(item.get(change_field))
        excluded = (
            "no_session_price"
            if change is not None and Decimal(change) == Decimal("-100")
            else None
        )
        rows.append(
            {
                "symbol": symbol,
                "rank": rank,
                "change_pct": change,
                "volume": _dec(item.get(volume_field)),
                "price": _dec(item.get("price")),
                "previous_close": _dec(item.get("previousClose")),
                "market_cap": _dec(item.get("marketCap")),
                "sector": (str(item["sector"]) if item.get("sector") else None),
                "excluded_reason": excluded,
            }
        )
    return rows


def capture(
    *,
    session: str,
    volume_floor: int,
    pages: int,
    run_js: RunJs = run_aside,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    timeout: int = 240,
) -> dict:
    change_field, volume_field = SESSION_FIELDS[session]
    js = build_js(
        change_field=change_field,
        volume_field=volume_field,
        volume_floor=volume_floor,
        pages=pages,
    )
    result = parse_result(run_js(js, timeout))
    captured_at = now().isoformat().replace("+00:00", "Z")
    if "error" in result:
        raise RuntimeError(f"capture failed: {result['error']}")
    movers = {}
    errors = {}
    for side, direction in (("up", "DESC"), ("down", "ASC")):
        block = result.get(direction)
        if not isinstance(block, dict) or not isinstance(block.get("rows"), list):
            errors[side] = "missing_block"
            block = {}
        elif block.get("error") is not None:
            errors[side] = block["error"]
        movers[side] = to_rows(
            block.get("rows") or [],
            change_field=change_field,
            volume_field=volume_field,
        )
    payload = {
        "schema": SCHEMA,
        "captured_at": captured_at,
        "session": session,
        "scanner_request": {
            "fields": f"symbol,{change_field},{volume_field},price,previousClose,marketCap,sector",
            "filter": f"{volume_field} > {volume_floor}",
            "sort_field": change_field,
            "page_size": PAGE_SIZE,
            "pages": pages,
        },
        "movers": movers,
        "breaking_events": [],
    }
    return {"payload": payload, "errors": errors}


class IngestConfigError(ValueError):
    """Ingest configuration is unusable. The message names env vars only."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Any 3xx becomes an HTTPError, so the token is never re-sent elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def validate_ingest(url: str, token: str, header: str) -> None:
    """Fail before any request. Errors name the env var, never its value."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        raise IngestConfigError("BENZINGA_PRO_INGEST_URL must not carry userinfo")
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and host:
        pass
    elif parsed.scheme == "http" and host in _LOOPBACK_HOSTS:
        pass
    else:
        raise IngestConfigError(
            "BENZINGA_PRO_INGEST_URL must be https, or http to a loopback host"
        )
    if (
        not token
        or token != token.strip()
        or any(ord(c) < 33 or ord(c) > 126 for c in token)
    ):
        raise IngestConfigError(
            "BENZINGA_PRO_INGEST_TOKEN is empty or has whitespace/control/non-ASCII characters"
        )
    if not header or any(ord(c) < 33 or ord(c) > 126 or c == ":" for c in header):
        raise IngestConfigError(
            "BENZINGA_PRO_INGEST_TOKEN_HEADER is not a valid header name"
        )


def post(
    payload: dict, *, url: str, token: str, header: str, timeout: int = 30
) -> dict:
    validate_ingest(url, token, header)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", header: token},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310 - validated URL
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        name = type(exc).__name__
        raise RuntimeError(f"ingest request failed: {name}") from None
    raise RuntimeError(f"ingest request failed: HTTP {status}") from None


def ingest_env() -> tuple[str, str, str]:
    url = os.environ.get("BENZINGA_PRO_INGEST_URL", "")
    token = os.environ.get("BENZINGA_PRO_INGEST_TOKEN", "")
    header = os.environ.get(
        "BENZINGA_PRO_INGEST_TOKEN_HEADER", "X-Benzinga-Pro-Ingest-Token"
    )
    return url, token, header


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--session", choices=sorted(SESSION_FIELDS))
    parser.add_argument("--volume-floor", type=int, default=50000)
    parser.add_argument("--pages", type=int, default=4)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path.home() / "work" / "herdr-inbox" / "benzinga-pro",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.pages < 1 or args.pages > 20 or args.volume_floor < 0:
        parser.error("--pages must be 1..20 and --volume-floor >= 0")

    session = args.session or us_session(dt.datetime.now(dt.UTC))
    if session is None:
        print(json.dumps({"status": "outside_session"}))
        return 0

    # Preconditions before opening the browser: a missing or malformed ingest
    # config must not capture, write JSONL or reach the session.
    url, token, header = ingest_env()
    if not args.dry_run:
        missing = [
            n
            for n, v in (
                ("BENZINGA_PRO_INGEST_URL", url),
                ("BENZINGA_PRO_INGEST_TOKEN", token),
            )
            if not v
        ]
        if missing:
            print(json.dumps({"status": "not_posted", "missing_env": missing}))
            return 2
        try:
            validate_ingest(url, token, header)
        except IngestConfigError as exc:
            print(json.dumps({"status": "not_posted", "config_error": str(exc)}))
            return 2

    result = capture(session=session, volume_floor=args.volume_floor, pages=args.pages)
    payload = result["payload"]
    errors = result["errors"]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    day = payload["captured_at"][:10]
    # The local record keeps the failure; a failed or partial side is never
    # sent to AT, where an empty or truncated list would read as "no movers".
    with (args.out_dir / f"benzinga-pro-{day}.jsonl").open("a") as fh:
        fh.write(json.dumps({"payload": payload, "errors": errors}) + "\n")

    summary = {
        "status": "dry_run" if args.dry_run else "posted",
        "session": session,
        "captured_at": payload["captured_at"],
        "up": len(payload["movers"]["up"]),
        "down": len(payload["movers"]["down"]),
        "excluded": sum(
            1
            for side in ("up", "down")
            for r in payload["movers"][side]
            if r["excluded_reason"]
        ),
        "errors": errors,
    }
    if errors:
        if not args.dry_run:
            summary["status"] = "not_posted"
        print(json.dumps(summary))
        return 1
    if not args.dry_run:
        try:
            summary["ingest"] = post(payload, url=url, token=token, header=header)
        except (IngestConfigError, RuntimeError) as exc:
            summary["status"] = "post_failed"
            summary["post_error"] = str(exc)
            print(json.dumps(summary))
            return 1
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
