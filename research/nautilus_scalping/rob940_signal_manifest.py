"""ROB-943 (H3, ROB-940) — exact frozen 24-row signal manifest (pure, stdlib).

Fable Q1=A (``orch-fable-answer-strategy-20260717.md``): the 12-config/strategy
shortlist below (24 rows total) is byte-stable and frozen exactly as approved
— row order, param values, and the ex-ante hypothesis phrases from the
consult doc (``strategy-fable-consult-20260717-123049.md``). Any change to
this manifest is a NEW campaign lineage, not an edit.

S1-07 (payoff ratio 1.50, SL floor 45bp -> TP 67.5bp) sits below the
execution-layer 68bp gate and can be a structural no-trade config in
low-volatility regimes. It is retained deliberately (orch: "저변동 체제에서
거래 불가한 payoff 설정"이라는 해석 가능한 가설) — do not delete it.

This module owns identity/registration only: NO signal math, NO 81-grid
generator API, NO parameter search. ``signal_manifest_hash`` identifies the
24-row manifest itself; it is NOT the H4 full-campaign hash (walk-forward
window/cost model/selection rule are separate identity components there) —
do not conflate the two.

No DB/network/app/broker/random/current-time imports — pure stdlib plus the
existing research_contracts canonical-hash authority, deterministic given no
input (the manifest is a fixed literal).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from research_contracts.canonical_hash import canonical_sha256

# AC8/ROB-943 spec: four symbols share IDENTICAL code/config. No per-symbol
# override exists anywhere in this module (no BTC-only threshold path).
SYMBOLS: tuple[str, ...] = ("BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT")

# Exact 3-value exploration domains per free parameter (ROB-940 research
# draft table). Explored ex-ante shortlist rows (below) must draw every
# param value from these sets — no 81-grid generator, this is validation
# only for the pre-registered 12 rows/strategy.
S1_DOMAINS: dict[str, tuple[float, ...]] = {
    "L": (12, 16, 24),
    "q_min": (1.00, 1.25, 1.50),
    "k_SL": (1.00, 1.25, 1.50),
    "R_TP": (1.50, 1.80, 2.00),
}
S2_DOMAINS: dict[str, tuple[float, ...]] = {
    "z_min": (2.75, 3.00, 3.25),
    "v_min": (1.50, 2.00, 2.50),
    "ER_max": (0.25, 0.35, 0.45),
    "R_min": (1.20, 1.25, 1.35),
}


@dataclass(frozen=True)
class S1Config:
    L: int
    q_min: float
    k_SL: float
    R_TP: float
    config_id: str
    hypothesis: str


@dataclass(frozen=True)
class S2Config:
    z_min: float
    v_min: float
    ER_max: float
    R_min: float
    config_id: str
    hypothesis: str


# ultrathink: fixed (non-tunable) constants shared by every config row of a
# strategy. Kept OUT of signal_manifest_hash (that hash identifies the
# 24-row config shortlist specifically) but validated by dedicated tests so
# a silent edit to e.g. the ATR period or the S2 288-bar window fails a test
# rather than drifting unnoticed.
class FrozenSignalConstants(NamedTuple):
    ATR_PERIOD: int
    VOLUME_MEDIAN_WINDOW: int
    A_T_MIN: float
    A_T_MAX: float
    CHASE_MAX_ATR_MULT: float
    S1_SL_CLIP_MIN_BPS: float
    S1_SL_CLIP_MAX_BPS: float
    S1_TIMEOUT_1M_BARS: int
    S1_COOLDOWN_1M_BARS: int
    S2_MAD_WINDOW: int
    S2_ER_WINDOW: int
    S2_SHOCK_ABS_RETURN_MIN: float
    S2_SL_CLIP_MIN_BPS: float
    S2_SL_CLIP_MAX_BPS: float
    S2_TP_MAX_BPS: float
    S2_TP_ABS_FLOOR_BPS: float
    S2_TIMEOUT_1M_BARS: int
    S2_COOLDOWN_1M_BARS: int


FrozenSignalConstants = FrozenSignalConstants(
    ATR_PERIOD=20,
    VOLUME_MEDIAN_WINDOW=20,
    A_T_MIN=0.002,
    A_T_MAX=0.012,
    CHASE_MAX_ATR_MULT=0.50,
    S1_SL_CLIP_MIN_BPS=45.0,
    S1_SL_CLIP_MAX_BPS=110.0,
    S1_TIMEOUT_1M_BARS=12 * 15,  # 180
    S1_COOLDOWN_1M_BARS=4 * 15,  # 60
    S2_MAD_WINDOW=288,
    S2_ER_WINDOW=48,
    S2_SHOCK_ABS_RETURN_MIN=0.006,
    S2_SL_CLIP_MIN_BPS=45.0,
    S2_SL_CLIP_MAX_BPS=90.0,
    S2_TP_MAX_BPS=120.0,
    S2_TP_ABS_FLOOR_BPS=68.0,
    S2_TIMEOUT_1M_BARS=6 * 5,  # 30
    S2_COOLDOWN_1M_BARS=60,  # 1h == 60 * 1m bars
)


FROZEN_S1_CONFIGS: tuple[S1Config, ...] = (
    S1Config(16, 1.25, 1.25, 1.80, "S1-00", "연구 default"),
    S1Config(12, 1.25, 1.25, 1.80, "S1-01", "shorter breakout lookback"),
    S1Config(24, 1.25, 1.25, 1.80, "S1-02", "longer breakout lookback"),
    S1Config(16, 1.00, 1.25, 1.80, "S1-03", "looser volume confirmation"),
    S1Config(16, 1.50, 1.25, 1.80, "S1-04", "stricter volume confirmation"),
    S1Config(16, 1.25, 1.00, 1.80, "S1-05", "tighter ATR stop"),
    S1Config(16, 1.25, 1.50, 1.80, "S1-06", "wider ATR stop"),
    S1Config(16, 1.25, 1.25, 1.50, "S1-07", "lower payoff ratio"),
    S1Config(16, 1.25, 1.25, 2.00, "S1-08", "higher payoff ratio"),
    S1Config(12, 1.50, 1.25, 1.80, "S1-09", "fast breakout requires stronger volume"),
    S1Config(24, 1.00, 1.25, 1.80, "S1-10", "slow breakout tolerates weaker volume"),
    S1Config(16, 1.25, 1.00, 2.00, "S1-11", "tight-stop/high-payoff cost resilience"),
)

FROZEN_S2_CONFIGS: tuple[S2Config, ...] = (
    S2Config(3.00, 2.00, 0.35, 1.25, "S2-00", "연구 default"),
    S2Config(2.75, 2.00, 0.35, 1.25, "S2-01", "lower shock threshold"),
    S2Config(3.25, 2.00, 0.35, 1.25, "S2-02", "higher shock threshold"),
    S2Config(3.00, 1.50, 0.35, 1.25, "S2-03", "looser volume spike"),
    S2Config(3.00, 2.50, 0.35, 1.25, "S2-04", "stricter volume spike"),
    S2Config(3.00, 2.00, 0.25, 1.25, "S2-05", "stricter mean-reversion regime"),
    S2Config(3.00, 2.00, 0.45, 1.25, "S2-06", "looser regime filter"),
    S2Config(3.00, 2.00, 0.35, 1.20, "S2-07", "lower reward floor"),
    S2Config(3.00, 2.00, 0.35, 1.35, "S2-08", "higher reward floor"),
    S2Config(2.75, 1.50, 0.45, 1.20, "S2-09", "permissive/frequency frontier"),
    S2Config(3.25, 2.50, 0.25, 1.35, "S2-10", "selective/quality frontier"),
    S2Config(
        2.75, 2.50, 0.25, 1.25, "S2-11", "lower z only when volume/regime are strict"
    ),
)


def validate_s1_configs(rows: tuple[S1Config, ...]) -> None:
    """Fail-closed structural + domain validation for an S1 config tuple."""
    if len(rows) != 12:
        raise ValueError(f"S1 manifest must have exactly 12 rows, got {len(rows)}")
    seen_ids: set[str] = set()
    for row in rows:
        if row.config_id in seen_ids:
            raise ValueError(f"duplicate S1 config_id {row.config_id!r}")
        seen_ids.add(row.config_id)
        if row.L not in S1_DOMAINS["L"]:
            raise ValueError(f"S1 {row.config_id}: L={row.L!r} outside domain")
        if row.q_min not in S1_DOMAINS["q_min"]:
            raise ValueError(f"S1 {row.config_id}: q_min={row.q_min!r} outside domain")
        if row.k_SL not in S1_DOMAINS["k_SL"]:
            raise ValueError(f"S1 {row.config_id}: k_SL={row.k_SL!r} outside domain")
        if row.R_TP not in S1_DOMAINS["R_TP"]:
            raise ValueError(f"S1 {row.config_id}: R_TP={row.R_TP!r} outside domain")


def validate_s2_configs(rows: tuple[S2Config, ...]) -> None:
    """Fail-closed structural + domain validation for an S2 config tuple."""
    if len(rows) != 12:
        raise ValueError(f"S2 manifest must have exactly 12 rows, got {len(rows)}")
    seen_ids: set[str] = set()
    for row in rows:
        if row.config_id in seen_ids:
            raise ValueError(f"duplicate S2 config_id {row.config_id!r}")
        seen_ids.add(row.config_id)
        if row.z_min not in S2_DOMAINS["z_min"]:
            raise ValueError(f"S2 {row.config_id}: z_min={row.z_min!r} outside domain")
        if row.v_min not in S2_DOMAINS["v_min"]:
            raise ValueError(f"S2 {row.config_id}: v_min={row.v_min!r} outside domain")
        if row.ER_max not in S2_DOMAINS["ER_max"]:
            raise ValueError(
                f"S2 {row.config_id}: ER_max={row.ER_max!r} outside domain"
            )
        if row.R_min not in S2_DOMAINS["R_min"]:
            raise ValueError(f"S2 {row.config_id}: R_min={row.R_min!r} outside domain")


validate_s1_configs(FROZEN_S1_CONFIGS)
validate_s2_configs(FROZEN_S2_CONFIGS)

_S1_BY_ID: dict[str, S1Config] = {c.config_id: c for c in FROZEN_S1_CONFIGS}
_S2_BY_ID: dict[str, S2Config] = {c.config_id: c for c in FROZEN_S2_CONFIGS}


def get_s1_config(config_id: str) -> S1Config:
    """Look up a frozen S1 config by id; unregistered ids fail closed (KeyError)."""
    return _S1_BY_ID[config_id]


def get_s2_config(config_id: str) -> S2Config:
    """Look up a frozen S2 config by id; unregistered ids fail closed (KeyError)."""
    return _S2_BY_ID[config_id]


def _validate_symbol(symbol: str) -> None:
    if symbol not in SYMBOLS:
        raise ValueError(
            f"unknown symbol {symbol!r}; must be one of {SYMBOLS} "
            "(no per-symbol config override exists)"
        )


def resolve_s1_config_for_symbol(config_id: str, symbol: str) -> S1Config:
    """Same config for every symbol — this function exists ONLY to make the
    "no symbol override" invariant an explicit, testable call site rather
    than an implicit assumption callers must remember on their own.
    """
    _validate_symbol(symbol)
    return get_s1_config(config_id)


def resolve_s2_config_for_symbol(config_id: str, symbol: str) -> S2Config:
    _validate_symbol(symbol)
    return get_s2_config(config_id)


def _manifest_payload(
    s1_rows: tuple[S1Config, ...], s2_rows: tuple[S2Config, ...]
) -> dict:
    return {
        "symbols": list(SYMBOLS),
        "s1": [
            {
                "config_id": c.config_id,
                "L": c.L,
                "q_min": c.q_min,
                "k_SL": c.k_SL,
                "R_TP": c.R_TP,
                "hypothesis": c.hypothesis,
            }
            for c in s1_rows
        ],
        "s2": [
            {
                "config_id": c.config_id,
                "z_min": c.z_min,
                "v_min": c.v_min,
                "ER_max": c.ER_max,
                "R_min": c.R_min,
                "hypothesis": c.hypothesis,
            }
            for c in s2_rows
        ],
    }


# AC6/§4: identity hash of the 24-row manifest, named ``signal_manifest_hash``
# specifically so it is never confused with an H4 full-campaign hash (which
# will additionally cover the walk-forward window, cost scenarios, and
# selection rule as separate identity components).
signal_manifest_hash: str = canonical_sha256(
    _manifest_payload(FROZEN_S1_CONFIGS, FROZEN_S2_CONFIGS)
)
