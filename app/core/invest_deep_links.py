"""Deep-link URL builders for the /invest web UI (INVEST-WATCH-UI §57차 item ③).

Pure functions only — no telegram/discord/order_proposals imports. This is the
"separation design" the job brief called for: the notifier/formatter files
that actually attach links to Telegram/Discord cards (``app/monitoring/
trade_notifier/{notifier,formatters_discord}.py``) are owned by an in-flight
PR (#1844) and are out of scope here. Once that PR lands, its formatters can
import and call these builders instead of constructing paths inline — mirrors
the existing ``app/core/portfolio_links.py`` pattern (same ``public_base_url``
source, same "pure builder, caller attaches" split).

The loss-cut builder carries only a proposal UUID. Approval nonce, actor,
account, quantity, and price never enter the URL.
"""

from __future__ import annotations

from urllib.parse import quote

from app.core.config import settings


def build_loss_cut_approval_url(*, proposal_id: object) -> str:
    """URL to the authenticated evidence and two-step loss-cut page."""
    proposal = quote(str(proposal_id).strip(), safe="")
    if not proposal:
        raise ValueError("proposal_id is required")
    return (
        f"{settings.public_base_url.rstrip('/')}/invest/approvals/loss-cut/{proposal}"
    )


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


def build_order_detail_url(
    *, broker: str | None, market: str | None, ledger_id: int | None
) -> str | None:
    """URL to the standalone order/fill detail view for one ledger row.

    Example: https://mgh3326.duckdns.org/invest/orders/kis/us/482

    ``market`` is required alongside ``broker`` — the literal broker value
    "kis" is written to two different ledger tables with independent id
    sequences (KR domestic orders vs. US live orders placed via KIS), so
    broker alone cannot disambiguate which row ``ledger_id`` refers to (see
    the matching guard in ``app/routers/invest_fills.py::order_detail`` —
    this builder and that endpoint must agree on the same
    broker+market+ledger_id key, or a generated link resolves to the wrong
    order). Returns None when any identifier is missing (same fail-open
    convention as ``build_position_detail_url``) — callers should skip
    attaching a link rather than send a broken or ambiguous one.
    """
    if not broker or not market or not ledger_id:
        return None
    broker_key = quote(broker.strip().lower())
    market_key = quote(market.strip().lower())
    return (
        f"{settings.public_base_url.rstrip('/')}/invest/orders/"
        f"{broker_key}/{market_key}/{ledger_id}"
    )


def build_funding_advisory_url(advisory_id: str) -> str | None:
    """Build a read-only funding detail URL with no approval authority."""

    normalized = str(advisory_id or "").strip()
    if not normalized:
        return None
    return (
        f"{settings.public_base_url.rstrip('/')}/invest/funding/"
        f"{quote(normalized, safe='')}"
    )


def build_funding_declaration_url() -> str:
    """Build the admin declaration form URL; opening it never writes cash."""

    return (
        f"{settings.public_base_url.rstrip('/')}/invest/funding"
        "#external-cash-declaration"
    )
