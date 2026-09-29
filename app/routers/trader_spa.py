"""SPA shell router for /trader (task 889, stage 1).

Serves the prebuilt React + Vite second entry (frontend/invest/dist/
trader.html). The bundle's asset URLs share the /invest/app/assets/ prefix
emitted by the Vite ``base`` setting and are served by the existing
invest_app_spa asset route from the same dist/ directory, so this router only
needs the HTML entry points.

This module MUST NOT import any broker, watch, Redis, KIS, Upbit, or
task-queue module. See tests/test_trader_page_safety.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trader", tags=["trader-spa"])

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "frontend" / "invest" / "dist"
INDEX_FILE = DIST_DIR / "trader.html"

_BUILD_MISSING_HTML = """\
<!doctype html>
<html><head><meta charset="utf-8"><title>/trader · build missing</title></head>
<body style="font:16px/1.6 ui-sans-serif,system-ui;max-width:680px;margin:4rem auto;padding:0 1rem;">
<h1>/trader · build missing</h1>
<p>The React bundle has not been built yet. Run:</p>
<pre><code>cd frontend/invest &amp;&amp; npm ci &amp;&amp; npm run build</code></pre>
</body></html>
"""


def _no_cache(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@router.get("/", include_in_schema=False)
async def spa_index() -> Response:
    return _serve_index()


@router.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str) -> Response:
    # Defensive: never shadow a future /trader/api/* if the router somehow gets
    # ordered above an API router.
    if full_path.startswith("api/"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _serve_index()


def _serve_index() -> Response:
    if not INDEX_FILE.is_file():
        logger.warning(
            "SPA build missing at %s; returning 503 build-missing page", INDEX_FILE
        )
        return _no_cache(
            HTMLResponse(
                content=_BUILD_MISSING_HTML,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        )
    return _no_cache(FileResponse(INDEX_FILE, media_type="text/html"))
