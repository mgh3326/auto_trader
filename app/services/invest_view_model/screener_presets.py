"""ROB-147 — static catalog of /invest screener presets and the deterministic
mapping from preset id to underlying screening filter parameters.

Each preset's filter mapping is intentionally simple and bounded.
The consecutive_gainers preset now applies a real min_consecutive_up_days=5 filter
via the OHLCV-backed enrichment pipeline."""

from __future__ import annotations

from app.schemas.invest_screener import (
    ScreenerFilterChip,
    ScreenerParityStatus,
    ScreenerPreset,
    ScreenerPresetOrigin,
)

# ROB-359 Scope B — short aliases for catalog provenance metadata.
_TOSS: ScreenerPresetOrigin = "toss_parity"
_AT_OWN: ScreenerPresetOrigin = "auto_trader_original"
_FULL: ScreenerParityStatus = "full"
_PARTIAL: ScreenerParityStatus = "partial"
_MISMATCH: ScreenerParityStatus = "mismatch"

DEFAULT_PRESET_ID = "consecutive_gainers"
CONSECUTIVE_GAINERS_LIMIT = 80
CRYPTO_DEFAULT_PRESET_ID = "crypto_high_volume"
_KR_ONLY_PRESET_IDS = {
    "investor_flow_momentum",
    "double_buy",
    "high_yield_value",
    "undervalued_breakout",
    "profitable_company",
    "undervalued_growth",
    "stable_growth",
    "future_dividend_king",
    "cheap_value",
    "steady_dividend",
    "growth_expectation_toss",
    "support_proximity",
}

# ROB-427: per-market availability. Instead of HIDING the KR-only presets for US
# (the old hard-filter), the US catalog now exposes them with an honest status.
# unsupported = no US equivalent (KR 외국인·기관 수급); data_pending = catalogued
# but disabled until a US data source exists (Yahoo valuation / US fundamentals).
# The remaining KR-only presets default to data_pending via _KR_ONLY_PRESET_IDS.
# ROB-427 PR3: KR-only presets that ARE active for US (backed by US data now).
# high_yield_value (ROE+PER) runs on Yahoo valuation snapshots — see
# high_yield_value_screener.load_high_yield_value_from_snapshots(market="us").
# ROB-440: undervalued_breakout runs on the same Yahoo valuation snapshots
# (high_52w_date recency) — load_undervalued_breakout_from_snapshots(market="us").
# ROB-441 PR3: profitable_company/undervalued_growth/cheap_value/stable_growth run on
# the market-parameterized derive loader (market_valuation US per/pbr/roe +
# financial_fundamentals US annual periods → derive) — load_fundamentals_preset_from_snapshots(market="us").
# ROB-441 PR4: growth_expectation_toss (QoQ) runs on yfinance quarterly income.
# ROB-441 PR5: steady_dividend / future_dividend_king run on yfinance dividends
# (per-share) + cashflow dividends-paid → payout_ratio + dividend streaks (derive).
# Every Toss fundamentals preset is now US-active; only the supply-flow presets
# (double_buy / investor_flow_momentum) remain KR-only (no US equivalent feed).
_US_ACTIVE_PRESET_IDS = {
    "high_yield_value",
    "undervalued_breakout",
    "profitable_company",
    "undervalued_growth",
    "cheap_value",
    "stable_growth",
    "growth_expectation_toss",
    "steady_dividend",
    "future_dividend_king",
}
# ROB-976: support_proximity is a KR-first rollout scoping choice, not a
# structural no-US-equivalent gap (get_support_resistance already runs for
# equity_us) — but ROB-441 PR5 retired data_pending entirely (see
# test_us_no_fundamentals_presets_data_pending), so it is classified
# unsupported-with-reason until the US follow-up lands, same as the flow presets.
_US_UNSUPPORTED_PRESET_IDS = {
    "double_buy",
    "investor_flow_momentum",
    "support_proximity",
}
_US_UNSUPPORTED_REASON = "외국인·기관 수급은 국내 전용 지표입니다"
_US_UNSUPPORTED_REASON_OVERRIDES: dict[str, str] = {
    "support_proximity": "지지선 근접 스캔은 KR 우선 롤아웃 — US는 후속 예정",
}
_US_DATA_PENDING_REASON: dict[str, str] = {}


def _preset_availability(preset_id: str, market: str) -> tuple[str, str | None]:
    """(availability, reason) for a preset in a market (ROB-427).

    KR/crypto: every catalogued preset is ``active``. US: ``unsupported`` for
    KR-only flow presets, ``data_pending`` for the rest of the KR-only set
    (disabled with an honest reason, never fabricated), ``active`` otherwise.
    """
    if market != "us":
        return "active", None
    if preset_id in _US_ACTIVE_PRESET_IDS:  # ROB-427 PR3: Yahoo-backed US activation
        return "active", None
    if preset_id in _US_UNSUPPORTED_PRESET_IDS:
        return "unsupported", _US_UNSUPPORTED_REASON_OVERRIDES.get(
            preset_id, _US_UNSUPPORTED_REASON
        )
    if preset_id in _KR_ONLY_PRESET_IDS:
        return "data_pending", _US_DATA_PENDING_REASON.get(
            preset_id, "미국 데이터 준비중"
        )
    return "active", None


SCREENER_PRESETS: list[ScreenerPreset] = [
    ScreenerPreset(
        id="consecutive_gainers",
        name="연속 상승세",
        description="일주일 연속 상승세를 보이는 주식",
        badges=["인기"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="주가등락률", detail="1주일 전 보다 · 0% 이상"),
            ScreenerFilterChip(label="주가 연속상승", detail="5일 연속 상승"),
        ],
        metricLabel="주가등락률",
        market="kr",
        presetOrigin=_TOSS,
        parityStatus=_FULL,
    ),
    ScreenerPreset(
        id="cheap_value",
        name="아직 저렴한 가치주",
        description="PER·PBR이 낮으면서 순이익이 역성장하지 않는 저평가 종목 (지연 스냅샷 기반)",
        badges=[],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="PER", detail="0~15"),
            ScreenerFilterChip(label="PBR", detail="0~1.5"),
            ScreenerFilterChip(label="순이익증가율", detail="3년평균 0% 이상"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        # metricLabel matches the result-ordering metric (sort_by=earnings_growth_3y_avg);
        # PER/PBR remain the headline filter conditions, shown via filterChips above.
        metricLabel="순이익증가율",
        market="kr",
        presetOrigin=_TOSS,
        parityStatus=_FULL,
    ),
    ScreenerPreset(
        id="steady_dividend",
        name="꾸준한 배당주",
        description="배당수익률·배당성향이 높고 배당 연속지급·순이익 연속증가를 갖춘 종목 (지연 스냅샷 기반)",
        badges=["인기"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="배당수익률", detail="3% 이상"),
            ScreenerFilterChip(label="배당성향", detail="30% 이상"),
            ScreenerFilterChip(label="배당", detail="연속지급 3년 이상"),
            ScreenerFilterChip(label="순이익", detail="연속증가 3년 이상"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="배당수익률",
        market="kr",
        presetOrigin=_TOSS,
        parityStatus=_FULL,
    ),
    ScreenerPreset(
        id="oversold_recovery",
        name="과매도 반등 (RSI)",
        description="RSI가 30 이하 과매도 구간에 들어온 종목 (auto_trader 자체 스크린)",
        badges=["인기"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="RSI", detail="30 이하"),
        ],
        metricLabel="RSI",
        market="kr",
        presetOrigin=_AT_OWN,
        parityNote=(
            "auto_trader 자체 스크린(RSI ≤ 30 과매도 반등). "
            "Toss '저평가 탈출'(PER 0~10 + PBR 0~1 + 신고가)과는 별개이며, "
            "Toss 의미 프리셋은 별도(PR2c-2)로 추가 예정."
        ),
    ),
    ScreenerPreset(
        id="kr_high_volume_surge",
        name="거래량 급증",
        description="거래량이 폭발적으로 늘어난 종목",
        badges=[],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="거래량", detail="상위"),
        ],
        metricLabel="거래량",
        market="kr",
        presetOrigin=_AT_OWN,
        parityNote="auto_trader 자체 프리셋 (Toss 기본 골라보기에 없음).",
    ),
    ScreenerPreset(
        id="investor_flow_momentum",
        name="수급 모멘텀",
        description="외국인 연속 순매수 흐름이 강한 종목 (스냅샷 기반)",
        badges=["MVP"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="투자자별 수급", detail="외국인 3일+ 연속 순매수"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="외국인 순매수",
        market="kr",
        presetOrigin=_AT_OWN,
        parityNote=(
            "auto_trader 자체 프리셋 (Toss 기본 골라보기에 없음). "
            "외국인 연속 순매수 중심으로, '쌍끌이 매수'(double_buy)와는 별개."
        ),
    ),
    ScreenerPreset(
        id="double_buy",
        name="쌍끌이 매수",
        description="기관과 외국인이 동시에 매수하는 종목",
        badges=["NEW"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="외국인", detail="순매수"),
            ScreenerFilterChip(label="기관", detail="순매수"),
            ScreenerFilterChip(label="주가등락률", detail="1일 ≥ 0%"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="주가등락률",
        market="kr",
        presetOrigin=_TOSS,
        parityStatus=_FULL,
    ),
    ScreenerPreset(
        id="support_proximity",
        name="지지선 근접",
        description=(
            "우량주 중 최근접 지지선(피보나치·거래량프로파일·볼린저밴드 클러스터링)까지 "
            "거리가 가장 가까운 종목 (auto_trader 자체 스크린, 지연 스냅샷 기반)"
        ),
        badges=["NEW"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="시가총액", detail="3천억원 이상"),
            ScreenerFilterChip(label="거래대금", detail="10억원 이상"),
            ScreenerFilterChip(label="지지선까지 거리", detail="가까운 순"),
            ScreenerFilterChip(
                label="데이터", detail="지연 스냅샷 + 상위 후보 실시간 재검증"
            ),
        ],
        metricLabel="지지선까지 거리",
        market="kr",
        presetOrigin=_AT_OWN,
        parityNote=(
            "auto_trader 자체 프리셋 (Toss 기본 골라보기에 없음). "
            "get_support_resistance와 동일한 클러스터링 로직을 유니버스 스캔에 재사용. "
            "심볼 단위 상세는 get_support_resistance, 시세 재검증은 get_quote를 이용할 것. "
            "지지선이 없는(현재가 아래 클러스터 없음) 종목은 결과에서 제외됨. KR 우선, US는 후속."
        ),
    ),
    ScreenerPreset(
        id="growth_expectation",
        name="대형 모멘텀 (시총·등락률)",
        description="시가총액이 충분하고 등락률 상위인 대형 모멘텀 종목 (auto_trader 자체 스크린)",
        badges=[],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="시가총액", detail="1조 이상"),
            ScreenerFilterChip(label="주가등락률", detail="상위"),
        ],
        metricLabel="주가등락률",
        market="kr",
        presetOrigin=_AT_OWN,
        parityNote=(
            "auto_trader 자체 스크린(시가총액 ≥ 1조 + 등락률 상위). "
            "Toss '성장 기대주'(순이익 3년 성장 + 직전분기 순이익 성장)와는 별개이며, "
            "Toss 의미 프리셋은 분기 재무 수집 후 별도 이슈로 추가 예정."
        ),
    ),
    ScreenerPreset(
        id="growth_expectation_toss",
        name="성장 기대주",
        description="3년 평균 순이익 증감률 3% 이상 및 직전분기 대비 순이익 증감률(QoQ) 10% 이상 종목 (지연 스냅샷 기반)",
        badges=[],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="순이익증가율", detail="3년평균 3%+"),
            ScreenerFilterChip(label="순이익", detail="직전분기 대비 10%+"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="순이익증가율",
        market="kr",
        presetOrigin=_TOSS,
        parityStatus=_FULL,
    ),
    ScreenerPreset(
        id="high_yield_value",
        name="고수익 저평가",
        description="ROE가 높으면서 PER이 낮은 고수익·저평가 종목 (지연 스냅샷 기반)",
        badges=[],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="ROE", detail="15% 이상"),
            ScreenerFilterChip(label="PER", detail="0~10"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="ROE",
        market="kr",
        presetOrigin=_TOSS,
        parityStatus=_FULL,
    ),
    ScreenerPreset(
        id="undervalued_breakout",
        name="저평가 탈출",
        description="PER·PBR이 낮고 최근 20거래일 내 52주 신고가를 기록한 저평가 탈출 종목 (지연 스냅샷 기반)",
        badges=["인기"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="PER", detail="0~10"),
            ScreenerFilterChip(label="PBR", detail="0~1"),
            ScreenerFilterChip(label="신고가", detail="최근 20거래일 이내"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="신고가 경과",
        market="kr",
        presetOrigin=_TOSS,
        parityStatus=_FULL,
    ),
    ScreenerPreset(
        id="profitable_company",
        name="돈 잘버는 회사",
        description="매출총이익률(TTM)과 ROE가 모두 높은 고수익성 기업 (지연 스냅샷 기반)",
        badges=[],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="매출총이익률", detail="TTM 20% 이상"),
            ScreenerFilterChip(label="ROE", detail="15% 이상"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="ROE",
        market="kr",
        presetOrigin=_TOSS,
        parityStatus=_FULL,
    ),
    ScreenerPreset(
        id="undervalued_growth",
        name="저평가 성장주",
        description="저평가(PER)면서 매출·순이익이 꾸준히 성장하는 기업",
        badges=["국내"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="PER", detail="0~20"),
            ScreenerFilterChip(label="매출증가율", detail="3년평균 10% 이상"),
            ScreenerFilterChip(label="순이익증가율", detail="3년평균 20% 이상"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="순이익증가율",
        market="kr",
        presetOrigin=_TOSS,
        parityStatus=_FULL,
    ),
    ScreenerPreset(
        id="stable_growth",
        name="안정 성장주",
        description="높은 ROE와 꾸준한 순이익 성장·연속증가를 갖춘 안정 성장 기업",
        badges=["국내"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="ROE", detail="15% 이상"),
            ScreenerFilterChip(label="순이익증가율", detail="3년평균 10% 이상"),
            ScreenerFilterChip(label="순이익", detail="연속증가 3년 이상"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="ROE",
        market="kr",
        presetOrigin=_TOSS,
        parityStatus=_FULL,
    ),
    ScreenerPreset(
        id="future_dividend_king",
        name="미래의 배당왕",
        description="배당을 꾸준히 늘리고 순이익도 연속 증가하는 미래 배당 성장 기업",
        badges=["국내"],
        filterChips=[
            ScreenerFilterChip(label="국내", detail=None),
            ScreenerFilterChip(label="배당수익률", detail="1% 이상"),
            ScreenerFilterChip(label="배당", detail="연속성장 3년 이상"),
            ScreenerFilterChip(label="순이익", detail="연속증가 3년 이상"),
            ScreenerFilterChip(label="배당성향", detail="30% 이상"),
            ScreenerFilterChip(label="데이터", detail="지연 스냅샷 기반"),
        ],
        metricLabel="배당수익률",
        market="kr",
        presetOrigin=_TOSS,
        parityStatus=_FULL,
    ),
]


CRYPTO_SCREENER_PRESETS: list[ScreenerPreset] = [
    ScreenerPreset(
        id="crypto_high_volume",
        name="거래대금 상위 코인",
        description="Upbit 거래대금이 큰 가상자산",
        badges=["가상자산"],
        filterChips=[
            ScreenerFilterChip(label="가상자산", detail=None),
            ScreenerFilterChip(label="거래대금", detail="24시간 상위"),
        ],
        metricLabel="거래대금",
        market="crypto",
        presetOrigin=_AT_OWN,
        parityNote="auto_trader 자체 가상자산 프리셋 (Toss 국내주식 골라보기 대상 아님).",
    ),
    ScreenerPreset(
        id="crypto_oversold",
        name="저RSI 반등 후보",
        description="RSI가 낮아 과매도 구간에 가까운 가상자산",
        badges=[],
        filterChips=[
            ScreenerFilterChip(label="가상자산", detail=None),
            ScreenerFilterChip(label="RSI", detail="35 이하"),
        ],
        metricLabel="RSI",
        market="crypto",
        presetOrigin=_AT_OWN,
        parityNote="auto_trader 자체 가상자산 프리셋 (Toss 국내주식 골라보기 대상 아님).",
    ),
    ScreenerPreset(
        id="crypto_momentum",
        name="상승률 상위 코인",
        description="단기 상승률이 높은 Upbit 가상자산",
        badges=[],
        filterChips=[
            ScreenerFilterChip(label="가상자산", detail=None),
            ScreenerFilterChip(label="등락률", detail="상위"),
        ],
        metricLabel="등락률",
        market="crypto",
        presetOrigin=_AT_OWN,
        parityNote="auto_trader 자체 가상자산 프리셋 (Toss 국내주식 골라보기 대상 아님).",
    ),
    ScreenerPreset(
        id="crypto_funding_squeeze",
        name="펀딩비 음수 (숏 과열)",
        description="펀딩비가 음수(숏이 롱에 지급) — 숏 쏠림으로 숏스퀴즈 가능성이 있는 가상자산",
        badges=["가상자산"],
        filterChips=[
            ScreenerFilterChip(label="가상자산", detail=None),
            ScreenerFilterChip(label="펀딩비", detail="음수 (숏 과열)"),
        ],
        metricLabel="펀딩비",
        market="crypto",
        presetOrigin=_AT_OWN,
        parityNote="auto_trader 자체 가상자산 프리셋 (선물 펀딩비 기반, Toss 대상 아님).",
    ),
    ScreenerPreset(
        id="crypto_funding_overheated",
        name="펀딩비 과열 (롱 과열)",
        description="펀딩비가 높은 양수(롱이 숏에 지급) — 롱 쏠림으로 되돌림 주의가 필요한 가상자산",
        badges=["가상자산"],
        filterChips=[
            ScreenerFilterChip(label="가상자산", detail=None),
            ScreenerFilterChip(label="펀딩비", detail="높은 양수 (롱 과열)"),
        ],
        metricLabel="펀딩비",
        market="crypto",
        presetOrigin=_AT_OWN,
        parityNote="auto_trader 자체 가상자산 프리셋 (선물 펀딩비 기반, Toss 대상 아님).",
    ),
    ScreenerPreset(
        id="crypto_oi_surge",
        name="미결제약정 급증",
        description="24시간 미결제약정(OI)이 크게 늘어난 가상자산 — 신규 자금/포지션 유입 신호",
        badges=["가상자산"],
        filterChips=[
            ScreenerFilterChip(label="가상자산", detail=None),
            ScreenerFilterChip(label="미결제약정", detail="24시간 증가 상위"),
        ],
        metricLabel="OI 변화",
        market="crypto",
        presetOrigin=_AT_OWN,
        parityNote="auto_trader 자체 가상자산 프리셋 (선물 미결제약정 기반, Toss 대상 아님).",
    ),
    ScreenerPreset(
        id="crypto_long_short_skew",
        name="롱숏 쏠림 (리테일)",
        description="리테일 롱숏 계정 비율이 1에서 가장 벗어난 가상자산 — 포지션 쏠림/역추세 참고 신호",
        badges=["가상자산"],
        filterChips=[
            ScreenerFilterChip(label="가상자산", detail=None),
            ScreenerFilterChip(label="롱숏비율", detail="1에서 가장 치우침"),
        ],
        metricLabel="롱숏비율",
        market="crypto",
        presetOrigin=_AT_OWN,
        parityNote="auto_trader 자체 가상자산 프리셋 (선물 롱숏비율 기반, Toss 대상 아님).",
    ),
]


_SCREENING_FILTERS: dict[str, dict[str, object]] = {
    "consecutive_gainers": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "week_change_rate",
        "sort_order": "desc",
        "min_consecutive_up_days": 5,
        "min_week_change_rate": 0.0,
        "limit": CONSECUTIVE_GAINERS_LIMIT,
    },
    "cheap_value": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "market_cap",
        "sort_order": "desc",
        "max_per": 15.0,
        "max_pbr": 1.5,
        "limit": 20,
    },
    "steady_dividend": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "dividend_yield",
        "sort_order": "desc",
        "min_dividend_yield": 2.0,
        "limit": 20,
    },
    "oversold_recovery": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "rsi",
        "sort_order": "asc",
        "max_rsi": 30.0,
        "limit": 20,
    },
    "kr_high_volume_surge": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "volume",
        "sort_order": "desc",
        "limit": 20,
    },
    "double_buy": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "change_rate",
        "sort_order": "desc",
        "min_change_rate": 0.0,
        "include_double_buy": True,
        "limit": 50,
    },
    "growth_expectation": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "change_rate",
        "sort_order": "desc",
        "min_market_cap": 1_000_000_000_000.0,
        "limit": 20,
    },
    # support_proximity is snapshot-only (invest_screener_snapshots supplies
    # price/quality-filter inputs; the nearest-support distance itself is
    # computed live, bounded to the top candidates — see
    # support_proximity_screener.py). min_market_cap/min_turnover here are the
    # loader's internal quality floors, not a live-screening-provider filter.
    "support_proximity": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "dist_to_support_pct",
        "sort_order": "asc",
        "min_market_cap": 300_000_000_000.0,
        "min_turnover": 1_000_000_000.0,
        "limit": 30,
    },
    # high_yield_value is snapshot-only (market_valuation_snapshots); the generic
    # screening provider has no ROE filter, so build_screener_results never falls
    # through to it. These kwargs keep market/limit bounded for callers that
    # inspect the mapping.
    "high_yield_value": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "roe",
        "sort_order": "desc",
        "min_roe": 15.0,
        "min_per": 0.01,
        "max_per": 10.0,
        "limit": 20,
    },
    # undervalued_breakout is snapshot-only; the generic screening provider has no
    # recent-52w-high-date filter.
    "undervalued_breakout": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "per",
        "sort_order": "asc",
        "min_per": 0.01,
        "max_per": 10.0,
        "min_pbr": 0.01,
        "max_pbr": 1.0,
        "limit": 20,
    },
    "investor_flow_momentum": {
        "market": "kr",
        "asset_type": "stock",
        "sort_by": "investor_flow",
        "sort_order": "desc",
        "min_foreign_consecutive_buy_days": 3,
        "include_double_buy": True,
        "limit": 20,
    },
    "crypto_high_volume": {
        "market": "crypto",
        "sort_by": "trade_amount",
        "sort_order": "desc",
        "limit": 20,
    },
    "crypto_oversold": {
        "market": "crypto",
        "sort_by": "rsi",
        "sort_order": "asc",
        "max_rsi": 35.0,
        "limit": 20,
    },
    "crypto_momentum": {
        "market": "crypto",
        "sort_by": "change_rate",
        "sort_order": "desc",
        "limit": 20,
    },
}


def _market_chip(market: str) -> ScreenerFilterChip:
    if market == "us":
        label = "미국"
    elif market == "crypto":
        label = "가상자산"
    else:
        label = "국내"
    return ScreenerFilterChip(label=label, detail=None)


def _with_market(preset: ScreenerPreset, market: str) -> ScreenerPreset:
    chips = list(preset.filterChips)
    if chips:
        chips[0] = _market_chip(market)
    else:
        chips = [_market_chip(market)]
    availability, reason = _preset_availability(preset.id, market)
    return preset.model_copy(
        update={
            "market": market,
            "filterChips": chips,
            "availability": availability,
            "availabilityReason": reason,
        }
    )


def _normalize_requested_market(market: str) -> str:
    return market if market in {"kr", "us", "crypto"} else "kr"


def preset_definitions(market: str = "kr") -> list[ScreenerPreset]:
    """Return preset definitions localized for the requested market."""
    normalized_market = _normalize_requested_market(market)
    if normalized_market == "crypto":
        return [_with_market(p, "crypto") for p in CRYPTO_SCREENER_PRESETS]
    # ROB-427: no longer hide KR-only presets for US — expose the full catalog and
    # let _with_market stamp per-market availability (active/data_pending/unsupported)
    # so the US tab is honest instead of artificially sparse.
    return [_with_market(p, normalized_market) for p in SCREENER_PRESETS]


def get_preset(preset_id: str, market: str = "kr") -> ScreenerPreset | None:
    normalized_market = _normalize_requested_market(market)
    catalog = (
        CRYPTO_SCREENER_PRESETS if normalized_market == "crypto" else SCREENER_PRESETS
    )
    for p in catalog:
        if p.id == preset_id:
            return _with_market(p, normalized_market)
    return None


def screening_filters_for(preset_id: str, market: str = "kr") -> dict[str, object]:
    """Return screening service kwargs for a preset/market, or {} if unknown."""
    filters = dict(_SCREENING_FILTERS.get(preset_id, {}))
    if not filters:
        return {}
    normalized_market = _normalize_requested_market(market)
    crypto_ids = {p.id for p in CRYPTO_SCREENER_PRESETS}
    if normalized_market == "crypto":
        if preset_id not in crypto_ids:
            return {}
        filters["market"] = "crypto"
        return filters
    if preset_id in crypto_ids:
        return {}
    filters["market"] = normalized_market
    if normalized_market == "us":
        # The US screening path supports PER/dividend/RSI/volume/change filters,
        # but not the KR-specific PBR constraint used by this logical preset.
        filters.pop("max_pbr", None)
        if preset_id == "growth_expectation":
            # US engine expects market cap in USD, not KRW.
            filters["min_market_cap"] = 1_000_000_000.0
    return filters
