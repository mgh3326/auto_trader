# scripts/news_relevance_judge_smoke.py
"""Operator/session plumbing for the news-relevance judgment loop (ROB-889).

This is **plumbing only — it contains no LLM and makes no relevance decision**.
The judgment itself (relationship / relevance / price_relevance / reason) is made
out-of-process by a Claude/Hermes session, honoring the ROB-501 LLM-ownership
boundary. This helper just does the safe HTTP glue around that session:

    fetch    GET  /trading/api/news-relevance/pending   (read-only)
    validate local pydantic check of a judgments JSON     (no network, no token)
    submit   POST /trading/api/news-relevance/ingest/bulk (mutation; --confirm)

Typical loop:
    1. uv run python -m scripts.news_relevance_judge_smoke --mode fetch \
           --market kr --limit 50 > pending.json
    2. <the session reads pending.json, judges, writes judgments.json>
    3. uv run python -m scripts.news_relevance_judge_smoke --mode validate \
           --file judgments.json
    4. uv run python -m scripts.news_relevance_judge_smoke --mode submit \
           --file judgments.json --confirm
    5. re-fetch / call get_news to confirm excluded_count rose and pending fell.

Safety:
    * The ingest token is read from ``NEWS_RELEVANCE_INGEST_TOKEN`` and is NEVER
      printed — only the presence/absence of the env key is reported.
    * ``submit`` mutates DB state (status transitions) and therefore requires an
      explicit ``--confirm``; without it the payload is validated and previewed
      but nothing is sent.
    * Server status is derived server-side (unrelated/low -> excluded); this
      script never sends ``status``.

See docs/runbooks/news-relevance-judgment.md for the judgment criteria.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

import httpx
from pydantic import ValidationError

from app.schemas.news_relevance import NewsRelevanceIngestRequest

_TOKEN_ENV = "NEWS_RELEVANCE_INGEST_TOKEN"
_TOKEN_HEADER_ENV = "NEWS_RELEVANCE_INGEST_TOKEN_HEADER"
_DEFAULT_TOKEN_HEADER = "X-News-Relevance-Ingest-Token"
_HOST_ENV = "NEWS_RELEVANCE_SMOKE_HOST"
_DEFAULT_HOST = "http://localhost:8000"

_PENDING_PATH = "/trading/api/news-relevance/pending"
_INGEST_PATH = "/trading/api/news-relevance/ingest/bulk"


class SmokeRejected(RuntimeError):
    """Raised when operator inputs violate a smoke safety boundary."""


def resolve_host(host: str | None, env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return (host or env.get(_HOST_ENV) or _DEFAULT_HOST).rstrip("/")


def resolve_token_header(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get(_TOKEN_HEADER_ENV) or _DEFAULT_TOKEN_HEADER).strip()


def require_ingest_token(env: dict[str, str] | None = None) -> str:
    """Return the ingest token, or raise reporting the KEY NAME only.

    The token value is never included in the error message or any output.
    """
    env = os.environ if env is None else env
    token = (env.get(_TOKEN_ENV) or "").strip()
    if not token:
        raise SmokeRejected(
            f"missing env {_TOKEN_ENV} (value never printed) — set it to the "
            "server's news-relevance ingest token"
        )
    return token


def build_auth_headers(
    token: str, *, env: dict[str, str] | None = None
) -> dict[str, str]:
    return {resolve_token_header(env): token}


def validate_judgments_payload(raw: Any) -> NewsRelevanceIngestRequest:
    """Validate a judgments payload against the server contract (offline).

    Accepts either the wrapped ``{"judgments": [...]}`` shape or a bare list of
    judgment objects. Raises ``SmokeRejected`` with a readable, index-tagged
    message on any enum/shape violation — mirrors the server's 422 so bad
    payloads are caught before they ever hit the network.
    """
    if isinstance(raw, list):
        payload: dict[str, Any] = {"judgments": raw}
    elif isinstance(raw, dict):
        payload = raw
    else:
        raise SmokeRejected(
            "judgments payload must be a JSON object with a 'judgments' list "
            "or a bare list of judgment objects"
        )
    try:
        return NewsRelevanceIngestRequest.model_validate(payload)
    except ValidationError as exc:
        raise SmokeRejected(f"invalid judgments payload:\n{exc}") from exc


def load_judgments_file(path: str) -> NewsRelevanceIngestRequest:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError as exc:
        raise SmokeRejected(f"judgments file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SmokeRejected(f"judgments file is not valid JSON: {path}\n{exc}") from exc
    return validate_judgments_payload(raw)


async def fetch_pending(
    *,
    host: str,
    token: str,
    market: str,
    limit: int,
    symbol: str | None = None,
    client: httpx.AsyncClient | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """GET the pending judgment batch. Read-only; token-authed."""
    params: dict[str, Any] = {"market": market, "limit": limit}
    if symbol:
        params["symbol"] = symbol
    headers = build_auth_headers(token, env=env)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        response = await client.get(
            f"{host}{_PENDING_PATH}", params=params, headers=headers
        )
    finally:
        if owns_client:
            await client.aclose()
    return _envelope(response)


async def submit_judgments(
    *,
    host: str,
    token: str,
    request: NewsRelevanceIngestRequest,
    client: httpx.AsyncClient | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """POST validated judgments to the ingest endpoint. Mutation; token-authed."""
    headers = build_auth_headers(token, env=env)
    body = request.model_dump(mode="json")
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60.0)
    try:
        response = await client.post(
            f"{host}{_INGEST_PATH}", json=body, headers=headers
        )
    finally:
        if owns_client:
            await client.aclose()
    return _envelope(response)


def _envelope(response: httpx.Response) -> dict[str, Any]:
    try:
        body: Any = response.json()
    except (json.JSONDecodeError, ValueError):
        body = response.text
    return {"http_status": response.status_code, "body": body}


def summarize_judgments(request: NewsRelevanceIngestRequest) -> dict[str, Any]:
    """Local, no-network preview: how many will confirm vs exclude (server rule)."""
    would_exclude = sum(
        1
        for j in request.judgments
        if j.relationship == "unrelated" or j.relevance == "low"
    )
    return {
        "judgments": len(request.judgments),
        "would_exclude": would_exclude,
        "would_confirm": len(request.judgments) - would_exclude,
    }


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


async def _run(args: argparse.Namespace) -> int:
    host = resolve_host(args.host)

    if args.mode == "validate":
        request = load_judgments_file(args.file)
        _print({"status": "valid", **summarize_judgments(request)})
        return 0

    if args.mode == "fetch":
        token = require_ingest_token()
        result = await fetch_pending(
            host=host,
            token=token,
            market=args.market,
            limit=args.limit,
            symbol=args.symbol,
        )
        _print(result)
        return 0 if result["http_status"] < 400 else 2

    if args.mode == "submit":
        request = load_judgments_file(args.file)
        preview = summarize_judgments(request)
        if not args.confirm:
            _print(
                {
                    "status": "dry_run",
                    "note": "pass --confirm to POST; nothing was sent",
                    "host": host,
                    **preview,
                }
            )
            return 0
        token = require_ingest_token()
        result = await submit_judgments(host=host, token=token, request=request)
        _print({"submitted": preview, **result})
        return 0 if result["http_status"] < 400 else 2

    raise SmokeRejected(f"unknown mode: {args.mode}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="news_relevance_judge_smoke",
        description="Plumbing for the news-relevance judgment loop (no LLM).",
    )
    parser.add_argument(
        "--mode", required=True, choices=("fetch", "validate", "submit")
    )
    parser.add_argument(
        "--host", default=None, help=f"default: ${_HOST_ENV} or {_DEFAULT_HOST}"
    )
    parser.add_argument("--market", default="kr", choices=("kr", "us", "crypto"))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--file", default=None, help="judgments JSON (validate/submit)")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="required to actually POST judgments (submit mode mutates DB state)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode in ("validate", "submit") and not args.file:
        raise SmokeRejected(f"--file is required for --mode {args.mode}")
    try:
        return asyncio.run(_run(args))
    except SmokeRejected as exc:
        _print({"status": "rejected", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
