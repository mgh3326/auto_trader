"""ROB-974 round-2 H3 immutable S3/S4 preregistration authority.

This module owns one canonical 48-row roster and two distinct structured
strategy-contract seals.  It is pure registration code: no data access,
runtime state, parameter search, execution, clock, or environment authority.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from research_contracts.canonical_hash import canonical_sha256

RESEARCH_DOCUMENT_SHA256 = (
    "2f535196cf0f0a03292e8f4c1806794ffbf8282ba7b5c3f564a930763577a009"
)
SYMBOLS: tuple[str, ...] = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
PAIRS: tuple[str, ...] = ("XRP-DOGE", "XRP-SOL", "DOGE-SOL")
DESIGN_TYPES = ("baseline", "OFAT", "interaction")
S3_NO_SIGNAL_TAXONOMY: tuple[str, ...] = (
    "missing_required_context",
    "market_regime",
    "market_breadth",
    "trend_strength",
    "efficiency",
    "pullback_depth",
    "vwap_reclaim",
    "momentum",
    "prior_l_non_breakout",
    "volatility_percentile",
    "range_tp_capacity",
)
S3_GENERATOR_REJECTION_TAXONOMY: tuple[str, ...] = (
    "simultaneous_candidate_arbitration_loser",
)
S4_NO_SIGNAL_TAXONOMY: tuple[str, ...] = (
    "missing_required_context",
    "degenerate_beta_market_variance",
    "degenerate_rho_variance",
    "degenerate_phi_denominator",
    "phi_not_in_open_unit_interval",
    "nonfinite_required_input",
    "convergence_sign",
    "prior_z_entry",
    "current_z_entry",
    "convergence_fraction",
    "rho",
    "half_life",
    "beta_stability",
    "absolute_distance",
    "distance_to_tp",
    "historical_notional_feasibility",
)
S4_GENERATOR_REJECTION_TAXONOMY: tuple[str, ...] = (
    "simultaneous_pair_arbitration_loser",
)

S3_HYPOTHESIS_UTF8 = (
    "사전등록 가설: 암호자산의 정보·포지션 조정이 여러 시간에 걸쳐 반영되는 구간에서는 24~80h 방향성이 형성될 수 있다. "
    "그러나 이미 고가를 돌파한 시점에 추격하면 역선택과 false breakout 비용을 부담한다. 따라서 ①공통시장과 개별 심볼의 장주기 방향성이 일치하고 "
    "②가격이 4~8h 동안 12h VWAP 아래/위로 제한된 눌림을 보인 뒤 ③기존 고가·저가를 돌파하기 전에 VWAP을 재회복하며 ④최근 정상적인 일간 range가 TP를 감당할 때만 기존 방향으로 진입하면 "
    "단기 돌파보다 높은 gross 이동폭과 낮은 timeout을 얻을 수 있다는 가설이다.\n"
).encode()
S4_HYPOTHESIS_UTF8 = (
    "사전등록 가설: XRP/DOGE/SOL에는 공통 암호자산 요인과 심볼별 일시적 자금흐름이 동시에 존재할 수 있다. 공통 요인을 베타중립 long/short로 제거한 뒤에도 상대가격 spread가 정상 범위에서 반복적으로 이탈·복귀하고, "
    "그 반감기가 8~48h 범위에서 안정적이라면 부분 수렴 108bp 이상을 거래비용 후 포착할 수 있다는 가설이다.\n"
).encode()

S3_HYPOTHESIS_SHA256 = (
    "f8893d558067e07f6248beb66fbc3f917af558634226484c6506c489e2752e01"
)
S4_HYPOTHESIS_SHA256 = (
    "c5152f9d207682a569c4df197e4e276cdf907d8eb6a2f68d3d9725b9bf229d28"
)


def _exact_str(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be built-in str")
    return value


def _exact_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


def _exact_float(value: object, name: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{name} must be built-in float")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _exact_bytes(value: object, name: str) -> bytes:
    if type(value) is not bytes:
        raise TypeError(f"{name} must be built-in bytes")
    return value


def _sha256(value: object, name: str) -> str:
    text = _exact_str(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return text


def _validate_common_config_fields(
    config_id: object,
    k_sl: object,
    r_tp: object,
    design_type: object,
    authority_label: object,
    hypothesis_utf8: object,
) -> None:
    _exact_str(config_id, "config_id")
    _exact_float(k_sl, "k_SL")
    _exact_float(r_tp, "R_TP")
    design = _exact_str(design_type, "design_type")
    if design not in DESIGN_TYPES:
        raise ValueError("design_type is outside the closed set")
    if not _exact_str(authority_label, "authority_label"):
        raise ValueError("authority_label must not be empty")
    _exact_bytes(hypothesis_utf8, "hypothesis_utf8")


@dataclass(frozen=True, slots=True)
class S3Config:
    config_id: str
    L: int
    q_min: float
    ER_min: float
    k_SL: float
    R_TP: float
    design_type: str
    authority_label: str
    hypothesis_utf8: bytes

    def __post_init__(self) -> None:
        _validate_common_config_fields(
            self.config_id,
            self.k_SL,
            self.R_TP,
            self.design_type,
            self.authority_label,
            self.hypothesis_utf8,
        )
        _exact_int(self.L, "L")
        _exact_float(self.q_min, "q_min")
        _exact_float(self.ER_min, "ER_min")


@dataclass(frozen=True, slots=True)
class S4Config:
    config_id: str
    W: int
    z_entry: float
    d_min_bp: int
    k_SL: float
    R_TP: float
    design_type: str
    authority_label: str
    hypothesis_utf8: bytes

    def __post_init__(self) -> None:
        _validate_common_config_fields(
            self.config_id,
            self.k_SL,
            self.R_TP,
            self.design_type,
            self.authority_label,
            self.hypothesis_utf8,
        )
        _exact_int(self.W, "W")
        _exact_float(self.z_entry, "z_entry")
        _exact_int(self.d_min_bp, "d_min_bp")


S3_DOMAINS = {
    "L": (8, 10, 12, 16, 20),
    "q_min": (0.20, 0.30, 0.35, 0.50, 0.65),
    "ER_min": (0.25, 0.30, 0.35, 0.40, 0.45),
    "k_SL": (1.00, 1.10, 1.25, 1.40, 1.60),
    "R_TP": (1.35, 1.45, 1.60, 1.80, 2.00),
}
S4_DOMAINS = {
    "W": (120, 150, 180, 240, 300),
    "z_entry": (1.40, 1.60, 1.80, 2.00, 2.20),
    "d_min_bp": (140, 160, 180, 220, 260),
    "k_SL": (1.00, 1.10, 1.25, 1.40, 1.60),
    "R_TP": (1.35, 1.50, 1.65, 1.80, 2.00),
}


# The sole production roster source. Strategy-specific views below are slices,
# never independently maintained copies.
FROZEN_H3_ROSTER: tuple[S3Config | S4Config, ...] = (
    S3Config(
        "S3-00", 12, 0.35, 0.35, 1.25, 1.60, "baseline", "baseline", S3_HYPOTHESIS_UTF8
    ),
    S3Config(
        "S3-01",
        8,
        0.35,
        0.35,
        1.25,
        1.60,
        "OFAT",
        "OFAT: 짧은 trend",
        S3_HYPOTHESIS_UTF8,
    ),
    S3Config("S3-02", 10, 0.35, 0.35, 1.25, 1.60, "OFAT", "OFAT", S3_HYPOTHESIS_UTF8),
    S3Config("S3-03", 16, 0.35, 0.35, 1.25, 1.60, "OFAT", "OFAT", S3_HYPOTHESIS_UTF8),
    S3Config(
        "S3-04",
        20,
        0.35,
        0.35,
        1.25,
        1.60,
        "OFAT",
        "OFAT: 긴 trend",
        S3_HYPOTHESIS_UTF8,
    ),
    S3Config(
        "S3-05",
        12,
        0.20,
        0.35,
        1.25,
        1.60,
        "OFAT",
        "OFAT: 얕은 pullback",
        S3_HYPOTHESIS_UTF8,
    ),
    S3Config("S3-06", 12, 0.30, 0.35, 1.25, 1.60, "OFAT", "OFAT", S3_HYPOTHESIS_UTF8),
    S3Config("S3-07", 12, 0.50, 0.35, 1.25, 1.60, "OFAT", "OFAT", S3_HYPOTHESIS_UTF8),
    S3Config(
        "S3-08",
        12,
        0.65,
        0.35,
        1.25,
        1.60,
        "OFAT",
        "OFAT: 깊은 pullback",
        S3_HYPOTHESIS_UTF8,
    ),
    S3Config(
        "S3-09",
        12,
        0.35,
        0.25,
        1.25,
        1.60,
        "OFAT",
        "OFAT: 낮은 효율",
        S3_HYPOTHESIS_UTF8,
    ),
    S3Config("S3-10", 12, 0.35, 0.30, 1.25, 1.60, "OFAT", "OFAT", S3_HYPOTHESIS_UTF8),
    S3Config("S3-11", 12, 0.35, 0.40, 1.25, 1.60, "OFAT", "OFAT", S3_HYPOTHESIS_UTF8),
    S3Config(
        "S3-12",
        12,
        0.35,
        0.45,
        1.25,
        1.60,
        "OFAT",
        "OFAT: 높은 효율",
        S3_HYPOTHESIS_UTF8,
    ),
    S3Config(
        "S3-13", 12, 0.35, 0.35, 1.00, 1.60, "OFAT", "OFAT: 좁은 SL", S3_HYPOTHESIS_UTF8
    ),
    S3Config("S3-14", 12, 0.35, 0.35, 1.10, 1.60, "OFAT", "OFAT", S3_HYPOTHESIS_UTF8),
    S3Config("S3-15", 12, 0.35, 0.35, 1.40, 1.60, "OFAT", "OFAT", S3_HYPOTHESIS_UTF8),
    S3Config(
        "S3-16", 12, 0.35, 0.35, 1.60, 1.60, "OFAT", "OFAT: 넓은 SL", S3_HYPOTHESIS_UTF8
    ),
    S3Config(
        "S3-17", 12, 0.35, 0.35, 1.25, 1.35, "OFAT", "OFAT: 낮은 RR", S3_HYPOTHESIS_UTF8
    ),
    S3Config("S3-18", 12, 0.35, 0.35, 1.25, 1.45, "OFAT", "OFAT", S3_HYPOTHESIS_UTF8),
    S3Config("S3-19", 12, 0.35, 0.35, 1.25, 1.80, "OFAT", "OFAT", S3_HYPOTHESIS_UTF8),
    S3Config(
        "S3-20", 12, 0.35, 0.35, 1.25, 2.00, "OFAT", "OFAT: 높은 RR", S3_HYPOTHESIS_UTF8
    ),
    S3Config(
        "S3-21",
        10,
        0.30,
        0.30,
        1.25,
        1.60,
        "interaction",
        "interaction: 빠른 trend + 얕은 pullback",
        S3_HYPOTHESIS_UTF8,
    ),
    S3Config(
        "S3-22",
        16,
        0.50,
        0.40,
        1.25,
        1.60,
        "interaction",
        "interaction: 느린 trend + 깊은 pullback",
        S3_HYPOTHESIS_UTF8,
    ),
    S3Config(
        "S3-23",
        12,
        0.50,
        0.35,
        1.40,
        1.80,
        "interaction",
        "interaction: 깊은 pullback + 넓은 risk/return",
        S3_HYPOTHESIS_UTF8,
    ),
    S4Config(
        "S4-00", 180, 1.80, 180, 1.25, 1.50, "baseline", "baseline", S4_HYPOTHESIS_UTF8
    ),
    S4Config(
        "S4-01",
        120,
        1.80,
        180,
        1.25,
        1.50,
        "OFAT",
        "OFAT: 짧은 beta window",
        S4_HYPOTHESIS_UTF8,
    ),
    S4Config("S4-02", 150, 1.80, 180, 1.25, 1.50, "OFAT", "OFAT", S4_HYPOTHESIS_UTF8),
    S4Config("S4-03", 240, 1.80, 180, 1.25, 1.50, "OFAT", "OFAT", S4_HYPOTHESIS_UTF8),
    S4Config(
        "S4-04",
        300,
        1.80,
        180,
        1.25,
        1.50,
        "OFAT",
        "OFAT: 긴 beta window",
        S4_HYPOTHESIS_UTF8,
    ),
    S4Config(
        "S4-05", 180, 1.40, 180, 1.25, 1.50, "OFAT", "OFAT: 낮은 z", S4_HYPOTHESIS_UTF8
    ),
    S4Config("S4-06", 180, 1.60, 180, 1.25, 1.50, "OFAT", "OFAT", S4_HYPOTHESIS_UTF8),
    S4Config("S4-07", 180, 2.00, 180, 1.25, 1.50, "OFAT", "OFAT", S4_HYPOTHESIS_UTF8),
    S4Config(
        "S4-08", 180, 2.20, 180, 1.25, 1.50, "OFAT", "OFAT: 높은 z", S4_HYPOTHESIS_UTF8
    ),
    S4Config(
        "S4-09",
        180,
        1.80,
        140,
        1.25,
        1.50,
        "OFAT",
        "OFAT: 작은 절대거리",
        S4_HYPOTHESIS_UTF8,
    ),
    S4Config("S4-10", 180, 1.80, 160, 1.25, 1.50, "OFAT", "OFAT", S4_HYPOTHESIS_UTF8),
    S4Config("S4-11", 180, 1.80, 220, 1.25, 1.50, "OFAT", "OFAT", S4_HYPOTHESIS_UTF8),
    S4Config(
        "S4-12",
        180,
        1.80,
        260,
        1.25,
        1.50,
        "OFAT",
        "OFAT: 큰 절대거리",
        S4_HYPOTHESIS_UTF8,
    ),
    S4Config(
        "S4-13", 180, 1.80, 180, 1.00, 1.50, "OFAT", "OFAT: 좁은 SL", S4_HYPOTHESIS_UTF8
    ),
    S4Config("S4-14", 180, 1.80, 180, 1.10, 1.50, "OFAT", "OFAT", S4_HYPOTHESIS_UTF8),
    S4Config("S4-15", 180, 1.80, 180, 1.40, 1.50, "OFAT", "OFAT", S4_HYPOTHESIS_UTF8),
    S4Config(
        "S4-16", 180, 1.80, 180, 1.60, 1.50, "OFAT", "OFAT: 넓은 SL", S4_HYPOTHESIS_UTF8
    ),
    S4Config(
        "S4-17", 180, 1.80, 180, 1.25, 1.35, "OFAT", "OFAT: 낮은 RR", S4_HYPOTHESIS_UTF8
    ),
    S4Config("S4-18", 180, 1.80, 180, 1.25, 1.65, "OFAT", "OFAT", S4_HYPOTHESIS_UTF8),
    S4Config("S4-19", 180, 1.80, 180, 1.25, 1.80, "OFAT", "OFAT", S4_HYPOTHESIS_UTF8),
    S4Config(
        "S4-20", 180, 1.80, 180, 1.25, 2.00, "OFAT", "OFAT: 높은 RR", S4_HYPOTHESIS_UTF8
    ),
    S4Config(
        "S4-21",
        150,
        1.60,
        160,
        1.25,
        1.50,
        "interaction",
        "interaction: 빠른 적응 + 낮은 entry",
        S4_HYPOTHESIS_UTF8,
    ),
    S4Config(
        "S4-22",
        240,
        2.00,
        220,
        1.25,
        1.50,
        "interaction",
        "interaction: 안정성 우선 strict",
        S4_HYPOTHESIS_UTF8,
    ),
    S4Config(
        "S4-23",
        180,
        2.00,
        220,
        1.40,
        1.80,
        "interaction",
        "interaction: 큰 이탈 + tail 완충",
        S4_HYPOTHESIS_UTF8,
    ),
)
FROZEN_S3_CONFIGS: tuple[S3Config, ...] = FROZEN_H3_ROSTER[:24]  # type: ignore[assignment]
FROZEN_S4_CONFIGS: tuple[S4Config, ...] = FROZEN_H3_ROSTER[24:]  # type: ignore[assignment]


def _validate_hypothesis(raw: bytes, size: int, digest: str) -> None:
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("registered hypothesis bytes/hash mismatch")
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n") or b"\r" in raw:
        raise ValueError("registered hypothesis must carry exactly one terminal LF")
    if any(line.endswith(b" ") for line in raw.splitlines()):
        raise ValueError("registered hypothesis has trailing whitespace")


def validate_manifest(rows: tuple[S3Config | S4Config, ...]) -> None:
    if type(rows) is not tuple:
        raise TypeError("manifest must be built-in tuple")
    if len(rows) != 48:
        raise ValueError("H3 manifest must contain exactly 48 rows")
    expected_ids = tuple(
        [f"S3-{index:02d}" for index in range(24)]
        + [f"S4-{index:02d}" for index in range(24)]
    )
    if tuple(row.config_id for row in rows) != expected_ids:
        raise ValueError("missing, duplicate, reordered, or cross-strategy config id")
    if any(type(row) is not S3Config for row in rows[:24]) or any(
        type(row) is not S4Config for row in rows[24:]
    ):
        raise TypeError("strategy rows must use their exact built-in DTO class")
    for row in rows[:24]:
        for name, domain in S3_DOMAINS.items():
            if getattr(row, name) not in domain:
                raise ValueError(f"{row.config_id} {name} outside registered domain")
        if row.hypothesis_utf8 != S3_HYPOTHESIS_UTF8:
            raise ValueError("S3 hypothesis mutation")
    for row in rows[24:]:
        for name, domain in S4_DOMAINS.items():
            if getattr(row, name) not in domain:
                raise ValueError(f"{row.config_id} {name} outside registered domain")
        if row.hypothesis_utf8 != S4_HYPOTHESIS_UTF8:
            raise ValueError("S4 hypothesis mutation")
    if rows != FROZEN_H3_ROSTER:
        raise ValueError("manifest row does not exactly match preregistration")


_validate_hypothesis(S3_HYPOTHESIS_UTF8, 699, S3_HYPOTHESIS_SHA256)
_validate_hypothesis(S4_HYPOTHESIS_UTF8, 423, S4_HYPOTHESIS_SHA256)
validate_manifest(FROZEN_H3_ROSTER)
_BY_ID = {row.config_id: row for row in FROZEN_H3_ROSTER}


def get_config(config_id: str) -> S3Config | S4Config:
    _exact_str(config_id, "config_id")
    return _BY_ID[config_id]


def assert_registered_config(config: S3Config | S4Config) -> None:
    if type(config) not in (S3Config, S4Config):
        raise TypeError("config must be an exact H3 config DTO")
    canonical = _BY_ID.get(config.config_id)
    if canonical is None or config != canonical:
        raise ValueError("config does not exactly match its registered row")


_S3_MECHANISM = (
    "Donchian high/low is not calculated.",
    "A bar that breaks the prior high/low is excluded from entry.",
    "No single-5m shock z-score or shock-origin reversion is used.",
    "The three-symbol 24h common regime and breadth determine direction first.",
    "Expected holding time is 8-48h.",
)
_S4_MECHANISM = (
    "No individual-symbol 5m shock magnitude is used.",
    "There is no immediate contrarian entry after a shock.",
    "Long/short legs neutralize common-market beta instead of predicting direction.",
    "Being outside the spread threshold alone never permits entry.",
    "Historical half-life, correlation, beta stability, and convergence onset are required.",
    "TP and SL apply to gross basket return, never individual legs.",
)


def _config_payload(row: S3Config | S4Config) -> dict[str, object]:
    common = {
        "config_id": row.config_id,
        "k_SL": row.k_SL,
        "R_TP": row.R_TP,
        "design_type": row.design_type,
        "authority_label": row.authority_label,
        "hypothesis_utf8": row.hypothesis_utf8.decode("utf-8"),
    }
    if type(row) is S3Config:
        return {"L": row.L, "q_min": row.q_min, "ER_min": row.ER_min, **common}
    return {
        "W": row.W,
        "z_entry": row.z_entry,
        "d_min_bp": row.d_min_bp,
        **common,
    }


def strategy_contract_payload(strategy: str) -> dict[str, object]:
    """Return a fresh plain payload for the requested frozen strategy seal."""
    _exact_str(strategy, "strategy")
    if strategy == "S3":
        return {
            "contract_key": "rob974.s3.rpt-4h",
            "contract_version": "1",
            "source_research_sha256": RESEARCH_DOCUMENT_SHA256,
            "strategy": "S3",
            "name": "RPT-4H Regime-Aligned Pullback Trend",
            "posture": "historical_champion",
            "symbols": list(SYMBOLS),
            "formulas": {
                "R": "ln(C_t/C_(t-L)); L deltas and L+1 closes",
                "ER": "abs(C_t-C_(t-L))/fsum(abs(C_j-C_(j-1)),j=t-L+1..t)",
                "S": "R/max(A_t*sqrt(L),1e-6)",
                "Qplus": "-min((C_j-VWAP12_j)/ATR20_j,j in {t-2,t-1})",
                "Qminus": "max((C_j-VWAP12_j)/ATR20_j,j in {t-2,t-1})",
                "d_SL": "clip(k_SL*A_t,0.008,0.020)",
                "d_TP": "max(0.0068,R_TP*d_SL)",
            },
            "fixed_constants": {
                "bar_hours": 4,
                "strength_threshold": 1.25,
                "regime_abs_fraction": 0.0075,
                "breadth_min": 2,
                "q_max": 1.25,
                "volatility_percentile_min": 20.0,
                "volatility_percentile_max": 90.0,
                "range24_tp_fraction": 0.60,
                "timeout_4h_bars": 12,
                "all_config_tp_floor_fraction": 0.0108,
                "baseline_tp_floor_fraction": 0.0128,
            },
            "parameter_domains": {
                key: list(value) for key, value in S3_DOMAINS.items()
            },
            "diagnostic_bins": {
                "abs_S": ["[1.25,1.75)", "[1.75,2.50)", "[2.50,inf)"],
                "pullback_Q": ["[q_min,0.50)", "[0.50,0.85)", "[0.85,1.25]"],
                "abs_M": ["[0.0075,0.015)", "[0.015,0.03)", "[0.03,inf)"],
                "volatility_percentile": ["[20,40)", "[40,60)", "[60,75)", "[75,90]"],
                "direction": ["Long", "Short"],
                "symbol": ["XRP", "DOGE", "SOL"],
                "exit": ["TP", "SL", "THESIS_EXIT", "TIMEOUT"],
            },
            "mechanism_statements": list(_S3_MECHANISM),
            "no_signal_reasons": list(S3_NO_SIGNAL_TAXONOMY),
            "generator_rejection_reasons": list(S3_GENERATOR_REJECTION_TAXONOMY),
            "hypothesis_utf8": S3_HYPOTHESIS_UTF8.decode("utf-8"),
            "hypothesis_sha256": S3_HYPOTHESIS_SHA256,
            "authority_labels": [row.authority_label for row in FROZEN_S3_CONFIGS],
            "configs": [_config_payload(row) for row in FROZEN_S3_CONFIGS],
        }
    if strategy == "S4":
        return {
            "contract_key": "rob974.s4.brc-4h",
            "contract_version": "1",
            "source_research_sha256": RESEARCH_DOCUMENT_SHA256,
            "strategy": "S4",
            "name": "BRC-4H Beta-Neutral Residual Convergence",
            "posture": "historical_research_only",
            "symbols": list(SYMBOLS),
            "pairs": list(PAIRS),
            "formulas": {
                "beta": "clip(Cov_W(r_i,m)/Var_W(m),0.25,3.00)",
                "weights": "w_a=beta_b/(beta_a+beta_b);w_b=beta_a/(beta_a+beta_b)",
                "spread": "s_j=w_a*ln(C_a,j)-w_b*ln(C_b,j); current weights over W including t",
                "z": "(s_t-median_W(s))/max(1.4826*MAD_W(s),1e-6)",
                "D": "abs(s_t-median_W(s))",
                "phi": "fsum(x_(j-1)*x_j)/fsum(x_(j-1)^2), x=s-median(s)",
                "half_life": "ln(0.5)/ln(phi)",
                "sigma_pair": "population_stddev_W(w_a*r_a-w_b*r_b)",
                "d_SL": "clip(k_SL*sigma_pair,0.008,0.016)",
                "d_TP": "max(0.0068,R_TP*d_SL)",
                "gross_notional": "G_min=max(6/w_a,6/w_b);G_max=min(10/w_a,10/w_b);G=G_min",
            },
            "fixed_constants": {
                "bar_hours": 4,
                "rho_min": 0.60,
                "beta_clip_min": 0.25,
                "beta_clip_max": 3.0,
                "half_life_min_4h_bars": 2.0,
                "half_life_max_4h_bars": 12.0,
                "beta_stability_max": 0.20,
                "convergence_fraction": 0.10,
                "current_abs_z_max_prior_fraction": 0.90,
                "distance_tp_multiple": 1.25,
                "mean_exit_abs_z": 0.25,
                "stall_after_4h_bars": 2,
                "stall_min_convergence_fraction": 0.15,
                "timeout_4h_bars": 9,
                "all_config_tp_floor_fraction": 0.0108,
                "baseline_tp_floor_fraction": 0.0120,
            },
            "parameter_domains": {
                key: list(value) for key, value in S4_DOMAINS.items()
            },
            "diagnostic_bins": {
                "abs_z": ["[z_entry,2.2)", "[2.2,2.8)", "[2.8,inf)"],
                "D_bps": ["[140,200)", "[200,300)", "[300,inf)"],
                "rho": ["[0.60,0.70)", "[0.70,0.80)", "[0.80,1.00]"],
                "half_life_hours": ["[8,16)", "[16,32)", "[32,48]"],
                "M_24h": [
                    "[-inf,-0.03)",
                    "[-0.03,-0.01)",
                    "[-0.01,0.01]",
                    "(0.01,0.03]",
                    "(0.03,inf)",
                ],
                "pair": list(PAIRS),
                "exit": [
                    "TP",
                    "SL",
                    "MEAN_EXIT",
                    "STALL_EXIT",
                    "TIMEOUT",
                    "PAIR_EXEC_FAIL",
                ],
            },
            "mechanism_statements": list(_S4_MECHANISM),
            "no_signal_reasons": list(S4_NO_SIGNAL_TAXONOMY),
            "generator_rejection_reasons": list(S4_GENERATOR_REJECTION_TAXONOMY),
            "hypothesis_utf8": S4_HYPOTHESIS_UTF8.decode("utf-8"),
            "hypothesis_sha256": S4_HYPOTHESIS_SHA256,
            "authority_labels": [row.authority_label for row in FROZEN_S4_CONFIGS],
            "configs": [_config_payload(row) for row in FROZEN_S4_CONFIGS],
        }
    raise ValueError("strategy must be S3 or S4")


def hash_contract_payload(payload: dict[str, object]) -> str:
    if type(payload) is not dict:
        raise TypeError("contract payload must be built-in dict")
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class StrategyContract:
    key: str
    version: str
    source_research_sha256: str
    contract_hash: str

    def __post_init__(self) -> None:
        if not _exact_str(self.key, "key") or not _exact_str(self.version, "version"):
            raise ValueError("contract key/version must not be empty")
        _sha256(self.source_research_sha256, "source_research_sha256")
        _sha256(self.contract_hash, "contract_hash")


S3_STRATEGY_CONTRACT = StrategyContract(
    "rob974.s3.rpt-4h",
    "1",
    RESEARCH_DOCUMENT_SHA256,
    hash_contract_payload(strategy_contract_payload("S3")),
)
S4_STRATEGY_CONTRACT = StrategyContract(
    "rob974.s4.brc-4h",
    "1",
    RESEARCH_DOCUMENT_SHA256,
    hash_contract_payload(strategy_contract_payload("S4")),
)


def validate_contract_seals(s3: StrategyContract, s4: StrategyContract) -> None:
    if type(s3) is not StrategyContract or type(s4) is not StrategyContract:
        raise TypeError("contract seals must be exact StrategyContract values")
    if (
        s3.source_research_sha256 != RESEARCH_DOCUMENT_SHA256
        or s4.source_research_sha256 != RESEARCH_DOCUMENT_SHA256
    ):
        raise ValueError("strategy contracts must cite the one research document SHA")
    if (
        s3.contract_hash in (RESEARCH_DOCUMENT_SHA256, s4.contract_hash)
        or s4.contract_hash == RESEARCH_DOCUMENT_SHA256
    ):
        raise ValueError("source/strategy contract hash collision")
    expected = (
        (
            "rob974.s3.rpt-4h",
            "1",
            hash_contract_payload(strategy_contract_payload("S3")),
        ),
        (
            "rob974.s4.brc-4h",
            "1",
            hash_contract_payload(strategy_contract_payload("S4")),
        ),
    )
    if (s3.key, s3.version, s3.contract_hash) != expected[0] or (
        s4.key,
        s4.version,
        s4.contract_hash,
    ) != expected[1]:
        raise ValueError("strategy contract seal drift")


validate_contract_seals(S3_STRATEGY_CONTRACT, S4_STRATEGY_CONTRACT)

__all__ = [
    "FROZEN_H3_ROSTER",
    "FROZEN_S3_CONFIGS",
    "FROZEN_S4_CONFIGS",
    "PAIRS",
    "RESEARCH_DOCUMENT_SHA256",
    "S3Config",
    "S3_GENERATOR_REJECTION_TAXONOMY",
    "S3_HYPOTHESIS_SHA256",
    "S3_HYPOTHESIS_UTF8",
    "S3_NO_SIGNAL_TAXONOMY",
    "S3_STRATEGY_CONTRACT",
    "S4Config",
    "S4_GENERATOR_REJECTION_TAXONOMY",
    "S4_HYPOTHESIS_SHA256",
    "S4_HYPOTHESIS_UTF8",
    "S4_NO_SIGNAL_TAXONOMY",
    "S4_STRATEGY_CONTRACT",
    "SYMBOLS",
    "StrategyContract",
    "assert_registered_config",
    "get_config",
    "hash_contract_payload",
    "strategy_contract_payload",
    "validate_contract_seals",
    "validate_manifest",
]
