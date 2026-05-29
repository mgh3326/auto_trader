"""Deterministic portfolio+journal stage (ROB-279)."""

from __future__ import annotations

from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)


def _nested_amount(value: object, currency: str) -> float | None:
    """Return the amount for ``currency`` from a nested ``{"krw": x, "usd": y}``
    block, or ``None`` when absent.

    The production portfolio collector emits cash / buying power as nested
    per-currency dicts. ROB-366 B7: there is NO cross-currency fallback — a USD
    account whose ``usd`` figure is absent returns ``None`` (unavailable), never
    the KRW figure, so the report never mixes currencies or fabricates a value.
    """
    if isinstance(value, dict):
        amount = value.get(currency)
        if amount is not None:
            return float(amount)
    return None


def _select_currency(payload: dict) -> str:
    """Pick the portfolio currency from the collector-tagged ``market``.

    US (overseas) accounts report USD; KR and crypto (Upbit, KRW-denominated)
    use KRW. The market tag is collector-provided, so the stage needs no
    StageContext signal.
    """
    market = str(payload.get("market") or "kr").strip().lower()
    return "usd" if market == "us" else "krw"


def _krw_totals(payload: dict) -> tuple[float, float, float]:
    """``(nav_krw, buying_power_krw, cash_krw)`` — unchanged ROB-314 behaviour.

    NAV = ``sum(holdings value_krw) + cash.krw``; legacy flat keys
    (``nav_krw`` / ``buying_power_krw`` / ``cash_krw``) are honoured as a
    fallback. All three are floats (absent buying power defaults to 0.0 to
    preserve the existing KR contract).
    """
    cash_krw = _nested_amount(payload.get("cash"), "krw")
    if cash_krw is None:
        cash_krw = float(payload.get("cash_krw") or 0.0)

    buying_power_krw = _nested_amount(payload.get("buying_power"), "krw")
    if buying_power_krw is None:
        buying_power_krw = float(payload.get("buying_power_krw") or 0.0)

    explicit_nav = payload.get("nav_krw")
    if explicit_nav is None:
        explicit_nav = payload.get("total_evaluation_krw")
    if explicit_nav is not None:
        nav_krw = float(explicit_nav)
    else:
        holdings = payload.get("holdings") or []
        holdings_value_krw = sum(
            float(h.get("value_krw") or 0.0) for h in holdings if isinstance(h, dict)
        )
        nav_krw = holdings_value_krw + cash_krw

    return nav_krw, buying_power_krw, cash_krw


def _usd_totals(payload: dict) -> tuple[float | None, float | None, float | None]:
    """``(nav_usd, buying_power_usd, cash_usd)`` for a US (overseas) account.

    ROB-366 B7: honest, no fabrication and no cross-currency arithmetic. Cash /
    buying power come from the ``usd`` sub-key only (``None`` when absent —
    e.g. the documented OPSQ0002 overseas-cash case). NAV is summed from
    ``holdings[].value_native`` (the native USD value) plus USD cash; if ANY
    holding lacks ``value_native`` the sum cannot be done honestly so NAV is
    ``None`` (never falls back to the KRW-normalized ``value_krw``).
    """
    cash_usd = _nested_amount(payload.get("cash"), "usd")
    buying_power_usd = _nested_amount(payload.get("buying_power"), "usd")

    holdings = [h for h in (payload.get("holdings") or []) if isinstance(h, dict)]
    natives = [h.get("value_native") for h in holdings]
    if any(n is None for n in natives):
        nav_usd: float | None = None  # cannot sum honestly — no value_krw fallback
    elif not holdings and cash_usd is None:
        nav_usd = None  # nothing to report
    else:
        nav_usd = sum(float(n) for n in natives) + (cash_usd or 0.0)

    return nav_usd, buying_power_usd, cash_usd


class PortfolioJournalStage:
    stage_type = "portfolio_journal"

    async def run(self, context: StageContext) -> StageArtifactPayload:
        portfolio_snaps = context.snapshots_for("portfolio")
        if not portfolio_snaps:
            raise UnavailableStageError("portfolio snapshot missing — required")
        portfolio = portfolio_snaps[0]
        payload = portfolio.payload_json or {}
        journal_snaps = context.snapshots_for("journal")

        currency = _select_currency(payload)
        if currency == "usd":
            nav, buying_power, _cash = _usd_totals(payload)
        else:
            nav, buying_power, _cash = _krw_totals(payload)

        entries = []
        for snap in journal_snaps:
            entries.extend((snap.payload_json or {}).get("entries", []))
        symbols = ", ".join(e.get("symbol", "?") for e in entries[:5])

        citations = [
            StageCitation(
                snapshot_uuid=portfolio.snapshot_uuid,
                snapshot_kind="portfolio",
                payload_path=f"$.buying_power.{currency}",
            )
        ]
        for snap in journal_snaps:
            citations.append(
                StageCitation(
                    snapshot_uuid=snap.snapshot_uuid,
                    snapshot_kind="journal",
                    payload_path="$.entries",
                )
            )

        missing_data = [] if journal_snaps else ["journal"]
        key_points = [e.get("thesis", "") for e in entries[:5] if e.get("thesis")]

        if currency == "krw":
            # Byte-identical legacy KR contract: buying power is always a float
            # (flat-key default 0.0) and the ratio is always shown.
            bp_ratio = (buying_power / nav) if nav > 0 else 0.0
            summary = (
                f"NAV={nav:,.0f}, buying_power_krw={buying_power:,.0f} "
                f"({bp_ratio:.1%}), open journal: {symbols or 'none'}"
            )
            confidence = 60 if bp_ratio >= 0.05 else 40
            risk_evidence = [] if bp_ratio >= 0.05 else ["buying_power < 5% NAV"]
        else:
            # USD: surface unavailable explicitly; absence is not a low-buying-
            # power risk signal and must not punish confidence.
            nav_str = f"{nav:,.0f}" if nav is not None else "unavailable"
            if buying_power is None:
                bp_ratio = None
                bp_segment = "buying_power_usd=unavailable"
                missing_data = [*missing_data, "buying_power_usd"]
            else:
                bp_ratio = (
                    (buying_power / nav) if (nav is not None and nav > 0) else None
                )
                ratio_str = f" ({bp_ratio:.1%})" if bp_ratio is not None else ""
                bp_segment = f"buying_power_usd={buying_power:,.0f}{ratio_str}"
            summary = (
                f"NAV(USD)={nav_str}, {bp_segment}, open journal: {symbols or 'none'}"
            )
            if bp_ratio is not None and bp_ratio < 0.05:
                confidence, risk_evidence = 40, ["buying_power < 5% NAV"]
            else:
                confidence, risk_evidence = 60, []

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=StageVerdict.NEUTRAL,
            confidence=confidence,
            summary=summary,
            key_points=key_points,
            risk_evidence=risk_evidence,
            cited_snapshots=citations,
            missing_data=missing_data,
        )
