#!/usr/bin/env python3
"""Read-only ROB-145/ROB-155 quality gate runner for news_issue_lab.

This wrapper only calls the lab payload builder with --no-llm/--store disabled,
then writes local JSON/markdown artifacts. It does not mutate DB rows.

ROB-155 adds --mode tag-precision: evaluates US scope and crypto relevance
classification against deterministic labeled JSONL fixtures. No DB or LLM needed.
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

# Default fixture paths for tag-precision mode.
_DEFAULT_US_LABELS = Path("tests/data/news_us_tag_precision_labels.jsonl")
_DEFAULT_CRYPTO_LABELS = Path("tests/data/news_crypto_relevance_labels.jsonl")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ROB-145/ROB-155 read-only news quality gate evaluator"
    )
    parser.add_argument(
        "--mode",
        choices=["default", "tag-precision"],
        default="default",
        help="default: ROB-145 clustering quality; tag-precision: ROB-155 scope/category precision against JSONL fixtures",
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
    # ROB-155 tag-precision mode options.
    parser.add_argument(
        "--us-labels",
        default=str(_DEFAULT_US_LABELS),
        help="path to US tag-precision labeled JSONL fixture",
    )
    parser.add_argument(
        "--crypto-labels",
        default=str(_DEFAULT_CRYPTO_LABELS),
        help="path to crypto relevance labeled JSONL fixture",
    )
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
        "| market | status | duplicate title | duplicate topic | single article | single source | mismatch | source noise | suppressed | json | markdown |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for market in summary["markets"]:
        row = summary["results"][market]
        metrics = row["metrics"]
        lines.append(
            "| {market} | `{status}` | {duplicate_title} | {duplicate_topic} | {single_article} | {single_source} | {mismatch} | {source_noise} | {suppressed} | `{json_path}` | `{markdown_path}` |".format(
                market=market,
                status=row["status"],
                duplicate_title=metrics.get("duplicate_title_count_topn", 0),
                duplicate_topic=metrics.get("duplicate_topic_count_topn", 0),
                single_article=metrics.get("single_article_count_topn", 0),
                single_source=metrics.get("single_source_count_topn", 0),
                mismatch=metrics.get("market_mismatch_count_topn", 0),
                source_noise=metrics.get("source_noise_count_topn", 0),
                suppressed=metrics.get("suppressed_candidate_count", 0),
                json_path=row.get("json_path") or "-",
                markdown_path=row.get("markdown_path") or "-",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def _worst_status(statuses: list[str]) -> str:
    order = {"pass": 0, "warn": 1, "fail": 2}
    return max(statuses, key=lambda s: order.get(s, 2)) if statuses else "fail"


# ---------------------------------------------------------------------------
# ROB-155 tag-precision mode
# ---------------------------------------------------------------------------

def _load_jsonl(path: str) -> list[dict[str, Any]]:
    """Load newline-delimited JSON fixtures; return empty list if file missing."""
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rows.append(json.loads(line))
    return rows


def _run_us_tag_precision(fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate US scope classification against labeled fixtures (no DB/LLM)."""
    from app.services.news_entity_matcher import (
        classify_article_scope,
        match_symbols_for_article,
    )

    tp = fp = fn = 0
    demotion_correct = 0
    demotion_labeled = 0
    fp_examples: list[dict[str, Any]] = []
    fn_examples: list[dict[str, Any]] = []

    for row in fixtures:
        title = row.get("title") or ""
        summary = row.get("summary") or ""
        keywords = row.get("keywords") or []
        expected_scope = row.get("expected_scope") or "symbol_specific"
        expected_demoted = set(row.get("expected_demoted") or [])

        alias_matches = match_symbols_for_article(
            title=title, summary=summary, keywords=keywords, market="us"
        )
        result = classify_article_scope(
            title,
            summary=summary,
            keywords=keywords,
            market="us",
            matches=alias_matches,
        )
        predicted_scope = result.scope
        predicted_demoted = set(result.demoted_symbols)

        scope_correct = predicted_scope == expected_scope
        demotion_labeled += 1
        if predicted_demoted == expected_demoted:
            demotion_correct += 1
        else:
            fp_examples.append(
                {
                    "title": title[:100],
                    "expected_demoted": sorted(expected_demoted),
                    "got_demoted": sorted(predicted_demoted),
                }
            )
        if scope_correct:
            tp += 1
        else:
            if expected_scope == "market_wide" and predicted_scope == "symbol_specific":
                fn += 1
                fn_examples.append({"title": title[:100], "expected": expected_scope, "got": predicted_scope})
            else:
                fp += 1
                fp_examples.append({"title": title[:100], "expected": expected_scope, "got": predicted_scope})

    total = len(fixtures)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0

    demotion_accuracy = (
        demotion_correct / demotion_labeled if demotion_labeled else 0.0
    )

    return {
        "sample_count": total,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "demotion_accuracy": round(demotion_accuracy, 4),
        "fp_examples": fp_examples[:5],
        "fn_examples": fn_examples[:5],
    }


def _run_crypto_tag_precision(fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate crypto relevance scoring against labeled fixtures (no DB/LLM)."""
    from app.services.crypto_news_relevance_service import (
        score_crypto_news_article,
        user_facing_category,
    )

    tp = fp = fn = tn = 0
    fp_examples: list[dict[str, Any]] = []
    fn_examples: list[dict[str, Any]] = []
    category_distribution: dict[str, int] = {}

    for row in fixtures:
        expected_include = row.get("expected_include", True)
        relevance = score_crypto_news_article(row)
        predicted_include = relevance.include_in_briefing
        user_cat = user_facing_category(relevance.category)
        if user_cat:
            category_distribution[user_cat] = category_distribution.get(user_cat, 0) + 1

        if expected_include and predicted_include:
            tp += 1
        elif expected_include and not predicted_include:
            fn += 1
            fn_examples.append({
                "title": (row.get("title") or "")[:100],
                "score": relevance.score,
                "noise_reason": relevance.noise_reason,
            })
        elif not expected_include and predicted_include:
            fp += 1
            fp_examples.append({
                "title": (row.get("title") or "")[:100],
                "score": relevance.score,
                "category": user_cat,
            })
        else:
            tn += 1

    total = len(fixtures)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0

    return {
        "sample_count": total,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "category_distribution": category_distribution,
        "fp_examples": fp_examples[:5],
        "fn_examples": fn_examples[:5],
    }


def _tag_precision_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# ROB-155 tag-precision quality eval",
        "",
        f"- created_at: `{summary['created_at']}`",
        "- mode: `tag-precision`",
        "- safety: read-only; LLM disabled; no DB writes; no broker/order/watch paths.",
        "",
        "## US scope precision",
    ]
    us = summary.get("us", {})
    lines += [
        f"- sample_count: {us.get('sample_count', 0)}",
        f"- precision: {us.get('precision', 0):.2%}",
        f"- recall: {us.get('recall', 0):.2%}",
        f"- tp/fp/fn: {us.get('tp')}/{us.get('fp')}/{us.get('fn')}",
    ]
    if us.get("fp_examples"):
        lines += ["", "**FP examples (classified market_wide, expected symbol_specific):**"]
        for ex in us["fp_examples"]:
            lines.append(f"- `{ex.get('title', '')}` → got `{ex.get('got')}`, expected `{ex.get('expected')}`")
    lines += ["", "## Crypto relevance precision"]
    cr = summary.get("crypto", {})
    lines += [
        f"- sample_count: {cr.get('sample_count', 0)}",
        f"- precision: {cr.get('precision', 0):.2%}",
        f"- recall: {cr.get('recall', 0):.2%}",
        f"- tp/fp/fn/tn: {cr.get('tp')}/{cr.get('fp')}/{cr.get('fn')}/{cr.get('tn')}",
        f"- category_distribution: {cr.get('category_distribution', {})}",
    ]
    return "\n".join(lines).rstrip() + "\n"


async def _run_tag_precision_mode(args: argparse.Namespace) -> int:
    """Execute tag-precision evaluation against JSONL fixtures (no DB/LLM)."""
    created_at = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(
        args.output_dir or f"/tmp/news_issue_lab_quality_eval_{created_at}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    us_fixtures = _load_jsonl(args.us_labels)
    crypto_fixtures = _load_jsonl(args.crypto_labels)

    us_results = _run_us_tag_precision(us_fixtures)
    crypto_results = _run_crypto_tag_precision(crypto_fixtures)

    summary: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "tag-precision",
        "us_labels": args.us_labels,
        "crypto_labels": args.crypto_labels,
        "us": us_results,
        "crypto": crypto_results,
        "safety": {
            "read_only": True,
            "llm_disabled": True,
            "db_mutations": False,
            "broker_order_watch_paths": False,
        },
    }

    summary_json = output_dir / "summary.json"
    summary_md = output_dir / "summary.md"
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_md.write_text(_tag_precision_markdown(summary), encoding="utf-8")

    print(f"output_dir: {output_dir}")
    print(f"summary.json: {summary_json}")
    print(f"summary.md: {summary_md}")
    print(
        f"US precision={us_results.get('precision', 0):.2%}  "
        f"recall={us_results.get('recall', 0):.2%}  "
        f"n={us_results.get('sample_count', 0)}"
    )
    print(
        f"Crypto precision={crypto_results.get('precision', 0):.2%}  "
        f"recall={crypto_results.get('recall', 0):.2%}  "
        f"n={crypto_results.get('sample_count', 0)}"
    )
    return 0


async def async_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # ROB-155: tag-precision mode is fully local/deterministic.
    if args.mode == "tag-precision":
        return await _run_tag_precision_mode(args)
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
