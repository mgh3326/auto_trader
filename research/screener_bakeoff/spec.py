"""ROB-§140차 screener source bakeoff — PRE-REGISTERED specification.

Scoring constants in this module stay frozen. Live ``tv_rsi45`` comparison was
withdrawn after two adversarial rounds failed on source-definition parity.
The prospective comparator is logged live fanout picks
(``screener-source-weighting-v1``), not reconstructed ``tv_rsi45``.

Read-only research scoring.  This package never writes to any application
table, never touches a broker, and never emits an order/watch/proposal.
The observation log lives outside this package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

EXPERIMENT_ID = "screener-bakeoff-v1-20260822"
#: r2 rework of the same experiment (parity labels / source definitions).
#: Not a new scoring contract — constants above stay frozen.
REWORK_ID = "r2-20260823"
#: Prospective turn: live fanout reconstruction was abandoned after two
#: adversarial rounds failed on the same axis (source definition ≠ production).
#: Scoring constants above stay frozen. Comparator for H1–H4 is now the
#: logged live fanout pick, not reconstructed tv_rsi45.
PROSPECTIVE_ID = "screener-source-weighting-v1"
#: The single source of truth for the withdrawn reconstructed comparators.
WITHDRAWN_SOURCES: frozenset[str] = frozenset({"crypto.tv_rsi45"})


def is_withdrawn_source(source_id: str) -> bool:
    """Return whether a source is barred from historical output paths."""

    return source_id in WITHDRAWN_SOURCES


LOGGING_START_RULE = (
    "first production insert into review.screener_pick_log after PR #1940 "
    "is merged AND SCREENER_PICK_LOG_ENABLED=true. No historical backfill. "
    "Dates before that first row are not in-sample."
)

# --------------------------------------------------------------------------
# §1 Scoring contract
# --------------------------------------------------------------------------
#: Candidates taken from each source's ranked list on each decision date.
TOP_N = 10
#: How deep into a source's ranked list the gate may look before TOP_N is
#: taken.  Gate-filtered variants rank the *gate survivors* of this pool.
GATE_POOL_DEPTH = 100
#: Forward horizons, in market sessions (KR/US) or calendar days (crypto).
HORIZONS: tuple[int, ...] = (5, 20)
#: Entry price = the decision-date CLOSE (the snapshot row's own frozen
#: ``latest_close``).  No intraday/open entry is modelled.
ENTRY_PRICE = "decision_date_close"

# --------------------------------------------------------------------------
# §2 Windows
# --------------------------------------------------------------------------
#: "full" = every decision date on the market's common grid.
#: "recent3w" = the last 15 KR/US sessions (21 calendar days for crypto)
#: of the grid, reported separately so it can be held against the operator
#: retrospective window.
RECENT_WINDOW_SESSIONS = 15
RECENT_WINDOW_CALENDAR_DAYS = 21

#: A market's decision-date grid only admits snapshot dates whose partition is
#: materially complete (guards the 1-row bootstrap partitions in the table).
MIN_PARTITION_ROWS = 1000

# --------------------------------------------------------------------------
# §3 Indicator reconstruction (point-in-time; decision-date bar inclusive)
# --------------------------------------------------------------------------
#: Trailing daily bars used for RSI / fibonacci / bollinger / volume-POC.
#: Matches the analyzer contract ("일봉 200개").
SR_WINDOW_BARS = 200
RSI_PERIOD = 14
BOLLINGER_PERIOD = 20
BOLLINGER_STD = 2.0
FIBONACCI_LEVELS: tuple[float, ...] = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
#: Price-level clustering tolerance, mirrors _cluster_price_levels.
CLUSTER_TOLERANCE_PCT = 0.02
#: >=3 distinct level sources in a cluster => "strong"; ==2 => "moderate".
STRONG_SOURCE_COUNT = 3
MODERATE_SOURCE_COUNT = 2

# --------------------------------------------------------------------------
# §4 The live buy gate, as reconstructed (config/trading_policy.yaml
#    thresholds screen.* and decision_rules buy.support_reserve_net,
#    policy version 2026-08-21.4).
# --------------------------------------------------------------------------
GATE_RSI_MAX = 45.0
GATE_SUPPORT_WITHIN_PCT = 8.0
GATE_SUPPORT_FAMILIES: tuple[str, ...] = ("fib", "bb_lower", "volume_profile")
GATE_B_MIN_INDEPENDENT_FAMILIES = 2
#: honest_upside_pct_min = 40 in policy.  NEUTRALISED here: analyst consensus
#: is not stored per historical date anywhere in this database, so a
#: point-in-time upside is unrecoverable.  Every gate result below is
#: therefore an UPPER BOUND on gate-passing population.
GATE_UPSIDE_MIN_PCT_NEUTRALISED = 40.0

GateVariant = Literal["none", "A_strong", "B_moderate2"]
GATE_VARIANTS: tuple[GateVariant, ...] = ("none", "A_strong", "B_moderate2")
#: Crypto has no equity support-family gate (the reserve-net rule is an equity
#: lane construct) and crypto_candles_1d coverage cannot support a bar-based
#: level reconstruction, so only the RSI leg is applied there.
CRYPTO_GATE_VARIANTS: tuple[str, ...] = ("none", "rsi45_only")

# --------------------------------------------------------------------------
# §5 Liquidity floors for reconstructed sources and for the control /
#    benchmark universes.  Applied on the decision-date snapshot row.
# --------------------------------------------------------------------------
#: Liquidity = daily_volume * latest_close, both frozen snapshot columns.
#: (daily_turnover is NULL before 2026-07-21 — see README §0 amendment A1.)
KR_MIN_TURNOVER_KRW = 1_000_000_000.0
US_MIN_TURNOVER_USD = 5_000_000.0
CRYPTO_MIN_TRADE_AMOUNT_KRW = 1_000_000_000.0

# --------------------------------------------------------------------------
# §6 Controls
# --------------------------------------------------------------------------
RANDOM_SEED = 20260822
#: Random control draws TOP_N symbols per decision date from the same
#: liquidity-filtered universe the benchmark uses.
RANDOM_DRAWS_PER_DATE = TOP_N
#: Benchmark = equal-weight mean forward return of the whole
#: liquidity-filtered universe on that decision date.
BENCHMARK_ID = "eqw_liquid_universe"


@dataclass(frozen=True)
class SourceSpec:
    """One screener source under test."""

    source_id: str
    market: Literal["kr", "us", "crypto"]
    #: Where the ranked list comes from.
    family: Literal["snapshot_preset", "reconstructed", "control", "benchmark"]
    #: Human label for the report.
    label: str
    #: Provenance note printed in the report (honesty column).
    provenance: str
    #: True when every input column was frozen on the decision date by the
    #: production writer (no reconstruction, no look-ahead possible).
    frozen_at_decision_time: bool
    #: Divergences from the production preset, stated up front.
    caveats: tuple[str, ...] = field(default_factory=tuple)
    #: False when this source is a research definition that must not be
    #: read as the live production preset (path-② label demotion).
    live_comparable: bool = True

    def __post_init__(self) -> None:
        # Derive withdrawn-source metadata from the canonical registry rather
        # than maintaining a second list in the individual source rows.
        if self.source_id in WITHDRAWN_SOURCES:
            object.__setattr__(self, "live_comparable", False)


_FROZEN_SNAP = "invest_screener_snapshots row, written by the production snapshot builder on the decision date"
_FROZEN_KRFUND = (
    "invest_kr_fundamentals_snapshots row (tvscreener KR), written on the decision date"
)
_FROZEN_USVAL = "market_valuation_snapshots (market='us', source='tvscreener') row, written on the decision date"
_FROZEN_FLOW = "investor_flow_snapshots row, written on the decision date"
_FROZEN_CRYPTO = "invest_crypto_screener_snapshots row, written on the decision date"
SOURCES: tuple[SourceSpec, ...] = (
    # ---------------- KR: production presets on frozen snapshot columns ----
    SourceSpec(
        "kr.consecutive_gainers",
        "kr",
        "snapshot_preset",
        "연속 상승세",
        _FROZEN_SNAP,
        True,
        (
            "filter consecutive_up_days>=5 and week_change_rate>=0, rank week_change_rate desc",
        ),
    ),
    SourceSpec(
        "kr.high_volume_surge",
        "kr",
        "snapshot_preset",
        "거래량 급증",
        _FROZEN_SNAP,
        True,
        ("rank daily_volume desc",),
    ),
    SourceSpec(
        "kr.top_gainers",
        "kr",
        "snapshot_preset",
        "등락률 상위 (연구정의 — 라이브 fanout change_rate asc 와 다름)",
        _FROZEN_SNAP,
        True,
        (
            "RESEARCH DEFINITION. Ranks snapshot change_rate desc. Live fanout's "
            "change_rate source is ASC (pullback). This is not the live pullback "
            "leg. Do not read these numbers as live fanout change_rate results.",
        ),
        False,
    ),
    SourceSpec(
        "kr.top_losers",
        "kr",
        "snapshot_preset",
        "등락률 하위 (fanout change_rate asc)",
        _FROZEN_SNAP,
        True,
        ("rank change_rate asc — this is the leg the live fanout actually uses",),
    ),
    SourceSpec(
        "kr.trade_amount",
        "kr",
        "snapshot_preset",
        "거래대금 상위 (fanout trade_amount)",
        _FROZEN_SNAP,
        True,
        ("rank daily_turnover desc",),
    ),
    SourceSpec(
        "kr.investor_flow_momentum",
        "kr",
        "snapshot_preset",
        "수급 모멘텀",
        _FROZEN_FLOW,
        True,
        ("filter foreign_consecutive_buy_days>=3, rank foreign_net desc",),
    ),
    SourceSpec(
        "kr.double_buy",
        "kr",
        "snapshot_preset",
        "쌍끌이",
        _FROZEN_FLOW,
        True,
        (
            "ROB-431 day-over-day: foreign_net(today)>foreign_net(prior) AND "
            "institution_net(today)>institution_net(prior); prior missing → fail-closed "
            "(no level-only fallback). Rank change_rate desc, symbol asc. "
            "change_rate comes from the decision-date invest_screener_snapshots row "
            "(inner join; missing price row drops the name). "
            "Remaining vs production: the KR common-stock name heuristic "
            "(_is_kr_toss_common_stock) is NOT applied here. On the r2 picks, "
            "that leftover filter would have dropped about 17.5% of gate=none "
            "D+20 names. Direction survived; magnitude is a lower bound.",
        ),
    ),
    SourceSpec(
        "kr.oversold_recovery",
        "kr",
        "snapshot_preset",
        "과매도 반등 (RSI<=30)",
        _FROZEN_KRFUND,
        True,
        (
            "filter rsi14<=30, rank rsi14 asc. "
            "WARNING: 31.8% of picks have no kr_candles_1d bar (ETN/ELW mix); "
            "D+5 is missing too so the sign of the missing cohort is unknowable. "
            "Truncated-but-scored KR rows are directionally WORSE than the full "
            "KR usable set (artifact S2: −7.45pp) — missing/truncation is not random.",
        ),
    ),
    SourceSpec(
        "kr.cheap_value",
        "kr",
        "snapshot_preset",
        "아직 저렴한 가치주",
        _FROZEN_KRFUND,
        True,
        (
            "PER 0-15, PBR 0-1.5, eps_yoy>=0 (tvscreener 1y proxy for the 3y-avg spec field), rank pbr asc",
        ),
    ),
    SourceSpec(
        "kr.high_yield_value",
        "kr",
        "snapshot_preset",
        "고수익 가치주",
        _FROZEN_KRFUND,
        True,
        ("roe_ttm>=15, PER 0-10, rank roe desc",),
    ),
    SourceSpec(
        "kr.undervalued_breakout",
        "kr",
        "snapshot_preset",
        "저평가 탈출",
        _FROZEN_KRFUND,
        True,
        ("PER 0-10, PBR 0-1, 52w-high set within 20 trading sessions, rank per asc",),
    ),
    SourceSpec(
        "kr.profitable_company",
        "kr",
        "snapshot_preset",
        "돈 잘 버는 회사",
        _FROZEN_KRFUND,
        True,
        ("roe_ttm>=15, gross_margin_ttm>=0.20, rank gross_margin desc",),
    ),
    SourceSpec(
        "kr.undervalued_growth",
        "kr",
        "snapshot_preset",
        "저평가 성장주",
        _FROZEN_KRFUND,
        True,
        (
            "PER<=20, revenue_yoy>=0.10, eps_yoy>=0.20 (1y proxies), rank revenue_yoy desc",
        ),
    ),
    SourceSpec(
        "kr.stable_growth",
        "kr",
        "snapshot_preset",
        "꾸준히 성장",
        _FROZEN_KRFUND,
        True,
        (
            "roe_ttm>=15, eps_yoy>=0.10 (1y proxy); the 3-year earnings-increase streak "
            "condition is NOT reproducible from this table and is SKIPPED (looser than production). "
            "WARNING: 30.0% of picks have no kr_candles_1d bar (three names with no "
            "coverage); D+5 is missing too so the missing cohort's sign is unknowable.",
        ),
    ),
    SourceSpec(
        "kr.growth_expectation_toss",
        "kr",
        "snapshot_preset",
        "성장 기대주",
        _FROZEN_KRFUND,
        True,
        ("eps_yoy>=0.03, eps_qoq>=0.10, rank eps_yoy desc",),
    ),
    SourceSpec(
        "kr.steady_dividend",
        "kr",
        "snapshot_preset",
        "꾸준한 배당주",
        _FROZEN_KRFUND,
        True,
        (
            "dividend_yield>=3%, payout_ratio_ttm>=30, continuous_dividend_payout>=3, rank yield desc; "
            "the DART-first payout fallback is not reproduced (tvscreener column only)",
        ),
    ),
    SourceSpec(
        "kr.future_dividend_king",
        "kr",
        "snapshot_preset",
        "미래 배당킹",
        _FROZEN_KRFUND,
        True,
        (
            "dividend_yield>=1%, continuous_dividend_growth>=3, payout_ratio_ttm>=30, rank yield desc",
        ),
    ),
    SourceSpec(
        "kr.random",
        "kr",
        "control",
        "무작위 대조군",
        "seed-fixed draw",
        False,
        ("uniform draw of TOP_N from the liquidity-filtered universe, seed 20260822",),
    ),
    SourceSpec(
        "kr.benchmark",
        "kr",
        "benchmark",
        "시장 등가중 (유동성 유니버스)",
        "n/a",
        False,
        (),
    ),
    # ---------------- US -------------------------------------------------
    SourceSpec(
        "us.consecutive_gainers",
        "us",
        "snapshot_preset",
        "연속 상승세",
        _FROZEN_SNAP,
        True,
        ("consecutive_up_days>=5, week_change_rate>=0, rank week_change_rate desc",),
    ),
    SourceSpec(
        "us.high_volume_surge",
        "us",
        "snapshot_preset",
        "거래량 급증",
        _FROZEN_SNAP,
        True,
        ("rank daily_volume desc",),
    ),
    SourceSpec(
        "us.top_gainers",
        "us",
        "snapshot_preset",
        "등락률 상위 (연구정의 — 라이브 fanout change_rate asc 와 다름)",
        _FROZEN_SNAP,
        True,
        (
            "RESEARCH DEFINITION. Ranks snapshot change_rate desc. Live fanout "
            "change_rate is ASC (pullback). Not the live pullback leg.",
        ),
        False,
    ),
    SourceSpec(
        "us.top_losers",
        "us",
        "snapshot_preset",
        "등락률 하위 (fanout change_rate asc)",
        _FROZEN_SNAP,
        True,
        ("rank change_rate asc",),
    ),
    SourceSpec(
        "us.trade_amount",
        "us",
        "snapshot_preset",
        "거래대금 상위",
        _FROZEN_SNAP,
        True,
        ("rank daily_turnover desc",),
    ),
    SourceSpec(
        "us.cheap_value",
        "us",
        "snapshot_preset",
        "아직 저렴한 가치주",
        _FROZEN_USVAL,
        True,
        (
            "PER 0-15, PBR 0-1.5, rank pbr asc; the earnings-growth leg is "
            "NOT in market_valuation_snapshots and is SKIPPED (looser)",
        ),
    ),
    SourceSpec(
        "us.high_yield_value",
        "us",
        "snapshot_preset",
        "고수익 가치주 (연구정의 tvscreener — 라이브 parity 미검증)",
        _FROZEN_USVAL,
        True,
        (
            "RESEARCH DEFINITION (tvscreener) — live parity NOT verified. "
            "Ranks market_valuation_snapshots roe>=15, 0<PER<=10, no quality "
            "guard. Production load_high_yield_value_from_snapshots does NOT "
            "filter source='yahoo' (ROE/PER are vendor-agnostic). The r2 "
            "yahoo-partition demotion is deleted; live loader parity was not "
            "re-run. Do not read these numbers as live-preset results.",
        ),
        False,
    ),
    SourceSpec(
        "us.undervalued_breakout",
        "us",
        "snapshot_preset",
        "저평가 탈출",
        _FROZEN_USVAL,
        True,
        ("PER 0-10, PBR 0-1, 52w-high within 20 sessions, rank per asc",),
    ),
    SourceSpec(
        "us.steady_dividend",
        "us",
        "snapshot_preset",
        "꾸준한 배당주",
        _FROZEN_USVAL,
        True,
        (
            "dividend_yield>=3%, rank yield desc; payout/streak legs "
            "not in this table and SKIPPED (looser)",
        ),
    ),
    SourceSpec(
        "us.random", "us", "control", "무작위 대조군", "seed-fixed draw", False, ()
    ),
    SourceSpec(
        "us.benchmark",
        "us",
        "benchmark",
        "시장 등가중 (유동성 유니버스)",
        "n/a",
        False,
        (),
    ),
    # ---------------- Crypto ---------------------------------------------
    SourceSpec(
        "crypto.high_volume",
        "crypto",
        "snapshot_preset",
        "거래대금 상위",
        _FROZEN_CRYPTO,
        True,
        ("rank trade_amount_24h desc",),
    ),
    SourceSpec(
        "crypto.oversold",
        "crypto",
        "snapshot_preset",
        "과매도 (RSI<=35)",
        _FROZEN_CRYPTO,
        True,
        ("rsi<=35, rank rsi asc",),
    ),
    SourceSpec(
        "crypto.momentum",
        "crypto",
        "snapshot_preset",
        "등락률 상위",
        _FROZEN_CRYPTO,
        True,
        ("rank change_rate desc",),
    ),
    SourceSpec(
        "crypto.funding_squeeze",
        "crypto",
        "snapshot_preset",
        "펀딩 숏과열",
        _FROZEN_CRYPTO,
        True,
        ("funding_rate<0, rank funding_rate asc",),
    ),
    SourceSpec(
        "crypto.funding_overheated",
        "crypto",
        "snapshot_preset",
        "펀딩 롱과열",
        _FROZEN_CRYPTO,
        True,
        ("funding_rate>0, rank funding_rate desc",),
    ),
    SourceSpec(
        "crypto.oi_surge",
        "crypto",
        "snapshot_preset",
        "미결제약정 급증",
        _FROZEN_CRYPTO,
        True,
        ("rank oi_change_24h desc",),
    ),
    SourceSpec(
        "crypto.long_short_skew",
        "crypto",
        "snapshot_preset",
        "롱숏 쏠림",
        _FROZEN_CRYPTO,
        True,
        ("rank abs(long_short_account_ratio - 1) desc",),
    ),
    SourceSpec(
        "crypto.tv_rsi45",
        "crypto",
        "reconstructed",
        "연구정의 (재구성 RSI 순위) — 라이브 비교 철회",
        _FROZEN_CRYPTO,
        True,
        (
            "LIVE COMPARISON WITHDRAWN. This historical reconstruction has the "
            "top-100 vs live top-10 gap. Snapshot rsi column, not live TradingView.",
        ),
    ),
    SourceSpec(
        "crypto.random",
        "crypto",
        "control",
        "무작위 대조군",
        "seed-fixed draw",
        False,
        (),
    ),
    SourceSpec(
        "crypto.benchmark",
        "crypto",
        "benchmark",
        "시장 등가중 (유동성 유니버스)",
        "n/a",
        False,
        (),
    ),
)

SOURCES_BY_ID = {s.source_id: s for s in SOURCES}

__all__ = [name for name in dir() if not name.startswith("_")]
