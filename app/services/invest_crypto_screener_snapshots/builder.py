from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.services.invest_crypto_screener_snapshots.derivatives import (
    fetch_funding_rates,
    fetch_oi_and_long_short,
)
from app.services.invest_crypto_screener_snapshots.freshness import (
    today_crypto_snapshot_date,
)
from app.services.invest_crypto_screener_snapshots.repository import (
    CryptoSnapshotUpsert,
    InvestCryptoScreenerSnapshotsRepository,
)


@dataclass(frozen=True)
class CryptoProviderRow:
    symbol: str
    latest_close: Decimal
    name: str | None = None
    change_amount: Decimal | None = None
    change_rate: Decimal | None = None
    trade_amount_24h: Decimal | None = None
    volume_24h: Decimal | None = None
    volume_24h_usd: Decimal | None = None
    market_cap: Decimal | None = None
    rsi: Decimal | None = None
    adx: Decimal | None = None
    market_warning: bool = False
    raw_payload: dict[str, Any] = field(default_factory=dict)


class CryptoSnapshotProvider(Protocol):
    async def fetch_rows(
        self, *, limit: int | None = None
    ) -> list[CryptoProviderRow]: ...


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def build_crypto_snapshot_payloads(
    rows: list[CryptoProviderRow],
    *,
    snapshot_date: dt.date,
    funding_by_symbol: dict[str, Decimal] | None = None,
    oi_ls_by_symbol: dict[str, dict[str, Decimal | None]] | None = None,
) -> list[CryptoSnapshotUpsert]:
    funding = funding_by_symbol or {}
    oi_ls = oi_ls_by_symbol or {}
    payloads: list[CryptoSnapshotUpsert] = []
    for row in rows:
        symbol = str(row.symbol or "").strip().upper()
        if not symbol.startswith("KRW-"):
            continue
        deriv = oi_ls.get(symbol) or {}
        payloads.append(
            CryptoSnapshotUpsert(
                symbol=symbol,
                snapshot_date=snapshot_date,
                name=row.name,
                latest_close=row.latest_close,
                change_amount=row.change_amount,
                change_rate=row.change_rate,
                trade_amount_24h=row.trade_amount_24h,
                volume_24h=row.volume_24h,
                volume_24h_usd=row.volume_24h_usd,
                market_cap=row.market_cap,
                rsi=row.rsi,
                adx=row.adx,
                # ROB-443: None when the coin has no USD-M perp (fail-closed).
                funding_rate=funding.get(symbol),
                open_interest_usd=deriv.get("open_interest_usd"),
                oi_change_24h=deriv.get("oi_change_24h"),
                long_short_account_ratio=deriv.get("long_short_account_ratio"),
                market_warning=row.market_warning,
                raw_payload=row.raw_payload,
                source="tvscreener_upbit",
            )
        )
    return payloads


async def build_crypto_snapshots(
    *,
    provider: CryptoSnapshotProvider,
    repository: InvestCryptoScreenerSnapshotsRepository,
    snapshot_date: dt.date | None = None,
    commit: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    date_value = snapshot_date or today_crypto_snapshot_date()
    provider_rows = await provider.fetch_rows(limit=limit)
    # ROB-443: enrich with USD-M perp funding rate (one batch call; coins without
    # a perp stay None). Fail-open — funding errors never block the snapshot build.
    funding_by_symbol = await fetch_funding_rates([row.symbol for row in provider_rows])
    # ROB-443 follow-up: OI + long/short are per-symbol, so enrich ONLY the perp
    # coins (the ones funding matched) — not the whole universe. Fail-open per coin.
    oi_ls_by_symbol = await fetch_oi_and_long_short(list(funding_by_symbol))
    payloads = build_crypto_snapshot_payloads(
        provider_rows,
        snapshot_date=date_value,
        funding_by_symbol=funding_by_symbol,
        oi_ls_by_symbol=oi_ls_by_symbol,
    )
    upserted = 0
    if commit:
        for payload in payloads:
            await repository.upsert(payload)
            upserted += 1
    return {
        "snapshot_date": date_value.isoformat(),
        "fetched": len(provider_rows),
        "would_upsert": len(payloads),
        "upserted": upserted,
        "fundingEnriched": sum(1 for p in payloads if p.funding_rate is not None),
        "oiEnriched": sum(1 for p in payloads if p.open_interest_usd is not None),
        "longShortEnriched": sum(
            1 for p in payloads if p.long_short_account_ratio is not None
        ),
        "committed": commit,
        "samples": [payload.model_dump(mode="json") for payload in payloads[:5]],
    }


def provider_row_from_mapping(row: dict[str, Any]) -> CryptoProviderRow | None:
    symbol = str(row.get("symbol") or row.get("market") or "").strip().upper()
    latest_close = _decimal_or_none(
        row.get("close") or row.get("current_price") or row.get("price")
    )
    if not symbol.startswith("KRW-") or latest_close is None:
        return None
    return CryptoProviderRow(
        symbol=symbol,
        name=str(row.get("name") or "").strip() or None,
        latest_close=latest_close,
        change_amount=_decimal_or_none(row.get("change_amount")),
        change_rate=_decimal_or_none(row.get("change_rate")),
        trade_amount_24h=_decimal_or_none(
            row.get("trade_amount_24h") or row.get("value_traded")
        ),
        volume_24h=_decimal_or_none(row.get("volume_24h")),
        volume_24h_usd=_decimal_or_none(row.get("volume_24h_usd")),
        market_cap=_decimal_or_none(row.get("market_cap")),
        rsi=_decimal_or_none(row.get("rsi")),
        adx=_decimal_or_none(row.get("adx")),
        market_warning=bool(row.get("market_warning") or False),
        raw_payload={
            k: v
            for k, v in row.items()
            if k
            in {
                "symbol",
                "name",
                "market",
                "source",
                "market_cap_rank",
                "rsi_bucket",
            }
        },
    )
