"""Public sector labels for KR symbols (yfinance ``Ticker.info``), cached once.

The operating DB's ``symbol_sectors`` is out of reach (localhost:5432 is a
frozen production copy -- never connected), so the sector benchmark uses the
public yfinance ``sector`` field. Coverage is partial (many KOSDAQ names come
back empty); a symbol without a sector, or with fewer than
``outcomes.MIN_PEERS`` priced peers, has a *missing* sector adjustment.
``quoteType == "ETF"`` marks names exempt from the sell tax.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from research.rob1383_buy_drought.prices import PriceStore

LOG = logging.getLogger(__name__)
CACHE_NAME = "sectors_kr.json"


def kr_symbols_in_cache(cache_dir: Path) -> list[str]:
    out = []
    for p in sorted(cache_dir.glob("kr__*.parquet")):
        sym = p.stem.split("__", 1)[1]
        if not sym.startswith("IDX_"):
            out.append(sym)
    return out


def load_or_fetch(
    cache_dir: Path, store: PriceStore, symbols: list[str], *, offline: bool
) -> dict[str, dict]:
    path = cache_dir / CACHE_NAME
    data: dict = json.loads(path.read_text()) if path.exists() else {}
    table: dict[str, dict] = data.get("symbols", {})
    todo = [s for s in symbols if s not in table]
    if todo and not offline:
        import yfinance as yf

        for s in todo:
            store.bars("kr", s)
            ticker = store.resolved_ticker.get(("kr", s))
            rec: dict = {"ticker": ticker}
            if ticker:
                try:
                    info = yf.Ticker(ticker).info or {}
                    rec.update(
                        sector=info.get("sector"),
                        industry=info.get("industry"),
                        quote_type=info.get("quoteType"),
                    )
                except Exception as exc:  # noqa: BLE001 -- recorded per symbol
                    LOG.warning("sector fetch failed %s: %s", ticker, exc)
                    rec["error"] = type(exc).__name__
            table[s] = rec
        data = {
            "source": "yfinance Ticker.info (sector, industry, quoteType)",
            "fetched_at": datetime.now(UTC).isoformat(),
            "symbols": table,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))
    return table


def fetched_at(cache_dir: Path) -> str | None:
    path = cache_dir / CACHE_NAME
    return json.loads(path.read_text()).get("fetched_at") if path.exists() else None
