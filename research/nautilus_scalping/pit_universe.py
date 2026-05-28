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

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SymbolListing:
    symbol: str
    listed_from: int
    delisted_at: int | None = None

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
                delisted_at=None if r.get("delisted_at") is None else int(r["delisted_at"]),
            ).validate()
            for r in records
        )
        seen: set[str] = set()
        for listing in listings:
            if listing.symbol in seen:
                raise ValueError(f"duplicate symbol in manifest: {listing.symbol}")
            seen.add(listing.symbol)
        return cls(listings=listings)

    def to_records(self) -> list[dict]:
        return [
            {"symbol": x.symbol, "listed_from": x.listed_from, "delisted_at": x.delisted_at}
            for x in self.listings
        ]

    def universe_as_of(self, ts: int, min_seasoning: int = 0) -> frozenset[str]:
        """Symbols tradeable at ``ts`` (survivorship-safe). Call per rebalance."""
        return frozenset(
            x.symbol for x in self.listings if x.tradeable_at(ts, min_seasoning)
        )

    @classmethod
    def load(cls, path: str | Path) -> PITManifest:
        data = json.loads(Path(path).read_text())
        return cls.from_records(data["listings"])

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({"listings": self.to_records()}, indent=2))
