"""ROB-117 — Read-only wrapper around screen_stocks_impl for the
Candidate Discovery page.

Provides:
  - the same `screen_stocks` filters MCP exposes,
  - held-position annotation via MergedPortfolioService + Upbit holdings,
  - data-quality warnings forwarded as-is (NEVER hidden).

NEVER mutates broker state. NEVER writes order-intent / watch / candidate rows.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.candidate_discovery import (
    CandidateScreenResponse,
    ScreenedCandidate,
)


class CandidateScreeningService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _screen_stocks(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Keep this import lazy so importing app.main / API routers does not pull the
        # full MCP tooling graph into the FastAPI process before the Candidate
        # Discovery endpoint is actually used.
        from app.mcp_server.tooling.analysis_tool_handlers import screen_stocks_impl

        return await screen_stocks_impl(**kwargs)

    async def screen(
        self,
        *,
        user_id: int,
        market: str,
        asset_type: str | None = None,
        strategy: str | None = None,
        sort_by: str | None = None,
        sort_order: str = "desc",
        min_market_cap: float | None = None,
        max_per: float | None = None,
        max_pbr: float | None = None,
        min_dividend_yield: float | None = None,
        max_rsi: float | None = None,
        adv_krw_min: int | None = None,
        market_cap_min_krw: int | None = None,
        market_cap_max_krw: int | None = None,
        exclude_sectors: list[str] | None = None,
        instrument_types: list[str] | None = None,
        krw_only: bool = False,
        exclude_warnings: bool = False,
        limit: int = 50,
    ) -> CandidateScreenResponse:
        raw = await self._screen_stocks(
            market=market,  # type: ignore[arg-type]
            asset_type=asset_type,  # type: ignore[arg-type]
            strategy=strategy,
            sort_by=sort_by,  # type: ignore[arg-type]
            sort_order=sort_order,  # type: ignore[arg-type]
            min_market_cap=min_market_cap,
            max_per=max_per,
            max_pbr=max_pbr,
            min_dividend_yield=min_dividend_yield,
            max_rsi=max_rsi,
            exclude_sectors=exclude_sectors,
            instrument_types=instrument_types,
            adv_krw_min=adv_krw_min,
            market_cap_min_krw=market_cap_min_krw,
            market_cap_max_krw=market_cap_max_krw,
            limit=limit,
        )

        held = await self._load_held_symbols(user_id, market)
        rows = list(raw.get("stocks") or raw.get("results") or [])

        if krw_only and market == "crypto":
            rows = [
                r for r in rows if str(r.get("symbol", "")).upper().startswith("KRW-")
            ]

        candidates: list[ScreenedCandidate] = []
        for r in rows:
            symbol = str(r.get("symbol") or "")
            warnings = _row_warnings(r)
            if exclude_warnings and warnings:
                continue
            candidates.append(
                ScreenedCandidate(
                    symbol=symbol,
                    name=r.get("name"),
                    market=r.get("market"),
                    instrument_type=r.get("instrument_type"),
                    price=_to_float(_pick_first(r, "price", "close", "current_price")),
                    change_rate=_to_float(r.get("change_rate")),
                    volume=_to_float(_pick_first(r, "volume", "volume_24h")),
                    trade_amount_24h=_to_float(
                        _pick_first(r, "trade_amount_24h", "trade_amount")
                    ),
                    volume_ratio=_to_float(r.get("volume_ratio")),
                    rsi=_to_float(r.get("rsi")),
                    market_cap=_to_float(r.get("market_cap")),
                    per=_to_float(r.get("per")),
                    pbr=_to_float(r.get("pbr")),
                    sector=r.get("sector"),
                    is_held=symbol in held,
                    data_warnings=warnings,
                )
            )

        rsi_enrichment = _rsi_enrichment(raw)
        return CandidateScreenResponse(
            generated_at=datetime.now(UTC).isoformat(),
            market=market,
            strategy=strategy,
            sort_by=sort_by,
            total=len(candidates),
            candidates=candidates,
            warnings=list(raw.get("warnings") or []),
            rsi_enrichment_attempted=int(rsi_enrichment.get("attempted") or 0),
            rsi_enrichment_succeeded=int(rsi_enrichment.get("succeeded") or 0),
        )

    async def _load_held_symbols(self, user_id: int, market: str) -> set[str]:
        try:
            from app.services.brokers.kis import KISClient
            from app.services.merged_portfolio_service import MergedPortfolioService
        except ImportError:
            return set()

        held: set[str] = set()
        try:
            service = MergedPortfolioService(self.db)
            kis_client = KISClient()
            if market in ("kr", "kospi", "kosdaq", "konex", "all"):
                rows = await service.get_merged_portfolio_domestic(user_id, kis_client)
                held.update(str(r.ticker).upper() for r in rows if r.quantity)
            if market in ("us", "all"):
                rows = await service.get_merged_portfolio_overseas(user_id, kis_client)
                held.update(str(r.ticker).upper() for r in rows if r.quantity)
        except Exception:
            pass

        if market in ("crypto", "all"):
            try:
                from app.services.upbit_holdings_service import (
                    fetch_upbit_holdings_for_user,
                )

                rows = await fetch_upbit_holdings_for_user(self.db, user_id)
                held.update(str(r.ticker).upper() for r in rows if r.quantity)
            except Exception:
                pass

        return held


def _pick_first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _row_warnings(row: dict[str, Any]) -> list[str]:
    warnings = list(row.get("warnings") or [])
    market_warning = row.get("market_warning")
    if market_warning and str(market_warning) not in warnings:
        warnings.append(str(market_warning))
    return warnings


def _rsi_enrichment(raw: dict[str, Any]) -> dict[str, Any]:
    direct = raw.get("rsi_enrichment")
    if isinstance(direct, dict):
        return direct
    meta = raw.get("meta")
    if isinstance(meta, dict):
        nested = meta.get("rsi_enrichment")
        if isinstance(nested, dict):
            return nested
    return {}


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
