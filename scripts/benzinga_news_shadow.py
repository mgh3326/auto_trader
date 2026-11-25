#!/usr/bin/env python3
"""hk 6395: Benzinga news shadow collector (JSONL, no DB writes).

Manual CLI. Polls Benzinga /api/v2/news per symbol and Finnhub company-news
for the same symbols in the same pass, appending JSON records to a JSONL file
OUTSIDE the repo: one ``article`` row per story plus one ``poll_status`` row
per symbol per source (ok/empty row count, or the error class). A ``summary``
subcommand compares the two streams over a window.

Usage:
    uv run python -m scripts.benzinga_news_shadow poll \\
        --symbols NVDA,AMD --out-dir /tmp/benzinga-shadow [--limit 50]
    uv run python -m scripts.benzinga_news_shadow summary \\
        --input /tmp/benzinga-shadow/<poll>.jsonl [--poll-id <id>]

``--date-from/--date-to`` (``YYYY-MM-DD``, UTC calendar dates) narrow the
Finnhub side client-side by published time; Benzinga receives them as its
``dateFrom``/``dateTo`` request parameters. ``--input`` accepts several
files or a directory (``*.jsonl``) so a window of polls can be compared.

``first_received_at`` is stamped per source after that source's own response
arrives — never one shared stamp for both sides. Sequential fetching
inherently biases the difference toward the second source; the benchmark
reads latency as an ordering signal, not a calibrated race.

Gate: BENZINGA_NEWS_SHADOW_ENABLED must be exactly "true", otherwise the CLI
exits 2 without any network call. The key comes only from env
BENZINGA_API_KEY and never appears in logs, errors, or output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.core.cli import setup_logging_and_sentry
from app.services import benzinga_news as bz

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]

_POLL_ID_RE = re.compile(r"[A-Za-z0-9._-]+")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    poll = sub.add_parser("poll", help="poll both sources, append JSONL")
    poll.add_argument("--symbols", required=True, help="comma/space separated")
    poll.add_argument("--out-dir", required=True, help="outside the repo")
    poll.add_argument("--limit", type=int, default=bz.DEFAULT_PAGE_SIZE)
    poll.add_argument("--date-from", default=None, help="YYYY-MM-DD UTC, inclusive")
    poll.add_argument("--date-to", default=None, help="YYYY-MM-DD UTC, inclusive")
    poll.add_argument("--poll-id", default=None, help="[A-Za-z0-9._-]+")
    poll.add_argument(
        "--skip-finnhub",
        action="store_true",
        help="Benzinga only (e.g. Finnhub quota exhausted)",
    )

    summary = sub.add_parser("summary", help="compare two streams over JSONL input")
    summary.add_argument(
        "--input", required=True, nargs="+", help="JSONL file(s) or directories"
    )
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
    limit: int,
    date_from: str | None,
    date_to: str | None,
    skip_finnhub: bool,
) -> tuple[list[dict], list[str]]:
    """Poll one symbol, stamping each source after its own response."""
    rows: list[dict] = []
    errors: list[str] = []
    try:
        fetched = await bz.fetch_benzinga_news(
            symbol,
            api_key=api_key,
            poll_id=poll_id,
            now=datetime.now(UTC),
            page_size=limit,
            date_from=date_from,
            date_to=date_to,
        )
        seen = datetime.now(UTC)
        for record in fetched:
            record["query_symbol"] = symbol
            record["first_received_at"] = seen.astimezone(UTC).isoformat()
        rows.extend(fetched)
        rows.append(
            _status_row(
                source="benzinga",
                symbol=symbol,
                poll_id=poll_id,
                status="ok" if fetched else "empty",
                count=len(fetched),
                error_class=None,
                seen_at=seen,
            )
        )
    except Exception as exc:  # noqa: BLE001 - per-source failure must not kill the pass
        seen = datetime.now(UTC)
        errors.append(f"benzinga {symbol}: {type(exc).__name__}")
        logger.warning(
            "benzinga poll failed for %s: %s", symbol, bz.redact_token(str(exc))
        )
        rows.append(
            _status_row(
                source="benzinga",
                symbol=symbol,
                poll_id=poll_id,
                status="error",
                count=0,
                error_class=type(exc).__name__,
                seen_at=seen,
            )
        )
    if not skip_finnhub:
        try:
            from app.services.finnhub_news import fetch_news_finnhub

            result = await fetch_news_finnhub(symbol, "us", limit)
            seen = datetime.now(UTC)
            kept = 0
            for item in result.get("news", []):
                record = bz.normalize_finnhub_record(
                    item, poll_id=poll_id, first_received_at=seen
                )
                record["query_symbol"] = symbol
                if not _in_window(record, date_from, date_to):
                    continue
                record["first_received_at"] = seen.astimezone(UTC).isoformat()
                rows.append(record)
                kept += 1
            rows.append(
                _status_row(
                    source="finnhub",
                    symbol=symbol,
                    poll_id=poll_id,
                    status="ok" if kept else "empty",
                    count=kept,
                    error_class=None,
                    seen_at=seen,
                )
            )
        except Exception as exc:  # noqa: BLE001 - same reason
            seen = datetime.now(UTC)
            errors.append(f"finnhub {symbol}: {type(exc).__name__}")
            logger.warning("finnhub poll failed for %s: %s", symbol, type(exc).__name__)
            rows.append(
                _status_row(
                    source="finnhub",
                    symbol=symbol,
                    poll_id=poll_id,
                    status="error",
                    count=0,
                    error_class=type(exc).__name__,
                    seen_at=seen,
                )
            )
    return rows, errors


def _validate_poll_id(raw: str | None) -> str:
    """Restrict ``--poll-id`` to a safe filename pattern (or mint one)."""
    if raw is None:
        return (
            f"poll-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
            f"-{uuid.uuid4().hex[:8]}"
        )
    if not _POLL_ID_RE.fullmatch(raw):
        raise ValueError(
            f"--poll-id must match [A-Za-z0-9._-]+, got {raw!r} "
            "(absolute paths and ../ are refused)"
        )
    return raw


def _in_window(record: dict, date_from: str | None, date_to: str | None) -> bool:
    """Keep Finnhub articles whose UTC published date is inside the window."""
    if date_from is None and date_to is None:
        return True
    raw = record.get("created_utc")
    if not isinstance(raw, str):
        return False
    try:
        day = datetime.fromisoformat(raw).date().isoformat()
    except ValueError:
        return False
    if date_from is not None and day < date_from:
        return False
    return not (date_to is not None and day > date_to)


def _status_row(
    *,
    source: str,
    symbol: str,
    poll_id: str,
    status: str,
    count: int,
    error_class: str | None,
    seen_at: datetime,
) -> dict:
    return {
        "record_type": "poll_status",
        "source": source,
        "query_symbol": symbol,
        "poll_id": poll_id,
        "status": status,
        "count": count,
        "error_class": error_class,
        "first_received_at": seen_at.astimezone(UTC).isoformat(),
    }


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
        poll_id = _validate_poll_id(args.poll_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        api_key = bz.read_api_key()
    except bz.BenzingaApiKeyMissing as exc:
        print(str(exc), file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = (out_dir / f"{poll_id}.jsonl").resolve()
    try:
        out_path.relative_to(REPO_ROOT)
    except ValueError:
        pass
    else:
        print(f"output path must be outside the repo: {out_path}", file=sys.stderr)
        return 2

    counts: dict[str, dict[str, int]] = {}
    errors: list[str] = []
    with open(out_path, "a", encoding="utf-8") as fh:
        for symbol in symbols:
            rows, errs = await _poll_symbol(
                symbol,
                api_key=api_key,
                poll_id=poll_id,
                limit=args.limit,
                date_from=args.date_from,
                date_to=args.date_to,
                skip_finnhub=args.skip_finnhub,
            )
            errors.extend(errs)
            for record in rows:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            counts[symbol] = {
                "benzinga": sum(
                    1
                    for r in rows
                    if r.get("record_type") == "article"
                    and r.get("source") == "benzinga"
                ),
                "finnhub": sum(
                    1
                    for r in rows
                    if r.get("record_type") == "article"
                    and r.get("source") == "finnhub"
                ),
            }
            # Explicit zeros stay visible: a symbol with no coverage is a
            # gap data point (the tickers=NVDA anomaly), not silence.
            logger.info(
                "poll %s %s: benzinga=%d finnhub=%d",
                poll_id,
                symbol,
                counts[symbol]["benzinga"],
                counts[symbol]["finnhub"],
            )

    report = {
        "poll_id": poll_id,
        "out_path": str(out_path),
        "counts": counts,
        "errors": errors,
    }
    print(json.dumps(report, sort_keys=True))
    if errors:
        # A source outage must never look like a clean collection: the
        # shadow could otherwise collect no Benzinga data for days while
        # Dagu logs success.
        print(f"errors: {'; '.join(errors)}", file=sys.stderr)
        return 1
    return 0


def _iter_inputs(raw_inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in raw_inputs:
        path = Path(raw).expanduser()
        if path.is_dir():
            paths.extend(sorted(path.glob("*.jsonl")))
        else:
            paths.append(path)
    return paths


def _run_summary(args: argparse.Namespace) -> int:
    paths = _iter_inputs(args.input)
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        print(f"input not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    if not paths:
        print("no JSONL inputs", file=sys.stderr)
        return 2
    ben, fin = [], []
    for path in paths:
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
    summary["inputs"] = [str(p) for p in paths]
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
