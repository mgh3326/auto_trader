#!/usr/bin/env python3
"""hk 6395: Benzinga news shadow collector (JSONL, no DB writes).

Manual CLI. Polls Benzinga /api/v2/news per symbol and Finnhub company-news
for the same symbols in the same pass, appending one JSON record per article
to a JSONL file OUTSIDE the repo. A ``summary`` subcommand compares two
streams over a window.

Usage:
    uv run python -m scripts.benzinga_news_shadow poll \\
        --symbols NVDA,AMD --out-dir /tmp/benzinga-shadow [--limit 50]
    uv run python -m scripts.benzinga_news_shadow summary \\
        --input /tmp/benzinga-shadow/<poll>.jsonl [--poll-id <id>]

Gate: BENZINGA_NEWS_SHADOW_ENABLED must be exactly "true", otherwise the CLI
exits 2 without any network call. The key comes only from env
BENZINGA_API_KEY and never appears in logs, errors, or output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.core.cli import setup_logging_and_sentry
from app.services import benzinga_news as bz

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    poll = sub.add_parser("poll", help="poll both sources, append JSONL")
    poll.add_argument("--symbols", required=True, help="comma/space separated")
    poll.add_argument("--out-dir", required=True, help="outside the repo")
    poll.add_argument("--limit", type=int, default=bz.DEFAULT_PAGE_SIZE)
    poll.add_argument("--date-from", default=None)
    poll.add_argument("--date-to", default=None)
    poll.add_argument("--poll-id", default=None)
    poll.add_argument(
        "--skip-finnhub",
        action="store_true",
        help="Benzinga only (e.g. Finnhub quota exhausted)",
    )

    summary = sub.add_parser("summary", help="compare two streams in a JSONL file")
    summary.add_argument("--input", required=True, help="JSONL file from poll")
    summary.add_argument("--poll-id", default=None, help="restrict to one poll")

    return parser.parse_args(argv)


def _parse_symbols(raw: str) -> list[str]:
    seen: list[str] = []
    for token in raw.replace(",", " ").split():
        symbol = token.strip().upper()
        if symbol and symbol not in seen:
            seen.append(symbol)
    return seen


def _resolve_out_dir(raw: str) -> Path:
    """Refuse any path inside the repo — shadow output stays outside."""
    path = Path(raw).expanduser().resolve()
    try:
        path.relative_to(REPO_ROOT)
    except ValueError:
        return path
    raise ValueError(f"--out-dir must be outside the repo: {raw}")


def _check_gate() -> int | None:
    """Return exit code if gated off, else None. No network before this."""
    if not bz.shadow_enabled():
        print(
            f"{bz.SHADOW_ENABLED_ENV} is not exactly 'true'; "
            "shadow collection is disabled, no network call made",
            file=sys.stderr,
        )
        return 2
    return None


async def _poll_symbol(
    symbol: str,
    *,
    api_key: str,
    poll_id: str,
    now: datetime,
    limit: int,
    date_from: str | None,
    date_to: str | None,
    skip_finnhub: bool,
) -> tuple[list[dict], list[dict], list[str]]:
    benzinga_records: list[dict] = []
    finnhub_records: list[dict] = []
    errors: list[str] = []
    try:
        benzinga_records = await bz.fetch_benzinga_news(
            symbol,
            api_key=api_key,
            poll_id=poll_id,
            now=now,
            page_size=limit,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception as exc:  # noqa: BLE001 - per-source failure must not kill the pass
        errors.append(f"benzinga {symbol}: {type(exc).__name__}")
        logger.warning(
            "benzinga poll failed for %s: %s", symbol, bz.redact_token(str(exc))
        )
    if not skip_finnhub:
        try:
            from app.services.finnhub_news import fetch_news_finnhub

            result = await fetch_news_finnhub(symbol, "us", limit)
            for item in result.get("news", []):
                finnhub_records.append(
                    bz.normalize_finnhub_record(
                        item, poll_id=poll_id, first_received_at=now
                    )
                )
        except Exception as exc:  # noqa: BLE001 - same reason
            errors.append(f"finnhub {symbol}: {type(exc).__name__}")
            logger.warning("finnhub poll failed for %s: %s", symbol, type(exc).__name__)
    return benzinga_records, finnhub_records, errors


async def _run_poll(args: argparse.Namespace) -> int:
    symbols = _parse_symbols(args.symbols)
    if not symbols:
        print("no symbols given", file=sys.stderr)
        return 2
    try:
        out_dir = _resolve_out_dir(args.out_dir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        api_key = bz.read_api_key()
    except bz.BenzingaApiKeyMissing as exc:
        print(str(exc), file=sys.stderr)
        return 2

    poll_id = (
        args.poll_id
        or f"poll-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{poll_id}.jsonl"

    counts: dict[str, dict[str, int]] = {}
    errors: list[str] = []
    with open(out_path, "a", encoding="utf-8") as fh:
        for symbol in symbols:
            now = datetime.now(UTC)
            ben, fin, errs = await _poll_symbol(
                symbol,
                api_key=api_key,
                poll_id=poll_id,
                now=now,
                limit=args.limit,
                date_from=args.date_from,
                date_to=args.date_to,
                skip_finnhub=args.skip_finnhub,
            )
            errors.extend(errs)
            for record in ben + fin:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            counts[symbol] = {"benzinga": len(ben), "finnhub": len(fin)}
            # Explicit zeros stay visible: a symbol with no coverage is a
            # gap data point (the tickers=NVDA anomaly), not silence.
            logger.info(
                "poll %s %s: benzinga=%d finnhub=%d",
                poll_id,
                symbol,
                len(ben),
                len(fin),
            )

    report = {
        "poll_id": poll_id,
        "out_path": str(out_path),
        "counts": counts,
        "errors": errors,
    }
    print(json.dumps(report, sort_keys=True))
    if errors:
        print(f"errors: {'; '.join(errors)}", file=sys.stderr)
    if (
        all(v["benzinga"] == 0 and v["finnhub"] == 0 for v in counts.values())
        and errors
    ):
        return 1
    return 0


def _run_summary(args: argparse.Namespace) -> int:
    path = Path(args.input).expanduser()
    if not path.is_file():
        print(f"input not found: {args.input}", file=sys.stderr)
        return 2
    ben, fin = [], []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if args.poll_id and record.get("poll_id") != args.poll_id:
                continue
            if record.get("source") == "benzinga":
                ben.append(record)
            elif record.get("source") == "finnhub":
                fin.append(record)
    summary = bz.summarize_shadow(ben, fin)
    summary["poll_id"] = args.poll_id
    summary["input"] = str(path)
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    gated = _check_gate()
    if gated is not None:
        return gated
    setup_logging_and_sentry(service_name="benzinga-news-shadow")
    if args.command == "poll":
        return asyncio.run(_run_poll(args))
    if args.command == "summary":
        return _run_summary(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
