"""Response builders for MCP screening results."""

from __future__ import annotations

import datetime
from typing import Any


def _empty_rsi_enrichment_diagnostics() -> dict[str, Any]:
    return {
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "rate_limited": 0,
        "timeout": 0,
        "error_samples": [],
    }


def _finalize_rsi_enrichment_diagnostics(
    diagnostics: dict[str, Any],
    statuses: list[str],
    errors: list[str | None],
) -> dict[str, Any]:
    diagnostics["succeeded"] = sum(1 for status in statuses if status == "success")
    diagnostics["failed"] = sum(1 for status in statuses if status == "error")
    diagnostics["rate_limited"] = sum(
        1 for status in statuses if status == "rate_limited"
    )
    diagnostics["timeout"] = sum(1 for status in statuses if status == "timeout")

    samples: list[str] = []
    for error in errors:
        if not error:
            continue
        samples.append(str(error)[:100])
        if len(samples) >= 3:
            break

    diagnostics["error_samples"] = samples
    return diagnostics


def _build_screen_response(
    results: list[dict[str, Any]],
    total_count: int,
    filters_applied: dict[str, Any],
    market: str,
    rsi_enrichment: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    meta_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the final screening response."""
    diagnostics = _empty_rsi_enrichment_diagnostics()
    if rsi_enrichment:
        diagnostics.update(
            {
                "attempted": int(rsi_enrichment.get("attempted", 0) or 0),
                "succeeded": int(rsi_enrichment.get("succeeded", 0) or 0),
                "failed": int(rsi_enrichment.get("failed", 0) or 0),
                "rate_limited": int(rsi_enrichment.get("rate_limited", 0) or 0),
                "timeout": int(rsi_enrichment.get("timeout", 0) or 0),
                "error_samples": [
                    str(message)[:100]
                    for message in (rsi_enrichment.get("error_samples") or [])[:3]
                ],
            }
        )

    response_meta: dict[str, Any] = {"rsi_enrichment": diagnostics}
    if meta_fields:
        response_meta.update(meta_fields)

    response: dict[str, Any] = {
        "results": results,
        "total_count": total_count,
        "returned_count": len(results),
        "filters_applied": filters_applied,
        "market": market,
        "meta": response_meta,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }

    if warnings:
        response["warnings"] = warnings

    return response
