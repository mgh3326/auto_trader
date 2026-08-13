"""Deep-link URL builders for the /invest web UI (INVEST-WATCH-UI §57차 item ③).

Pure functions only — no telegram/discord/order_proposals imports. This is the
"separation design" the job brief called for: the notifier/formatter files
that actually attach links to Telegram/Discord cards (``app/monitoring/
trade_notifier/{notifier,formatters_discord}.py``) are owned by an in-flight
PR (#1844) and are out of scope here. Once that PR lands, its formatters can
import and call these builders instead of constructing paths inline — mirrors
the existing ``app/core/portfolio_links.py`` pattern (same ``public_base_url``
source, same "pure builder, caller attaches" split).

No builder here targets the Phase 2 approval page — it does not exist yet
(§57차 explicitly forbids starting Phase 2 UI in this job), so there is
nothing to link to for the third deep-link type ("승인 카드 → 승인 페이지").
"""

from __future__ import annotations

from urllib.parse import quote

from app.core.config import settings


def build_watches_url(
    *,
    market: str | None = None,
    status: str | None = None,
    symbol: str | None = None,
) -> str:
    """URL to the /invest/watches browsing page, optionally pre-filtered.

    Example: https://mgh3326.duckdns.org/invest/watches?market=kr&status=active
    """
    base = f"{settings.public_base_url.rstrip('/')}/invest/watches"
    params: list[str] = []
    if market:
        params.append(f"market={quote(market.strip().lower())}")
    if status:
        params.append(f"status={quote(status.strip().lower())}")
    if symbol:
        params.append(f"symbol={quote(symbol.strip())}")
    if not params:
        return base
    return f"{base}?{'&'.join(params)}"


def build_order_detail_url(*, broker: str | None, ledger_id: int | None) -> str | None:
    """URL to the standalone order/fill detail view for one ledger row.

    Example: https://mgh3326.duckdns.org/invest/orders/kis/482
    Returns None when either identifier is missing (same fail-open convention
    as ``build_position_detail_url``) — callers should skip attaching a link
    rather than send a broken one.
    """
    if not broker or not ledger_id:
        return None
    broker_key = quote(broker.strip().lower())
    return f"{settings.public_base_url.rstrip('/')}/invest/orders/{broker_key}/{ledger_id}"
