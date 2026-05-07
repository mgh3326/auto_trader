"""SPA shell router for /invest/ (ROB-141 desktop).

Serves the prebuilt React + Vite bundle from frontend/invest/dist/.
MUST NOT import broker/watch/redis/kis/upbit/task-queue. See safety test.
This router is registered AFTER invest_api and invest_app_spa, so
/invest/api/* and /invest/app/* take precedence.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invest", tags=["invest-web-spa"])

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "frontend" / "invest" / "dist"
INDEX_FILE = DIST_DIR / "index.html"
ASSETS_DIR = DIST_DIR / "assets"

_BUILD_MISSING_HTML = """\
<!doctype html>
<html><head><meta charset="utf-8"><title>/invest · build missing</title></head>
<body style="font:16px/1.6 ui-sans-serif,system-ui;max-width:680px;margin:4rem auto;padding:0 1rem;">
<h1>/invest · build missing</h1>
<p>The React bundle has not been built yet. Run:</p>
<pre><code>cd frontend/invest &amp;&amp; npm ci &amp;&amp; npm run build</code></pre>
</body></html>
"""


def _no_cache(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@router.get("/assets/{asset_path:path}", include_in_schema=False)
async def serve_asset(asset_path: str) -> FileResponse:
    candidate = (ASSETS_DIR / asset_path).resolve()
    try:
        candidate.relative_to(ASSETS_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
    if not candidate.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return FileResponse(candidate)


@router.get("/", include_in_schema=False)
async def spa_index() -> Response:
    return _serve_index()


@router.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str) -> Response:
    # Defensive: never shadow /invest/api/* or /invest/app/* if the router
    # somehow gets ordered above them.
    if full_path.startswith("api/") or full_path.startswith("app/"):
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
