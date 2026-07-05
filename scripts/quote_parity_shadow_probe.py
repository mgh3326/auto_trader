#!/usr/bin/env python3
"""ROB-709 — read-only A/B parity shadow operator probe.

Compares the Toss ``prices()`` batch against the raw KIS batch layer over the
/invest KR+US universe and emits structured go/no-go JSON for the ROB-710
decision to flip batch current-price reads to Toss-first.

READ-ONLY: this script calls only price/quote GETs. It never calls
place/modify/cancel order APIs and never mutates a broker/order/watch path.

Dry-run-default: without --confirm-live, the script enumerates the universe
and prints the planned batch counts but performs ZERO network calls.

Example (dry-run, DB universe):
    uv run python -m scripts.quote_parity_shadow_probe --user-id 1

Example (live read-only, ROB-708 already landed on this branch):
    uv run python -m scripts.quote_parity_shadow_probe \
      --user-id 1 --us-kis-live-last --confirm-live

Exit codes: go=0, no_go|blocked=2, crash=1.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.symbol import to_db_symbol
from app.models.manual_holdings import MarketType
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.quote_parity_shadow import run_quote_parity_probe

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.brokers.toss.client import TossReadClient
    from app.services.invest_quote_service import InvestQuoteService

logger = logging.getLogger(__name__)

# Mirrors the secret-reject pattern in
# scripts/diagnose_invest_screener_toss_parity.py:36-68 — a --symbols-file must
# never carry cookies, auth headers, or tokens (it is an operator-supplied
# canary list, not a credential channel).
_SENSITIVE_HEADER_RE = re.compile(
    r"(cookie|authorization|x[-_]?csrf|token|secret|password|session)", re.I
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(bearer\s+[A-Za-z0-9._~+/-]+|cookie\s*:|authorization\s*:"
    r"|token=|secret=|password=|session=)",
    re.I,
)


def _reject_if_sensitive(label: str, value: Any) -> None:
    text = str(value or "")
    if _SENSITIVE_HEADER_RE.search(label) or _SENSITIVE_VALUE_RE.search(text):
        raise ValueError(
            "--symbols-file must not contain cookies, headers, tokens, or "
            "secrets; remove sensitive fields and retry."
        )


def load_symbols_file(path: Path) -> tuple[list[str], list[str]]:
    """Load a --symbols-file (CSV/JSON) of {market, symbol} rows.

    Secret-rejected. Returns (kr_symbols, us_symbols), de-duped and
    to_db_symbol-normalized (e.g. BRK-B / BRK/B -> BRK.B) so dotted-symbol
    coverage (BRK.B) can be forced onto the probe regardless of input format.
    """
    raw_text = path.read_text(encoding="utf-8-sig")
    _reject_if_sensitive("file", raw_text)

    rows: list[dict[str, Any]]
    if path.suffix.lower() == ".json":
        parsed = json.loads(raw_text)
        if not isinstance(parsed, list):
            raise ValueError(
                "--symbols-file JSON must be a list of {market, symbol} rows"
            )
        rows = [r for r in parsed if isinstance(r, dict)]
    else:
        reader = csv.DictReader(raw_text.splitlines())
        for field in reader.fieldnames or []:
            _reject_if_sensitive(field, "")
        rows = list(reader)

    kr: list[str] = []
    us: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        for key, value in row.items():
            _reject_if_sensitive(str(key), value)
        market = str(row.get("market") or "").strip().upper()
        symbol_raw = str(row.get("symbol") or "").strip()
        if not symbol_raw or market not in {"KR", "US"}:
            continue
        symbol = to_db_symbol(symbol_raw)
        dedupe_key = (market, symbol)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        (kr if market == "KR" else us).append(symbol)
    return kr, us


async def enumerate_db_universe(
    session: AsyncSession, *, user_id: int, limit: int
) -> tuple[list[str], list[str]]:
    """Default universe: distinct active Toss manual_holdings tickers, split
    KR/US — the exact production /invest hot-path symbols."""
    holdings = await ManualHoldingsService(session).get_holdings_by_user(
        user_id, broker_type="toss"
    )
    kr = [
        to_db_symbol(h.ticker) for h in holdings if h.market_type == MarketType.KR
    ][:limit]
    us = [
        to_db_symbol(h.ticker) for h in holdings if h.market_type == MarketType.US
    ][:limit]
    return kr, us


def exit_code_for(decision: str) -> int:
    return {"go": 0, "no_go": 2, "blocked": 2}.get(decision, 1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ROB-709 read-only A/B parity shadow: Toss prices() batch vs the "
            "raw KIS batch layer, over the /invest KR+US universe."
        )
    )
    parser.add_argument(
        "--symbols-file",
        type=Path,
        default=None,
        help="CSV/JSON of {market, symbol} rows; overrides --user-id enumeration.",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Enumerate the DB universe via ManualHoldingsService for this user.",
    )
    parser.add_argument("--limit", type=int, default=200, help="Cap per market.")
    parser.add_argument(
        "--allowlist",
        type=str,
        default="",
        help="Comma-separated symbols allowlisted as known coverage misses.",
    )
    parser.add_argument(
        "--us-kis-live-last",
        action="store_true",
        default=False,
        help=(
            "Pass ONLY after ROB-708 has landed (KIS US layer reads live-last, "
            "not daily close) — else US divergence is not a valid go-signal."
        ),
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        default=False,
        help="Arm real Toss/KIS network reads. Without this flag: dry-run only.",
    )
    parser.add_argument(
        "--json", action="store_true", default=False, help="(reserved) force JSON output."
    )
    return parser.parse_args(argv)


def _build_live_clients(
    session: AsyncSession,
) -> tuple[TossReadClient, InvestQuoteService]:
    """Construct REAL read-only clients. Called ONLY on the --confirm-live
    branch — never in dry-run (see test_dry_run_performs_no_network)."""
    from app.services.brokers.toss.client import TossReadClient
    from app.services.invest_home_readers import SafeKISClient
    from app.services.invest_quote_service import InvestQuoteService

    toss_client = TossReadClient.from_settings()
    kis_client = SafeKISClient()
    quote_service = InvestQuoteService(kis_client, session)
    return toss_client, quote_service


async def _resolve_universe(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    if args.symbols_file is not None:
        return load_symbols_file(args.symbols_file)
    if args.user_id is not None:
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            return await enumerate_db_universe(
                session, user_id=args.user_id, limit=args.limit
            )
    raise ValueError("Provide --symbols-file or --user-id to enumerate a universe")


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        kr_symbols, us_symbols = await _resolve_universe(args)
        allowlist = frozenset(
            s.strip() for s in args.allowlist.split(",") if s.strip()
        )

        if not args.confirm_live:
            # Dry-run: enumerate + print the plan. ZERO network calls.
            batches = 0
            if kr_symbols:
                batches += math.ceil(len(kr_symbols) / 200)
            if us_symbols:
                batches += math.ceil(len(us_symbols) / 200)
            report: dict[str, Any] = {
                "mode": "dry_run",
                "universe": {
                    "kr_count": len(kr_symbols),
                    "us_count": len(us_symbols),
                },
                "planned_toss_batches": batches,
            }
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        from app.core.cli import setup_logging_and_sentry
        from app.core.db import AsyncSessionLocal

        setup_logging_and_sentry(service_name="quote-parity-shadow-probe")

        async with AsyncSessionLocal() as session:
            toss_client, quote_service = _build_live_clients(session)
            try:
                report = await run_quote_parity_probe(
                    kr_symbols=kr_symbols,
                    us_symbols=us_symbols,
                    toss_prices_fn=toss_client.prices,
                    kis_kr_fetch=quote_service.kis_only_kr_prices,
                    kis_us_fetch=quote_service.kis_only_us_prices,
                    allowlist=allowlist,
                    us_kis_live_last=args.us_kis_live_last,
                )
            finally:
                await toss_client.aclose()

        report["mode"] = "live"
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return exit_code_for(report["go_no_go"]["decision"])
    except Exception as exc:  # noqa: BLE001 — crash must exit 1, never raise raw
        logger.exception("quote_parity_shadow_probe crashed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
