#!/usr/bin/env python3
"""ROB-155 read-only smoke for /invest/api/feed/news.

GET-only endpoint validation. The script never prints credentials and does not
mutate DB, broker, order, watch, or scheduler state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

_DEFAULT_PATHS = (
    "/invest/api/feed/news?tab=latest&limit=20",
    "/invest/api/feed/news?tab=us&limit=20",
    "/invest/api/feed/news?tab=crypto&limit=20",
)
_ADDITIVE_FIELDS = ("scope", "tags", "category", "noiseReason")
# ROB-172: optional during the dual-emission window. After the backend rollout
# settles, a follow-up ticket should move "sourceMarket" into _ADDITIVE_FIELDS
# (required) and remove this constant. Do not flip in this PR.
_OPTIONAL_ADDITIVE_FIELDS_WARN = ("sourceMarket",)
_ALLOWED_SCOPES = {"market_wide", "symbol_specific", "mixed"}


@dataclass(frozen=True)
class SmokeResult:
    path: str
    ok: bool
    item_count: int
    warnings: list[str]
    errors: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "ok": self.ok,
            "item_count": self.item_count,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ROB-155 GET-only news feed smoke")
    parser.add_argument("--base-url", required=True, help="API base URL, e.g. https://example")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--auth-header-env",
        default=None,
        help="Optional env var containing an Authorization header value; value is never printed",
    )
    return parser.parse_args(argv)


def _items_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return [item for item in data["items"] if isinstance(item, dict)]
    return []


def validate_feed_payload(path: str, payload: Any) -> SmokeResult:
    """Validate additive ROB-155 fields and conservative quality warnings."""
    errors: list[str] = []
    warnings: list[str] = []
    items = _items_from_payload(payload)

    if not isinstance(payload, dict):
        errors.append("payload_not_object")
    if "items" not in payload and not (isinstance(payload.get("data"), dict) and "items" in payload["data"]):
        errors.append("missing_items")

    crypto_category_count = 0
    market_wide_big_tech_chip_warnings = 0
    source_market_missing_count = 0
    source_market_divergent_count = 0
    for idx, item in enumerate(items):
        for field in _ADDITIVE_FIELDS:
            if field not in item:
                errors.append(f"item_{idx}_missing_{field}")
        # ROB-172: optional warn loop for sourceMarket during dual-emission window.
        for field in _OPTIONAL_ADDITIVE_FIELDS_WARN:
            if field not in item:
                source_market_missing_count += 1
            elif field == "sourceMarket" and item.get("sourceMarket") != item.get("market"):
                source_market_divergent_count += 1
        scope = item.get("scope")
        if scope not in _ALLOWED_SCOPES:
            errors.append(f"item_{idx}_invalid_scope")
        if not isinstance(item.get("tags"), list):
            errors.append(f"item_{idx}_tags_not_list")
        if path.find("tab=crypto") >= 0 and item.get("category"):
            crypto_category_count += 1
        if path.find("tab=us") >= 0 and scope == "market_wide":
            related = item.get("relatedSymbols") or []
            if isinstance(related, list) and len(related) >= 3:
                market_wide_big_tech_chip_warnings += 1

    if path.find("tab=crypto") >= 0 and items and crypto_category_count == 0:
        warnings.append("crypto_items_present_but_no_category_distribution")
    if market_wide_big_tech_chip_warnings:
        warnings.append("market_wide_us_rows_still_have_many_related_symbols")
    if source_market_missing_count:
        warnings.append(f"source_market_missing_on_{source_market_missing_count}_items")
    if source_market_divergent_count:
        warnings.append(f"source_market_diverges_from_market_on_{source_market_divergent_count}_items")

    return SmokeResult(
        path=path,
        ok=not errors,
        item_count=len(items),
        warnings=warnings,
        errors=errors,
    )


def _fetch_json(base_url: str, path: str, timeout: float, auth_header: str | None) -> Any:
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    headers = {"Accept": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-supplied endpoint
        body = resp.read().decode("utf-8")
    return json.loads(body)


def run_smoke(base_url: str, timeout: float = 10.0, auth_header: str | None = None) -> list[SmokeResult]:
    results: list[SmokeResult] = []
    for path in _DEFAULT_PATHS:
        try:
            payload = _fetch_json(base_url, path, timeout, auth_header)
            results.append(validate_feed_payload(path, payload))
        except HTTPError as exc:
            results.append(SmokeResult(path, False, 0, [], [f"http_error:{exc.code}"]))
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            results.append(SmokeResult(path, False, 0, [], [type(exc).__name__]))
    return results


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    auth_header = os.environ.get(args.auth_header_env) if args.auth_header_env else None
    results = run_smoke(args.base_url, timeout=args.timeout, auth_header=auth_header)
    summary = {
        "read_only": True,
        "method": "GET",
        "paths": [r.path for r in results],
        "ok": all(r.ok for r in results),
        "results": [r.as_dict() for r in results],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
    except Exception as exc:
        print(f"news_feed_readonly_smoke failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from exc
