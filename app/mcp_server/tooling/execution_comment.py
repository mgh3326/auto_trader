"""Format trade execution data into structured markdown comments."""

from __future__ import annotations

STAGE_FIELDS: dict[str, set[str]] = {
    "strategy": {"symbol", "side", "thesis"},
    "dry_run": {"symbol", "side", "qty", "price", "mode", "currency"},
    "live": {
        "symbol",
        "side",
        "qty",
        "price",
        "mode",
        "order_id",
        "journal_id",
        "fill_status",
        "account_type",
        "currency",
    },
    "fill": {
        "symbol",
        "side",
        "qty",
        "price",
        "mode",
        "order_id",
        "journal_id",
        "fill_status",
        "filled_qty",
        "unfilled_qty",
        "fee",
        "account_type",
        "currency",
    },
    "follow_up": {"symbol", "journal_id", "next_action", "market_context"},
}

VALID_STAGES = frozenset(STAGE_FIELDS.keys())


def _format_value(key: str, value: object, currency: str | None) -> str:
    if key == "price" and currency:
        return f"{currency}{value}"
    if key == "fee" and currency:
        return f"{currency}{value}"
    return str(value)


async def format_execution_comment(
    stage: str,
    symbol: str,
    side: str | None = None,
    qty: float | None = None,
    price: float | None = None,
    mode: str | None = None,
    order_id: str | None = None,
    journal_id: int | None = None,
    fill_status: str | None = None,
    filled_qty: float | None = None,
    unfilled_qty: float | None = None,
    fee: float | None = None,
    currency: str | None = None,
    thesis: str | None = None,
    next_action: str | None = None,
    market_context: str | None = None,
    account_type: str | None = None,
) -> str:
    """Format trade execution data into a structured markdown comment.

    Args:
        stage: Execution stage — one of "strategy", "dry_run", "live", "fill", "follow_up".
        symbol: Trading symbol (e.g. "AAPL", "KRW-BTC").
        side: Trade direction — "buy" or "sell".
        qty: Order quantity.
        price: Order or fill price.
        mode: Execution mode — "dry_run", "live", or "paper".
        order_id: Broker order identifier.
        journal_id: Trade journal record ID.
        fill_status: Fill state — "pending", "filled", "partial", or "cancelled".
        filled_qty: Quantity filled so far.
        unfilled_qty: Remaining unfilled quantity.
        fee: Transaction fee.
        currency: Currency symbol prepended to price/fee (e.g. "$", "₩").
        thesis: Investment thesis text (strategy stage).
        next_action: Follow-up action description.
        market_context: Market context summary from market reports.
        account_type: Account type — "kis_auto", "toss_manual", or "paper".

    Returns:
        Formatted markdown string.
    """
    if stage not in VALID_STAGES:
        return f"Error: invalid stage '{stage}'. Must be one of: {', '.join(sorted(VALID_STAGES))}"

    if stage == "dry_run":
        mode = "dry_run"

    allowed = STAGE_FIELDS[stage]

    all_fields: list[tuple[str, object]] = [
        ("symbol", symbol),
        ("side", side),
        ("qty", qty),
        ("price", price),
        ("mode", mode),
        ("order_id", order_id),
        ("journal_id", journal_id),
        ("fill_status", fill_status),
        ("filled_qty", filled_qty),
        ("unfilled_qty", unfilled_qty),
        ("fee", fee),
        ("account_type", account_type),
        ("thesis", thesis),
        ("next_action", next_action),
        ("market_context", market_context),
    ]

    rows: list[str] = []
    for key, value in all_fields:
        if key not in allowed or value is None:
            continue
        formatted = _format_value(key, value, currency)
        rows.append(f"| {key} | {formatted} |")

    if not rows:
        return f"## 실행 기록\n\nNo data for stage '{stage}'."

    header = "## 실행 기록\n| 필드 | 값 |\n|------|-----|"
    return header + "\n" + "\n".join(rows)


__all__ = ["format_execution_comment"]
