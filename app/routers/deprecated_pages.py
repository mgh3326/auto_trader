from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(tags=["Deprecated Pages"])

LEGACY_PREFIXES = (
    "/manual-holdings",
    "/kis-domestic-trading",
    "/kis-overseas-trading",
    "/upbit-trading",
    "/dashboard",
    "/stock-latest",
    "/analysis-json",
    "/orderbook",
)
_DEPRECATED_AT = "2026-02-20T00:00:00+09:00"
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def legacy_exempt_url_patterns() -> tuple[re.Pattern[str], ...]:
    return tuple(
        re.compile(rf"^{re.escape(prefix)}(?:/|$)") for prefix in LEGACY_PREFIXES
    )


def _is_api_request(request: Request) -> bool:
    path = str(request.url.path)
    if "/api/" in path:
        return True

    accept = request.headers.get("accept", "").lower()
    if "application/json" in accept and "text/html" not in accept:
        return True

    return False


def _build_payload(prefix: str) -> dict[str, str]:
    return {
        "detail": (
            f"'{prefix}' has been permanently deprecated and is no longer available."
        ),
        "replacement_url": "/portfolio/",
        "deprecated_at": _DEPRECATED_AT,
    }


def _build_html(prefix: str, payload: dict[str, str]) -> str:
    return (
        "<!doctype html>"
        '<html lang="ko">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>410 Gone</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "background:#f5f3ef;color:#242220;margin:0;padding:24px;}"
        ".panel{max-width:760px;margin:40px auto;background:#fff;border:1px solid #d1ccc4;"
        "border-radius:12px;padding:24px;box-shadow:0 12px 24px rgba(0,0,0,.08);}"
        "h1{margin:0 0 12px;color:#b54a4a;}"
        "p{line-height:1.6;margin:8px 0;}"
        "a{color:#c05a3c;text-decoration:none;font-weight:600;}"
        "</style>"
        "</head>"
        "<body>"
        '<div class="panel">'
        "<h1>410 Gone</h1>"
        f"<p>{payload['detail']}</p>"
        f"<p>deprecated_at: {payload['deprecated_at']}</p>"
        f'<p>replacement: <a href="{payload["replacement_url"]}">{payload["replacement_url"]}</a></p>'
        f"<p>requested prefix: {prefix}</p>"
        "</div>"
        "</body>"
        "</html>"
    )


def _register_prefix(prefix: str) -> None:
    async def _handler(request: Request, path: str = ""):
        _ = path
        payload = _build_payload(prefix)
        if _is_api_request(request):
            return JSONResponse(status_code=410, content=payload)
        return HTMLResponse(status_code=410, content=_build_html(prefix, payload))

    router.add_api_route(
        prefix,
        _handler,
        methods=_METHODS,
        include_in_schema=False,
    )
    router.add_api_route(
        f"{prefix}/",
        _handler,
        methods=_METHODS,
        include_in_schema=False,
    )
    router.add_api_route(
        f"{prefix}/{{path:path}}",
        _handler,
        methods=_METHODS,
        include_in_schema=False,
    )


for _prefix in LEGACY_PREFIXES:
    _register_prefix(_prefix)
