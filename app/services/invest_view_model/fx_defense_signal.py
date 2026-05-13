"""Pure ROB-218 USD/KRW 1500 defense-signal scoring helpers.

The scorer is intentionally deterministic and side-effect free. It transforms
already-collected dashboard inputs into the existing FX dashboard transport
schema; it does not call providers, databases, broker/order tooling, or
schedulers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.invest_fx_dashboard import (
    FxDashboardDefenseSignal,
    FxDashboardEvidenceItem,
)

DEFENSE_THRESHOLD_LEVEL = 1500.0
NEAR_THRESHOLD_DISTANCE_PCT = 0.75


@dataclass(frozen=True)
class DefenseScoringInput:
    spot: float | None
    threshold: float = DEFENSE_THRESHOLD_LEVEL
    recent_high: float | None = None
    recent_close_or_last: float | None = None
    rejected_within_minutes: int | None = None
    global_dollar_change_pct: float | None = None
    usdcnh_change_pct: float | None = None
    usd_jpy_change_pct: float | None = None
    krw_cross_change_pcts: dict[str, float | None] = field(default_factory=dict)
    news_context: list[FxDashboardEvidenceItem] = field(default_factory=list)
    authority_context: list[FxDashboardEvidenceItem] = field(default_factory=list)
    after_verification_has_strong_evidence: bool = False


def _threshold_state(*, level: float, spot: float | None) -> str:
    """Classify proximity to a dashboard threshold.

    For the 1500 USD/KRW defense level, ``near`` covers the 0.75% band below
    the level and ``breached`` covers spot at/above the level. Missing spot is
    treated as watch so the caller can separately mark source freshness.
    """
    if spot is None:
        return "watch"
    if spot >= level:
        return "breached"
    if 0 <= level - spot <= level * (NEAR_THRESHOLD_DISTANCE_PCT / 100):
        return "near"
    return "watch"


def _score_defense_signal(
    scoring_input: DefenseScoringInput,
) -> FxDashboardDefenseSignal:
    reasons: list[str] = []
    evidence: list[FxDashboardEvidenceItem] = []
    score = 0

    threshold = scoring_input.threshold
    spot = scoring_input.spot

    if spot is None:
        reasons.append("USD/KRW 현물 미수집")
        evidence.append(
            FxDashboardEvidenceItem(
                kind="price",
                labelKo="USD/KRW 현물 미수집",
                value=None,
                source="fixture_usdkrw_spot",
                dataState="missing",
            )
        )
    else:
        gap = threshold - spot
        evidence.append(
            FxDashboardEvidenceItem(
                kind="price",
                labelKo="USD/KRW spot",
                value=f"{spot:.2f}",
                source="fixture_usdkrw_spot",
                dataState="fresh",
            )
        )
        if spot >= threshold:
            score += 35
            reasons.append("1500원 상향 돌파")
        elif 0 <= gap <= 1.5:
            score += 30
            reasons.append("1500원 1.5원 이내 근접")
        elif 1.5 < gap <= 5:
            score += 22
            reasons.append("1500원 5원 이내 근접")
        elif 5 < gap <= 15:
            score += 12
            reasons.append("1500원 감시권 접근")

    rejection_points = _score_rejection(scoring_input, reasons, evidence)
    score += rejection_points

    divergence_points = _score_divergence(scoring_input, reasons, evidence)
    score += divergence_points

    context_points = _score_context(scoring_input, reasons, evidence)
    score += context_points

    score = min(score, 100)
    state, confidence, label, summary = _map_signal(score, scoring_input)

    if not scoring_input.after_verification_has_strong_evidence:
        evidence.append(
            FxDashboardEvidenceItem(
                kind="missing_context",
                labelKo="공식/딜러/NDF 사후 검증 자료 미수집",
                value=None,
                source="official_after_verification",
                dataState="missing",
            )
        )
        if "사후 검증 자료 없음" not in reasons:
            reasons.append("사후 검증 자료 없음")

    return FxDashboardDefenseSignal(
        state=state,
        score=score,
        confidence=confidence,
        labelKo=label,
        summaryKo=summary,
        reasonsKo=reasons,
        evidence=evidence,
        notConfirmedIntervention=True,
        needsAfterVerification=state != "none"
        or not scoring_input.after_verification_has_strong_evidence,
    )


def _score_rejection(
    scoring_input: DefenseScoringInput,
    reasons: list[str],
    evidence: list[FxDashboardEvidenceItem],
) -> int:
    high = scoring_input.recent_high
    close = scoring_input.recent_close_or_last
    if high is None or close is None:
        reasons.append("상단 꼬리 판단 자료 미수집")
        evidence.append(
            FxDashboardEvidenceItem(
                kind="rejection",
                labelKo="1500원 부근 상단 꼬리/되밀림 자료 미수집",
                value=None,
                source="fixture_usdkrw_intraday",
                dataState="missing",
            )
        )
        return 0

    pullback = high - close
    evidence.append(
        FxDashboardEvidenceItem(
            kind="rejection",
            labelKo="1500원 부근 고점 대비 되밀림",
            value=f"high {high:.2f} / last {close:.2f} / pullback {pullback:.2f}",
            source="fixture_usdkrw_intraday",
            dataState="fresh",
        )
    )
    window_suffix = (
        f" ({scoring_input.rejected_within_minutes}분 이내)"
        if scoring_input.rejected_within_minutes is not None
        else ""
    )
    if high >= 1499.5 and pullback >= 2.0:
        reasons.append(f"1500원 직전 상단 꼬리/되밀림{window_suffix}")
        return 25
    if high >= 1498.0 and pullback >= 1.0:
        reasons.append(f"1500원 부근 되밀림{window_suffix}")
        return 15
    return 0


def _score_divergence(
    scoring_input: DefenseScoringInput,
    reasons: list[str],
    evidence: list[FxDashboardEvidenceItem],
) -> int:
    points = 0
    dollar_firm = _is_global_dollar_firm(scoring_input)
    usdkrw_failed_near = _usdkrw_failed_near_1500(scoring_input)
    missing_global = (
        scoring_input.global_dollar_change_pct is None
        and scoring_input.usdcnh_change_pct is None
        and scoring_input.usd_jpy_change_pct is None
    )
    missing_crosses = not scoring_input.krw_cross_change_pcts or all(
        value is None for value in scoring_input.krw_cross_change_pcts.values()
    )

    if dollar_firm and usdkrw_failed_near:
        points += 15
        reasons.append("글로벌 달러 강세 대비 USD/KRW 상단 제한")
        evidence.append(
            FxDashboardEvidenceItem(
                kind="divergence",
                labelKo="글로벌 달러 강세 대비 USD/KRW 상단 제한",
                value=_format_global_dollar_value(scoring_input),
                source="fixture_global_dollar",
                dataState="stale",
            )
        )

    negative_crosses = [
        symbol
        for symbol, value in scoring_input.krw_cross_change_pcts.items()
        if value is not None and value < 0
    ]
    if dollar_firm and len(negative_crosses) >= 2:
        points += 10
        reasons.append("원화 교차환율 동반 강세")
        evidence.append(
            FxDashboardEvidenceItem(
                kind="divergence",
                labelKo="원화 교차환율 동반 강세",
                value=", ".join(negative_crosses),
                source="fixture_krw_crosses",
                dataState="stale",
            )
        )

    if missing_global or missing_crosses:
        reasons.append("글로벌 달러/원화 교차 비교 일부 미수집")
        evidence.append(
            FxDashboardEvidenceItem(
                kind="missing_context",
                labelKo="글로벌 달러/원화 교차 비교 일부 미수집",
                value=None,
                source="fixture_global_dollar",
                dataState="missing",
            )
        )

    return min(points, 25)


def _score_context(
    scoring_input: DefenseScoringInput,
    reasons: list[str],
    evidence: list[FxDashboardEvidenceItem],
) -> int:
    """Attach news/authority context without letting context-only evidence score.

    ROB-218 treats news, authority comments, dealer context, and NDF references as
    explanatory evidence for the user-facing card. Price/rejection/divergence
    drive the deterministic score; context changes confidence wording only via
    ``after_verification_has_strong_evidence`` in ``_map_signal``.
    """
    if scoring_input.news_context:
        reasons.append("환율/당국 경계 뉴스 확인")
        evidence.extend(scoring_input.news_context)

    if scoring_input.authority_context:
        evidence.extend(scoring_input.authority_context)
        if scoring_input.after_verification_has_strong_evidence:
            reasons.append("사후 검증 근거 일부 확인")
        else:
            reasons.append("공식/딜러/NDF 사후 근거 확인 필요")

    return 0


def _map_signal(
    score: int,
    scoring_input: DefenseScoringInput,
) -> tuple[str, str, str, str]:
    if score < 20:
        return (
            "none",
            "low",
            "방어 신호 낮음",
            "1500원 방어성 수급 신호는 낮습니다.",
        )
    if score < 45:
        return (
            "watch",
            "low",
            "당국 경계감/방어성 수급 감시",
            "1500원 접근으로 당국 경계감/방어성 수급 감시는 필요하지만 확정 개입 근거는 없습니다.",
        )
    if score < 70:
        return (
            "elevated",
            "high"
            if scoring_input.after_verification_has_strong_evidence
            else "medium",
            "1500원 부근 방어성 오퍼 의심",
            "1500원 부근 가격 되밀림과 비교 지표가 겹쳐 방어성 오퍼 가능성이 높아졌지만 확정 개입으로 볼 수는 없습니다.",
        )
    return (
        "after_verification_required",
        "high" if scoring_input.after_verification_has_strong_evidence else "medium",
        "사후 검증 필요한 방어성 수급 신호",
        "1500원 부근 방어성 수급 신호가 강하지만 공식 발표·딜러 코멘트·NDF 등 사후 검증 전까지 확정 개입으로 표현하지 않습니다.",
    )


def _is_global_dollar_firm(scoring_input: DefenseScoringInput) -> bool:
    global_dollar = scoring_input.global_dollar_change_pct
    usdcnh = scoring_input.usdcnh_change_pct
    return (global_dollar is not None and global_dollar >= 0.2) or (
        usdcnh is not None and usdcnh >= 0.15
    )


def _usdkrw_failed_near_1500(scoring_input: DefenseScoringInput) -> bool:
    high = scoring_input.recent_high
    close = scoring_input.recent_close_or_last
    return high is not None and close is not None and high >= 1498.0 and close < high


def _format_global_dollar_value(scoring_input: DefenseScoringInput) -> str | None:
    parts: list[str] = []
    if scoring_input.global_dollar_change_pct is not None:
        parts.append(f"DXY {scoring_input.global_dollar_change_pct:+.2f}%")
    if scoring_input.usdcnh_change_pct is not None:
        parts.append(f"USDCNH {scoring_input.usdcnh_change_pct:+.2f}%")
    if scoring_input.usd_jpy_change_pct is not None:
        parts.append(f"USDJPY {scoring_input.usd_jpy_change_pct:+.2f}%")
    return ", ".join(parts) if parts else None
