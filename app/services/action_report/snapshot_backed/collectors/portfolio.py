"""Portfolio snapshot collector (read-only).

For the ROB-278 lockdown the collector emits a v2 payload that is
**additive** over the legacy ``holdings``/``count``/``market`` shape:

* ``primary_source``: ``"kis" | "upbit" | "manual" | "none"`` — explicit label
  so the viewer/audit can tell which source backs the holdings.
* ``reference_holdings``: manual rows surfaced when KIS live is primary, so
  manual/Toss entries remain visible for cross-check without being mislabeled
  as KIS live.
* ``cash`` / ``buying_power`` / ``sellable_summary``: derived from the KIS
  read-only account reader; absent when KIS was not consulted.
* ``provenance``: per-fetch metadata (``kis_fetch_status``, warnings, errors,
  fetched_at) for audit.

Policy invariants:

* Manual/Toss/reference holdings are **never** promoted to ``kis_live``
  primary. KIS unavailable on a ``kis_live`` request yields
  ``primary_source="none"`` with ``freshness="unavailable"`` — the report
  generator's stale gate then handles publishing.
* ``account_scope="kis_live"`` on KR or US requires an explicit ``user_id``
  on the collector request. ``user_id`` missing → fail-closed (no implicit
  default).
* Non-(kis_live) combos preserve the v1 manual-primary behaviour and add
  the ``primary_source="manual"`` label only.

ROB-369 E9 — ``market="crypto" + account_scope="upbit_live"`` reads the live
Upbit account via ``UpbitHomeReader`` (``primary_source="upbit"``), mirroring
the KIS-live path: live holdings + KRW cash/orderable are primary, manual
``CRYPTO`` rows stay reference-only, and an Upbit failure yields
``primary_source="none"`` / ``freshness="unavailable"``. Provenance uses
``upbit_fetch_status``. Crypto has no sellable/pending-sell concept, so
``sellable_summary`` is ``None``.

ROB-297 — ``market="us" + account_scope="kis_live"`` is the canonical KIS
overseas combo and takes the same KIS-live path as ``market="kr"``. The KR/US
disambiguation lives in ``market`` per ROB-297 guardrail #2; no
``kis_overseas_live`` alias is introduced. Toss/manual US reference quantity
is NEVER summed into KIS-primary holdings or ``sellable_summary``
(guardrail #3).

The collector itself never calls broker mutation paths. KIS reads go through
``KISHomeReader`` which uses ``BaseKISClient`` for read-only account/margin
queries.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual_holdings import ManualHolding, MarketType
from app.schemas.invest_home import Holding
from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)
from app.services.kr_symbol_universe_service import get_kr_names_by_symbols

logger = logging.getLogger(__name__)

_MARKET_TO_TYPES: dict[str, tuple[MarketType, ...]] = {
    "kr": (MarketType.KR,),
    "us": (MarketType.US,),
    "crypto": (MarketType.CRYPTO,),
}

# Maps the request's ``market`` to the value used on
# :class:`~app.schemas.invest_home.Holding.market`. The KIS reader returns a
# mixed list of KR + US holdings on the same account fetch; the KIS-live
# branch filters that list by the request's market.
_REQUEST_MARKET_TO_HOLDING_MARKET: dict[str, str] = {
    "kr": "KR",
    "us": "US",
}


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _manual_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "ticker": row.ticker,
        "market_type": (
            row.market_type.value
            if isinstance(row.market_type, MarketType)
            else str(row.market_type)
        ),
        "quantity": row.quantity,
        "avg_price": row.avg_price,
        "display_name": row.display_name,
        "updated_at": row.updated_at,
        "source": "manual",
    }


def _classify_fetch_status(
    holdings_dicts: list[dict[str, Any]],
    account: Any | None,
    fetch_warnings: list[str],
) -> str:
    """Classify a live read-only fetch as ``ok`` / ``partial`` / ``failed``.

    Shared by the KIS-live and Upbit-live paths so they stay in lockstep:

    * ``failed`` — nothing usable returned (no holdings AND no account), or
      warnings present with no holdings (data-quality gate).
    * ``partial`` — holdings present but the reader flagged a warning.
    * ``ok`` — holdings (and/or account) present with no warnings.
    """
    if not holdings_dicts and account is None:
        return "failed"
    if fetch_warnings and not holdings_dicts:
        return "failed"
    if fetch_warnings:
        return "partial"
    return "ok"


def _krw_or_zero(value: float | None) -> float:
    """Coerce an Upbit KRW figure to an explicit float.

    The Upbit reader reports a missing KRW row as ``None``, which for Upbit
    means the account genuinely holds 0 KRW (distinct from the KIS-overseas
    case where ``None`` means cash is *unsupported*/unknown). Emitting an
    explicit ``0.0`` keeps the portfolio stage's ``$.buying_power.krw`` citation
    pointed at a real value instead of a null.
    """
    return float(value) if value is not None else 0.0


def _reader_holding_to_dict(h: Holding) -> dict[str, Any]:
    """Map a read-only :class:`Holding` to the snapshot dict shape.

    Used by both the KIS-live (``h.source == "kis"``) and the Upbit-live
    (``h.source == "upbit"``) paths; ``source`` is taken from the holding so
    the audit trail records the real provenance rather than a hard-coded label.
    """
    return {
        "ticker": h.symbol,
        "market": h.market,
        "asset_type": h.assetType,
        "asset_category": h.assetCategory,
        "quantity": h.quantity,
        "avg_price": h.averageCost,
        "cost_basis": h.costBasis,
        "currency": h.currency,
        "display_name": h.displayName,
        "value_native": h.valueNative,
        "value_krw": h.valueKrw,
        "pnl_krw": h.pnlKrw,
        "pnl_rate": h.pnlRate,
        "sellable_quantity": h.sellableQuantity,
        "pending_sell_quantity": h.pendingSellQuantity,
        "source": h.source,
    }


def _apply_kr_name_fallback(
    rows: list[dict[str, Any]], name_map: dict[str, str]
) -> None:
    """Fill ``display_name`` for rows whose name is missing or equals the code.

    In-place. A row is fixed only when ``name_map`` has a real name for its
    ``ticker``; otherwise the code is kept (never fabricate a name).
    """
    for row in rows:
        ticker = row.get("ticker")
        if not isinstance(ticker, str):
            continue
        current = row.get("display_name")
        is_code_as_name = not current or current == ticker
        if is_code_as_name and ticker in name_map:
            row["display_name"] = name_map[ticker]


class PortfolioSnapshotCollector:
    """Required-kind ``portfolio`` collector backed by ``manual_holdings``
    plus the ROB-278 KIS live source for KR + ``kis_live``."""

    snapshot_kind: str = "portfolio"

    def __init__(
        self,
        session: AsyncSession,
        *,
        kis_reader: Any | None = None,
        upbit_reader: Any | None = None,
    ) -> None:
        self._session = session
        # Home readers are imported lazily to avoid pulling the broker module
        # graph into call sites that don't need them (tests, unit imports).
        self._kis_reader = kis_reader
        self._upbit_reader = upbit_reader

    def _get_kis_reader(self) -> Any:
        if self._kis_reader is not None:
            return self._kis_reader
        from app.services.invest_home_readers import KISHomeReader

        self._kis_reader = KISHomeReader(self._session)
        return self._kis_reader

    def _get_upbit_reader(self) -> Any:
        if self._upbit_reader is not None:
            return self._upbit_reader
        from app.services.invest_home_readers import UpbitHomeReader

        self._upbit_reader = UpbitHomeReader(self._session)
        return self._upbit_reader

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        market_types = _MARKET_TO_TYPES.get(request.market)
        now = utcnow()
        if not market_types:
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="auto_trader_db",
                    reason=f"no portfolio mapping for market={request.market!r}",
                    as_of=now,
                )
            ]

        # ROB-278 / ROB-297 — (kr|us) + kis_live uses the KIS live path.
        # Other combos preserve v1 manual-primary behaviour. KR/US disambig
        # lives in ``market`` per ROB-297 guardrail #2; no ``kis_overseas_live``
        # alias is introduced.
        if request.account_scope == "kis_live" and request.market in (
            "kr",
            "us",
        ):
            return await self._collect_kis_live(request, market_types, now=now)
        # ROB-369 E9 — crypto + upbit_live reads the live Upbit account so the
        # portfolio stage gets real NAV / cash / orderable instead of the
        # manual-primary empty payload that produced "NAV=0".
        if request.account_scope == "upbit_live" and request.market == "crypto":
            return await self._collect_upbit_live(request, market_types, now=now)
        return await self._collect_manual_primary(request, market_types, now=now)

    async def _collect_manual_primary(
        self,
        request: CollectorRequest,
        market_types: tuple[MarketType, ...],
        *,
        now: dt.datetime,
    ) -> list[SnapshotCollectResult]:
        manual_rows = await self._read_manual_rows(market_types)
        holdings = [_manual_row_to_dict(r) for r in manual_rows]
        payload: dict[str, Any] = {
            "holdings": holdings,
            "count": len(holdings),
            "market": request.market,
            "primary_source": "manual",
            "reference_holdings": [],
            "cash": None,
            "buying_power": None,
            "sellable_summary": None,
            "provenance": {
                "kis_fetch_status": "skipped",
                "account_scope": request.account_scope,
                "fetched_at": _iso(now),
                "warnings": [],
                "errors": [],
            },
        }
        if not holdings:
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=payload,
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="partial",
                    coverage={"holdings_found": False},
                )
            ]
        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                coverage={"holdings_count": len(holdings)},
            )
        ]

    async def _collect_kis_live(
        self,
        request: CollectorRequest,
        market_types: tuple[MarketType, ...],
        *,
        now: dt.datetime,
    ) -> list[SnapshotCollectResult]:
        """KIS live path shared by ``(kr|us) + kis_live``.

        ROB-278 introduced this for KR; ROB-297 extended it to US. The
        request's ``market`` selects the holding filter on the
        :class:`KISHomeReader` result (``"KR"`` vs ``"US"``); manual/Toss
        rows are scoped to the matching :class:`MarketType` and surface
        only via ``reference_holdings``.
        """
        manual_rows = await self._read_manual_rows(market_types)
        reference_holdings = [_manual_row_to_dict(r) for r in manual_rows]
        holding_market_filter = _REQUEST_MARKET_TO_HOLDING_MARKET[request.market]

        if request.user_id is None:
            # Lockdown — kis_live requires explicit user_id; manual is NOT
            # promoted to primary. Surface as unavailable for the stale gate.
            payload: dict[str, Any] = {
                "holdings": [],
                "count": 0,
                "market": request.market,
                "primary_source": "none",
                "reference_holdings": reference_holdings,
                "cash": None,
                "buying_power": None,
                "sellable_summary": None,
                "provenance": {
                    "kis_fetch_status": "skipped",
                    "account_scope": request.account_scope,
                    "fetched_at": _iso(now),
                    "warnings": [],
                    "errors": [],
                },
            }
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=payload,
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="unavailable",
                    coverage={"holdings_count": 0},
                    errors={
                        "reason_code": "user_id_missing",
                        "reason": (
                            "kis_live portfolio requires explicit user_id; none supplied"
                        ),
                    },
                )
            ]

        # Call KIS read-only. Catch hard failures so the collector reports
        # them as 'unavailable' rather than crashing report generation.
        reader = self._get_kis_reader()
        fetch_warnings: list[str] = []
        fetch_errors: list[str] = []
        kis_result: Any = None
        try:
            kis_result = await reader.fetch(user_id=request.user_id)
        except Exception as exc:  # noqa: BLE001 — collector must never crash
            logger.warning("KIS read-only fetch failed: %s", exc, exc_info=True)
            fetch_errors.append(f"{type(exc).__name__}: {exc}")

        # Map KIS holdings filtered to the requested market. KR/US live on
        # the same KIS account fetch; the per-market filter keeps the
        # payload scoped to ``request.market``.
        kis_holdings_dicts: list[dict[str, Any]] = []
        cash_payload: dict[str, Any] | None = None
        buying_power_payload: dict[str, Any] | None = None
        sellable_summary: dict[str, Any] | None = None
        kis_fetch_status: str
        if kis_result is None:
            kis_fetch_status = "failed"
        else:
            market_holdings = [
                h
                for h in (kis_result.holdings or [])
                if h.market == holding_market_filter
            ]
            kis_holdings_dicts = [_reader_holding_to_dict(h) for h in market_holdings]
            if kis_result.warning is not None:
                fetch_warnings.append(
                    f"{kis_result.warning.source}: {kis_result.warning.message}"
                )
            account = next(iter(kis_result.accounts or []), None)
            if account is not None:
                # Surface both currencies as-is; consumers pick the one that
                # matches ``market``. The KIS account fetch returns USD for
                # overseas accounts and KRW for domestic; either may be None.
                cash_payload = {
                    "krw": account.cashBalances.krw,
                    "usd": account.cashBalances.usd,
                }
                buying_power_payload = {
                    "krw": account.buyingPower.krw,
                    "usd": account.buyingPower.usd,
                }
            sellable_count = sum(
                1
                for h in market_holdings
                if h.sellableQuantity is not None and h.sellableQuantity > 0
            )
            pending_sell_count = sum(
                1 for h in market_holdings if (h.pendingSellQuantity or 0) > 0
            )
            sellable_summary = {
                "sellable_count": sellable_count,
                "pending_sell_count": pending_sell_count,
            }
            kis_fetch_status = _classify_fetch_status(
                kis_holdings_dicts, account, fetch_warnings
            )

        if kis_fetch_status == "failed":
            primary_source = "none"
            holdings_out: list[dict[str, Any]] = []
            cash_payload = None
            buying_power_payload = None
            sellable_summary = None
            freshness = "unavailable"
        else:
            primary_source = "kis"
            holdings_out = kis_holdings_dicts
            freshness = "fresh" if kis_fetch_status == "ok" else "partial"

        # ROB-392 — resolve KR rows whose ``display_name`` is missing or equals
        # the code (e.g. "035420") to the universe name. Best-effort: a lookup
        # failure keeps the code (never fabricate a name). In-place on the dicts
        # shared by ``holdings_out`` / ``reference_holdings`` → reflected below.
        if request.market == "kr":
            name_rows = [*holdings_out, *reference_holdings]
            need = sorted(
                {
                    r["ticker"]
                    for r in name_rows
                    if isinstance(r.get("ticker"), str)
                    and (
                        not r.get("display_name")
                        or r.get("display_name") == r["ticker"]
                    )
                }
            )
            if need:
                try:
                    name_map = await get_kr_names_by_symbols(need, db=self._session)
                except Exception:  # noqa: BLE001 — name fallback is best-effort
                    name_map = {}
                _apply_kr_name_fallback(name_rows, name_map)

        payload = {
            "holdings": holdings_out,
            "count": len(holdings_out),
            "market": request.market,
            "primary_source": primary_source,
            "reference_holdings": reference_holdings,
            "cash": cash_payload,
            "buying_power": buying_power_payload,
            "sellable_summary": sellable_summary,
            "provenance": {
                "kis_fetch_status": kis_fetch_status,
                "account_scope": request.account_scope,
                "fetched_at": _iso(now),
                "warnings": fetch_warnings,
                "errors": fetch_errors,
            },
        }

        # ROB-392 — label the NAV scope so the report makes explicit that the
        # number is KIS-primary (sellable) holdings + cash, with ISA/Toss
        # reference rows excluded (ROB-297). KR-only wording; numbers unchanged.
        if request.market == "kr":
            payload["nav_scope"] = "kis_primary_sellable"
            payload["nav_scope_label"] = (
                "NAV는 KIS 실거래(매도가능) 보유 + 현금 기준 · "
                "ISA/Toss 참조분(reference_holdings)은 제외"
            )

        coverage = {
            "holdings_count": len(holdings_out),
            "reference_count": len(reference_holdings),
            "kis_fetch_status": kis_fetch_status,
        }

        if freshness == "unavailable":
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=payload,
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="unavailable",
                    coverage=coverage,
                    errors={
                        "reason_code": "kis_fetch_failed",
                        "reason": "KIS live portfolio fetch failed",
                        "warnings": fetch_warnings,
                        "errors": fetch_errors,
                    },
                )
            ]

        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                freshness_status=freshness,
                coverage=coverage,
            )
        ]

    async def _collect_upbit_live(
        self,
        request: CollectorRequest,
        market_types: tuple[MarketType, ...],
        *,
        now: dt.datetime,
    ) -> list[SnapshotCollectResult]:
        """Upbit live path for ``crypto + upbit_live`` (ROB-369 E9).

        Mirrors ``_collect_kis_live``: live Upbit holdings + KRW cash/orderable
        become ``primary_source="upbit"``; any manual ``CRYPTO`` rows surface
        only via ``reference_holdings`` and are NEVER promoted to primary. An
        Upbit fetch failure yields ``primary_source="none"`` with
        ``freshness="unavailable"`` so the report's stale gate handles it.

        Unlike KIS, Upbit uses global env credentials rather than a per-user
        account, so ``user_id`` does not select an account and is not required
        (passed through as ``0`` when absent, matching ``UpbitHomeReader``
        usage in ``symbol_derivation``). Crypto positions are continuously
        liquid, so there is no sellable/pending-sell concept — the payload
        carries ``sellable_summary=None``.
        """
        manual_rows = await self._read_manual_rows(market_types)
        reference_holdings = [_manual_row_to_dict(r) for r in manual_rows]

        reader = self._get_upbit_reader()
        fetch_warnings: list[str] = []
        fetch_errors: list[str] = []
        upbit_result: Any = None
        try:
            upbit_result = await reader.fetch(user_id=request.user_id or 0)
        except Exception as exc:  # noqa: BLE001 — collector must never crash
            logger.warning("Upbit read-only fetch failed: %s", exc, exc_info=True)
            fetch_errors.append(f"{type(exc).__name__}: {exc}")

        upbit_holdings_dicts: list[dict[str, Any]] = []
        cash_payload: dict[str, Any] | None = None
        buying_power_payload: dict[str, Any] | None = None
        upbit_fetch_status: str
        if upbit_result is None:
            upbit_fetch_status = "failed"
        else:
            holdings = list(upbit_result.holdings or [])
            upbit_holdings_dicts = [_reader_holding_to_dict(h) for h in holdings]
            if upbit_result.warning is not None:
                fetch_warnings.append(
                    f"{upbit_result.warning.source}: {upbit_result.warning.message}"
                )
            account = next(iter(upbit_result.accounts or []), None)
            if account is not None:
                # Upbit reports a KRW-only account (no USD leg); surface both
                # keys so the consumer contract matches the KIS payload shape.
                # KRW is coerced to an explicit 0.0 (Upbit None = 0 KRW) so the
                # portfolio citation never points at a null.
                cash_payload = {
                    "krw": _krw_or_zero(account.cashBalances.krw),
                    "usd": account.cashBalances.usd,
                }
                buying_power_payload = {
                    "krw": _krw_or_zero(account.buyingPower.krw),
                    "usd": account.buyingPower.usd,
                }
            upbit_fetch_status = _classify_fetch_status(
                upbit_holdings_dicts, account, fetch_warnings
            )

        if upbit_fetch_status == "failed":
            primary_source = "none"
            holdings_out: list[dict[str, Any]] = []
            cash_payload = None
            buying_power_payload = None
            freshness = "unavailable"
        else:
            primary_source = "upbit"
            holdings_out = upbit_holdings_dicts
            freshness = "fresh" if upbit_fetch_status == "ok" else "partial"

        payload: dict[str, Any] = {
            "holdings": holdings_out,
            "count": len(holdings_out),
            "market": request.market,
            "primary_source": primary_source,
            "reference_holdings": reference_holdings,
            "cash": cash_payload,
            "buying_power": buying_power_payload,
            "sellable_summary": None,
            "provenance": {
                "upbit_fetch_status": upbit_fetch_status,
                "account_scope": request.account_scope,
                "fetched_at": _iso(now),
                "warnings": fetch_warnings,
                "errors": fetch_errors,
            },
        }

        coverage = {
            "holdings_count": len(holdings_out),
            "reference_count": len(reference_holdings),
            "upbit_fetch_status": upbit_fetch_status,
        }
        # Surface the reader's dust (<5000 KRW) / inactive filtering so the
        # snapshot NAV's divergence from the raw Upbit eval is auditable rather
        # than a silent drop.
        hidden_counts = getattr(upbit_result, "hidden_counts", None)
        if hidden_counts is not None:
            coverage["hidden_dust_count"] = getattr(hidden_counts, "upbitDust", 0)
            coverage["hidden_inactive_count"] = getattr(
                hidden_counts, "upbitInactive", 0
            )

        if freshness == "unavailable":
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=payload,
                    origin="auto_trader_db",
                    as_of=now,
                    freshness_status="unavailable",
                    coverage=coverage,
                    errors={
                        "reason_code": "upbit_fetch_failed",
                        "reason": "Upbit live portfolio fetch failed",
                        "warnings": fetch_warnings,
                        "errors": fetch_errors,
                    },
                )
            ]

        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                freshness_status=freshness,
                coverage=coverage,
            )
        ]

    async def _read_manual_rows(
        self, market_types: tuple[MarketType, ...]
    ) -> list[Any]:
        stmt = select(ManualHolding).where(ManualHolding.market_type.in_(market_types))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
