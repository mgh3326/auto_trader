"""ROB-928 — downside watch auto-register + stop_loss->watch mirror.

Mirrors ``review.trade_journals.stop_loss`` for currently-held KR equity
positions into a support-break (``operator=below``) watch alert so a
stop-loss breach produces a notify-only Hermes trigger instead of the
silent -14.6% drift observed on 005930 (stop 286,000 breached 2026-07-02,
unactioned for two weeks — the incident that motivated this ticket).
Symbols with no journal ``stop_loss`` fall back to the trailing 20-session
low as a support-break proxy.

Safety boundary: the only mutation this module performs is inserting a
``review.investment_watch_alerts`` row via ``DirectWatchCreateService``
(action_mode=notify_only). It never imports an order/broker surface —
see tests/services/test_downside_watch_service_no_broker_imports.py.
Registration is fixed-level, not trailing: rerun periodically to refresh.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType
from app.schemas.investment_reports import CreateInvestmentWatchRequest
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_reports.watch_create import DirectWatchCreateService

logger = logging.getLogger(__name__)

# Level-selection rule (ROB-928 AC): journal stop_loss wins when present
# (max across lots for the same symbol); otherwise fall back to the
# trailing N-session low. Kept as module constants so the rule is a
# single tunable, not scattered magic numbers.
RECENT_LOW_LOOKBACK_SESSIONS = 20
DEFAULT_VALID_FOR = timedelta(days=14)
CREATED_BY = "downside_watch_service"
DEFENSIVE_INTENT: Literal["sell_review"] = "sell_review"
_DEFENSIVE_INTENTS = frozenset({"sell_review", "risk_review"})

LevelSource = Literal["stop_loss_mirror", "recent_low_20d"]
RecentLowFetcher = Callable[[str], Awaitable[Decimal | None]]
CurrentPriceFetcher = Callable[[str], Awaitable[Decimal | None]]


@dataclass(frozen=True)
class DownsideWatchLevel:
    symbol: str
    threshold: Decimal
    source: LevelSource
    quantity: Decimal


async def _fetch_recent_low_from_db(
    session: AsyncSession, symbol: str
) -> Decimal | None:
    """Trailing 20-session KRX low, read directly (bypassing the
    freshness-gated cache_first_kr helper so callers control staleness
    policy explicitly for this advisory-only fallback)."""
    from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey

    repo = DailyCandlesRepository(session=session)
    rows = await repo.fetch_recent(
        market=MarketKey.KR,
        symbol=symbol,
        partition="KRX",
        count=RECENT_LOW_LOOKBACK_SESSIONS,
    )
    if not rows:
        return None
    return Decimal(str(min(row.low for row in rows)))


class DownsideWatchService:
    """Session-invoked sweep: hydrate KR holdings -> support-break watch."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        watch_repo: InvestmentReportsRepository | None = None,
        recent_low_fetcher: RecentLowFetcher | None = None,
        current_price_fetcher: CurrentPriceFetcher | None = None,
    ) -> None:
        self._session = session
        self._watch_repo = watch_repo or InvestmentReportsRepository(session)
        self._recent_low_fetcher = recent_low_fetcher or (
            lambda symbol: _fetch_recent_low_from_db(session, symbol)
        )
        self._current_price_fetcher = current_price_fetcher or _fetch_current_price

    async def _active_kr_holdings(self) -> dict[str, list[TradeJournal]]:
        stmt = sa.select(TradeJournal).where(
            TradeJournal.status == JournalStatus.active,
            TradeJournal.account_type == "live",
            TradeJournal.side == "buy",
            TradeJournal.instrument_type == InstrumentType.equity_kr,
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        grouped: dict[str, list[TradeJournal]] = {}
        for row in rows:
            grouped.setdefault(row.symbol, []).append(row)
        return grouped

    async def compute_levels(self) -> list[DownsideWatchLevel]:
        levels: list[DownsideWatchLevel] = []
        for symbol, lots in (await self._active_kr_holdings()).items():
            quantity = sum((lot.quantity or Decimal("0")) for lot in lots)
            stop_losses = [lot.stop_loss for lot in lots if lot.stop_loss is not None]
            if stop_losses:
                levels.append(
                    DownsideWatchLevel(
                        symbol=symbol,
                        threshold=max(stop_losses),
                        source="stop_loss_mirror",
                        quantity=quantity,
                    )
                )
                continue

            recent_low = await self._recent_low_fetcher(symbol)
            if recent_low is None:
                logger.info(
                    "downside_watch: no stop_loss and no recent-low data for "
                    "%s; skipping this sweep",
                    symbol,
                )
                continue
            levels.append(
                DownsideWatchLevel(
                    symbol=symbol,
                    threshold=recent_low,
                    source="recent_low_20d",
                    quantity=quantity,
                )
            )
        return levels

    async def _has_active_downside_watch(self, symbol: str) -> bool:
        existing = await self._watch_repo.list_alerts(
            market="kr", symbol=symbol, status="active", limit=250
        )
        return any(
            alert.operator == "below" and alert.intent in _DEFENSIVE_INTENTS
            for alert in existing
        )

    def _build_request(
        self, level: DownsideWatchLevel, *, valid_until
    ) -> CreateInvestmentWatchRequest:
        rationale = (
            f"ROB-928 하방 워치 자동등록 (source={level.source}): "
            f"근거 레벨={level.threshold}. 트레일링 미지원 — 레벨 고정, "
            "주기 재등록 필요 (stale 경고)."
        )
        return CreateInvestmentWatchRequest.model_validate(
            {
                "created_by": CREATED_BY,
                "market": "kr",
                "symbol": level.symbol,
                "intent": DEFENSIVE_INTENT,
                "rationale": rationale,
                "watch_condition": {
                    "metric": "price",
                    "operator": "below",
                    "threshold": level.threshold,
                    "action_mode": "notify_only",
                },
                "valid_until": valid_until,
                "metadata": {
                    "rob928_level_source": level.source,
                    "rob928_quantity": str(level.quantity),
                    "trailing_supported": False,
                },
            }
        )

    async def register_sweep(self, *, dry_run: bool = True) -> dict[str, Any]:
        levels = await self.compute_levels()
        valid_until = now_kst() + DEFAULT_VALID_FOR

        registered: list[dict[str, Any]] = []
        skipped_existing: list[dict[str, Any]] = []
        skipped_already_triggered: list[dict[str, Any]] = []
        level_summaries: list[dict[str, Any]] = []

        for level in levels:
            level_summaries.append(
                {
                    "symbol": level.symbol,
                    "threshold": str(level.threshold),
                    "source": level.source,
                    "quantity": str(level.quantity),
                }
            )

            if await self._has_active_downside_watch(level.symbol):
                skipped_existing.append(
                    {
                        "symbol": level.symbol,
                        "reason": "active_downside_watch_exists",
                    }
                )
                continue

            # ROB-971: a below watch whose price is already at/below its
            # threshold fires on the first scanner pass. Never register such
            # a condition: doing so turns historical stop-loss breaches into
            # an alert burst instead of a useful future watch.
            current_price = await self._current_price_fetcher(level.symbol)
            if current_price is None:
                skipped_already_triggered.append(
                    {
                        "symbol": level.symbol,
                        "reason": "current_price_unavailable",
                    }
                )
                continue
            if current_price <= level.threshold:
                skipped_already_triggered.append(
                    {
                        "symbol": level.symbol,
                        "reason": "condition_already_true_at_registration",
                        "current_price": str(current_price),
                        "threshold": str(level.threshold),
                    }
                )
                continue

            if dry_run:
                registered.append(
                    {
                        "symbol": level.symbol,
                        "threshold": str(level.threshold),
                        "source": level.source,
                        "would_register": True,
                    }
                )
                continue

            request = self._build_request(level, valid_until=valid_until)
            alert, idempotent = await DirectWatchCreateService(
                self._session, self._watch_repo
            ).create(request)
            await self._session.commit()
            registered.append(
                {
                    "symbol": level.symbol,
                    "alert_uuid": str(alert.alert_uuid),
                    "threshold": str(level.threshold),
                    "source": level.source,
                    "idempotent": idempotent,
                }
            )

        return {
            "dry_run": dry_run,
            "registered": registered,
            "skipped_existing": skipped_existing,
            "skipped_already_triggered": skipped_already_triggered,
            "levels": level_summaries,
        }


async def _fetch_current_price(symbol: str) -> Decimal | None:
    """Fetch the registration-time KR price; failures fail closed upstream."""
    from app.services import market_data

    try:
        quote = await market_data.get_quote(symbol=symbol, market="equity_kr")
    except Exception:
        logger.warning("downside_watch: current price unavailable for %s", symbol)
        return None
    price = getattr(quote, "price", None)
    return Decimal(str(price)) if price is not None else None
