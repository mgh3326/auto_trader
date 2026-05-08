#!/usr/bin/env python3
"""Read-only ROB-145 quality gate runner for news_issue_lab.

This wrapper only calls the lab payload builder with --no-llm/--store disabled,
then writes local JSON/markdown artifacts. It does not mutate DB rows.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts import news_issue_lab as lab

VALID_MARKETS = ("all", "kr", "us", "crypto")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ROB-145 read-only news_issue_lab quality gate evaluator"
    )
    parser.add_argument("--markets", default="all,kr,us,crypto")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=240)
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--quality-top", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=0.78)
    parser.add_argument("--dedupe-threshold", type=float, default=0.90)
    parser.add_argument("--embedding-endpoint", default=lab.DEFAULT_BGE_ENDPOINT)
    parser.add_argument("--embedding-model", default=lab.DEFAULT_BGE_MODEL)
    parser.add_argument(
        "--embedding-api-key",
        default=None,
        help="optional bearer token forwarded to news_issue_lab; defaults there to EMBEDDING_API_KEY",
    )
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument(
        "--compare-v1", dest="compare_v1", action="store_true", default=True
    )
    parser.add_argument("--no-compare-v1", dest="compare_v1", action="store_false")
    parser.add_argument(
        "--merge-clusters", dest="merge_clusters", action="store_true", default=True
    )
    parser.add_argument(
        "--no-merge-clusters", dest="merge_clusters", action="store_false"
    )
    parser.add_argument("--drop-regular-reports", action="store_true")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument(
        "--format", choices=["json", "markdown", "both"], default="both"
    )
    parser.add_argument("--fail-on-quality-fail", action="store_true")
    args = parser.parse_args(argv)
    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    invalid = sorted(set(markets) - set(VALID_MARKETS))
    if invalid:
        parser.error(f"invalid markets: {', '.join(invalid)}")
    if not markets:
        parser.error("--markets must include at least one market")
    for name in ("window_hours", "limit", "top", "quality_top", "batch_size"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    args.markets = markets
    return args


def _lab_args(args: argparse.Namespace, market: str) -> argparse.Namespace:
    return lab.parse_args(
        [
            "--market",
            market,
            "--window-hours",
            str(args.window_hours),
            "--limit",
            str(args.limit),
            "--top",
            str(args.top),
            "--quality-top",
            str(args.quality_top),
            "--threshold",
            str(args.threshold),
            "--dedupe-threshold",
            str(args.dedupe_threshold),
            "--embedding-endpoint",
            args.embedding_endpoint,
            "--embedding-model",
            args.embedding_model,
            *(
                ["--embedding-api-key", args.embedding_api_key]
                if args.embedding_api_key
                else []
            ),
            "--batch-size",
            str(args.batch_size),
            "--no-llm",
            *(["--compare-v1"] if args.compare_v1 else []),
            *(["--merge-clusters"] if args.merge_clusters else ["--no-merge-clusters"]),
            *(["--drop-regular-reports"] if args.drop_regular_reports else []),
            "--format",
            "json",
        ]
    )


def _summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# ROB-145 news_issue_lab quality eval",
        "",
        f"- created_at: `{summary['created_at']}`",
        f"- overall_status: `{summary['overall_status']}`",
        f"- window: {summary['window_hours']}h / limit: {summary['limit']} / top: {summary['top']} / quality_top: {summary['quality_top']}",
        "- safety: read-only lab run; LLM disabled; no --store used; no broker/order/watch/order-intent/scheduler/API/UI changes.",
        "",
        "| market | status | duplicate title | duplicate topic | single article | single source | mismatch | source noise | suppressed warn | suppressed audit | json | markdown |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for market in summary["markets"]:
        row = summary["results"][market]
        metrics = row["metrics"]
        lines.append(
            "| {market} | `{status}` | {duplicate_title} | {duplicate_topic} | {single_article} | {single_source} | {mismatch} | {source_noise} | {suppressed_warn} | {suppressed_audit} | `{json_path}` | `{markdown_path}` |".format(
                market=market,
                status=row["status"],
                duplicate_title=metrics.get("duplicate_title_count_topn", 0),
                duplicate_topic=metrics.get("duplicate_topic_count_topn", 0),
                single_article=metrics.get("single_article_count_topn", 0),
                single_source=metrics.get("single_source_count_topn", 0),
                mismatch=metrics.get("market_mismatch_count_topn", 0),
                source_noise=metrics.get("source_noise_count_topn", 0),
                suppressed_warn=metrics.get("suppressed_warning_count", 0),
                suppressed_audit=metrics.get("suppressed_audit_count", 0),
                json_path=row.get("json_path") or "-",
                markdown_path=row.get("markdown_path") or "-",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def _worst_status(statuses: list[str]) -> str:
    order = {"pass": 0, "warn": 1, "fail": 2}
    return max(statuses, key=lambda s: order.get(s, 2)) if statuses else "fail"


async def async_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    created_at = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(
        args.output_dir or f"/tmp/news_issue_lab_quality_eval_{created_at}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "window_hours": args.window_hours,
        "limit": args.limit,
        "top": args.top,
        "quality_top": args.quality_top,
        "markets": args.markets,
        "output_dir": str(output_dir),
        "results": {},
    }
    statuses: list[str] = []
    for market in args.markets:
        payload = await lab.build_payload(_lab_args(args, market))
        quality = payload.get("quality_gate") or {}
        status = str(quality.get("status") or "fail")
        statuses.append(status)
        json_path = output_dir / f"{market}.json"
        markdown_path = output_dir / f"{market}.md"
        if args.format in {"json", "both"}:
            json_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if args.format in {"markdown", "both"}:
            markdown_path.write_text(lab.render_markdown(payload), encoding="utf-8")
        summary["results"][market] = {
            "status": status,
            "metrics": quality.get("metrics") or {},
            "finding_count": len(quality.get("findings") or []),
            "json_path": str(json_path) if args.format in {"json", "both"} else None,
            "markdown_path": str(markdown_path)
            if args.format in {"markdown", "both"}
            else None,
        }
    summary["overall_status"] = _worst_status(statuses)
    summary_json = output_dir / "summary.json"
    summary_md = output_dir / "summary.md"
    summary_json.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary_md.write_text(_summary_markdown(summary), encoding="utf-8")
    print(summary_md)
    if args.fail_on_quality_fail and summary["overall_status"] == "fail":
        return 2
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(async_main()))
    except BrokenPipeError:
        raise SystemExit(0)
    except Exception as exc:  # pragma: no cover - operator-facing final guard
        print(f"news_issue_lab_quality_eval failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
