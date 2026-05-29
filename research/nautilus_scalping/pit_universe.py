"""ROB-351 (eng-review Issue 2) — point-in-time (PIT) universe manifest (pure, stdlib).

Single survivorship-safe authority for "which symbols were tradeable as of time T".
Both the cost-blind screen and the gate consult this so a delisted/unlisted symbol
cannot leak into a window it could not have been traded in (the bias ROB-349 set
out to close). Cross-sectional strategies call ``universe_as_of`` at EACH rebalance.

The manifest holds only small listing metadata (symbol + listing/delisting
timestamps) — it is durable and committable; the raw OHLCV it describes is NOT
(kept out of git via AUTO_TRADER_RESEARCH_ARTIFACT_ROOT, see artifact_paths.py).

Units: ``listed_from`` / ``delisted_at`` / query ``ts`` / ``min_seasoning`` are all
integers in the SAME caller-defined unit (e.g. epoch ms). ``delisted_at`` is
EXCLUSIVE; ``None`` means still live. A symbol is tradeable at ``ts`` iff
``listed_from + min_seasoning <= ts`` and (``delisted_at`` is None or ``ts <
delisted_at``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

_META_FIELDS = (
    "status",
    "kline_coverage",
    "funding_coverage",
    "oi_coverage",
    "confidence",
    "missing_data_reason",
)


def _date_to_epoch_ms(date_str: str) -> int:
    """Midnight-UTC epoch ms for ``YYYY-MM-DD``."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _month_first_day(month_str: str) -> str:
    """``YYYY-MM`` -> ``YYYY-MM-01``."""
    return f"{month_str}-01"


@dataclass(frozen=True)
class SymbolListing:
    symbol: str
    listed_from: int
    delisted_at: int | None = None
    status: Literal["live", "settling", "dead"] | None = None
    kline_coverage: float | None = None
    funding_coverage: float | None = None
    oi_coverage: float | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    missing_data_reason: str | None = None

    def validate(self) -> SymbolListing:
        if self.delisted_at is not None and self.delisted_at < self.listed_from:
            raise ValueError(
                f"{self.symbol}: delisted_at {self.delisted_at} < listed_from "
                f"{self.listed_from}"
            )
        return self

    def tradeable_at(self, ts: int, min_seasoning: int = 0) -> bool:
        if ts < self.listed_from + min_seasoning:
            return False
        if self.delisted_at is not None and ts >= self.delisted_at:
            return False
        return True


@dataclass(frozen=True)
class PITManifest:
    listings: tuple[SymbolListing, ...]

    @classmethod
    def from_records(cls, records: list[dict]) -> PITManifest:
        listings = tuple(
            SymbolListing(
                symbol=r["symbol"],
                listed_from=int(r["listed_from"]),
                delisted_at=None
                if r.get("delisted_at") is None
                else int(r["delisted_at"]),
                status=r.get("status"),
                kline_coverage=r.get("kline_coverage"),
                funding_coverage=r.get("funding_coverage"),
                oi_coverage=r.get("oi_coverage"),
                confidence=r.get("confidence"),
                missing_data_reason=r.get("missing_data_reason"),
            ).validate()
            for r in records
        )
        seen: set[str] = set()
        for listing in listings:
            if listing.symbol in seen:
                raise ValueError(f"duplicate symbol in manifest: {listing.symbol}")
            seen.add(listing.symbol)
        return cls(listings=listings)

    @classmethod
    def from_pit_index_records(cls, rows: list[dict]) -> PITManifest:
        """Build from ROB-349 index rows (day-precise active_from/active_to)."""
        recs: list[dict] = []
        for r in rows:
            af = r.get("active_from") or (
                _month_first_day(r["first_seen"]) if r.get("first_seen") else None
            )
            if not af:
                continue
            listed_from = _date_to_epoch_ms(af)
            at = r.get("active_to")
            if at in (None, "ongoing"):
                delisted_at = None
            else:
                delisted_at = _date_to_epoch_ms(
                    (datetime.strptime(at, "%Y-%m-%d") + timedelta(days=1)).strftime(
                        "%Y-%m-%d"
                    )
                )
            recs.append(
                {
                    "symbol": r["symbol"],
                    "listed_from": listed_from,
                    "delisted_at": delisted_at,
                    "status": r.get("status"),
                    "kline_coverage": r.get("kline_coverage"),
                    "funding_coverage": r.get("funding_coverage"),
                    "oi_coverage": r.get("oi_coverage"),
                    "confidence": r.get("confidence"),
                    "missing_data_reason": r.get("missing_data_reason") or None,
                }
            )
        return cls.from_records(recs)

    def to_records(self) -> list[dict]:
        out = []
        for x in self.listings:
            rec: dict = {
                "symbol": x.symbol,
                "listed_from": x.listed_from,
                "delisted_at": x.delisted_at,
            }
            for f in _META_FIELDS:
                v = getattr(x, f)
                if v is not None:
                    rec[f] = v
            out.append(rec)
        return out

    def universe_as_of(self, ts: int, min_seasoning: int = 0) -> frozenset[str]:
        """Symbols tradeable at ``ts`` (survivorship-safe). Call per rebalance."""
        return frozenset(
            x.symbol for x in self.listings if x.tradeable_at(ts, min_seasoning)
        )

    def strict_usdt_perp(self) -> PITManifest:
        """Perp-only honest universe: live/dead plain *USDT, excl. settling/BUSD/USDC/dated/SETTLED."""
        kept = tuple(
            x
            for x in self.listings
            if x.status in ("live", "dead")
            and x.symbol.endswith("USDT")
            and "_" not in x.symbol
            and "SETTLED" not in x.symbol
        )
        return PITManifest(listings=kept)

    def snapshot_hash(self) -> str:
        """Stable sha256 over canonical (order-independent) records — pins a committed manifest."""
        canon = json.dumps(
            sorted(self.to_records(), key=lambda r: r["symbol"]),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canon.encode()).hexdigest()

    @classmethod
    def load(cls, path: str | Path) -> PITManifest:
        data = json.loads(Path(path).read_text())
        return cls.from_records(data["listings"])

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({"listings": self.to_records()}, indent=2))
