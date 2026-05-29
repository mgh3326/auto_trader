"""Pure transformer from normalized screener rows to CandidateEvidence
(ROB-304). No DB access, no I/O — fixture-testable."""

from __future__ import annotations

from typing import Any

from app.services.screener_evidence import scoring
from app.services.screener_evidence.models import CandidateEvidence

_MOMENTUM_REASON = "단기 상승 모멘텀 후보"
_OVERSOLD_REASON = "RSI 저점권 후보"
_HIGH_VOLUME_REASON = "24시간 KRW 거래대금 상위"
_WARNING_FLAG = "Upbit 유의 종목"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _source_of(row: dict[str, Any], market: str) -> str:
    raw = str(row.get("source") or "").strip().lower()
    if market == "crypto":
        if raw in {"tvscreener", "tvscreener_upbit"}:
            return "tvscreener_upbit"
        if raw in {"upbit", "upbit_official"}:
            return "upbit_official"
        return "external_reference" if raw else "mcp_screen_stocks"
    # equity
    if raw in {"kis", "yahoo"}:
        return raw
    return "external_reference" if raw else "mcp_screen_stocks"


def _risk_flags(row: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if row.get("market_warning") or row.get("warning"):
        flags.append(_WARNING_FLAG)
    return flags


def build_candidate_evidence(
    *, market: str, preset: str, rows: list[dict[str, Any]]
) -> list[CandidateEvidence]:
    """Normalize rows into scored, sorted (desc) CandidateEvidence."""
    if not rows:
        return []

    # high_volume needs batch ranking by turnover.
    volume_rank: dict[int, int] = {}
    if preset == "crypto_high_volume":
        ordered = sorted(
            range(len(rows)),
            key=lambda i: _to_float(rows[i].get("trade_amount_24h")) or 0.0,
            reverse=True,
        )
        volume_rank = {row_idx: rank for rank, row_idx in enumerate(ordered)}

    out: list[CandidateEvidence] = []
    for idx, row in enumerate(rows):
        change_rate = _to_float(row.get("change_rate"))
        rsi = _to_float(row.get("rsi"))
        price = _to_float(row.get("price") or row.get("latest_close"))

        if preset == "crypto_oversold":
            score = scoring.oversold_score(rsi)
            score_label = f"RSI {rsi:.1f}" if rsi is not None else "-"
            reasons = [_OVERSOLD_REASON]
            volume_value = _to_float(row.get("trade_amount_24h"))
        elif preset == "crypto_high_volume":
            volume_value = _to_float(row.get("trade_amount_24h"))
            score = scoring.rank_score(volume_rank.get(idx, idx), len(rows))
            score_label = (
                f"거래대금 {int(volume_value):,}" if volume_value is not None else "-"
            )
            reasons = [_HIGH_VOLUME_REASON]
        else:  # crypto_momentum + equity top_gainers
            score = scoring.momentum_score(change_rate)
            score_label = f"{change_rate:+.2f}%" if change_rate is not None else "-"
            reasons = [_MOMENTUM_REASON]
            volume_value = _to_float(
                row.get("trade_amount_24h") or row.get("daily_volume")
            )
            up_days = row.get("consecutive_up_days")
            if isinstance(up_days, int) and up_days >= 2:
                reasons.append(f"{up_days}일 연속 상승")

        out.append(
            CandidateEvidence(
                symbol=str(row.get("symbol")),
                market=market,
                name=str(row.get("name") or row.get("symbol") or ""),
                score=round(score, 4),
                score_label=score_label,
                change_rate=change_rate,
                price=price,
                volume_value=volume_value,
                reasons=reasons,
                source=_source_of(row, market),
                risk_flags=_risk_flags(row),
                source_preset=preset,
            )
        )

    out.sort(key=lambda e: e.score, reverse=True)
    return out
